from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from contextlib import closing
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session, sessionmaker


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.api.master_data_common import audit_master_change  # noqa: E402
from app.core.database import backup_to_nas, create_sqlite_engine  # noqa: E402
from app.models.material import Material  # noqa: E402
from app.models.material_price_history import (  # noqa: E402
    MaterialPriceAdjustmentBatch,
    MaterialPriceHistory,
)
from app.models.master_data_object_version import (  # noqa: E402
    MasterDataObjectVersion,
)
from app.models.user import User  # noqa: E402
from app.services.master_data_versioning import apply_versioned_update  # noqa: E402
from app.services.material_purchase_contract import (  # noqa: E402
    CONFIRMED_PURCHASE_CURRENCY,
    CONFIRMED_PURCHASE_TAX_INCLUDED,
    CONFIRMED_PURCHASE_TAX_RATE,
    normalize_purchase_price_unit,
)


BATCH_CODE = "MATERIAL_PURCHASE_CONTRACT_BACKFILL_20260824"
CONFIRMATION = "APPLY_MATERIAL_PURCHASE_CONTRACT_BACKFILL_20260824"
SOURCE = "scripts.admin.backfill_material_purchase_contracts"
REASON = (
    "老板已确认全部材质现行报价均为人民币含13%税；补齐历史主档缺失的币种、"
    "含税标记和税率，不修改报价、计价单位或供应商"
)
PROTECTED_COUNT_TABLES = (
    "materials",
    "products",
    "sales_orders",
    "sales_order_items",
    "supplier_requisition_orders",
    "supplier_requisition_order_items",
    "incoming_receipts",
    "incoming_receipt_items",
    "inventory_lots",
    "inventory_pallets",
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def database_checks(path: Path) -> dict[str, Any]:
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=30)) as connection:
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA query_only = ON")
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
            for table in PROTECTED_COUNT_TABLES
            if table in table_names
        }
        for table in (
            "master_data_object_versions",
            "operation_logs",
            "material_price_history",
            "material_price_adjustment_batches",
        ):
            counts[table] = int(
                connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            )
    return {
        "integrity_check": integrity,
        "foreign_key_violations": len(foreign_keys),
        "counts": counts,
    }


def _decimal_text(value: object) -> str | None:
    if value is None:
        return None
    return format(Decimal(value).normalize(), "f")


def build_plan(db: Session) -> dict[str, Any]:
    materials = list(db.scalars(select(Material).order_by(Material.id)).all())
    targets: list[dict[str, Any]] = []
    blockers: list[dict[str, Any]] = []
    ready_count = 0
    unpriced_count = 0
    for material in materials:
        price = Decimal(material.quote_price) if material.quote_price is not None else None
        if price is None or price <= 0:
            unpriced_count += 1
            continue

        row_blockers: list[str] = []
        if normalize_purchase_price_unit(material.price_unit) is None:
            row_blockers.append("采购计价单位不受实收合同支持")
        current_currency = str(material.purchase_currency or "").strip().upper()
        if current_currency and current_currency != CONFIRMED_PURCHASE_CURRENCY:
            row_blockers.append("已有采购币种不是 CNY")
        if (
            material.purchase_tax_included is not None
            and bool(material.purchase_tax_included)
            is not CONFIRMED_PURCHASE_TAX_INCLUDED
        ):
            row_blockers.append("已有报价被标记为未税")
        if (
            material.purchase_tax_rate is not None
            and Decimal(material.purchase_tax_rate) != CONFIRMED_PURCHASE_TAX_RATE
        ):
            row_blockers.append("已有采购税率不是 13%")
        if row_blockers:
            blockers.append(
                {
                    "id": int(material.id),
                    "code": material.code,
                    "version": int(material.version),
                    "reasons": row_blockers,
                }
            )
            continue

        missing_fields = []
        if not current_currency:
            missing_fields.append("purchase_currency")
        if material.purchase_tax_included is None:
            missing_fields.append("purchase_tax_included")
        if material.purchase_tax_rate is None:
            missing_fields.append("purchase_tax_rate")
        if not missing_fields:
            ready_count += 1
            continue
        has_v1 = db.scalar(
            select(MasterDataObjectVersion.id)
            .where(
                MasterDataObjectVersion.object_type == "material",
                MasterDataObjectVersion.object_id == material.id,
                MasterDataObjectVersion.version == 1,
            )
            .limit(1)
        )
        targets.append(
            {
                "id": int(material.id),
                "code": material.code,
                "version": int(material.version),
                "is_active": bool(material.is_active),
                "quote_price": _decimal_text(material.quote_price),
                "price_unit": material.price_unit,
                "purchase_currency": material.purchase_currency,
                "purchase_tax_included": material.purchase_tax_included,
                "purchase_tax_rate": _decimal_text(material.purchase_tax_rate),
                "missing_fields": missing_fields,
                "baseline_needed": int(material.version) == 1 and has_v1 is None,
            }
        )
    status = "blocked" if blockers else "ready" if targets else "already_applied"
    return {
        "status": status,
        "batch_code": BATCH_CODE,
        "confirmed_contract": {
            "currency": CONFIRMED_PURCHASE_CURRENCY,
            "tax_included": CONFIRMED_PURCHASE_TAX_INCLUDED,
            "tax_rate": _decimal_text(CONFIRMED_PURCHASE_TAX_RATE),
        },
        "material_count": len(materials),
        "priced_ready_count": ready_count,
        "unpriced_count": unpriced_count,
        "target_count": len(targets),
        "active_target_count": sum(row["is_active"] for row in targets),
        "baseline_needed_count": sum(row["baseline_needed"] for row in targets),
        "blocker_count": len(blockers),
        "blockers": blockers,
        "targets": targets,
    }


