from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi import FastAPI


def _create_database(path: Path, marker: str) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE marker (value TEXT NOT NULL)")
        connection.execute("INSERT INTO marker VALUES (?)", (marker,))
        connection.commit()


def _read_marker(path: Path) -> str:
    with sqlite3.connect(path) as connection:
        return str(connection.execute("SELECT value FROM marker").fetchone()[0])


def test_promote_database_backs_up_target_before_atomic_replacement(
    tmp_path: Path,
) -> None:
    from scripts.deploy_production import promote_database

    source = tmp_path / "preview.sqlite3"
    target = tmp_path / "data" / "carton_erp.sqlite3"
    backup_dir = tmp_path / "nas"
    target.parent.mkdir()
    _create_database(source, "preview")
    _create_database(target, "production-before")

    result = promote_database(
        source=source,
        target=target,
        backup_dir=backup_dir,
    )

    assert _read_marker(target) == "preview"
    assert result.backup_path is not None
    assert _read_marker(result.backup_path) == "production-before"
    assert result.integrity_check == "ok"
    assert not target.with_suffix(".sqlite3.deploying").exists()


def test_promote_same_database_only_backs_up_and_validates(
    tmp_path: Path,
) -> None:
    from scripts.deploy_production import promote_database

    target = tmp_path / "carton_erp.sqlite3"
    _create_database(target, "validated")

    result = promote_database(
        source=target,
        target=target,
        backup_dir=tmp_path / "nas",
    )

    assert result.replaced is False
    assert _read_marker(target) == "validated"
    assert result.backup_path is not None


def test_invalid_preview_database_never_replaces_target(tmp_path: Path) -> None:
    from scripts.deploy_production import promote_database

    source = tmp_path / "broken.sqlite3"
    target = tmp_path / "carton_erp.sqlite3"
    source.write_text("not sqlite", encoding="utf-8")
    _create_database(target, "keep-me")

    with pytest.raises(RuntimeError, match="完整性"):
        promote_database(
            source=source,
            target=target,
            backup_dir=tmp_path / "nas",
        )

    assert _read_marker(target) == "keep-me"


def test_write_env_file_preserves_unrelated_values(tmp_path: Path) -> None:
    from scripts.deploy_production import write_env_file

    env_path = tmp_path / ".env"
    env_path.write_text(
        "CUSTOM_FLAG=keep\nERP_ENVIRONMENT=development\n",
        encoding="utf-8",
    )

    write_env_file(
        env_path,
        {
            "ERP_ENVIRONMENT": "production",
            "ERP_DATABASE_PATH": r"D:\ERP\data\carton_erp.sqlite3",
        },
    )

    content = env_path.read_text(encoding="utf-8")
    assert "CUSTOM_FLAG=keep" in content
    assert "ERP_ENVIRONMENT=production" in content
    assert content.count("ERP_ENVIRONMENT=") == 1
    assert r"ERP_DATABASE_PATH=D:\ERP\data\carton_erp.sqlite3" in content


def test_production_security_removes_docs_and_uses_private_lan_regex(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.config import load_settings
    from app.main import apply_production_security

    monkeypatch.setenv("ERP_ENVIRONMENT", "production")
    monkeypatch.setenv("ERP_SECRET_KEY", "phase13-secret")
    settings = load_settings()
    app = FastAPI()

    apply_production_security(app, settings)

    paths = {route.path for route in app.routes}
    assert "/docs" not in paths
    assert "/redoc" not in paths
    assert "/openapi.json" not in paths
    assert settings.is_production is True
    assert "192\\.168\\." in settings.allowed_origin_regex


def test_start_batch_uses_project_venv_one_worker_and_production_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.config import load_settings

    project_root = Path(__file__).resolve().parents[1]
    bat = (project_root / "start_erp.bat").read_text(encoding="utf-8")
    launcher = (
        project_root / "scripts" / "windows" / "start_erp.ps1"
    ).read_text(encoding="utf-8")

    assert "scripts\\windows\\start_erp.bat" in bat
    assert ".venv\\Scripts\\python.exe" in launcher
    assert "app.main:app" in launcher
    assert '"--host", "127.0.0.1"' in launcher
    assert '"--port", "8000"' in launcher
    assert '"--workers", "1"' in launcher
    assert "erp_server.log" in launcher
    monkeypatch.setenv("ERP_ENVIRONMENT", "production")
    assert load_settings().is_production is True
