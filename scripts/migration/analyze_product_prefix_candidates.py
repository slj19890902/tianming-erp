"""Read-only slash-prefix product candidate analysis for Ruida legacy items."""

from __future__ import annotations

import argparse
import csv
import hashlib
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


REPORT_DIR = Path("docs/migration_reports")
TIANHUA_NAME = "苏州天华超净科技股份有限公司"
FEE_KEYWORDS = ("模具费", "制版费", "样品费", "加工费", "模具", "制版", "运费", "版费", "刀模")
SLASH_RE = re.compile(r"[/／]")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read-only slash-prefix product candidate analysis.")
    parser.add_argument("--sqlite-path", type=Path, required=True)
    parser.add_argument("--customer-name")
    parser.add_argument("--top", type=int, default=200)
    parser.add_argument("--output-dir", type=Path, default=REPORT_DIR)
    return parser


def extract_prefix(value: Any) -> str | None:
    text = str(value or "")
    match = SLASH_RE.search(text)
    if not match:
        return None
    prefix = text[: match.start()].strip()
    return prefix or None


def is_fee_item(value: Any) -> bool:
    text = str(value or "")
    return any(keyword in text for keyword in FEE_KEYWORDS)


def suggested_action(match_status: str, fee_item: bool) -> str:
    if fee_item:
        return "reject_fee_item"
    if match_status == "prefix_unique":
        return "approve_prefix_match"
    if match_status == "prefix_multi_match":
        return "manual_check_required"
    if match_status == "prefix_no_match":
        return "create_product_later"
    return "unclear"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def decimal_value(value: Any) -> Decimal:
    if value in (None, ""):
        return Decimal("0")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def money_text(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01")))


def quantity_text(value: Decimal) -> str:
    normalized = value.normalize()
    return format(normalized, "f")


def format_spec(product: dict[str, Any]) -> str:
    dimensions = (product["length_mm"], product["width_mm"], product["height_mm"])
    if not any(value is not None for value in dimensions):
        return ""

    def part(value: Any) -> str:
        if value is None:
            return ""
        number = Decimal(str(value))
        return format(number.normalize(), "f")

    return "×".join(part(value) for value in dimensions) + "mm"


def read_only_connection(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise FileNotFoundError(f"SQLite file does not exist: {path}")
    uri = path.resolve().as_uri() + "?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def require_tables(connection: sqlite3.Connection) -> None:
    required = {"legacy_ruida_orders", "legacy_ruida_order_items", "customers", "products", "materials"}
    present = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN (?,?,?,?,?)",
            tuple(sorted(required)),
        )
    }
    missing = sorted(required - present)
    if missing:
        raise RuntimeError("Missing required tables: " + ", ".join(missing))


def load_products(
    connection: sqlite3.Connection,
) -> tuple[dict[tuple[int, str], dict[int, dict[str, Any]]], dict[int, dict[str, Any]]]:
    by_key: dict[tuple[int, str], dict[int, dict[str, Any]]] = defaultdict(dict)
    by_id: dict[int, dict[str, Any]] = {}
    rows = connection.execute(
        """
        SELECT p.id, p.customer_id, p.product_code, p.customer_material_code,
               p.product_name, p.length_mm, p.width_mm, p.height_mm,
               COALESCE(NULLIF(TRIM(p.legacy_material_text), ''), m.code, '') AS material
        FROM products p
        LEFT JOIN materials m ON m.id = p.material_id
        WHERE p.deleted_at IS NULL
        """
    )
    for row in rows:
        product = dict(row)
        by_id[product["id"]] = product
        for raw_code in (product["product_code"], product["customer_material_code"]):
            code = str(raw_code or "").strip()
            if code:
                by_key[(product["customer_id"], code)][product["id"]] = product
    return by_key, by_id


def load_items(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in connection.execute(
            """
            SELECT i.legacy_item_id, i.legacy_order_id, i.customer_id, i.style_no,
                   i.order_quantity, i.amount, i.order_date,
                   COALESCE(c.name, o.customer_name, '') AS customer_name,
                   o.customer_id AS order_customer_id
            FROM legacy_ruida_order_items i
            LEFT JOIN legacy_ruida_orders o ON o.legacy_order_id = i.legacy_order_id
            LEFT JOIN customers c ON c.id = COALESCE(i.customer_id, o.customer_id)
            """
        )
    ]


