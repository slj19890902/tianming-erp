from __future__ import annotations

import base64
from datetime import date, datetime, timedelta
from io import BytesIO
import json
import socket
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request
import qrcode
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.api.deps import (
    PermissionChecker,
    RoleChecker,
    customer_scope_ids,
    get_db,
    has_unrestricted_customer_access,
    require_customer_access,
)
from app.models.user import User
from app.models.audit import OperationLog
from app.models.customer import Customer
from app.models.mold_tool import MoldTool
from app.models.product import Product
from app.models.order import Order, OrderItem
from app.models.warehouse_inventory import (
    FinishedGoodsInventoryDetail,
    InventoryLot,
    InventoryMovement,
    InventoryReservation,
    OrderItemSemiRequirement,
    SemiFinishedInventoryDetail,
    WarehouseLocation,
)
from app.services.semi_finished_inventory import (
    SemiFinishedCandidate,
    SemiFinishedLotVersion,
    active_semi_requirement_credited_quantity,
    browse_semi_finished_inventory,
    browse_semi_finished_inventory_for_product,
    confirm_semi_finished_match,
    consume_semi_finished_reservation,
    release_semi_finished_reservation,
    replace_semi_finished_lot_product_assignments,
    reserve_semi_finished_inventory,
    reverse_semi_finished_consumption,
    save_order_item_semi_requirement,
    semi_finished_lot_assigned_product_ids,
    semi_finished_candidates_for_product,
    semi_finished_inventory_candidates,
)
from app.services.warehouse_inventory import (
    WarehouseInventoryError,
    active_finished_reserved_qty,
    finished_inventory_candidates,
    finished_inventory_candidates_for_product,
    inventory_age_warning,
    manual_finished_in,
    manual_semi_finished_in,
    mutate_lot,
    normalize_material_code,
    release_finished_reservation,
    reserve_finished_inventory,
)
from app.services.inventory_insights import build_inventory_insights
from app.services.mold_location import describe_mold_location


router = APIRouter()
# Configuration/master-data operations have no N028 permission equivalent and
# intentionally retain their legacy admin-only boundary.
admin_only = RoleChecker(["admin"])
can_read = PermissionChecker("warehouse.view")
can_operate = PermissionChecker("warehouse.execute")
can_reserve = PermissionChecker("warehouse.reserve")
can_view_reservations = PermissionChecker("warehouse.view")
VALID_SOURCE_TYPES = {
    "manual",
    "production_surplus",
    "purchase_surplus",
    "stocktake",
    "transfer",
    "replenishment",
}


class LocationPayload(BaseModel):
    location_code: str = Field(min_length=1, max_length=50)
    location_name: str = Field(min_length=1, max_length=100)
    warehouse_type: str
    remarks: str | None = None

    @field_validator("location_code", "location_name")
    @classmethod
    def strip_required(cls, value: str) -> str:
        return value.strip()

    @field_validator("warehouse_type")
    @classmethod
    def valid_type(cls, value: str) -> str:
        if value not in {"finished", "semi_finished", "shared"}:
            raise ValueError("库位类型必须是成品、半成品或共用")
        return value


class MoldToolPayload(BaseModel):
    mold_code: str = Field(min_length=1, max_length=100)
    mold_name: str = Field(min_length=1, max_length=200)
    rack_location: str = Field(min_length=1, max_length=250)
    remarks: str | None = None

    @field_validator("mold_code", "mold_name", "rack_location")
    @classmethod
    def strip_mold_fields(cls, value: str) -> str:
        return value.strip()


class FinishedManualInPayload(BaseModel):
    customer_id: int
    product_id: int
    location_id: int
    quantity: int = Field(gt=0)
    stock_date: date
    source_type: str = "manual"
    remarks: str | None = None
    idempotency_key: str | None = Field(default=None, max_length=100)

    @field_validator("source_type")
    @classmethod
    def valid_source_type(cls, value: str) -> str:
        if value not in VALID_SOURCE_TYPES:
            raise ValueError("库存来源无效")
        return value


class SemiFinishedManualInPayload(BaseModel):
    location_id: int
    quantity: int = Field(gt=0)
    stock_date: date
    source_type: str = "manual"
    material_code: str = Field(min_length=1, max_length=100)
    layer_count: int
    flute_type: str
    board_length_mm: int = Field(gt=0)
    board_width_mm: int = Field(gt=0)
    sheet_type: str
    component_type: str = "whole"
    pieces_per_box: int = Field(default=1, gt=0)
    stock_yield_per_sheet: int = Field(default=1, gt=0)
    supplier_name: str | None = None
    customer_id: int | None = None
    crease_type: str | None = None
    crease_left_mm: int | None = Field(default=None, ge=0)
    crease_middle_mm: int | None = Field(default=None, ge=0)
    crease_right_mm: int | None = Field(default=None, ge=0)
    cutting_note: str | None = None
    remarks: str | None = None
    idempotency_key: str | None = Field(default=None, max_length=100)

    @field_validator("source_type")
    @classmethod
    def valid_source_type(cls, value: str) -> str:
        if value not in VALID_SOURCE_TYPES:
            raise ValueError("库存来源无效")
        return value


class VersionPayload(BaseModel):
    expected_version: int = Field(gt=0)
    reason: str | None = None
    idempotency_key: str | None = Field(default=None, max_length=100)


class QuantityOperationPayload(VersionPayload):
    quantity: int = Field(gt=0)

    @field_validator("reason")
    @classmethod
    def reason_required(cls, value: str | None) -> str:
        if not value or not value.strip():
            raise ValueError("必须填写原因")
        return value.strip()


class AdjustPayload(VersionPayload):
    quantity_delta: int

    @field_validator("quantity_delta")
    @classmethod
    def nonzero(cls, value: int) -> int:
        if value == 0:
            raise ValueError("调整数量不能为0")
        return value

    @field_validator("reason")
    @classmethod
    def reason_required(cls, value: str | None) -> str:
        if not value or not value.strip():
            raise ValueError("必须填写调整原因")
        return value.strip()


class FinishedReservationPayload(BaseModel):
    order_item_id: int
    inventory_lot_id: int
    quantity: int = Field(gt=0)
    expected_version: int = Field(gt=0)
    idempotency_key: str = Field(min_length=8, max_length=100)
    warning_acknowledged_codes: list[str] = Field(default_factory=list)


class ReleaseReservationPayload(BaseModel):
    release_reason: str = Field(min_length=1, max_length=500)
    idempotency_key: str = Field(min_length=8, max_length=100)

    @field_validator("release_reason")
    @classmethod
    def strip_reason(cls, value: str) -> str:
        reason = value.strip()
        if not reason:
            raise ValueError("取消抵扣必须填写原因")
        return reason


class SemiRequirementPayload(BaseModel):
    component_type: str
    board_length_mm: int = Field(gt=0)
    board_width_mm: int = Field(gt=0)
    material_code: str = Field(min_length=1, max_length=100)
    material_id: int | None = None
    flute_type: str = Field(min_length=1, max_length=20)
    pieces_per_box: int = Field(gt=0)
    stock_yield_per_sheet: int = Field(gt=0)
    required_piece_quantity: int | None = Field(default=None, gt=0)


