from __future__ import annotations

import sqlite3
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
UPDATE_SCRIPT = (
    PROJECT_ROOT / "scripts" / "admin" / "update_erp.ps1"
).read_text(encoding="utf-8")


def test_update_script_defaults_to_formal_database_and_backup_directory() -> None:
    assert 'Join-Path $projectRoot "data\\carton_erp.sqlite3"' in UPDATE_SCRIPT
    assert 'Join-Path $projectRoot "data\\backups"' in UPDATE_SCRIPT
    assert 'Join-Path $projectRoot "erp.db"' not in UPDATE_SCRIPT


def test_factory_update_hotfix_has_release_version() -> None:
    from app.version import APP_VERSION, APP_VERSION_NAME

    assert APP_VERSION == "v0.20.1"
    assert "安全更新" in APP_VERSION_NAME


def test_update_script_stops_service_before_database_backup() -> None:
    stop_position = UPDATE_SCRIPT.index('Write-Log "停止 ERP 服务..."')
    backup_position = UPDATE_SCRIPT.index("$backupOutput =")

    assert stop_position < backup_position


def test_update_script_uses_sqlite_backup_helper_not_file_copy() -> None:
    assert "pre_update_backup.py" in UPDATE_SCRIPT
    assert "Copy-Item -LiteralPath $dbPath" not in UPDATE_SCRIPT


def test_update_script_only_fast_forwards_formal_baseline_branch() -> None:
    assert "factory-current-baseline" in UPDATE_SCRIPT
    assert "git fetch origin factory-current-baseline" in UPDATE_SCRIPT
    assert "git merge --ff-only origin/factory-current-baseline" in UPDATE_SCRIPT
    assert (
        UPDATE_SCRIPT.index("git fetch origin factory-current-baseline")
        < UPDATE_SCRIPT.index('Write-Log "停止 ERP 服务..."')
        < UPDATE_SCRIPT.index("$backupOutput =")
        < UPDATE_SCRIPT.index("git merge --ff-only origin/factory-current-baseline")
    )


def test_update_script_reads_real_version_endpoint() -> None:
    assert "/api/system/version" in UPDATE_SCRIPT
    assert '"http://127.0.0.1:8000/api/version"' not in UPDATE_SCRIPT


def test_update_script_checks_backup_integrity_and_hash() -> None:
    assert "integrity_check" in UPDATE_SCRIPT
    assert "sha256" in UPDATE_SCRIPT


def test_pre_update_backup_helper_exists() -> None:
    helper = PROJECT_ROOT / "scripts" / "admin" / "pre_update_backup.py"
    assert helper.is_file()


def test_pre_update_backup_helper_creates_valid_sqlite_copy(tmp_path: Path) -> None:
    from scripts.admin.pre_update_backup import create_backup

    source = tmp_path / "carton_erp.sqlite3"
    backup_dir = tmp_path / "backups"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY, value TEXT)")
        connection.execute("INSERT INTO sample(value) VALUES ('工厂更新测试')")
        connection.commit()

    result = create_backup(source, backup_dir)
    backup = Path(result["path"])

    assert backup.is_file()
    assert backup.parent == backup_dir
    assert result["integrity_check"] == "ok"
    assert len(result["sha256"]) == 64
    with sqlite3.connect(backup) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("SELECT value FROM sample").fetchone()[0] == "工厂更新测试"
