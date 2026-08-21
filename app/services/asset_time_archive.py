from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
import json
from typing import Iterable, Literal

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.core.time_contract import beijing_naive_to_api, beijing_today, utc_naive_to_api
from app.models.customer import Customer
from app.models.audit import OperationLog
from app.models.master_data_object_version import MasterDataObjectVersion
from app.models.mold_tool import MoldLocationMovement, MoldRepairEvent, MoldTool
from app.models.order import Order, OrderItem
from app.models.printing_plate import PrintingPlate, PrintingPlateLocationMovement
from app.models.product import Product
from app.models.product_bom import SalesOrderItemBomComponent
from app.models.production import ProductionCompletion, ProductionTask
from app.models.stocktake import StocktakeItem, StocktakeOrder
from app.models.user import User
from app.models.warehouse_inventory import (
    InventoryLocationMovement,
    InventoryLot,
    InventoryLotTransfer,
    InventoryMovement,
    WarehouseArea,
    WarehouseLocation,
)
from app.services.warehouse_location_address import employee_location_name


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
    def instant(item: dict) -> datetime:
        value = str(item.get("occurred_at") or "")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return datetime.min.replace(tzinfo=timezone.utc)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    return sorted(
        events,
        key=instant,
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


def _location_labels(db: Session, location_ids: set[int]) -> dict[int, str]:
    if not location_ids:
        return {}
    return {
        row.id: employee_location_name(row)
        for row in db.scalars(
            select(WarehouseLocation)
            .options(
                selectinload(WarehouseLocation.address_area).selectinload(
                    WarehouseArea.floor
                )
            )
            .where(WarehouseLocation.id.in_(location_ids))
        ).all()
    }


def _operator_names(db: Session, operator_ids: set[int]) -> dict[int, str]:
    if not operator_ids:
        return {}
    return {
        row.id: row.display_name or row.real_name or row.username
        for row in db.scalars(select(User).where(User.id.in_(operator_ids))).all()
    }


def _transfer_movement_key(transfer_key: str, side: str) -> str:
    raw = f"location-transfer:{transfer_key}:{side}"
    if len(raw) <= 100:
        return raw
    digest = sha256(raw.encode("utf-8")).hexdigest()[:24]
    return f"{raw[:75]}:{digest}"


def build_inventory_lot_detail_timeline(
    db: Session,
    lot: InventoryLot,
    *,
    include_sensitive_details: bool = False,
) -> list[dict]:
    """Build a complete, read-only timeline from persisted inventory facts.

    ``InventoryLotTransfer`` is authoritative for location movement.  The
    mirrored ``InventoryMovement.location_transfer`` rows are therefore not
    repeated.  A split lot keeps direct source/target facts visible without
    guessing older lineage that is not attached to the selected lot.
    """

    events: list[dict] = [
        {
            "event_type": "lot_registration",
            "label": "库存批次建档",
            "occurred_at": _api_time(lot.created_at),
            "basis": "inventory_lot",
            "operator_id": lot.created_by,
        }
    ]
    if lot.stock_date_accuracy != "unknown":
        events.append(
            {
                "event_type": "inventory_formed",
                "label": "形成库存",
                "occurred_at": lot.stock_date.isoformat(),
                "basis": f"stock_date_{lot.stock_date_accuracy}",
                "stock_date_accuracy": lot.stock_date_accuracy,
            }
        )

    transfers = db.execute(
        select(InventoryLotTransfer).where(
            or_(
                InventoryLotTransfer.source_lot_id == lot.id,
                InventoryLotTransfer.target_lot_id == lot.id,
            )
        )
    ).scalars().all()
    location_ids = {
        location_id
        for transfer in transfers
        for location_id in (
            transfer.source_location_id,
            transfer.target_location_id,
        )
    }
    location_labels = _location_labels(db, location_ids)
    transfer_movement_keys: set[str] = set()
    for transfer in transfers:
        if transfer.source_lot_id == lot.id and transfer.target_lot_id == lot.id:
            direction = "移位"
            direction_code = "move"
        elif transfer.target_lot_id == lot.id:
            direction = "转入"
            direction_code = "in"
        else:
            direction = "转出"
            direction_code = "out"
        transfer_movement_keys.update(
            {
                _transfer_movement_key(transfer.idempotency_key, "source"),
                _transfer_movement_key(transfer.idempotency_key, "target"),
            }
        )
        events.append(
            {
                "event_type": "location_transfer",
                "label": f"库存位置{direction}" if direction != "移位" else "库存位置移位",
                "occurred_at": _api_time(transfer.transferred_at),
                "basis": "inventory_lot_transfer",
                "direction": direction_code,
                "transfer_id": transfer.id,
                "from_location_id": transfer.source_location_id,
                "from_location": location_labels.get(transfer.source_location_id),
                "to_location_id": transfer.target_location_id,
                "to_location": location_labels.get(transfer.target_location_id),
                "quantity": transfer.quantity,
                "unit": lot.unit,
                "operator_id": transfer.transferred_by,
            }
        )

    movements = db.scalars(
        select(InventoryMovement)
        .where(InventoryMovement.inventory_lot_id == lot.id)
        .order_by(InventoryMovement.created_at, InventoryMovement.id)
    ).all()

    # Floor-three loose-goods merging records the selected lot with an
    # ``InventoryMovement`` key ending in ``:lot:{lot.id}``, while the physical
    # source/target pallet facts are stored as an exact ``clear``/``add_item``
    # pair.  Only combine all three immutable facts when the keys and locations
    # agree; incomplete historical rows remain visible as a generic movement.
    loose_merge_bases: dict[str, InventoryMovement] = {}
    lot_key_suffix = f":lot:{lot.id}"
    for movement in movements:
        key = movement.idempotency_key or ""
        if (
            movement.movement_type == "location_transfer"
            and movement.quantity == 0
            and key.endswith(lot_key_suffix)
            and len(key) > len(lot_key_suffix)
        ):
            loose_merge_bases[key[: -len(lot_key_suffix)]] = movement

    matched_loose_merge_movement_ids: set[int] = set()
    if loose_merge_bases:
        pallet_keys = {
            key
            for base_key in loose_merge_bases
            for key in (base_key, f"{base_key}:target")
        }
        loose_pallet_rows = db.scalars(
            select(InventoryLocationMovement).where(
                InventoryLocationMovement.idempotency_key.in_(sorted(pallet_keys))
            )
        ).all()
        loose_pallet_by_key = {
            row.idempotency_key: row
            for row in loose_pallet_rows
            if row.idempotency_key is not None
        }
        loose_location_ids: set[int] = set()
        valid_loose_merges: list[
            tuple[InventoryMovement, InventoryLocationMovement, InventoryLocationMovement]
        ] = []
        for base_key, lot_movement in loose_merge_bases.items():
            source = loose_pallet_by_key.get(base_key)
            target = loose_pallet_by_key.get(f"{base_key}:target")
            if (
                source is None
                or target is None
                or source.movement_type != "clear"
                or target.movement_type != "add_item"
                or source.pallet_id == target.pallet_id
                or source.from_location_id is None
                or source.to_location_id is None
                or source.to_location_id != target.from_location_id
                or source.to_location_id != target.to_location_id
                or source.moved_at != target.moved_at
                or source.operator_id != target.operator_id
                or source.remarks != target.remarks
            ):
                continue
            loose_location_ids.update(
                {source.from_location_id, source.to_location_id}
            )
            valid_loose_merges.append((lot_movement, source, target))

        loose_location_labels = _location_labels(db, loose_location_ids)
        for lot_movement, source, target in valid_loose_merges:
            matched_loose_merge_movement_ids.add(lot_movement.id)
            events.append(
                {
                    "event_type": "pallet_location_move",
                    "movement_subtype": "loose_goods_merge",
                    "label": "零散货合并移位",
                    "occurred_at": beijing_naive_to_api(source.moved_at),
                    "basis": "inventory_location_movement_loose_goods_merge",
                    "direction": "move",
                    "movement_id": lot_movement.id,
                    "source_pallet_movement_id": source.id,
                    "target_pallet_movement_id": target.id,
                    "source_pallet_id": source.pallet_id,
                    "target_pallet_id": target.pallet_id,
                    "from_location_id": source.from_location_id,
                    "from_location": loose_location_labels.get(
                        source.from_location_id
                    ),
                    "to_location_id": source.to_location_id,
                    "to_location": loose_location_labels.get(source.to_location_id),
                    "operator_id": source.operator_id,
                    "remarks": source.remarks if include_sensitive_details else None,
                }
            )

    pallet_item = lot.pallet_item
    pallet_move_keys: set[str] = set()
    if pallet_item is not None:
        pallet_movements = db.scalars(
            select(InventoryLocationMovement)
            .where(
                InventoryLocationMovement.pallet_id == pallet_item.pallet_id,
                InventoryLocationMovement.movement_type == "move",
                InventoryLocationMovement.moved_at
                >= pallet_item.created_at + timedelta(hours=8),
            )
            .order_by(
                InventoryLocationMovement.moved_at,
                InventoryLocationMovement.id,
            )
        ).all()
        pallet_location_ids = {
            location_id
            for movement in pallet_movements
            for location_id in (movement.from_location_id, movement.to_location_id)
            if location_id is not None
        }
        pallet_location_labels = _location_labels(db, pallet_location_ids)
        for movement in pallet_movements:
            if movement.idempotency_key:
                pallet_move_keys.add(movement.idempotency_key)
            from_location = pallet_location_labels.get(movement.from_location_id)
            to_location = pallet_location_labels.get(movement.to_location_id)
            events.append(
                {
                    "event_type": "pallet_location_move",
                    "label": "整栈板位置移位",
                    "occurred_at": beijing_naive_to_api(movement.moved_at),
                    "basis": "inventory_location_movement_current_binding",
                    "movement_id": movement.id,
                    "pallet_id": movement.pallet_id,
                    "from_location": from_location,
                    "to_location": to_location,
                    "operator_id": movement.operator_id,
                    "remarks": movement.remarks if include_sensitive_details else None,
                    "scope_notice": "仅覆盖当前批次绑定该栈板后的可靠记录",
                }
            )

    stocktake_rows = db.execute(
        select(StocktakeItem, StocktakeOrder)
        .join(StocktakeOrder, StocktakeOrder.id == StocktakeItem.order_id)
        .where(StocktakeItem.inventory_lot_id == lot.id)
    ).all()
    for item, order in stocktake_rows:
        events.append(
            {
                "event_type": "stocktake_submitted",
                "label": "盘点提交",
                "occurred_at": _api_time(order.submitted_at),
                "basis": "stocktake_order",
                "order_number": order.order_number,
                "status": "submitted",
                "current_status": order.status,
                "operator_id": order.submitted_by,
                "system_quantity": item.on_hand_quantity_snapshot,
                "counted_quantity": item.counted_quantity,
                "difference_quantity": item.difference_quantity,
                "adjustment_movement_id": item.adjustment_movement_id,
            }
        )
        if order.reviewed_at is not None:
            events.append(
                {
                    "event_type": f"stocktake_{order.status}",
                    "label": "盘点审核通过" if order.status == "approved" else "盘点驳回",
                    "occurred_at": _api_time(order.reviewed_at),
                    "basis": "stocktake_order_review",
                    "order_number": order.order_number,
                    "status": order.status,
                    "operator_id": order.reviewed_by,
                    "note": order.review_note if include_sensitive_details else None,
                }
            )

    for movement in movements:
        if movement.id in matched_loose_merge_movement_ids:
            continue
        if (
            movement.movement_type == "location_transfer"
            and movement.idempotency_key in transfer_movement_keys
        ):
            continue
        event = {
            "event_type": f"inventory_{movement.movement_type}",
            "label": movement.movement_type,
            "occurred_at": _api_time(movement.created_at),
            "basis": "inventory_movement",
            "movement_id": movement.id,
            "movement_number": movement.movement_number,
            "quantity": movement.quantity,
            "unit": movement.unit,
            "before_available": movement.before_available,
            "after_available": movement.after_available,
            "before_reserved": movement.before_reserved,
            "after_reserved": movement.after_reserved,
            "before_consumed": movement.before_consumed,
            "after_consumed": movement.after_consumed,
            "before_damaged": movement.before_damaged,
            "after_damaged": movement.after_damaged,
            "before_scrapped": movement.before_scrapped,
            "after_scrapped": movement.after_scrapped,
            "reason": movement.reason if include_sensitive_details else None,
            "remarks": (
                movement.remarks
                if include_sensitive_details
                and movement.movement_type != "adjust"
                else None
            ),
            "operator_id": movement.operator_id,
        }
        if movement.movement_type == "adjust" and movement.remarks:
            try:
                audit = json.loads(movement.remarks)
            except (TypeError, ValueError):
                audit = None
            location_change = (
                (audit or {}).get("changes", {}).get("warehouse_location_id")
                if isinstance(audit, dict)
                else None
            )
            if isinstance(location_change, dict):
                edit_move_key = (
                    f"finished-edit:{sha256(movement.idempotency_key.encode('utf-8')).hexdigest()}"
                    if movement.idempotency_key
                    else None
                )
                if edit_move_key not in pallet_move_keys:
                    from_id = location_change.get("before")
                    to_id = location_change.get("after")
                    inferred_labels = _location_labels(
                        db,
                        {
                            int(value)
                            for value in (from_id, to_id)
                            if value is not None
                        },
                    )
                    event.update(
                        {
                            "label": "编辑批次并移位",
                            "from_location_id": from_id,
                            "from_location": inferred_labels.get(from_id),
                            "to_location_id": to_id,
                            "to_location": inferred_labels.get(to_id),
                            "basis": "inferred_from_finished_lot_edit_audit",
                            "scope_notice": "位置方向来自不可变编辑审计，历史无独立移位单",
                        }
                    )
                    if include_sensitive_details:
                        event["remarks"] = movement.remarks
        events.append(event)
    if include_sensitive_details:
        operator_names = _operator_names(
            db,
            {
                int(event["operator_id"])
                for event in events
                if event.get("operator_id") is not None
            },
        )
        for event in events:
            if event.get("operator_id") is not None:
                event["operator_name"] = operator_names.get(int(event["operator_id"]))
    return _timeline(events)


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


def build_mold_detail_timeline(
    db: Session,
    mold: MoldTool,
    *,
    products: list[Product],
    allowed_customer_ids: set[int] | None,
) -> dict:
    """Return every reliable mold time fact without inventing inbound/stocktake data."""

    summary = build_mold_time_archives(
        db,
        [mold],
        products_by_mold={mold.id: products},
        allowed_customer_ids=allowed_customer_ids,
    )[mold.id]
    events: list[dict] = [
        {
            "event_type": "mold_registration",
            "label": "模具档案登记",
            "occurred_at": _api_time(mold.created_at),
            "basis": "mold_master",
            "operator_id": mold.created_by,
        }
    ]
    movements = db.scalars(
        select(MoldLocationMovement)
        .where(MoldLocationMovement.mold_tool_id == mold.id)
        .order_by(MoldLocationMovement.moved_at, MoldLocationMovement.id)
    ).all()
    movement_labels = {
        "archive": "封存移位",
        "restore": "搬回恢复",
        "manual_input": "位置移位",
        "scanner_paste": "扫码粘贴移位",
        "url_parameter": "扫码链接移位",
        "api": "位置移位",
    }
    for movement in movements:
        events.append(
            {
                "event_type": "mold_location_move",
                "label": movement_labels.get(movement.source, "位置移位"),
                "occurred_at": _api_time(movement.moved_at),
                "basis": "mold_location_movement",
                "movement_id": movement.id,
                "from_location": movement.from_location,
                "to_location": movement.to_location,
                "source": movement.source,
                "note": movement.note if allowed_customer_ids is None else None,
                "operator_id": movement.actor_id,
                "expected_version": movement.expected_version,
                "resulting_version": movement.resulting_version,
            }
        )
    repair_events = db.scalars(
        select(MoldRepairEvent)
        .where(MoldRepairEvent.mold_tool_id == mold.id)
        .order_by(MoldRepairEvent.occurred_at, MoldRepairEvent.id)
    ).all()
    for repair_event in repair_events:
        events.append(
            {
                "event_type": "mold_repair_status",
                "label": "标记待维修" if repair_event.after_status == "needs_repair" else "维修完毕",
                "occurred_at": _api_time(repair_event.occurred_at),
                "basis": "mold_repair_event",
                "repair_event_id": repair_event.id,
                "before_status": repair_event.before_status,
                "after_status": repair_event.after_status,
                "operator_id": repair_event.actor_id,
                "operator_name": (
                    repair_event.actor_username_snapshot
                    if allowed_customer_ids is None
                    else None
                ),
                "expected_version": repair_event.expected_version,
                "resulting_version": repair_event.resulting_version,
            }
        )
    if mold.last_location_confirmed_at is not None and not movements:
        events.append(
            {
                "event_type": "mold_location_confirmation",
                "label": "位置确认",
                "occurred_at": _api_time(mold.last_location_confirmed_at),
                "basis": "mold_location_confirmation",
                "to_location": mold.rack_location,
                "operator_id": mold.last_location_confirmed_by,
            }
        )
    legacy_restore_logs = db.scalars(
        select(OperationLog)
        .where(
            OperationLog.entity_type == "mold_tool",
            OperationLog.entity_id == mold.id,
            OperationLog.action_code == "mold.legacy_disabled.restore",
        )
        .order_by(OperationLog.created_at, OperationLog.id)
    ).all()
    for log in legacy_restore_logs:
        events.append(
            {
                "event_type": "mold_legacy_disabled_restore",
                "label": "历史停用恢复使用",
                "occurred_at": _api_time(log.created_at),
                "basis": "operation_log",
                "operator_id": log.actor_user_id_snapshot or log.user_id,
                "scope_notice": "兼容恢复历史普通停用事实，不代表实物移位",
            }
        )
    for event in summary.get("timeline") or []:
        if event.get("event_type") in {
            "customer_order_use",
            "actual_production_use",
        }:
            events.append(dict(event))
    if allowed_customer_ids is None:
        operator_names = _operator_names(
            db,
            {
                int(event["operator_id"])
                for event in events
                if event.get("operator_id") is not None
            },
        )
        for event in events:
            if event.get("operator_id") is not None:
                event["operator_name"] = operator_names.get(
                    int(event["operator_id"])
                )
    summary["timeline"] = _timeline(events)
    summary["stocktake_status"] = "not_supported"
    summary["stocktake_notice"] = "当前尚无模具盘点事实；不会从档案更新时间推算。"
    summary["inbound_status"] = "not_recorded"
    summary["inbound_notice"] = "当前尚无模具实物入库单；档案登记不等于实物入库。"
    return summary


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
