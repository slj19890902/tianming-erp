from __future__ import annotations

import sqlite3
from collections.abc import Generator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker


def _write_marker(database_path: Path, value: str) -> None:
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS restore_marker (value TEXT NOT NULL)"
        )
        connection.execute("DELETE FROM restore_marker")
        connection.execute(
            "INSERT INTO restore_marker (value) VALUES (?)",
            (value,),
        )
        connection.commit()


def _read_marker(database_path: Path) -> str:
    with sqlite3.connect(database_path) as connection:
        return str(
            connection.execute(
                "SELECT value FROM restore_marker"
            ).fetchone()[0]
        )


def test_restore_forces_verified_pre_restore_backup(tmp_path: Path) -> None:
    from app.core.database import backup_to_nas, restore_from_backup

    database_path = tmp_path / "carton_erp.sqlite3"
    backup_dir = tmp_path / "nas"
    _write_marker(database_path, "backup-version")
    source_backup = backup_to_nas(
        source_path=database_path,
        backup_dir=backup_dir,
    )
    _write_marker(database_path, "current-version")

    result = restore_from_backup(
        source_backup.path,
        target_path=database_path,
        backup_dir=backup_dir,
    )

    assert result.integrity_check == "ok"
    assert result.emergency_backup.name.endswith("_pre_restore.sqlite3")
    assert _read_marker(database_path) == "backup-version"
    assert _read_marker(result.emergency_backup) == "current-version"


@pytest.fixture()
def system_api_app(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    from app.api.auth import router as auth_router
    from app.api.deps import get_db
    from app.api.system import router as system_router
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.user import User

    database_path = tmp_path / "system.sqlite3"
    backup_dir = tmp_path / "nas"
    monkeypatch.setenv("ERP_DATABASE_PATH", str(database_path))
    monkeypatch.setenv("ERP_BACKUP_DIR", str(backup_dir))
    monkeypatch.setenv("ERP_SECRET_KEY", "phase9-system-secret")
    engine = create_sqlite_engine(database_path)
    Base.metadata.create_all(engine)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"
        )
        connection.execute(
            "INSERT INTO alembic_version VALUES ('f4b2c9d7a110')"
        )
        connection.commit()
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        session.add_all(
            User(
                username=role,
                password_hash=hash_password("RolePass123!"),
                role=role,
                real_name=role,
                must_change_password=False,
            )
            for role in ("admin", "finance")
        )
        session.commit()

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(system_router, prefix="/api/system")

    def override_get_db() -> Generator[Session, None, None]:
        with factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    yield app, database_path, backup_dir
    engine.dispose()


def _login(client: TestClient, role: str) -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": role, "password": "RolePass123!"},
    )
    assert response.status_code == 200, response.text


def test_system_backup_api_is_admin_only_and_lists_created_backup(
    system_api_app,
) -> None:
    app, _, _ = system_api_app
    with TestClient(app) as client:
        _login(client, "finance")
        denied = client.get("/api/system/backups")
        _login(client, "admin")
        empty = client.get("/api/system/backups")
        created = client.post("/api/system/backups")
        listed = client.get("/api/system/backups")

    assert denied.status_code == 403
    assert empty.status_code == 200
    assert empty.json()["items"] == []
    assert created.status_code == 201, created.text
    assert listed.status_code == 200, listed.text
    assert listed.json()["items"][0]["filename"] == created.json()["filename"]
    assert listed.json()["items"][0]["size"] > 0


