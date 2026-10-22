"""Versioned read-only issue queue. Run on a managed snapshot via uat/run_task.py.

Candidates are evidence for review, never instructions to delete/merge records.
No ERP application import, ORM session, repair SQL or business writes are used.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3

RULE_VERSION = "mobile-data-quality-v1"
# Each row is one affected business object. Codes and names alone are not identities.
RULES = [
    ("ambiguous_product_code", "review", "同客户同码存在多个启用产品；核对款式、BOM和历史来源，不自动合并", "products", """
SELECT p.id, p.customer_id, p.product_code, p.product_name FROM products p
JOIN (SELECT customer_id,lower(trim(product_code)) code FROM products
 WHERE is_active=1 AND deleted_at IS NULL AND trim(coalesce(product_code,''))<>''
 GROUP BY customer_id,lower(trim(product_code)) HAVING count(*)>1) d
ON d.customer_id=p.customer_id AND d.code=lower(trim(p.product_code))
WHERE p.is_active=1 AND p.deleted_at IS NULL ORDER BY p.id"""),
    ("inactive_customer_product", "review", "客户已停用但产品仍启用；核对现有库存和未结单据后决定新业务候选资格", "products", """
SELECT p.id,p.customer_id FROM products p JOIN customers c ON c.id=p.customer_id
WHERE p.is_active=1 AND p.deleted_at IS NULL AND c.status='inactive' ORDER BY p.id"""),
    ("missing_product_unit", "high", "启用产品缺少单位；补齐前不能据此换算数量，历史事实保持", "products", """
SELECT id,customer_id,product_code FROM products WHERE is_active=1 AND deleted_at IS NULL
AND trim(coalesce(unit,''))='' ORDER BY id"""),
    ("negative_inventory_bucket", "high", "库存数量分项出现负数；核对流水和原单据，不直接重算余额", "inventory_lots", """
SELECT id,quantity_available,quantity_reserved,quantity_consumed,quantity_damaged,quantity_scrapped
FROM inventory_lots WHERE quantity_available<0 OR quantity_reserved<0 OR quantity_consumed<0
OR quantity_damaged<0 OR quantity_scrapped<0 ORDER BY id"""),
    ("reservation_balance", "high", "批次预占与未消耗预占台账不一致；追查原请求、释放和消耗流水", "inventory_lots", """
SELECT l.id,l.quantity_reserved,coalesce(r.open_quantity,0) open_quantity FROM inventory_lots l
LEFT JOIN (SELECT inventory_lot_id,sum(reserved_stock_quantity-consumed_stock_quantity-released_stock_quantity) open_quantity
FROM inventory_reservations WHERE status IN ('active','partial') GROUP BY inventory_lot_id) r
ON r.inventory_lot_id=l.id WHERE l.quantity_reserved<>coalesce(r.open_quantity,0) ORDER BY l.id"""),
    ("stale_requisition_draft", "review", "草稿超过30天待确认用途；可能仍有效，不自动作废或删除", "material_requisitions", """
SELECT id,created_at,status FROM material_requisitions WHERE status='draft'
AND julianday(:as_of)-julianday(created_at)>30 ORDER BY id"""),
]


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def scan(database: Path, *, captured_at: str, as_of: str, expected_sha256: str | None = None) -> dict:
    database = database.resolve(strict=True)
    captured = datetime.fromisoformat(captured_at)
    cutoff = datetime.fromisoformat(as_of)
    if captured.tzinfo is None or cutoff.tzinfo is None or captured > cutoff:
        raise ValueError("快照时点和检查时点必须带时区且顺序正确")
    if any(Path(str(database) + suffix).exists() for suffix in ("-wal", "-journal")):
        raise ValueError("需要已一致封存的独立快照，不能遗漏WAL或活动事务")
    before = digest(database)
    if expected_sha256 and before != expected_sha256:
        raise ValueError("数据库摘要与指定来源不符")
    result = dict(rule_version=RULE_VERSION, source=str(database), source_sha256=before,
                  source_captured_at=captured.isoformat(), as_of=cutoff.isoformat(),
                  snapshot_age_hours=round((cutoff-captured).total_seconds()/3600, 2),
                  generated_at=datetime.now(timezone.utc).isoformat(),
                  readonly=True, repairs_executed=False, issues=[], checks=[])
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        result["integrity"] = [row[0] for row in connection.execute("PRAGMA integrity_check")]
        result["foreign_key_violations"] = [dict(row) for row in connection.execute("PRAGMA foreign_key_check")]
        result["revision"] = [row[0] for row in connection.execute("SELECT version_num FROM alembic_version")]
        for rule, severity, action, table, sql in RULES:
            try:
                rows = [dict(row) for row in connection.execute(sql, {"as_of": cutoff.isoformat()})]
            except sqlite3.Error as error:
                result["checks"].append(dict(rule=rule, status="unavailable", reason=str(error), sql=sql))
                continue
            result["checks"].append(dict(rule=rule, status="checked", count=len(rows), sql=sql))
            for row in rows:
                result["issues"].append(dict(issue_id=f"{rule}:{row['id']}", rule=rule, severity=severity,
                    table=table, object_id=row["id"], state="needs_review", evidence=row,
                    next_action=action, repair_preview={"executable": False, "requires_current_evidence": True}))
        connection.rollback()
    after = digest(database)
    if before != after:
        raise ValueError("检查期间来源发生变化，报告不可用")
    result["database_unchanged"] = True
    result["counts"] = dict(Counter(issue["rule"] for issue in result["issues"]))
    result["complete"] = all(check["status"] == "checked" for check in result["checks"])
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--captured-at", required=True, help="源快照的真实时点，不能用副本mtime代替")
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--expected-sha256")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    # The existing runner owns source selection and copying. Refuse a formal path.
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from app.core.uat_isolation import validate_uat_environment
    validate_uat_environment()
    database = Path(os.environ["ERP_DATABASE_PATH"])
    report = scan(database, captured_at=args.captured_at, as_of=args.as_of, expected_sha256=args.expected_sha256)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # Keep each scan immutable; callers choose a new evidence path for a rerun.
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    print(json.dumps({key: report[key] for key in ("rule_version", "source_captured_at", "snapshot_age_hours", "complete", "counts", "database_unchanged", "repairs_executed")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