class SemiProductCandidatePayload(BaseModel):
    customer_id: int
    board_length_mm: int = Field(gt=0)
    board_width_mm: int = Field(gt=0)
    material_code: str = Field(min_length=1, max_length=100)
    flute_type: str = Field(min_length=1, max_length=20)
    component_type: str
    pieces_per_box: int = Field(gt=0)
    stock_yield_per_sheet: int = Field(gt=0)


class SemiMatchConfirmPayload(BaseModel):
    inventory_lot_id: int
    override: bool = False
    warning_acknowledged_codes: list[str] = Field(default_factory=list)


class SemiLotVersionPayload(BaseModel):
    lot_id: int
    expected_version: int = Field(gt=0)


class SemiReservationPayload(BaseModel):
    requested_requirement_quantity: int = Field(gt=0)
    lots: list[SemiLotVersionPayload] = Field(min_length=1)
    confirmed: bool
    override: bool = False
    warning_acknowledged_codes: list[str] = Field(default_factory=list)
    idempotency_key: str = Field(min_length=1, max_length=80)


class SemiReleasePayload(BaseModel):
    expected_version: int = Field(gt=0)
    stock_quantity: int | None = Field(default=None, gt=0)
    release_reason: str = Field(min_length=1, max_length=500)
    idempotency_key: str = Field(min_length=1, max_length=100)


class SemiProductAssignmentsPayload(BaseModel):
    product_ids: list[int] = Field(default_factory=list, max_length=500)

    @field_validator("product_ids")
    @classmethod
    def valid_product_ids(cls, values: list[int]) -> list[int]:
        if any(int(value) <= 0 for value in values):
            raise ValueError("成品款号ID必须是正整数")
        return list(dict.fromkeys(int(value) for value in values))


class SemiConsumePayload(BaseModel):
    expected_version: int = Field(gt=0)
    stock_quantity: int = Field(gt=0)
    delivery_item_id: int | None = None
    idempotency_key: str = Field(min_length=1, max_length=100)


class SemiReverseConsumePayload(BaseModel):
    expected_version: int = Field(gt=0)
    stock_quantity: int = Field(gt=0)
    allocation_id: int | None = None
    idempotency_key: str = Field(min_length=1, max_length=100)


def _handle(error: WarehouseInventoryError) -> None:
    raise HTTPException(status_code=error.status_code, detail=str(error)) from error


def _handle_integrity(error: IntegrityError) -> None:
    raise HTTPException(
        status_code=409,
        detail="库存或学习规则已被其他请求修改，请刷新后重试",
    ) from error


def _visible_customer_ids(user: User, db: Session) -> set[int] | None:
    if has_unrestricted_customer_access(user, db):
        return None
    return customer_scope_ids(user, db)


def _require_order_item_customer_access(
    db: Session,
    order_item_id: int,
    user: User,
) -> None:
    customer_id = db.scalar(
        select(Order.customer_id)
        .join(OrderItem, OrderItem.order_id == Order.id)
        .where(OrderItem.id == order_item_id)
    )
    if customer_id is not None:
        require_customer_access(customer_id, user, db)


def _require_requirement_customer_access(
    db: Session,
    requirement_id: int,
    user: User,
) -> None:
    requirement = db.get(OrderItemSemiRequirement, requirement_id)
    if requirement is not None:
        require_customer_access(requirement.customer_id, user, db)


def _require_reservation_customer_access(
    db: Session,
    reservation_id: int,
    user: User,
) -> None:
    reservation = db.get(InventoryReservation, reservation_id)
    if reservation is None:
        return
    customer_id = None
    if reservation.order_id is not None:
        customer_id = db.scalar(
            select(Order.customer_id).where(Order.id == reservation.order_id)
        )
    elif reservation.semi_requirement_id is not None:
        customer_id = db.scalar(
            select(OrderItemSemiRequirement.customer_id).where(
                OrderItemSemiRequirement.id == reservation.semi_requirement_id
            )
        )
    if customer_id is not None:
        require_customer_access(customer_id, user, db)


def _location_dict(row: WarehouseLocation) -> dict:
    return {
        "id": row.id,
        "location_code": row.location_code,
        "location_name": row.location_name,
        "warehouse_type": row.warehouse_type,
        "is_active": row.is_active,
        "remarks": row.remarks,
    }


def _lot_query():
    return select(InventoryLot).options(
        selectinload(InventoryLot.location),
        selectinload(InventoryLot.finished_detail),
        selectinload(InventoryLot.semi_finished_detail),
    )


def _visible_lot_condition(visible_customer_ids: set[int]):
    finished_lot_ids = select(
        FinishedGoodsInventoryDetail.inventory_lot_id
    ).where(
        or_(
            FinishedGoodsInventoryDetail.owner_customer_id.is_(None),
            FinishedGoodsInventoryDetail.owner_customer_id.in_(visible_customer_ids),
        )
    )
    semi_finished_lot_ids = select(
        SemiFinishedInventoryDetail.inventory_lot_id
    ).where(
        or_(
            SemiFinishedInventoryDetail.owner_customer_id.is_(None),
            SemiFinishedInventoryDetail.owner_customer_id.in_(visible_customer_ids),
        )
    )
    return or_(
        InventoryLot.id.in_(finished_lot_ids),
        InventoryLot.id.in_(semi_finished_lot_ids),
    )


def _require_lot_customer_access(
    db: Session,
    lot_id: int,
    user: User,
) -> InventoryLot | None:
    lot = db.scalar(_lot_query().where(InventoryLot.id == lot_id))
    if lot is None:
        return None
    customer_id = None
    if lot.finished_detail is not None:
        customer_id = lot.finished_detail.owner_customer_id
    elif lot.semi_finished_detail is not None:
        customer_id = lot.semi_finished_detail.owner_customer_id
    if customer_id is not None:
        require_customer_access(customer_id, user, db)
    return lot


def _lot_dict(row: InventoryLot) -> dict:
    warning = inventory_age_warning(row)
    detail: dict = {}
    if row.finished_detail:
        item = row.finished_detail
        detail = {
            "owner_customer_id": item.owner_customer_id,
            "owner_customer_name": item.owner_customer_name_snapshot,
            "is_general": item.is_general,
            "product_id": item.product_id,
            "inventory_code": item.inventory_code_snapshot,
            "product_name": item.product_name_snapshot,
            "box_type": item.box_type_snapshot,
            "length_mm": item.length_mm,
            "width_mm": item.width_mm,
            "height_mm": item.height_mm,
            "material_code": item.material_code_snapshot,
            "flute_type": item.flute_type_snapshot,
        }
    elif row.semi_finished_detail:
        item = row.semi_finished_detail
        detail = {
            "supplier_name": item.supplier_name,
            "owner_customer_id": item.owner_customer_id,
            "owner_customer_name": item.owner_customer_name_snapshot,
            "material_id": item.material_id,
            "material_code": item.material_code_snapshot,
            "normalized_material_code": item.normalized_material_code,
            "layer_count": item.layer_count,
            "flute_type": item.flute_type,
            "board_length_mm": item.board_length_mm,
            "board_width_mm": item.board_width_mm,
            "component_type": item.component_type,
            "pieces_per_box": item.pieces_per_box,
            "stock_yield_per_sheet": item.stock_yield_per_sheet,
            "sheet_type": item.sheet_type,
            "crease_type": item.crease_type,
            "crease_left_mm": item.crease_left_mm,
            "crease_middle_mm": item.crease_middle_mm,
            "crease_right_mm": item.crease_right_mm,
            "cutting_note": item.cutting_note,
        }
    return {
        "id": row.id,
        "lot_number": row.lot_number,
        "inventory_type": row.inventory_type,
        "location": _location_dict(row.location),
        "quantity_available": row.quantity_available,
        "quantity_reserved": row.quantity_reserved,
        "quantity_consumed": row.quantity_consumed,
        "quantity_damaged": row.quantity_damaged,
        "quantity_scrapped": row.quantity_scrapped,
        "unit": row.unit,
        "status": row.status,
        "source_type": row.source_type,
        "stock_date": row.stock_date,
        "last_movement_at": row.last_movement_at,
        "version": row.version,
        "remarks": row.remarks,
        "age_days": warning.days,
        "age_warning_level": warning.level,
        "age_warning_text": warning.text,
        "detail": detail,
    }


