from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
import re
from typing import Literal
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import String, and_, case, cast, exists, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.api.deps import (
    PermissionChecker,
    customer_scope_ids,
    get_db,
    has_permission,
    has_unrestricted_customer_access,
)
from app.api.incoming import _received_rows
from app.core.time_contract import (
    beijing_date_bounds_utc_naive,
    beijing_today,
    utc_naive_to_api,
)
from app.models.customer import Customer
from app.models.order import OrderItem
from app.models.product import Product
from app.models.user import User
from app.services.production_workflow import list_production_tasks
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
can_read_orders = PermissionChecker("orders.view")
_BEIJING = ZoneInfo("Asia/Shanghai")


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "private, no-store"


def _visible_customer_ids(user: User, db: Session) -> set[int] | None:
    if has_unrestricted_customer_access(user, db):
        return None
    return customer_scope_ids(user, db)


def _production_period_bounds(
    period: Literal["today", "3d", "7d", "custom"],
    *,
    date_from: date | None,
    date_to: date | None,
) -> tuple[date, date, datetime, datetime]:
    today = beijing_today()
    if period == "custom":
        if date_from is None or date_to is None:
            raise HTTPException(status_code=422, detail="自定义日期必须同时填写开始和结束日期")
        if date_from > date_to:
            raise HTTPException(status_code=422, detail="开始日期不能晚于结束日期")
        start_date, end_date = date_from, date_to
    else:
        days = {"today": 1, "3d": 3, "7d": 7}[period]
        start_date, end_date = today - timedelta(days=days - 1), today
    start_utc, _ = beijing_date_bounds_utc_naive(start_date)
    _, end_utc = beijing_date_bounds_utc_naive(end_date)
    return start_date, end_date, start_utc, end_utc


def _task_status_text(status: str) -> str:
    return {
        "waiting_material": "材料未齐",
        "pending": "可以生产",
        "completed": "已经完工",
        "not_required": "无需生产",
    }.get(status, status or "状态未知")


def _safe_production_task(task: dict, *, drawing_path: str | None) -> dict:
    """Expose workshop facts only; supplier material codes and prices stay private."""

    available_input = max(int(task.get("available_material_input_quantity") or 0), 0)
    output_factor = max(int(task.get("output_factor") or 1), 1)
    pieces_per_box = max(int(task.get("pieces_per_box") or 1), 1)
    return {
        "task_id": task["id"],
        "status": task["status"],
        "status_text": _task_status_text(task["status"]),
        "order_number": task.get("order_number"),
        "item_order_number": task.get("item_order_number"),
        "product_code": task.get("product_code"),
        "product_name": task.get("product_name"),
        "carton_specification": task.get("specification"),
        "flute_type": task.get("flute"),
        "order_quantity": task.get("ordered_quantity"),
        "received_material_quantity": task.get("material_received_quantity"),
        "current_producible_quantity": available_input * output_factor // pieces_per_box,
        "planned_output_quantity": task.get("planned_output_quantity"),
        "cutting_mode": task.get("special_process"),
        "production_process": task.get("production_process"),
        "production_notes": task.get("production_notes"),
        "mold_name": task.get("mold_name"),
        "mold_location": task.get("mold_location"),
        "drawing_path": drawing_path,
        "is_component_task": task.get("is_component_task") is True,
    }


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


def _product_payload(product: Product, *, inventory_summary: dict | None = None) -> dict:
    payload = {
        "id": product.id,
        "customer_id": product.customer_id,
        "customer_name": product.customer.name,
        "customer_code": product.customer.customer_code,
        "product_code": product.product_code,
        "customer_material_code": product.customer_material_code,
        "product_name": product.product_name,
        "specification": _product_specification(product),
        "box_style": product.box_style,
    }
    if inventory_summary is not None:
        payload["inventory_summary"] = inventory_summary
    return payload


def _dimension_search_condition(keyword: str):
    normalized = re.sub(r"\s+", "", keyword).removesuffix("mm").removesuffix("MM")
    parts = re.split(r"[xX×*]", normalized)
    if len(parts) not in {2, 3} or any(not part for part in parts):
        return None
    try:
        dimensions = [Decimal(part) for part in parts]
    except InvalidOperation:
        return None
    fields = [Product.length_mm, Product.width_mm, Product.height_mm]
    return and_(*(fields[index] == value for index, value in enumerate(dimensions)))


def _product_search_condition(keyword: str):
    pattern = _escaped_like(keyword)
    conditions = [
        Product.product_code.ilike(pattern, escape="\\"),
        Product.customer_material_code.ilike(pattern, escape="\\"),
        Product.product_name.ilike(pattern, escape="\\"),
        Product.box_style.ilike(pattern, escape="\\"),
        Customer.name.ilike(pattern, escape="\\"),
        Customer.customer_code.ilike(pattern, escape="\\"),
        cast(Product.length_mm, String).ilike(pattern, escape="\\"),
        cast(Product.width_mm, String).ilike(pattern, escape="\\"),
        cast(Product.height_mm, String).ilike(pattern, escape="\\"),
    ]
    dimension_condition = _dimension_search_condition(keyword)
    if dimension_condition is not None:
        conditions.append(dimension_condition)
    return or_(*conditions)


