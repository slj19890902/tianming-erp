"""Synthetic SQLite-only tests; runnable with unittest without application startup."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/admin/check_supplier_receipt_prices.py"
spec = importlib.util.spec_from_file_location("receipt_price_health", SCRIPT)
health = importlib.util.module_from_spec(spec)
spec.loader.exec_module(health)


SCHEMA = """
CREATE TABLE incoming_receipts(id INTEGER PRIMARY KEY, receipt_number, status, received_at);
CREATE TABLE incoming_receipt_items(id INTEGER PRIMARY KEY, receipt_id, status, received_quantity,
    supplier_order_item_id, requisition_item_id, stock_replenishment_item_id);
CREATE TABLE incoming_receipt_purpose_allocations(id INTEGER PRIMARY KEY, incoming_receipt_item_id,
    status, purpose_contract_status_snapshot, purchase_receipt_fact_id,
    purchase_purpose_source_snapshot_id, source_key, supplier_requisition_order_item_id,
    material_requisition_item_id, receipt_total_sheet_qty, purchase_unit_cost);
CREATE TABLE incoming_receipt_purpose_reversals(id INTEGER PRIMARY KEY, incoming_receipt_item_id,
    incoming_receipt_purpose_allocation_id);
CREATE TABLE incoming_receipt_reversal_facts(id INTEGER PRIMARY KEY, incoming_receipt_item_id);
CREATE TABLE purchase_receipt_facts(id INTEGER PRIMARY KEY, supplier_requisition_order_item_id,
    material_requisition_item_id, purchase_purpose_source_snapshot_id, source_key,
    actual_material_code_snapshot, unit_price, price_unit, currency, tax_included, tax_rate);
CREATE TABLE supplier_receipt_settlement_price_facts(id INTEGER PRIMARY KEY, incoming_receipt_item_id,
    supplier_id, supplier_name_snapshot, material_code_snapshot, source_kind,
    purchase_document_number_snapshot, receipt_number_snapshot, receipt_date_snapshot,
    received_quantity_snapshot, quantity_unit, report_length_mm, report_width_mm,
    unit_price, price_unit, currency, tax_included, tax_rate, shipping_fee_mode, fact_origin,
    match_strategy, finance_only_test_classification, adoption_reason, adoption_evidence_reference);
CREATE TABLE supplier_requisition_order_items(id INTEGER PRIMARY KEY, supplier_order_id,
    supplier_name_snapshot, report_length_mm, report_width_mm);
CREATE TABLE supplier_requisition_orders(id INTEGER PRIMARY KEY, order_number, supplier_name);
CREATE TABLE material_requisition_items(id INTEGER PRIMARY KEY, requisition_id, cardboard_len, cardboard_width);
CREATE TABLE material_requisitions(id INTEGER PRIMARY KEY, requisition_number, supplier_name);
CREATE TABLE stock_replenishment_order_items(id INTEGER PRIMARY KEY, replenishment_order_id,
    procurement_route_snapshot, target_inventory_type, product_id);