def _movement_dict(row: InventoryMovement) -> dict:
    return {
        "id": row.id,
        "movement_number": row.movement_number,
        "inventory_lot_id": row.inventory_lot_id,
        "lot_number": row.lot.lot_number if row.lot else None,
        "inventory_type": row.lot.inventory_type if row.lot else None,
        "movement_type": row.movement_type,
        "quantity": row.quantity,
        "unit": row.unit,
        "before_available": row.before_available,
        "after_available": row.after_available,
        "before_reserved": row.before_reserved,
        "after_reserved": row.after_reserved,
        "before_consumed": row.before_consumed,
        "after_consumed": row.after_consumed,
        "before_damaged": row.before_damaged,
        "after_damaged": row.after_damaged,
        "before_scrapped": row.before_scrapped,
        "after_scrapped": row.after_scrapped,
        "reason": row.reason,
        "operator_id": row.operator_id,
        "created_at": row.created_at,
    }


def _reservation_dict(
    row: InventoryReservation,
    db: Session,
) -> dict:
    item = db.get(OrderItem, row.order_item_id) if row.order_item_id else None
    order = db.get(Order, row.order_id) if row.order_id else None
    lot = db.get(InventoryLot, row.inventory_lot_id)
    customer = db.get(Customer, order.customer_id) if order else None
    warning_codes: list[str] = []
    if row.warning_codes:
        try:
            parsed = json.loads(row.warning_codes)
            warning_codes = parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            warning_codes = [row.warning_codes]
    return {
        "id": row.id,
        "reservation_number": row.reservation_number,
        "reservation_type": row.reservation_type,
        "inventory_lot_id": row.inventory_lot_id,
        "lot_number": lot.lot_number if lot else None,
        "order_id": row.order_id,
        "order_item_id": row.order_item_id,
        "order_number": order.order_number if order else None,
        "customer_name": customer.name if customer else None,
        "product_name": item.snapshot_product_name if item else None,
        "reserved_stock_quantity": row.reserved_stock_quantity,
        "credited_requirement_quantity": row.credited_requirement_quantity,
        "consumed_stock_quantity": row.consumed_stock_quantity,
        "released_stock_quantity": row.released_stock_quantity,
        "consumed_requirement_quantity": row.consumed_requirement_quantity,
        "released_requirement_quantity": row.released_requirement_quantity,
        "remaining_reserved_stock_quantity": (
            row.reserved_stock_quantity
            - row.consumed_stock_quantity
            - row.released_stock_quantity
        ),
        "semi_requirement_id": row.semi_requirement_id,
        "match_rule_id": row.match_rule_id,
        "reservation_group_key": row.reservation_group_key,
        "status": row.status,
        "warning_codes": warning_codes,
        "reserved_at": row.reserved_at,
        "released_at": row.released_at,
        "release_reason": row.release_reason,
    }


def _semi_requirement_dict(row: OrderItemSemiRequirement) -> dict:
    return {
        "id": row.id,
        "order_item_id": row.order_item_id,
        "customer_id": row.customer_id,
        "component_type": row.component_type,
        "board_length_mm": row.board_length_mm,
        "board_width_mm": row.board_width_mm,
        "material_code": row.material_code_snapshot,
        "normalized_material_code": row.normalized_material_code,
        "flute_type": row.flute_type,
        "pieces_per_box": row.pieces_per_box,
        "stock_yield_per_sheet": row.stock_yield_per_sheet,
        "required_piece_quantity": row.required_piece_quantity,
    }


def _semi_candidate_dict(row: SemiFinishedCandidate) -> dict:
    lot = row.lot
    detail = lot.semi_finished_detail
    return {
        "lot_id": lot.id,
        "lot_number": lot.lot_number,
        "version": lot.version,
        "source": row.source,
        "match_rule_id": row.match_rule_id,
        "available_stock_quantity": row.available_stock_quantity,
        "deductible_requirement_quantity": row.deductible_requirement_quantity,
        "warehouse_location": {
            "id": lot.location.id,
            "location_code": lot.location.location_code,
            "location_name": lot.location.location_name,
        },
        "customer_id": detail.owner_customer_id,
        "customer_name": detail.owner_customer_name_snapshot,
        "board_length_mm": detail.board_length_mm,
        "board_width_mm": detail.board_width_mm,
        "material_code": detail.material_code_snapshot,
        "normalized_material_code": detail.normalized_material_code,
        "flute_type": detail.flute_type,
        "component_type": detail.component_type,
        "pieces_per_box": detail.pieces_per_box,
        "stock_yield_per_sheet": detail.stock_yield_per_sheet,
        "signature_differences": list(row.signature_differences),
        "warning_codes": list(row.warning_codes),
        "warning_messages": list(row.warning_messages),
    }


@router.get("/finished/candidates")
def finished_candidates(
    order_item_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(can_view_reservations),
) -> dict:
    _require_order_item_customer_access(db, order_item_id, user)
    try:
        item = db.get(OrderItem, order_item_id)
        if item is None:
            raise WarehouseInventoryError("订单明细不存在", 404)
        reserved = active_finished_reserved_qty(db, order_item_id)
        rows = finished_inventory_candidates(db, order_item_id)
        return {
            "order_item_id": order_item_id,
            "order_quantity": item.quantity,
            "finished_reserved_quantity": reserved,
            "remaining_requirement": max(item.quantity - reserved, 0),
            "items": [
                {
                    "lot_id": lot.id,
                    "lot_number": lot.lot_number,
                    "version": lot.version,
                    "customer_name": lot.finished_detail.owner_customer_name_snapshot,
                    "is_general": lot.finished_detail.is_general,
                    "inventory_code": lot.finished_detail.inventory_code_snapshot,
                    "product_name": lot.finished_detail.product_name_snapshot,
                    "specification": " × ".join(
                        str(value)
                        for value in (
                            lot.finished_detail.length_mm,
                            lot.finished_detail.width_mm,
                            lot.finished_detail.height_mm,
                        )
                        if value is not None
                    ),
                    "material_display": " / ".join(
                        value
                        for value in (
                            lot.finished_detail.material_code_snapshot,
                            lot.finished_detail.flute_type_snapshot,
                        )
                        if value
                    ),
                    "warehouse_location": (
                        f"{lot.location.location_code} {lot.location.location_name}"
                    ),
                    "quantity_available": lot.quantity_available,
                    "stock_date": lot.stock_date,
                    "last_movement_at": lot.last_movement_at,
                    "warning_codes": (
                        ["GENERAL_FINISHED_STOCK"]
                        if lot.finished_detail.is_general
                        else []
                    ),
                    "warning_messages": (
                        ["通用库存，请人工确认是否用于该客户订单。"]
                        if lot.finished_detail.is_general
                        else []
                    ),
                }
                for lot in rows
            ],
        }
    except WarehouseInventoryError as error:
        _handle(error)