def match_products(
    product_map: dict[tuple[int, str], dict[int, dict[str, Any]]],
    customer_id: Any,
    code: Any,
) -> dict[int, dict[str, Any]]:
    if not customer_id:
        return {}
    clean_code = str(code or "").strip()
    if not clean_code:
        return {}
    return product_map.get((int(customer_id), clean_code), {})


def risk_note(status: str, fee_item: bool) -> str:
    if fee_item:
        return "疑似费用或工装项，禁止自动映射为普通纸箱产品"
    if status == "prefix_unique":
        return "同客户斜杠前编码唯一命中；仍需人工核对描述、规格和材质"
    if status == "prefix_multi_match":
        return "同客户存在多个产品命中，禁止自动匹配"
    return "斜杠前编码未命中产品，需人工查找或后续建产品"


def candidate_rows(
    items: list[dict[str, Any]],
    product_map: dict[tuple[int, str], dict[int, dict[str, Any]]],
    customer_name_filter: str | None,
) -> tuple[list[dict[str, Any]], Counter[str]]:
    groups: dict[tuple[Any, str, str], dict[str, Any]] = {}
    item_status_counts: Counter[str] = Counter()
    for item in items:
        raw = str(item["style_no"] or "")
        customer_id = item["customer_id"] or item["order_customer_id"]
        exact = match_products(product_map, customer_id, raw)
        prefix = extract_prefix(raw)
        if len(exact) == 1 or prefix is None:
            continue
        if customer_name_filter and item["customer_name"] != customer_name_filter:
            continue
        matches = match_products(product_map, customer_id, prefix)
        if len(matches) == 1:
            status = "prefix_unique"
        elif len(matches) > 1:
            status = "prefix_multi_match"
        else:
            status = "prefix_no_match"
        item_status_counts[status] += 1
        key = (customer_id, raw, prefix)
        group = groups.setdefault(
            key,
            {
                "customer_id": customer_id,
                "customer_name": item["customer_name"] or "",
                "legacy_style_no_raw": raw,
                "prefix_code": prefix,
                "matches": matches,
                "source_item_count": 0,
                "order_ids": set(),
                "source_quantity_sum": Decimal("0"),
                "source_amount_sum": Decimal("0"),
                "first_order_date": None,
                "last_order_date": None,
                "match_status": status,
                "fee_item": is_fee_item(raw),
            },
        )
        group["source_item_count"] += 1
        if item["legacy_order_id"] is not None:
            group["order_ids"].add(item["legacy_order_id"])
        group["source_quantity_sum"] += decimal_value(item["order_quantity"])
        group["source_amount_sum"] += decimal_value(item["amount"])
        date = str(item["order_date"] or "").strip()
        if date:
            group["first_order_date"] = min(filter(None, (group["first_order_date"], date)))
            group["last_order_date"] = max(filter(None, (group["last_order_date"], date)))

    rows: list[dict[str, Any]] = []
    for group in groups.values():
        products = list(group["matches"].values())
        product = products[0] if len(products) == 1 else None
        rows.append(
            {
                "customer_id": group["customer_id"],
                "customer_name": group["customer_name"],
                "legacy_style_no_raw": group["legacy_style_no_raw"],
                "prefix_code": group["prefix_code"],
                "matched_product_id": product["id"] if product else "|".join(str(p["id"]) for p in products),
                "matched_product_code": product["product_code"] if product else "|".join(str(p["product_code"]) for p in products),
                "matched_customer_material_code": (
                    product["customer_material_code"]
                    if product
                    else "|".join(str(p["customer_material_code"]) for p in products)
                ),
                "matched_product_name": product["product_name"] if product else "|".join(str(p["product_name"]) for p in products),
                "matched_spec": format_spec(product) if product else "",
                "matched_material": product["material"] if product else "",
                "source_item_count": group["source_item_count"],
                "source_order_count": len(group["order_ids"]),
                "source_quantity_sum": quantity_text(group["source_quantity_sum"]),
                "source_amount_sum": money_text(group["source_amount_sum"]),
                "first_order_date": group["first_order_date"] or "",
                "last_order_date": group["last_order_date"] or "",
                "match_status": group["match_status"],
                "review_status": "pending",
                "risk_note": risk_note(group["match_status"], group["fee_item"]),
                "_fee_item": group["fee_item"],
            }
        )
    rows.sort(
        key=lambda row: (
            row["customer_name"],
            -decimal_value(row["source_amount_sum"]),
            -row["source_item_count"],
            row["legacy_style_no_raw"],
        )
    )
    return rows, item_status_counts


