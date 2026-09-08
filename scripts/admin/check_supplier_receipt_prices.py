"""P0-45: check every effective paperboard receipt, across all dates, read-only.

Run with an explicit SQLite file: python scripts/admin/check_supplier_receipt_prices.py
--database PATH --json. Exit 0 means complete, 1 means unresolved receipts, and 2
means the check failed (including missing schema). This tool never adopts prices
or imports application startup, database, backup, or migration code.
"""

from __future__ import annotations

import argparse
import csv
from collections import Counter
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import sqlite3
from typing import Any


# Explicit columns also make schema drift fail closed, including on an empty DB.
_COLUMNS = {
    "incoming_receipts": "id receipt_number status received_at",
    "incoming_receipt_items": (
        "id receipt_id status received_quantity supplier_order_item_id "
        "requisition_item_id stock_replenishment_item_id"
    ),
    "incoming_receipt_purpose_allocations": (
        "id incoming_receipt_item_id status purpose_contract_status_snapshot "
        "purchase_receipt_fact_id purchase_purpose_source_snapshot_id source_key "
        "supplier_requisition_order_item_id material_requisition_item_id receipt_total_sheet_qty"
    ),
    "incoming_receipt_purpose_reversals": (
        "id incoming_receipt_item_id incoming_receipt_purpose_allocation_id"
    ),
    "incoming_receipt_reversal_facts": "id incoming_receipt_item_id",
    "purchase_receipt_facts": (
        "id supplier_requisition_order_item_id material_requisition_item_id "
        "purchase_purpose_source_snapshot_id source_key actual_material_code_snapshot "
        "unit_price price_unit currency tax_included tax_rate"
    ),
    "supplier_receipt_settlement_price_facts": (
        "id incoming_receipt_item_id supplier_id supplier_name_snapshot material_code_snapshot "
        "source_kind purchase_document_number_snapshot receipt_number_snapshot receipt_date_snapshot "
        "received_quantity_snapshot quantity_unit report_length_mm report_width_mm "
        "unit_price price_unit currency tax_included tax_rate shipping_fee_mode fact_origin "
        "match_strategy finance_only_test_classification adoption_reason adoption_evidence_reference"
    ),
    "supplier_requisition_order_items": (
        "id supplier_order_id supplier_name_snapshot report_length_mm report_width_mm"
    ),
    "supplier_requisition_orders": "id order_number supplier_name",
    "material_requisition_items": "id requisition_id cardboard_len cardboard_width",
    "material_requisitions": "id requisition_number supplier_name",
    "stock_replenishment_order_items": (
        "id replenishment_order_id procurement_route_snapshot target_inventory_type product_id"
    ),
    "stock_replenishment_orders": "id order_number supplier_name",
    "external_packaging_purchase_items": "id stock_replenishment_item_id",
    "products": "id box_style",
    "supplier_master_records": "id normalized_name",
    "supplier_master_aliases": "id supplier_id normalized_alias",
}

_SOURCES = {
    "supplier_order_item": (
        "supplier_requisition_order_items", "supplier_order_id",
        "supplier_requisition_orders", "order_number",
    ),
    "requisition_item": (
        "material_requisition_items", "requisition_id",
        "material_requisitions", "requisition_number",
    ),
    "stock_replenishment_item": (
        "stock_replenishment_order_items", "replenishment_order_id",
        "stock_replenishment_orders", "order_number",
    ),
}


def _rows(db: sqlite3.Connection, table: str, field: str | None = None,
          value: Any = None) -> list[sqlite3.Row]:
    # Identifiers come only from the constants / internal callers above.
    columns = ", ".join(_COLUMNS[table].split())
    sql = f"SELECT {columns} FROM {table}"
    if field is not None:
        return db.execute(sql + f" WHERE {field} = ?", (value,)).fetchall()
    return db.execute(sql + " ORDER BY id").fetchall()


def _one(db: sqlite3.Connection, table: str, row_id: Any) -> sqlite3.Row | None:
    rows = _rows(db, table, "id", row_id)
    return rows[0] if rows else None


