from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from threading import RLock
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models.audit import OperationLog
from app.models.warehouse_inventory import (
    FinishedGoodsInventoryDetail,
    InventoryLot,
    InventoryPallet,
    InventoryPalletItem,
    WarehouseLocation,
)
from app.services.floor3_locations import Floor3LocationError, move_pallet
from app.services.location_candidates import operational_location_issue
from app.services.warehouse_inventory import (
    WarehouseInventoryError,
    transfer_finished_lot_between_locations,
)


WAREHOUSE_MOVEMENT_BATCH_LOCK = RLock()
BATCH_AUDIT_ACTION_CODE = "warehouse.movement_batch.confirmed"


@dataclass(frozen=True)
class WarehouseMovementBatchItem:
    client_item_id: str
    operation: Literal["pallet_move", "lot_transfer"]
    target_location_id: int
    expected_version: int
    expected_target_layout_version: int | None = None
    pallet_id: int | None = None
    lot_id: int | None = None
    quantity: int | None = None
    remarks: str | None = None


class WarehouseMovementBatchError(ValueError):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def movement_batch_request_hash(
    *, batch_id: str, items: list[WarehouseMovementBatchItem]
) -> str:
    canonical = {
        "batch_id": batch_id,
        "items": [
            {
                "client_item_id": item.client_item_id,
                "operation": item.operation,
                "target_location_id": item.target_location_id,
                "expected_version": item.expected_version,
                "expected_target_layout_version": item.expected_target_layout_version,
                "pallet_id": item.pallet_id,
                "lot_id": item.lot_id,
                "quantity": item.quantity,
                "remarks": item.remarks,
            }
            for item in items
        ],
    }
    return sha256(
        json.dumps(
            canonical,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def movement_batch_replay(
    db: Session,
    *,
    batch_id: str,
    request_hash: str,
    actor_user_id: int | None,
) -> dict | None:
    row = db.scalar(
        select(OperationLog)
        .where(
            OperationLog.batch_id == batch_id,
            OperationLog.action_code == BATCH_AUDIT_ACTION_CODE,
            OperationLog.result == "success",
        )
        .order_by(OperationLog.id.desc())
        .limit(1)
    )
    if row is None:
        return None
    if row.actor_user_id_snapshot != actor_user_id:
        raise WarehouseMovementBatchError(
            "该批次标识已由其他操作员使用，不能查看或重放其移货结果",
            409,
        )
    try:
        details = json.loads(row.details or "{}")
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise WarehouseMovementBatchError(
            "已完成移货批次的审计记录损坏，请停止重试并联系管理员",
            409,
        ) from error
    if details.get("request_hash") != request_hash:
        raise WarehouseMovementBatchError(
            "同一批次标识已用于不同的移货内容",
            409,
        )
    result = details.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("items"), list):
        raise WarehouseMovementBatchError(
            "已完成移货批次缺少可回放结果，请停止重试并联系管理员",
            409,
        )
    return result


def _pallet_query():
    return select(InventoryPallet).options(
        selectinload(InventoryPallet.items).selectinload(
            InventoryPalletItem.inventory_lot
        )
    )


def load_movable_pallet(db: Session, pallet_id: int) -> InventoryPallet:
    pallet = db.scalar(_pallet_query().where(InventoryPallet.id == pallet_id))
    if pallet is None:
        raise WarehouseMovementBatchError("栈板不存在", 404)
    if (
        pallet.status != "active"
        or not pallet.is_current
        or pallet.location_id is None
    ):
        raise WarehouseMovementBatchError("栈板当前不在有效货位，不能移货", 409)
    source = db.get(WarehouseLocation, pallet.location_id)
    if source is None:
        raise WarehouseMovementBatchError("栈板所在库位不存在", 409)
    if source.warehouse_floor not in {1, 3}:
        raise WarehouseMovementBatchError("移货模式当前只接入一楼和三楼正式库存", 409)
    supported_source = bool(
        (source.source_version == "V11" and source.warehouse_floor == 3)
        or (
            source.source_version in {"TWIN_V1", "CURRENT_MAP"}
            and source.warehouse_floor in {1, 3}
        )
        or (
            source.source_version == "P1-25C"
            and source.warehouse_floor == 1
            and source.location_code == "F1-DISPATCH-01"
        )
    )
    if not supported_source:
        raise WarehouseMovementBatchError("栈板来源不属于已接入的正式地图库位", 409)
    if source.storage_type == "rack":
        raise WarehouseMovementBatchError("真实栈板不能从货架格直接移出", 409)
    if not pallet.items:
        raise WarehouseMovementBatchError("空栈板不能通过库存移货模式移动", 409)
    for pallet_item in pallet.items:
        lot = pallet_item.inventory_lot
        if (
            pallet_item.item_type != "finished"
            or lot is None
            or lot.inventory_type != "finished"
            or lot.status != "active"
            or lot.finished_detail is None
            or lot.warehouse_location_id != pallet.location_id
            or (
                int(lot.quantity_available or 0)
                + int(lot.quantity_reserved or 0)
                + int(lot.quantity_damaged or 0)
            )
            <= 0
            or int(lot.quantity_damaged or 0) > 0
            or int(lot.quantity_scrapped or 0) > 0
        ):
            raise WarehouseMovementBatchError(
                "整板移动只允许全部内容均已接入正式有效成品库存的栈板",
                409,
            )
        detail: FinishedGoodsInventoryDetail = lot.finished_detail
        if (
            pallet_item.customer_id != detail.owner_customer_id
            or pallet_item.product_id != detail.product_id
        ):
            raise WarehouseMovementBatchError(
                "栈板内容与正式库存客户或产品归属不一致",
                409,
            )
    return pallet


def _preflight_item_source(
    db: Session,
    item: WarehouseMovementBatchItem,
) -> tuple[int, int | None]:
    """Return source location and linked pallet (for overlap checks)."""

    if item.operation == "pallet_move":
        if item.pallet_id is None or item.lot_id is not None or item.quantity is not None:
            raise WarehouseMovementBatchError("整板移动参数不完整", 422)
        pallet = load_movable_pallet(db, item.pallet_id)
        if int(pallet.version) != item.expected_version:
            raise WarehouseMovementBatchError(
                "栈板已被其他操作更新，请刷新后重试", 409
            )
        return int(pallet.location_id), int(pallet.id)

    if item.lot_id is None or item.pallet_id is not None or item.quantity is None:
        raise WarehouseMovementBatchError("部分移货参数不完整", 422)
    lot = db.scalar(
        select(InventoryLot)
        .options(
            selectinload(InventoryLot.finished_detail),
            selectinload(InventoryLot.pallet_item),
        )
        .where(InventoryLot.id == item.lot_id)
    )
    if lot is None or lot.finished_detail is None:
        raise WarehouseMovementBatchError("成品库存批次不存在", 404)
    if lot.inventory_type != "finished" or lot.status != "active":
        raise WarehouseMovementBatchError("只有有效成品库存批次可以移货", 409)
    if int(lot.version) != item.expected_version:
        raise WarehouseMovementBatchError(
            "库存已被其他操作更新，请刷新后重试", 409
        )
    movable = int(lot.quantity_available or 0) + int(lot.quantity_reserved or 0)
    if item.quantity <= 0 or item.quantity > movable:
        raise WarehouseMovementBatchError(f"当前只有 {movable} 个可移货", 409)
    if int(lot.quantity_damaged or 0) > 0 or int(lot.quantity_scrapped or 0) > 0:
        raise WarehouseMovementBatchError(
            "该批次仍有报损或报废数量，不能直接移货", 409
        )
    source = db.get(WarehouseLocation, lot.warehouse_location_id)
    if source is None:
        raise WarehouseMovementBatchError("库存来源库位不存在", 409)
    linked_pallet_id = (
        int(lot.pallet_item.pallet_id) if lot.pallet_item is not None else None
    )
    return int(source.id), linked_pallet_id


def preflight_warehouse_movement_batch(
    db: Session,
    *,
    items: list[WarehouseMovementBatchItem],
) -> None:
    client_ids: set[str] = set()
    source_keys: set[tuple[str, int]] = set()
    target_ids: set[int] = set()
    whole_pallet_ids: set[int] = set()
    linked_pallets_by_lot: dict[int, int | None] = {}

    for item in items:
        if item.client_item_id in client_ids:
            raise WarehouseMovementBatchError("批次内存在重复的页面草稿标识", 409)
        client_ids.add(item.client_item_id)
        source_id = item.pallet_id if item.operation == "pallet_move" else item.lot_id
        assert source_id is not None
        source_key = (item.operation, int(source_id))
        if source_key in source_keys:
            raise WarehouseMovementBatchError("同一货物不能在一个批次中重复移动", 409)
        source_keys.add(source_key)
        if item.target_location_id in target_ids:
            raise WarehouseMovementBatchError("一个空货位不能在同批次中接收多项货物", 409)
        target_ids.add(item.target_location_id)
        source_location_id, linked_pallet_id = _preflight_item_source(db, item)
        if source_location_id == item.target_location_id:
            raise WarehouseMovementBatchError("目标货位不能与来源货位相同", 409)
        if item.operation == "pallet_move":
            whole_pallet_ids.add(int(item.pallet_id))
        else:
            linked_pallets_by_lot[int(item.lot_id)] = linked_pallet_id

        target = db.get(WarehouseLocation, item.target_location_id)
        if target is None:
            raise WarehouseMovementBatchError("目标货位不存在", 404)
        issue = operational_location_issue(
            db,
            target,
            warehouse_types={"finished", "shared"},
            pallet_storage_only=True,
            require_published=True,
            require_map_geometry=True,
            required_inventory_type="finished",
            require_empty=True,
            capacity_source_location_id=(
                source_location_id if item.operation == "pallet_move" else None
            ),
        )
        if issue:
            raise WarehouseMovementBatchError(f"目标货位不可用：{issue}", 409)

    overlapping = {
        pallet_id
        for pallet_id in linked_pallets_by_lot.values()
        if pallet_id is not None and pallet_id in whole_pallet_ids
    }
    if overlapping:
        raise WarehouseMovementBatchError(
            "不能在同一批次中同时整板移动并拆分该板上的批次", 409
        )


def execute_warehouse_movement_batch(
    db: Session,
    *,
    batch_id: str,
    items: list[WarehouseMovementBatchItem],
    operator_id: int | None,
) -> dict:
    preflight_warehouse_movement_batch(db, items=items)
    results: list[dict] = []
    for item in items:
        subkey = "p147c:" + sha256(
            f"{batch_id}:{item.client_item_id}:{item.operation}".encode("utf-8")
        ).hexdigest()
        try:
            if item.operation == "pallet_move":
                moved = move_pallet(
                    db,
                    pallet_id=int(item.pallet_id),
                    expected_version=item.expected_version,
                    to_location_id=item.target_location_id,
                    remarks=item.remarks,
                    operator_id=operator_id,
                    idempotency_key=subkey,
                    require_published_target=True,
                    expected_target_layout_version=item.expected_target_layout_version,
                )
                results.append(
                    {
                        "client_item_id": item.client_item_id,
                        "operation": item.operation,
                        "pallet_id": moved.pallet.id,
                        "target_location_id": item.target_location_id,
                        "version_after": moved.pallet.version,
                        "movement_id": moved.movement.id,
                    }
                )
            else:
                transferred = transfer_finished_lot_between_locations(
                    db,
                    lot_id=int(item.lot_id),
                    expected_version=item.expected_version,
                    quantity=int(item.quantity),
                    location_id=item.target_location_id,
                    operator_id=operator_id,
                    idempotency_key=subkey,
                    require_empty_target=True,
                    expected_target_layout_version=item.expected_target_layout_version,
                )
                results.append(
                    {
                        "client_item_id": item.client_item_id,
                        "operation": item.operation,
                        "source_lot_id": transferred.source_lot.id,
                        "target_lot_id": transferred.target_lot.id,
                        "target_location_id": item.target_location_id,
                        "quantity": transferred.transfer.quantity,
                        "transfer_id": transferred.transfer.id,
                    }
                )
        except Floor3LocationError as error:
            raise WarehouseMovementBatchError(str(error), error.status_code) from error
        except WarehouseInventoryError as error:
            raise WarehouseMovementBatchError(str(error), error.status_code) from error
    return {"batch_id": batch_id, "items": results}
