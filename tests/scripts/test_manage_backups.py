from __future__ import annotations

import gc
import importlib.util
import sqlite3
import sys
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[2] / "scripts" / "admin" / "manage_backups.py"
spec = importlib.util.spec_from_file_location("manage_backups", MODULE_PATH)
module = importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules[spec.name] = module
spec.loader.exec_module(module)


def _make_backup_file(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE IF NOT EXISTS marker (id INTEGER PRIMARY KEY)")
    connection.execute("INSERT INTO marker DEFAULT VALUES")
    connection.commit()
    connection.close()
    del connection
    gc.collect()


def test_manage_backups_default_keep_is_five() -> None:
    args = module.build_parser().parse_args(["list"])
    assert args.keep == 5
    assert args.dry_run is False
    assert args.apply is False


def test_manage_backups_cli_cleanup_respects_protected_keywords(tmp_path: Path) -> None:
    backup_dir = tmp_path / "backups"
    for index in range(7):
        _make_backup_file(
            backup_dir / f"carton_erp_before_user_20260620_18{index:02d}00.sqlite3"
        )
    protected = backup_dir / "carton_erp_before_tianhua_batch_2_apply_20260620_074551.sqlite3"
    _make_backup_file(protected)

    result = module.run_command(
        command="cleanup",
        backup_dir=backup_dir,
        keep=5,
        apply=False,
    )

    assert result["regular_count"] == 7
    assert result["protected_count"] == 1
    assert len(result["delete_files"]) == 2
    assert protected.name in result["protected_files"]
    assert protected.exists()


def test_manage_backups_missing_directory_exits_safely(tmp_path: Path) -> None:
    missing = tmp_path / "missing-backups"

    result = module.run_command(
        command="cleanup",
        backup_dir=missing,
        keep=5,
        apply=False,
    )

    assert result["exists"] is False
    assert result["total_sqlite_backups"] == 0
    assert result["delete_files"] == []


def test_manage_backups_cleanup_does_not_touch_main_db_or_sandboxes(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    backup_dir = data_dir / "backups"
    sandbox_dir = data_dir / "sandboxes"
    main_db = data_dir / "carton_erp.sqlite3"
    sandbox_db = sandbox_dir / "copy.sqlite3"

    _make_backup_file(main_db)
    _make_backup_file(sandbox_db)
    for index in range(7):
        _make_backup_file(
            backup_dir / f"carton_erp_before_user_20260620_19{index:02d}00.sqlite3"
        )

    result = module.run_command(
        command="cleanup",
        backup_dir=backup_dir,
        keep=5,
        apply=True,
    )

    assert result["deleted_count"] == 2
    assert main_db.exists()
    assert sandbox_db.exists()
