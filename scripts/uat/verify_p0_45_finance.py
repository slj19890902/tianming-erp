"""Run the bounded P0-45 financial regression gate on disposable pytest data.

An optional explicit --database adds a read-only, all-date price check after
tests. It never applies prices, generates statements, migrates or deploys.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[2]
TESTS = [
    "tests/test_p0_45_monthly_completeness_gate.py",
    "tests/test_p0_45_monthly_completeness_frontend.py",
    "tests/test_p0_45_monthly_transaction_guard.py",
    "tests/test_p0_45_paid_statement_guard.py",
    "tests/test_p0_45_payable_link_guard.py",
    "tests/test_p0_45_receipt_price_gate.py",
    "tests/test_p0_45_supplier_receipt_price_health.py",
    "tests/test_p0_45_receipt_price_frontend.py",
    "tests/test_p0_39_supplier_receipt_price_facts.py",
    "tests/test_p0_22_incoming_frontend.py",
    "tests/test_p1_91v_incoming_receive_409_busy.py",
    "tests/test_stock_replenishment_flow.py::test_historical_replenishment_requires_priced_receipt_instead_of_direct_stock",
    "tests/test_stock_replenishment_flow.py::test_replenishment_missing_price_rolls_back_every_fact_and_same_key_can_retry",
    "tests/test_stock_replenishment_flow.py::test_replenishment_auto_stages_material_without_location_choice",
    "tests/test_stock_replenishment_flow.py::test_liner_stock_warning_can_create_draft_and_receive_as_finished",
    "tests/test_stock_replenishment_flow.py::test_liner_replenishment_fails_atomically_when_f34_and_f12_are_full",
    "tests/test_p1_81_receipt_purpose_flow.py::test_receipt_fact_uses_formal_price_not_master_quote_and_binds_idempotency",
    "tests/test_p1_81_receipt_purpose_flow.py::test_square_meter_price_uses_frozen_dimensions_and_cost_conserves",
    "tests/test_p1_81_receipt_purpose_flow.py::test_audit_failure_rolls_back_receipt_allocation_completion_and_inventory",
    "tests/test_p1_81_receipt_purpose_flow.py::test_receive_idempotency_binds_payload_and_actor_without_double_counting",
    "tests/test_p1_81_receipt_purpose_flow.py::test_frozen_receipt_revert_replays_same_actor_payload_and_key",
    "tests/test_p1_132_supplier_monthly_settlement.py",
    "tests/test_p1_149_supplier_settlement.py::test_supplier_period_generation_waits_for_the_complete_cutoff_day",
    "tests/test_p1_149_supplier_settlement.py::test_regenerate_creates_revision_n_plus_one_and_keeps_old_statement_and_lines",
    "tests/test_p1_149_supplier_settlement.py::test_payment_batch_applies_existing_credit_acceptance_and_bank_atomically",
    "tests/test_finance_manual_cas_idempotency.py::test_partial_and_full_settlement_increment_version_once_per_fact",
    "tests/test_finance_manual_cas_idempotency.py::test_audit_failure_rolls_back_statement_business_and_idempotency_fact",
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, help="explicit SQLite file for the additional read-only health gate")
    args = parser.parse_args()
    report_dir = ROOT / "data" / "p0_45_verification"
    report_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="p045-finance-regression-") as scratch:
        environment = dict(os.environ)
        environment.update(
            ERP_DATABASE_PATH=str(Path(scratch) / "test.sqlite3"),
            ERP_BACKUP_DIR=str(Path(scratch) / "backups"),
            ERP_SECRET_KEY_FILE=str(Path(scratch) / "secret.key"),
            ERP_SECRET_KEY="p045-disposable-regression-only",
            ERP_ENVIRONMENT="test",
            PYTHONUTF8="1",
            PYTHONIOENCODING="utf-8",
        )
        result = subprocess.run([
            sys.executable, "-m", "pytest", *TESTS, "-q", "--tb=short",
            f"--junitxml={report_dir / 'regression.xml'}",
        ], cwd=ROOT, env=environment)
    if result.returncode:
        return result.returncode
    if args.database is not None:
        return subprocess.run([
            sys.executable, str(ROOT / "scripts/admin/check_supplier_receipt_prices.py"),
            "--database", str(args.database.expanduser().resolve()), "--json",
        ], cwd=ROOT).returncode
    print("Regression gate passed. No business database was scanned; use --database for the read-only data gate.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
