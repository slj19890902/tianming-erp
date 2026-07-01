from __future__ import annotations

from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.api.deps import RoleChecker, get_db
from app.models.user import User
from app.models.customer import Customer
from app.models.product import Product
from app.models.warehouse_inventory import (
    InventoryLot,
    InventoryMovement,
    WarehouseLocation,
)
from app.services.warehouse_inventory import (
    WarehouseInventoryError,
    inventory_age_warning,
    manual_finished_in,
    manual_semi_finished_in,
    mutate_lot,
)


router = APIRouter()
can_read = RoleChecker(["admin", "workshop"])
can_operate = RoleChecker(["admin", "workshop"])
admin_only = RoleChecker(["admin"])
VALID_SOURCE_TYPES = {
    "manual",
    "production_surplus",
    "purchase_surplus",
    "stocktake",
    "transfer",
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


def _handle(error: WarehouseInventoryError) -> None:
    raise HTTPException(status_code=error.status_code, detail=str(error)) from error


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
            "material_code": item.material_code_snapshot,
            "layer_count": item.layer_count,
            "flute_type": item.flute_type,
            "board_length_mm": item.board_length_mm,
            "board_width_mm": item.board_width_mm,
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
    _user: User = Depends(can_read),
) -> dict:
    rows = db.scalars(
        select(Customer)
        .where(Customer.is_active.is_(True))
        .order_by(Customer.customer_number, Customer.id)
    ).all()
    return {"items": [{"id": row.id, "name": row.name} for row in rows]}


@router.get("/references/products")
def reference_products(
    customer_id: int,
    db: Session = Depends(get_db),
    _user: User = Depends(can_read),
) -> dict:
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
    _user: User = Depends(can_read),
) -> dict:
    query = _lot_query()
    if inventory_type:
        query = query.where(InventoryLot.inventory_type == inventory_type)
    if status:
        query = query.where(InventoryLot.status == status)
    if location_id:
        query = query.where(InventoryLot.warehouse_location_id == location_id)
    if keyword:
        query = query.where(InventoryLot.lot_number.contains(keyword.strip()))
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


@router.get("/lots/{lot_id}")
def get_lot(
    lot_id: int,
    db: Session = Depends(get_db),
    _user: User = Depends(can_read),
) -> dict:
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
    _user: User = Depends(can_read),
) -> dict:
    query = select(InventoryMovement).join(InventoryLot).options(
        selectinload(InventoryMovement.lot)
    )
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