def _product_has_stock_condition():
    finished = exists(
        select(1)
        .select_from(InventoryLot)
        .join(
            FinishedGoodsInventoryDetail,
            FinishedGoodsInventoryDetail.inventory_lot_id == InventoryLot.id,
        )
        .where(
            InventoryLot.inventory_type == "finished",
            InventoryLot.status == "active",
            or_(
                InventoryLot.quantity_available > 0,
                InventoryLot.quantity_reserved > 0,
            ),
            FinishedGoodsInventoryDetail.product_id == Product.id,
            FinishedGoodsInventoryDetail.owner_customer_id == Product.customer_id,
            FinishedGoodsInventoryDetail.is_general.is_(False),
            FinishedGoodsInventoryDetail.inventory_code_snapshot
            == Product.product_code,
        )
        .correlate(Product)
    )
    semi_finished = exists(
        select(1)
        .select_from(InventoryLot)
        .join(
            SemiFinishedInventoryDetail,
            SemiFinishedInventoryDetail.inventory_lot_id == InventoryLot.id,
        )
        .join(
            SemiFinishedLotAllowedProduct,
            SemiFinishedLotAllowedProduct.inventory_lot_id == InventoryLot.id,
        )
        .where(
            InventoryLot.inventory_type == "semi_finished",
            InventoryLot.status == "active",
            or_(
                InventoryLot.quantity_available > 0,
                InventoryLot.quantity_reserved > 0,
            ),
            SemiFinishedLotAllowedProduct.product_id == Product.id,
            SemiFinishedInventoryDetail.owner_customer_id == Product.customer_id,
        )
        .correlate(Product)
    )
    return or_(finished, semi_finished)


def _empty_product_inventory_summary() -> dict:
    return {
        "has_stock": False,
        "position_count": 0,
        "finished": {
            "unit": "只",
            "quantity_total": 0,
            "quantity_available": 0,
            "quantity_reserved": 0,
            "quantity_pending_pick": 0,
        },
        "semi_finished": {
            "unit": "张",
            "quantity_total": 0,
            "quantity_available": 0,
            "quantity_reserved": 0,
            "quantity_pending_pick": 0,
        },
    }


def _product_inventory_summaries(
    db: Session,
    products: list[Product],
) -> dict[int, dict]:
    product_ids = [product.id for product in products]
    summaries = {
        product.id: _empty_product_inventory_summary() for product in products
    }
    if not product_ids:
        return summaries

    finished_rows = db.execute(
        select(
            FinishedGoodsInventoryDetail.product_id,
            InventoryLot.id,
            InventoryLot.warehouse_location_id,
            InventoryLot.quantity_available,
            InventoryLot.quantity_reserved,
        )
        .select_from(InventoryLot)
        .join(
            FinishedGoodsInventoryDetail,
            FinishedGoodsInventoryDetail.inventory_lot_id == InventoryLot.id,
        )
        .join(Product, Product.id == FinishedGoodsInventoryDetail.product_id)
        .where(
            FinishedGoodsInventoryDetail.product_id.in_(product_ids),
            FinishedGoodsInventoryDetail.owner_customer_id == Product.customer_id,
            FinishedGoodsInventoryDetail.is_general.is_(False),
            FinishedGoodsInventoryDetail.inventory_code_snapshot == Product.product_code,
            InventoryLot.inventory_type == "finished",
            InventoryLot.status == "active",
            or_(
                InventoryLot.quantity_available > 0,
                InventoryLot.quantity_reserved > 0,
            ),
        )
    ).all()
    pending_pick_by_lot = _pending_pick_by_lot(
        db, [int(row.id) for row in finished_rows]
    )
    position_ids: dict[int, set[int]] = {product.id: set() for product in products}
    for row in finished_rows:
        summary = summaries[int(row.product_id)]
        group = summary["finished"]
        available = int(row.quantity_available or 0)
        reserved = int(row.quantity_reserved or 0)
        group["quantity_available"] += available
        group["quantity_reserved"] += reserved
        group["quantity_total"] += available + reserved
        group["quantity_pending_pick"] += pending_pick_by_lot.get(int(row.id), 0)
        position_ids[int(row.product_id)].add(int(row.warehouse_location_id))

    semi_rows = db.execute(
        select(
            SemiFinishedLotAllowedProduct.product_id,
            InventoryLot.warehouse_location_id,
            InventoryLot.quantity_available,
            InventoryLot.quantity_reserved,
        )
        .select_from(InventoryLot)
        .join(
            SemiFinishedInventoryDetail,
            SemiFinishedInventoryDetail.inventory_lot_id == InventoryLot.id,
        )
        .join(
            SemiFinishedLotAllowedProduct,
            SemiFinishedLotAllowedProduct.inventory_lot_id == InventoryLot.id,
        )
        .join(Product, Product.id == SemiFinishedLotAllowedProduct.product_id)
        .where(
            SemiFinishedLotAllowedProduct.product_id.in_(product_ids),
            SemiFinishedInventoryDetail.owner_customer_id == Product.customer_id,
            InventoryLot.inventory_type == "semi_finished",
            InventoryLot.status == "active",
            or_(
                InventoryLot.quantity_available > 0,
                InventoryLot.quantity_reserved > 0,
            ),
        )
    ).all()
    for row in semi_rows:
        summary = summaries[int(row.product_id)]
        group = summary["semi_finished"]
        available = int(row.quantity_available or 0)
        reserved = int(row.quantity_reserved or 0)
        group["quantity_available"] += available
        group["quantity_reserved"] += reserved
        group["quantity_total"] += available + reserved
        position_ids[int(row.product_id)].add(int(row.warehouse_location_id))

    for product_id, summary in summaries.items():
        summary["position_count"] = len(position_ids[product_id])
        summary["has_stock"] = any(
            summary[group]["quantity_total"] > 0
            for group in ("finished", "semi_finished")
        )
    return summaries


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


