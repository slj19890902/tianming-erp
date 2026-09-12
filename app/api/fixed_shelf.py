from __future__ import annotations
import hashlib
import json
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from app.api.deps import PermissionChecker, customer_scope_ids, get_db, has_unrestricted_customer_access, require_customer_access
from app.models.fixed_shelf import ShelfBinding, ShelfLotState, ShelfMutation, ShelfProfile
from app.models.product import Product
from app.models.user import User
from app.models.warehouse_inventory import FinishedGoodsInventoryDetail, InventoryLot, WarehouseLocation
from app.services import fixed_shelf as service
from app.services.audit_log import append_audit_event
from app.services.location_candidates import load_warehouse_location_projection_contexts
from app.services.warehouse_inventory import WarehouseInventoryError, transfer_finished_lot_between_locations

router = APIRouter()
can_read = PermissionChecker("warehouse.view")
can_write = PermissionChecker("warehouse.execute")


class StoragePayload(BaseModel):
    expected_version: int = Field(ge=0)
    area_id: int | None = Field(default=None, gt=0)
    location_id: int | None = Field(default=None, gt=0)
    address_version: int | None = Field(default=None, gt=0)
    layout_version: int | None = Field(default=None, gt=0)
    idempotency_key: str = Field(min_length=1, max_length=100, pattern=r"\S")


@router.get("/receipt-storage/{product_id}")
def receipt_storage(product_id: int, db: Session = Depends(get_db), user: User = Depends(can_read)):
    product_for_user(db, user, product_id)
    from app.services import receipt_putaway
    from app.models.warehouse_inventory import WarehouseArea, WarehouseFloor
    rows = db.scalars(select(WarehouseLocation).where(WarehouseLocation.is_active.is_(True),
        WarehouseLocation.placement_status == "placed").order_by(WarehouseLocation.sort_order, WarehouseLocation.id)).all()
    contexts = load_warehouse_location_projection_contexts(db, rows)
    locations = []
    for row in rows:
        from app.services.location_candidates import operational_location_issue
        if operational_location_issue(db, row, warehouse_types={"finished", "shared"}, require_published=True,
                require_map_geometry=True, required_inventory_type="finished", projection_context=contexts.get(row.id)) is None:
            item = service.location_info(db, row, contexts.get(row.id))
            item["area_id"] = contexts[row.id]["area"].id
            item["area_name"] = contexts[row.id]["area"].area_name
            item["issue"] = None
            locations.append(item)
    return {**receipt_putaway.info(db, product_id), "locations": locations}


@router.put("/receipt-storage/{product_id}")
def save_receipt_storage(product_id: int, payload: StoragePayload, request: Request,
                         db: Session = Depends(get_db), user: User = Depends(can_write)):
    if user.role not in {"admin", "boss"}:
        raise HTTPException(403, "仅管理员或老板可设置默认货位")
    product_for_user(db, user, product_id)
    from app.services import receipt_putaway
    return mutate(db, user, request, "warehouse.receipt_storage.configure", product_id, payload,
        lambda: receipt_putaway.save(db, product_id, **payload.model_dump(exclude={"idempotency_key"})))


class BindingPayload(BaseModel):
    location_id: int = Field(gt=0)
    priority: int = Field(ge=0)
    capacity: int | None = Field(default=None, gt=0)
    address_version: int = Field(gt=0)
    layout_version: int = Field(gt=0)


class ProfilePayload(BaseModel):
    expected_version: int = Field(ge=0)
    units_per_bundle: int | None = Field(default=None, gt=0, le=1000000)
    staging_location_id: int | None = Field(default=None, gt=0)
    bindings: list[BindingPayload] = Field(min_length=1, max_length=50)
    idempotency_key: str = Field(min_length=1, max_length=100, pattern=r"\S")


