from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
import json
from typing import Iterable, Literal

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.time_contract import beijing_today, utc_naive_to_api
from app.models.customer import Customer
from app.models.master_data_object_version import MasterDataObjectVersion
from app.models.mold_tool import MoldLocationMovement, MoldTool
from app.models.order import Order, OrderItem
from app.models.printing_plate import PrintingPlate, PrintingPlateLocationMovement
from app.models.product import Product
from app.models.product_bom import SalesOrderItemBomComponent
from app.models.production import ProductionCompletion, ProductionTask
from app.models.stocktake import StocktakeItem, StocktakeOrder
from app.models.warehouse_inventory import InventoryLot, InventoryLotTransfer


_PLATE_FIELDS = (
    "printing_plate_1_id",
    "printing_plate_2_id",
    "printing_plate_3_id",
)


def _api_time(value: datetime | None) -> str | None:
    return utc_naive_to_api(value) if value is not None else None


def _idle_days(value: datetime | None, as_of: date) -> int | None:
    if value is None:
        return None
    return max(0, (as_of - value.date()).days)


def _timeline(events: Iterable[dict]) -> list[dict]:
    return sorted(
        events,
        key=lambda item: str(item.get("occurred_at") or ""),
        reverse=True,
    )


def build_inventory_lot_time_archives(
    db: Session,
    lots: Iterable[InventoryLot],
    *,
    as_of: date | None = None,
) -> dict[int, dict]:
    rows = list(lots)
    if not rows:
        return {}
    today = as_of or beijing_today()
    lot_ids = [row.id for row in rows]

    latest_transfer: dict[int, tuple[datetime, int, int]] = {}
    entered_current: dict[int, tuple[datetime, int]] = {}
    transfers = db.execute(
        select(
            InventoryLotTransfer.source_lot_id,
            InventoryLotTransfer.target_lot_id,
            InventoryLotTransfer.source_location_id,
            InventoryLotTransfer.target_location_id,
            InventoryLotTransfer.transferred_at,
        ).where(
            or_(
                InventoryLotTransfer.source_lot_id.in_(lot_ids),
                InventoryLotTransfer.target_lot_id.in_(lot_ids),
            )
        )
    ).all()
    for source_lot_id, target_lot_id, source_location_id, target_location_id, moved_at in transfers:
        for lot_id in (source_lot_id, target_lot_id):
            current = latest_transfer.get(lot_id)
            if current is None or moved_at > current[0]:
                latest_transfer[lot_id] = (moved_at, source_location_id, target_location_id)
        current_entry = entered_current.get(target_lot_id)
        if current_entry is None or moved_at > current_entry[0]:
            entered_current[target_lot_id] = (moved_at, target_location_id)

    latest_stocktake: dict[int, tuple[datetime, str, str]] = {}
    stocktakes = db.execute(
        select(
            StocktakeItem.inventory_lot_id,
            StocktakeOrder.submitted_at,
            StocktakeOrder.order_number,
            StocktakeOrder.status,
        )
        .join(StocktakeOrder, StocktakeOrder.id == StocktakeItem.order_id)
        .where(StocktakeItem.inventory_lot_id.in_(lot_ids))
    ).all()
    for lot_id, submitted_at, order_number, status in stocktakes:
        current = latest_stocktake.get(lot_id)
        if current is None or submitted_at > current[0]:
            latest_stocktake[lot_id] = (submitted_at, order_number, status)

    result: dict[int, dict] = {}
    for row in rows:
        transfer = latest_transfer.get(row.id)
        entry = entered_current.get(row.id)
        stocktake = latest_stocktake.get(row.id)
        formation_known = row.stock_date_accuracy != "unknown"
        formed_on = row.stock_date.isoformat() if formation_known and row.stock_date else None
        entered_at = entry[0] if entry is not None else row.created_at
        entered_basis = "location_transfer" if entry is not None else "lot_registration"
        age_days = (
            max(0, (today - row.stock_date).days)
            if formation_known and row.stock_date is not None
            else None
        )
        events: list[dict] = [
            {
                "event_type": "current_location_entry",
                "label": "进入当前位置",
                "occurred_at": _api_time(entered_at),
                "basis": entered_basis,
            },
            {
                "event_type": "registration",
                "label": "系统批次建档",
                "occurred_at": _api_time(row.created_at),
                "basis": "inventory_lot",
            },
        ]
        if formed_on is not None:
            events.append(
                {
                    "event_type": "inventory_formed",
                    "label": "形成库存",
                    "occurred_at": formed_on,
                    "basis": f"stock_date_{row.stock_date_accuracy}",
                }
            )
        if transfer is not None:
            events.append(
                {
                    "event_type": "location_transfer",
                    "label": "最近移位",
                    "occurred_at": _api_time(transfer[0]),
                    "basis": "inventory_lot_transfer",
                    "from_location_id": transfer[1],
                    "to_location_id": transfer[2],
                }
            )
        if stocktake is not None:
            events.append(
                {
                    "event_type": "stocktake",
                    "label": "最近盘点",
                    "occurred_at": _api_time(stocktake[0]),
                    "basis": "stocktake_order",
                    "order_number": stocktake[1],
                    "status": stocktake[2],
                }
            )
        result[row.id] = {
            "as_of": today.isoformat(),
            "formed_on": formed_on,
            "formation_accuracy": row.stock_date_accuracy,
            "formation_status": "已建立" if formation_known else "历史未建立/待确认",
            "entered_current_location_at": _api_time(entered_at),
            "entered_current_location_basis": entered_basis,
            "latest_location_transfer_at": _api_time(transfer[0]) if transfer else None,
            "latest_stocktake_at": _api_time(stocktake[0]) if stocktake else None,
            "age_days": age_days,
            "timeline": _timeline(events),
        }
    return result