def test_restore_api_restores_data_and_writes_audit(system_api_app) -> None:
    app, database_path, backup_dir = system_api_app
    _write_marker(database_path, "backup-version")
    with TestClient(app) as client:
        _login(client, "admin")
        created = client.post("/api/system/backups")
        assert created.status_code == 201, created.text
        _write_marker(database_path, "current-version")
        restored = client.post(
            "/api/system/backups/restore",
            json={"filename": created.json()["filename"]},
        )

    assert restored.status_code == 200, restored.text
    assert restored.json()["integrity_check"] == "ok"
    emergency = backup_dir / restored.json()["pre_restore_backup"]
    assert emergency.name.endswith("_pre_restore.sqlite3")
    assert _read_marker(database_path) == "backup-version"
    assert _read_marker(emergency) == "current-version"
    with sqlite3.connect(database_path) as connection:
        action = connection.execute(
            "SELECT action FROM operation_logs "
            "WHERE action = 'RESTORE_DATABASE' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert action == ("RESTORE_DATABASE",)


@pytest.mark.parametrize(
    "filename",
    ["../outside.sqlite3", r"C:\outside.sqlite3", "backup.txt"],
)
def test_restore_rejects_unsafe_backup_filename(
    system_api_app,
    filename: str,
) -> None:
    app, _, _ = system_api_app
    with TestClient(app) as client:
        _login(client, "admin")
        response = client.post(
            "/api/system/backups/restore",
            json={"filename": filename},
        )

    assert response.status_code == 400


def _plant_fake_backups(backup_dir: Path, count: int) -> list[str]:
    """在备份目录直接创建 count 个哑备份文件（绕过 backup_to_nas 自动清理）。"""
    import time

    backup_dir.mkdir(parents=True, exist_ok=True)
    names = []
    for i in range(count):
        name = f"fake_backup_{i:04d}.sqlite3"
        path = backup_dir / name
        # 写入最小合法 SQLite 文件头（16 KB 零填充即可被统计大小）
        path.write_bytes(b"\x53\x51\x4c\x69\x74\x65\x20\x66\x6f\x72\x6d\x61\x74\x20\x33\x00" + b"\x00" * 1000)
        # 确保每个文件 mtime 递增，排序稳定
        mtime = 1_000_000 + i
        import os
        os.utime(path, (mtime, mtime))
        names.append(name)
    return names


def test_list_backups_returns_stats_and_protection_flags(system_api_app) -> None:
    """list_backups 包含统计字段，文件按修改时间最新排列，前5个标记 is_protected=True。"""
    app, _, backup_dir = system_api_app
    _plant_fake_backups(backup_dir, 6)
    with TestClient(app) as client:
        _login(client, "admin")
        listed = client.get("/api/system/backups")

    assert listed.status_code == 200, listed.text
    data = listed.json()
    assert data["total_count"] == 6
    assert data["protected_count"] == 5
    assert data["deletable_count"] == 1
    assert data["total_size"] > 0
    assert data["location_type"] in ("local", "nas")
    items = data["items"]
    assert len(items) == 6
    # 前5受保护，第6个可删除
    for item in items[:5]:
        assert item["is_protected"] is True
    assert items[5]["is_protected"] is False


def test_delete_single_backup_succeeds_for_deletable_file(system_api_app) -> None:
    """可以删除超出保护范围的备份，返回 freed_size，列表缩短。"""
    app, _, backup_dir = system_api_app
    _plant_fake_backups(backup_dir, 6)
    with TestClient(app) as client:
        _login(client, "admin")
        listed = client.get("/api/system/backups").json()
        deletable = next(f for f in listed["items"] if not f["is_protected"])
        deleted = client.delete(f"/api/system/backups/{deletable['filename']}")
        after = client.get("/api/system/backups").json()

    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["deleted"] == deletable["filename"]
    assert deleted.json()["freed_size"] > 0
    assert after["total_count"] == 5


def test_delete_protected_backup_is_rejected(system_api_app) -> None:
    """尝试删除受保护（最新5个之内）的备份，返回 409。"""
    app, _, backup_dir = system_api_app
    _plant_fake_backups(backup_dir, 3)
    with TestClient(app) as client:
        _login(client, "admin")
        listed = client.get("/api/system/backups").json()
        protected_name = listed["items"][0]["filename"]
        response = client.delete(f"/api/system/backups/{protected_name}")

    assert response.status_code == 409
    assert "保护" in response.json()["detail"]


@pytest.mark.parametrize(
    "filename",
    [r"C:\absolute.sqlite3", "not-a-db.txt"],
)
def test_delete_rejects_unsafe_filenames(system_api_app, filename: str) -> None:
    """绝对路径、非 .sqlite3 扩展名均返回 400。"""
    app, _, _ = system_api_app
    with TestClient(app) as client:
        _login(client, "admin")
        response = client.delete(f"/api/system/backups/{filename}")

    assert response.status_code == 400


def test_delete_rejects_path_traversal(system_api_app) -> None:
    """路径遍历（../ 前缀）被 HTTP 层或后端拒绝，访问无效（返回 400 或 404）。"""
    app, _, _ = system_api_app
    with TestClient(app) as client:
        _login(client, "admin")
        # HTTP 层可能在路由前规范化路径，导致 404；后端校验返回 400；均可接受
        response = client.delete("/api/system/backups/../evil.sqlite3")
    assert response.status_code in (400, 404)


def test_cleanup_preview_shows_correct_split(system_api_app) -> None:
    """cleanup-preview 返回 will_keep（最新5个）和 will_delete（其余）。"""
    app, _, backup_dir = system_api_app
    _plant_fake_backups(backup_dir, 7)
    with TestClient(app) as client:
        _login(client, "admin")
        preview = client.get("/api/system/backups/cleanup-preview")

    assert preview.status_code == 200, preview.text
    data = preview.json()
    assert len(data["will_keep"]) == 5
    assert len(data["will_delete"]) == 2
    assert data["deletable_count"] == 2
    assert data["freed_size"] > 0


def test_cleanup_bulk_deletes_old_backups_and_logs_audit(system_api_app) -> None:
    """POST /backups/cleanup 删除旧备份，保留最新5个，写入审计日志。"""
    app, database_path, backup_dir = system_api_app
    _plant_fake_backups(backup_dir, 8)
    with TestClient(app) as client:
        _login(client, "admin")
        before = client.get("/api/system/backups").json()
        assert before["total_count"] == 8

        cleaned = client.post("/api/system/backups/cleanup")
        after = client.get("/api/system/backups").json()

    assert cleaned.status_code == 200, cleaned.text
    result = cleaned.json()
    assert len(result["deleted"]) == 3
    assert result["freed_size"] > 0
    assert result["errors"] == []
    assert after["total_count"] == 5
    # 审计日志已写入
    with sqlite3.connect(database_path) as connection:
        action = connection.execute(
            "SELECT action FROM operation_logs WHERE action='CLEANUP_BACKUPS' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert action == ("CLEANUP_BACKUPS",)


def test_delete_backup_writes_audit_log(system_api_app) -> None:
    """DELETE /backups/{filename} 写入 DELETE_BACKUP 审计记录。"""
    app, database_path, backup_dir = system_api_app
    _plant_fake_backups(backup_dir, 6)
    with TestClient(app) as client:
        _login(client, "admin")
        listed = client.get("/api/system/backups").json()
        deletable = next(f for f in listed["items"] if not f["is_protected"])
        client.delete(f"/api/system/backups/{deletable['filename']}")
    with sqlite3.connect(database_path) as connection:
        action = connection.execute(
            "SELECT action FROM operation_logs WHERE action='DELETE_BACKUP' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert action == ("DELETE_BACKUP",)


def test_wildcard_and_public_cors_origins_are_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.config import load_settings

    for value in ("*", "https://erp.example.com", "ftp://192.168.1.20"):
        monkeypatch.setenv("ERP_ALLOWED_ORIGINS", value)
        with pytest.raises(ValueError, match="ERP_ALLOWED_ORIGINS"):
            load_settings()

    monkeypatch.setenv(
        "ERP_ALLOWED_ORIGINS",
        "http://192.168.1.20:8000,http://10.0.0.8:8000",
    )
    assert load_settings().allowed_origins == (
        "http://192.168.1.20:8000",
        "http://10.0.0.8:8000",
    )


def test_version_changelog_requires_login(system_api_app) -> None:
    app, _, _ = system_api_app
    with TestClient(app) as client:
        denied_version = client.get("/api/system/version")
        denied = client.get("/api/system/version/changelog")
        _login(client, "admin")
        version = client.get("/api/system/version")
        allowed = client.get("/api/system/version/changelog")

    assert denied_version.status_code == 401
    assert denied.status_code == 401
    assert version.status_code == 200
    from app.version import current_release_metadata

    assert version.json() == current_release_metadata()
    assert allowed.status_code == 200
    assert isinstance(allowed.json()["changelog"], list)