class PutawayPayload(BaseModel):
    expected_version: int = Field(gt=0)
    quantity: int = Field(gt=0)
    location_id: int = Field(gt=0)
    address_version: int = Field(gt=0)
    layout_version: int = Field(gt=0)
    source_location_id: int = Field(gt=0)
    source_address_version: int = Field(gt=0)
    source_layout_version: int | None = Field(default=None, gt=0)
    idempotency_key: str = Field(min_length=1, max_length=100, pattern=r"\S")


class PackagingPayload(BaseModel):
    expected_version: int = Field(gt=0)
    units_per_bundle: int = Field(gt=0, le=1000000)
    idempotency_key: str = Field(min_length=1, max_length=100, pattern=r"\S")


def product_for_user(db, user, product_id):
    product = db.get(Product, product_id)
    if not product or product.deleted_at is not None:
        raise HTTPException(404, "产品不存在")
    require_customer_access(product.customer_id, user, db)
    return product


def lot_for_user(db, user, lot_id):
    lot = db.get(InventoryLot, lot_id)
    if not lot or lot.inventory_type != "finished" or not lot.finished_detail or lot.finished_detail.is_general:
        raise HTTPException(404, "客户专用成品批次不存在")
    require_customer_access(lot.finished_detail.owner_customer_id, user, db)
    product_for_user(db, user, lot.finished_detail.product_id)
    return lot


def mutate(db, user, request, action, resource_id, payload, operation):
    raw = json.dumps({"actor": user.id, "action": action, "id": resource_id, "payload": payload.model_dump()}, sort_keys=True)
    digest = hashlib.sha256(raw.encode()).hexdigest()
    previous = db.get(ShelfMutation, payload.idempotency_key)
    if previous:
        if previous.request_hash != digest:
            raise HTTPException(409, "相同请求编号不能提交不同内容")
        return json.loads(previous.result_json)
    try:
        result = operation()
        db.add(ShelfMutation(idempotency_key=payload.idempotency_key, request_hash=digest,
            result_json=json.dumps(result, ensure_ascii=False)))
        append_audit_event(db, event_category="business", result="success", source="web", module_code="warehouse",
            action_code=action, resource="fixed_shelf", request=request, actor=user, object_ref=str(resource_id),
            details={"request": payload.model_dump(), "result": result})
        db.commit()
        return result
    except (service.ShelfError, WarehouseInventoryError, IntegrityError) as error:
        db.rollback()
        message = "配置或库存已被其他操作更改，请刷新" if isinstance(error, IntegrityError) else str(error)
        raise HTTPException(409, message) from error
    except Exception:
        db.rollback()
        raise


@router.get("/products")
def search_products(q: str = Query(default="", max_length=100), customer_id: int | None = None,
                    db: Session = Depends(get_db), user: User = Depends(can_read)):
    stmt = select(Product).where(Product.deleted_at.is_(None))
    if not has_unrestricted_customer_access(user, db):
        stmt = stmt.where(Product.customer_id.in_(customer_scope_ids(user, db)))
    if customer_id is not None:
        require_customer_access(customer_id, user, db)
        stmt = stmt.where(Product.customer_id == customer_id)
    if q.strip():
        stmt = stmt.where(Product.customer_material_code.contains(q.strip(), autoescape=True) | Product.product_name.contains(q.strip(), autoescape=True))
    else:
        stmt = stmt.join(ShelfProfile, ShelfProfile.product_id == Product.id)
    return {"items": [service.profile_info(db, row) for row in db.scalars(stmt.order_by(Product.customer_id, Product.customer_material_code).limit(30)).all()]}


@router.get("/products/{product_id}")
def get_profile(product_id: int, db: Session = Depends(get_db), user: User = Depends(can_read)):
    return service.profile_info(db, product_for_user(db, user, product_id))


