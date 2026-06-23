from __future__ import annotations

import importlib.util
from argparse import Namespace
from decimal import Decimal
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "migration" / "migrate_legacy_ruida_to_sales_orders.py"


def load_module():
    spec = importlib.util.spec_from_file_location("formal_migration", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load migration script")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_defaults_are_dry_run_complete_orders_limit_100():
    module = load_module()
    args = module.build_parser().parse_args(["--sqlite-path", "copy.sqlite3"])
    assert args.apply is False
    assert args.limit == 100
    assert args.sample_mode == "complete_orders"


def test_apply_rejects_canonical_main_database():
    module = load_module()
    with pytest.raises(module.SafetyError, match="main database"):
        module.validate_apply_target(True, module.CANONICAL_SQLITE, module.CANONICAL_SQLITE)


def test_order_number_is_deterministic():
    module = load_module()
    assert module.order_number_for(42842) == "RUIDA-42842"


def test_money_uses_decimal_rounding():
    module = load_module()
    assert module.money("1.005") == Decimal("1.01")


def test_parser_accepts_multi_item_sample_mode():
    module = load_module()
    args = module.build_parser().parse_args(
        ["--sqlite-path", "copy.sqlite3", "--sample-mode", "multi_item_orders", "--limit", "20"]
    )
    assert args.sample_mode == "multi_item_orders"
    assert args.limit == 20


def test_parser_accepts_review_csv_and_customer_name():
    module = load_module()
    args = module.build_parser().parse_args(
        [
            "--sqlite-path", "copy.sqlite3",
            "--product-review-csv", "review.csv",
            "--customer-name", "苏州天华超净科技股份有限公司",
        ]
    )
    assert args.product_review_csv == Path("review.csv")
    assert args.customer_name == "苏州天华超净科技股份有限公司"


def test_review_loader_only_uses_approved_and_keeps_rejected_as_deny_list(tmp_path):
    module = load_module()
    review = tmp_path / "review.csv"
    review.write_text(
        "customer_id,legacy_style_no_raw,review_status,review_decision,"
        "approved_product_id,matched_product_id,review_note\n"
        "5,A / 箱,approved,approve_prefix_match,10,10,\n"
        "5,B / 箱,rejected,create_product_later,,,\n",
        encoding="utf-8",
    )
    approved, rejected, counts = module.load_product_review(review)
    assert approved == {(5, "A / 箱"): 10}
    assert rejected == {(5, "B / 箱")}
    assert counts == {"approved": 1, "rejected": 1}


def test_fee_item_detection_blocks_normal_product_migration():
    module = load_module()
    assert module.is_fee_item("ABC / 模具费")
    assert not module.is_fee_item("ABC / 普通外箱")


def test_parser_accepts_explicit_all_scope():
    module = load_module()
    args = module.build_parser().parse_args(
        [
            "--sqlite-path", "copy.sqlite3",
            "--product-review-csv", "review.csv",
            "--customer-name", "苏州天华超净科技股份有限公司",
            "--all",
        ]
    )
    assert args.all is True
    assert module.effective_limit(args) is None


def test_all_scope_requires_customer_and_review_csv():
    module = load_module()
    args = module.build_parser().parse_args(["--sqlite-path", "copy.sqlite3", "--all"])
    with pytest.raises(module.SafetyError, match="requires --customer-name"):
        module.validate_scope(args)


def main_apply_args(**overrides):
    values = {
        "apply": True,
        "allow_main_sandbox": False,
        "confirm_main_apply": None,
        "expected_source_db_sha256": None,
        "batch_manifest": None,
        "batch_id": None,
        "confirm_apply": None,
    }
    values.update(overrides)
    return Namespace(**values)


def test_main_apply_requires_confirmation_phrase():
    module = load_module()
    args = main_apply_args(
        allow_main_sandbox=True,
        expected_source_db_sha256="A" * 64,
        batch_manifest=Path("manifest.csv"),
        batch_id="batch_1",
    )
    with pytest.raises(module.SafetyError, match="confirm-main-apply"):
        module.validate_apply_authorization(args, module.CANONICAL_SQLITE, "A" * 64)


def test_main_apply_requires_expected_hash():
    module = load_module()
    args = main_apply_args(
        allow_main_sandbox=True,
        confirm_main_apply=module.MAIN_CONFIRMATION,
        batch_manifest=Path("manifest.csv"),
        batch_id="batch_1",
    )
    with pytest.raises(module.SafetyError, match="expected-source-db-sha256"):
        module.validate_apply_authorization(args, module.CANONICAL_SQLITE, "A" * 64)


def test_main_apply_rejects_hash_mismatch():
    module = load_module()
    args = main_apply_args(
        allow_main_sandbox=True,
        confirm_main_apply=module.MAIN_CONFIRMATION,
        expected_source_db_sha256="B" * 64,
        batch_manifest=Path("manifest.csv"),
        batch_id="batch_1",
    )
    with pytest.raises(module.SafetyError, match="SHA-256 mismatch"):
        module.validate_apply_authorization(args, module.CANONICAL_SQLITE, "A" * 64)


@pytest.mark.parametrize(
    ("manifest", "batch_id", "message"),
    [(None, "batch_1", "batch-manifest"), (Path("manifest.csv"), None, "batch-id")],
)
def test_main_apply_requires_manifest_and_batch_id(manifest, batch_id, message):
    module = load_module()
    args = main_apply_args(
        allow_main_sandbox=True,
        confirm_main_apply=module.MAIN_CONFIRMATION,
        expected_source_db_sha256="A" * 64,
        batch_manifest=manifest,
        batch_id=batch_id,
    )
    with pytest.raises(module.SafetyError, match=message):
        module.validate_apply_authorization(args, module.CANONICAL_SQLITE, "A" * 64)


def test_manifest_selection_is_fixed_and_does_not_slide():
    module = load_module()
    candidates = [
        {"order": {"legacy_order_id": 3}},
        {"order": {"legacy_order_id": 2}},
        {"order": {"legacy_order_id": 1}},
    ]
    selected = module.select_manifest_entries(candidates, [2, 1])
    assert [entry["order"]["legacy_order_id"] for entry in selected] == [2, 1]
    pending = module.pending_entries(selected, {2})
    assert [entry["order"]["legacy_order_id"] for entry in pending] == [1]


def test_sandbox_apply_keeps_existing_confirmation_flow(tmp_path):
    module = load_module()
    target = tmp_path / "copy.sqlite3"
    target.touch()
    args = main_apply_args(confirm_apply=module.CONFIRMATION)
    module.validate_apply_authorization(args, target, "A" * 64)
