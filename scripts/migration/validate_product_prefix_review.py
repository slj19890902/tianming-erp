"""Validate a manually reviewed prefix-product CSV without modifying SQLite."""

from __future__ import annotations

import argparse
import csv
import hashlib
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


REPORT_DIR = Path("docs/migration_reports")
ALLOWED_DECISIONS = {
    "approve_prefix_match",
    "reject_fee_item",
    "manual_check_required",
    "create_product_later",
    "reject_wrong_product",
}
ALLOWED_STATUSES = {"pending", "approved", "rejected", "needs_check"}
FEE_KEYWORDS = ("模具费", "制版费", "样品费", "加工费", "模具", "制版", "运费", "版费", "刀模")
REQUIRED_FIELDS = [
    "rank", "customer_id", "customer_name", "legacy_style_no_raw", "prefix_code",
    "matched_product_id", "matched_product_code", "matched_product_name", "matched_spec",
    "source_item_count", "source_order_count", "source_quantity_sum", "source_amount_sum",
    "suggested_action", "review_status", "review_decision", "approved_product_id",
    "reviewed_by", "reviewed_at", "review_note",
]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read-only validation of product prefix review CSV.")
    parser.add_argument("--sqlite-path", type=Path, required=True)
    parser.add_argument("--review-csv", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=REPORT_DIR)
    return parser


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def is_fee_item(value: Any) -> bool:
    text = str(value or "")
    return any(keyword in text for keyword in FEE_KEYWORDS)


def explicit_exception(note: str) -> bool:
    clean = note.strip()
    return len(clean) >= 10 and any(word in clean for word in ("特殊", "例外", "非费用"))


def integer(value: Any) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def decimal_value(value: Any) -> Decimal:
    try:
        return Decimal(str(value or "0").strip() or "0")
    except InvalidOperation:
        return Decimal("0")


def add_error(errors: list[dict[str, Any]], row_no: int, key: str, message: str) -> None:
    errors.append({"row": row_no, "key": key, "message": message})


def validate_review_rows(
    rows: list[dict[str, str]], products: dict[int, dict[str, Any]]
) -> dict[str, Any]:
    errors: list[dict[str, Any]] = []
    summary: Counter[str] = Counter()
    seen: set[tuple[str, str]] = set()
    approved_keys: dict[tuple[int, str], int] = {}

    for row_no, row in enumerate(rows, 2):
        status = row.get("review_status", "").strip()
        decision = row.get("review_decision", "").strip()
        approved_product_text = row.get("approved_product_id", "").strip()
        customer_text = row.get("customer_id", "").strip()
        raw_style = row.get("legacy_style_no_raw", "")
        note = row.get("review_note", "").strip()
        key = (customer_text, raw_style)

        summary["total_rows"] += 1
        if key in seen:
            add_error(errors, row_no, "|".join(key), "重复的 customer_id + legacy_style_no_raw")
        seen.add(key)

        if status not in ALLOWED_STATUSES:
            add_error(errors, row_no, key[1], f"review_status 非法：{status}")
        else:
            summary[status] += 1

        if not decision:
            if status != "pending":
                add_error(errors, row_no, key[1], "非 pending 行必须填写 review_decision")
            continue
        if decision not in ALLOWED_DECISIONS:
            add_error(errors, row_no, key[1], f"review_decision 非法：{decision}")
            continue
        summary[decision] += 1

        if not row.get("reviewed_by", "").strip() or not row.get("reviewed_at", "").strip():
            add_error(errors, row_no, key[1], "已填写决定的行必须填写 reviewed_by 和 reviewed_at")

        if decision == "approve_prefix_match":
            if status != "approved":
                add_error(errors, row_no, key[1], "approve_prefix_match 要求 review_status=approved")
            approved_id = integer(approved_product_text)
            matched_id = integer(row.get("matched_product_id"))
            customer_id = integer(customer_text)
            if approved_id is None:
                add_error(errors, row_no, key[1], "approve_prefix_match 时 approved_product_id 必填")
                continue
            product = products.get(approved_id)
            if product is None:
                add_error(errors, row_no, key[1], "approved_product_id 不存在于 products")
                continue
            if customer_id is None or int(product["customer_id"]) != customer_id:
                add_error(errors, row_no, key[1], "approved_product_id 不属于当前客户")
            if approved_id != matched_id and len(note) < 10:
                add_error(errors, row_no, key[1], "改判其他产品时 review_note 必须明确说明原因")
            if is_fee_item(raw_style) and not explicit_exception(note):
                add_error(errors, row_no, key[1], "含费用关键词，不允许批准为普通产品；特殊情况须明确说明")
            if not any(error["row"] == row_no for error in errors):
                summary["approved_mappings"] += 1
                summary["new_migratable_items"] += integer(row.get("source_item_count")) or 0
                if customer_id is not None:
                    approved_keys[(customer_id, raw_style)] = approved_id
        elif decision == "reject_fee_item":
            if approved_product_text:
                add_error(errors, row_no, key[1], "reject_fee_item 不允许填写 approved_product_id")
            if status != "rejected":
                add_error(errors, row_no, key[1], "reject_fee_item 要求 review_status=rejected")
        elif decision == "manual_check_required":
            if status != "needs_check":
                add_error(errors, row_no, key[1], "manual_check_required 要求 review_status=needs_check")
        else:
            if status != "rejected":
                add_error(errors, row_no, key[1], f"{decision} 要求 review_status=rejected")

    summary["validation_errors"] = len(errors)
    summary["approved_mappings"] += 0
    summary["new_migratable_items"] += 0
    summary["rejected_fee_items"] = summary["reject_fee_item"]
    summary["needs_manual_check"] = summary["manual_check_required"]
    summary["products_to_create_later"] = summary["create_product_later"]
    summary["wrong_candidates"] = summary["reject_wrong_product"]
    return {"errors": errors, "summary": dict(summary), "approved_keys": approved_keys}