def _number(value: Any) -> Decimal:
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else Decimal(-1)
    except (InvalidOperation, ValueError):
        return Decimal(-1)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _price_gaps(fact: sqlite3.Row, length: Any, width: Any) -> list[str]:
    gaps = []
    if _number(fact["unit_price"]) <= 0:
        gaps.append("invalid_unit_price")
    if fact["price_unit"] not in {"per_sheet", "per_square_meter"}:
        gaps.append("invalid_price_unit")
    if (not 0 <= _number(fact["tax_rate"]) <= 1
            or fact["tax_included"] not in (0, 1)):
        gaps.append("invalid_tax_contract")
    currency = _text(fact["currency"])
    if len(currency) != 3 or not currency.isascii() or not currency.isalpha():
        gaps.append("invalid_currency")
    if _number(length) <= 0 or _number(width) <= 0:
        gaps.append("missing_paperboard_dimensions")
    return gaps


def _finance_test_classification(fact: sqlite3.Row) -> bool:
    return (fact["finance_only_test_classification"] == 1
            and fact["fact_origin"] == "historical_master_adoption"
            and fact["match_strategy"] == "owner_authorized_finance_test_classification")


def _source(db: sqlite3.Connection, item: sqlite3.Row, *,
            finance_test_classified: bool = False) -> dict[str, Any]:
    # Supplier order receipts can retain a legacy requisition link; the supplier
    # order is authoritative, matching _paperboard_source in monthly settlement.
    kinds = [kind for kind in _SOURCES if item[kind + "_id"] is not None]
    kind = kinds[0] if kinds else "unknown"
    result: dict[str, Any] = {"kind": kind, "id": None, "gaps": []}
    if not kinds:
        result["gaps"].append("unknown_purchase_source")
        return result
    result["id"] = item[kind + "_id"]
    if "stock_replenishment_item" in kinds and len(kinds) != 1:
        result["gaps"].append("conflicting_purchase_sources")
    table, header_key, header_table, document_key = _SOURCES[kind]
    source = _one(db, table, result["id"])
    if source is None:
        result["gaps"].append("missing_purchase_source")
        return result
    if kind == "stock_replenishment_item":
        # A route flag or a purchase link does not prove an independent payable
        # receipt exists. A generic posted receipt must remain visible as a gap.
        external = _rows(db, "external_packaging_purchase_items", "stock_replenishment_item_id", source["id"])
        route = source["procurement_route_snapshot"]
        product = _one(db, "products", source["product_id"])
        liner = (product is not None and "".join(_text(product["box_style"]).upper().split())
                 in {"LINER", "衬板"})
        paperboard = (not external and route != "external_packaging"
                      and (route == "paperboard" or source["target_inventory_type"] == "semi_finished"
                           or (source["target_inventory_type"] == "finished" and liner)))
        if not paperboard and not finance_test_classified:
            result["gaps"].append("unresolved_replenishment_payable_route")
    header = _one(db, header_table, source[header_key])
    if header is None:
        result["gaps"].append("missing_purchase_document")
        return result
    result["document_number"] = header[document_key]
    result["supplier_name"] = _text(
        (source["supplier_name_snapshot"] if kind == "supplier_order_item" else None)
        or header["supplier_name"]
    )
    if kind != "stock_replenishment_item":
        result["length"] = source["report_length_mm" if kind == "supplier_order_item" else "cardboard_len"]
        result["width"] = source["report_width_mm" if kind == "supplier_order_item" else "cardboard_width"]
    return result


def _native_gaps(db: sqlite3.Connection, item: sqlite3.Row,
                 allocation: sqlite3.Row, source: dict[str, Any]) -> list[str]:
    fact = _one(db, "purchase_receipt_facts", allocation["purchase_receipt_fact_id"])
    gaps = []
    if allocation["status"] != "posted" or allocation["purpose_contract_status_snapshot"] != "frozen":
        gaps.append("invalid_native_allocation_status")
    if _number(allocation["receipt_total_sheet_qty"]) != _number(item["received_quantity"]):
        gaps.append("native_quantity_mismatch")
    if fact is None:
        return gaps + ["missing_native_purchase_price_fact"]
    supplier_source = source["kind"] == "supplier_order_item"
    field = "supplier_requisition_order_item_id" if supplier_source else "material_requisition_item_id"
    other = "material_requisition_item_id" if supplier_source else "supplier_requisition_order_item_id"
    if (source["kind"] not in {"supplier_order_item", "requisition_item"}
            or allocation[field] != source["id"] or fact[field] != source["id"]
            or allocation[other] is not None or fact[other] is not None
            or not _text(allocation["source_key"])
            or allocation["source_key"] != fact["source_key"]
            or allocation["purchase_purpose_source_snapshot_id"] is None
            or allocation["purchase_purpose_source_snapshot_id"] != fact["purchase_purpose_source_snapshot_id"]):
        gaps.append("native_purchase_source_mismatch")
    if not _text(fact["actual_material_code_snapshot"]):
        gaps.append("missing_frozen_material")
    normalized = "".join(source.get("supplier_name", "").split()).casefold()
    suppliers = _rows(db, "supplier_master_records", "normalized_name", normalized) if normalized else []
    supplier_ids = {row["id"] for row in suppliers}
    if not supplier_ids and normalized:
        supplier_ids = {
            row["supplier_id"] for row in _rows(db, "supplier_master_aliases", "normalized_alias", normalized)
            if _one(db, "supplier_master_records", row["supplier_id"]) is not None
        }
    if len(supplier_ids) != 1:
        gaps.append("supplier_not_uniquely_resolved")
    return gaps + _price_gaps(fact, source.get("length"), source.get("width"))


