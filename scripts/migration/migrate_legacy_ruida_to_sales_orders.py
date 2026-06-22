"""Dry-run or migrate a fixed sample of complete Ruida orders into sales tables.

Apply is permanently blocked for the canonical main database. The script may
write only to an explicitly supplied SQLite copy.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
CANONICAL_SQLITE = (ROOT / "data" / "carton_erp.sqlite3").resolve()
REPORT_DIR = ROOT / "docs" / "migration_reports"
CONFIRMATION = "APPLY_100_RUIDA_SALES_SAMPLE"
MAIN_CONFIRMATION = "APPLY_TIANHUA_FORMAL_SALES"
FEE_KEYWORDS = ("模具费", "制版费", "样品费", "加工费", "模具", "制版", "运费", "版费", "刀模")
MANIFEST_FIELDS = [
    "batch_id", "legacy_order_id", "customer_name", "planned_item_count",
    "planned_amount", "match_source", "batch_order", "review_csv", "status",
]


class SafetyError(RuntimeError):
    pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Migrate a fixed complete-order sample; dry-run by default.")
    parser.add_argument("--sqlite-path", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--all", action="store_true", help="Process all complete orders for one explicit customer.")
    parser.add_argument(
        "--sample-mode",
        choices=["complete_orders", "multi_item_orders"],
        default="complete_orders",
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-apply")
    parser.add_argument("--batch-manifest", type=Path)
    parser.add_argument("--batch-id")
    parser.add_argument("--generate-batch-manifest", type=Path)
    parser.add_argument("--expected-source-db-sha256")
    parser.add_argument("--allow-main-sandbox", action="store_true")
    parser.add_argument("--confirm-main-apply")
    parser.add_argument("--product-review-csv", type=Path)
    parser.add_argument("--customer-name")
    parser.add_argument("--report-dir", type=Path, default=REPORT_DIR)
    return parser


def validate_scope(args: argparse.Namespace) -> None:
    if args.all and not args.customer_name:
        raise SafetyError("--all requires --customer-name")
    if args.all and not args.product_review_csv:
        raise SafetyError("--all requires --product-review-csv")
    if bool(args.batch_manifest) != bool(args.batch_id):
        raise SafetyError("--batch-manifest and --batch-id must be provided together")
    if args.batch_manifest and (not args.customer_name or not args.product_review_csv):
        raise SafetyError("batch execution requires --customer-name and --product-review-csv")
    if args.generate_batch_manifest and (
        not args.all or not args.customer_name or not args.product_review_csv
    ):
        raise SafetyError(
            "--generate-batch-manifest requires --all, --customer-name and --product-review-csv"
        )
    if args.generate_batch_manifest and args.apply:
        raise SafetyError("batch manifest generation is dry-run only")


def effective_limit(args: argparse.Namespace) -> int | None:
    return None if args.all or args.batch_manifest else args.limit


def validate_apply_target(apply: bool, target: Path, canonical: Path) -> None:
    if apply and target.resolve() == canonical.resolve():
        raise SafetyError("main database apply requires the guarded authorization flow")


def validate_apply_authorization(
    args: argparse.Namespace, target: Path, current_hash: str
) -> None:
    if not args.apply:
        return
    is_main = target.resolve() == CANONICAL_SQLITE.resolve()
    if not is_main:
        if args.confirm_apply != CONFIRMATION:
            raise SafetyError(f"--apply requires --confirm-apply {CONFIRMATION}")
        return
    if not args.allow_main_sandbox:
        raise SafetyError("main apply requires --allow-main-sandbox")
    if args.confirm_main_apply != MAIN_CONFIRMATION:
        raise SafetyError(f"main apply requires --confirm-main-apply {MAIN_CONFIRMATION}")
    if not args.expected_source_db_sha256:
        raise SafetyError("main apply requires --expected-source-db-sha256")
    if args.expected_source_db_sha256.upper() != current_hash.upper():
        raise SafetyError("source database SHA-256 mismatch")
    if not args.batch_manifest:
        raise SafetyError("main apply requires --batch-manifest")
    if not args.batch_id:
        raise SafetyError("main apply requires --batch-id")


def order_number_for(legacy_order_id: int) -> str:
    return f"RUIDA-{legacy_order_id}"


def money(value: Any) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def unit_price(value: Any) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def is_fee_item(value: Any) -> bool:
    text = str(value or "")
    return any(keyword in text for keyword in FEE_KEYWORDS)


def load_product_review(
    path: Path,
) -> tuple[dict[tuple[int, str], int], set[tuple[int, str]], dict[str, int]]:
    if not path.is_file():
        raise SafetyError(f"product review CSV not found: {path}")
    approved: dict[tuple[int, str], int] = {}
    rejected: set[tuple[int, str]] = set()
    counts = {"approved": 0, "rejected": 0}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {
            "customer_id", "legacy_style_no_raw", "review_status", "review_decision",
            "approved_product_id", "matched_product_id", "review_note",
        }
        missing = sorted(required - set(reader.fieldnames or []))
        if missing:
            raise SafetyError("product review CSV missing fields: " + ", ".join(missing))
        seen: set[tuple[int, str]] = set()
        for row_no, row in enumerate(reader, 2):
            try:
                customer_id = int(row["customer_id"])
            except (TypeError, ValueError):
                raise SafetyError(f"review row {row_no}: invalid customer_id") from None
            raw_style = str(row["legacy_style_no_raw"] or "")
            key = (customer_id, raw_style)
            if key in seen:
                raise SafetyError(f"review row {row_no}: duplicate customer/style key")
            seen.add(key)
            status = str(row["review_status"] or "").strip()
            decision = str(row["review_decision"] or "").strip()
            if status == "approved" and decision == "approve_prefix_match":
                try:
                    approved_id = int(row["approved_product_id"])
                    matched_id = int(row["matched_product_id"])
                except (TypeError, ValueError):
                    raise SafetyError(f"review row {row_no}: approved product id required") from None
                if approved_id != matched_id and len(str(row["review_note"] or "").strip()) < 10:
                    raise SafetyError(f"review row {row_no}: changed product requires review_note")
                if is_fee_item(raw_style):
                    raise SafetyError(f"review row {row_no}: fee item cannot be approved")
                approved[key] = approved_id
                counts["approved"] += 1
            elif status == "rejected":
                rejected.add(key)
                counts["rejected"] += 1
            else:
                raise SafetyError(
                    f"review row {row_no}: only approved mappings and rejected rows are accepted"
                )
    return approved, rejected, counts


def parse_date(value: str | None) -> str:
    if not value:
        raise ValueError("missing date")
    for fmt in ("%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            pass
    raise ValueError(f"invalid date: {value}")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def load_batch_manifest(
    path: Path, batch_id: str, customer_name: str, review_csv: Path
) -> list[int]:
    if not path.is_file():
        raise SafetyError(f"batch manifest not found: {path}")
    order_ids: list[int] = []
    seen: set[int] = set()
    expected_review = str(review_csv.resolve())
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = sorted(set(MANIFEST_FIELDS) - set(reader.fieldnames or []))
        if missing:
            raise SafetyError("batch manifest missing fields: " + ", ".join(missing))
        for row_no, row in enumerate(reader, 2):
            if row["batch_id"] != batch_id:
                continue
            if row["customer_name"] != customer_name:
                raise SafetyError(f"manifest row {row_no}: customer mismatch")
            if str(Path(row["review_csv"]).resolve()) != expected_review:
                raise SafetyError(f"manifest row {row_no}: review CSV mismatch")
            if row["status"] != "planned":
                raise SafetyError(f"manifest row {row_no}: status must be planned")
            try:
                order_id = int(row["legacy_order_id"])
            except ValueError:
                raise SafetyError(f"manifest row {row_no}: invalid legacy_order_id") from None
            if order_id in seen:
                raise SafetyError(f"manifest row {row_no}: duplicate legacy_order_id")
            seen.add(order_id)
            order_ids.append(order_id)
    if not order_ids:
        raise SafetyError(f"batch id not found in manifest: {batch_id}")
    return order_ids


def select_manifest_entries(
    candidates: list[dict[str, Any]], manifest_order_ids: list[int]
) -> list[dict[str, Any]]:
    by_id = {entry["order"]["legacy_order_id"]: entry for entry in candidates}
    missing = [order_id for order_id in manifest_order_ids if order_id not in by_id]
    if missing:
        raise SafetyError(
            f"manifest contains {len(missing)} orders that are no longer complete candidates"
        )
    return [by_id[order_id] for order_id in manifest_order_ids]


def pending_entries(
    selected: list[dict[str, Any]], existing_order_ids: set[int]
) -> list[dict[str, Any]]:
    return [
        entry
        for entry in selected
        if entry["order"]["legacy_order_id"] not in existing_order_ids
    ]


def write_batch_manifest(
    path: Path, selected: list[dict[str, Any]], review_csv: Path, customer_name: str
) -> None:
    if path.exists():
        raise SafetyError(f"refusing to overwrite immutable batch manifest: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    sizes = [100, 1000, 5000, 5000]
    boundaries: list[int] = []
    total = 0
    for size in sizes:
        total += size
        boundaries.append(total)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=MANIFEST_FIELDS)
        writer.writeheader()
        batch_positions: dict[str, int] = defaultdict(int)
        for index, entry in enumerate(selected):
            batch_number = next(
                (i + 1 for i, boundary in enumerate(boundaries) if index < boundary),
                5,
            )
            batch_id = f"batch_{batch_number}"
            batch_positions[batch_id] += 1
            methods = {item["match_method"] for item in entry["items"]}
            match_source = next(iter(methods)) if len(methods) == 1 else "mixed"
            writer.writerow({
                "batch_id": batch_id,
                "legacy_order_id": entry["order"]["legacy_order_id"],
                "customer_name": customer_name,
                "planned_item_count": len(entry["items"]),
                "planned_amount": str(sum(
                    (money(item["amount"]) for item in entry["items"]), Decimal("0.00")
                )),
                "match_source": match_source,
                "batch_order": batch_positions[batch_id],
                "review_csv": str(review_csv.resolve()),
                "status": "planned",
            })


def product_map(connection: sqlite3.Connection) -> dict[tuple[int, str], list[dict[str, Any]]]:
    result: dict[tuple[int, str], list[dict[str, Any]]] = defaultdict(list)
    rows = connection.execute(
        """SELECT id, customer_id, product_code, customer_material_code,
                  product_name, legacy_material_text, length_mm, width_mm, height_mm
           FROM products WHERE deleted_at IS NULL"""
    )
    for row in rows:
        data = {
            "id": row[0], "customer_id": row[1], "product_code": row[2],
            "customer_material_code": row[3], "product_name": row[4],
            "legacy_material_text": row[5], "length_mm": row[6],
            "width_mm": row[7], "height_mm": row[8],
        }
        for key in (row[2], row[3]):
            if key and str(key).strip():
                result[(row[1], str(key).strip())].append(data)
    return result


def products_by_id(connection: sqlite3.Connection) -> dict[int, dict[str, Any]]:
    return {
        row[0]: {
            "id": row[0], "customer_id": row[1], "product_code": row[2],
            "customer_material_code": row[3], "product_name": row[4],
            "legacy_material_text": row[5], "length_mm": row[6],
            "width_mm": row[7], "height_mm": row[8],
        }
        for row in connection.execute(
            """SELECT id, customer_id, product_code, customer_material_code,
                      product_name, legacy_material_text, length_mm, width_mm, height_mm
               FROM products WHERE deleted_at IS NULL"""
        )
    }


def customer_ids_for_name(connection: sqlite3.Connection, customer_name: str | None) -> set[int] | None:
    if not customer_name:
        return None
    ids = {
        row[0]
        for row in connection.execute("SELECT id FROM customers WHERE name=?", (customer_name,))
    }
    ids.update(
        row[0]
        for row in connection.execute(
            "SELECT DISTINCT customer_id FROM legacy_ruida_orders WHERE customer_name=?",
            (customer_name,),
        )
        if row[0] is not None
    )
    if not ids:
        raise SafetyError(f"customer not found: {customer_name}")
    return ids


def build_plan(
    connection: sqlite3.Connection,
    limit: int | None,
    sample_mode: str = "complete_orders",
    approved_review: dict[tuple[int, str], int] | None = None,
    rejected_review: set[tuple[int, str]] | None = None,
    customer_name: str | None = None,
    manifest_order_ids: list[int] | None = None,
) -> dict[str, Any]:
    if limit is not None and (limit < 1 or limit > 100):
        raise SafetyError("--limit must be between 1 and 100")
    products = product_map(connection)
    product_ids = products_by_id(connection)
    approved_review = approved_review or {}
    rejected_review = rejected_review or set()
    customer_ids = customer_ids_for_name(connection, customer_name)
    orders = {
        row[0]: {
            "legacy_order_id": row[0], "customer_id": row[1], "po": row[2],
            "remark": row[3], "customer_name": row[4],
        }
        for row in connection.execute(
            "SELECT legacy_order_id,customer_id,po,remark,customer_name FROM legacy_ruida_orders"
        )
    }
    items: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in connection.execute(
        """SELECT legacy_item_id,legacy_order_id,customer_id,style_no,customer_order_no,
                  order_date,delivery_date,order_quantity,unit_price,amount,material,
                  length_mm,width_mm,height_mm,box_type,product_note
           FROM legacy_ruida_order_items"""
    ):
        items[row[1]].append({
            "legacy_item_id": row[0], "legacy_order_id": row[1], "customer_id": row[2],
            "style_no": row[3], "customer_order_no": row[4], "order_date": row[5],
            "delivery_date": row[6], "quantity": row[7], "unit_price": row[8],
            "amount": row[9], "material": row[10], "length_mm": row[11],
            "width_mm": row[12], "height_mm": row[13], "box_type": row[14],
            "product_note": row[15],
        })
    candidates = []
    skip_reasons: dict[str, int] = defaultdict(int)
    rejected_style_hits = 0
    for legacy_id in sorted(orders, reverse=True):
        order = orders[legacy_id]
        if customer_ids is not None and order["customer_id"] not in customer_ids:
            continue
        order_items = items.get(legacy_id, [])
        if not order_items:
            skip_reasons["no_items"] += 1
            continue
        if sample_mode == "multi_item_orders" and len(order_items) < 2:
            skip_reasons["single_item_order"] += 1
            continue
        if not order["customer_id"]:
            skip_reasons["customer_unmatched"] += 1
            continue
        mapped_items = []
        reason = None
        for item in order_items:
            raw_style = str(item["style_no"] or "")
            review_key = (item["customer_id"], raw_style)
            if review_key in rejected_review:
                rejected_style_hits += 1
                reason = "rejected_product_review"
                break
            if is_fee_item(raw_style):
                reason = "suspected_fee_item"
                break
            exact_key = (item["customer_id"], raw_style.strip())
            matches = {product["id"]: product for product in products.get(exact_key, [])}
            match_method = "exact"
            if len(matches) != 1:
                approved_id = approved_review.get(review_key)
                approved_product = product_ids.get(approved_id) if approved_id is not None else None
                if approved_product is None or approved_product["customer_id"] != item["customer_id"]:
                    reason = "product_unmatched_or_ambiguous"
                    break
                matches = {approved_product["id"]: approved_product}
                match_method = "approved_prefix"
            if not item["order_date"] or not item["quantity"] or item["quantity"] <= 0:
                reason = "invalid_quantity_or_date"
                break
            if item["unit_price"] is None or item["unit_price"] < 0 or item["amount"] is None or item["amount"] < 0:
                reason = "invalid_amount"
                break
            mapped_items.append({
                **item,
                "product": next(iter(matches.values())),
                "match_method": match_method,
            })
        if reason:
            skip_reasons[reason] += 1
            continue
        candidates.append({"order": order, "items": mapped_items})
        if limit is not None and len(candidates) == limit:
            break

    existing = {
        row[0] for row in connection.execute(
            "SELECT legacy_order_id FROM migration_ruida_sales_order_map"
        )
    } if connection.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='migration_ruida_sales_order_map'"
    ).fetchone()[0] else set()
    selected = (
        select_manifest_entries(candidates, manifest_order_ids)
        if manifest_order_ids is not None
        else candidates
    )
    pending = pending_entries(selected, existing)
    amount_total = sum((sum(money(item["amount"]) for item in entry["items"]) for entry in pending), Decimal("0.00"))
    pending_items = [item for entry in pending for item in entry["items"]]
    return {
        "selected": selected,
        "pending": pending,
        "skip_reasons": dict(skip_reasons),
        "planned_orders": len(pending),
        "planned_items": sum(len(entry["items"]) for entry in pending),
        "selected_orders": len(selected),
        "selected_items": sum(len(entry["items"]) for entry in selected),
        "already_imported": len(selected) - len(pending),
        "amount_total": amount_total,
        "approved_prefix_items": sum(
            item["match_method"] == "approved_prefix" for item in pending_items
        ),
        "exact_match_items": sum(item["match_method"] == "exact" for item in pending_items),
        "rejected_style_hits": rejected_style_hits,
        "selected_rejected_items": sum(
            (item["customer_id"], str(item["style_no"] or "")) in rejected_review
            for entry in pending for item in entry["items"]
        ),
        "multi_item_orders": sum(len(entry["items"]) >= 2 for entry in pending),
        "single_item_orders": sum(len(entry["items"]) == 1 for entry in pending),
        "selected_fee_items": sum(
            is_fee_item(item["style_no"]) for entry in pending for item in entry["items"]
        ),
        "selected_unmatched_items": sum(
            item.get("match_method") not in {"exact", "approved_prefix"}
            for entry in pending for item in entry["items"]
        ),
    }


def create_ledger(connection: sqlite3.Connection) -> None:
    connection.execute("""CREATE TABLE IF NOT EXISTS migration_ruida_sales_order_map (
        legacy_order_id INTEGER PRIMARY KEY, sales_order_id INTEGER NOT NULL UNIQUE,
        migrated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
    connection.execute("""CREATE TABLE IF NOT EXISTS migration_ruida_sales_item_map (
        legacy_item_id INTEGER PRIMARY KEY, sales_order_item_id INTEGER NOT NULL UNIQUE,
        legacy_order_id INTEGER NOT NULL, migrated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")


def apply_plan(connection: sqlite3.Connection, plan: dict[str, Any]) -> dict[str, int]:
    create_ledger(connection)
    inserted_orders = inserted_items = 0
    for entry in plan["pending"]:
        order = entry["order"]
        order_items = entry["items"]
        order_date = min(parse_date(item["order_date"]) for item in order_items)
        delivery_dates = [parse_date(item["delivery_date"]) for item in order_items if item["delivery_date"]]
        total = sum((money(item["amount"]) for item in order_items), Decimal("0.00"))
        customer_po = next((item["customer_order_no"] for item in order_items if item["customer_order_no"]), order["po"])
        cursor = connection.execute(
            """INSERT INTO sales_orders
               (order_number,customer_id,customer_po,order_date,delivery_date,status,
                payment_status,total_amount,remark)
               VALUES (?,?,?,?,?,'pending_production','unpaid',?,?)""",
            (order_number_for(order["legacy_order_id"]), order["customer_id"], customer_po,
             order_date, max(delivery_dates) if delivery_dates else None, str(total),
             f"[瑞达历史订单 {order['legacy_order_id']}] {order['remark'] or ''}".strip()),
        )
        sales_order_id = cursor.lastrowid
        connection.execute(
            "INSERT INTO migration_ruida_sales_order_map(legacy_order_id,sales_order_id) VALUES (?,?)",
            (order["legacy_order_id"], sales_order_id),
        )
        inserted_orders += 1
        for item in order_items:
            product = item["product"]
            spec = f"{int(item['length_mm'] or 0)}×{int(item['width_mm'] or 0)}×{int(item['height_mm'] or 0)}mm"
            cursor = connection.execute(
                """INSERT INTO sales_order_items
                   (order_id,product_id,quantity,unit_price,subtotal,material_status,
                    snapshot_product_name,snapshot_spec,snapshot_material,delivered_quantity,
                    is_force_closed,inventory_deducted_qty,requisition_status,special_process,
                    snapshot_product_code)
                   VALUES (?,?,?,?,?,'pending',?,?,?,0,0,0,'未报料','无',?)""",
                (sales_order_id, product["id"], item["quantity"], str(unit_price(item["unit_price"])),
                 str(money(item["amount"])), product["product_name"], spec,
                 item["material"], product["product_code"]),
            )
            connection.execute(
                """INSERT INTO migration_ruida_sales_item_map
                   (legacy_item_id,sales_order_item_id,legacy_order_id) VALUES (?,?,?)""",
                (item["legacy_item_id"], cursor.lastrowid, order["legacy_order_id"]),
            )
            inserted_items += 1
    return {"orders": inserted_orders, "items": inserted_items}


def write_outputs(report_dir: Path, timestamp: str, payload: dict[str, Any], selected: list[dict[str, Any]]) -> tuple[Path, Path]:
    report_dir.mkdir(parents=True, exist_ok=True)
    report = report_dir / f"FORMAL_SALES_SAMPLE_{payload['mode'].upper()}_{timestamp}.md"
    sample = report_dir / f"FORMAL_SALES_SAMPLE_IDS_{timestamp}.csv"
    with sample.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["legacy_order_id", "item_count", "customer_id", "amount"])
        for entry in selected:
            writer.writerow([entry["order"]["legacy_order_id"], len(entry["items"]),
                             entry["order"]["customer_id"],
                             str(sum((money(i["amount"]) for i in entry["items"]), Decimal("0.00")))])
    report.write_text("# Ruida Formal Sales Sample Migration\n\n```json\n" +
                      json.dumps(payload, ensure_ascii=False, indent=2, default=str) +
                      "\n```\n", encoding="utf-8")
    return report, sample


def main() -> int:
    args = build_parser().parse_args()
    target = args.sqlite_path.resolve()
    try:
        validate_scope(args)
        if not target.is_file():
            raise SafetyError(f"SQLite not found: {target}")
        target_hash_before = sha256(target)
        validate_apply_authorization(args, target, target_hash_before)
        uri = f"file:{target.as_posix()}?mode={'rw' if args.apply else 'ro'}"
        connection = sqlite3.connect(uri, uri=True)
        if not args.apply:
            connection.execute("PRAGMA query_only=ON")
        before = {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                  for table in ("sales_orders", "sales_order_items", "legacy_ruida_orders", "legacy_ruida_order_items")}
        approved_review: dict[tuple[int, str], int] = {}
        rejected_review: set[tuple[int, str]] = set()
        review_counts = {"approved": 0, "rejected": 0}
        review_path = args.product_review_csv.resolve() if args.product_review_csv else None
        if review_path:
            approved_review, rejected_review, review_counts = load_product_review(review_path)
        manifest_path = args.batch_manifest.resolve() if args.batch_manifest else None
        manifest_order_ids = (
            load_batch_manifest(
                manifest_path, args.batch_id, args.customer_name, review_path
            )
            if manifest_path
            else None
        )
        plan = build_plan(
            connection,
            effective_limit(args),
            args.sample_mode,
            approved_review,
            rejected_review,
            args.customer_name,
            manifest_order_ids,
        )
        generated_manifest = (
            args.generate_batch_manifest.resolve()
            if args.generate_batch_manifest
            else None
        )
        if generated_manifest:
            write_batch_manifest(
                generated_manifest, plan["selected"], review_path, args.customer_name
            )
        inserted = {"orders": 0, "items": 0}
        if args.apply:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("BEGIN IMMEDIATE")
            try:
                inserted = apply_plan(connection, plan)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        after = {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                 for table in ("sales_orders", "sales_order_items", "legacy_ruida_orders", "legacy_ruida_order_items")}
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        connection.close()
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        payload = {
            "mode": "apply" if args.apply else "dry-run", "sqlite": str(target),
            "sha256_before": target_hash_before, "sha256": sha256(target),
            "limit": effective_limit(args), "all": args.all,
            "sample_mode": args.sample_mode,
            "customer_name": args.customer_name,
            "product_review_csv": str(review_path) if review_path else None,
            "batch_manifest": str(manifest_path) if manifest_path else None,
            "batch_id": args.batch_id,
            "generated_batch_manifest": str(generated_manifest) if generated_manifest else None,
            "review_counts": review_counts,
            "planned_orders": plan["planned_orders"], "planned_items": plan["planned_items"],
            "selected_orders": plan["selected_orders"], "selected_items": plan["selected_items"],
            "already_imported": plan["already_imported"], "skip_reasons": plan["skip_reasons"],
            "customer_matching": "legacy customer_id direct; unmatched order skipped",
            "product_matching": "exact customer/style first, then approved review mapping; rejected review denies whole order",
            "approved_prefix_items": plan["approved_prefix_items"],
            "exact_match_items": plan["exact_match_items"],
            "rejected_style_hits": plan["rejected_style_hits"],
            "selected_rejected_items": plan["selected_rejected_items"],
            "multi_item_orders": plan["multi_item_orders"],
            "single_item_orders": plan["single_item_orders"],
            "selected_fee_items": plan["selected_fee_items"],
            "selected_unmatched_items": plan["selected_unmatched_items"],
            "amount_total": str(plan["amount_total"]), "inserted": inserted,
            "before": before, "after": after, "integrity_check": integrity,
            "sample_legacy_order_ids": [e["order"]["legacy_order_id"] for e in plan["selected"]],
        }
        report, sample = write_outputs(args.report_dir.resolve(), timestamp, payload, plan["selected"])
        print(json.dumps({
            k: payload[k] for k in (
                "planned_orders", "planned_items", "approved_prefix_items",
                "exact_match_items", "rejected_style_hits", "selected_rejected_items",
                "multi_item_orders", "single_item_orders", "selected_fee_items",
                "selected_unmatched_items",
                "already_imported", "skip_reasons", "amount_total", "inserted",
            )
        }, ensure_ascii=False))
        print(f"report: {report}\nsample: {sample}")
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