def read_only_connection(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise FileNotFoundError(f"SQLite file does not exist: {path}")
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def load_products(connection: sqlite3.Connection) -> tuple[
    dict[int, dict[str, Any]], dict[tuple[int, str], set[int]]
]:
    products: dict[int, dict[str, Any]] = {}
    exact_map: dict[tuple[int, str], set[int]] = defaultdict(set)
    for row in connection.execute(
        "SELECT id,customer_id,product_code,customer_material_code FROM products WHERE deleted_at IS NULL"
    ):
        products[row["id"]] = dict(row)
        for value in (row["product_code"], row["customer_material_code"]):
            code = str(value or "").strip()
            if code:
                exact_map[(row["customer_id"], code)].add(row["id"])
    return products, exact_map


def estimate_order_impact(
    connection: sqlite3.Connection,
    exact_map: dict[tuple[int, str], set[int]],
    approved_keys: dict[tuple[int, str], int],
) -> dict[str, int]:
    order_customers = {
        row["legacy_order_id"]: row["customer_id"]
        for row in connection.execute("SELECT legacy_order_id,customer_id FROM legacy_ruida_orders")
    }
    items_by_order: dict[int, list[sqlite3.Row]] = defaultdict(list)
    for row in connection.execute(
        """
        SELECT legacy_order_id,customer_id,style_no,order_date,order_quantity,amount
        FROM legacy_ruida_order_items
        """
    ):
        items_by_order[row["legacy_order_id"]].append(row)

    def complete(order_id: int, include_approved: bool) -> bool:
        customer_id = order_customers.get(order_id)
        items = items_by_order.get(order_id, [])
        if not customer_id or not items:
            return False
        for item in items:
            item_customer = item["customer_id"] or customer_id
            raw = str(item["style_no"] or "")
            exact = len(exact_map.get((item_customer, raw.strip()), set())) == 1
            approved = include_approved and (item_customer, raw) in approved_keys
            valid = (
                bool(item["order_date"])
                and decimal_value(item["order_quantity"]) > 0
                and decimal_value(item["amount"]) >= 0
            )
            if not (exact or approved) or not valid:
                return False
        return True

    current = {order_id for order_id in order_customers if complete(order_id, False)}
    enhanced = {order_id for order_id in order_customers if complete(order_id, True)}
    return {
        "current_complete_orders": len(current),
        "new_migratable_orders": len(enhanced - current),
        "estimated_complete_orders": len(enhanced),
        "estimated_remaining_orders": len(order_customers) - len(enhanced),
    }


def report_text(
    sqlite_path: Path,
    review_csv: Path,
    hash_before: str,
    hash_after: str,
    integrity: str,
    result: dict[str, Any],
    impact: dict[str, int],
) -> str:
    summary = result["summary"]
    lines = [
        "# 产品前缀人工复核校验报告",
        "",
        f"- 执行时间：{datetime.now().astimezone().isoformat(timespec='seconds')}",
        "- 是否修改主库：否",
        "- 是否修改正式表：否",
        "- 是否修改产品表：否",
        f"- SQLite：`{sqlite_path}`",
        f"- 复核文件：`{review_csv}`",
        f"- integrity_check：`{integrity}`",
        f"- SHA-256 前：`{hash_before}`",
        f"- SHA-256 后：`{hash_after}`",
        f"- 哈希未变化：{'是' if hash_before == hash_after else '否'}",
        "",
        "## 校验汇总",
        "",
        f"- 总复核行数：{summary.get('total_rows', 0)}",
        f"- 当前 pending：{summary.get('pending', 0)}",
        f"- 已批准映射数：{summary.get('approved_mappings', 0)}",
        f"- 拒绝费用项数：{summary.get('rejected_fee_items', 0)}",
        f"- 需人工继续确认数：{summary.get('needs_manual_check', 0)}",
        f"- 待建产品数：{summary.get('products_to_create_later', 0)}",
        f"- 错误候选数：{summary.get('wrong_candidates', 0)}",
        f"- 可新增可迁移明细数：{summary.get('new_migratable_items', 0)}",
        f"- 可新增可迁移订单数预估：{impact['new_migratable_orders']}",
        f"- 仍不可迁移订单数预估：{impact['estimated_remaining_orders']}",
        f"- 审批错误数：{len(result['errors'])}",
        f"- 校验结果：{'通过' if not result['errors'] else '不通过'}",
        "",
        "## 审批错误列表",
        "",
    ]
    if not result["errors"]:
        lines.append("- 无。当前 worksheet 可保持全部 pending，但尚无任何批准映射可用于迁移。")
    else:
        lines.extend(
            f"- CSV 第 {error['row']} 行 `{error['key']}`：{error['message']}"
            for error in result["errors"]
        )
    lines.extend(
        [
            "",
            "## 说明",
            "",
            "- 本报告仅校验人工复核文件，不写数据库、不应用映射、不导入订单。",
            "- 只有校验通过的 `approve_prefix_match` 才计入影响模拟。",
            "- 后续仍须在隔离副本中显式加载批准结果并做小批量试迁移。",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    args = build_parser().parse_args()
    sqlite_path = args.sqlite_path.resolve()
    review_csv = args.review_csv.resolve()
    output_dir = args.output_dir.resolve()
    if not review_csv.is_file():
        raise FileNotFoundError(f"Review CSV does not exist: {review_csv}")
    output_dir.mkdir(parents=True, exist_ok=True)
    hash_before = sha256_file(sqlite_path)

    with review_csv.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = [field for field in REQUIRED_FIELDS if field not in (reader.fieldnames or [])]
        if missing:
            raise ValueError("Review CSV missing required fields: " + ", ".join(missing))
        rows = list(reader)

    with read_only_connection(sqlite_path) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"SQLite integrity_check failed: {integrity}")
        products, exact_map = load_products(connection)
        result = validate_review_rows(rows, products)
        impact = estimate_order_impact(connection, exact_map, result["approved_keys"])

    hash_after = sha256_file(sqlite_path)
    if hash_before != hash_after:
        raise RuntimeError("SQLite hash changed during read-only validation")
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report = output_dir / f"PRODUCT_PREFIX_REVIEW_VALIDATION_{timestamp}.md"
    report.write_text(
        report_text(
            sqlite_path, review_csv, hash_before, hash_after, integrity, result, impact
        ),
        encoding="utf-8",
    )
    print(f"total_rows={result['summary'].get('total_rows', 0)}")
    print(f"approved_mappings={result['summary'].get('approved_mappings', 0)}")
    print(f"pending={result['summary'].get('pending', 0)}")
    print(f"new_migratable_items={result['summary'].get('new_migratable_items', 0)}")
    print(f"new_migratable_orders={impact['new_migratable_orders']}")
    print(f"remaining_orders={impact['estimated_remaining_orders']}")
    print(f"validation_errors={len(result['errors'])}")
    print(f"main_hash_unchanged={hash_before == hash_after}")
    print(f"report={report}")
    return 0 if not result["errors"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