@router.post("/finished/reservations")
def create_finished_reservation(
    payload: FinishedReservationPayload,
    db: Session = Depends(get_db),
    user: User = Depends(can_reserve),
) -> dict:
    _require_order_item_customer_access(db, payload.order_item_id, user)
    try:
        row = reserve_finished_inventory(
            db,
            operator_id=user.id,
            **payload.model_dump(),
        )
        db.commit()
        return _reservation_dict(row, db)
    except WarehouseInventoryError as error:
        db.rollback()
        _handle(error)


@router.get("/reservations")
def list_reservations(
    order_item_id: int | None = None,
    inventory_lot_id: int | None = None,
    status_filter: str | None = Query(default=None, alias="status"),
    db: Session = Depends(get_db),
    user: User = Depends(can_view_reservations),
) -> dict:
    query = select(InventoryReservation).where(
        InventoryReservation.reservation_type == "finished_order"
    )
    if order_item_id:
        _require_order_item_customer_access(db, order_item_id, user)
        query = query.where(InventoryReservation.order_item_id == order_item_id)
    if inventory_lot_id:
        query = query.where(
            InventoryReservation.inventory_lot_id == inventory_lot_id
        )
    if status_filter:
        query = query.where(InventoryReservation.status == status_filter)
    visible_customer_ids = _visible_customer_ids(user, db)
    if visible_customer_ids is not None:
        query = query.join(
            Order, Order.id == InventoryReservation.order_id
        ).where(Order.customer_id.in_(visible_customer_ids))
    rows = db.scalars(
        query.order_by(InventoryReservation.id.desc()).limit(500)
    ).all()
    return {"items": [_reservation_dict(row, db) for row in rows]}


@router.post("/reservations/{reservation_id}/release")
def release_reservation(
    reservation_id: int,
    payload: ReleaseReservationPayload,
    db: Session = Depends(get_db),
    user: User = Depends(can_reserve),
) -> dict:
    _require_reservation_customer_access(db, reservation_id, user)
    try:
        row = release_finished_reservation(
            db,
            reservation_id=reservation_id,
            operator_id=user.id,
            release_reason=payload.release_reason,
            idempotency_key=payload.idempotency_key,
        )
        db.commit()
        return _reservation_dict(row, db)
    except WarehouseInventoryError as error:
        db.rollback()
        _handle(error)


@router.get("/finished/products/{product_id}/candidates")
def finished_product_candidates(
    product_id: int,
    customer_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(can_view_reservations),
) -> dict:
    require_customer_access(customer_id, user, db)
    try:
        rows = finished_inventory_candidates_for_product(
            db,
            customer_id=customer_id,
            product_id=product_id,
        )
        return {
            "customer_id": customer_id,
            "product_id": product_id,
            "items": [
                {
                    "lot_id": lot.id,
                    "lot_number": lot.lot_number,
                    "version": lot.version,
                    "is_general": lot.finished_detail.is_general,
                    "quantity_available": lot.quantity_available,
                    "warehouse_location": {
                        "id": lot.location.id,
                        "location_code": lot.location.location_code,
                        "location_name": lot.location.location_name,
                    },
                    "warning_codes": (
                        ["GENERAL_FINISHED_STOCK"]
                        if lot.finished_detail.is_general
                        else []
                    ),
                    "warning_messages": (
                        ["通用成品库存，必须人工确认后才能预占。"]
                        if lot.finished_detail.is_general
                        else []
                    ),
                }
                for lot in rows
            ],
        }
    except WarehouseInventoryError as error:
        _handle(error)


@router.put("/semi-finished/order-items/{order_item_id}/requirements")
def upsert_semi_requirement(
    order_item_id: int,
    payload: SemiRequirementPayload,
    db: Session = Depends(get_db),
    user: User = Depends(can_reserve),
) -> dict:
    _require_order_item_customer_access(db, order_item_id, user)
    try:
        row = save_order_item_semi_requirement(
            db,
            order_item_id=order_item_id,
            operator_id=user.id,
            **payload.model_dump(),
        )
        db.commit()
        return _semi_requirement_dict(row)
    except WarehouseInventoryError as error:
        db.rollback()
        _handle(error)
    except IntegrityError as error:
        db.rollback()
        _handle_integrity(error)


@router.post("/semi-finished/products/{product_id}/candidates")
def semi_product_candidates(
    product_id: int,
    payload: SemiProductCandidatePayload,
    db: Session = Depends(get_db),
    user: User = Depends(can_view_reservations),
) -> dict:
    require_customer_access(payload.customer_id, user, db)
    try:
        rows = semi_finished_candidates_for_product(
            db,
            product_id=product_id,
            **payload.model_dump(),
        )
        return {
            "product_id": product_id,
            "items": [_semi_candidate_dict(row) for row in rows],
        }
    except WarehouseInventoryError as error:
        _handle(error)


@router.post("/semi-finished/products/{product_id}/inventory")
def semi_product_inventory_browser(
    product_id: int,
    payload: SemiProductCandidatePayload,
    db: Session = Depends(get_db),
    user: User = Depends(can_view_reservations),
) -> dict:
    require_customer_access(payload.customer_id, user, db)
    try:
        rows = browse_semi_finished_inventory_for_product(
            db,
            product_id=product_id,
            **payload.model_dump(),
        )
        return {
            "product_id": product_id,
            "items": [_semi_candidate_dict(row) for row in rows],
        }
    except WarehouseInventoryError as error:
        _handle(error)


@router.get("/semi-finished/requirements/{requirement_id}/candidates")
def semi_requirement_candidates(
    requirement_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(can_view_reservations),
) -> dict:
    _require_requirement_customer_access(db, requirement_id, user)
    try:
        requirement = db.get(OrderItemSemiRequirement, requirement_id)
        if requirement is None:
            raise WarehouseInventoryError("半成品需求不存在", 404)
        credited = active_semi_requirement_credited_quantity(db, requirement.id)
        rows = semi_finished_inventory_candidates(db, requirement.id)
        return {
            "requirement": _semi_requirement_dict(requirement),
            "credited_requirement_quantity": credited,
            "remaining_requirement_quantity": max(
                requirement.required_piece_quantity - credited, 0
            ),
            "items": [_semi_candidate_dict(row) for row in rows],
        }
    except WarehouseInventoryError as error:
        _handle(error)


@router.get("/semi-finished/requirements/{requirement_id}/inventory")
def semi_requirement_inventory_browser(
    requirement_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(can_view_reservations),
) -> dict:
    _require_requirement_customer_access(db, requirement_id, user)
    try:
        rows = browse_semi_finished_inventory(db, requirement_id)
        return {"items": [_semi_candidate_dict(row) for row in rows]}
    except WarehouseInventoryError as error:
        _handle(error)


