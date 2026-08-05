from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.database import create_sqlite_engine  # noqa: E402
from app.models.customer import Customer  # noqa: E402
from app.models.product import Product  # noqa: E402
from app.models.user import User  # noqa: E402
from app.services.master_data_versioning import apply_versioned_update  # noqa: E402


TARGET_COUNTS = {"YKE": 100, "KEW": 31}
TARGET_TOTAL = sum(TARGET_COUNTS.values())
BATCH_ID = "YKE_KEW_REPORT_NOTES_CLEAR_20260805"
SOURCE = "scripts.admin.clear_yke_kew_import_report_notes"
REASON = "老板明确要求：清除 YKE/KEW 导入时误写入的报料备注，原始追踪证据保留在冻结清单、版本历史和备份中"
CONFIRMATION = "CLEAR_YKE_KEW_IMPORT_REPORT_NOTES"
PROTECTED_TABLES = (
    "customers",
    "products",
    "product_drawings",
    "sales_orders",
    "sales_order_items",
    "supplier_requisition_orders",
    "supplier_requisition_order_items",
    "inventory_lots",
    "finished_goods_lots",
    "delivery_orders",
    "materials",
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def database_checks(path: Path) -> dict[str, Any]:
    with closing(sqlite3.connect(path, timeout=30)) as connection:
        connection.execute("PRAGMA busy_timeout = 30000")
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
        table_names = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        counts = {
            table: int(
                connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            )
            for table in PROTECTED_TABLES
            if table in table_names
        }
        counts["master_data_object_versions"] = int(
            connection.execute(
                "SELECT COUNT(*) FROM master_data_object_versions"
            ).fetchone()[0]
        )
        counts["operation_logs"] = int(
            connection.execute("SELECT COUNT(*) FROM operation_logs").fetchone()[0]
        )
    return {
        "integrity_check": integrity,
        "foreign_key_violations": len(foreign_keys),
        "counts": counts,
    }


def online_backup(source: Path, backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    target = backup_dir / (
        f"carton_erp_{stamp}_before_YKE_KEW_report_notes_clear.sqlite3"
    )
    with closing(sqlite3.connect(source, timeout=30)) as source_connection:
        source_connection.execute("PRAGMA busy_timeout = 30000")
        with closing(sqlite3.connect(target, timeout=30)) as backup_connection:
            source_connection.backup(backup_connection)
    return target


def build_plan(db: Session) -> dict[str, Any]:
    rows = db.execute(
        select(Product, Customer.customer_code)
        .join(Customer, Customer.id == Product.customer_id)
        .where(Customer.customer_code.in_(TARGET_COUNTS))
        .order_by(Customer.customer_code, Product.product_code, Product.id)
    ).all()
    actual_counts: dict[str, int] = {code: 0 for code in TARGET_COUNTS}
    products: list[dict[str, Any]] = []
    nonblank = 0
    blank = 0
    for product, customer_code in rows:
        code = str(customer_code)
        actual_counts[code] += 1
        note = str(product.report_notes or "").strip()
        if note:
            nonblank += 1
            expected_prefix = f"来源：{code}!原表第"
            if not note.startswith(expected_prefix):
                raise RuntimeError(
                    f"{code}/{product.product_code} 的报料备注不是本批导入追踪格式，拒绝清理"
                )
        else:
            blank += 1
        if str(product.base_report_notes or "").strip():
            raise RuntimeError(
                f"{code}/{product.product_code} 存在底片报料备注，不属于本次清理范围"
            )
        products.append(
            {
                "id": int(product.id),
                "customer_code": code,
                "product_code": product.product_code,
                "version": int(product.version),
                "manual_modified": bool(product.manual_modified),
                "report_notes_sha256": (
                    hashlib.sha256(note.encode("utf-8")).hexdigest() if note else None
                ),
            }
        )
    if actual_counts != TARGET_COUNTS:
        raise RuntimeError(
            f"YKE/KEW 常用箱数量与正式导入门禁不一致：{actual_counts}"
        )
    if len(products) != TARGET_TOTAL:
        raise RuntimeError(f"目标常用箱总数不是 {TARGET_TOTAL}")
    if nonblank == TARGET_TOTAL and blank == 0:
        unexpected = [
            row
            for row in products
            if row["version"] != 1 or row["manual_modified"]
        ]
        if unexpected:
            raise RuntimeError(
                "目标报料备注存在导入后人工修改或版本变化，拒绝覆盖"
            )
        status = "ready"
    elif blank == TARGET_TOTAL and nonblank == 0:
        status = "already_applied"
    else:
        raise RuntimeError(
            f"目标数据处于部分清理状态：有备注 {nonblank} 条、空备注 {blank} 条"
        )
    return {
        "status": status,
        "target_counts": actual_counts,
        "target_total": len(products),
        "nonblank_report_notes": nonblank,
        "blank_report_notes": blank,
        "manual_modified": sum(1 for row in products if row["manual_modified"]),
        "versions": sorted({row["version"] for row in products}),
        "products": products,
    }


def inspect_plan(database: Path) -> dict[str, Any]:
    engine = create_sqlite_engine(database, busy_timeout_ms=30_000)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        with factory() as db:
            return build_plan(db)
    finally:
        engine.dispose()


def apply_clear(
    *,
    database: Path,
    backup_dir: Path,
    actor_username: str,
) -> dict[str, Any]:
    before_checks = database_checks(database)
    if (
        before_checks["integrity_check"].lower() != "ok"
        or before_checks["foreign_key_violations"] != 0
    ):
        raise RuntimeError(f"正式库写入前检查失败：{before_checks}")
    preflight = inspect_plan(database)
    if preflight["status"] == "already_applied":
        return {
            "changed": False,
            "message": "YKE/KEW 导入报料备注已经全部清空",
            "checks": before_checks,
            "plan": preflight,
        }

    backup = online_backup(database, backup_dir)
    backup_checks = database_checks(backup)
    backup_plan = inspect_plan(backup)
    if (
        backup_checks != before_checks
        or backup_plan != preflight
        or backup_checks["integrity_check"].lower() != "ok"
        or backup_checks["foreign_key_violations"] != 0
    ):
        raise RuntimeError("在线备份未通过完整性、表计数或目标数据一致性检查")

    engine = create_sqlite_engine(database, busy_timeout_ms=30_000)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        with factory() as db:
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
            actor = db.scalar(select(User).where(User.username == actor_username))
            if actor is None or not actor.is_active or actor.role not in {"admin", "boss"}:
                raise RuntimeError("执行人必须是启用的 admin 或 boss 账号")
            live_plan = build_plan(db)
            if live_plan["status"] != "ready" or live_plan != preflight:
                raise RuntimeError("备份后目标数据已变化，拒绝继续写入")
            products = {
                product.id: product
                for product in db.scalars(
                    select(Product).where(
                        Product.id.in_([row["id"] for row in live_plan["products"]])
                    )
                ).all()
            }
            if len(products) != TARGET_TOTAL:
                raise RuntimeError("事务内目标常用箱数量变化，拒绝继续")
            for row in live_plan["products"]:
                product = products[row["id"]]
                apply_versioned_update(
                    db,
                    object_type="product",
                    entity=product,
                    updates={"report_notes": None},
                    expected_version=row["version"],
                    user=actor,
                    reason=REASON,
                    source=SOURCE,
                    action="system_consistency_fix",
                )
            db.commit()
    finally:
        engine.dispose()

    after_checks = database_checks(database)
    for table in PROTECTED_TABLES:
        if before_checks["counts"].get(table) != after_checks["counts"].get(table):
            raise RuntimeError(f"保护表 {table} 的行数发生变化，请立即使用备份核对")
    if (
        after_checks["counts"]["master_data_object_versions"]
        - before_checks["counts"]["master_data_object_versions"]
        != TARGET_TOTAL
    ):
        raise RuntimeError("主数据版本增量不是 131 条")
    if (
        after_checks["counts"]["operation_logs"]
        - before_checks["counts"]["operation_logs"]
        != TARGET_TOTAL
    ):
        raise RuntimeError("操作日志增量不是 131 条")
    if (
        after_checks["integrity_check"].lower() != "ok"
        or after_checks["foreign_key_violations"] != 0
    ):
        raise RuntimeError("清理后数据库完整性检查失败，请使用备份恢复")
    result_plan = inspect_plan(database)
    if result_plan["status"] != "already_applied":
        raise RuntimeError("清理后回读仍存在 YKE/KEW 导入报料备注")
    return {
        "changed": True,
        "batch_id": BATCH_ID,
        "backup": {
            "path": str(backup),
            "sha256": sha256_file(backup),
            "size": backup.stat().st_size,
            "checks": backup_checks,
        },
        "checks_before": before_checks,
        "checks_after": after_checks,
        "plan_before": preflight,
        "plan_after": result_plan,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="精确清理 YKE/KEW 首批 131 条导入常用箱的报料备注"
    )
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--actor", default="admin")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm")
    parser.add_argument("--report", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    database = args.database.resolve()
    if not database.is_file():
        raise SystemExit(f"数据库不存在：{database}")
    if args.apply:
        if args.confirm != CONFIRMATION:
            raise SystemExit(f"--apply 必须同时提供 --confirm {CONFIRMATION}")
        result = apply_clear(
            database=database,
            backup_dir=(args.backup_dir or database.parent / "backups").resolve(),
            actor_username=args.actor,
        )
    else:
        result = {
            "changed": False,
            "apply": False,
            "database": str(database),
            "checks": database_checks(database),
            "plan": inspect_plan(database),
        }
    if args.report:
        report = args.report.resolve()
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(
            json.dumps(result, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
