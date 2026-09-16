from __future__ import annotations

from app.services.warehouse_storage_usage import effective_inventory_usages

from dataclasses import dataclass
from datetime import date
from hashlib import sha256
import json
from threading import RLock
from typing import Literal, Sequence

from sqlalchemy import and_, func, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, selectinload

from app.core.time_contract import beijing_now_naive
from app.models.audit import OperationLog
from app.models.customer import Customer
from app.models.product import Product
from app.models.production import ProductionCompletion
from app.models.stocktake import StocktakeItem, StocktakeOrder
from app.models.warehouse_inventory import (
    DeliveryInventoryAllocation,
    FinishedGoodsInventoryDetail,
    Floor3LocationLayout,
    InventoryLot,
    InventoryMovement,
    InventoryPallet,
    InventoryPalletItem,
    InventoryReservation,
    SemiFinishedInventoryDetail,
    UnorderedFinishedDeliveryAllocation,
    WarehouseArea,
    WarehouseAreaStoragePolicy,
    WarehouseFloor,
    WarehouseLocation,
)
from app.services.floor3_locations import Floor3LocationError, clear_pallet
from app.services.location_candidates import operational_location_issue
from app.services.location_candidates import claim_active_placed_location
from app.services.warehouse_inventory import (
    WarehouseInventoryError,
    _pallet_has_physical_goods,
    manual_finished_in,
    manual_semi_finished_in,
    mutate_lot,
    replace_semi_finished_lot_allowed_products,
    semi_finished_lot_allowed_product_ids,
)


WAREHOUSE_STOCKTAKE_BATCH_LOCK = RLock()
STOCKTAKE_BATCH_ACTION_CODE = "warehouse.stocktake_batch.confirmed"


@dataclass(frozen=True)
class WarehouseStocktakeBatchItem:
    client_item_id: str
    operation: Literal["add", "decrease"]
    location_id: int
    expected_layout_version: int
    quantity: int
    inventory_type: Literal["finished", "semi_finished", "raw_material"] | None = None
    unit: Literal["boxes", "sheets"] | None = None
    customer_id: int | None = None
    product_id: int | None = None
    stock_date: date | None = None
    lot_id: int | None = None
    expected_version: int | None = None
    source_kind: Literal["existing_stocktake", "partner_transfer"] | None = None
    stock_stage: Literal["complete", "body"] = "complete"