@router.post("/semi-finished/requirements/{requirement_id}/confirm-match")
def confirm_semi_requirement_match(
    requirement_id: int,
    payload: SemiMatchConfirmPayload,
    db: Session = Depends(get_db),
    user: User = Depends(can_reserve),
) -> dict:
    _require_requirement_customer_access(db, requirement_id, user)
    try:
        result = confirm_semi_finished_match(
            db,
            requirement_id=requirement_id,
            operator_id=user.id,
            **payload.model_dump(),
        )
        db.commit()
        return {
            "rule_id": result.rule.id,
            "product_id": result.mapping.product_id,
            "source": "learned",
            "signature_differences": list(result.signature_differences),
            "warning_codes": list(result.warning_codes),
        }
    except WarehouseInventoryError as error:
        db.rollback()
        _handle(error)
    except IntegrityError as error:
        db.rollback()
        _handle_integrity(error)


@router.post("/semi-finished/requirements/{requirement_id}/reserve")
def reserve_semi_requirement(
    requirement_id: int,
    payload: SemiReservationPayload,
    db: Session = Depends(get_db),
    user: User = Depends(can_reserve),
) -> dict:
    _require_requirement_customer_access(db, requirement_id, user)
    try:
        result = reserve_semi_finished_inventory(
            db,
            requirement_id=requirement_id,
            requested_requirement_quantity=payload.requested_requirement_quantity,
            lots=[SemiFinishedLotVersion(**row.model_dump()) for row in payload.lots],
            operator_id=user.id,
            idempotency_key=payload.idempotency_key,
            confirmed=payload.confirmed,
            override=payload.override,
            warning_acknowledged_codes=payload.warning_acknowledged_codes,
        )
        db.commit()
        return {
            "requested_requirement_quantity": result.requested_requirement_quantity,
            "allocated_requirement_quantity": result.allocated_requirement_quantity,
            "unallocated_requirement_quantity": result.unallocated_requirement_quantity,
            "reservations": [
                _reservation_dict(row, db) for row in result.reservations
            ],
        }
    except WarehouseInventoryError as error:
        db.rollback()
        _handle(error)
    except IntegrityError as error:
        db.rollback()
        _handle_integrity(error)


@router.get("/semi-finished/requirements/{requirement_id}/reservations")
def list_semi_requirement_reservations(
    requirement_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(can_view_reservations),
) -> dict:
    _require_requirement_customer_access(db, requirement_id, user)
    rows = db.scalars(
        select(InventoryReservation)
        .where(
            InventoryReservation.semi_requirement_id == requirement_id,
            InventoryReservation.reservation_type == "semi_order",
        )
        .order_by(InventoryReservation.id)
    ).all()
    return {"items": [_reservation_dict(row, db) for row in rows]}


@router.post("/semi-finished/reservations/{reservation_id}/release")
def release_semi_reservation(
    reservation_id: int,
    payload: SemiReleasePayload,
    db: Session = Depends(get_db),
    user: User = Depends(can_reserve),
) -> dict:
    _require_reservation_customer_access(db, reservation_id, user)
    try:
        result = release_semi_finished_reservation(
            db,
            reservation_id=reservation_id,
            operator_id=user.id,
            **payload.model_dump(),
        )
        db.commit()
        return _reservation_dict(result.reservation, db)
    except WarehouseInventoryError as error:
        db.rollback()
        _handle(error)
    except IntegrityError as error:
        db.rollback()
        _handle_integrity(error)