def _snapshot_gaps(db: sqlite3.Connection, item: sqlite3.Row, receipt: sqlite3.Row,
                   fact: sqlite3.Row, source: dict[str, Any]) -> list[str]:
    gaps = _price_gaps(fact, fact["report_length_mm"], fact["report_width_mm"])
    if _one(db, "supplier_master_records", fact["supplier_id"]) is None:
        gaps.append("missing_frozen_supplier")
    if (fact["source_kind"] != source["kind"]
            or fact["purchase_document_number_snapshot"] != source.get("document_number")):
        gaps.append("snapshot_purchase_source_mismatch")
    if fact["receipt_number_snapshot"] != receipt["receipt_number"]:
        gaps.append("snapshot_receipt_number_mismatch")
    classification = fact["finance_only_test_classification"]
    authorized_test = _finance_test_classification(fact)
    if classification not in (0, 1) or (classification == 1 and not authorized_test):
        gaps.append("invalid_finance_test_classification")
    if (not authorized_test and _number(fact["received_quantity_snapshot"]) != _number(item["received_quantity"])):
        gaps.append("snapshot_quantity_mismatch")
    if _number(fact["received_quantity_snapshot"]) <= 0 or fact["quantity_unit"] != "张":
        gaps.append("invalid_snapshot_quantity")
    if (fact["currency"] != "CNY" or fact["tax_included"] != 1
            or _number(fact["tax_rate"]) != Decimal("0.13") or fact["shipping_fee_mode"] != "included"):
        gaps.append("invalid_snapshot_tax_shipping_contract")
    if not _text(fact["supplier_name_snapshot"]) or not _text(fact["material_code_snapshot"]):
        gaps.append("missing_frozen_supplier_or_material")
    if fact["fact_origin"] == "historical_master_adoption":
        if (fact["adoption_reason"] != "2026-09-02 老板确认采用当前主数据"
                or not _text(fact["adoption_evidence_reference"])):
            gaps.append("missing_historical_adoption_evidence")
    elif fact["fact_origin"] != "receipt_frozen":
        gaps.append("unknown_snapshot_origin")
    try:
        date.fromisoformat(fact["receipt_date_snapshot"])
    except (TypeError, ValueError):
        gaps.append("invalid_frozen_receipt_date")
    return gaps


