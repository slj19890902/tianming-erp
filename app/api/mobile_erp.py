from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import String, cast, or_, select
from sqlalchemy.orm import Session, selectinload

from app.api.deps import (
    PermissionChecker,
    customer_scope_ids,
    get_db,
    has_unrestricted_customer_access,
)
from app.core.time_contract import utc_naive_to_api
from app.models.customer import Customer
from app.models.product import Product
from app.models.user import User
from app.models.warehouse_inventory import (
    FinishedGoodsInventoryDetail,
    InventoryLot,
    InventoryPalletItem,
    InventoryReservation,
    SemiFinishedInventoryDetail,
    SemiFinishedLotAllowedProduct,
    WarehouseLocation,
)


router = APIRouter()
can_read_inventory = PermissionChecker("warehouse.view")
_BEIJING = ZoneInfo("Asia/Shanghai")


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "private, no-store"


def _visible_customer_ids(user: User, db: Session) -> set[int] | None:
    if has_unrestricted_customer_access(user, db):
        return None
    return customer_scope_ids(user, db)


def _escaped_like(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _number_text(value) -> str | None:
    if value is None:
        return None
    number = float(value)
    return str(int(number)) if number.is_integer() else f"{number:g}"


def _product_specification(product: Product) -> str:
    dimensions = [
        _number_text(product.length_mm),
        _number_text(product.width_mm),
        _number_text(product.height_mm),
    ]
    present = [value for value in dimensions if value is not None]
    return "×".join(present) + ("mm" if present else "")


def _product_payload(product: Product) -> dict:
    return {
        "id": product.id,
        "customer_id": product.customer_id,
        "customer_name": product.customer.name,
        "product_code": product.product_code,
        "customer_material_code": product.customer_material_code,
        "product_name": product.product_name,
        "specification": _product_specification(product),
        "box_style": product.box_style,
    }


def _product_query(db: Session, *, product_id: int | None = None):
    statement = (
        select(Product)
        .join(Customer, Customer.id == Product.customer_id)
        .options(selectinload(Product.customer))
        .where(
            Product.is_active.is_(True),
            Product.deleted_at.is_(None),
            Customer.is_active.is_(True),
        )
    )
    if product_id is not None:
        statement = statement.where(Product.id == product_id)
    return statement


def _require_visible_product(
    db: Session,
    *,
    product_id: int,
    visible_customer_ids: set[int] | None,
) -> Product:
    statement = _product_query(db, product_id=product_id)
    if visible_customer_ids is not None:
        statement = statement.where(Product.customer_id.in_(visible_customer_ids))
    product = db.scalar(statement)
    if product is None:
        # A direct ID must not reveal whether an inaccessible product exists.
        raise HTTPException(status_code=404, detail="产品不存在或当前账号无权查看")
    return product


def _lot_load_options():
    return (
        selectinload(InventoryLot.location).selectinload(
            WarehouseLocation.floor3_layout
        ),
        selectinload(InventoryLot.pallet_item).selectinload(
            InventoryPalletItem.pallet
        ),
    )


def _position_payload(lot: InventoryLot) -> dict:
    location = lot.location
    pallet_item = lot.pallet_item
    pallet = pallet_item.pallet if pallet_item is not None else None
    is_mapped = (
        location.warehouse_floor == 3 and location.floor3_layout is not None
    )
    is_unplaced = (
        location.placement_status == "unplaced"
        or location.is_temporary
        or (pallet is not None and pallet.needs_relocation)
    )
    if is_unplaced:
        map_status = "unplaced"
        map_status_text = "待归位，暂不能在地图定位"
    elif is_mapped:
        map_status = "mapped"
        map_status_text = "三楼地图已定位"
    else:
        map_status = "ledger_only"
        map_status_text = "已登记库位，尚未接入平面图"
    map_url = None
    if map_status == "mapped":
        map_url = (
            "/warehouse.html?embedded=1&readonly=1&tab=locations"
            f"&location_view=floor3&location_id={location.id}"
            f"&lot_id={lot.id}&source=mobile-product"
        )
    unit_label = "只" if lot.inventory_type == "finished" else "张"
    return {
        "lot_id": lot.id,
        "lot_number": lot.lot_number,
        "location_id": location.id,
        "location_code": location.location_code,
        "location_name": location.location_name,
        "floor": location.warehouse_floor,
        "area_code": location.area_code,
        "pallet_code": (
            pallet.pallet_code
            if pallet is not None and pallet.is_current
            else None
        ),
        "quantity_available": lot.quantity_available,
        "quantity_reserved": lot.quantity_reserved,
        "quantity_total": lot.quantity_available + lot.quantity_reserved,
        "unit": unit_label,
        "map_status": map_status,
        "map_status_text": map_status_text,
        "map_url": map_url,
        "last_updated_at": utc_naive_to_api(lot.last_movement_at),
    }


def _pending_pick_by_lot(db: Session, lot_ids: list[int]) -> dict[int, int]:
    if not lot_ids:
        return {}
    rows = db.scalars(
        select(InventoryReservation).where(
            InventoryReservation.inventory_lot_id.in_(lot_ids),
            InventoryReservation.reservation_type.in_(
                ["finished_order", "finished_surplus_delivery"]
            ),
            InventoryReservation.status.in_(["active", "partial"]),
        )
    ).all()
    totals: dict[int, int] = {}
    for row in rows:
        pending = max(
            row.reserved_stock_quantity
            - row.consumed_stock_quantity
            - row.released_stock_quantity,
            0,
        )
        totals[row.inventory_lot_id] = totals.get(row.inventory_lot_id, 0) + pending
    return totals


def _inventory_group(
    lots: list[InventoryLot],
    *,
    unit: str,
    pending_pick_by_lot: dict[int, int] | None = None,
) -> dict:
    pending_pick_by_lot = pending_pick_by_lot or {}
    positions = [_position_payload(lot) for lot in lots]
    positions.sort(
        key=lambda row: (
            row["floor"] is None,
            row["floor"] or 0,
            row["area_code"] or "",
            row["location_code"],
            row["lot_id"],
        )
    )
    return {
        "unit": unit,
        "quantity_total": sum(lot.quantity_available + lot.quantity_reserved for lot in lots),
        "quantity_available": sum(lot.quantity_available for lot in lots),
        "quantity_reserved": sum(lot.quantity_reserved for lot in lots),
        "quantity_pending_pick": sum(
            pending_pick_by_lot.get(lot.id, 0) for lot in lots
        ),
        "position_count": len(positions),
        "positions": positions,
    }


@router.get("/products")
def search_products(
    response: Response,
    q: str = Query(min_length=1, max_length=100),
    limit: int = Query(default=12, ge=1, le=30),
    db: Session = Depends(get_db),
    user: User = Depends(can_read_inventory),
) -> dict:
    _no_store(response)
    keyword = q.strip()
    if not keyword:
        raise HTTPException(status_code=422, detail="请输入存货编码、客户或产品名称")
    pattern = _escaped_like(keyword)
    statement = _product_query(db).where(
        or_(
            Product.product_code.ilike(pattern, escape="\\"),
            Product.customer_material_code.ilike(pattern, escape="\\"),
            Product.product_name.ilike(pattern, escape="\\"),
            Product.box_style.ilike(pattern, escape="\\"),
            Customer.name.ilike(pattern, escape="\\"),
            cast(Product.length_mm, String).ilike(pattern, escape="\\"),
            cast(Product.width_mm, String).ilike(pattern, escape="\\"),
            cast(Product.height_mm, String).ilike(pattern, escape="\\"),
        )
    )
    visible_customer_ids = _visible_customer_ids(user, db)
    if visible_customer_ids is not None:
        statement = statement.where(Product.customer_id.in_(visible_customer_ids))
    products = list(
        db.scalars(
            statement.order_by(
                Customer.name,
                Product.customer_material_code,
                Product.id,
            ).limit(limit)
        ).all()
    )
    return {
        "query": keyword,
        "count": len(products),
        "requires_selection": len(products) > 1,
        "auto_selected": False,
        "items": [_product_payload(product) for product in products],
        "as_of": datetime.now(_BEIJING).isoformat(timespec="seconds"),
    }


@router.get("/products/{product_id}/inventory")
def product_inventory(
    product_id: int,
    response: Response,
    db: Session = Depends(get_db),
    user: User = Depends(can_read_inventory),
) -> dict:
    _no_store(response)
    visible_customer_ids = _visible_customer_ids(user, db)
    product = _require_visible_product(
        db,
        product_id=product_id,
        visible_customer_ids=visible_customer_ids,
    )
    finished_lots = list(
        db.scalars(
            select(InventoryLot)
            .join(
                FinishedGoodsInventoryDetail,
                FinishedGoodsInventoryDetail.inventory_lot_id == InventoryLot.id,
            )
            .options(*_lot_load_options())
            .where(
                InventoryLot.inventory_type == "finished",
                InventoryLot.status == "active",
                or_(
                    InventoryLot.quantity_available > 0,
                    InventoryLot.quantity_reserved > 0,
                ),
                FinishedGoodsInventoryDetail.product_id == product.id,
                FinishedGoodsInventoryDetail.owner_customer_id == product.customer_id,
                FinishedGoodsInventoryDetail.is_general.is_(False),
                FinishedGoodsInventoryDetail.inventory_code_snapshot
                == product.product_code,
            )
            .order_by(InventoryLot.stock_date, InventoryLot.id)
        ).all()
    )
    semi_finished_lots = list(
        db.scalars(
            select(InventoryLot)
            .join(
                SemiFinishedInventoryDetail,
                SemiFinishedInventoryDetail.inventory_lot_id == InventoryLot.id,
            )
            .join(
                SemiFinishedLotAllowedProduct,
                SemiFinishedLotAllowedProduct.inventory_lot_id == InventoryLot.id,
            )
            .options(*_lot_load_options())
            .where(
                InventoryLot.inventory_type == "semi_finished",
                InventoryLot.status == "active",
                or_(
                    InventoryLot.quantity_available > 0,
                    InventoryLot.quantity_reserved > 0,
                ),
                SemiFinishedLotAllowedProduct.product_id == product.id,
                SemiFinishedInventoryDetail.owner_customer_id == product.customer_id,
            )
            .order_by(InventoryLot.stock_date, InventoryLot.id)
        ).all()
    )
    pending_pick = _pending_pick_by_lot(
        db, [lot.id for lot in finished_lots]
    )
    timestamps = [
        lot.last_movement_at for lot in [*finished_lots, *semi_finished_lots]
    ]
    last_updated_at = (
        utc_naive_to_api(max(timestamps)) if timestamps else None
    )
    return {
        "product": _product_payload(product),
        "inventory": {
            "finished": _inventory_group(
                finished_lots,
                unit="只",
                pending_pick_by_lot=pending_pick,
            ),
            "semi_finished": _inventory_group(
                semi_finished_lots,
                unit="张",
            ),
            "raw_material": {
                "state": "not_configured",
                "label": "原料仓尚未建立",
                "message": "当前不显示原料数量，避免把未知数据当成零库存。",
            },
        },
        "last_updated_at": last_updated_at,
        "as_of": datetime.now(_BEIJING).isoformat(timespec="seconds"),
        "read_only": True,
    }
