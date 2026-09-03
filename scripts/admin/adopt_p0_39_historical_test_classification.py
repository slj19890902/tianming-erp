"""One-time, hash-guarded finance-only classification for P0-39's 43 legacy rows.

It deliberately writes *only* supplier settlement price facts.  It never
updates incoming receipts, supplier-order snapshots, inventory, or common-box
data, so the owner-authorized test classification cannot become a common-box
historical reference.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sys
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.admin import adopt_supplier_receipt_price_facts as base

from app.models.finance import FinanceIdempotencyRecord
from app.models.incoming_receipt import IncomingReceipt, IncomingReceiptItem
from app.models.material import Material
from app.models.supplier_requisition_order import (
    SupplierRequisitionOrder,
    SupplierRequisitionOrderItem,
)
from app.models.supplier_settlement import SupplierReceiptSettlementPriceFact
from app.models.user import User
from app.services.audit_log import append_audit_event
from app.services.material_purchase_contract import normalize_purchase_price_unit
from app.services.purchase_receipt_facts import (
    calculate_purchase_sheet_cost_breakdown,
    canonical_purchase_receipt_hash,
)
from app.services.supplier_master import resolve_supplier
from app.version import APP_VERSION


ACTION = "P0_39_HISTORICAL_TEST_CLASSIFICATION"
RESOURCE_TYPE = "supplier_receipt_price_fact"
CONFIRM = "APPLY_P0_39_HISTORICAL_TEST_CLASSIFICATION"
MONTH = "2026-08"
TARGET_IDS = (
    21, 22, 23, 24, 36, 37, 38, 39, 40, 41, 42, 43, 44, 45, 49, 50, 51,
    52, 53, 54, 55, 56, 59, 60, 61, 62, 63, 64, 65, 66, 67, 68, 69, 70,
    71, 72, 73, 74, 75, 76, 77, 78, 80,
)
OVERRIDE = {
    "material_code": "J616J",
    "supplier_name": "嘉林亿",
    "report_length_mm": "100.000",
    "report_width_mm": "100.000",
    "received_quantity": "100.000000",
    "unit_price": "3.000000",
    "currency": "CNY",
    "tax_included": True,
    "tax_rate": "0.130000",
    "shipping_fee_mode": "included",
}


class ClassificationError(RuntimeError):
    pass


def _sha(path: Path) -> str:
    return base.sha256_file(path)


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _operator(db: Session, username: str) -> User:
    row = db.scalar(select(User).where(User.username == username))
    if row is None or not row.is_active or row.role not in {"admin", "boss"}:
        raise ClassificationError("正式测试归类只允许现有 admin 或 boss 操作员执行")
    return row


def _material_and_supplier(db: Session) -> tuple[Material, Any, str]:
    material = db.scalar(select(Material).where(func.upper(Material.code) == "J616J"))
    if material is None or not material.is_active:
        raise ClassificationError("找不到启用的嘉林亿 J616J 材质")
    supplier = resolve_supplier(db, str(material.supplier_name or ""), require_active=True)
    price_unit = normalize_purchase_price_unit(material.price_unit)
    expected = (
        Decimal(str(material.quote_price or 0)) == Decimal("3")
        and price_unit == "per_square_meter"
        and str(material.purchase_currency or "").upper() == "CNY"
        and bool(material.purchase_tax_included)
        and Decimal(str(material.purchase_tax_rate or 0)) == Decimal("0.13")
    )
    if not expected:
        raise ClassificationError("J616J 当前主数据不再是嘉林亿、3元/㎡、人民币、含13%税的合同")
    return material, supplier, price_unit


def _rows(db: Session) -> list[dict[str, Any]]:
    material, supplier, price_unit = _material_and_supplier(db)
    items = list(db.scalars(select(IncomingReceiptItem).where(IncomingReceiptItem.id.in_(TARGET_IDS))).all())
    if {int(row.id) for row in items} != set(TARGET_IDS):
        raise ClassificationError("43 条指定历史实收的 ID 集合已变化")
    existing = set(db.scalars(select(SupplierReceiptSettlementPriceFact.incoming_receipt_item_id).where(SupplierReceiptSettlementPriceFact.incoming_receipt_item_id.in_(TARGET_IDS))).all())
    if existing:
        raise ClassificationError(f"指定历史实收已存在结算价格事实：{sorted(existing)}")
    breakdown = calculate_purchase_sheet_cost_breakdown(
        unit_price=Decimal("3"), price_unit=price_unit, tax_included=True, tax_rate=Decimal("0.13"),
        report_length_mm=Decimal("100"), report_width_mm=Decimal("100"),
    )
    result: list[dict[str, Any]] = []
    for item in sorted(items, key=lambda value: value.id):
        if item.status != "posted" or item.supplier_order_item_id is None:
            raise ClassificationError(f"收料明细 #{item.id} 不再是有效供应商采购实收")
        receipt = db.get(IncomingReceipt, item.receipt_id)
        source = db.get(SupplierRequisitionOrderItem, item.supplier_order_item_id)
        order = db.get(SupplierRequisitionOrder, source.supplier_order_id) if source else None
        if receipt is None or receipt.status != "posted" or source is None or order is None:
            raise ClassificationError(f"收料明细 #{item.id} 的原始收料或采购来源不存在")
        payload = {
            "incoming_receipt_item_id": int(item.id), "receipt_number": receipt.receipt_number,
            "receipt_date": receipt.received_at.date().isoformat(), "source_kind": "supplier_order_item",
            "purchase_document_number": order.order_number, "supplier_order_item_id": int(source.id),
            "original_received_quantity": int(item.received_quantity), "override": OVERRIDE,
            "material_id": int(material.id), "material_version": int(material.version), "supplier_id": int(supplier.id),
        }
        result.append({
            **payload, "source_hash": _hash(payload), "supplier_name_snapshot": str(supplier.display_name or supplier.standard_name),
            "material_code_snapshot": material.code, "price_unit": price_unit,
            "erp_amount": str((breakdown.gross_per_sheet * Decimal("100")).quantize(Decimal("0.01"))),
            "tax_amount": str((breakdown.tax_per_sheet * Decimal("100")).quantize(Decimal("0.01"))),
        })
    return result


def build_plan(database: Path) -> dict[str, Any]:
    checks = base.database_checks(database)
    heads = base.code_alembic_heads()
    if len(heads) != 1:
        raise ClassificationError(f"代码存在多个 Alembic head：{heads}")
    base._assert_healthy_database(checks, expected_head=heads[0])
    engine = base._read_only_engine(database)
    try:
        with Session(engine) as db:
            rows = _rows(db)
    finally:
        engine.dispose()
    if _sha(database) != checks["sha256"]:
        raise ClassificationError("dry-run 期间正式数据库哈希变化")
    return {
        "schema": "tianming.p0_39.historical_test_classification.v1", "mode": "dry-run", "writes_performed": False,
        "settlement_month": MONTH, "app_version": APP_VERSION, "alembic_head": heads[0], "database": checks,
        "override": OVERRIDE, "rows": rows, "row_count": len(rows), "received_quantity_snapshot_total": "4300.000000",
        "erp_amount": str(sum(Decimal(row["erp_amount"]) for row in rows)), "tax_amount": str(sum(Decimal(row["tax_amount"]) for row in rows)),
        "plan_hash": _hash([{ "id": row["incoming_receipt_item_id"], "source_hash": row["source_hash"] } for row in rows]),
        "adoption_reason": base.HISTORICAL_ADOPTION_REASON,
        "scope": "finance_only; original receipt, requisition, inventory and common-box history unchanged",
    }


def write_plan(plan: dict[str, Any], output_dir: Path) -> tuple[Path, str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"p0_39_test_classification_{datetime.now():%Y%m%d_%H%M%S_%f}.plan.json"
    path.write_text(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path, _sha(path)


def apply_plan(*, database: Path, plan_path: Path, plan_sha256: str, expected_database_sha256: str, backup_path: Path, backup_sha256: str, operator_username: str, batch_key: str, confirmation: str, service_stopped: bool, allow_formal_database: bool) -> dict[str, Any]:
    if confirmation != CONFIRM or not service_stopped or not allow_formal_database:
        raise ClassificationError("apply 必须包含确认短语、停服确认和正式数据库显式许可")
    if _sha(plan_path) != plan_sha256.lower():
        raise ClassificationError("计划 SHA-256 不匹配")
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    if plan.get("schema") != "tianming.p0_39.historical_test_classification.v1" or plan.get("writes_performed") is not False:
        raise ClassificationError("只接受本脚本生成的零写计划")
    if APP_VERSION != plan.get("app_version") or base.code_alembic_heads() != [plan.get("alembic_head")]:
        raise ClassificationError("代码版本或 Alembic head 已变化")
    if _sha(database) != expected_database_sha256 or plan["database"]["sha256"] != expected_database_sha256:
        raise ClassificationError("正式数据库哈希已变化，必须重新 dry-run")
    backup_checks = base.database_checks(backup_path)
    base._assert_healthy_database(backup_checks, expected_head=plan["alembic_head"])
    if backup_checks["sha256"] != backup_sha256.lower():
        raise ClassificationError("采用前备份 SHA-256 不匹配")
    payload = {"action": ACTION, "batch_key": batch_key, "plan_sha256": plan_sha256, "plan_hash": plan["plan_hash"], "database_sha256": expected_database_sha256, "backup_sha256": backup_sha256, "operator": operator_username}
    request_hash = _hash(payload)
    engine = base._apply_engine(database)
    try:
        with Session(engine) as db:
            with db.begin():
                operator = _operator(db, operator_username)
                replay = db.scalar(select(FinanceIdempotencyRecord).where(FinanceIdempotencyRecord.idempotency_key == batch_key))
                if replay is not None:
                    if replay.action != ACTION or replay.request_hash != request_hash:
                        raise ClassificationError("幂等键已用于不同历史测试归类")
                    return json.loads(replay.response_json)
                if _sha(database) != expected_database_sha256:
                    raise ClassificationError("取得写锁后数据库哈希已变化")
                fresh = build_plan(database)
                if fresh["plan_hash"] != plan["plan_hash"]:
                    raise ClassificationError("逐行来源或 J616J 合同已变化，计划失效")
                created: list[SupplierReceiptSettlementPriceFact] = []
                evidence = f"P0-39A 2026-09-03 owner-authorized finance-only test classification; backup_sha256={backup_sha256}; batch={batch_key}"
                for row in plan["rows"]:
                    fact = SupplierReceiptSettlementPriceFact(
                        incoming_receipt_item_id=int(row["incoming_receipt_item_id"]), supplier_id=int(row["supplier_id"]), supplier_name_snapshot=row["supplier_name_snapshot"],
                        material_id=int(row["material_id"]), material_code_snapshot=row["material_code_snapshot"], source_material_version=int(row["material_version"]),
                        source_kind="supplier_order_item", purchase_document_number_snapshot=row["purchase_document_number"], receipt_number_snapshot=row["receipt_number"],
                        receipt_date_snapshot=date.fromisoformat(row["receipt_date"]), received_quantity_snapshot=Decimal("100"), quantity_unit="张", report_length_mm=Decimal("100"), report_width_mm=Decimal("100"),
                        unit_price=Decimal("3"), price_unit=row["price_unit"], currency="CNY", tax_included=True, tax_rate=Decimal("0.13"), shipping_fee_mode="included",
                        fact_origin="historical_master_adoption", match_strategy="owner_authorized_finance_test_classification", finance_only_test_classification=True, source_hash=row["source_hash"],
                        adoption_reason=base.HISTORICAL_ADOPTION_REASON, adoption_evidence_reference=evidence, created_by=int(operator.id),
                    )
                    db.add(fact); db.flush(); created.append(fact)
                response = {"status": "applied", "writes_performed": True, "created_count": len(created), "fact_ids": [int(row.id) for row in created], "erp_amount": plan["erp_amount"], "tax_amount": plan["tax_amount"], "plan_hash": plan["plan_hash"], "scope": plan["scope"]}
                db.add(FinanceIdempotencyRecord(idempotency_key=batch_key, request_hash=request_hash, action=ACTION, actor_user_id=int(operator.id), resource_type=RESOURCE_TYPE, resource_id=int(created[0].id), response_json=json.dumps(response, ensure_ascii=False, sort_keys=True)))
                append_audit_event(db, event_category="business", result="success", source="script", module_code="finance", action_code=ACTION, resource=RESOURCE_TYPE, actor=operator, entity_type=RESOURCE_TYPE, entity_id=int(created[0].id), object_ref=batch_key, batch_id=batch_key, description="老板授权：43条历史实收仅财务测试归类，常用箱与原收料不变", details={"row_count": len(created), "erp_amount": plan["erp_amount"], "tax_amount": plan["tax_amount"], "plan_sha256": plan_sha256, "backup_sha256": backup_sha256, "target_ids": list(TARGET_IDS)})
            return response
    finally:
        engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description="P0-39 43条历史实收仅财务测试归类")
    parser.add_argument("--database", type=Path, required=True); parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--apply", action="store_true"); parser.add_argument("--plan-json", type=Path); parser.add_argument("--expected-plan-sha256")
    parser.add_argument("--expected-database-sha256"); parser.add_argument("--backup-path", type=Path); parser.add_argument("--expected-backup-sha256")
    parser.add_argument("--operator-username"); parser.add_argument("--batch-idempotency-key"); parser.add_argument("--confirm-apply"); parser.add_argument("--confirm-service-stopped", action="store_true"); parser.add_argument("--allow-formal-database", action="store_true")
    args = parser.parse_args()
    try:
        if not args.apply:
            plan = build_plan(args.database.resolve()); path, sha = write_plan(plan, args.output_dir.resolve()); print(json.dumps({"status":"dry-run","writes_performed":False,"plan_json":str(path),"plan_json_sha256":sha,"row_count":plan["row_count"],"erp_amount":plan["erp_amount"],"tax_amount":plan["tax_amount"]}, ensure_ascii=False)); return 0
        required = (args.plan_json, args.expected_plan_sha256, args.expected_database_sha256, args.backup_path, args.expected_backup_sha256, args.operator_username, args.batch_idempotency_key, args.confirm_apply)
        if any(value is None or value == "" for value in required): raise ClassificationError("apply 缺少哈希、备份、操作员或确认参数")
        result = apply_plan(database=args.database.resolve(), plan_path=args.plan_json.resolve(), plan_sha256=args.expected_plan_sha256, expected_database_sha256=args.expected_database_sha256.lower(), backup_path=args.backup_path.resolve(), backup_sha256=args.expected_backup_sha256.lower(), operator_username=args.operator_username, batch_key=args.batch_idempotency_key, confirmation=args.confirm_apply, service_stopped=args.confirm_service_stopped, allow_formal_database=args.allow_formal_database)
        print(json.dumps(result, ensure_ascii=False)); return 0
    except Exception as error:
        print(json.dumps({"status":"blocked","writes_performed":False,"error":str(error)}, ensure_ascii=False)); return 2


if __name__ == "__main__":
    raise SystemExit(main())