@router.put("/products/{product_id}")
def update_profile(product_id: int, payload: ProfilePayload, request: Request,
                   db: Session = Depends(get_db), user: User = Depends(can_write)):
    product = product_for_user(db, user, product_id)
    return mutate(db, user, request, "warehouse.fixed_shelf.configure", product_id, payload,
        lambda: service.save_profile(db, product, expected_version=payload.expected_version,
            units_per_bundle=payload.units_per_bundle, staging_location_id=payload.staging_location_id,
            bindings=sorted([x.model_dump() for x in payload.bindings], key=lambda x: x["priority"])))


@router.get("/locations")
def locations(floor: int | None = None, db: Session = Depends(get_db), user: User = Depends(can_read)):
    stmt = select(WarehouseLocation).where(WarehouseLocation.is_active.is_(True), WarehouseLocation.storage_type == "rack",
        WarehouseLocation.placement_status == "placed", WarehouseLocation.is_temporary.is_(False))
    if floor is not None:
        stmt = stmt.where(WarehouseLocation.warehouse_floor == floor)
    rows = db.scalars(stmt.order_by(WarehouseLocation.warehouse_floor, WarehouseLocation.area_code,
        WarehouseLocation.sort_order, WarehouseLocation.rack_code, WarehouseLocation.level_no, WarehouseLocation.slot_no).limit(1500)).all()
    contexts = load_warehouse_location_projection_contexts(db, rows)
    return {"items": [service.location_info(db, row, contexts.get(row.id, {})) for row in rows], "limit": 1500}


@router.get("/products/{product_id}/lots")
def product_lots(product_id: int, db: Session = Depends(get_db), user: User = Depends(can_read)):
    product = product_for_user(db, user, product_id)
    rows = db.scalars(select(InventoryLot).join(FinishedGoodsInventoryDetail,
        FinishedGoodsInventoryDetail.inventory_lot_id == InventoryLot.id).where(
        FinishedGoodsInventoryDetail.product_id == product.id,
        FinishedGoodsInventoryDetail.owner_customer_id == product.customer_id,
        FinishedGoodsInventoryDetail.is_general.is_(False), InventoryLot.status == "active",
        InventoryLot.quantity_available + InventoryLot.quantity_reserved > 0).order_by(InventoryLot.id).limit(100)).all()
    result = []
    for lot in rows:
        state = db.get(ShelfLotState, lot.id)
        source = service.location_info(db, db.get(WarehouseLocation, lot.warehouse_location_id))
        source_binding = db.get(ShelfBinding, lot.warehouse_location_id)
        result.append({"lot_id": lot.id, "lot_number": lot.lot_number, "version": lot.version,
            "quantity": service.physical_quantity(lot), "movable_quantity": int(lot.quantity_available or 0) + int(lot.quantity_reserved or 0),
            "units_per_bundle": state.units_per_bundle if state else None,
            "can_correct": not lot.quantity_consumed,
            "can_putaway": bool((state and state.target_location_id) or (source_binding and source_binding.product_id == product.id)),
            "source": source, "location": source['name']})
    return {"items": result, "limit": 100}


@router.get('/staging-locations')
def staging_locations(db: Session = Depends(get_db), user: User = Depends(can_read)):
    rows = db.scalars(select(WarehouseLocation).where(WarehouseLocation.is_active.is_(True),
        WarehouseLocation.storage_type.in_(['ground', 'temporary_aisle']),
        WarehouseLocation.placement_status == 'placed').order_by(WarehouseLocation.warehouse_floor,
        WarehouseLocation.area_code, WarehouseLocation.sort_order).limit(1500)).all()
    contexts = load_warehouse_location_projection_contexts(db, rows)
    return {'items': [service.staging_info(db, row, contexts.get(row.id, {})) for row in rows]}