class WarehouseStocktakeBatchError(ValueError):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def stocktake_batch_request_hash(
    *, batch_id: str, items: list[WarehouseStocktakeBatchItem]
) -> str:
    def canonical_item(item: WarehouseStocktakeBatchItem) -> dict[str, object]:
        value: dict[str, object] = {
            "client_item_id": item.client_item_id,
            "operation": item.operation,
            "location_id": item.location_id,
            "expected_layout_version": item.expected_layout_version,
            "quantity": item.quantity,
            "inventory_type": item.inventory_type,
            "unit": item.unit,
            "customer_id": item.customer_id,
            "product_id": item.product_id,
            "stock_date": item.stock_date.isoformat() if item.stock_date else None,
            "lot_id": item.lot_id,
            "expected_version": item.expected_version,
        }
        # Keep the legacy request hash stable for clients that do not send this
        # newly added field. Explicit source selections remain part of the hash.
        if item.source_kind is not None:
            value["source_kind"] = item.source_kind
        if item.stock_stage != "complete":
            value["stock_stage"] = item.stock_stage
        return value

    canonical = {
        "batch_id": batch_id,
        "items": [canonical_item(item) for item in items],
    }
    return sha256(
        json.dumps(
            canonical,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def stocktake_batch_replay(
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
            OperationLog.action_code == STOCKTAKE_BATCH_ACTION_CODE,
            OperationLog.result == "success",
        )
        .order_by(OperationLog.id.desc())
        .limit(1)
    )
    if row is None:
        return None
    if row.actor_user_id_snapshot != actor_user_id:
        raise WarehouseStocktakeBatchError(
            "该盘点批次标识已由其他操作员使用，不能查看或重放其结果", 409
        )
    try:
        details = json.loads(row.details or "{}")
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise WarehouseStocktakeBatchError(
            "已完成盘点批次的审计记录损坏，请停止重试并联系管理员", 409
        ) from error
    if details.get("request_hash") != request_hash:
        raise WarehouseStocktakeBatchError(
            "同一盘点批次标识已用于不同的库存调整内容", 409
        )
    result = details.get("result")
    compact = details.get("result_compact")
    if result is None and isinstance(compact, dict):
        try:
            result = {
                "message": compact["message"],
                "batch_id": compact["batch_id"],
                "confirmed_at": compact["confirmed_at"],
                "operator_id": compact.get("operator_id"),
                "request_hash": request_hash,
                "items": [
                    {
                        "client_item_id": values[0],
                        "operation": values[1],
                        "lot_id": values[2],
                        "movement_id": values[3],
                        "location_id": values[4],
                        "customer_id": values[5],
                        "product_id": values[6],
                        "inventory_type": values[7],
                        "unit": values[8],
                        "quantity_before": values[9],
                        "quantity_after": values[10],
                        "version_after": values[11],
                        "status_after": values[12],
                        "released_pallet_id": values[13],
                        "source_kind": values[14] if len(values) > 14 else None,
                        "stock_stage": values[15] if len(values) > 15 else "complete",
                    }
                    for values in compact["items"]
                ],
            }
        except (KeyError, IndexError, TypeError) as error:
            raise WarehouseStocktakeBatchError(
                "已完成盘点批次的压缩回放结果损坏，请联系管理员", 409
            ) from error
    if not isinstance(result, dict) or not isinstance(result.get("items"), list):
        raise WarehouseStocktakeBatchError(
            "已完成盘点批次缺少可回放结果，请停止重试并联系管理员", 409
        )
    return result


def stocktake_batch_audit_details(
    *, request_hash: str, result: dict
) -> dict:
    """Keep replay evidence below the audit serializer's 16 KB hard limit."""

    full = {"request_hash": request_hash, "result": result}
    rendered = json.dumps(full, ensure_ascii=False, sort_keys=True)
    if len(rendered) <= 14_000:
        return full
    compact = {
        "message": result.get("message"),
        "batch_id": result.get("batch_id"),
        "confirmed_at": result.get("confirmed_at"),
        "operator_id": result.get("operator_id"),
        "items": [
            [
                row.get("client_item_id"),
                row.get("operation"),
                row.get("lot_id"),
                row.get("movement_id"),
                row.get("location_id"),
                row.get("customer_id"),
                row.get("product_id"),
                row.get("inventory_type"),
                row.get("unit"),
                row.get("quantity_before"),
                row.get("quantity_after"),
                row.get("version_after"),
                row.get("status_after"),
                row.get("released_pallet_id"),
                row.get("source_kind"),
                row.get("stock_stage", "complete"),
            ]
            for row in result.get("items", [])
        ],
    }
    details = {"request_hash": request_hash, "result_compact": compact}
    if len(json.dumps(details, ensure_ascii=False, sort_keys=True)) > 14_000:
        raise WarehouseStocktakeBatchError(
            "盘点批次结果超过安全审计上限，请缩短页面草稿标识后重试", 422
        )
    return details


def _lot_query():
    return select(InventoryLot).options(
        selectinload(InventoryLot.finished_detail),
        selectinload(InventoryLot.semi_finished_detail),
        selectinload(InventoryLot.allowed_products),
        selectinload(InventoryLot.pallet_item).selectinload(
            InventoryPalletItem.pallet
        ),
    )


def load_stocktake_lot(db: Session, lot_id: int) -> InventoryLot:
    lot = db.scalar(_lot_query().where(InventoryLot.id == lot_id))
    if lot is None:
        raise WarehouseStocktakeBatchError("库存批次不存在", 404)
    return lot


def stocktake_item_customer_id(
    db: Session, item: WarehouseStocktakeBatchItem
) -> int:
    if item.operation == "add":
        if item.customer_id is None:
            raise WarehouseStocktakeBatchError("盘点新增缺少客户", 422)
        return int(item.customer_id)
    if item.lot_id is None:
        raise WarehouseStocktakeBatchError("盘点调减缺少库存批次", 422)
    lot = load_stocktake_lot(db, item.lot_id)
    detail = lot.finished_detail or lot.semi_finished_detail
    customer_id = detail.owner_customer_id if detail is not None else None
    if customer_id is None:
        raise WarehouseStocktakeBatchError("该库存批次缺少明确客户归属", 409)
    return int(customer_id)


def _supported_formal_location(
    db: Session,
    *,
    location_id: int,
    inventory_type: str,
    capacity_source_location_id: int | None = None,
) -> WarehouseLocation:
    location = db.get(WarehouseLocation, location_id)
    if location is None:
        raise WarehouseStocktakeBatchError("盘点货位不存在", 404)
    supported = bool(
        (location.source_version == "V11" and location.warehouse_floor == 3)
        or (
            location.source_version in {"TWIN_V1", "CURRENT_MAP"}
            and location.warehouse_floor in {1, 3, 4}
        )
    )
    if not supported:
        raise WarehouseStocktakeBatchError(
            "盘点调整只允许一楼、三楼或四楼已接入的正式地图库位", 409
        )
    warehouse_types = (
        {"finished", "shared"}
        if inventory_type == "finished"
        else {"semi_finished", "shared"}
    )
    issue = operational_location_issue(
        db,
        location,
        warehouse_types=warehouse_types,
        require_published=True,
        require_map_geometry=True,
        required_inventory_type=inventory_type,
        capacity_source_location_id=capacity_source_location_id,
    )
    if issue:
        raise WarehouseStocktakeBatchError(f"盘点货位不可用：{issue}", 409)
    return location


def _is_dispatch_location(
    *,
    area_code: str | None,
    location_code: str | None,
) -> bool:
    """Fail closed for every formal direct-delivery staging identity."""

    normalized_area = str(area_code or "").strip().upper()
    normalized_code = str(location_code or "").strip().upper()
    return normalized_area == "DISPATCH" or normalized_code in {
        "F1-DISPATCH-01",
        "1F-DISPATCH-01",
    }


def stocktake_decrease_issues(
    db: Session,
    lots: Sequence[InventoryLot],
) -> dict[int, str | None]:
    """Return one scope-safe, batched operability projection for formal lots.

    Callers must pass only lots already authorized for the current user.  The
    query returns blocker booleans and generic reasons only, so task/customer
    details cannot leak through the dashboard projection.
    """

    scoped_lots = list(lots)
    lot_ids = {int(row.id) for row in scoped_lots}
    if not lot_ids:
        return {}

    active_reservation_exists = (
        select(InventoryReservation.id)
        .where(
            InventoryReservation.inventory_lot_id == InventoryLot.id,
            InventoryReservation.status.in_(("active", "partial")),
        )
        .exists()
    )
    delivery_allocation_exists = (
        select(DeliveryInventoryAllocation.id)
        .join(
            InventoryReservation,
            InventoryReservation.id == DeliveryInventoryAllocation.reservation_id,
        )
        .where(
            InventoryReservation.inventory_lot_id == InventoryLot.id,
            DeliveryInventoryAllocation.status.in_(("active", "partial")),
        )
        .exists()
    )
    unordered_allocation_exists = (
        select(UnorderedFinishedDeliveryAllocation.id)
        .where(
            UnorderedFinishedDeliveryAllocation.inventory_lot_id == InventoryLot.id,
            UnorderedFinishedDeliveryAllocation.status.in_(
                ("planned", "dispatched", "partial_restored")
            ),
        )
        .exists()
    )
    pending_stocktake_exists = (
        select(StocktakeItem.id)
        .join(StocktakeOrder, StocktakeOrder.id == StocktakeItem.order_id)
        .where(
            StocktakeItem.inventory_lot_id == InventoryLot.id,
            StocktakeOrder.status.in_(("draft", "submitted")),
        )
        .exists()
    )
    posted_completion_exists = (
        select(ProductionCompletion.id)
        .where(
            ProductionCompletion.inventory_lot_id == InventoryLot.id,
            ProductionCompletion.status == "posted",
        )
        .exists()
    )
    blocker_rows = db.execute(
        select(
            InventoryLot.id.label("lot_id"),
            WarehouseLocation.id.label("location_id"),
            WarehouseLocation.source_version.label("source_version"),
            WarehouseLocation.warehouse_floor.label("warehouse_floor"),
            WarehouseLocation.warehouse_type.label("warehouse_type"),
            WarehouseLocation.placement_status.label("placement_status"),
            WarehouseLocation.is_active.label("location_is_active"),
            WarehouseLocation.area_code.label("area_code"),
            WarehouseLocation.location_code.label("location_code"),
            WarehouseFloor.id.label("floor_ledger_id"),
            WarehouseFloor.construction_status.label("floor_status"),
            WarehouseArea.id.label("area_ledger_id"),
            WarehouseArea.construction_status.label("area_status"),
            WarehouseAreaStoragePolicy.id.label("policy_id"),
            WarehouseAreaStoragePolicy.status.label("policy_status"),
            WarehouseAreaStoragePolicy.published_map_revision.label(
                "published_map_revision"
            ),
            WarehouseAreaStoragePolicy.allowed_inventory_types_json.label(
                "allowed_inventory_types_json"
            ),
            Floor3LocationLayout.id.label("map_geometry_id"),
            active_reservation_exists.label("has_active_reservation"),
            delivery_allocation_exists.label("has_delivery_allocation"),
            unordered_allocation_exists.label("has_unordered_allocation"),
            pending_stocktake_exists.label("has_pending_stocktake"),
            posted_completion_exists.label("has_posted_completion"),
        )
        .outerjoin(
            WarehouseLocation,
            WarehouseLocation.id == InventoryLot.warehouse_location_id,
        )
        .outerjoin(
            WarehouseFloor,
            WarehouseFloor.floor_number == WarehouseLocation.warehouse_floor,
        )
        .outerjoin(
            WarehouseArea,
            and_(
                WarehouseArea.floor_id == WarehouseFloor.id,
                func.upper(WarehouseArea.area_code)
                == func.upper(WarehouseLocation.area_code),
            ),
        )
        .outerjoin(
            WarehouseAreaStoragePolicy,
            WarehouseAreaStoragePolicy.area_id == WarehouseArea.id,
        )
        .outerjoin(
            Floor3LocationLayout,
            Floor3LocationLayout.location_id == WarehouseLocation.id,
        )
        .where(InventoryLot.id.in_(lot_ids))
    ).all()
    blocker_by_lot_id = {int(row.lot_id): row for row in blocker_rows}

    issues: dict[int, str | None] = {}
    for lot in scoped_lots:
        lot_id = int(lot.id)
        blockers = blocker_by_lot_id.get(lot_id)
        if blockers is None:
            issues[lot_id] = "库存批次已不存在，请刷新后重试"
        elif lot.status == "frozen":
            issues[lot_id] = "库存已冻结，不能盘点调减"
        elif lot.status != "active":
            issues[lot_id] = "库存不是正常状态，不能盘点调减"
        elif blockers.location_id is None:
            issues[lot_id] = "库存缺少实际货位，不能盘点调减"
        elif not bool(
            (
                blockers.source_version == "V11"
                and blockers.warehouse_floor == 3
            )
            or (
                blockers.source_version in {"TWIN_V1", "CURRENT_MAP"}
                and blockers.warehouse_floor in {1, 3, 4}
            )
        ):
            issues[lot_id] = "库存不在已接入的正式盘点货位"
        elif not blockers.location_is_active or blockers.placement_status != "placed":
            issues[lot_id] = "库存所在正式货位当前不可用"
        elif blockers.floor_ledger_id is None or blockers.floor_status != "enabled":
            issues[lot_id] = "库存所在正式楼层尚未启用"
        elif blockers.area_ledger_id is None or blockers.area_status != "enabled":
            issues[lot_id] = "库存所在正式区域尚未启用"
        elif (
            blockers.policy_id is None
            or blockers.policy_status != "published"
            or not str(blockers.published_map_revision or "").strip()
        ):
            issues[lot_id] = "库存所在正式区域尚未发布"
        elif blockers.warehouse_type not in {"finished", "semi_finished", "shared"}:
            issues[lot_id] = "库存类型与货位类型不匹配"
        elif not _published_policy_allows_inventory_type(
            blockers.allowed_inventory_types_json,
            inventory_type=lot.inventory_type,
        ):
            issues[lot_id] = "库存类型不在正式区域发布范围内"
        elif blockers.map_geometry_id is None:
            issues[lot_id] = "库存所在正式货位缺少地图位置"
        elif lot.unit != {
            "finished": "boxes",
            "semi_finished": "sheets",
        }.get(lot.inventory_type):
            issues[lot_id] = "库存类型与单位不匹配，不能盘点调减"
        elif _is_dispatch_location(
            area_code=blockers.area_code,
            location_code=blockers.location_code,
        ):
            issues[lot_id] = "待送区库存不能通过盘点直接调减"
        elif int(lot.quantity_reserved or 0) > 0:
            issues[lot_id] = "库存仍有预占数量，必须先释放预占"
        elif blockers.has_delivery_allocation:
            issues[lot_id] = "库存仍绑定待送分配，不能盘点调减"
        elif blockers.has_unordered_allocation:
            issues[lot_id] = "库存仍绑定无订单待送分配，不能盘点调减"
        elif blockers.has_active_reservation:
            issues[lot_id] = "库存仍绑定活跃预占或业务任务，不能盘点调减"
        elif blockers.has_pending_stocktake:
            issues[lot_id] = "库存仍绑定未完成盘点任务，不能盘点调减"
        elif int(lot.quantity_damaged or 0) > 0:
            issues[lot_id] = "库存仍有报损数量，不能盘点调减"
        elif int(lot.quantity_scrapped or 0) > 0:
            issues[lot_id] = "库存仍有报废数量，不能盘点调减"
        elif lot.source_type == "purchase_reserve":
            issues[lot_id] = "采购用途备库仍绑定收料追溯，不能通过地图盘点直接调减"
        elif (
            lot.source_type == "production_completion"
            or lot.source_ref_type == "production_completion"
            or blockers.has_posted_completion
        ):
            issues[lot_id] = "生产完工或任务绑定库存不能盘点调减"
        elif int(lot.quantity_available or 0) <= 0:
            issues[lot_id] = "当前没有可用库存可供盘点调减"
        else:
            issues[lot_id] = None
    return issues


def _published_policy_allows_inventory_type(
    raw_allowed_types: str | None,
    *,
    inventory_type: str,
) -> bool:
    try:
        allowed_types = json.loads(raw_allowed_types or "")
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    return bool(
        isinstance(allowed_types, list)
        and all(isinstance(value, str) for value in allowed_types)
        and inventory_type in effective_inventory_usages(allowed_types)
    )


def _active_customer_product(
    db: Session, *, customer_id: int, product_id: int
) -> tuple[Customer, Product]:
    customer = db.get(Customer, customer_id)
    if (
        customer is None
        or not customer.is_active
        or customer.status != "active"
    ):
        raise WarehouseStocktakeBatchError("客户不存在或已停用", 404)
    product = db.get(Product, product_id)
    if (
        product is None
        or product.deleted_at is not None
        or not product.is_active
        or product.customer_id != customer.id
    ):
        raise WarehouseStocktakeBatchError(
            "所选产品不属于当前客户或已停用", 409
        )
    return customer, product


def _location_live_lots(db: Session, location_id: int) -> list[InventoryLot]:
    physical_quantity = (
        InventoryLot.quantity_available
        + InventoryLot.quantity_reserved
        + InventoryLot.quantity_damaged
    )
    return list(
        db.scalars(
            _lot_query().where(
                InventoryLot.warehouse_location_id == location_id,
                InventoryLot.status.in_(("active", "frozen")),
                physical_quantity > 0,
            )
        ).unique().all()
    )


def _assert_add_compatible(
    db: Session,
    *,
    item: WarehouseStocktakeBatchItem,
) -> None:
    assert item.inventory_type is not None
    assert item.unit is not None
    assert item.customer_id is not None
    assert item.product_id is not None
    assert_location_add_compatible(db, item.location_id)


def assert_location_add_compatible(db: Session, location_id: int) -> None:
    # Co-location does not merge lots, owners, units, or product identities.
    for lot in _location_live_lots(db, location_id):
        detail = lot.finished_detail if lot.inventory_type == "finished" else lot.semi_finished_detail
        expected_unit = "boxes" if lot.inventory_type == "finished" else "sheets"
        if detail is None or lot.unit != expected_unit or lot.status != "active" or int(lot.quantity_damaged or 0) > 0:
            raise WarehouseStocktakeBatchError("当前货位存在冻结、报损或明细不完整的库存，请先核对", 409)

    current_pallet = db.scalar(
        select(InventoryPallet)
        .options(selectinload(InventoryPallet.items))
        .where(
            InventoryPallet.location_id == location_id,
            InventoryPallet.is_current.is_(True),
        )
    )
    if current_pallet is None:
        return
    for pallet_item in current_pallet.items:
        if pallet_item.inventory_lot_id is None:
            raise WarehouseStocktakeBatchError(
                "当前栈板仍有仅快照或待匹配条目，必须先核成正式库存后再盘点新增",
                409,
            )
        linked_lot = pallet_item.inventory_lot
        # Ledger history is not current occupancy. A fully counted-out/moved-out
        # batch may keep its old pallet snapshot for traceability.
        if linked_lot is not None and (
            int(linked_lot.quantity_available or 0)
            + int(linked_lot.quantity_reserved or 0)
            + int(linked_lot.quantity_damaged or 0)
        ) == 0:
            continue
        linked_detail = linked_lot.finished_detail if linked_lot is not None else None
        linked_product = (
            db.get(Product, linked_detail.product_id)
            if linked_detail is not None
            else None
        )
        if (
            pallet_item.item_type != "finished"
            or pallet_item.unit != "boxes"
            or pallet_item.match_status != "matched"
            or linked_lot is None
            or linked_lot.status != "active"
            or int(linked_lot.quantity_damaged or 0) > 0
            or linked_lot.inventory_type != "finished"
            or linked_lot.unit != "boxes"
            or linked_lot.warehouse_location_id != location_id
            or linked_detail is None
            or linked_product is None
            or linked_product.deleted_at is not None
            or not linked_product.is_active
            or linked_product.customer_id != linked_detail.owner_customer_id
            or pallet_item.customer_id != linked_detail.owner_customer_id
            or pallet_item.product_id != linked_detail.product_id
        ):
            raise WarehouseStocktakeBatchError(
                "当前栈板条目与正式库存、客户、产品、类型或单位不一致，不能盘点新增",
                409,
            )


def _semi_product_facts(product: Product) -> dict:
    material = product.material
    material_code = str(
        (material.code if material is not None else None)
        or product.default_material_code
        or product.legacy_material_text
        or ""
    ).strip()
    layer_count = product.layer_count or (
        material.layer_count if material is not None else None
    )
    flute_type = str(product.flute_type or "").strip().upper()
    board_length = product.report_length_mm or product.default_cardboard_length
    board_width = product.report_width_mm or product.default_cardboard_width
    if (
        not material_code
        or not layer_count
        or not flute_type
        or not board_length
        or not board_width
    ):
        raise WarehouseStocktakeBatchError(
            "该产品缺少材质、楞型或报料长宽，不能盘点新增半成品", 409
        )
    crease_text = str(product.crease_type or "").strip()
    return {
        "material_code": material_code,
        "material_id": product.material_id,
        "layer_count": int(layer_count),
        "flute_type": flute_type,
        "board_length_mm": int(board_length),
        "board_width_mm": int(board_width),
        "sheet_type": (
            "creased_sheet"
            if "压线" in crease_text
            else "net_sheet"
            if "净" in crease_text
            else "raw_board"
        ),
    }


def _preflight_add(
    db: Session, item: WarehouseStocktakeBatchItem
) -> None:
    if item.stock_stage not in {"complete", "body"} or (item.stock_stage == "body" and item.inventory_type != "finished"):
        raise WarehouseStocktakeBatchError("本体盘点类型无效", 422)
    expected_unit = {
        "finished": "boxes",
        "semi_finished": "sheets",
        "raw_material": "sheets",
    }.get(item.inventory_type or "")
    if (
        item.inventory_type is None
        or item.unit != expected_unit
        or item.customer_id is None
        or item.product_id is None
        or item.stock_date is None
        or item.lot_id is not None
        or item.expected_version is not None
        or item.source_kind not in {None, "existing_stocktake", "partner_transfer"}
    ):
        raise WarehouseStocktakeBatchError(
            "盘点新增字段不完整，且库存类型与单位必须严格匹配", 422
        )
    if item.quantity <= 0:
        raise WarehouseStocktakeBatchError("盘点新增数量必须大于0", 422)
    occupied_without_new_pallet = bool(
        db.scalar(
            select(InventoryLot.id)
            .where(
                InventoryLot.warehouse_location_id == item.location_id,
                InventoryLot.status.in_(("active", "frozen")),
                (
                    InventoryLot.quantity_available
                    + InventoryLot.quantity_reserved
                    + InventoryLot.quantity_damaged
                )
                > 0,
            )
            .limit(1)
        )
        or db.scalar(
            select(InventoryPallet.id)
            .where(
                InventoryPallet.location_id == item.location_id,
                InventoryPallet.is_current.is_(True),
            )
            .limit(1)
        )
    )
    location = _supported_formal_location(
        db,
        location_id=item.location_id,
        inventory_type=item.inventory_type,
        capacity_source_location_id=(
            item.location_id if occupied_without_new_pallet else None
        ),
    )
    if _is_dispatch_location(
        area_code=location.area_code,
        location_code=location.location_code,
    ):
        raise WarehouseStocktakeBatchError(
            "待送区不允许通过普通盘点直接新增库存", 409
        )
    _, product = _active_customer_product(
        db,
        customer_id=item.customer_id,
        product_id=item.product_id,
    )
    if item.inventory_type in {"semi_finished", "raw_material"}:
        _semi_product_facts(product)
    _assert_add_compatible(db, item=item)


def _preflight_decrease(
    db: Session, item: WarehouseStocktakeBatchItem
) -> None:
    if (
        item.lot_id is None
        or item.expected_version is None
        or item.inventory_type is not None
        or item.unit is not None
        or item.customer_id is not None
        or item.product_id is not None
        or item.stock_date is not None
        or item.source_kind is not None
        or item.stock_stage != "complete"
    ):
        raise WarehouseStocktakeBatchError(
            "盘点调减只允许填写货位、批次、版本和数量", 422
        )
    if item.quantity <= 0:
        raise WarehouseStocktakeBatchError("盘点调减数量必须大于0", 422)
    lot = load_stocktake_lot(db, item.lot_id)
    if lot.warehouse_location_id != item.location_id:
        raise WarehouseStocktakeBatchError(
            "库存批次实际货位与盘点草稿不一致，请刷新后重试", 409
        )
    if int(lot.version) != item.expected_version:
        raise WarehouseStocktakeBatchError(
            "库存已被其他操作更新，请刷新后重试", 409
        )
    _supported_formal_location(
        db,
        location_id=item.location_id,
        inventory_type=lot.inventory_type,
        capacity_source_location_id=item.location_id,
    )
    issue = stocktake_decrease_issues(db, [lot])[int(lot.id)]
    if issue is not None:
        raise WarehouseStocktakeBatchError(issue, 409)
    if item.quantity > int(lot.quantity_available or 0):
        raise WarehouseStocktakeBatchError(
            "盘点调减数量不能大于当前可用库存", 409
        )


def preflight_warehouse_stocktake_batch(
    db: Session, *, items: list[WarehouseStocktakeBatchItem]
) -> None:
    if not items:
        raise WarehouseStocktakeBatchError("盘点批次至少包含一项", 422)
    client_ids: set[str] = set()
    decreased_lot_ids: set[int] = set()
    for item in items:
        if item.client_item_id in client_ids:
            raise WarehouseStocktakeBatchError(
                "批次内 client_item_id 不能重复", 409
            )
        client_ids.add(item.client_item_id)
        if item.operation == "add":
            _preflight_add(db, item)
            assert item.inventory_type is not None
            assert item.unit is not None
            assert item.customer_id is not None
            assert item.product_id is not None
        else:
            _preflight_decrease(db, item)
            assert item.lot_id is not None
            if item.lot_id in decreased_lot_ids:
                raise WarehouseStocktakeBatchError(
                    "同一库存批次不能在一个盘点批次中重复调减", 409
                )
            decreased_lot_ids.add(item.lot_id)


def _claim_stocktake_batch_state(
    db: Session, *, items: list[WarehouseStocktakeBatchItem]
) -> None:
    """Acquire the writer slot with guarded no-op updates before any mutation.

    The first write claims every source lot and every add target using the exact
    state that passed preflight.  A second preflight after the claims protects
    against a change committed between the initial read and this writer claim.
    """

    expected_layout_versions: dict[int, int] = {}
    for item in items:
        previous = expected_layout_versions.setdefault(
            item.location_id, item.expected_layout_version
        )
        if previous != item.expected_layout_version:
            raise WarehouseStocktakeBatchError(
                "同一盘点货位提交了不同的地图版本，请刷新后重试", 409
            )
    for location_id, expected_layout_version in expected_layout_versions.items():
        try:
            claimed = claim_active_placed_location(
                db,
                location_id,
                expected_layout_version=expected_layout_version,
            )
        except OperationalError as error:
            raise WarehouseStocktakeBatchError(
                "盘点货位正在被其他库存或地图操作使用，请稍后重试", 409
            ) from error
        if not claimed:
            raise WarehouseStocktakeBatchError(
                "盘点货位已停用、尚未完成空间放置或地图版本已变化，请刷新后重试",
                409,
            )

    claimed_lot_ids: set[int] = set()
    for item in items:
        if item.operation == "decrease":
            assert item.lot_id is not None
            assert item.expected_version is not None
            if item.lot_id in claimed_lot_ids:
                continue
            claimed_lot_ids.add(item.lot_id)
            lot = load_stocktake_lot(db, item.lot_id)
            claimed = db.execute(
                update(InventoryLot)
                .where(
                    InventoryLot.id == lot.id,
                    InventoryLot.version == item.expected_version,
                    InventoryLot.warehouse_location_id == item.location_id,
                    InventoryLot.status == "active",
                    InventoryLot.inventory_type == lot.inventory_type,
                    InventoryLot.unit == lot.unit,
                    InventoryLot.quantity_available == lot.quantity_available,
                    InventoryLot.quantity_reserved == lot.quantity_reserved,
                    InventoryLot.quantity_consumed == lot.quantity_consumed,
                    InventoryLot.quantity_damaged == lot.quantity_damaged,
                    InventoryLot.quantity_scrapped == lot.quantity_scrapped,
                    InventoryLot.last_movement_at == lot.last_movement_at,
                    InventoryLot.source_type == lot.source_type,
                    InventoryLot.source_ref_type == lot.source_ref_type,
                    InventoryLot.source_ref_id == lot.source_ref_id,
                )
                .values(updated_at=InventoryLot.updated_at)
                .execution_options(synchronize_session=False)
            )
            if claimed.rowcount != 1:
                raise WarehouseStocktakeBatchError(
                    "库存数量、版本或业务绑定已变化，请刷新后重试", 409
                )
    db.flush()
    db.expire_all()
    preflight_warehouse_stocktake_batch(db, items=items)


def _movement_for_key(db: Session, key: str) -> InventoryMovement:
    movement = db.scalar(
        select(InventoryMovement).where(InventoryMovement.idempotency_key == key)
    )
    if movement is None:
        raise WarehouseStocktakeBatchError("盘点库存流水生成失败", 409)
    return movement


def _lot_identity(lot: InventoryLot) -> tuple[int | None, int | None]:
    if lot.finished_detail is not None:
        return (
            lot.finished_detail.owner_customer_id,
            lot.finished_detail.product_id,
        )
    if lot.semi_finished_detail is not None:
        product_ids = [row.product_id for row in lot.allowed_products]
        return (
            lot.semi_finished_detail.owner_customer_id,
            product_ids[0] if len(product_ids) == 1 else None,
        )
    return None, None


def _release_empty_linked_pallet(
    db: Session,
    *,
    lot: InventoryLot,
    batch_id: str,
    client_item_id: str,
    operator_id: int | None,
) -> int | None:
    if lot.pallet_item is None:
        return None
    pallet = lot.pallet_item.pallet
    if pallet is None or not pallet.is_current or pallet.location_id is None:
        return None
    # Pallet snapshots are still physical goods even though they are not formal
    # inventory lots.  Keep the authoritative delivery/release predicate here so
    # reducing the last linked lot to zero cannot hide a positive snapshot row.
    if _pallet_has_physical_goods(db, pallet.id):
        return None
    clear_key = "p147d-clear:" + sha256(
        f"{batch_id}:{client_item_id}:{pallet.id}".encode("utf-8")
    ).hexdigest()
    try:
        clear_pallet(
            db,
            pallet_id=pallet.id,
            expected_version=int(pallet.version),
            remarks="盘点调减至零后释放空栈板；库存批次与历史流水保留",
            operator_id=operator_id,
            idempotency_key=clear_key,
        )
    except Floor3LocationError as error:
        raise WarehouseStocktakeBatchError(str(error), error.status_code) from error
    return int(pallet.id)


def _execute_add(
    db: Session,
    *,
    batch_id: str,
    item: WarehouseStocktakeBatchItem,
    operator_id: int | None,
    subkey: str,
) -> dict:
    assert item.inventory_type is not None
    assert item.customer_id is not None
    assert item.product_id is not None
    assert item.stock_date is not None
    assert item.source_kind in {None, "existing_stocktake", "partner_transfer"}
    source_label = (
        "合作纸箱厂搬入"
        if item.source_kind == "partner_transfer"
        else "现场盘点发现"
    )
    if item.inventory_type == "finished":
        current_pallet = db.scalar(
            select(InventoryPallet).where(
                InventoryPallet.location_id == item.location_id,
                InventoryPallet.is_current.is_(True),
            )
        )
        lot = manual_finished_in(
            db,
            customer_id=item.customer_id,
            product_id=item.product_id,
            location_id=item.location_id,
            quantity=item.quantity,
            stock_date=item.stock_date,
            source_type="stocktake",
            stock_stage=item.stock_stage,
            remarks=f"{source_label}；盘点批次 {batch_id}",
            operator_id=operator_id,
            idempotency_key=subkey,
            pallet_id=current_pallet.id if current_pallet is not None else None,
            movement_reason=f"盘点新增（{source_label}）",
            expected_layout_version=item.expected_layout_version,
            remember_storage=False,
        )
    else:
        product = db.get(Product, item.product_id)
        assert product is not None
        facts = _semi_product_facts(product)
        # Raw boards and processed sheets share the existing sheet ledger;
        # sheet_type retains their physical stage without creating finished stock.
        sheet_type = facts["sheet_type"]
        if item.inventory_type == "raw_material":
            sheet_type = "raw_board"
        elif sheet_type == "raw_board":
            sheet_type = "net_sheet"
        occupied_location = bool(
            _location_live_lots(db, item.location_id)
            or db.scalar(
                select(InventoryPallet.id).where(
                    InventoryPallet.location_id == item.location_id,
                    InventoryPallet.is_current.is_(True),
                ).limit(1)
            )
        )
        lot = manual_semi_finished_in(
            db,
            location_id=item.location_id,
            quantity=item.quantity,
            stock_date=item.stock_date,
            source_type="stocktake",
            material_code=facts["material_code"],
            material_id=facts["material_id"],
            layer_count=facts["layer_count"],
            flute_type=facts["flute_type"],
            board_length_mm=facts["board_length_mm"],
            board_width_mm=facts["board_width_mm"],
            capacity_source_location_id=item.location_id if occupied_location else None,
            sheet_type=sheet_type,
            supplier_name=None,
            customer_id=item.customer_id,
            crease_type=product.crease_type,
            crease_left_mm=product.crease_left_mm,
            crease_middle_mm=product.crease_middle_mm,
            crease_right_mm=product.crease_right_mm,
            cutting_note=product.report_notes,
            remarks=f"{source_label}；盘点批次 {batch_id}",
            operator_id=operator_id,
            idempotency_key=subkey,
            movement_reason=f"盘点新增（{source_label}）",
            expected_layout_version=item.expected_layout_version,
        )
        lot = replace_semi_finished_lot_allowed_products(
            db,
            inventory_lot_id=lot.id,
            product_ids=[item.product_id],
            expected_version=int(lot.version),
            operator_id=operator_id,
        )
    db.flush()
    movement = _movement_for_key(db, subkey)
    customer_id, product_id = _lot_identity(lot)
    return {
        "client_item_id": item.client_item_id,
        "operation": item.operation,
        "lot_id": int(lot.id),
        "movement_id": int(movement.id),
        "location_id": int(lot.warehouse_location_id),
        "customer_id": customer_id,
        "product_id": product_id,
        "inventory_type": lot.inventory_type,
        "unit": lot.unit,
        "quantity_before": int(movement.before_available),
        "quantity_after": int(movement.after_available),
        "version_after": int(lot.version),
        "status_after": lot.status,
        "released_pallet_id": None,
        "source_kind": item.source_kind or "existing_stocktake",
        "stock_stage": item.stock_stage,
    }


def _execute_decrease(
    db: Session,
    *,
    batch_id: str,
    item: WarehouseStocktakeBatchItem,
    operator_id: int | None,
    subkey: str,
) -> dict:
    assert item.lot_id is not None
    assert item.expected_version is not None
    lot = mutate_lot(
        db,
        lot_id=item.lot_id,
        operation="adjust",
        expected_version=item.expected_version,
        operator_id=operator_id,
        quantity=-item.quantity,
        reason="盘点调减",
        idempotency_key=subkey,
    )
    db.flush()
    movement = _movement_for_key(db, subkey)
    released_pallet_id = None
    if int(lot.quantity_available or 0) == 0:
        lot.status = "closed"
        db.flush()
        released_pallet_id = _release_empty_linked_pallet(
            db,
            lot=lot,
            batch_id=batch_id,
            client_item_id=item.client_item_id,
            operator_id=operator_id,
        )
    customer_id, product_id = _lot_identity(lot)
    return {
        "client_item_id": item.client_item_id,
        "operation": item.operation,
        "lot_id": int(lot.id),
        "movement_id": int(movement.id),
        "location_id": int(lot.warehouse_location_id),
        "customer_id": customer_id,
        "product_id": product_id,
        "inventory_type": lot.inventory_type,
        "unit": lot.unit,
        "quantity_before": int(movement.before_available),
        "quantity_after": int(movement.after_available),
        "version_after": int(lot.version),
        "status_after": lot.status,
        "released_pallet_id": released_pallet_id,
    }


def execute_warehouse_stocktake_batch(
    db: Session,
    *,
    batch_id: str,
    items: list[WarehouseStocktakeBatchItem],
    operator_id: int | None,
) -> dict:
    preflight_warehouse_stocktake_batch(db, items=items)
    _claim_stocktake_batch_state(db, items=items)
    results: list[dict] = []
    for item in items:
        subkey = "p147d:" + sha256(
            f"{batch_id}:{item.client_item_id}:{item.operation}".encode("utf-8")
        ).hexdigest()
        try:
            if item.operation == "add":
                result = _execute_add(
                    db,
                    batch_id=batch_id,
                    item=item,
                    operator_id=operator_id,
                    subkey=subkey,
                )
            else:
                result = _execute_decrease(
                    db,
                    batch_id=batch_id,
                    item=item,
                    operator_id=operator_id,
                    subkey=subkey,
                )
        except Floor3LocationError as error:
            raise WarehouseStocktakeBatchError(str(error), error.status_code) from error
        except WarehouseInventoryError as error:
            raise WarehouseStocktakeBatchError(str(error), error.status_code) from error
        results.append(result)
    from app.services.receipt_putaway import remember_stocktake
    for result in results:
        if result["operation"] == "add" and result["inventory_type"] == "finished":
            remember_stocktake(db, db.get(InventoryLot, result["lot_id"]), operator_id)
    return {
        "message": "盘点批次已确认，正式库存与流水已原子提交",
        "batch_id": batch_id,
        "confirmed_at": beijing_now_naive().isoformat(),
        "operator_id": operator_id,
        "items": results,
    }