CREATE TABLE stock_replenishment_orders(id INTEGER PRIMARY KEY, order_number, supplier_name);
CREATE TABLE external_packaging_purchase_items(id INTEGER PRIMARY KEY, stock_replenishment_item_id);
CREATE TABLE products(id INTEGER PRIMARY KEY, box_style);
CREATE TABLE supplier_master_records(id INTEGER PRIMARY KEY, normalized_name);
CREATE TABLE supplier_master_aliases(id INTEGER PRIMARY KEY, supplier_id, normalized_alias);
"""


class SupplierReceiptPriceHealthTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="p0-45-price-health-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.database = self.directory / "isolated # 收料.sqlite3"
        self.db = sqlite3.connect(self.database)
        self.addCleanup(self.db.close)
        self.db.executescript(SCHEMA)
        self.insert("supplier_master_records", id=1, normalized_name="供应商甲")

    def insert(self, table, **values):
        self.db.execute(
            f"INSERT INTO {table} ({', '.join(values)}) VALUES ({', '.join('?' for _ in values)})",
            tuple(values.values()),
        )

    def receipt(self, row_id, kind="supplier_order_item", received_at="2026-08-22 08:00:00"):
        self.insert("incoming_receipts", id=row_id, receipt_number=f"IR-{row_id}",
                    status="posted", received_at=received_at)
        values = dict(id=row_id, receipt_id=row_id, status="posted", received_quantity=100)
        if kind != "unknown":
            values[kind + "_id"] = row_id
        self.insert("incoming_receipt_items", **values)
        if kind == "supplier_order_item":
            self.insert("supplier_requisition_orders", id=row_id, order_number=f"DOC-{row_id}", supplier_name="供应商甲")
            self.insert("supplier_requisition_order_items", id=row_id, supplier_order_id=row_id,
                        supplier_name_snapshot="供应商甲", report_length_mm=1000, report_width_mm=500)
        elif kind == "requisition_item":
            self.insert("material_requisitions", id=row_id, requisition_number=f"DOC-{row_id}", supplier_name="供应商甲")
            self.insert("material_requisition_items", id=row_id, requisition_id=row_id,
                        cardboard_len=1000, cardboard_width=500)
        elif kind == "stock_replenishment_item":
            self.insert("stock_replenishment_orders", id=row_id, order_number=f"DOC-{row_id}", supplier_name="供应商甲")
            self.insert("stock_replenishment_order_items", id=row_id, replenishment_order_id=row_id,
                        procurement_route_snapshot="paperboard", target_inventory_type="semi_finished")

    def native(self, row_id, kind="supplier_order_item"):
        key = "supplier_requisition_order_item_id" if kind == "supplier_order_item" else "material_requisition_item_id"
        self.insert("purchase_receipt_facts", id=row_id, **{key: row_id},
                    purchase_purpose_source_snapshot_id=row_id, source_key=f"order_item:{row_id}",
                    actual_material_code_snapshot="K=K", unit_price="2.500000", price_unit="per_square_meter",
                    currency="CNY", tax_included=1, tax_rate="0.13")
        self.insert("incoming_receipt_purpose_allocations", id=row_id, incoming_receipt_item_id=row_id,
                    status="posted", purpose_contract_status_snapshot="frozen", purchase_receipt_fact_id=row_id,
                    purchase_purpose_source_snapshot_id=row_id, source_key=f"order_item:{row_id}",
                    **{key: row_id}, receipt_total_sheet_qty=100, purchase_unit_cost=999)

    def snapshot(self, row_id, kind="stock_replenishment_item", **overrides):
        values = dict(
            id=row_id, incoming_receipt_item_id=row_id, supplier_id=1, supplier_name_snapshot="供应商甲",
            material_code_snapshot="K=K", source_kind=kind, purchase_document_number_snapshot=f"DOC-{row_id}",
            receipt_number_snapshot=f"IR-{row_id}", receipt_date_snapshot="2026-08-22", received_quantity_snapshot=100,
            quantity_unit="张", report_length_mm=1000, report_width_mm=500, unit_price="2.5", price_unit="per_square_meter",
            currency="CNY", tax_included=1, tax_rate="0.13", shipping_fee_mode="included", fact_origin="receipt_frozen",
            match_strategy="stable_material_id", finance_only_test_classification=0,
        )
        values.update(overrides)
        self.insert("supplier_receipt_settlement_price_facts", **values)

    def check(self):
        self.db.commit()
        return health.check_database(self.database)

    def cli(self, *arguments):
        self.db.commit()
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
        return subprocess.run([sys.executable, str(SCRIPT), *arguments], cwd=self.directory,
                              env=env, capture_output=True, text=True, encoding="utf-8", check=False)

    def test_scans_all_months_and_accepts_both_real_price_fact_routes(self):
        self.receipt(1, received_at="2025-01-02 01:00:00")
        self.native(1)
        self.receipt(2, "stock_replenishment_item", "2026-09-07 01:00:00")
        self.snapshot(2)
        self.receipt(3, received_at="2025-12-31 23:00:00")
        self.receipt(4, received_at="2026-10-01 01:00:00")
        self.receipt(5, "requisition_item")
        self.native(5, "requisition_item")
        report = self.check()
        self.assertFalse(report["ok"])
        self.assertEqual((report["effective_receipt_count"], report["frozen_price_count"], report["unresolved_count"]), (5, 3, 2))
        self.assertEqual([row["incoming_receipt_item_id"] for row in report["unresolved"]], [3, 4])
        self.assertEqual(report["unresolved"][0]["source_id"], 3)
        self.assertIn("2025", report["unresolved"][0]["received_at"])
        self.assertEqual(report["scope"], "all_dates")
        self.assertTrue(report["checked_at"])

    def test_existing_historical_snapshot_is_valid_without_purpose_allocation(self):
        self.receipt(1)
        self.snapshot(1, "supplier_order_item", fact_origin="historical_master_adoption",
                      adoption_reason="2026-09-02 老板确认采用当前主数据", adoption_evidence_reference="isolated-test-evidence")
        report = self.check()
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["frozen_price_count"], 1)

    def test_reversals_are_excluded_but_external_flags_do_not_hide_generic_receipts(self):
        for row_id in range(1, 5):
            self.receipt(row_id)
        self.db.execute("UPDATE incoming_receipts SET status='reversed' WHERE id=1")
        self.db.execute("UPDATE incoming_receipt_items SET status='reversed' WHERE id=2")
        self.native(3)
        self.insert("incoming_receipt_purpose_reversals", id=1, incoming_receipt_purpose_allocation_id=3)
        self.insert("incoming_receipt_reversal_facts", id=1, incoming_receipt_item_id=4)
        self.receipt(5, "stock_replenishment_item")
        self.insert("external_packaging_purchase_items", id=1, stock_replenishment_item_id=5)
        self.receipt(6, "stock_replenishment_item")
        self.db.execute("UPDATE stock_replenishment_order_items SET procurement_route_snapshot='external_packaging' WHERE id=6")
        report = self.check()
        self.assertFalse(report["ok"], report)
        self.assertEqual(report["effective_receipt_count"], 2)
        self.assertEqual(report["unresolved_count"], 2)
        self.assertEqual(report["excluded_counts"], {"reversed": 4})
        self.assertEqual([row["incoming_receipt_item_id"] for row in report["unresolved"]], [5, 6])
        for row in report["unresolved"]:
            self.assertIn("unresolved_replenishment_payable_route", row["gaps"])
            self.assertIn("missing_frozen_settlement_price", row["gaps"])

    def test_existing_authorized_finance_classification_is_still_a_valid_frozen_fact(self):
        self.receipt(1, "stock_replenishment_item")
        self.db.execute("UPDATE stock_replenishment_order_items SET procurement_route_snapshot='external_packaging' WHERE id=1")
        self.snapshot(1, fact_origin="historical_master_adoption",
                      finance_only_test_classification=1,
                      match_strategy="owner_authorized_finance_test_classification",
                      adoption_reason="2026-09-02 老板确认采用当前主数据",
                      adoption_evidence_reference="isolated-existing-authorized-fact",
                      received_quantity_snapshot=2)
        report = self.check()
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["frozen_price_count"], 1)
        self.assertEqual(report["effective_receipt_count"], 1)

    def test_allocation_cost_is_never_a_supplier_price(self):
        self.receipt(1)
        self.native(1)
        self.db.execute("DELETE FROM purchase_receipt_facts")
        report = self.check()
        self.assertEqual(report["frozen_price_count"], 0)
        self.assertIn("missing_native_purchase_price_fact", report["unresolved"][0]["gaps"])

    def test_native_link_status_source_quantity_and_price_fail_closed(self):
        mutations = [
            ("incoming_receipt_purpose_allocations", "status", "reversed", "invalid_native_allocation_status"),
            ("incoming_receipt_purpose_allocations", "receipt_total_sheet_qty", 99, "native_quantity_mismatch"),
            ("incoming_receipt_purpose_allocations", "supplier_requisition_order_item_id", 99, "native_purchase_source_mismatch"),
            ("purchase_receipt_facts", "supplier_requisition_order_item_id", 99, "native_purchase_source_mismatch"),
            ("purchase_receipt_facts", "source_key", "order_item:999", "native_purchase_source_mismatch"),
            ("purchase_receipt_facts", "unit_price", "NaN", "invalid_unit_price"),
            ("purchase_receipt_facts", "price_unit", "purpose_cost", "invalid_price_unit"),
        ]
        for row_id, (table, field, value, expected) in enumerate(mutations, 1):
            self.receipt(row_id)
            self.native(row_id)
            self.db.execute(f"UPDATE {table} SET {field}=? WHERE id=?", (value, row_id))
        report = self.check()
        self.assertEqual(report["unresolved_count"], len(mutations))
        for row, mutation in zip(report["unresolved"], mutations):
            with self.subTest(mutation=mutation):
                self.assertIn(mutation[3], row["gaps"])

    def test_snapshot_quantity_source_number_tax_and_supplier_fail_closed(self):
        cases = [
            ({"received_quantity_snapshot": 99}, "snapshot_quantity_mismatch"),
            ({"source_kind": "supplier_order_item"}, "snapshot_purchase_source_mismatch"),
            ({"purchase_document_number_snapshot": "OTHER"}, "snapshot_purchase_source_mismatch"),
            ({"receipt_number_snapshot": "OTHER"}, "snapshot_receipt_number_mismatch"),
            ({"tax_rate": "0.09"}, "invalid_snapshot_tax_shipping_contract"),
            ({"supplier_id": 99}, "missing_frozen_supplier"),
            ({"finance_only_test_classification": 1, "received_quantity_snapshot": 99}, "invalid_finance_test_classification"),
        ]
        for row_id, (overrides, _) in enumerate(cases, 1):
            self.receipt(row_id, "stock_replenishment_item")
            self.snapshot(row_id, **overrides)
        report = self.check()
        self.assertEqual(report["unresolved_count"], len(cases))
        for row, case in zip(report["unresolved"], cases):
            with self.subTest(case=case):
                self.assertIn(case[1], row["gaps"])

    def test_unknown_missing_and_undetermined_sources_cannot_report_green(self):
        self.receipt(1, "unknown")
        self.snapshot(1)
        self.receipt(2)
        self.snapshot(2, "supplier_order_item")
        self.db.execute("DELETE FROM supplier_requisition_order_items WHERE id=2")
        self.receipt(3, "stock_replenishment_item")
        self.snapshot(3)
        self.db.execute("UPDATE stock_replenishment_order_items SET procurement_route_snapshot=NULL, target_inventory_type='finished' WHERE id=3")
        report = self.check()
        self.assertEqual(report["unresolved_count"], 3)
        self.assertIn("unknown_purchase_source", report["unresolved"][0]["gaps"])
        self.assertIn("missing_purchase_source", report["unresolved"][1]["gaps"])
        self.assertIn("unresolved_replenishment_payable_route", report["unresolved"][2]["gaps"])

    def test_legacy_finished_liner_and_semi_finished_use_paperboard_route(self):
        for row_id in (1, 2):
            self.receipt(row_id, "stock_replenishment_item")
            self.snapshot(row_id)
        self.db.execute("UPDATE stock_replenishment_order_items SET procurement_route_snapshot=NULL")
        self.insert("products", id=1, box_style=" 衬板 ")
        self.db.execute("UPDATE stock_replenishment_order_items SET target_inventory_type='finished', product_id=1 WHERE id=1")
        self.assertTrue(self.check()["ok"])

    def test_duplicate_links_are_not_double_counted_or_considered_valid(self):
        self.receipt(1, "stock_replenishment_item")
        self.snapshot(1)
        self.snapshot(1, id=2)
        report = self.check()
        self.assertEqual(report["effective_receipt_count"], 1)
        self.assertEqual(report["unresolved_count"], 1)
        self.assertIn("duplicate_receipt_price_links", report["unresolved"][0]["gaps"])

    def test_read_only_json_cli_exit_codes_and_original_database_hash(self):
        self.receipt(1)
        self.db.commit()
        before = hashlib.sha256(self.database.read_bytes()).hexdigest()
        result = self.cli("--database", str(self.database), "--json")
        self.assertEqual(result.returncode, 1, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["unresolved_count"], 1)
        self.assertEqual(before, hashlib.sha256(self.database.read_bytes()).hexdigest())
        self.assertEqual(sorted(path.name for path in self.directory.iterdir()), [self.database.name])
        self.native(1)
        self.db.commit()
        before = hashlib.sha256(self.database.read_bytes()).hexdigest()
        result = self.cli("--database", str(self.database), "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["ok"])
        self.assertEqual(before, hashlib.sha256(self.database.read_bytes()).hexdigest())

    def test_missing_schema_and_database_fail_nonzero_without_zero_counts(self):
        self.db.execute("DROP TABLE supplier_receipt_settlement_price_facts")
        result = self.cli("--database", str(self.database), "--json")
        self.assertEqual(result.returncode, 2)
        report = json.loads(result.stdout)
        self.assertFalse(report["ok"])
        self.assertIsNone(report["unresolved_count"])
        self.assertIn("supplier_receipt_settlement_price_facts", report["errors"][0])
        missing = self.directory / "nonexistent.sqlite3"
        result = self.cli("--database", str(missing), "--json")
        self.assertEqual(result.returncode, 2)
        self.assertFalse(missing.exists())

    def test_explicit_database_is_required_and_apply_is_unavailable(self):
        self.assertEqual(self.cli("--json").returncode, 2)
        self.assertEqual(self.cli("--database", str(self.database), "--apply").returncode, 2)


if __name__ == "__main__":
    unittest.main()