@router.get("/putaway")
def pending_putaway(db: Session = Depends(get_db), user: User = Depends(can_read)):
    stmt = select(InventoryLot).join(ShelfLotState, ShelfLotState.lot_id == InventoryLot.id).join(FinishedGoodsInventoryDetail,
        FinishedGoodsInventoryDetail.inventory_lot_id == InventoryLot.id).where(ShelfLotState.target_location_id.is_not(None),
        InventoryLot.status == "active", InventoryLot.quantity_available + InventoryLot.quantity_reserved > 0)
    if not has_unrestricted_customer_access(user, db):
        stmt = stmt.where(FinishedGoodsInventoryDetail.owner_customer_id.in_(customer_scope_ids(user, db)))
    result = []
    for lot in db.scalars(stmt.order_by(InventoryLot.stock_date, InventoryLot.id).limit(100)).all():
        product = db.get(Product, lot.finished_detail.product_id)
        profile = service.profile_info(db, product)
        state = db.get(ShelfLotState, lot.id)
        source = db.get(WarehouseLocation, lot.warehouse_location_id)
        result.append({"lot_id": lot.id, "lot_number": lot.lot_number, "version": lot.version,
            "quantity": int(lot.quantity_available or 0) + int(lot.quantity_reserved or 0),
            "source": service.location_info(db, source), "target_location_id": state.target_location_id,
            "profile": profile, "units_per_bundle": state.units_per_bundle})
    return {"items": result}


@router.post("/putaway/{lot_id}")
def putaway(lot_id: int, payload: PutawayPayload, request: Request,
            db: Session = Depends(get_db), user: User = Depends(can_write)):
    lot = lot_for_user(db, user, lot_id)
    def apply():
        state = db.get(ShelfLotState, lot.id)
        binding = db.get(ShelfBinding, payload.location_id)
        source_binding = db.get(ShelfBinding, lot.warehouse_location_id)
        if (not state or state.target_location_id is None) and (not source_binding or source_binding.product_id != lot.finished_detail.product_id):
            raise service.ShelfError("此批次没有待上架任务或固定货位，请通过仓库正式移库核对")
        if not binding or binding.product_id != lot.finished_detail.product_id:
            raise service.ShelfError("请选择该客户料号的主位或补充位")
        target = db.get(WarehouseLocation, binding.location_id)
        issue = service.location_issue(db, target)
        if issue:
            raise service.ShelfError(issue)
        result = transfer_finished_lot_between_locations(db, lot_id=lot.id, expected_version=payload.expected_version,
            quantity=payload.quantity, location_id=target.id, operator_id=user.id, idempotency_key=payload.idempotency_key,
            expected_source_location_id=payload.source_location_id,
            expected_source_address_version=payload.source_address_version,
            expected_source_layout_version=payload.source_layout_version,
            expected_target_address_version=payload.address_version, expected_target_layout_version=payload.layout_version)
        db.flush()
        return {"source_lot_id": result.source_lot.id, "target_lot_id": result.target_lot.id, "location_id": target.id, "quantity": payload.quantity}
    return mutate(db, user, request, "warehouse.fixed_shelf.putaway", lot_id, payload, apply)


@router.put("/lots/{lot_id}/packaging")
def update_packaging(lot_id: int, payload: PackagingPayload, request: Request,
                     db: Session = Depends(get_db), user: User = Depends(can_write)):
    lot = lot_for_user(db, user, lot_id)
    def apply():
        result = db.execute(update(InventoryLot).where(InventoryLot.id == lot.id, InventoryLot.version == payload.expected_version,
            InventoryLot.status == "active", InventoryLot.quantity_consumed == 0).values(version=InventoryLot.version + 1))
        if result.rowcount != 1:
            raise service.ShelfError("批次已变化或已有发货历史，不能修改包装")
        state = db.get(ShelfLotState, lot.id)
        if state is None:
            db.add(ShelfLotState(lot_id=lot.id, units_per_bundle=payload.units_per_bundle))
        else:
            state.units_per_bundle = payload.units_per_bundle
        return {"lot_id": lot.id, "units_per_bundle": payload.units_per_bundle}
    return mutate(db, user, request, "warehouse.fixed_shelf.packaging", lot_id, payload, apply)