def inspect_plan(database: Path) -> dict[str, Any]:
    uri = f"file:{database.resolve().as_posix()}?mode=ro"

    def readonly_connection() -> sqlite3.Connection:
        return sqlite3.connect(
            uri,
            uri=True,
            timeout=30,
            check_same_thread=False,
        )

    engine = create_engine(
        "sqlite+pysqlite://",
        creator=readonly_connection,
        future=True,
    )

    @event.listens_for(engine, "connect")
    def configure_readonly(dbapi_connection: sqlite3.Connection, _record) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA busy_timeout = 30000")
            cursor.execute("PRAGMA foreign_keys = ON")
            cursor.execute("PRAGMA query_only = ON")
        finally:
            cursor.close()

    factory = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        with factory() as db:
            return build_plan(db)
    finally:
        engine.dispose()


def apply_backfill(
    *,
    database: Path,
    backup_dir: Path,
    actor_username: str,
    expected_sha256: str,
    report_path: Path | None = None,
) -> dict[str, Any]:
    source_sha256 = sha256_file(database)
    if source_sha256.lower() != expected_sha256.lower():
        raise RuntimeError("数据库 SHA-256 已变化，拒绝执行")
    before_checks = database_checks(database)
    if (
        before_checks["integrity_check"].lower() != "ok"
        or before_checks["foreign_key_violations"] != 0
    ):
        raise RuntimeError(f"写入前数据库检查失败：{before_checks}")
    preflight = inspect_plan(database)
    if preflight["status"] == "blocked":
        raise RuntimeError("存在与已确认采购口径冲突的材质，拒绝静默覆盖")
    if preflight["status"] == "already_applied":
        return {
            "changed": False,
            "message": "历史材质采购价格合同已经完整补齐",
            "database_sha256": source_sha256,
            "checks": before_checks,
            "plan": preflight,
        }

    backup = backup_to_nas(
        source_path=database,
        backup_dir=backup_dir,
        filename_suffix="_MATERIAL_PURCHASE_CONTRACT_BEFORE_BACKFILL",
        keep_regular=1_000_000,
    )
    backup_checks = database_checks(backup.path)
    backup_plan = inspect_plan(backup.path)
    if (
        backup.integrity_check.lower() != "ok"
        or backup.size <= 0
        or backup_checks != before_checks
        or backup_plan != preflight
        or sha256_file(database).lower() != source_sha256.lower()
    ):
        raise RuntimeError("补齐前备份未通过完整性、计数或计划一致性验证")

    engine = create_sqlite_engine(database, busy_timeout_ms=30_000)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        with factory() as db:
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
            actor = db.scalar(select(User).where(User.username == actor_username))
            if actor is None or not actor.is_active or actor.role not in {"admin", "boss"}:
                raise RuntimeError("执行人必须是启用的 admin 或 boss 账号")
            live_plan = build_plan(db)
            if live_plan != preflight or live_plan["status"] != "ready":
                raise RuntimeError("备份后材质主档已变化，拒绝继续写入")

            batch = MaterialPriceAdjustmentBatch(
                supplier_name=None,
                adjust_percent=Decimal("0"),
                effective_date=None,
                affected_count=live_plan["target_count"],
                remark=f"{BATCH_CODE}；{REASON}",
                operator=actor.username,
                backup_path=str(backup.path),
                report_path=str(report_path) if report_path else None,
            )
            db.add(batch)
            db.flush()

            for item in live_plan["targets"]:
                material = db.get(Material, item["id"])
                if material is None or int(material.version) != item["version"]:
                    raise RuntimeError(f"材质 {item['id']} 的版本已变化")
                updates = {
                    "purchase_currency": CONFIRMED_PURCHASE_CURRENCY,
                    "purchase_tax_included": CONFIRMED_PURCHASE_TAX_INCLUDED,
                    "purchase_tax_rate": CONFIRMED_PURCHASE_TAX_RATE,
                }
                before = {
                    key: getattr(material, key)
                    for key in updates
                }
                apply_versioned_update(
                    db,
                    object_type="material",
                    entity=material,
                    updates=updates,
                    expected_version=item["version"],
                    user=actor,
                    reason=REASON,
                    source=SOURCE,
                    action="system_consistency_fix",
                )
                db.add(
                    MaterialPriceHistory(
                        material_id=material.id,
                        supplier_name=material.supplier_name,
                        material_code=material.code,
                        old_price=material.quote_price,
                        new_price=material.quote_price,
                        adjust_percent=Decimal("0"),
                        effective_date=material.quote_date,
                        adjust_reason=(
                            f"{BATCH_CODE}；补齐字段："
                            + "、".join(item["missing_fields"])
                        ),
                        operator=actor.username,
                        batch_id=batch.id,
                    )
                )
                audit_master_change(
                    db,
                    user=actor,
                    action="UPDATE",
                    resource="Material",
                    resource_id=material.id,
                    details={
                        "before": before,
                        "after": updates,
                        "reason": REASON,
                        "source": SOURCE,
                        "batch_code": BATCH_CODE,
                    },
                )
            db.commit()
    finally:
        engine.dispose()

    after_checks = database_checks(database)
    target_count = int(preflight["target_count"])
    baseline_count = int(preflight["baseline_needed_count"])
    for table in PROTECTED_COUNT_TABLES:
        if before_checks["counts"].get(table) != after_checks["counts"].get(table):
            raise RuntimeError(f"保护表 {table} 的行数变化，请立即按备份核对")
    expected_deltas = {
        "master_data_object_versions": target_count + baseline_count,
        "operation_logs": target_count * 2,
        "material_price_history": target_count,
        "material_price_adjustment_batches": 1,
    }
    for table, expected_delta in expected_deltas.items():
        actual_delta = after_checks["counts"][table] - before_checks["counts"][table]
        if actual_delta != expected_delta:
            raise RuntimeError(
                f"{table} 增量 {actual_delta} 与预期 {expected_delta} 不一致"
            )
    if (
        after_checks["integrity_check"].lower() != "ok"
        or after_checks["foreign_key_violations"] != 0
    ):
        raise RuntimeError("补齐后数据库完整性检查失败，请使用备份恢复")
    result_plan = inspect_plan(database)
    if result_plan["status"] != "already_applied":
        raise RuntimeError("补齐后回读仍有遗漏或冲突材质")
    return {
        "changed": True,
        "batch_code": BATCH_CODE,
        "database_sha256_before": source_sha256,
        "database_sha256_after": sha256_file(database),
        "backup": {**asdict(backup), "path": str(backup.path), "checks": backup_checks},
        "checks_before": before_checks,
        "checks_after": after_checks,
        "plan_before": preflight,
        "plan_after": result_plan,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="按已确认人民币含13%税口径，审计补齐历史材质采购价格合同"
    )
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--actor", default="admin")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm")
    parser.add_argument("--confirm-service-stopped", action="store_true")
    parser.add_argument("--expected-sha256")
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
        if not args.confirm_service_stopped:
            raise SystemExit("正式写入前必须停止 ERP，并提供 --confirm-service-stopped")
        if not args.expected_sha256:
            raise SystemExit("正式写入必须提供 --expected-sha256")
        result = apply_backfill(
            database=database,
            backup_dir=(args.backup_dir or database.parent / "backups").resolve(),
            actor_username=args.actor,
            expected_sha256=args.expected_sha256,
            report_path=args.report.resolve() if args.report else None,
        )
    else:
        result = {
            "changed": False,
            "apply": False,
            "database": str(database),
            "database_sha256": sha256_file(database),
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