def _current_binding_starts(
    db: Session,
    products: Iterable[Product],
    *,
    kind: Literal["mold", "plate"],
) -> dict[tuple[int, int], datetime]:
    rows = list(products)
    product_ids = sorted({row.id for row in rows})
    if not product_ids:
        return {}
    versions_by_product: dict[int, list[tuple[datetime, dict]]] = defaultdict(list)
    versions = db.execute(
        select(
            MasterDataObjectVersion.object_id,
            MasterDataObjectVersion.created_at,
            MasterDataObjectVersion.snapshot_json,
        )
        .where(
            MasterDataObjectVersion.object_type == "product",
            MasterDataObjectVersion.object_id.in_(product_ids),
        )
        .order_by(MasterDataObjectVersion.object_id, MasterDataObjectVersion.version)
    ).all()
    for product_id, created_at, snapshot_json in versions:
        try:
            snapshot = json.loads(snapshot_json)
        except (TypeError, ValueError):
            continue
        if isinstance(snapshot, dict):
            versions_by_product[product_id].append((created_at, snapshot))

    result: dict[tuple[int, int], datetime] = {}
    for product in rows:
        current_asset_ids = (
            [product.mold_tool_id]
            if kind == "mold"
            else [getattr(product, field) for field in _PLATE_FIELDS]
        )
        for asset_id in {int(value) for value in current_asset_ids if value is not None}:
            start: datetime | None = None
            linked_before = False
            for created_at, snapshot in versions_by_product.get(product.id, []):
                linked = (
                    snapshot.get("mold_tool_id") == asset_id
                    if kind == "mold"
                    else asset_id in {snapshot.get(field) for field in _PLATE_FIELDS}
                )
                if linked and not linked_before:
                    start = created_at
                elif not linked:
                    start = None
                linked_before = linked
            if linked_before and start is not None:
                result[(product.id, asset_id)] = start
    return result


