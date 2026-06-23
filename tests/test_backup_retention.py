from __future__ import annotations

import gc
import sqlite3
from pathlib import Path


def _make_backup_file(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE IF NOT EXISTS marker (id INTEGER PRIMARY KEY, name TEXT)")
    connection.execute("INSERT INTO marker (name) VALUES ('backup')")
    connection.commit()
    connection.close()
    del connection
    gc.collect()


def test_backup_retention_classifies_regular_and_protected_backups(tmp_path: Path) -> None:
    from app.core.backup_retention import classify_backup_file

    regular = tmp_path / "carton_erp_before_account_rbac_hardening_20260620_150305.sqlite3"
    regular_lower_final = tmp_path / "carton_erp_before_final_password_handoff_20260620_154200.sqlite3"
    protected = tmp_path / "carton_erp_before_tianhua_batch_5_apply_20260620_134925.sqlite3"
    unknown = tmp_path / "notes.txt"
    _make_backup_file(regular)
    _make_backup_file(regular_lower_final)
    _make_backup_file(protected)
    unknown.write_text("skip", encoding="utf-8")

    assert classify_backup_file(regular).category == "regular"
    assert classify_backup_file(regular_lower_final).category == "regular"
    assert classify_backup_file(protected).category == "protected"
    assert classify_backup_file(unknown).category == "ignored"


def test_backup_retention_protects_all_required_keywords(tmp_path: Path) -> None:
    from app.core.backup_retention import classify_backup_file

    for filename in (
        "carton_erp_FINAL_AFTER_TIANHUA_MIGRATION_20260620_145234.sqlite3",
        "carton_erp_ARCHIVE_20260620_145234.sqlite3",
        "carton_erp_MIGRATION_20260620_145234.sqlite3",
        "carton_erp_before_tianhua_batch_2_apply_20260620_074551.sqlite3",
        "carton_erp_before_tianhua_sales_apply_20260619_202543.sqlite3",
        "carton_erp_before_legacy_refresh_apply_20260619_090705.sqlite3",
    ):
        path = tmp_path / filename
        _make_backup_file(path)
        assert classify_backup_file(path).category == "protected"


def test_backup_retention_plan_keeps_latest_five_regular_backups(tmp_path: Path) -> None:
    from app.core.backup_retention import build_cleanup_plan

    backup_dir = tmp_path / "backups"
    regular_files: list[Path] = []
    for index in range(7):
        path = backup_dir / f"carton_erp_before_account_rbac_hardening_20260620_15{index:02d}00.sqlite3"
        _make_backup_file(path)
        regular_files.append(path)
    protected = backup_dir / "carton_erp_before_tianhua_batch_4_apply_20260620_130056.sqlite3"
    _make_backup_file(protected)

    plan = build_cleanup_plan(backup_dir=backup_dir, keep=5)

    assert plan.total_sqlite_backups == 8
    assert plan.regular_count == 7
    assert plan.protected_count == 1
    assert len(plan.regular_keep) == 5
    assert len(plan.regular_delete) == 2
    assert protected in plan.protected_files
    assert regular_files[0] in plan.regular_delete
    assert regular_files[1] in plan.regular_delete


def test_backup_retention_apply_deletes_only_extra_regular_backups(tmp_path: Path) -> None:
    from app.core.backup_retention import apply_cleanup_plan, build_cleanup_plan

    backup_dir = tmp_path / "backups"
    for index in range(6):
        _make_backup_file(
            backup_dir / f"carton_erp_before_account_rbac_hardening_20260620_16{index:02d}00.sqlite3"
        )
    protected = backup_dir / "carton_erp_FINAL_AFTER_TIANHUA_MIGRATION_20260620_145234.sqlite3"
    _make_backup_file(protected)

    plan = build_cleanup_plan(backup_dir=backup_dir, keep=5)
    result = apply_cleanup_plan(plan, apply=True)

    assert result.deleted_count == 1
    assert protected.exists()
    remaining_regular = [
        path for path in backup_dir.glob("*.sqlite3") if "FINAL" not in path.name
    ]
    assert len(remaining_regular) == 5


def test_backup_retention_dry_run_does_not_delete_files(tmp_path: Path) -> None:
    from app.core.backup_retention import apply_cleanup_plan, build_cleanup_plan

    backup_dir = tmp_path / "backups"
    for index in range(8):
        _make_backup_file(
            backup_dir / f"carton_erp_before_password_reset_20260620_17{index:02d}00.sqlite3"
        )

    plan = build_cleanup_plan(backup_dir=backup_dir, keep=5)
    result = apply_cleanup_plan(plan, apply=False)

    assert result.deleted_count == 0
    assert len(list(backup_dir.glob("*.sqlite3"))) == 8


def test_backup_to_nas_auto_cleans_only_regular_backups(tmp_path: Path) -> None:
    from app.core.database import backup_to_nas

    source = tmp_path / "source.sqlite3"
    _make_backup_file(source)
    backup_dir = tmp_path / "backups"
    for index in range(6):
        _make_backup_file(
            backup_dir / f"carton_erp_before_account_rbac_hardening_20260620_19{index:02d}00.sqlite3"
        )
    protected = backup_dir / "carton_erp_before_tianhua_batch_1_apply_20260619_203207.sqlite3"
    _make_backup_file(protected)

    result = backup_to_nas(source_path=source, backup_dir=backup_dir)

    assert result.path.exists()
    assert result.cleanup_deleted_count >= 2
    remaining_regular = [
        path for path in backup_dir.glob("*.sqlite3") if "tianhua_batch" not in path.name
    ]
    assert len(remaining_regular) == 5
    assert protected.exists()


def test_backup_to_nas_failure_does_not_cleanup_existing_backups(tmp_path: Path) -> None:
    from app.core.database import backup_to_nas

    backup_dir = tmp_path / "backups"
    for index in range(6):
        _make_backup_file(
            backup_dir / f"carton_erp_before_user_20260620_20{index:02d}00.sqlite3"
        )

    before = {path.name for path in backup_dir.glob("*.sqlite3")}
    missing_source = tmp_path / "missing.sqlite3"

    try:
        backup_to_nas(source_path=missing_source, backup_dir=backup_dir)
    except FileNotFoundError:
        pass
    else:
        raise AssertionError("expected FileNotFoundError")

    after = {path.name for path in backup_dir.glob("*.sqlite3")}
    assert before == after
