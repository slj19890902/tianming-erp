from __future__ import annotations

import json
from hashlib import sha256
from uuid import uuid4

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, selectinload

from app.core.time_contract import beijing_now_naive, utc_naive_to_api, utc_now_naive
from app.models.audit import OperationLog
from app.models.stocktake import StocktakeItem, StocktakeOrder, StocktakeReview
from app.models.user import User
from app.models.warehouse_inventory import (
    Floor3LocationLayout,
    InventoryLot,
    InventoryMovement,
    WarehouseArea,
    WarehouseLocation,
)
from app.services.location_candidates import (
    claim_active_placed_location,
    load_warehouse_location_projection_contexts,
    list_operational_locations,
    operational_location_issue,
    warehouse_location_projection,
)
from app.services.product_specification import dimension_specification
from app.services.warehouse_location_address import (
    employee_area_name,
    employee_location_name,
    location_address_payload,
)


COUNTABLE_LOT_STATUSES = frozenset({"active", "frozen"})


def _location_identity_snapshot(
    db: Session,
    location: WarehouseLocation,
) -> tuple[int, str, str | None]:
    context = load_warehouse_location_projection_contexts(db, [location]).get(
        int(location.id),
        {},
    )
    projection = warehouse_location_projection(location, **context)
    return (
        int(location.address_version or 1),
        str(projection["position_status"]),
        str(projection.get("published_map_revision") or "").strip() or None,
    )


