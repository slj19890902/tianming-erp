"""Scoped, auditable adoption of current reference costs for unpriced physical stock."""
import json
from decimal import Decimal
from sqlalchemy import select, update
from app.models.warehouse_inventory import InventoryLot
from app.services.inventory_cost_snapshot import InventoryCostEstimate, apply_cost_snapshot
from app.services.inventory_valuation import ALGORITHM, positive, resolve_lot_cost, frozen_cost
from app.services.material_cost_supplement import canonical, fingerprint
from app.services.audit_log import append_audit_event

COST_FIELDS = ("estimated_unit_cost_snapshot", "estimated_square_price_snapshot",
    "estimated_cost_area_m2_snapshot", "cost_snapshot_source", "cost_snapshot_detail_json", "cost_snapshot_at")


def preview(db):
    rows, missing, priced = [], [], 0
    query = select(InventoryLot).where(InventoryLot.quantity_available + InventoryLot.quantity_reserved + InventoryLot.quantity_damaged > 0)
    for lot in db.scalars(query.order_by(InventoryLot.id)):
        unit, evidence = frozen_cost(lot, db)
        unit_correction = lot.cost_snapshot_source == "material_quote_area" and bool(evidence.get("validation_issue"))
        if positive(lot.estimated_unit_cost_snapshot) and not unit_correction:
            priced += 1
            continue
        result = resolve_lot_cost(db, lot)
        product = lot.finished_detail
        identity = dict(lot_id=lot.id, version=lot.version, code=product.inventory_code_snapshot if product else None,
            correction_kind="legacy_estimate_unit_error" if unit_correction else "missing_cost",
            quantity=lot.quantity_available + lot.quantity_reserved + lot.quantity_damaged)
        if result.estimate is None:
            missing.append({**identity, "reason": result.missing})
            continue
        estimate = result.estimate
        rows.append({**identity, "before": {field: str(getattr(lot, field)) if getattr(lot, field) is not None else None for field in COST_FIELDS},
            "unit_cost": str(estimate.unit_cost), "square_price": str(estimate.square_price),
            "area_m2": str(estimate.area_m2), "source": estimate.source, "evidence": estimate.detail})
    value = dict(algorithm=ALGORITHM, already_priced=priced, proposals=rows, missing=missing)
    return {**value, "fingerprint": fingerprint(value)}


def adopt(db, *, user, expected, batch_id):
    if not user or not user.is_active or user.role != "admin":
        raise PermissionError("成本补定仅限有效管理员")
    plan = preview(db)
    if expected != plan["fingerprint"]:
        raise ValueError("库存或报价已变化，请重新预览；未补价")
    for p in plan["proposals"]:
        lot = db.get(InventoryLot, p["lot_id"])
        cost_match = (InventoryLot.estimated_unit_cost_snapshot.is_(None)) if p["before"]["estimated_unit_cost_snapshot"] is None else (
            InventoryLot.estimated_unit_cost_snapshot == Decimal(p["before"]["estimated_unit_cost_snapshot"]))
        changed = db.execute(update(InventoryLot).where(InventoryLot.id==lot.id, InventoryLot.version==p["version"],
            cost_match).values(version=InventoryLot.version+1),
            execution_options={"synchronize_session":False})
        if changed.rowcount != 1:
            raise ValueError("批次已变化，整批取消补价")
        db.refresh(lot)
        evidence={**p["evidence"], "adoption_batch":batch_id, "original_cost":p["before"],
            "historical_reference_adoption":True, "correction_kind":p["correction_kind"],
            "scope":"当前参考成本补定，不认定当年采购价，不新增应付"}
        apply_cost_snapshot(lot,InventoryCostEstimate(Decimal(p["unit_cost"]),Decimal(p["square_price"]),
            Decimal(p["area_m2"]),p["source"],evidence))
        append_audit_event(db,actor=user,event_category="business",result="success",source="script",
            module_code="finance.cost",action_code="inventory_entry_cost_adopt",resource="inventory_cost_adoption",
            entity_type="inventory_lot",entity_id=lot.id,batch_id=batch_id,
            description="按老板确认补定在库批次材料成本；数量及位置不变",details=p)
    db.flush()
    return dict(adopted=len(plan["proposals"]), missing=len(plan["missing"]), fingerprint=expected, batch_id=batch_id)