@router.get("/production/recent")
def recent_production_materials(
    response: Response,
    period: Literal["today", "3d", "7d", "custom"] = Query(default="3d"),
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    db: Session = Depends(get_db),
    user: User = Depends(can_read_orders),
) -> dict:
    """Return formally received material and linked production facts, read only."""

    _no_store(response)
    if not has_permission(user, "incoming.view"):
        raise HTTPException(status_code=403, detail="当前账号没有查看来料资料的权限")
    start_date, end_date, start_utc, end_utc = _production_period_bounds(
        period,
        date_from=date_from,
        date_to=date_to,
    )
    received_rows = [
        row
        for row in _received_rows(
            db,
            user=user,
            received_since=start_utc,
        )
        if row.get("material_received_at") is not None
        and row["material_received_at"] < end_utc
        and row.get("receipt_status", "posted") == "posted"
    ]
    task_rows = list_production_tasks(
        db,
        allowed_customer_ids=_visible_customer_ids(user, db),
    )
    tasks_by_item: dict[int, list[dict]] = {}
    for task in task_rows:
        tasks_by_item.setdefault(int(task["order_item_id"]), []).append(task)
    received_order_item_ids = {
        int(row["order_item_id"])
        for row in received_rows
        if row.get("order_item_id") is not None
    }
    direction_notes = {
        item_id: note
        for item_id, note in db.execute(
            select(OrderItem.id, OrderItem.snapshot_report_notes).where(
                OrderItem.id.in_(received_order_item_ids)
            )
        ).all()
        if (note or "").strip()
    }

    items: list[dict] = []
    for row in received_rows:
        order_item_id = row.get("order_item_id")
        linked_tasks = tasks_by_item.get(int(order_item_id), []) if order_item_id else []
        board_direction_note = row.get("requisition_remark")
        if not board_direction_note and order_item_id:
            board_direction_note = direction_notes.get(int(order_item_id))
        received_product_code = (row.get("product_code") or "").strip()
        exact_tasks = [
            task
            for task in linked_tasks
            if (task.get("product_code") or "").strip() == received_product_code
        ]
        if exact_tasks:
            linked_tasks = exact_tasks
        remaining = max(int(row.get("remaining_quantity") or 0), 0)
        material_state = "材料未齐" if remaining > 0 else "材料已齐"
        items.append(
            {
                "receipt_item_id": row.get("receipt_item_id"),
                "receipt_number": row.get("receipt_number"),
                "received_at": utc_naive_to_api(row["material_received_at"]),
                "received_by_name": row.get("received_by_name") or "未记录",
                "supplier_document_number": row.get("supplier_order_number"),
                "customer_name": row.get("customer_name"),
                "order_number": row.get("display_order_number") or row.get("order_number"),
                "customer_po": row.get("customer_po"),
                "product_code": row.get("product_code"),
                "product_name": row.get("product_name"),
                "carton_specification": row.get("specification"),
                "board_length_mm": _number_text(row.get("cardboard_len")),
                "board_width_mm": _number_text(row.get("cardboard_width")),
                "flute_type": row.get("flute_type"),
                "crease_type": row.get("snapshot_crease_type"),
                "crease_values_mm": [
                    value
                    for value in (
                        row.get("snapshot_crease_left_mm"),
                        row.get("snapshot_crease_middle_mm"),
                        row.get("snapshot_crease_right_mm"),
                    )
                    if value is not None
                ],
                "received_quantity": int(row.get("received_quantity_this_time") or 0),
                "cumulative_received_quantity": int(
                    row.get("cumulative_received_quantity") or 0
                ),
                "remaining_quantity": remaining,
                "difference_quantity": int(row.get("variance_quantity") or 0),
                "material_state": material_state,
                "board_direction_note": board_direction_note,
                "delivery_date": (
                    row["delivery_date"].isoformat()
                    if row.get("delivery_date")
                    else None
                ),
                "drawing_path": row.get("drawing_path"),
                "drawing_is_pdf": row.get("drawing_is_pdf") is True,
                "production_tasks": [
                    _safe_production_task(
                        task,
                        drawing_path=row.get("drawing_path"),
                    )
                    for task in linked_tasks
                ],
            }
        )
    return {
        "period": period,
        "date_from": start_date.isoformat(),
        "date_to": end_date.isoformat(),
        "count": len(items),
        "items": items,
        "as_of": datetime.now(_BEIJING).isoformat(timespec="seconds"),
        "read_only": True,
    }