class StocktakeError(ValueError):
    def __init__(
        self,
        message: str,
        status_code: int = 400,
        code: str = "STOCKTAKE_INVALID",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code


def _order_number() -> str:
    return f"ST-{beijing_now_naive():%Y%m%d}-{uuid4().hex[:10].upper()}"


def _movement_number() -> str:
    return f"IM-{beijing_now_naive():%Y%m%d}-{uuid4().hex[:10].upper()}"


def _movement_idempotency_key(review_key: str, lot_id: int) -> str:
    digest = sha256(f"{review_key}:{lot_id}".encode("utf-8")).hexdigest()
    return f"stocktake:{digest}"


def _lot_options():
    return (
        selectinload(InventoryLot.location)
        .selectinload(WarehouseLocation.address_area)
        .selectinload(WarehouseArea.floor),
        selectinload(InventoryLot.finished_detail),
    )


def _order_options():
    return (
        selectinload(StocktakeOrder.location)
        .selectinload(WarehouseLocation.address_area)
        .selectinload(WarehouseArea.floor),
        selectinload(StocktakeOrder.submitter),
        selectinload(StocktakeOrder.reviewer),
        selectinload(StocktakeOrder.items),
        selectinload(StocktakeOrder.reviews).selectinload(StocktakeReview.reviewer),
    )


def _countable_lots_statement(location_id: int):
    return (
        select(InventoryLot)
        .where(
            InventoryLot.warehouse_location_id == location_id,
            InventoryLot.inventory_type == "finished",
            InventoryLot.status.in_(COUNTABLE_LOT_STATUSES),
        )
        .options(*_lot_options())
        .order_by(InventoryLot.id)
    )


def _get_countable_location(db: Session, location_id: int) -> WarehouseLocation:
    location = db.scalar(
        select(WarehouseLocation)
        .options(
            selectinload(WarehouseLocation.address_area).selectinload(
                WarehouseArea.floor
            )
        )
        .where(WarehouseLocation.id == location_id)
    )
    if location is None:
        raise StocktakeError(
            "可盘点库位不存在或已停用",
            404,
            "STOCKTAKE_LOCATION_NOT_FOUND",
        )
    issue = operational_location_issue(
        db,
        location,
        warehouse_types={"finished", "shared"},
    )
    if issue:
        error_code = (
            "STOCKTAKE_LOCATION_UNPLACED"
            if (location.placement_status or "placed") != "placed"
            else "STOCKTAKE_LOCATION_NOT_OPERATIONAL"
        )
        raise StocktakeError(
            f"{issue}，不能发起成品盘点",
            409,
            error_code,
        )
    return location


def _stocktake_address_payload(location: WarehouseLocation, candidate) -> dict:
    payload = location_address_payload(
        location,
        area=candidate.area,
        floor=candidate.floor,
        area_sequence=(
            candidate.projection_context.get("area_sequence")
            if candidate.projection_context
            else None
        ),
    )
    return {
        "address_zone_code": (
            candidate.area.address_zone_code if candidate.area is not None else None
        ),
        "address_subzone_no": (
            candidate.area.address_subzone_no if candidate.area is not None else None
        ),
        "current_address_code": payload["current_address_code"],
        "current_address_name": payload["current_address_name"],
        "employee_location_name": payload["employee_location_name"],
        "map_rack_id": payload["map_rack_id"],
        "rack_display_name": payload["rack_display_name"],
        "level_no": payload["level_no"],
        "slot_no": payload["slot_no"],
        "address_kind": payload["address_kind"],
        "address_version": payload["address_version"],
    }


def list_locations(db: Session) -> list[dict[str, object]]:
    candidates = list_operational_locations(
        db,
        warehouse_types={"finished", "shared"},
    )
    candidate_by_id = {row.location.id: row for row in candidates}
    if not candidate_by_id:
        return []
    active_count = func.sum(case((InventoryLot.status == "active", 1), else_=0))
    frozen_count = func.sum(case((InventoryLot.status == "frozen", 1), else_=0))
    on_hand = func.sum(
        case(
            (
                InventoryLot.status.in_(COUNTABLE_LOT_STATUSES),
                InventoryLot.quantity_available + InventoryLot.quantity_reserved,
            ),
            else_=0,
        )
    )
    rows = db.execute(
        select(
            WarehouseLocation,
            func.coalesce(active_count, 0),
            func.coalesce(frozen_count, 0),
            func.coalesce(on_hand, 0),
        )
        .outerjoin(
            InventoryLot,
            and_(
                InventoryLot.warehouse_location_id == WarehouseLocation.id,
                InventoryLot.inventory_type == "finished",
            ),
        )
        .where(
            WarehouseLocation.id.in_(candidate_by_id),
        )
        .group_by(WarehouseLocation.id)
        .order_by(
            WarehouseLocation.sort_order,
            WarehouseLocation.location_code,
            WarehouseLocation.id,
        )
    ).all()
    layout_versions = {
        int(location_id): int(version)
        for location_id, version in db.execute(
            select(
                Floor3LocationLayout.location_id,
                Floor3LocationLayout.version,
            ).where(Floor3LocationLayout.location_id.in_(candidate_by_id))
        ).all()
    }
    result: list[dict[str, object]] = []
    for location, active_lot_count, frozen_lot_count, current_on_hand in rows:
        candidate = candidate_by_id[location.id]
        address_payload = _stocktake_address_payload(location, candidate)
        projection = warehouse_location_projection(
            location,
            **dict(candidate.projection_context or {}),
        )
        result.append({
            "id": location.id,
            "location_code": location.location_code,
            "location_name": address_payload["employee_location_name"],
            "location_master_name": location.location_name,
            "warehouse_type": location.warehouse_type,
            "warehouse_floor": location.warehouse_floor,
            "floor_id": (
                candidate.floor.id
                if candidate.floor
                else None
            ),
            "floor_code": (
                candidate.floor.floor_code
                if candidate.floor
                else None
            ),
            "floor_name": (
                candidate.floor.floor_name
                if candidate.floor
                else None
            ),
            "area_id": (
                candidate.area.id
                if candidate.area
                else None
            ),
            "area_code": location.area_code,
            "area_name": employee_area_name(
                candidate.area,
                area_code=location.area_code,
                floor_number=(
                    candidate.floor.floor_number
                    if candidate.floor is not None
                    else location.warehouse_floor
                ),
            ),
            "area_master_name": (
                candidate.area.area_name
                if candidate.area is not None
                else None
            ),
            **address_payload,
            "placement_status": location.placement_status or "placed",
            "position_status": projection["position_status"],
            "layout_version": layout_versions.get(int(location.id)),
            "published_map_revision": projection.get("published_map_revision"),
            "is_temporary": location.is_temporary,
            "active_lot_count": int(active_lot_count),
            "frozen_lot_count": int(frozen_lot_count),
            "lot_count": int(active_lot_count) + int(frozen_lot_count),
            "current_on_hand": int(current_on_hand),
        })
    return result


def _lot_identity(lot: InventoryLot) -> dict[str, object]:
    customer_id: int | None = None
    customer_name: str | None = None
    product_id: int | None = None
    product_name: str | None = None
    inventory_code: str | None = None
    material_code: str | None = None
    specification: str | None = None

    if lot.finished_detail is not None:
        detail = lot.finished_detail
        customer_id = detail.owner_customer_id
        customer_name = detail.owner_customer_name_snapshot
        product_id = detail.product_id
        product_name = detail.product_name_snapshot
        inventory_code = detail.inventory_code_snapshot
        material_code = detail.material_code_snapshot
        specification = dimension_specification(
            detail.length_mm,
            detail.width_mm,
            detail.height_mm,
        )

    return {
        "customer_id": customer_id,
        "customer_name": customer_name,
        "product_id": product_id,
        "product_name": product_name,
        "inventory_code": inventory_code,
        "material_code": material_code,
        "specification": specification,
    }


def lot_payload(lot: InventoryLot) -> dict[str, object]:
    available = int(lot.quantity_available)
    reserved = int(lot.quantity_reserved)
    return {
        "id": lot.id,
        "inventory_lot_id": lot.id,
        "lot_number": lot.lot_number,
        "batch_number": lot.lot_number,
        "inventory_type": lot.inventory_type,
        "status": lot.status,
        "quantity_available": available,
        "quantity_reserved": reserved,
        "on_hand": available + reserved,
        "unit": lot.unit,
        "version": lot.version,
        "expected_version": lot.version,
        "expected_available": available,
        "expected_reserved": reserved,
        **_lot_identity(lot),
    }


def get_location_detail(db: Session, location_id: int) -> dict[str, object]:
    location = _get_countable_location(db, location_id)
    projection_context = load_warehouse_location_projection_contexts(
        db, [location]
    ).get(int(location.id), {})
    current_address = location_address_payload(
        location,
        area=projection_context.get("area"),
        floor=projection_context.get("floor"),
        area_sequence=projection_context.get("area_sequence"),
    )
    layout_version = db.scalar(
        select(Floor3LocationLayout.version).where(
            Floor3LocationLayout.location_id == location_id
        )
    )
    projection = warehouse_location_projection(location, **projection_context)
    lots = db.scalars(_countable_lots_statement(location_id)).all()
    lot_rows = [lot_payload(lot) for lot in lots]
    pending_order = db.scalar(
        select(StocktakeOrder)
        .where(
            StocktakeOrder.location_id == location_id,
            StocktakeOrder.status == "submitted",
        )
        .options(*_order_options())
        .order_by(StocktakeOrder.submitted_at.desc(), StocktakeOrder.id.desc())
    )
    return {
        "id": location.id,
        "location_code": location.location_code,
        "location_name": current_address["employee_location_name"],
        "location_master_name": location.location_name,
        **current_address,
        "warehouse_type": location.warehouse_type,
        "area_code": location.area_code,
        "position_status": projection["position_status"],
        "layout_version": (
            int(layout_version) if layout_version is not None else None
        ),
        "published_map_revision": projection.get("published_map_revision"),
        "is_temporary": location.is_temporary,
        "active_lot_count": sum(lot.status == "active" for lot in lots),
        "frozen_lot_count": sum(lot.status == "frozen" for lot in lots),
        "lot_count": len(lots),
        "current_on_hand": sum(int(row["on_hand"]) for row in lot_rows),
        "lots": lot_rows,
        "pending_stocktake": (
            order_payload(
                pending_order,
                projection_context=projection_context,
            )
            if pending_order is not None
            else None
        ),
    }


def _get_order_by_key(db: Session, idempotency_key: str) -> StocktakeOrder | None:
    return db.scalar(
        select(StocktakeOrder)
        .where(StocktakeOrder.idempotency_key == idempotency_key)
        .options(*_order_options())
    )


def _submitted_counts(items: list[dict[str, object]]) -> dict[int, int]:
    return {
        int(item["inventory_lot_id"]): int(item["counted_quantity"])
        for item in items
    }


def _submitted_snapshots(
    items: list[dict[str, object]],
) -> dict[int, tuple[int, int, int, int]]:
    return {
        int(item["inventory_lot_id"]): (
            int(item["counted_quantity"]),
            int(item["expected_version"]),
            int(item["expected_available"]),
            int(item["expected_reserved"]),
        )
        for item in items
    }


def resolve_submission_replay(
    db: Session,
    *,
    location_id: int,
    location_layout_version: int | None = None,
    location_address_version: int,
    location_position_status: str,
    published_map_revision: str | None,
    items: list[dict[str, object]],
    idempotency_key: str,
) -> StocktakeOrder | None:
    existing = _get_order_by_key(db, idempotency_key)
    if existing is None:
        return None
    existing_snapshots = {
        item.inventory_lot_id: (
            int(item.counted_quantity),
            int(item.lot_version_snapshot),
            int(item.available_quantity_snapshot),
            int(item.reserved_quantity_snapshot),
        )
        for item in existing.items
    }
    if (
        existing.location_id != location_id
        or existing.location_layout_version != location_layout_version
        or existing.location_address_version != location_address_version
        or existing.location_position_status != location_position_status
        or existing.published_map_revision != published_map_revision
        or existing_snapshots != _submitted_snapshots(items)
    ):
        raise StocktakeError(
            "幂等键已用于不同的盘点提交",
            409,
            "IDEMPOTENCY_CONFLICT",
        )
    return existing


def create_stocktake(
    db: Session,
    *,
    location_id: int,
    items: list[dict[str, object]],
    idempotency_key: str,
    submitter: User,
    location_layout_version: int | None = None,
    location_address_version: int,
    location_position_status: str,
    published_map_revision: str | None,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> StocktakeOrder:
    key = idempotency_key.strip()
    if not key:
        raise StocktakeError("幂等键不能为空", code="STOCKTAKE_INVALID")
    normalized_position_status = str(location_position_status or "").strip()
    normalized_map_revision = (
        str(published_map_revision or "").strip() or None
    )
    if location_address_version < 1 or not normalized_position_status:
        raise StocktakeError(
            "盘点库位身份令牌无效，请重新打开该库位",
            409,
            "STOCKTAKE_LOCATION_CHANGED",
        )
    replay = resolve_submission_replay(
        db,
        location_id=location_id,
        location_layout_version=location_layout_version,
        location_address_version=location_address_version,
        location_position_status=normalized_position_status,
        published_map_revision=normalized_map_revision,
        items=items,
        idempotency_key=key,
    )
    if replay is not None:
        return replay

    location = _get_countable_location(db, location_id)
    try:
        claimed = claim_active_placed_location(
            db,
            location_id,
            expected_layout_version=location_layout_version,
        )
    except OperationalError as error:
        raise StocktakeError(
            "盘点库位正在被其他库存或布局操作使用，请稍后刷新后重新盘点",
            409,
            "STOCKTAKE_LOCATION_BUSY",
        ) from error
    current_layout_version = db.scalar(
        select(Floor3LocationLayout.version).where(
            Floor3LocationLayout.location_id == location_id
        )
    )
    normalized_current_layout_version = (
        int(current_layout_version) if current_layout_version is not None else None
    )
    if not claimed or normalized_current_layout_version != location_layout_version:
        raise StocktakeError(
            "盘点库位布局或状态已变化，请重新打开该库位后发起盘点",
            409,
            "STOCKTAKE_LOCATION_CHANGED",
        )
    # A concurrent request with the same key may have committed while this
    # request waited for the floor/location claim.  Recheck before treating
    # the existing submitted order as a different operation.
    replay = resolve_submission_replay(
        db,
        location_id=location_id,
        location_layout_version=location_layout_version,
        location_address_version=location_address_version,
        location_position_status=normalized_position_status,
        published_map_revision=normalized_map_revision,
        items=items,
        idempotency_key=key,
    )
    if replay is not None:
        return replay
    location = db.get(WarehouseLocation, int(location_id), populate_existing=True)
    if location is None:
        raise StocktakeError(
            "盘点库位已变化，请重新打开该库位",
            409,
            "STOCKTAKE_LOCATION_CHANGED",
        )
    current_address_version, current_position_status, current_map_revision = (
        _location_identity_snapshot(db, location)
    )
    if (
        current_address_version != location_address_version
        or current_position_status != normalized_position_status
        or current_map_revision != normalized_map_revision
    ):
        raise StocktakeError(
            "盘点库位地址或正式地图已变化，请重新打开该库位后发起盘点",
            409,
            "STOCKTAKE_LOCATION_CHANGED",
        )
    pending_order = db.scalar(
        select(StocktakeOrder)
        .where(
            StocktakeOrder.location_id == location_id,
            StocktakeOrder.status == "submitted",
        )
        .order_by(StocktakeOrder.submitted_at.desc(), StocktakeOrder.id.desc())
    )
    if pending_order is not None:
        raise StocktakeError(
            f"该库位已有盘点单 {pending_order.order_number} 等待审核，不能重复提交",
            409,
            "STOCKTAKE_PENDING_REVIEW",
        )
    input_ids = [int(item["inventory_lot_id"]) for item in items]
    if len(input_ids) != len(set(input_ids)):
        raise StocktakeError(
            "每个库存批次必须且只能提交一行",
            409,
            "STOCKTAKE_DRIFT",
        )
    client_line_ids = [
        str(item["client_line_id"])
        for item in items
        if item.get("client_line_id") is not None
    ]
    if len(client_line_ids) != len(set(client_line_ids)):
        raise StocktakeError(
            "client_line_id 不能重复",
            409,
            "STOCKTAKE_INVALID_ITEMS",
        )

    lots = db.scalars(
        _countable_lots_statement(location_id).with_for_update()
    ).all()
    expected_ids = {lot.id for lot in lots}
    submitted_ids = set(input_ids)
    if submitted_ids != expected_ids or len(items) != len(lots):
        missing = sorted(expected_ids - submitted_ids)
        extra = sorted(submitted_ids - expected_ids)
        raise StocktakeError(
            f"盘点明细必须完整覆盖库位全部批次；缺少 {missing or '无'}，额外 {extra or '无'}",
            409,
            "STOCKTAKE_DRIFT",
        )

    submitted_rows = {int(item["inventory_lot_id"]): item for item in items}
    drifted = [
        lot.id
        for lot in lots
        if (
            int(submitted_rows[lot.id]["expected_version"]) != lot.version
            or int(submitted_rows[lot.id]["expected_available"])
            != int(lot.quantity_available)
            or int(submitted_rows[lot.id]["expected_reserved"])
            != int(lot.quantity_reserved)
        )
    ]
    if drifted:
        raise StocktakeError(
            f"库存已发生变化，请刷新后重新盘点；涉及批次 {drifted}",
            409,
            "STOCKTAKE_DRIFT",
        )

    counts = _submitted_counts(items)
    order = StocktakeOrder(
        order_number=_order_number(),
        location_id=location.id,
        location_layout_version=location_layout_version,
        location_address_version=current_address_version,
        location_position_status=current_position_status,
        published_map_revision=current_map_revision,
        status="draft",
        version=1,
        submitted_by=submitter.id,
        submitted_at=utc_now_naive(),
        idempotency_key=key,
    )
    db.add(order)
    db.flush()
    for lot in lots:
        identity = _lot_identity(lot)
        available = int(lot.quantity_available)
        reserved = int(lot.quantity_reserved)
        on_hand = available + reserved
        counted = counts[lot.id]
        order.items.append(
            StocktakeItem(
                inventory_lot_id=lot.id,
                lot_version_snapshot=lot.version,
                available_quantity_snapshot=available,
                reserved_quantity_snapshot=reserved,
                on_hand_quantity_snapshot=on_hand,
                counted_quantity=counted,
                difference_quantity=counted - on_hand,
                lot_number_snapshot=lot.lot_number,
                customer_name_snapshot=identity["customer_name"],
                product_name_snapshot=identity["product_name"],
                specification_snapshot=identity["specification"],
                inventory_code_snapshot=identity["inventory_code"],
                location_code_snapshot=location.location_code,
                unit_snapshot=lot.unit,
            )
        )
    db.flush()
    order.version += 1
    order.status = "submitted"
    db.flush()
    db.add(
        _operation_log(
            user=submitter,
            order=order,
            action="submit",
            details={
                "order_id": order.id,
                "order_number": order.order_number,
                "location_id": location.id,
                "location_code": location.location_code,
                "location_layout_version": location_layout_version,
                "location_address_version": current_address_version,
                "location_position_status": current_position_status,
                "published_map_revision": current_map_revision,
                "idempotency_key": key,
                "items": [
                    {
                        "inventory_lot_id": item.inventory_lot_id,
                        "lot_version_snapshot": item.lot_version_snapshot,
                        "quantity_available_snapshot": item.available_quantity_snapshot,
                        "quantity_reserved_snapshot": item.reserved_quantity_snapshot,
                        "quantity_on_hand_snapshot": item.on_hand_quantity_snapshot,
                        "counted_quantity": item.counted_quantity,
                        "difference_quantity": item.difference_quantity,
                    }
                    for item in order.items
                ],
            },
            ip_address=ip_address,
            user_agent=user_agent,
        )
    )
    db.flush()
    return order


def get_order(db: Session, order_id: int, *, lock: bool = False) -> StocktakeOrder:
    statement = (
        select(StocktakeOrder)
        .where(StocktakeOrder.id == order_id)
        .options(*_order_options())
    )
    if lock:
        statement = statement.with_for_update()
    order = db.scalar(statement)
    if order is None:
        raise StocktakeError("盘点单不存在", 404, "STOCKTAKE_NOT_FOUND")
    return order


def list_orders(db: Session, *, status: str | None = None) -> list[StocktakeOrder]:
    statement = select(StocktakeOrder).options(*_order_options())
    if status is not None:
        statement = statement.where(StocktakeOrder.status == status)
    else:
        statement = statement.where(StocktakeOrder.status != "draft")
    return db.scalars(
        statement.order_by(StocktakeOrder.submitted_at.desc(), StocktakeOrder.id.desc())
    ).all()


def _review_replay(
    db: Session,
    *,
    order_id: int,
    action: str,
    idempotency_key: str,
    reason: str | None,
) -> StocktakeOrder | None:
    review = db.scalar(
        select(StocktakeReview).where(
            StocktakeReview.idempotency_key == idempotency_key
        )
    )
    if review is None:
        return None
    if (
        review.order_id != order_id
        or review.action != action
        or review.reason != reason
    ):
        raise StocktakeError(
            "幂等键已用于不同的盘点审核",
            409,
            "IDEMPOTENCY_CONFLICT",
        )
    return get_order(db, order_id)


def resolve_review_replay(
    db: Session,
    *,
    order_id: int,
    action: str,
    idempotency_key: str,
    reason: str | None,
) -> StocktakeOrder | None:
    return _review_replay(
        db,
        order_id=order_id,
        action=action,
        idempotency_key=idempotency_key.strip(),
        reason=reason,
    )


def _operation_log(
    *,
    user: User,
    order: StocktakeOrder,
    action: str,
    details: dict[str, object],
    ip_address: str | None,
    user_agent: str | None,
) -> OperationLog:
    return OperationLog(
        user_id=user.id,
        action=f"STOCKTAKE_{action.upper()}",
        resource="StocktakeOrder",
        details=json.dumps(details, ensure_ascii=False, sort_keys=True),
        ip_address=ip_address,
        username=user.username,
        role=user.role,
        entity_type="stocktake_order",
        entity_id=order.id,
        description={
            "submit": "提交库存盘点单",
            "approve": "库存盘点审核通过",
            "reject": "库存盘点驳回",
        }[action],
        user_agent=user_agent,
    )


def approve_stocktake(
    db: Session,
    *,
    order_id: int,
    idempotency_key: str,
    reason: str | None,
    reviewer: User,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> StocktakeOrder:
    key = idempotency_key.strip()
    replay = _review_replay(
        db,
        order_id=order_id,
        action="approve",
        idempotency_key=key,
        reason=reason,
    )
    if replay is not None:
        return replay

    order_location_snapshot = db.execute(
        select(
            StocktakeOrder.location_id,
            StocktakeOrder.location_layout_version,
            StocktakeOrder.location_address_version,
            StocktakeOrder.location_position_status,
            StocktakeOrder.published_map_revision,
        ).where(StocktakeOrder.id == order_id)
    ).one_or_none()
    if order_location_snapshot is None:
        raise StocktakeError(
            "盘点单或盘点库位不存在",
            404,
            "STOCKTAKE_LOCATION_NOT_FOUND",
        )
    (
        location_id,
        location_layout_version,
        location_address_version,
        location_position_status,
        published_map_revision,
    ) = order_location_snapshot
    try:
        claimed = claim_active_placed_location(
            db,
            int(location_id),
            expected_layout_version=(
                int(location_layout_version)
                if location_layout_version is not None
                else None
            ),
        )
    except OperationalError as error:
        raise StocktakeError(
            "盘点库位正在被其他库存或布局操作使用，请稍后重试",
            409,
            "STOCKTAKE_LOCATION_BUSY",
        ) from error
    if not claimed:
        raise StocktakeError(
            "盘点提交后库位已停用、尚未落位或状态已变化，请重新盘点",
            409,
            "STOCKTAKE_LOCATION_CHANGED",
        )
    location = db.get(WarehouseLocation, int(location_id), populate_existing=True)
    if (
        location is None
        or location_address_version is None
        or not str(location_position_status or "").strip()
    ):
        raise StocktakeError(
            "盘点单缺少当前货位身份快照，请驳回后重新盘点",
            409,
            "STOCKTAKE_LOCATION_CHANGED",
        )
    current_identity = _location_identity_snapshot(db, location)
    expected_identity = (
        int(location_address_version),
        str(location_position_status).strip(),
        str(published_map_revision or "").strip() or None,
    )
    if current_identity != expected_identity:
        raise StocktakeError(
            "盘点提交后货位地址或正式地图已变化，请驳回后重新盘点",
            409,
            "STOCKTAKE_LOCATION_CHANGED",
        )

    order = get_order(db, order_id, lock=True)
    if order.status != "submitted":
        raise StocktakeError(
            "盘点单已审核，不能重复处理",
            409,
            "ALREADY_REVIEWED",
        )

    items_by_lot = {item.inventory_lot_id: item for item in order.items}
    lots = db.scalars(
        _countable_lots_statement(order.location_id).with_for_update()
    ).all()
    if {lot.id for lot in lots} != set(items_by_lot):
        raise StocktakeError(
            "库位库存批次已变化，请重新盘点",
            409,
            "STOCKTAKE_DRIFT",
        )

    drifted: list[int] = []
    below_reserved: list[int] = []
    for lot in lots:
        item = items_by_lot[lot.id]
        if (
            lot.version != item.lot_version_snapshot
            or int(lot.quantity_available) != item.available_quantity_snapshot
            or int(lot.quantity_reserved) != item.reserved_quantity_snapshot
        ):
            drifted.append(lot.id)
        if item.counted_quantity < int(lot.quantity_reserved):
            below_reserved.append(lot.id)
    if drifted:
        raise StocktakeError(
            f"库存已发生变化，涉及批次 {drifted}",
            409,
            "STOCKTAKE_DRIFT",
        )
    if below_reserved:
        raise StocktakeError(
            f"盘点数不能小于已预占数，涉及批次 {below_reserved}",
            409,
            "BELOW_RESERVED",
        )

    reviewed_at = utc_now_naive()
    adjustments: list[dict[str, object]] = []
    movement_bindings: list[tuple[StocktakeItem, int]] = []
    for lot in lots:
        item = items_by_lot[lot.id]
        before_available = int(lot.quantity_available)
        reserved = int(lot.quantity_reserved)
        new_available = int(item.counted_quantity) - reserved
        delta = new_available - before_available
        movement: InventoryMovement | None = None
        if delta:
            before = {
                "available": before_available,
                "reserved": reserved,
                "consumed": int(lot.quantity_consumed),
                "damaged": int(lot.quantity_damaged),
                "scrapped": int(lot.quantity_scrapped),
            }
            lot.quantity_available = new_available
            lot.version += 1
            lot.last_movement_at = reviewed_at
            movement = InventoryMovement(
                movement_number=_movement_number(),
                inventory_lot_id=lot.id,
                movement_type="adjust",
                quantity=abs(delta),
                unit=lot.unit,
                before_available=before["available"],
                after_available=new_available,
                before_reserved=before["reserved"],
                after_reserved=before["reserved"],
                before_consumed=before["consumed"],
                after_consumed=before["consumed"],
                before_damaged=before["damaged"],
                after_damaged=before["damaged"],
                before_scrapped=before["scrapped"],
                after_scrapped=before["scrapped"],
                reason="库存盘点审核",
                remarks=f"盘点单 {order.order_number}",
                operator_id=reviewer.id,
                idempotency_key=_movement_idempotency_key(key, lot.id),
            )
            db.add(movement)
            db.flush()
            movement_bindings.append((item, movement.id))
        adjustments.append(
            {
                "inventory_lot_id": lot.id,
                "before_available": before_available,
                "reserved": reserved,
                "counted_quantity": int(item.counted_quantity),
                "after_available": new_available,
                "delta": delta,
                "movement_id": movement.id if movement is not None else None,
            }
        )

    order.status = "approved"
    order.version += 1
    order.reviewed_by = reviewer.id
    order.reviewed_at = reviewed_at
    order.review_note = reason
    # The data-layer guard deliberately requires the parent to be approved
    # before an immutable item may receive its one adjustment movement link.
    db.flush()
    for item, movement_id in movement_bindings:
        item.adjustment_movement_id = movement_id
    db.flush()

    review = StocktakeReview(
        order_id=order.id,
        sequence=len(order.reviews) + 1,
        action="approve",
        from_status="submitted",
        to_status="approved",
        reason=reason,
        idempotency_key=key,
        details_json={"adjustments": adjustments},
        reviewed_by=reviewer.id,
        reviewed_at=reviewed_at,
    )
    db.add(review)
    db.add(
        _operation_log(
            user=reviewer,
            order=order,
            action="approve",
            details={
                "order_id": order.id,
                "order_number": order.order_number,
                "idempotency_key": key,
                "reason": reason,
                "adjustments": adjustments,
            },
            ip_address=ip_address,
            user_agent=user_agent,
        )
    )
    db.flush()
    return order


def reject_stocktake(
    db: Session,
    *,
    order_id: int,
    idempotency_key: str,
    reason: str | None,
    reviewer: User,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> StocktakeOrder:
    key = idempotency_key.strip()
    normalized_reason = (reason or "").strip() or "驳回库存盘点单（系统记录）"
    replay = _review_replay(
        db,
        order_id=order_id,
        action="reject",
        idempotency_key=key,
        reason=normalized_reason,
    )
    if replay is not None:
        return replay
    order = get_order(db, order_id, lock=True)
    if order.status != "submitted":
        raise StocktakeError(
            "盘点单已审核，不能重复处理",
            409,
            "ALREADY_REVIEWED",
        )

    reviewed_at = utc_now_naive()
    order.status = "rejected"
    order.version += 1
    order.reviewed_by = reviewer.id
    order.reviewed_at = reviewed_at
    order.review_note = normalized_reason
    details = {
        "order_id": order.id,
        "order_number": order.order_number,
        "idempotency_key": key,
        "reason": normalized_reason,
    }
    db.add(
        StocktakeReview(
            order_id=order.id,
            sequence=len(order.reviews) + 1,
            action="reject",
            from_status="submitted",
            to_status="rejected",
            reason=normalized_reason,
            idempotency_key=key,
            details_json=details,
            reviewed_by=reviewer.id,
            reviewed_at=reviewed_at,
        )
    )
    db.add(
        _operation_log(
            user=reviewer,
            order=order,
            action="reject",
            details=details,
            ip_address=ip_address,
            user_agent=user_agent,
        )
    )
    db.flush()
    return order


def order_payload(
    order: StocktakeOrder,
    *,
    projection_context: dict | None = None,
) -> dict[str, object]:
    context = projection_context or {}
    current_address = location_address_payload(
        order.location,
        area=context.get("area"),
        floor=context.get("floor"),
        area_sequence=context.get("area_sequence"),
    )
    items = [
        {
            "id": item.id,
            "inventory_lot_id": item.inventory_lot_id,
            "lot_number": item.lot_number_snapshot,
            "customer_name": item.customer_name_snapshot,
            "product_name": item.product_name_snapshot,
            "specification": item.specification_snapshot,
            "inventory_code": item.inventory_code_snapshot,
            "location_code": item.location_code_snapshot,
            "unit": item.unit_snapshot,
            "lot_version_snapshot": item.lot_version_snapshot,
            "quantity_available_snapshot": item.available_quantity_snapshot,
            "quantity_reserved_snapshot": item.reserved_quantity_snapshot,
            "quantity_on_hand_snapshot": item.on_hand_quantity_snapshot,
            "counted_quantity": item.counted_quantity,
            "difference_quantity": item.difference_quantity,
            "adjustment_movement_id": item.adjustment_movement_id,
        }
        for item in order.items
    ]
    return {
        "id": order.id,
        "order_number": order.order_number,
        "stocktake_number": order.order_number,
        "number": order.order_number,
        "location_id": order.location_id,
        "location_layout_version": order.location_layout_version,
        "location_address_version": order.location_address_version,
        "location_position_status": order.location_position_status,
        "published_map_revision": order.published_map_revision,
        "location_code": order.location.location_code,
        "location_name": current_address["employee_location_name"],
        "location_master_name": order.location.location_name,
        "employee_location_name": current_address["employee_location_name"],
        "current_address_code": current_address["current_address_code"],
        "current_address_name": current_address["current_address_name"],
        "status": order.status,
        "version": order.version,
        "submitted_by": order.submitted_by,
        "submitted_by_name": order.submitter.display_name or order.submitter.real_name,
        "submitted_at": utc_naive_to_api(order.submitted_at),
        "reviewed_by": order.reviewed_by,
        "reviewed_by_name": (
            order.reviewer.display_name or order.reviewer.real_name
            if order.reviewer is not None
            else None
        ),
        "reviewed_at": (
            utc_naive_to_api(order.reviewed_at) if order.reviewed_at is not None else None
        ),
        "review_note": order.review_note,
        "idempotency_key": order.idempotency_key,
        "lot_count": len(items),
        "snapshot_on_hand": sum(int(item["quantity_on_hand_snapshot"]) for item in items),
        "counted_quantity": sum(int(item["counted_quantity"]) for item in items),
        "difference_quantity": sum(int(item["difference_quantity"]) for item in items),
        "items": items,
        "reviews": [
            {
                "id": review.id,
                "sequence": review.sequence,
                "action": review.action,
                "from_status": review.from_status,
                "to_status": review.to_status,
                "reason": review.reason,
                "details": review.details_json,
                "reviewed_by": review.reviewed_by,
                "reviewed_by_name": (
                    review.reviewer.display_name or review.reviewer.real_name
                ),
                "reviewed_at": utc_naive_to_api(review.reviewed_at),
            }
            for review in order.reviews
        ],
    }