def check_database(database: str | Path) -> dict[str, Any]:
    """Return a complete report; operational/schema errors cannot report zero gaps."""
    path = Path(database).expanduser().resolve()
    report: dict[str, Any] = {
        "check": "P0-45 supplier receipt frozen prices", "schema_version": 1,
        "database": str(path), "checked_at": datetime.now(timezone.utc).isoformat(),
        "scope": "all_dates", "read_only": True, "ok": False,
        "effective_receipt_count": None, "frozen_price_count": None,
        "unresolved_count": None, "excluded_counts": {}, "by_source": {},
        "unresolved": [], "errors": [],
    }
    db = None
    try:
        if not path.is_file():
            raise ValueError("database must be an existing SQLite file")
        db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only = ON")
        db.execute("BEGIN")  # One consistent read snapshot for all rows and counts.
        for table, columns in _COLUMNS.items():
            actual = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
            missing = set(columns.split()) - actual
            if missing:
                raise ValueError(f"missing schema: {table}: {', '.join(sorted(missing))}")
        excluded: Counter[str] = Counter()
        total = frozen = 0
        for item in _rows(db, "incoming_receipt_items"):
            receipt = _one(db, "incoming_receipts", item["receipt_id"])
            allocations = _rows(db, "incoming_receipt_purpose_allocations", "incoming_receipt_item_id", item["id"])
            reversed_item = (
                item["status"] == "reversed" or (receipt is not None and receipt["status"] == "reversed")
                or _rows(db, "incoming_receipt_purpose_reversals", "incoming_receipt_item_id", item["id"])
                or _rows(db, "incoming_receipt_reversal_facts", "incoming_receipt_item_id", item["id"])
                or any(_rows(db, "incoming_receipt_purpose_reversals", "incoming_receipt_purpose_allocation_id", row["id"])
                       for row in allocations)
            )
            if reversed_item:
                excluded["reversed"] += 1
                continue
            snapshots = _rows(db, "supplier_receipt_settlement_price_facts", "incoming_receipt_item_id", item["id"])
            source = _source(db, item, finance_test_classified=bool(
                snapshots and _finance_test_classification(snapshots[0])
            ))
            total += 1
            gaps = list(source["gaps"])
            if item["status"] != "posted" or receipt is None or receipt["status"] != "posted":
                gaps.append("receipt_not_effectively_posted")
            if _number(item["received_quantity"]) <= 0:
                gaps.append("invalid_received_quantity")
            if len(snapshots) > 1 or len(allocations) > 1:
                gaps.append("duplicate_receipt_price_links")
            if snapshots and receipt is not None:
                gaps.extend(_snapshot_gaps(db, item, receipt, snapshots[0], source))
            elif allocations:
                gaps.extend(_native_gaps(db, item, allocations[0], source))
            else:
                gaps.append("missing_frozen_settlement_price")
            grouped = report["by_source"].setdefault(source["kind"], {"total": 0, "frozen": 0, "unresolved": 0})
            grouped["total"] += 1
            if gaps:
                grouped["unresolved"] += 1
                report["unresolved"].append({
                    "incoming_receipt_item_id": item["id"], "receipt_id": item["receipt_id"],
                    "receipt_number": receipt["receipt_number"] if receipt is not None else None,
                    "received_at": receipt["received_at"] if receipt is not None else None,
                    "received_quantity": item["received_quantity"],
                    "source_kind": source["kind"], "source_id": source["id"],
                    "purchase_document_number": source.get("document_number"),
                    "supplier_name": source.get("supplier_name"), "gaps": sorted(set(gaps)),
                })
            else:
                frozen += 1
                grouped["frozen"] += 1
        report.update(effective_receipt_count=total, frozen_price_count=frozen,
                      unresolved_count=total - frozen, excluded_counts=dict(excluded), ok=total == frozen)
    except (OSError, sqlite3.Error, ValueError) as error:
        report["errors"].append(str(error))
    finally:
        if db is not None:
            db.close()
    return report


def write_review_csv(report: dict[str, Any], destination: Path) -> None:
    """Export unresolved evidence only; never overwrite a database or old review."""
    fields = ["incoming_receipt_item_id", "receipt_id", "receipt_number",
              "received_at", "supplier_name", "source_kind", "source_id",
              "purchase_document_number", "received_quantity", "gaps"]
    # Exclusive creation also protects symlinks/hardlinks to existing business files.
    with destination.open("x", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for row in report["unresolved"]:
            values = {key: row.get(key) for key in fields}
            values["gaps"] = "; ".join(row["gaps"])
            # Supplier and document text is untrusted when opened in Excel.
            for key, value in values.items():
                if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
                    values[key] = "'" + value
            writer.writerow(values)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, help="Existing SQLite database file (opened read-only)")
    parser.add_argument("--json", action="store_true", help="Emit one JSON report to stdout")
    parser.add_argument("--csv", type=Path, help="Create a new UTF-8 CSV of unresolved rows for review (never overwrite)")
    args = parser.parse_args(argv)
    report = check_database(args.database)
    if args.csv is not None and not report["errors"]:
        try:
            write_review_csv(report, args.csv)
            report["review_csv"] = str(args.csv.resolve())
        except OSError as error:
            report["ok"] = False
            report["errors"].append(f"CSV export failed: {error}")
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"{report['checked_at']} | {report['database']} | all dates | read-only")
        print(f"Effective: {report['effective_receipt_count']} | Frozen: {report['frozen_price_count']} | Unresolved: {report['unresolved_count']}")
        for row in report["unresolved"]:
            print(f"{row['receipt_number']} | receipt item {row['incoming_receipt_item_id']} | {row['source_kind']}:{row['source_id']} | {', '.join(row['gaps'])}")
        for error in report["errors"]:
            print(f"CHECK FAILED: {error}")
    return 2 if report["errors"] else (0 if report["ok"] else 1)


if __name__ == "__main__":
    raise SystemExit(main())