@router.post("/semi-finished/reservations/{reservation_id}/consume")
def consume_semi_reservation(
    reservation_id: int,
    payload: SemiConsumePayload,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    _require_reservation_customer_access(db, reservation_id, user)
    try:
        result = consume_semi_finished_reservation(
            db,
            reservation_id=reservation_id,
            operator_id=user.id,
            **payload.model_dump(),
        )
        db.commit()
        response = _reservation_dict(result.reservation, db)
        response["delivery_inventory_allocation_id"] = (
            result.allocation.id if result.allocation else None
        )
        return response
    except WarehouseInventoryError as error:
        db.rollback()
        _handle(error)
    except IntegrityError as error:
        db.rollback()
        _handle_integrity(error)


@router.post("/semi-finished/reservations/{reservation_id}/reverse-consume")
def reverse_semi_reservation_consumption(
    reservation_id: int,
    payload: SemiReverseConsumePayload,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    _require_reservation_customer_access(db, reservation_id, user)
    try:
        result = reverse_semi_finished_consumption(
            db,
            reservation_id=reservation_id,
            operator_id=user.id,
            **payload.model_dump(),
        )
        db.commit()
        return _reservation_dict(result.reservation, db)
    except WarehouseInventoryError as error:
        db.rollback()
        _handle(error)
    except IntegrityError as error:
        db.rollback()
        _handle_integrity(error)


def _semi_product_dimensions(
    product: Product,
    component_type: str,
) -> tuple[int | None, int | None]:
    if component_type == "base":
        return product.base_report_length_mm, product.base_report_width_mm
    return product.report_length_mm, product.report_width_mm


def _semi_product_material_code(product: Product) -> str:
    return (
        product.default_material_code
        or (product.material.code if product.material is not None else None)
        or product.legacy_material_text
        or ""
    ).strip()


def _semi_lot_assignment_response(
    db: Session,
    *,
    lot_id: int,
    keyword: str | None,
    limit: int,
) -> dict:
    lot = db.scalar(_lot_query().where(InventoryLot.id == lot_id))
    if lot is None or lot.inventory_type != "semi_finished":
        raise WarehouseInventoryError("半成品库存批次不存在", 404)
    detail = lot.semi_finished_detail
    if detail is None:
        raise WarehouseInventoryError("半成品库存批次缺少明细", 409)
    if detail.owner_customer_id is None:
        raise WarehouseInventoryError("请先为半成品库存指定归属客户", 409)
    assigned_ids = set(semi_finished_lot_assigned_product_ids(db, lot.id))
    query = (
        select(Product)
        .options(selectinload(Product.material))
        .where(
            Product.customer_id == detail.owner_customer_id,
            Product.is_active.is_(True),
            Product.deleted_at.is_(None),
        )
    )
    text = (keyword or "").strip()
    if text:
        pattern = f"%{text}%"
        query = query.where(
            or_(
                Product.product_code.like(pattern),
                Product.customer_material_code.like(pattern),
                Product.product_name.like(pattern),
                Product.die_cut_path.like(pattern),
            )
        )
    else:
        report_length_column = (
            Product.base_report_length_mm
            if detail.component_type == "base"
            else Product.report_length_mm
        )
        report_width_column = (
            Product.base_report_width_mm
            if detail.component_type == "base"
            else Product.report_width_mm
        )
        query = query.where(
            or_(
                Product.id.in_(assigned_ids),
                and_(
                    report_length_column == detail.board_length_mm,
                    report_width_column == detail.board_width_mm,
                    func.upper(func.trim(Product.flute_type)) == detail.flute_type,
                ),
            )
        )
    products = db.scalars(
        query.order_by(Product.product_code, Product.id).limit(limit)
    ).all()
    items = []
    for product in products:
        report_length, report_width = _semi_product_dimensions(
            product, detail.component_type
        )
        material_code = _semi_product_material_code(product)
        recommended = all(
            (
                report_length == detail.board_length_mm,
                report_width == detail.board_width_mm,
                normalize_material_code(material_code)
                == detail.normalized_material_code,
                (product.flute_type or "").strip().upper() == detail.flute_type,
            )
        )
        items.append(
            {
                "id": product.id,
                "product_code": product.product_code,
                "customer_material_code": product.customer_material_code,
                "product_name": product.product_name,
                "specification": " × ".join(
                    str(round(value))
                    for value in (product.length_mm, product.width_mm, product.height_mm)
                    if value is not None
                ),
                "report_length_mm": report_length,
                "report_width_mm": report_width,
                "material_code": material_code or None,
                "flute_type": product.flute_type,
                "template_location": (
                    product.mold_tool.rack_location
                    if product.mold_tool is not None
                    else product.die_cut_path
                ),
                "mold_code": (
                    product.mold_tool.mold_code
                    if product.mold_tool is not None
                    else None
                ),
                "assigned": product.id in assigned_ids,
                "recommended": recommended,
            }
        )
    items.sort(
        key=lambda row: (
            not row["assigned"],
            not row["recommended"],
            row["product_code"],
        )
    )
    return {
        "lot_id": lot.id,
        "lot_number": lot.lot_number,
        "customer_id": detail.owner_customer_id,
        "customer_name": detail.owner_customer_name_snapshot,
        "material_code": detail.material_code_snapshot,
        "flute_type": detail.flute_type,
        "board_length_mm": detail.board_length_mm,
        "board_width_mm": detail.board_width_mm,
        "component_type": detail.component_type,
        "assigned_product_ids": sorted(assigned_ids),
        "items": items,
        "mapping_scope": "同一客户、同一半成品规格的新旧批次共用此分配记忆",
    }


@router.get("/lots/{lot_id}/product-assignments")
def get_semi_lot_product_assignments(
    lot_id: int,
    q: str | None = None,
    limit: int = Query(default=200, ge=1, le=500),
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    _require_lot_customer_access(db, lot_id, user)
    try:
        return _semi_lot_assignment_response(
            db, lot_id=lot_id, keyword=q, limit=limit
        )
    except WarehouseInventoryError as error:
        _handle(error)


@router.put("/lots/{lot_id}/product-assignments")
def update_semi_lot_product_assignments(
    lot_id: int,
    payload: SemiProductAssignmentsPayload,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    try:
        rule, products = replace_semi_finished_lot_product_assignments(
            db,
            inventory_lot_id=lot_id,
            product_ids=payload.product_ids,
            operator_id=user.id,
        )
        db.add(
            OperationLog(
                user_id=user.id,
                username=user.username,
                role=user.role,
                action="UPDATE",
                resource=f"warehouse/semi-lot/{lot_id}/product-assignments",
                entity_type="semi_finished_match_rule",
                entity_id=rule.id if rule is not None else None,
                description="更新半成品库存适用成品款号",
                details=json.dumps(
                    {
                        "lot_id": lot_id,
                        "product_ids": [row.id for row in products],
                    },
                    ensure_ascii=False,
                ),
            )
        )
        db.commit()
        return _semi_lot_assignment_response(
            db, lot_id=lot_id, keyword=None, limit=500
        )
    except WarehouseInventoryError as error:
        db.rollback()
        _handle(error)
    except IntegrityError as error:
        db.rollback()
        _handle_integrity(error)


@router.get("/locations")
def list_locations(
    include_inactive: bool = False,
    db: Session = Depends(get_db),
    _user: User = Depends(can_read),
) -> dict:
    query = select(WarehouseLocation)
    if not include_inactive:
        query = query.where(WarehouseLocation.is_active.is_(True))
    rows = db.scalars(query.order_by(WarehouseLocation.location_code)).all()
    return {"items": [_location_dict(row) for row in rows]}


@router.get("/references/customers")
def reference_customers(
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    query = select(Customer).where(Customer.is_active.is_(True))
    visible_customer_ids = _visible_customer_ids(user, db)
    if visible_customer_ids is not None:
        query = query.where(Customer.id.in_(visible_customer_ids))
    rows = db.scalars(
        query.order_by(Customer.customer_number, Customer.id)
    ).all()
    return {"items": [{"id": row.id, "name": row.name} for row in rows]}


@router.get("/references/products")
def reference_products(
    customer_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    require_customer_access(customer_id, user, db)
    rows = db.scalars(
        select(Product)
        .where(
            Product.customer_id == customer_id,
            Product.is_active.is_(True),
            Product.deleted_at.is_(None),
        )
        .order_by(Product.product_code, Product.id)
    ).all()
    return {
        "items": [
            {
                "id": row.id,
                "product_code": row.product_code,
                "product_name": row.product_name,
                "specification": " × ".join(
                    str(round(value))
                    for value in (row.length_mm, row.width_mm, row.height_mm)
                    if value is not None
                ),
            }
            for row in rows
        ]
    }


def _mold_tool_dict(row: MoldTool) -> dict:
    products = sorted(
        (
            product
            for product in row.products
            if product.deleted_at is None and product.is_active
        ),
        key=lambda product: (product.customer.name if product.customer else "", product.product_code, product.id),
    )
    return {
        "id": row.id,
        "mold_code": row.mold_code,
        "mold_name": row.mold_name,
        "rack_location": row.rack_location,
        "location_guide": describe_mold_location(row.rack_location),
        "remarks": row.remarks,
        "is_active": row.is_active,
        "product_count": len(products),
        "products": [
            {
                "id": product.id,
                "customer_id": product.customer_id,
                "customer_name": product.customer.name if product.customer else None,
                "product_code": product.product_code,
                "product_name": product.product_name,
                "customer_material_code": product.customer_material_code,
                "specification": " × ".join(
                    str(round(value))
                    for value in (product.length_mm, product.width_mm, product.height_mm)
                    if value is not None
                ),
                "report_specification": " × ".join(
                    str(round(value))
                    for value in (product.report_length_mm, product.report_width_mm)
                    if value is not None
                ),
                "production_process": product.production_process,
                "direction_note": product.report_notes,
            }
            for product in products
        ],
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


@router.get("/molds")
def list_mold_tools(
    q: str | None = None,
    include_inactive: bool = False,
    limit: int = Query(default=200, ge=1, le=500),
    db: Session = Depends(get_db),
    _user: User = Depends(can_read),
) -> dict:
    query = select(MoldTool).options(
        selectinload(MoldTool.products).selectinload(Product.customer)
    )
    if not include_inactive:
        query = query.where(MoldTool.is_active.is_(True))
    keyword = (q or "").strip()
    if keyword:
        pattern = f"%{keyword}%"
        linked_molds = (
            select(Product.mold_tool_id)
            .join(Customer, Customer.id == Product.customer_id)
            .where(
                Product.mold_tool_id.is_not(None),
                Product.deleted_at.is_(None),
                or_(
                    Product.product_code.like(pattern),
                    Product.customer_material_code.like(pattern),
                    Product.product_name.like(pattern),
                    Customer.name.like(pattern),
                ),
            )
        )
        query = query.where(
            or_(
                MoldTool.mold_code.like(pattern),
                MoldTool.mold_name.like(pattern),
                MoldTool.rack_location.like(pattern),
                MoldTool.remarks.like(pattern),
                MoldTool.id.in_(linked_molds),
            )
        )
    rows = db.scalars(
        query.order_by(MoldTool.rack_location, MoldTool.mold_code, MoldTool.id).limit(limit)
    ).unique().all()
    return {"items": [_mold_tool_dict(row) for row in rows]}


def _lan_ip() -> str:
    connection = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        connection.connect(("8.8.8.8", 80))
        return connection.getsockname()[0]
    except OSError:
        return socket.gethostbyname(socket.gethostname())
    finally:
        connection.close()


@router.get("/molds/{mold_id}/label")
def get_mold_label(
    mold_id: int,
    request: Request,
    db: Session = Depends(get_db),
    _user: User = Depends(can_read),
) -> dict:
    row = db.scalar(
        select(MoldTool)
        .options(selectinload(MoldTool.products).selectinload(Product.customer))
        .where(MoldTool.id == mold_id)
    )
    if row is None:
        raise HTTPException(status_code=404, detail="模具不存在")
    port = request.url.port or 8000
    lookup_url = (
        f"http://{_lan_ip()}:{port}/mobile/mold-lookup"
        f"?mold={quote(row.mold_code, safe='')}"
    )
    image = qrcode.make(lookup_url)
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return {
        **_mold_tool_dict(row),
        "lookup_url": lookup_url,
        "qr_data_url": (
            "data:image/png;base64,"
            + base64.b64encode(buffer.getvalue()).decode("ascii")
        ),
    }


@router.post("/molds", status_code=201)
def create_mold_tool(
    payload: MoldToolPayload,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    row = MoldTool(**payload.model_dump(), created_by=user.id, updated_by=user.id)
    db.add(row)
    try:
        db.commit()
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(status_code=409, detail="模具编号已存在") from error
    db.refresh(row)
    return _mold_tool_dict(row)


@router.put("/molds/{mold_id}")
def update_mold_tool(
    mold_id: int,
    payload: MoldToolPayload,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    row = db.get(MoldTool, mold_id)
    if row is None:
        raise HTTPException(status_code=404, detail="模具不存在")
    for key, value in payload.model_dump().items():
        setattr(row, key, value)
    row.updated_by = user.id
    try:
        db.commit()
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(status_code=409, detail="模具编号已存在") from error
    db.refresh(row)
    return _mold_tool_dict(row)


@router.put("/molds/{mold_id}/enable")
def enable_mold_tool(
    mold_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    row = db.get(MoldTool, mold_id)
    if row is None:
        raise HTTPException(status_code=404, detail="模具不存在")
    row.is_active = True
    row.updated_by = user.id
    db.commit()
    return _mold_tool_dict(row)


@router.put("/molds/{mold_id}/disable")
def disable_mold_tool(
    mold_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    row = db.get(MoldTool, mold_id)
    if row is None:
        raise HTTPException(status_code=404, detail="模具不存在")
    row.is_active = False
    row.updated_by = user.id
    db.commit()
    return _mold_tool_dict(row)


@router.get("/references/template-locations")
def search_template_locations(
    q: str | None = None,
    limit: int = Query(default=100, ge=1, le=200),
    db: Session = Depends(get_db),
    _user: User = Depends(can_read),
) -> dict:
    keyword = (q or "").strip()
    if not keyword:
        return {"items": [], "message": "请输入存货编码、产品名称或模板位置"}
    pattern = f"%{keyword}%"
    rows = db.execute(
        select(Product, Customer, MoldTool)
        .join(Customer, Customer.id == Product.customer_id)
        .outerjoin(MoldTool, MoldTool.id == Product.mold_tool_id)
        .where(
            Product.is_active.is_(True),
            Product.deleted_at.is_(None),
            or_(
                MoldTool.id.is_not(None),
                and_(
                    Product.die_cut_path.is_not(None),
                    func.trim(Product.die_cut_path) != "",
                ),
            ),
            or_(
                Product.product_code.like(pattern),
                Product.customer_material_code.like(pattern),
                Product.product_name.like(pattern),
                Product.die_cut_path.like(pattern),
                MoldTool.mold_code.like(pattern),
                MoldTool.mold_name.like(pattern),
                MoldTool.rack_location.like(pattern),
                Customer.name.like(pattern),
            ),
        )
        .order_by(Customer.name, Product.product_code, Product.id)
        .limit(limit)
    ).all()
    return {
        "items": [
            {
                "product_id": product.id,
                "customer_id": customer.id,
                "customer_name": customer.name,
                "product_code": product.product_code,
                "customer_material_code": product.customer_material_code,
                "product_name": product.product_name,
                "mold_tool_id": mold_tool.id if mold_tool else None,
                "mold_code": mold_tool.mold_code if mold_tool else None,
                "mold_name": mold_tool.mold_name if mold_tool else None,
                "specification": " × ".join(
                    str(round(value))
                    for value in (
                        product.length_mm,
                        product.width_mm,
                        product.height_mm,
                    )
                    if value is not None
                ),
                "template_location": (
                    mold_tool.rack_location if mold_tool else product.die_cut_path
                ),
            }
            for product, customer, mold_tool in rows
        ]
    }


@router.post("/locations")
def create_location(
    payload: LocationPayload,
    db: Session = Depends(get_db),
    _user: User = Depends(admin_only),
) -> dict:
    row = WarehouseLocation(**payload.model_dump())
    db.add(row)
    try:
        db.commit()
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(status_code=409, detail="库位编码已存在") from error
    return _location_dict(row)


@router.put("/locations/{location_id}")
def update_location(
    location_id: int,
    payload: LocationPayload,
    db: Session = Depends(get_db),
    _user: User = Depends(admin_only),
) -> dict:
    row = db.get(WarehouseLocation, location_id)
    if row is None:
        raise HTTPException(status_code=404, detail="库位不存在")
    for key, value in payload.model_dump().items():
        setattr(row, key, value)
    try:
        db.commit()
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(status_code=409, detail="库位编码已存在") from error
    return _location_dict(row)


@router.put("/locations/{location_id}/enable")
def enable_location(
    location_id: int,
    db: Session = Depends(get_db),
    _user: User = Depends(admin_only),
) -> dict:
    row = db.get(WarehouseLocation, location_id)
    if row is None:
        raise HTTPException(status_code=404, detail="库位不存在")
    row.is_active = True
    db.commit()
    return _location_dict(row)


@router.put("/locations/{location_id}/disable")
def disable_location(
    location_id: int,
    db: Session = Depends(get_db),
    _user: User = Depends(admin_only),
) -> dict:
    row = db.get(WarehouseLocation, location_id)
    if row is None:
        raise HTTPException(status_code=404, detail="库位不存在")
    row.is_active = False
    db.commit()
    return _location_dict(row)


@router.get("/lots")
def list_lots(
    inventory_type: str | None = None,
    status: str | None = None,
    location_id: int | None = None,
    keyword: str | None = None,
    stale_level: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    query = _lot_query()
    visible_customer_ids = _visible_customer_ids(user, db)
    if visible_customer_ids is not None:
        query = query.where(_visible_lot_condition(visible_customer_ids))
    if inventory_type:
        query = query.where(InventoryLot.inventory_type == inventory_type)
    if status:
        query = query.where(InventoryLot.status == status)
    if location_id:
        query = query.where(InventoryLot.warehouse_location_id == location_id)
    if keyword:
        text = keyword.strip()
        pattern = f"%{text}%"
        location_ids = select(WarehouseLocation.id).where(
            or_(
                WarehouseLocation.location_code.like(pattern),
                WarehouseLocation.location_name.like(pattern),
            )
        )
        finished_lot_ids = select(FinishedGoodsInventoryDetail.inventory_lot_id).where(
            or_(
                FinishedGoodsInventoryDetail.inventory_code_snapshot.like(pattern),
                FinishedGoodsInventoryDetail.product_name_snapshot.like(pattern),
                FinishedGoodsInventoryDetail.owner_customer_name_snapshot.like(pattern),
            )
        )
        semi_lot_ids = select(SemiFinishedInventoryDetail.inventory_lot_id).where(
            or_(
                SemiFinishedInventoryDetail.material_code_snapshot.like(pattern),
                SemiFinishedInventoryDetail.owner_customer_name_snapshot.like(pattern),
                SemiFinishedInventoryDetail.supplier_name.like(pattern),
                SemiFinishedInventoryDetail.cutting_note.like(pattern),
            )
        )
        query = query.where(
            or_(
                InventoryLot.lot_number.like(pattern),
                InventoryLot.warehouse_location_id.in_(location_ids),
                InventoryLot.id.in_(finished_lot_ids),
                InventoryLot.id.in_(semi_lot_ids),
            )
        )
    if stale_level:
        days = {"attention": 365, "handling": 548, "cleanup": 730}.get(stale_level)
        if days:
            query = query.where(
                InventoryLot.last_movement_at <= datetime.now() - timedelta(days=days)
            )
    count_query = select(func.count()).select_from(query.order_by(None).subquery())
    total = db.scalar(count_query) or 0
    rows = db.scalars(
        query.order_by(InventoryLot.last_movement_at.desc(), InventoryLot.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return {"items": [_lot_dict(row) for row in rows], "total": total}


@router.get("/insights")
def get_inventory_insights(
    as_of: date | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    if not has_unrestricted_customer_access(user, db):
        raise HTTPException(
            status_code=403,
            detail="库存洞察暂不支持按客户范围安全聚合",
        )
    return build_inventory_insights(db, as_of=as_of)


@router.get("/lots/{lot_id}")
def get_lot(
    lot_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    _require_lot_customer_access(db, lot_id, user)
    row = db.scalar(_lot_query().where(InventoryLot.id == lot_id))
    if row is None:
        raise HTTPException(status_code=404, detail="库存批次不存在")
    result = _lot_dict(row)
    result["movements"] = [
        _movement_dict(item)
        for item in db.scalars(
            select(InventoryMovement)
            .options(selectinload(InventoryMovement.lot))
            .where(InventoryMovement.inventory_lot_id == lot_id)
            .order_by(InventoryMovement.id.desc())
        ).all()
    ]
    reservation_query = select(InventoryReservation).where(
        InventoryReservation.inventory_lot_id == lot_id
    )
    visible_customer_ids = _visible_customer_ids(user, db)
    if visible_customer_ids is not None:
        reservation_query = reservation_query.outerjoin(
            Order, Order.id == InventoryReservation.order_id
        ).where(
            or_(
                InventoryReservation.order_id.is_(None),
                Order.customer_id.in_(visible_customer_ids),
            )
        )
    result["reservations"] = [
        _reservation_dict(item, db)
        for item in db.scalars(
            reservation_query.order_by(InventoryReservation.id.desc())
        ).all()
    ]
    return result


@router.get("/movements")
def list_movements(
    lot_number: str | None = None,
    inventory_type: str | None = None,
    movement_type: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    query = select(InventoryMovement).join(InventoryLot).options(
        selectinload(InventoryMovement.lot)
    )
    visible_customer_ids = _visible_customer_ids(user, db)
    if visible_customer_ids is not None:
        query = query.where(_visible_lot_condition(visible_customer_ids))
    if lot_number:
        query = query.where(InventoryLot.lot_number.contains(lot_number.strip()))
    if inventory_type:
        query = query.where(InventoryLot.inventory_type == inventory_type)
    if movement_type:
        query = query.where(InventoryMovement.movement_type == movement_type)
    if date_from:
        query = query.where(InventoryMovement.created_at >= datetime.combine(date_from, datetime.min.time()))
    if date_to:
        query = query.where(InventoryMovement.created_at < datetime.combine(date_to + timedelta(days=1), datetime.min.time()))
    total = db.scalar(select(func.count()).select_from(query.order_by(None).subquery())) or 0
    rows = db.scalars(
        query.order_by(InventoryMovement.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return {"items": [_movement_dict(row) for row in rows], "total": total}


@router.post("/finished/manual-in")
def finished_manual_in(
    payload: FinishedManualInPayload,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    require_customer_access(payload.customer_id, user, db)
    try:
        row = manual_finished_in(db, operator_id=user.id, **payload.model_dump())
        db.commit()
        return _lot_dict(row)
    except WarehouseInventoryError as error:
        db.rollback()
        _handle(error)


@router.post("/semi-finished/manual-in")
def semi_finished_manual_in(
    payload: SemiFinishedManualInPayload,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    if payload.customer_id is not None:
        require_customer_access(payload.customer_id, user, db)
    try:
        row = manual_semi_finished_in(db, operator_id=user.id, **payload.model_dump())
        db.commit()
        return _lot_dict(row)
    except WarehouseInventoryError as error:
        db.rollback()
        _handle(error)


def _operate(
    db: Session,
    user: User,
    lot_id: int,
    operation: str,
    payload: VersionPayload,
    quantity: int = 0,
) -> dict:
    _require_lot_customer_access(db, lot_id, user)
    try:
        row = mutate_lot(
            db,
            lot_id=lot_id,
            operation=operation,
            expected_version=payload.expected_version,
            operator_id=user.id,
            quantity=quantity,
            reason=payload.reason,
            idempotency_key=payload.idempotency_key,
        )
        db.commit()
        return _lot_dict(row)
    except WarehouseInventoryError as error:
        db.rollback()
        _handle(error)


@router.post("/lots/{lot_id}/adjust")
def adjust_lot(lot_id: int, payload: AdjustPayload, db: Session = Depends(get_db), user: User = Depends(can_operate)) -> dict:
    return _operate(db, user, lot_id, "adjust", payload, payload.quantity_delta)


@router.post("/lots/{lot_id}/freeze")
def freeze_lot(lot_id: int, payload: VersionPayload, db: Session = Depends(get_db), user: User = Depends(can_operate)) -> dict:
    return _operate(db, user, lot_id, "freeze", payload)


@router.post("/lots/{lot_id}/unfreeze")
def unfreeze_lot(lot_id: int, payload: VersionPayload, db: Session = Depends(get_db), user: User = Depends(can_operate)) -> dict:
    return _operate(db, user, lot_id, "unfreeze", payload)


@router.post("/lots/{lot_id}/damage")
def damage_lot(lot_id: int, payload: QuantityOperationPayload, db: Session = Depends(get_db), user: User = Depends(can_operate)) -> dict:
    return _operate(db, user, lot_id, "damage", payload, payload.quantity)


@router.post("/lots/{lot_id}/scrap")
def scrap_lot(lot_id: int, payload: QuantityOperationPayload, db: Session = Depends(get_db), user: User = Depends(can_operate)) -> dict:
    return _operate(db, user, lot_id, "scrap", payload, payload.quantity)


@router.post("/lots/{lot_id}/transfer-to-general")
def transfer_to_general(lot_id: int, payload: VersionPayload, db: Session = Depends(get_db), user: User = Depends(admin_only)) -> dict:
    return _operate(db, user, lot_id, "transfer_to_general", payload)
