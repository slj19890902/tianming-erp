"""Explicit recount placement; quantities remain in the original inventory ledger."""
from __future__ import annotations

from hashlib import sha256
import json

from sqlalchemy import select, update
from sqlalchemy.orm import Session, selectinload

from app.core.time_contract import utc_now_naive
from app.models.audit import OperationLog
from app.models.warehouse_inventory import (
    InventoryLocationMovement, InventoryLot, InventoryMovement, InventoryPallet,
    WarehouseLocation,
)
from app.services.audit_log import append_audit_event
from app.services.warehouse_floor_claim import claim_warehouse_floor_projection

PENDING_CODE = "RECOUNT-PENDING"
PENDING_SOURCE = "RECOUNT_PENDING"
RESET_ACTION = "warehouse.recount.pending.reset"


class PendingRelocationError(ValueError):
    def __init__(self, message: str, status_code: int = 409):
        super().__init__(message)
        self.status_code = status_code


def is_pending_relocation_location(location: WarehouseLocation | None) -> bool:
    return bool(location is not None and location.location_code == PENDING_CODE
                and location.source_version == PENDING_SOURCE and location.is_active
                and location.is_temporary and location.warehouse_floor is None
                and location.placement_status == "unplaced"
                and location.storage_type is None and location.warehouse_type == "shared"
                and location.address_kind == "legacy"
                and all(getattr(location, name) is None for name in (
                    "area_code", "address_area_id", "map_rack_id", "rack_code",
                    "level_no", "ground_row_no", "slot_no")))


def claim_pending_relocation_source(db: Session, location_id: int) -> bool:
    row = db.get(WarehouseLocation, location_id, populate_existing=True)
    if not is_pending_relocation_location(row):
        return False
    return db.execute(update(WarehouseLocation).where(
        WarehouseLocation.id == location_id, WarehouseLocation.location_code == PENDING_CODE,
        WarehouseLocation.source_version == PENDING_SOURCE, WarehouseLocation.is_active.is_(True),
        WarehouseLocation.is_temporary.is_(True), WarehouseLocation.warehouse_floor.is_(None),
        WarehouseLocation.placement_status == "unplaced",
    ).values(address_version=WarehouseLocation.address_version,
             updated_at=WarehouseLocation.updated_at)).rowcount == 1


def _source_rows(db: Session):
    location_ids = select(WarehouseLocation.id).where(WarehouseLocation.warehouse_floor.in_((1, 3, 4)))
    lots = list(db.scalars(select(InventoryLot).where(
        InventoryLot.warehouse_location_id.in_(location_ids),
        (InventoryLot.quantity_available + InventoryLot.quantity_reserved + InventoryLot.quantity_damaged) > 0,
    ).order_by(InventoryLot.id).execution_options(populate_existing=True)))
    pallets = list(db.scalars(select(InventoryPallet).where(
        InventoryPallet.location_id.in_(location_ids), InventoryPallet.is_current.is_(True),
    ).options(selectinload(InventoryPallet.items)).order_by(InventoryPallet.id)
        .execution_options(populate_existing=True)))
    return lots, pallets


def _facts(lots, pallets):
    return {
        "floors": [1, 3, 4],
        "lots": [[l.id, l.warehouse_location_id, l.version, l.status,
                  l.quantity_available, l.quantity_reserved, l.quantity_damaged,
                  l.quantity_consumed, l.quantity_scrapped] for l in lots],
        "pallets": [[p.id, p.location_id, p.version, p.status, p.location_occupancy_key,
                     [[i.id, i.inventory_lot_id, str(i.quantity)] for i in p.items]] for p in pallets],
    }