def _usage_payload(
    *,
    occurred_at: date | datetime,
    order_id: int,
    order_number: str,
    customer_id: int,
    customer_name: str | None,
    product_id: int | None,
    product_code: str | None,
    product_name: str | None,
    task_id: int | None = None,
    basis: str,
) -> dict:
    return {
        "occurred_at": (
            occurred_at.isoformat()
            if isinstance(occurred_at, date) and not isinstance(occurred_at, datetime)
            else _api_time(occurred_at)
        ),
        "order_id": order_id,
        "order_number": order_number,
        "customer_id": customer_id,
        "customer_name": customer_name,
        "product_id": product_id,
        "product_code": product_code,
        "product_name": product_name,
        "production_task_id": task_id,
        "basis": basis,
    }


def _latest(current: dict | None, candidate: dict) -> dict:
    if current is None or str(candidate["occurred_at"]) > str(current["occurred_at"]):
        return candidate
    return current


def _asset_time_archives(
    db: Session,
    assets: Iterable[MoldTool | PrintingPlate],
    *,
    kind: Literal["mold", "plate"],
    products_by_asset: dict[int, list[Product]],
    allowed_customer_ids: set[int] | None,
    as_of: date | None,
) -> dict[int, dict]:
    rows = list(assets)
    if not rows:
        return {}
    today = as_of or beijing_today()
    asset_ids = {row.id for row in rows}
    all_products = {
        product.id: product
        for products in products_by_asset.values()
        for product in products
    }
    asset_ids_by_product: dict[int, list[int]] = defaultdict(list)
    for asset_id, products in products_by_asset.items():
        for product in products:
            asset_ids_by_product[product.id].append(asset_id)
    binding_starts = _current_binding_starts(
        db, all_products.values(), kind=kind
    )
    product_ids = sorted(all_products)

    latest_orders: dict[int, dict] = {}
    if product_ids and binding_starts:
        order_query = (
            select(
                OrderItem.product_id,
                Order.order_date,
                Order.created_at,
                Order.id,
                Order.order_number,
                Order.customer_id,
                Customer.name,
                OrderItem.snapshot_product_code,
                OrderItem.snapshot_product_name,
            )
            .join(Order, Order.id == OrderItem.order_id)
            .join(Customer, Customer.id == Order.customer_id)
            .where(OrderItem.product_id.in_(product_ids))
        )
        if allowed_customer_ids is not None:
            order_query = order_query.where(Order.customer_id.in_(allowed_customer_ids))
        for product_id, order_date, created_at, order_id, order_number, customer_id, customer_name, product_code, product_name in db.execute(order_query):
            for asset_id in asset_ids_by_product.get(product_id, []):
                start = binding_starts.get((product_id, asset_id))
                if start is None or created_at < start:
                    continue
                payload = _usage_payload(
                    occurred_at=order_date,
                    order_id=order_id,
                    order_number=order_number,
                    customer_id=customer_id,
                    customer_name=customer_name,
                    product_id=product_id,
                    product_code=product_code,
                    product_name=product_name,
                    basis="versioned_product_binding_and_order",
                )
                latest_orders[asset_id] = _latest(latest_orders.get(asset_id), payload)

    latest_production: dict[int, dict] = {}
    if product_ids and binding_starts:
        production_query = (
            select(
                OrderItem.product_id,
                ProductionTask.id,
                ProductionTask.created_at,
                ProductionCompletion.completed_at,
                Order.id,
                Order.order_number,
                Order.customer_id,
                Customer.name,
                OrderItem.snapshot_product_code,
                OrderItem.snapshot_product_name,
            )
            .join(ProductionTask, ProductionTask.order_item_id == OrderItem.id)
            .join(ProductionCompletion, ProductionCompletion.task_id == ProductionTask.id)
            .join(Order, Order.id == OrderItem.order_id)
            .join(Customer, Customer.id == Order.customer_id)
            .where(
                OrderItem.product_id.in_(product_ids),
                ProductionCompletion.status == "posted",
            )
        )
        if allowed_customer_ids is not None:
            production_query = production_query.where(Order.customer_id.in_(allowed_customer_ids))
        for product_id, task_id, task_created_at, completed_at, order_id, order_number, customer_id, customer_name, product_code, product_name in db.execute(production_query):
            for asset_id in asset_ids_by_product.get(product_id, []):
                start = binding_starts.get((product_id, asset_id))
                if start is None or task_created_at < start:
                    continue
                payload = _usage_payload(
                    occurred_at=completed_at,
                    order_id=order_id,
                    order_number=order_number,
                    customer_id=customer_id,
                    customer_name=customer_name,
                    product_id=product_id,
                    product_code=product_code,
                    product_name=product_name,
                    task_id=task_id,
                    basis="versioned_product_binding_and_posted_completion",
                )
                latest_production[asset_id] = _latest(latest_production.get(asset_id), payload)

    if kind == "mold":
        component_query = (
            select(
                SalesOrderItemBomComponent.snapshot_mold_tool_id,
                ProductionTask.id,
                ProductionCompletion.completed_at,
                Order.id,
                Order.order_number,
                Order.customer_id,
                Customer.name,
                SalesOrderItemBomComponent.component_product_id,
                SalesOrderItemBomComponent.snapshot_component_product_code,
                SalesOrderItemBomComponent.snapshot_component_product_name,
                Order.order_date,
            )
            .join(
                ProductionTask,
                ProductionTask.sales_order_item_bom_component_id
                == SalesOrderItemBomComponent.id,
            )
            .join(ProductionCompletion, ProductionCompletion.task_id == ProductionTask.id)
            .join(OrderItem, OrderItem.id == SalesOrderItemBomComponent.sales_order_item_id)
            .join(Order, Order.id == OrderItem.order_id)
            .join(Customer, Customer.id == Order.customer_id)
            .where(
                SalesOrderItemBomComponent.snapshot_mold_tool_id.in_(asset_ids),
                ProductionCompletion.status == "posted",
            )
        )
        if allowed_customer_ids is not None:
            component_query = component_query.where(Order.customer_id.in_(allowed_customer_ids))
        for mold_id, task_id, completed_at, order_id, order_number, customer_id, customer_name, product_id, product_code, product_name, order_date in db.execute(component_query):
            production_payload = _usage_payload(
                occurred_at=completed_at,
                order_id=order_id,
                order_number=order_number,
                customer_id=customer_id,
                customer_name=customer_name,
                product_id=product_id,
                product_code=product_code,
                product_name=product_name,
                task_id=task_id,
                basis="bom_mold_snapshot_and_posted_completion",
            )
            latest_production[mold_id] = _latest(latest_production.get(mold_id), production_payload)
            order_payload = _usage_payload(
                occurred_at=order_date,
                order_id=order_id,
                order_number=order_number,
                customer_id=customer_id,
                customer_name=customer_name,
                product_id=product_id,
                product_code=product_code,
                product_name=product_name,
                basis="bom_mold_snapshot_and_order",
            )
            latest_orders[mold_id] = _latest(latest_orders.get(mold_id), order_payload)
    else:
        plate_by_code = {row.plate_code: row.id for row in rows}
        plate_query = (
            select(
                ProductionTask.id,
                ProductionTask.printing_plate_codes_snapshot,
                ProductionCompletion.completed_at,
                Order.id,
                Order.order_number,
                Order.customer_id,
                Customer.name,
                OrderItem.product_id,
                OrderItem.snapshot_product_code,
                OrderItem.snapshot_product_name,
            )
            .join(ProductionCompletion, ProductionCompletion.task_id == ProductionTask.id)
            .join(OrderItem, OrderItem.id == ProductionTask.order_item_id)
            .join(Order, Order.id == OrderItem.order_id)
            .join(Customer, Customer.id == Order.customer_id)
            .where(
                ProductionCompletion.status == "posted",
                ProductionTask.printing_plate_codes_snapshot != "[]",
            )
        )
        if allowed_customer_ids is not None:
            plate_query = plate_query.where(Order.customer_id.in_(allowed_customer_ids))
        for task_id, codes_json, completed_at, order_id, order_number, customer_id, customer_name, product_id, product_code, product_name in db.execute(plate_query):
            try:
                codes = json.loads(codes_json or "[]")
            except (TypeError, ValueError):
                continue
            for code in codes if isinstance(codes, list) else []:
                plate_id = plate_by_code.get(str(code))
                if plate_id is None:
                    continue
                payload = _usage_payload(
                    occurred_at=completed_at,
                    order_id=order_id,
                    order_number=order_number,
                    customer_id=customer_id,
                    customer_name=customer_name,
                    product_id=product_id,
                    product_code=product_code,
                    product_name=product_name,
                    task_id=task_id,
                    basis="production_task_plate_snapshot_and_posted_completion",
                )
                latest_production[plate_id] = _latest(latest_production.get(plate_id), payload)

    movement_model = MoldLocationMovement if kind == "mold" else PrintingPlateLocationMovement
    foreign_key = (
        movement_model.mold_tool_id
        if kind == "mold"
        else movement_model.printing_plate_id
    )
    latest_moves = {
        asset_id: moved_at
        for asset_id, moved_at in db.execute(
            select(foreign_key, func.max(movement_model.moved_at))
            .where(foreign_key.in_(asset_ids))
            .group_by(foreign_key)
        )
    }

    result: dict[int, dict] = {}
    for row in rows:
        order_use = latest_orders.get(row.id)
        production_use = latest_production.get(row.id)
        latest_move = latest_moves.get(row.id)
        entered_current_location_at = (
            latest_move or row.last_location_confirmed_at or row.created_at
        )
        entered_current_location_basis = (
            f"{kind}_location_movement"
            if latest_move is not None
            else (
                f"{kind}_location_confirmation"
                if row.last_location_confirmed_at is not None
                else f"{kind}_registration"
            )
        )
        events = [
            {
                "event_type": "registration",
                "label": "资产登记",
                "occurred_at": _api_time(row.created_at),
                "basis": f"{kind}_master",
            }
        ]
        if latest_move is not None:
            events.append(
                {
                    "event_type": "location_move",
                    "label": "最近移动",
                    "occurred_at": _api_time(latest_move),
                    "basis": f"{kind}_location_movement",
                }
            )
        if order_use is not None:
            events.append(
                {
                    "event_type": "customer_order_use",
                    "label": "客户下单使用",
                    **order_use,
                }
            )
        if production_use is not None:
            events.append(
                {
                    "event_type": "actual_production_use",
                    "label": "实际生产使用",
                    **production_use,
                }
            )
        result[row.id] = {
            "as_of": today.isoformat(),
            "registered_at": _api_time(row.created_at),
            "entered_current_location_at": _api_time(
                entered_current_location_at
            ),
            "entered_current_location_basis": entered_current_location_basis,
            "latest_location_move_at": _api_time(latest_move),
            "latest_customer_order_use": order_use,
            "latest_actual_production_use": production_use,
            "idle_days": _idle_days(
                None
                if production_use is None
                else datetime.fromisoformat(
                    str(production_use["occurred_at"]).replace("Z", "+00:00")
                ).replace(tzinfo=None),
                today,
            ),
            "usage_history_status": (
                "已建立"
                if order_use is not None or production_use is not None
                else "历史未建立/待确认"
            ),
            "timeline": _timeline(events),
        }
    return result


def build_mold_time_archives(
    db: Session,
    molds: Iterable[MoldTool],
    *,
    products_by_mold: dict[int, list[Product]],
    allowed_customer_ids: set[int] | None,
    as_of: date | None = None,
) -> dict[int, dict]:
    return _asset_time_archives(
        db,
        molds,
        kind="mold",
        products_by_asset=products_by_mold,
        allowed_customer_ids=allowed_customer_ids,
        as_of=as_of,
    )


def build_printing_plate_time_archives(
    db: Session,
    plates: Iterable[PrintingPlate],
    *,
    products_by_plate: dict[int, list[Product]],
    allowed_customer_ids: set[int] | None,
    as_of: date | None = None,
) -> dict[int, dict]:
    return _asset_time_archives(
        db,
        plates,
        kind="plate",
        products_by_asset=products_by_plate,
        allowed_customer_ids=allowed_customer_ids,
        as_of=as_of,
    )
