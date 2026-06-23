from __future__ import annotations

import importlib.util
from decimal import Decimal
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "migration" / "refresh_legacy_ruida_from_sqlserver.py"


def load_script_module():
    spec = importlib.util.spec_from_file_location("refresh_legacy_ruida_from_sqlserver", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load script: {SCRIPT_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_default_arguments_are_read_only_dry_run():
    module = load_script_module()

    args = module.build_parser().parse_args([])

    assert args.apply is False
    assert args.allow_main_sandbox is False
    assert args.confirm_apply is None


def test_sqlite_path_alias_selects_explicit_target():
    module = load_script_module()

    args = module.build_parser().parse_args(["--sqlite-path", "data/example.sqlite3"])

    assert args.target_sqlite == Path("data/example.sqlite3")
    assert args.apply is False


def test_compute_missing_ids_is_set_based_and_idempotent():
    module = load_script_module()

    result = module.compute_missing_ids([1, 2, 3, 3], [1, 2, 4])

    assert result.source_only == (3,)
    assert result.target_only == (4,)
    assert result.source_duplicates == (3,)
    assert result.target_duplicates == ()


def test_apply_rejects_main_sandbox_without_extra_confirmation(tmp_path: Path):
    module = load_script_module()
    main_db = tmp_path / "data" / "carton_erp.sqlite3"
    main_db.parent.mkdir(parents=True)
    main_db.write_bytes(b"sqlite-placeholder")

    with pytest.raises(module.SafetyError, match="allow-main-sandbox"):
        module.validate_apply_safety(
            apply=True,
            target_sqlite=main_db,
            canonical_sqlite=main_db,
            allow_main_sandbox=False,
            confirm_apply=module.APPLY_CONFIRMATION,
        )


def test_apply_requires_exact_confirmation_phrase(tmp_path: Path):
    module = load_script_module()
    target_db = tmp_path / "sandbox-copy.sqlite3"
    target_db.write_bytes(b"sqlite-placeholder")

    with pytest.raises(module.SafetyError, match="confirm-apply"):
        module.validate_apply_safety(
            apply=True,
            target_sqlite=target_db,
            canonical_sqlite=tmp_path / "data" / "carton_erp.sqlite3",
            allow_main_sandbox=False,
            confirm_apply="wrong",
        )


def test_insert_sql_uses_unique_source_key_conflict_guard():
    module = load_script_module()

    sql = module.build_insert_sql(
        table="legacy_ruida_orders",
        columns=("legacy_order_id", "customer_name"),
        conflict_key="legacy_order_id",
    )

    assert "ON CONFLICT(legacy_order_id) DO NOTHING" in sql
    assert sql.startswith("INSERT INTO legacy_ruida_orders")


def test_help_contains_dry_run_apply_and_rollback_examples():
    module = load_script_module()

    help_text = module.build_parser().format_help()

    assert "Dry-run:" in help_text
    assert "Apply to an isolated copy:" in help_text
    assert "Rollback:" in help_text


def test_sqlite_value_converts_decimal_before_binding():
    module = load_script_module()

    assert module.sqlite_value(Decimal("12.3456")) == 12.3456
