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


def test_factory_update_reports_current_release_version() -> None:
    from app.version import APP_CHANGELOG, APP_VERSION, APP_VERSION_NAME

    assert APP_VERSION == "v0.22.15"
    assert "\u9001\u8d27\u5355\u8fde\u7eed\u7eb8" in APP_VERSION_NAME
    assert "\u5206\u9875\u6253\u5370" in APP_VERSION_NAME
    assert any(
        "\u5ba2\u6237" in item
        and "\u5e38\u7528\u7bb1" in item
        and "\u6750\u8d28" in item
        and "\u7248\u672c\u5386\u53f2" in item
        for item in APP_CHANGELOG
    )
    assert any(
        "\u4fee\u6539\u539f\u56e0" in item
        and "\u64cd\u4f5c\u8005" in item
        for item in APP_CHANGELOG
    )
    assert any(
        "\u5e76\u53d1\u51b2\u7a81" in item
        and "409" in item
        for item in APP_CHANGELOG
    )
    assert any(
        "\u5f02\u5e38\u53d8\u66f4" in item
        and "\u4e8c\u6b21\u786e\u8ba4" in item
        for item in APP_CHANGELOG
    )
    assert any(
        "\u7ba1\u7406\u5458\u6062\u590d" in item
        and "\u65b0\u7248\u672c" in item
        and "\u6062\u590d\u539f\u56e0" in item
        for item in APP_CHANGELOG
    )
    assert any(
        "\u654f\u611f\u4ef7\u683c" in item
        and "\u6743\u9650" in item
        and "\u8131\u654f" in item
        for item in APP_CHANGELOG
    )
    assert any(
        "\u6587\u672c\u5c42" in item
        and "OCR" in item
        and "\u6570\u91cf" in item
        and "\u91d1\u989d" in item
        for item in APP_CHANGELOG
    )
    assert any(
        "\u53ea\u9700\u9009\u62e9\u5ba2\u6237" in item
        and "\u4e09\u697c\u5177\u4f53\u8d27\u4f4d" in item
        for item in APP_CHANGELOG
    )
    assert any("396" in item and "\u9884\u8bbe\u8d27\u4f4d" in item for item in APP_CHANGELOG)
    assert any("F2" in item and "F3" in item and "F4" in item for item in APP_CHANGELOG)
    assert any(
        "\u6309\u6574\u5f20\u9001\u8d27\u5355\u9009\u62e9" in item
        and "\u5168\u90e8\u660e\u7ec6" in item
        for item in APP_CHANGELOG
    )
    assert any("\u6708\u7ed3\u7ed3\u8f6c\u65e5" in item and "20" in item for item in APP_CHANGELOG)
    assert any("\u5f85\u5bf9\u8d26" in item and "\u6309\u5ba2\u6237+\u6708\u4efd\u6c47\u603b" in item for item in APP_CHANGELOG)
    assert any("\u4e00\u952e\u542f\u52a8" in item for item in APP_CHANGELOG)
    assert any("\u684c\u9762\u56fe\u6807\u5c31\u80fd\u81ea\u52a8\u5347\u7ea7" in item for item in APP_CHANGELOG)


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


def test_update_script_reads_local_version_without_public_fingerprint_endpoint() -> None:
    assert "from app.version import APP_VERSION, APP_VERSION_NAME" in UPDATE_SCRIPT
    assert "/api/system/version" not in UPDATE_SCRIPT
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