def fee_analysis(items: list[dict[str, Any]]) -> dict[str, Any]:
    groups: dict[str, dict[str, Any]] = {}
    orders: set[Any] = set()
    total_amount = Decimal("0")
    item_count = 0
    for item in items:
        raw = str(item["style_no"] or "")
        if not is_fee_item(raw):
            continue
        item_count += 1
        amount = decimal_value(item["amount"])
        total_amount += amount
        if item["legacy_order_id"] is not None:
            orders.add(item["legacy_order_id"])
        group = groups.setdefault(
            raw,
            {
                "style_no": raw,
                "item_count": 0,
                "order_ids": set(),
                "quantity_sum": Decimal("0"),
                "amount_sum": Decimal("0"),
            },
        )
        group["item_count"] += 1
        group["order_ids"].add(item["legacy_order_id"])
        group["quantity_sum"] += decimal_value(item["order_quantity"])
        group["amount_sum"] += amount
    top = sorted(groups.values(), key=lambda row: (-row["amount_sum"], -row["item_count"], row["style_no"]))[:50]
    return {
        "item_count": item_count,
        "amount_sum": total_amount,
        "order_count": len(orders),
        "top": top,
    }


def migration_simulation(
    connection: sqlite3.Connection,
    items: list[dict[str, Any]],
    product_map: dict[tuple[int, str], dict[int, dict[str, Any]]],
) -> dict[str, Any]:
    order_customer = {
        row["legacy_order_id"]: row["customer_id"]
        for row in connection.execute("SELECT legacy_order_id, customer_id FROM legacy_ruida_orders")
    }
    by_order: dict[Any, list[dict[str, Any]]] = defaultdict(list)
    current_matchable_items = 0
    enhanced_matchable_items = 0
    remaining_reasons: Counter[str] = Counter()

    for item in items:
        by_order[item["legacy_order_id"]].append(item)
        customer_id = item["customer_id"] or item["order_customer_id"]
        raw = str(item["style_no"] or "")
        exact = match_products(product_map, customer_id, raw)
        exact_ok = len(exact) == 1 and not is_fee_item(raw)
        prefix = extract_prefix(raw)
        prefix_matches = match_products(product_map, customer_id, prefix)
        prefix_ok = not exact_ok and prefix is not None and len(prefix_matches) == 1 and not is_fee_item(raw)
        if exact_ok:
            current_matchable_items += 1
        if exact_ok or prefix_ok:
            enhanced_matchable_items += 1
        else:
            if is_fee_item(raw):
                remaining_reasons["suspected_fee_item"] += 1
            elif not raw.strip():
                remaining_reasons["blank_style_no"] += 1
            elif prefix is not None and len(prefix_matches) > 1:
                remaining_reasons["prefix_multi_match"] += 1
            elif prefix is not None:
                remaining_reasons["prefix_no_match"] += 1
            else:
                remaining_reasons["no_slash_or_exact_match"] += 1

    def item_valid(item: dict[str, Any]) -> bool:
        return bool(item["order_date"]) and decimal_value(item["order_quantity"]) > 0 and decimal_value(item["amount"]) >= 0

    def order_complete(order_id: Any, enhanced: bool) -> bool:
        order_items = by_order.get(order_id, [])
        if not order_items or not order_customer.get(order_id):
            return False
        for item in order_items:
            customer_id = item["customer_id"] or item["order_customer_id"]
            raw = str(item["style_no"] or "")
            matches = match_products(product_map, customer_id, raw)
            matched = len(matches) == 1 and not is_fee_item(raw)
            if enhanced and not matched:
                prefix = extract_prefix(raw)
                matched = (
                    prefix is not None
                    and len(match_products(product_map, customer_id, prefix)) == 1
                    and not is_fee_item(raw)
                )
            if not matched or not item_valid(item):
                return False
        return True

    current_orders = {order_id for order_id in order_customer if order_complete(order_id, False)}
    enhanced_orders = {order_id for order_id in order_customer if order_complete(order_id, True)}
    current_multi = sum(1 for order_id in current_orders if len(by_order[order_id]) >= 2)
    enhanced_multi = sum(1 for order_id in enhanced_orders if len(by_order[order_id]) >= 2)
    enhanced_complete_items = sum(len(by_order[order_id]) for order_id in enhanced_orders)
    return {
        "total_orders": len(order_customer),
        "total_items": len(items),
        "current_complete_orders": len(current_orders),
        "enhanced_complete_orders": len(enhanced_orders),
        "complete_order_increase": len(enhanced_orders) - len(current_orders),
        "current_matchable_items": current_matchable_items,
        "enhanced_matchable_items": enhanced_matchable_items,
        "matchable_item_increase": enhanced_matchable_items - current_matchable_items,
        "enhanced_complete_order_items": enhanced_complete_items,
        "remaining_orders": len(order_customer) - len(enhanced_orders),
        "remaining_items": len(items) - enhanced_matchable_items,
        "current_complete_multi_item_orders": current_multi,
        "enhanced_complete_multi_item_orders": enhanced_multi,
        "remaining_reasons": remaining_reasons,
    }


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def tianhua_rows(rows: list[dict[str, Any]], top: int) -> list[dict[str, Any]]:
    selected = [row for row in rows if row["customer_name"] == TIANHUA_NAME]
    selected.sort(
        key=lambda row: (
            -decimal_value(row["source_amount_sum"]),
            -row["source_item_count"],
            row["legacy_style_no_raw"],
        )
    )
    result = []
    for rank, row in enumerate(selected[:top], 1):
        result.append(
            {
                "rank": rank,
                "legacy_style_no_raw": row["legacy_style_no_raw"],
                "prefix_code": row["prefix_code"],
                "matched_product_id": row["matched_product_id"],
                "matched_product_code": row["matched_product_code"],
                "matched_product_name": row["matched_product_name"],
                "matched_spec": row["matched_spec"],
                "source_item_count": row["source_item_count"],
                "source_order_count": row["source_order_count"],
                "source_quantity_sum": row["source_quantity_sum"],
                "source_amount_sum": row["source_amount_sum"],
                "suggested_action": suggested_action(row["match_status"], row["_fee_item"]),
                "review_status": "pending",
                "review_note": "",
            }
        )
    return result