@router.get("/products")
def search_products(
    response: Response,
    q: str = Query(min_length=1, max_length=100),
    limit: int = Query(default=12, ge=1, le=30),
    include_zero: bool = Query(default=False),
    db: Session = Depends(get_db),
    user: User = Depends(can_read_inventory),
) -> dict:
    _no_store(response)
    keyword = q.strip()
    if not keyword:
        raise HTTPException(status_code=422, detail="请输入存货编码、客户或产品名称")
    if include_zero and user.role != "admin":
        raise HTTPException(status_code=403, detail="只有管理员可以查询零库存产品")
    statement = _product_query(db).where(_product_search_condition(keyword))
    visible_customer_ids = _visible_customer_ids(user, db)
    if visible_customer_ids is not None:
        statement = statement.where(Product.customer_id.in_(visible_customer_ids))
    matched_product_count = int(
        db.scalar(
            select(func.count()).select_from(statement.order_by(None).subquery())
        )
        or 0
    )
    stocked_statement = statement.where(_product_has_stock_condition())
    stocked_product_count = int(
        db.scalar(
            select(func.count()).select_from(
                stocked_statement.order_by(None).subquery()
            )
        )
        or 0
    )
    result_statement = statement if include_zero else stocked_statement
    normalized_keyword = keyword.casefold()
    exact_rank = case(
        (
            or_(
                func.lower(Product.customer_material_code) == normalized_keyword,
                func.lower(Product.product_code) == normalized_keyword,
            ),
            0,
        ),
        (func.lower(Customer.customer_code) == normalized_keyword, 1),
        (func.lower(Product.product_name) == normalized_keyword, 2),
        else_=3,
    )
    products = list(
        db.scalars(
            result_statement.order_by(
                exact_rank,
                Customer.name,
                Product.customer_material_code,
                Product.id,
            ).limit(limit)
        ).all()
    )
    summaries = _product_inventory_summaries(db, products)
    return {
        "query": keyword,
        "count": len(products),
        "matched_product_count": matched_product_count,
        "zero_stock_match_count": max(
            matched_product_count - stocked_product_count, 0
        ),
        "include_zero": include_zero,
        "include_zero_allowed": user.role == "admin",
        "requires_selection": len(products) > 1,
        "auto_selected": False,
        "items": [
            _product_payload(
                product,
                inventory_summary=summaries[product.id],
            )
            for product in products
        ],
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
    finished_group = _inventory_group(
        finished_lots,
        unit="只",
        pending_pick_by_lot=pending_pick,
    )
    semi_finished_group = _inventory_group(
        semi_finished_lots,
        unit="张",
    )
    mapped_positions = [
        position
        for position in [
            *finished_group["positions"],
            *semi_finished_group["positions"],
        ]
        if position["map_status"] == "mapped"
    ]
    map_url = None
    if mapped_positions:
        map_url = "/warehouse.html?" + urlencode(
            {
                "embedded": 1,
                "readonly": 1,
                "tab": "locations",
                "location_view": "floor3",
                "customer_id": product.customer_id,
                "keyword": product.customer_material_code or product.product_code,
                "source": "mobile-product-all",
            }
        )
    return {
        "product": _product_payload(product),
        "inventory": {
            "finished": finished_group,
            "semi_finished": semi_finished_group,
            "raw_material": {
                "state": "not_configured",
                "label": "原料仓尚未建立",
                "message": "当前不显示原料数量，避免把未知数据当成零库存。",
            },
        },
        "map_url": map_url,
        "mapped_position_count": len(mapped_positions),
        "last_updated_at": last_updated_at,
        "as_of": datetime.now(_BEIJING).isoformat(timespec="seconds"),
        "read_only": True,
    }