def _fingerprint(facts):
    return sha256(json.dumps(facts, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def preview_pending_relocation(db: Session) -> dict:
    lots, pallets = _source_rows(db)
    return {"floors": [1, 3, 4], "lot_count": len(lots), "pallet_count": len(pallets),
            "fingerprint": _fingerprint(_facts(lots, pallets))}


def reset_to_pending_relocation(db: Session, *, actor, expected_fingerprint: str,
                                operation_key: str, request=None) -> dict:
    """One administrator-confirmed transaction; caller commits only with its audit."""
    from app.services.warehouse_ground_slots import release_ground_occupancy_for_pallet

    if actor.role != "admin":
        raise PendingRelocationError("全仓转待归位只能由管理员确认", 403)
    if not operation_key.strip() or len(operation_key) > 100:
        raise PendingRelocationError("请求标识无效", 400)
    for floor in (1, 3, 4):
        claim_warehouse_floor_projection(db, floor_number=floor)
    repeated = db.scalar(select(OperationLog).where(
        OperationLog.action_code == RESET_ACTION, OperationLog.batch_id == operation_key,
        OperationLog.result == "success",
    ).order_by(OperationLog.id.desc()))
    if repeated is not None:
        details = json.loads(repeated.details or "{}")
        if repeated.actor_user_id_snapshot != actor.id or details.get("fingerprint") != expected_fingerprint:
            raise PendingRelocationError("该请求标识已用于不同的操作或操作员")
        return {**details["result"], "replayed": True}
    lots, pallets = _source_rows(db)
    facts = _facts(lots, pallets)
    if _fingerprint(facts) != expected_fingerprint:
        raise PendingRelocationError("库存或位置已经变化，请重新核对转待归位范围")
    lot_map = {l.id: l for l in lots}
    outside_ids = {i.inventory_lot_id for p in pallets for i in p.items
                   if i.inventory_lot_id is not None and i.inventory_lot_id not in lot_map}
    if outside_ids and db.scalar(select(InventoryLot.id).where(
        InventoryLot.id.in_(outside_ids),
        (InventoryLot.quantity_available + InventoryLot.quantity_reserved + InventoryLot.quantity_damaged) > 0,
    ).limit(1)) is not None:
        raise PendingRelocationError("栈板关联了本次范围外的有量库存，请先核对，尚未移动任何货物")
    for lot in lots:
        if lot.status not in {"active", "frozen"}:
            raise PendingRelocationError("存在状态异常的有量批次，请先核对，尚未移动任何货物")
    for pallet in pallets:
        if pallet.status != "active" or any(i.inventory_lot_id is None for i in pallet.items):
            raise PendingRelocationError("存在未接入库存账的栈板，请先核对，尚未移动任何货物")
        for item in pallet.items:
            lot = lot_map.get(item.inventory_lot_id)
            if lot is not None and lot.warehouse_location_id != pallet.location_id:
                raise PendingRelocationError("栈板与有量库存位置不一致，请先核对")
    pending = db.scalar(select(WarehouseLocation).where(WarehouseLocation.location_code == PENDING_CODE))
    if pending is not None and not is_pending_relocation_location(pending):
        raise PendingRelocationError("待归位位置身份冲突，不能覆盖已有位置")
    if pending is None:
        pending = WarehouseLocation(location_code=PENDING_CODE, location_name="盘点待归位",
            warehouse_type="shared", warehouse_floor=None, storage_type=None,
            source_version=PENDING_SOURCE, placement_status="unplaced", is_active=True,
            is_temporary=True, remarks="重新盘点前的位置待确认，不代表已实物搬入某区域")
        db.add(pending)
        db.flush()
    now = utc_now_naive()
    for lot in lots:
        old_location_id = lot.warehouse_location_id
        lot.warehouse_location_id = pending.id
        lot.version += 1
        lot.last_movement_at = now
        key = sha256(f"{operation_key}:lot:{lot.id}".encode()).hexdigest()
        balances = {f"{side}_{name}": getattr(lot, f"quantity_{name}")
                    for side in ("before", "after")
                    for name in ("available", "reserved", "consumed", "damaged", "scrapped")}
        db.add(InventoryMovement(movement_number="RP-" + key[:32], inventory_lot_id=lot.id,
            movement_type="location_transfer", quantity=lot.quantity_available + lot.quantity_reserved + lot.quantity_damaged,
            unit=lot.unit, idempotency_key="recount:" + key, operator_id=actor.id,
            reason="重新盘点，原位置待核实", remarks=json.dumps({"from_location_id": old_location_id,
            "to_location_id": pending.id, "recount_operation_key": operation_key}), **balances))
    for pallet in pallets:
        old_location_id, old_version = pallet.location_id, pallet.version
        pallet.location_id = pending.id
        pallet.location_occupancy_key = f"RECOUNT:{pallet.id}"
        pallet.needs_relocation = True
        pallet.version += 1
        db.add(InventoryLocationMovement(pallet_id=pallet.id, from_location_id=old_location_id,
            to_location_id=pending.id, movement_type="move", operator_id=actor.id,
            idempotency_key="recount:" + sha256(f"{operation_key}:pallet:{pallet.id}".encode()).hexdigest(),
            confirmed_at=now, pallet_version_before=old_version, pallet_version_after=pallet.version,
            remarks="重新盘点转待归位；保留原货位身份及历史"))
        release_ground_occupancy_for_pallet(db, pallet_id=pallet.id, operator_id=actor.id)
    result = {"pending_location_id": pending.id, "lot_count": len(lots),
              "pallet_count": len(pallets), "replayed": False}
    append_audit_event(db, actor=actor, request=request, event_category="business", result="success",
        source="web", module_code="warehouse", action_code=RESET_ACTION, resource="WarehouseLocation",
        entity_id=pending.id, batch_id=operation_key, description="一楼、三楼、四楼库存转盘点待归位",
        details={"fingerprint": expected_fingerprint, "before": facts, "result": result})
    return result