def markdown_report(
    timestamp: str,
    sqlite_path: Path,
    hash_before: str,
    hash_after: str,
    rows: list[dict[str, Any]],
    item_status_counts: Counter[str],
    tianhua: list[dict[str, Any]],
    fees: dict[str, Any],
    simulation: dict[str, Any],
    all_csv: Path,
    tianhua_csv: Path,
) -> str:
    group_counts = Counter(row["match_status"] for row in rows)
    tianhua_status = Counter(
        row["match_status"] for row in rows if row["customer_name"] == TIANHUA_NAME
    )
    lines = [
        "# 斜杠前编码产品候选映射只读分析报告",
        "",
        f"- 执行时间：{datetime.now().astimezone().isoformat(timespec='seconds')}",
        "- 本轮是否修改主库：否",
        "- 是否修改正式表：否",
        "- 是否修改产品表：否",
        f"- SQLite 主库：`{sqlite_path.resolve()}`",
        f"- 主库 SHA-256（前）：`{hash_before}`",
        f"- 主库 SHA-256（后）：`{hash_after}`",
        f"- 哈希是否未变化：{'是' if hash_before == hash_after else '否'}",
        "",
        "## 斜杠前编码候选总览",
        "",
        "| 状态 | 明细数 | 聚合候选行数 |",
        "|---|---:|---:|",
        f"| prefix_unique | {item_status_counts['prefix_unique']:,} | {group_counts['prefix_unique']:,} |",
        f"| prefix_multi_match | {item_status_counts['prefix_multi_match']:,} | {group_counts['prefix_multi_match']:,} |",
        f"| prefix_no_match | {item_status_counts['prefix_no_match']:,} | {group_counts['prefix_no_match']:,} |",
        "",
        "- 统计口径仅包含原始 `style_no` 含 `/` 或 `／`、且当前精确规则未唯一命中的明细；"
        "因此 `prefix_no_match` 不包含无斜杠、空款号等其他剩余缺口。",
        "- 与旧报告约数的差异：本次按产品 ID 去重后复算，斜杠记录严格口径为 "
        f"{item_status_counts['prefix_unique']:,}/{item_status_counts['prefix_multi_match']:,}/"
        f"{item_status_counts['prefix_no_match']:,}。",
        "",
        f"- 全量候选 CSV：`{all_csv.as_posix()}`",
        "- 候选仅供人工复核，`review_status` 全部为 `pending`，未自动批准或写库。",
        "",
        "## 天华超净专项统计",
        "",
        f"- Top 复核清单行数：{len(tianhua):,}",
        f"- prefix_unique 聚合候选：{tianhua_status['prefix_unique']:,}",
        f"- prefix_multi_match 聚合候选：{tianhua_status['prefix_multi_match']:,}",
        f"- prefix_no_match 聚合候选：{tianhua_status['prefix_no_match']:,}",
        f"- 专项 CSV：`{tianhua_csv.as_posix()}`",
        "",
        "## 疑似费用项统计",
        "",
        f"- 疑似费用项明细数：{fees['item_count']:,}",
        f"- 疑似费用项金额合计：{money_text(fees['amount_sum'])}",
        f"- 受影响订单数：{fees['order_count']:,}",
        "",
        "| 排名 | 原始款号 | 明细数 | 订单数 | 数量合计 | 金额合计 |",
        "|---:|---|---:|---:|---:|---:|",
    ]
    for rank, row in enumerate(fees["top"], 1):
        safe_style = row["style_no"].replace("|", "\\|").replace("\n", " ")
        lines.append(
            f"| {rank} | {safe_style} | {row['item_count']:,} | {len(row['order_ids']):,} | "
            f"{quantity_text(row['quantity_sum'])} | {money_text(row['amount_sum'])} |"
        )
    lines.extend(
        [
            "",
            "费用项不得自动映射为普通纸箱产品。建议后续单独定义费用类产品或迁移排除规则，并保留人工审批。",
            "",
            "## 批准 prefix_unique 后的迁移覆盖模拟",
            "",
            f"- 当前完整可迁移订单数：{simulation['current_complete_orders']:,}",
            f"- 预计完整可迁移订单数：{simulation['enhanced_complete_orders']:,}",
            f"- 完整订单预计增加：{simulation['complete_order_increase']:,}",
            f"- 当前可迁移明细数（排除疑似费用项）：{simulation['current_matchable_items']:,}",
            f"- 预计可迁移明细数（排除疑似费用项）：{simulation['enhanced_matchable_items']:,}",
            f"- 可迁移明细预计增加：{simulation['matchable_item_increase']:,}",
            f"- 完整订单内预计明细数：{simulation['enhanced_complete_order_items']:,}",
            f"- 仍不可完整迁移订单数：{simulation['remaining_orders']:,}",
            f"- 仍不可匹配明细数：{simulation['remaining_items']:,}",
            f"- 完整多明细订单覆盖：{simulation['current_complete_multi_item_orders']:,} → "
            f"{simulation['enhanced_complete_multi_item_orders']:,}",
            "",
            "主要剩余原因（明细口径）：",
            "",
        ]
    )
    for reason, count in simulation["remaining_reasons"].most_common():
        lines.append(f"- `{reason}`：{count:,}")
    lines.extend(
        [
            "",
            "## 人工复核建议",
            "",
            "1. 优先复核金额高、出现频次高的 `prefix_unique`，同时核对产品名称、规格、材质。",
            "2. `prefix_multi_match` 必须逐条指定产品，不能按前缀自动决定。",
            "3. 疑似费用项先标记拒绝普通产品映射，再决定排除或建立费用类产品。",
            "4. `prefix_no_match` 进入后续人工产品主数据整理，不在本轮创建产品。",
            "",
            "## 风险点",
            "",
            "- 斜杠前内容可能是简称、旧编码或费用编码，唯一命中不等于业务语义必然一致。",
            "- 同一原始款号按客户聚合，金额和数量用于排序，不代表自动批准依据。",
            "- 覆盖模拟按当前产品资料和历史数据快照计算；产品资料变化后需重新生成。",
            "",
            "## 下一步建议",
            "",
            "仅对人工确认通过的候选形成独立审批结果文件，再设计只读校验和副本迁移验证；不要直接改产品表或主库正式表。",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    args = build_parser().parse_args()
    if args.top < 1:
        raise ValueError("--top must be at least 1")
    sqlite_path = args.sqlite_path.resolve()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    hash_before = sha256_file(sqlite_path)

    with read_only_connection(sqlite_path) as connection:
        require_tables(connection)
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"SQLite integrity_check failed: {integrity}")
        products, _ = load_products(connection)
        items = load_items(connection)
        rows, item_status_counts = candidate_rows(items, products, args.customer_name)
        fees = fee_analysis(items)
        simulation = migration_simulation(connection, items, products)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    all_csv = output_dir / f"PRODUCT_PREFIX_CANDIDATES_{timestamp}.csv"
    tianhua_csv = output_dir / f"PRODUCT_PREFIX_TIANHUA_TOP_REVIEW_{timestamp}.csv"
    report = output_dir / f"PRODUCT_PREFIX_CANDIDATES_REPORT_{timestamp}.md"
    all_fields = [
        "customer_id", "customer_name", "legacy_style_no_raw", "prefix_code",
        "matched_product_id", "matched_product_code", "matched_customer_material_code",
        "matched_product_name", "matched_spec", "matched_material", "source_item_count",
        "source_order_count", "source_quantity_sum", "source_amount_sum",
        "first_order_date", "last_order_date", "match_status", "review_status", "risk_note",
    ]
    write_csv(all_csv, rows, all_fields)
    tianhua = tianhua_rows(rows, args.top)
    tianhua_fields = [
        "rank", "legacy_style_no_raw", "prefix_code", "matched_product_id",
        "matched_product_code", "matched_product_name", "matched_spec", "source_item_count",
        "source_order_count", "source_quantity_sum", "source_amount_sum", "suggested_action",
        "review_status", "review_note",
    ]
    write_csv(tianhua_csv, tianhua, tianhua_fields)
    hash_after = sha256_file(sqlite_path)
    if hash_before != hash_after:
        raise RuntimeError("Main SQLite hash changed during read-only analysis")
    report.write_text(
        markdown_report(
            timestamp, sqlite_path, hash_before, hash_after, rows, item_status_counts,
            tianhua, fees, simulation, all_csv, tianhua_csv,
        ),
        encoding="utf-8",
    )
    print(f"integrity_check={integrity}")
    print(f"prefix_unique={item_status_counts['prefix_unique']}")
    print(f"prefix_multi_match={item_status_counts['prefix_multi_match']}")
    print(f"prefix_no_match={item_status_counts['prefix_no_match']}")
    print(f"fee_items={fees['item_count']}")
    print(f"fee_amount={money_text(fees['amount_sum'])}")
    print(f"current_complete_orders={simulation['current_complete_orders']}")
    print(f"enhanced_complete_orders={simulation['enhanced_complete_orders']}")
    print(f"enhanced_matchable_items={simulation['enhanced_matchable_items']}")
    print(f"main_hash_unchanged={hash_before == hash_after}")
    print(f"candidates_csv={all_csv}")
    print(f"tianhua_csv={tianhua_csv}")
    print(f"report={report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
