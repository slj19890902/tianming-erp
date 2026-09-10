from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from hashlib import sha256
import json
import re
import unicodedata
from uuid import uuid4

from sqlalchemy import and_, case, delete, func, or_, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, selectinload

from app.core.time_contract import beijing_now_naive, beijing_today, utc_now_naive
from app.models.customer import Customer
from app.models.delivery import DeliveryItem
from app.models.material import Material
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.product_bom import (
    SalesOrderItemBomComponent,
    SalesOrderItemBomDemandAdjustment,
)
from app.models.production import ProductionCompletion
from app.models.requisition import RequisitionItem
from app.models.user import User
from app.models.warehouse_inventory import (
    DeliveryInventoryAllocation,
    Floor3LocationLayout,
    FinishedGoodsInventoryDetail,
    InventoryLot,
    InventoryLotTransfer,
    InventoryMovement,
    InventoryPallet,
    InventoryPalletItem,
    InventoryLocationMovement,
    InventoryReservation,
    OrderItemSemiRequirement,
    SemiFinishedInventoryDetail,
    SemiFinishedLotAllowedProduct,
    WarehouseArea,
    WarehouseAreaStoragePolicy,
    WarehouseGroundOccupancy,
    WarehouseGroundOccupancySlot,
    WarehouseGroundPlacementMutation,
    WarehouseFloor,
    WarehouseLocation,
)
from app.services.flute_mapping import seven_layer_code_error
from app.services.inventory_cost_snapshot import (
    apply_cost_snapshot,
    estimate_finished_product_cost,
    estimate_semi_finished_cost,
)
from app.services.location_candidates import (
    claim_active_placed_location,
    current_same_location_pallet,
    has_space_ledger,
    load_warehouse_location_projection_contexts,
    operational_location_issue,
    warehouse_location_projection,
)
from app.services.audit_log import append_audit_event
from app.services.warehouse_area_activation import WarehouseAreaActivationError
from app.services.warehouse_relocation_pending import (
    claim_pending_relocation_source, is_pending_relocation_location,
)
from app.services.warehouse_ground_plan_materialization import (
    ensure_one_step_ground_plan,
)
from app.services.warehouse_twin_layout import (
    WarehouseTwinLayoutNotFoundError,
    load_warehouse_twin_floor,
)


class WarehouseInventoryError(ValueError):
    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


def _claim_inventory_destination(
    db: Session,
    location_id: int,
    *,
    expected_layout_version: int | None = None,
) -> None:
    try:
        claimed = claim_active_placed_location(
            db,
            location_id,
            expected_layout_version=expected_layout_version,
        )
    except OperationalError as error:
        raise WarehouseInventoryError(
            "目标库位正在被其他入库、移位或布局操作使用，请稍后重试", 409
        ) from error
    if not claimed:
        raise WarehouseInventoryError(
            "目标库位已停用、尚未完成空间放置或地图状态已变化，请刷新后重试",
            409,
        )


def _claim_inventory_restore_destination(db: Session, location_id: int) -> None:
    """Claim the current placement when undoing an audited stock outflow.

    Dispatch may empty and release the original pallet. Such a slot no longer
    has live inventory to satisfy the ordinary no-token destination claim.
    Read its placement version and claim it atomically; callers still verify
    the original allocation, pallet identity and occupancy before restoring.
    """
    layout_version = db.scalar(
        select(Floor3LocationLayout.version).where(
            Floor3LocationLayout.location_id == location_id
        )
    )
    _claim_inventory_destination(
        db, location_id, expected_layout_version=layout_version
    )


def _claim_inventory_transfer_locations(
    db: Session,
    *,
    source_location_id: int,
    target_location_id: int,
    expected_source_layout_version: int | None,
    expected_target_layout_version: int | None,
    require_pending_source: bool = False,
) -> None:
    """Serialize both sides of a move in one stable floor/location order.

    Opposite moves (A -> B and B -> A) must claim the same rows in the same
    order.  The location no-op also shares the floor projection mutex used by
    map publishing.  Existing pallet and ground-occupancy rows are claimed
    afterwards because multiple lots may share those physical containers.
    """

    requested_ids = {int(source_location_id), int(target_location_id)}
    rows = list(
        db.execute(
            select(WarehouseLocation.id, WarehouseLocation.warehouse_floor).where(
                WarehouseLocation.id.in_(requested_ids)
            )
        ).all()
    )
    if {int(row.id) for row in rows} != requested_ids:
        raise WarehouseInventoryError("来源或目标库位不存在，请刷新后重试", 409)
    layout_versions = {
        int(source_location_id): expected_source_layout_version,
        int(target_location_id): expected_target_layout_version,
    }
    for row in sorted(
        rows,
        key=lambda item: (
            int(item.warehouse_floor) if item.warehouse_floor is not None else 10_000,
            int(item.id),
        ),
    ):
        if require_pending_source and int(row.id) == int(source_location_id):
            if not claim_pending_relocation_source(db, int(row.id)):
                raise WarehouseInventoryError("来源已不是盘点待归位，请刷新后重试", 409)
            continue
        _claim_inventory_destination(
            db,
            int(row.id),
            expected_layout_version=layout_versions[int(row.id)],
        )

    try:
        pallet_ids = list(
            db.scalars(
                select(InventoryPallet.id)
                .where(
                    InventoryPallet.location_id.in_(requested_ids),
                    InventoryPallet.is_current.is_(True),
                )
                .order_by(InventoryPallet.id)
            ).all()
        )
        for pallet_id in pallet_ids:
            db.execute(
                update(InventoryPallet)
                .where(
                    InventoryPallet.id == int(pallet_id),
                    InventoryPallet.is_current.is_(True),
                )
                .values(
                    status=InventoryPallet.status,
                    updated_at=InventoryPallet.updated_at,
                )
                .execution_options(synchronize_session=False)
            )

        occupancy_ids = list(
            db.scalars(
                select(WarehouseGroundOccupancy.id)
                .outerjoin(
                    WarehouseGroundOccupancySlot,
                    WarehouseGroundOccupancySlot.occupancy_id
                    == WarehouseGroundOccupancy.id,
                )
                .where(
                    WarehouseGroundOccupancy.status == "active",
                    or_(
                        WarehouseGroundOccupancy.primary_location_id.in_(requested_ids),
                        and_(
                            WarehouseGroundOccupancySlot.location_id.in_(requested_ids),
                            WarehouseGroundOccupancySlot.status == "active",
                        ),
                    ),
                )
                .distinct()
                .order_by(WarehouseGroundOccupancy.id)
            ).all()
        )
        for occupancy_id in occupancy_ids:
            # Occupancy facts only permit release/restore transitions, including
            # rejecting no-op UPDATEs. The floor/location claims above already
            # hold SQLite's writer lock; row-locking backends use FOR UPDATE.
            db.execute(
                select(WarehouseGroundOccupancy)
                .where(
                    WarehouseGroundOccupancy.id == int(occupancy_id),
                    WarehouseGroundOccupancy.status == "active",
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            ).scalar_one_or_none()
    except OperationalError as error:
        raise WarehouseInventoryError(
            "来源或目标位置正在被其他盘点、移位或布局操作使用，请稍后重试",
            409,
        ) from error


def _validate_transfer_location_snapshot(
    db: Session,
    *,
    location: WarehouseLocation,
    role: str,
    expected_address_version: int | None,
    expected_layout_version: int | None,
    expected_map_revision: str | None,
) -> None:
    """Reject a stale phone map after the location/floor mutex is held."""

    if (
        expected_address_version is not None
        and int(location.address_version or 1) != int(expected_address_version)
    ):
        raise WarehouseInventoryError(f"{role}货位地址已调整，请刷新地图后重试", 409)
    if expected_layout_version is None and expected_map_revision is None:
        return
    context = load_warehouse_location_projection_contexts(db, [location]).get(
        int(location.id), {}
    )
    layout = context.get("layout")
    live_layout_version = (
        int(layout.version) if isinstance(layout, Floor3LocationLayout) else None
    )
    if (
        expected_layout_version is not None
        and live_layout_version != int(expected_layout_version)
    ):
        raise WarehouseInventoryError(f"{role}货位布局已调整，请刷新地图后重试", 409)
    projection = warehouse_location_projection(location, **context)
    live_map_revision = str(projection.get("published_map_revision") or "").strip()
    expected_revision = str(expected_map_revision or "").strip()
    if expected_revision and (
        projection.get("position_status") != "mapped"
        or live_map_revision != expected_revision
    ):
        raise WarehouseInventoryError(f"{role}货位地图版本已更新，请刷新后重试", 409)


def _delivery_pallet_release_key(delivery_id: int, pallet_id: int) -> str:
    return f"delivery-{delivery_id}-auto-release-pallet-{pallet_id}"


def _pallet_has_physical_goods(db: Session, pallet_id: int) -> bool:
    snapshot_exists = db.scalar(
        select(InventoryPalletItem.id)
        .where(
            InventoryPalletItem.pallet_id == pallet_id,
            InventoryPalletItem.inventory_lot_id.is_(None),
            InventoryPalletItem.quantity > 0,
        )
        .limit(1)
    )
    if snapshot_exists is not None:
        return True
    live_lot_exists = db.scalar(
        select(InventoryLot.id)
        .join(
            InventoryPalletItem,
            InventoryPalletItem.inventory_lot_id == InventoryLot.id,
        )
        .where(
            InventoryPalletItem.pallet_id == pallet_id,
            (
                InventoryLot.quantity_available
                + InventoryLot.quantity_reserved
                + InventoryLot.quantity_damaged
            )
            > 0,
        )
        .limit(1)
    )
    return live_lot_exists is not None


def _finished_lot_physical_quantity(lot: InventoryLot) -> int:
    return max(
        int(lot.quantity_available or 0)
        + int(lot.quantity_reserved or 0)
        + int(lot.quantity_damaged or 0),
        0,
    )


def _ensure_finished_projection_postcondition(
    db: Session,
    *,
    lot: InventoryLot,
    operator_id: int | None,
    create_missing: bool,
    pallet_id: int | None = None,
    pallet_code: str | None = None,
    require_empty_pallet: bool = False,
    ground_secondary_location_id: int | None = None,
    ground_capacity_quantity: int | None = None,
) -> InventoryPallet | None:
    """Keep every positive finished fact on one authoritative map projection.

    Ground and temporary-aisle stock must stay on a real pallet.  Rack stock is
    represented as a loose lot in one published rack cell, because a rack cell
    is already the physical container.  Historical projection conflicts remain
    fail-closed instead of being silently rewritten.
    """

    if lot.inventory_type != "finished" or _finished_lot_physical_quantity(lot) <= 0:
        return None
    from app.services.fixed_shelf import ShelfError, assert_destination
    try:
        assert_destination(db, lot)
    except ShelfError as error:
        raise WarehouseInventoryError(str(error), 409) from error
    if lot.warehouse_location_id is None:
        raise WarehouseInventoryError("成品库存缺少正式库位，不能完成空间投影", 409)
    location = db.get(WarehouseLocation, int(lot.warehouse_location_id))
    if location is None:
        raise WarehouseInventoryError("成品库存缺少正式库位，不能完成空间投影", 409)
    space_ledger_exists = has_space_ledger(db)
    projection_required = space_ledger_exists or str(
        location.source_version or ""
    ).strip().upper() == "V11"
    if not projection_required:
        return None

    current_pallet = current_same_location_pallet(lot)
    capacity_source_location_id: int | None = None
    projection_pallet = current_pallet
    if projection_pallet is None and pallet_id is not None:
        supplied_pallet = db.get(InventoryPallet, int(pallet_id))
        if (
            supplied_pallet is not None
            and supplied_pallet.is_current
            and str(supplied_pallet.status or "").strip().lower() == "active"
            and supplied_pallet.location_id == int(location.id)
        ):
            projection_pallet = supplied_pallet
    if projection_pallet is not None:
        existing_ground_projection = db.scalar(
            select(WarehouseGroundOccupancySlot.id)
            .join(
                WarehouseGroundOccupancy,
                WarehouseGroundOccupancy.id
                == WarehouseGroundOccupancySlot.occupancy_id,
            )
            .where(
                WarehouseGroundOccupancy.pallet_id == int(projection_pallet.id),
                WarehouseGroundOccupancy.primary_location_id == int(location.id),
                WarehouseGroundOccupancy.status == "active",
                WarehouseGroundOccupancySlot.location_id == int(location.id),
                WarehouseGroundOccupancySlot.status == "active",
            )
            .limit(1)
        )
        if existing_ground_projection is not None:
            capacity_source_location_id = int(location.id)

    context: dict[str, object | None] = {}
    if space_ledger_exists:
        context = load_warehouse_location_projection_contexts(db, [location]).get(
            int(location.id), {}
        )
        issue = operational_location_issue(
            db,
            location,
            warehouse_types={"finished", "shared"},
            require_published=True,
            required_inventory_type="finished",
            capacity_source_location_id=capacity_source_location_id,
            projection_context=context,
        )
        if issue:
            raise WarehouseInventoryError(
                f"{issue}，不能形成成品库存空间投影",
                409,
            )

    storage_type = str(location.storage_type or "").strip().lower()
    if space_ledger_exists and storage_type == "rack":
        if lot.pallet_item is not None:
            raise WarehouseInventoryError(
                "货架成品库存不能同时绑定实体栈板，请先核对历史空间事实",
                409,
            )
        return None
    if space_ledger_exists and storage_type not in {"ground", "temporary_aisle"}:
        raise WarehouseInventoryError(
            "该成品库位的存储方式不支持形成正式空间投影",
            409,
        )
    if current_pallet is None:
        if not create_missing:
            raise WarehouseInventoryError(
                "已完成的成品库存缺少同库位当前真实栈板，请先核对历史数据",
                409,
            )
        if lot.pallet_item is not None:
            raise WarehouseInventoryError(
                "成品库存仍绑定历史或异位栈板，不能自动改写空间事实",
                409,
            )
        from app.services.floor3_locations import (
            Floor3LocationError,
            bind_finished_lot_to_floor3_pallet,
        )

        try:
            bind_finished_lot_to_floor3_pallet(
                db,
                lot=lot,
                operator_id=operator_id,
                pallet_id=pallet_id,
                pallet_code=pallet_code,
                require_empty_pallet=require_empty_pallet,
                allow_operational_location=has_space_ledger(db),
            )
        except Floor3LocationError as error:
            raise WarehouseInventoryError(str(error), error.status_code) from error
        db.flush()
        db.expire(lot, ["pallet_item"])
        current_pallet = current_same_location_pallet(lot)
    if current_pallet is None:
        raise WarehouseInventoryError(
            "成品库存未能形成同库位当前真实栈板，事务已停止",
            409,
        )

    pallet_item = lot.pallet_item
    if pallet_item is None:
        raise WarehouseInventoryError("成品库存缺少真实栈板明细，事务已停止", 409)
    expected_item_quantity = _finished_lot_physical_quantity(lot)
    if int(pallet_item.quantity or 0) != expected_item_quantity:
        if not create_missing:
            raise WarehouseInventoryError(
                "已完成的成品库存与真实栈板数量不一致，请先核对历史数据",
                409,
            )
        pallet_item.quantity = expected_item_quantity
        current_pallet.version = int(current_pallet.version or 0) + 1
        current_pallet.updated_by = operator_id
        db.flush()

    if not space_ledger_exists:
        # Pre-space-ledger test/installations retain the historical V11 pallet
        # projection.  Formal databases always have the floor/area ledger and
        # therefore cannot use this compatibility branch.
        return current_pallet

    ground_layout = context.get("ground_layout")
    published_identity = context.get("published_floor_identity")
    area_code = str(location.area_code or "").strip().upper()
    legacy_v11_compat = bool(
        str(location.source_version or "").strip().upper() == "V11"
        and int(location.warehouse_floor or 0) == 3
        and context.get("policy") is None
        and context.get("layout") is not None
        and isinstance(published_identity, dict)
        and str(published_identity.get("revision") or "").strip()
        and len(
            tuple(
                (published_identity.get("zone_ids_by_area") or {}).get(
                    area_code, ()
                )
            )
        )
        == 1
    )
    if legacy_v11_compat:
        # V11 is the accepted legacy third-floor map itself.  It has no area
        # policy/ground-plan lifecycle; adding a second occupancy projection
        # would recreate the dual-source bug this gate removes.
        return current_pallet
    if _current_map_temporary_finished_anchor_issue(location, context) is None:
        active_occupancy = db.scalar(
            select(WarehouseGroundOccupancy.id)
            .join(
                WarehouseGroundOccupancySlot,
                WarehouseGroundOccupancySlot.occupancy_id
                == WarehouseGroundOccupancy.id,
            )
            .where(
                WarehouseGroundOccupancySlot.location_id == int(location.id),
                WarehouseGroundOccupancySlot.status == "active",
                WarehouseGroundOccupancy.status == "active",
            )
            .limit(1)
        )
        if active_occupancy is not None:
            raise WarehouseInventoryError(
                "临时周转锚点不能同时存在虚构地堆占用，请先核对空间事实",
                409,
            )
        if not current_pallet.needs_relocation:
            raise WarehouseInventoryError(
                "临时周转栈板必须标记待归位，当前空间事实不完整",
                409,
            )
        # F34/F12 are owner-confirmed temporary corridor anchors.  They own a
        # real location and pallet but deliberately have no measured 1200x1000
        # ground slot.  Creating a fake ground occupancy here would turn a
        # temporary corridor into invented long-term capacity.
        return current_pallet
    if not isinstance(ground_layout, dict):
        raise WarehouseInventoryError(
            "地堆成品位置缺少当前发布排位，不能形成真实空间占用",
            409,
        )
    active_occupancies = list(
        db.scalars(
        select(WarehouseGroundOccupancy)
        .where(
            WarehouseGroundOccupancy.pallet_id == int(current_pallet.id),
            WarehouseGroundOccupancy.status == "active",
        )
        .options(selectinload(WarehouseGroundOccupancy.slots))
        ).all()
    )
    if len(active_occupancies) > 1:
        raise WarehouseInventoryError("真实栈板存在重复活动地堆占用", 409)
    existing_occupancy = active_occupancies[0] if active_occupancies else None
    pallet_physical_quantity = int(
        db.scalar(
            select(
                func.coalesce(
                    func.sum(
                        InventoryLot.quantity_available
                        + InventoryLot.quantity_reserved
                        + InventoryLot.quantity_damaged
                    ),
                    0,
                )
            )
            .join(
                InventoryPalletItem,
                InventoryPalletItem.inventory_lot_id == InventoryLot.id,
            )
            .where(
                InventoryPalletItem.pallet_id == int(current_pallet.id),
                InventoryLot.status.in_(("active", "frozen")),
            )
        )
        or 0
    )
    if pallet_physical_quantity <= 0:
        raise WarehouseInventoryError("真实栈板没有正数成品，不能建立地堆占用", 409)
    if existing_occupancy is not None:
        active_location_ids = {
            int(slot.location_id)
            for slot in existing_occupancy.slots
            if slot.status == "active"
        }
        if (
            int(existing_occupancy.primary_location_id) != int(location.id)
            or int(location.id) not in active_location_ids
        ):
            raise WarehouseInventoryError("真实栈板的地堆占用与库存库位不一致", 409)
        if int(existing_occupancy.capacity_quantity) < pallet_physical_quantity:
            if not create_missing:
                raise WarehouseInventoryError(
                    "已完成的地堆成品数量超过活动空间占用容量，请先核对历史数据",
                    409,
                )
            controlled_capacity = db.scalar(
                select(WarehouseGroundPlacementMutation.id)
                .where(
                    WarehouseGroundPlacementMutation.occupancy_id
                    == int(existing_occupancy.id)
                )
                .limit(1)
            )
            if controlled_capacity is not None:
                raise WarehouseInventoryError(
                    "地堆位置现场容量不足，不能增加成品数量",
                    409,
                )
            if operator_id is None:
                raise WarehouseInventoryError("更新自动空间投影必须记录操作人", 409)
            secondary_ids = active_location_ids - {int(location.id)}
            if (len(secondary_ids) > 1 or existing_occupancy.footprint_kind
                    != ("double" if secondary_ids else "single")):
                raise WarehouseInventoryError("现有空间占用范围不完整，请先核对", 409)
            existing_secondary_id = next(iter(secondary_ids), None)
            if ground_secondary_location_id is not None and int(ground_secondary_location_id) != existing_secondary_id:
                raise WarehouseInventoryError("补入货物不能改变现有空间占用范围", 409)
            # This legacy automatic projection was sized from stock, not an
            # operator-confirmed capacity. Keep its immutable fact and the same
            # occupied slots, then create the replacement below in this transaction.
            from app.services.warehouse_ground_slots import release_ground_occupancy_for_pallet
            release_ground_occupancy_for_pallet(db, pallet_id=current_pallet.id, operator_id=operator_id)
            ground_secondary_location_id = existing_secondary_id
        else:
            return current_pallet
    if not create_missing:
        raise WarehouseInventoryError(
            "已完成的地堆成品缺少活动空间占用，请先核对历史数据",
            409,
        )
    if operator_id is None:
        raise WarehouseInventoryError("建立真实地堆占用必须记录操作人", 409)
    occupancy_location_ids = [int(location.id)]
    if ground_secondary_location_id is not None:
        secondary_location_id = int(ground_secondary_location_id)
        if secondary_location_id == int(location.id):
            raise WarehouseInventoryError("地堆双位不能重复选择同一库位", 409)
        secondary_location = db.get(WarehouseLocation, secondary_location_id)
        if secondary_location is None:
            raise WarehouseInventoryError("地堆第二库位不存在", 409)
        secondary_context = load_warehouse_location_projection_contexts(
            db, [secondary_location]
        ).get(secondary_location_id, {})
        secondary_issue = operational_location_issue(
            db,
            secondary_location,
            warehouse_types={"finished", "shared"},
            require_published=True,
            required_inventory_type="finished",
            projection_context=secondary_context,
        )
        secondary_ground_layout = secondary_context.get("ground_layout")
        if secondary_issue:
            raise WarehouseInventoryError(
                f"{secondary_issue}，不能作为地堆第二库位",
                409,
            )
        if (
            not isinstance(secondary_ground_layout, dict)
            or int(secondary_ground_layout.get("plan_id") or 0)
            != int(ground_layout.get("plan_id") or 0)
        ):
            raise WarehouseInventoryError("地堆双位必须属于同一当前发布排位", 409)
        occupancy_location_ids.append(secondary_location_id)
    conflicting_occupancy = db.scalar(
        select(WarehouseGroundOccupancySlot.id)
        .join(
            WarehouseGroundOccupancy,
            WarehouseGroundOccupancy.id
            == WarehouseGroundOccupancySlot.occupancy_id,
        )
        .where(
            WarehouseGroundOccupancySlot.location_id.in_(occupancy_location_ids),
            WarehouseGroundOccupancySlot.status == "active",
            WarehouseGroundOccupancy.status == "active",
        )
        .limit(1)
    )
    if conflicting_occupancy is not None:
        raise WarehouseInventoryError("地堆位置已有其它活动空间占用", 409)
    detail = lot.finished_detail
    if detail is None:
        raise WarehouseInventoryError("成品库存缺少产品明细，不能建立地堆占用", 409)
    occupancy_customer_id = detail.owner_customer_id or db.scalar(
        select(Product.customer_id).where(Product.id == int(detail.product_id))
    )
    if occupancy_customer_id is None:
        raise WarehouseInventoryError("成品库存缺少客户归属，不能建立地堆占用", 409)
    occupancy_capacity = (
        int(ground_capacity_quantity)
        if ground_capacity_quantity is not None
        else pallet_physical_quantity
    )
    if occupancy_capacity < pallet_physical_quantity:
        raise WarehouseInventoryError("地堆位置现场容量不足，不能存放本次成品", 409)
    occupancy = WarehouseGroundOccupancy(
        pallet_id=int(current_pallet.id),
        primary_location_id=int(location.id),
        customer_id=int(occupancy_customer_id),
        product_id=int(detail.product_id),
        footprint_kind="double" if len(occupancy_location_ids) == 2 else "single",
        capacity_quantity=occupancy_capacity,
        status="active",
        version=1,
        created_by=int(operator_id),
    )
    db.add(occupancy)
    db.flush()
    db.add_all(
        [
            WarehouseGroundOccupancySlot(
                occupancy_id=int(occupancy.id),
                location_id=occupancy_location_id,
                slot_sequence=sequence,
                status="active",
            )
            for sequence, occupancy_location_id in enumerate(
                occupancy_location_ids, start=1
            )
        ]
    )
    db.flush()
    return current_pallet


def release_empty_pallets_after_delivery(
    db: Session,
    *,
    delivery_id: int,
    operator_id: int | None,
) -> list[int]:
    """Release only pallets emptied by this formal delivery transaction."""
    db.flush()
    pallet_ids = list(
        db.scalars(
            select(InventoryPalletItem.pallet_id)
            .join(
                InventoryMovement,
                InventoryMovement.inventory_lot_id
                == InventoryPalletItem.inventory_lot_id,
            )
            .where(
                InventoryMovement.related_delivery_id == delivery_id,
                InventoryMovement.movement_type == "consume",
            )
            .distinct()
            .order_by(InventoryPalletItem.pallet_id)
        )
    )
    if not pallet_ids:
        return []

    from app.services.floor3_locations import Floor3LocationError, clear_pallet
    from app.services.warehouse_ground_slots import (
        release_ground_occupancy_for_pallet,
    )

    released: list[int] = []
    for pallet_id in pallet_ids:
        pallet = db.get(InventoryPallet, pallet_id)
        if pallet is None or not pallet.is_current or pallet.location_id is None:
            continue
        if _pallet_has_physical_goods(db, pallet_id):
            continue
        try:
            clear_pallet(
                db,
                pallet_id=pallet_id,
                expected_version=int(pallet.version),
                remarks=f"送货单 {delivery_id} 正式发货后货物清零，自动释放空栈板",
                operator_id=operator_id,
                idempotency_key=_delivery_pallet_release_key(delivery_id, pallet_id),
                allow_non_operational_source=True,
            )
        except Floor3LocationError as error:
            raise WarehouseInventoryError(str(error), error.status_code) from error
        release_ground_occupancy_for_pallet(
            db,
            pallet_id=int(pallet_id),
            operator_id=operator_id,
        )
        released.append(int(pallet_id))
    db.flush()
    return released


def restore_auto_released_pallets_after_delivery_cancel(
    db: Session,
    *,
    delivery_id: int,
    operator_id: int | None,
) -> list[int]:
    """Restore pallet projection when cancelling a dispatch restores its stock."""
    db.flush()
    prefix = f"delivery-{delivery_id}-auto-release-pallet-"
    clear_movements = list(
        db.scalars(
            select(InventoryLocationMovement)
            .where(
                InventoryLocationMovement.movement_type == "clear",
                InventoryLocationMovement.idempotency_key.like(f"{prefix}%"),
            )
            .order_by(InventoryLocationMovement.pallet_id)
        )
    )
    from app.services.warehouse_ground_slots import (
        WarehouseGroundSlotError,
        restore_ground_occupancy_for_pallet,
    )

    restored: list[int] = []
    for clear_movement in clear_movements:
        pallet = db.get(InventoryPallet, clear_movement.pallet_id)
        if pallet is None or not _pallet_has_physical_goods(db, pallet.id):
            continue
        restore_key = f"{clear_movement.idempotency_key}:restore"
        existing = db.scalar(
            select(InventoryLocationMovement).where(
                InventoryLocationMovement.idempotency_key == restore_key
            )
        )
        if existing is not None:
            if existing.pallet_id != pallet.id or existing.movement_type != "move":
                raise WarehouseInventoryError("栈板恢复幂等键已用于其他业务", 409)
            continue
        target_location_id = clear_movement.from_location_id
        if target_location_id is None:
            raise WarehouseInventoryError("空栈板缺少原库位，无法安全取消发货", 409)
        _claim_inventory_restore_destination(db, int(target_location_id))
        target_location = db.get(WarehouseLocation, target_location_id)
        if target_location is None or not target_location.is_active:
            raise WarehouseInventoryError("空栈板原库位已停用，无法安全取消发货", 409)
        if pallet.is_current or pallet.location_id is not None:
            raise WarehouseInventoryError("空栈板状态已变化，无法安全取消发货", 409)
        occupied = db.scalar(
            select(InventoryPallet.id)
            .where(
                InventoryPallet.location_id == target_location_id,
                InventoryPallet.is_current.is_(True),
                InventoryPallet.location_occupancy_key
                == pallet.location_occupancy_key,
                InventoryPallet.id != pallet.id,
            )
            .limit(1)
        )
        if occupied is not None:
            raise WarehouseInventoryError(
                "原库位已被其他栈板占用，无法取消发货；请先腾空原库位后重试",
                409,
            )
        misplaced_lot = db.scalar(
            select(InventoryLot.id)
            .join(
                InventoryPalletItem,
                InventoryPalletItem.inventory_lot_id == InventoryLot.id,
            )
            .where(
                InventoryPalletItem.pallet_id == pallet.id,
                or_(
                    InventoryLot.warehouse_location_id.is_(None),
                    InventoryLot.warehouse_location_id != target_location_id,
                ),
                (
                    InventoryLot.quantity_available
                    + InventoryLot.quantity_reserved
                    + InventoryLot.quantity_damaged
                )
                > 0,
            )
            .limit(1)
        )
        if misplaced_lot is not None:
            raise WarehouseInventoryError("恢复库存位置与原栈板不一致，无法安全取消发货", 409)

        version_before = int(pallet.version)
        changed = db.execute(
            update(InventoryPallet)
            .where(
                InventoryPallet.id == pallet.id,
                InventoryPallet.version == version_before,
                InventoryPallet.is_current.is_(False),
                InventoryPallet.location_id.is_(None),
            )
            .values(
                location_id=target_location_id,
                status="active",
                is_current=True,
                needs_relocation=bool(target_location.is_temporary),
                version=InventoryPallet.version + 1,
                closed_at=None,
                updated_by=operator_id,
            )
            .execution_options(synchronize_session=False)
        )
        if changed.rowcount != 1:
            raise WarehouseInventoryError("空栈板状态已变化，请刷新后重试", 409)
        now = beijing_now_naive()
        db.add(
            InventoryLocationMovement(
                pallet_id=pallet.id,
                from_location_id=None,
                to_location_id=target_location_id,
                movement_type="move",
                operator_id=operator_id,
                moved_at=now,
                idempotency_key=restore_key,
                confirmed_at=now,
                pallet_version_before=version_before,
                pallet_version_after=version_before + 1,
                remarks=f"取消送货单 {delivery_id}，恢复原库存与栈板位置",
            )
        )
        try:
            restore_ground_occupancy_for_pallet(
                db,
                pallet_id=int(pallet.id),
                location_id=int(target_location_id),
            )
        except WarehouseGroundSlotError as error:
            raise WarehouseInventoryError(error.message, error.status_code) from error
        restored.append(int(pallet.id))
    db.flush()
    return restored


STOCK_DATE_ACCURACIES = frozenset({"exact", "estimated", "unknown"})


SEMI_FINISHED_FLUTES_BY_LAYER: dict[int, frozenset[str]] = {
    3: frozenset({"A", "B", "E"}),
    5: frozenset({"AB", "BE"}),
    7: frozenset({"AAA", "ABC"}),
}


RAW_MATERIAL_STAGING_WAREHOUSE_TYPES = frozenset({"semi_finished", "shared"})
CURRENT_MAP_LEFT_RAW_SEMI_AREA_CODES = frozenset(
    {"RAW-001", "RAW-004", "SEMI-006", "SEMI-010", "SEMI-011"}
)
CURRENT_MAP_LEFT_FINISHED_AREA_CODES = frozenset(
    {"FG-004", "FG-005", "FG-006", "FG-007", "FG-008", "FG-009"}
)
CURRENT_MAP_TEMPORARY_FINISHED_AREA_CODES = ("F34", "F12")
CURRENT_MAP_TEMPORARY_FINISHED_LOCATION_CODES = tuple(
    [*(f"F34-P{number:02d}" for number in range(1, 4))]
    + [*(f"F12-P{number:02d}" for number in range(1, 9))]
)
RAW_MATERIAL_STAGING_STORAGE_TYPES = frozenset(
    {"ground", "temporary_aisle", "rack"}
)


@dataclass(frozen=True)
class AgeWarning:
    days: int | None
    level: str | None
    text: str | None


@dataclass(frozen=True)
class FinishedReservationMutation:
    reservation: InventoryReservation
    movement: InventoryMovement
    allocation: DeliveryInventoryAllocation | None = None


@dataclass(frozen=True)
class FinishedLotLocationTransferResult:
    transfer: InventoryLotTransfer
    source_lot: InventoryLot
    target_lot: InventoryLot
    replayed: bool


# Compatibility export for service modules outside N033's write scope.
utc_now = utc_now_naive


def normalize_material_code(value: str | None) -> str:
    normalized = unicodedata.normalize("NFKC", value or "").upper()
    normalized = re.sub(r"\s+", "", normalized)
    if not normalized:
        raise WarehouseInventoryError("材质代码不能为空")
    return normalized


def normalize_stock_date_metadata(
    *,
    stock_date: date,
    stock_date_accuracy: str = "exact",
    stock_date_original_text: str | None = None,
) -> tuple[str, str | None]:
    accuracy = (stock_date_accuracy or "").strip().lower()
    if accuracy not in STOCK_DATE_ACCURACIES:
        raise WarehouseInventoryError(
            "入库日期可信度必须为 exact、estimated 或 unknown"
        )
    original = (stock_date_original_text or "").strip() or None
    if accuracy == "exact":
        if original is not None:
            try:
                parsed = date.fromisoformat(original)
            except ValueError as error:
                raise WarehouseInventoryError(
                    "精确入库日期原文必须使用 YYYY-MM-DD"
                ) from error
            if parsed != stock_date:
                raise WarehouseInventoryError(
                    "精确入库日期原文与入库日期不一致"
                )
        original = original or stock_date.isoformat()
    elif accuracy == "unknown":
        # A non-null marker distinguishes a deliberate post-migration
        # "unknown" fact from legacy rows backfilled as unknown + NULL.
        original = original or "未提供"
    return accuracy, original


def inventory_age_warning(lot: InventoryLot, *, today: date | None = None) -> AgeWarning:
    accuracy = getattr(lot, "stock_date_accuracy", "exact") or "unknown"
    if accuracy == "unknown":
        return AgeWarning(None, "unknown", "入库日期不明，不能按精确库龄判断")
    current = today or beijing_today()
    days = max((current - lot.stock_date).days, 0)
    estimated_prefix = "估算" if accuracy == "estimated" else ""
    if days >= 730:
        return AgeWarning(days, "cleanup", f"{estimated_prefix}库龄超过2年，请盘点并处理")
    if days >= 548:
        return AgeWarning(days, "handling", f"{estimated_prefix}库龄超过18个月，请安排处理")
    if days >= 365:
        return AgeWarning(days, "attention", f"{estimated_prefix}库龄超过1年，请重点关注")
    if accuracy == "estimated":
        return AgeWarning(days, "estimated", "入库日期为估算值，库龄仅供参考")
    return AgeWarning(days, None, None)


def _number(prefix: str) -> str:
    return f"{prefix}-{beijing_now_naive():%Y%m%d}-{uuid4().hex[:10].upper()}"


def _is_raw_material_staging_location(
    db: Session,
    location: WarehouseLocation,
    *,
    projection_context: dict[str, object] | None = None,
) -> bool:
    """Return whether a location is a current-map board staging point."""

    location_code = (location.location_code or "").strip().upper()
    if (
        not location.is_active
        or location.warehouse_type not in RAW_MATERIAL_STAGING_WAREHOUSE_TYPES
        or location.storage_type not in RAW_MATERIAL_STAGING_STORAGE_TYPES
        or location.placement_status != "placed"
        or location.source_version == "V11"
        or location_code == "F1-DISPATCH-01"
    ):
        return False
    context = projection_context or {}
    floor = context.get("floor")
    if floor is None:
        floor = db.scalar(
            select(WarehouseFloor).where(
                WarehouseFloor.floor_number == location.warehouse_floor
            )
        )
    if floor is None or floor.construction_status != "enabled":
        return False
    area = context.get("area")
    if area is None:
        area = db.scalar(
            select(WarehouseArea).where(
                WarehouseArea.floor_id == floor.id,
                WarehouseArea.area_code == location.area_code,
            )
        )
    if area is None or area.construction_status != "enabled":
        return False
    return True


def _raw_material_staging_policy_inventory_type(
    projection_context: dict[str, object] | None,
) -> str | None:
    """Return the policy usage that authorizes customer board staging."""

    policy = (projection_context or {}).get("policy")
    if policy is None or getattr(policy, "status", None) != "published":
        return None
    try:
        values = json.loads(policy.allowed_inventory_types_json)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        return None
    allowed = {value.strip() for value in values if value.strip()}
    if "raw_material" in allowed:
        return "raw_material"
    if "semi_finished" in allowed:
        return "semi_finished"
    return None


def _raw_material_staging_policy_priority(
    projection_context: dict[str, object] | None,
) -> int:
    """Prefer dedicated raw areas before approved mixed-area overflow."""

    policy = (projection_context or {}).get("policy")
    try:
        values = json.loads(getattr(policy, "allowed_inventory_types_json", ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return 3
    allowed = {
        value.strip()
        for value in values
        if isinstance(value, str) and value.strip()
    }
    if allowed == {"raw_material"}:
        return 0
    if "raw_material" in allowed:
        return 1
    if "semi_finished" in allowed:
        return 2
    return 3


def _raw_material_staging_write_authorized(
    *,
    source_type: str | None,
    source_ref_type: str | None,
    source_ref_id: int | None,
) -> bool:
    if int(source_ref_id or 0) <= 0:
        return False
    if source_type == "purchase_reserve":
        return source_ref_type == "incoming_receipt_item"
    if source_type == "replenishment":
        return source_ref_type in {
            "stock_replenishment_receipt",
            "stock_replenishment_item",
        }
    return False


def _repairable_legacy_raw_material_staging_location(
    db: Session,
    *,
    rows: list[WarehouseLocation],
    projection_contexts: dict[int, dict[str, object | None]],
    operator_id: int | None = None,
    repair_idempotency_key: str | None = None,
) -> WarehouseLocation | None:
    """Project or repair an exact legacy measured ground-plan omission.

    This is deliberately narrower than an operational-location fallback.  It
    only accepts an already enabled and published TWIN_V1 area whose saved
    rectangles round-trip to the standard pallet size on the current runtime
    map.  Any logical anchor, stale map binding, or non-standard geometry still
    fails closed.
    """

    try:
        floor_layout = load_warehouse_twin_floor("1F")
    except (OSError, ValueError, WarehouseTwinLayoutNotFoundError):
        return None

    grouped: dict[
        int,
        tuple[
            WarehouseArea,
            WarehouseAreaStoragePolicy,
            str,
            list[WarehouseLocation],
        ],
    ] = {}
    for row in rows:
        if (
            int(row.warehouse_floor or 0) != 1
            or str(row.source_version or "").strip().upper() != "TWIN_V1"
        ):
            continue
        context = projection_contexts.get(int(row.id), {})
        if not _is_raw_material_staging_location(
            db,
            row,
            projection_context=context,
        ):
            continue
        policy_inventory_type = _raw_material_staging_policy_inventory_type(context)
        area = context.get("area")
        policy = context.get("policy")
        if (
            policy_inventory_type is None
            or not isinstance(area, WarehouseArea)
            or not isinstance(policy, WarehouseAreaStoragePolicy)
        ):
            continue
        area_id = int(area.id)
        if area_id not in grouped:
            grouped[area_id] = (area, policy, policy_inventory_type, [])
        grouped[area_id][3].append(row)

    ordered_groups = sorted(
        grouped.values(),
        key=lambda item: (
            0 if item[2] == "raw_material" else 1,
            str(item[0].area_code),
            int(item[0].id),
        ),
    )
    should_materialize = operator_id is not None and bool(repair_idempotency_key)
    for area, policy, _inventory_type, area_rows in ordered_groups:
        location_ids = sorted(int(row.id) for row in area_rows)
        live_lot = db.scalar(
            select(InventoryLot.id)
            .where(
                InventoryLot.warehouse_location_id.in_(location_ids),
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
        current_pallet = db.scalar(
            select(InventoryPallet.id)
            .where(
                InventoryPallet.location_id.in_(location_ids),
                InventoryPallet.is_current.is_(True),
            )
            .limit(1)
        )
        active_ground_occupancy = db.scalar(
            select(WarehouseGroundOccupancySlot.id)
            .join(WarehouseGroundOccupancy)
            .where(
                WarehouseGroundOccupancySlot.location_id.in_(location_ids),
                WarehouseGroundOccupancySlot.status == "active",
                WarehouseGroundOccupancy.status == "active",
            )
            .limit(1)
        )
        if any(
            value is not None
            for value in (live_lot, current_pallet, active_ground_occupancy)
        ):
            continue
        operation_key = (
            f"legacy-ground-plan:{int(area.id)}:"
            f"{str(getattr(policy, 'published_map_revision', '') or '').strip()}"
        )
        try:
            result = ensure_one_step_ground_plan(
                db,
                floor_layout=floor_layout,
                feature_id=str(getattr(policy, "map_feature_id", "") or ""),
                area=area,
                storage_layout=str(getattr(policy, "storage_layout", "") or ""),
                location_count=len(area_rows),
                operation_key=operation_key,
                operator_id=int(operator_id or 0),
                materialize=should_materialize,
            )
        except WarehouseAreaActivationError:
            continue
        if result.get("ground_plan_status") not in {"repairable", "published"}:
            continue
        repaired_ids = [int(value) for value in result.get("location_ids") or []]
        if repaired_ids != location_ids:
            continue
        if should_materialize:
            actor = db.get(User, int(operator_id))
            if actor is None:
                raise WarehouseInventoryError(
                    "当前收料人员不存在，系统未修复原料位置台账，也未写入库存",
                    409,
                )
            append_audit_event(
                db,
                event_category="system",
                result="success",
                source="system",
                module_code="warehouse",
                action_code="warehouse.legacy_ground_plan.auto_repair",
                resource="WarehouseGroundLayoutPlan",
                legacy_action="AUTO_REPAIR",
                actor=actor,
                entity_type="warehouse_area",
                entity_id=int(area.id),
                object_ref=f"warehouse-area:{int(area.id)}",
                description="收料时修复旧版已实测原料区缺失的地堆排位台账",
                details={
                    "area_code": area.area_code,
                    "map_revision": getattr(policy, "published_map_revision", None),
                    "ground_plan_id": result.get("ground_plan_id"),
                    "location_ids": repaired_ids,
                    "legacy_layout_repaired_count": result.get(
                        "legacy_layout_repaired_count", 0
                    ),
                    "trigger_fingerprint": sha256(
                        str(repair_idempotency_key).encode("utf-8")
                    ).hexdigest(),
                },
            )
        return next(row for row in area_rows if int(row.id) == repaired_ids[0])
    return None


def automatic_raw_material_staging_location(
    db: Session,
    *,
    allow_repairable_legacy: bool = False,
    repair_operator_id: int | None = None,
    repair_idempotency_key: str | None = None,
    require_floor3_left: bool = False,
) -> WarehouseLocation:
    """Resolve the legal staging point for actual replenishment receipts."""

    rows = db.scalars(
        select(WarehouseLocation).where(
            WarehouseLocation.is_active.is_(True),
            WarehouseLocation.warehouse_type.in_(
                RAW_MATERIAL_STAGING_WAREHOUSE_TYPES
            ),
        )
    ).all()
    require_published_location = has_space_ledger(db)
    projection_contexts = (
        load_warehouse_location_projection_contexts(db, rows)
        if require_published_location
        else {}
    )
    candidates: list[tuple[WarehouseLocation, str | None, int]] = []
    for row in rows:
        if require_floor3_left and (
            int(row.warehouse_floor or 0) != 3
            or str(row.area_code or "").strip().upper()
            not in CURRENT_MAP_LEFT_RAW_SEMI_AREA_CODES
        ):
            continue
        context = projection_contexts.get(int(row.id), {})
        if not _is_raw_material_staging_location(
            db,
            row,
            projection_context=context,
        ):
            continue
        policy_inventory_type = _raw_material_staging_policy_inventory_type(context)
        if require_published_location and policy_inventory_type is None:
            continue
        if operational_location_issue(
            db,
            row,
            warehouse_types=RAW_MATERIAL_STAGING_WAREHOUSE_TYPES,
            require_published=require_published_location,
            require_map_geometry=require_published_location,
            required_inventory_type=(
                policy_inventory_type if require_published_location else None
            ),
            projection_context=context,
        ) is None:
            candidates.append(
                (
                    row,
                    policy_inventory_type,
                    _raw_material_staging_policy_priority(context),
                )
            )
    candidates.sort(
        key=lambda candidate: (
            0
            if str(candidate[0].source_version or "").strip().upper()
            == "CURRENT_MAP"
            else 1,
            candidate[2],
            int(candidate[0].warehouse_floor or 0),
            candidate[0].location_code,
            candidate[0].id,
        )
    )
    if (
        not candidates
        and not require_floor3_left
        and (allow_repairable_legacy or repair_operator_id is not None)
    ):
        repairable = _repairable_legacy_raw_material_staging_location(
            db,
            rows=rows,
            projection_contexts=projection_contexts,
            operator_id=repair_operator_id,
            repair_idempotency_key=repair_idempotency_key,
        )
        if repairable is not None:
            if repair_operator_id is None:
                return repairable
            # Re-run the canonical strict classifier after the plan and slots
            # have been flushed.  A repair is never itself enough to authorize
            # the subsequent inventory write.
            return automatic_raw_material_staging_location(db)
    if not candidates:
        if require_floor3_left:
            raise WarehouseInventoryError(
                "当前正式地图没有可用的三楼左区原料或半成品货位；系统不会回退到一楼或待送区。",
                409,
            )
        raise WarehouseInventoryError(
            "当前地图没有可用的已发布原料或片料位置，请联系仓库管理员核对区域策略、地图发布状态与真实排位；系统不会改用待送区。",
            409,
        )
    return candidates[0][0]


def automatic_floor3_left_finished_location(db: Session) -> WarehouseLocation:
    """Resolve a published third-floor-left finished-goods destination."""

    rows = db.scalars(
        select(WarehouseLocation).where(
            WarehouseLocation.is_active.is_(True),
            WarehouseLocation.warehouse_type.in_(("finished", "shared")),
            WarehouseLocation.warehouse_floor == 3,
            WarehouseLocation.area_code.in_(CURRENT_MAP_LEFT_FINISHED_AREA_CODES),
        )
    ).all()
    contexts = load_warehouse_location_projection_contexts(db, rows)
    candidates: list[WarehouseLocation] = []
    for row in rows:
        context = contexts.get(int(row.id), {})
        if operational_location_issue(
            db,
            row,
            warehouse_types={"finished", "shared"},
            require_published=True,
            require_map_geometry=True,
            required_inventory_type="finished",
            projection_context=context,
        ) is None:
            candidates.append(row)
    candidates.sort(
        key=lambda row: (
            0
            if str(row.source_version or "").strip().upper() == "CURRENT_MAP"
            else 1,
            str(row.area_code or ""),
            str(row.location_code or ""),
            int(row.id),
        )
    )
    if not candidates:
        raise WarehouseInventoryError(
            "当前正式地图没有可用的三楼左区成品货位；"
            "系统不会回退到一楼、待送区或旧版未发布位置。",
            409,
        )
    return candidates[0]


def _current_map_temporary_finished_anchor_issue(
    location: WarehouseLocation,
    context: dict[str, object | None],
) -> str | None:
    layout = context.get("layout")
    policy = context.get("policy")
    if (
        int(location.warehouse_floor or 0) != 3
        or str(location.area_code or "").strip().upper()
        not in CURRENT_MAP_TEMPORARY_FINISHED_AREA_CODES
        or location.location_code
        not in CURRENT_MAP_TEMPORARY_FINISHED_LOCATION_CODES
        or str(location.source_version or "").strip().upper() != "CURRENT_MAP"
        or location.storage_type != "temporary_aisle"
        or not location.is_temporary
        or location.address_kind != "functional"
        or not isinstance(layout, Floor3LocationLayout)
        or layout.layout_kind != "logical_anchor"
        or not isinstance(policy, WarehouseAreaStoragePolicy)
        or policy.status != "published"
        or context.get("ground_layout") is not None
    ):
        return "该位置不是已发布的 F34/F12 临时周转锚点"
    return None


def automatic_floor3_finished_turnover_location(
    db: Session,
) -> WarehouseLocation:
    """Claim the first empty F34/F12 temporary anchor for a liner receipt."""

    rows = list(
        db.scalars(
            select(WarehouseLocation)
            .where(
                WarehouseLocation.is_active.is_(True),
                WarehouseLocation.warehouse_floor == 3,
                WarehouseLocation.warehouse_type.in_(("finished", "shared")),
                WarehouseLocation.area_code.in_(
                    CURRENT_MAP_TEMPORARY_FINISHED_AREA_CODES
                ),
                WarehouseLocation.location_code.in_(
                    CURRENT_MAP_TEMPORARY_FINISHED_LOCATION_CODES
                ),
            )
            .order_by(
                case((WarehouseLocation.area_code == "F34", 0), else_=1),
                WarehouseLocation.location_code,
                WarehouseLocation.id,
            )
        ).all()
    )
    contexts = load_warehouse_location_projection_contexts(db, rows)
    for row in rows:
        context = contexts.get(int(row.id), {})
        if _current_map_temporary_finished_anchor_issue(row, context) is not None:
            continue
        layout = context.get("layout")
        assert isinstance(layout, Floor3LocationLayout)
        issue = operational_location_issue(
            db,
            row,
            warehouse_types={"finished", "shared"},
            pallet_storage_only=True,
            require_published=True,
            require_map_geometry=True,
            required_inventory_type="finished",
            require_empty=True,
            projection_context=context,
        )
        if issue is not None:
            continue
        try:
            claimed = claim_active_placed_location(
                db,
                int(row.id),
                expected_layout_version=int(layout.version),
            )
        except OperationalError as error:
            raise WarehouseInventoryError(
                "F34/F12 临时周转位置正在被其他入库或地图操作使用，请稍后重试",
                409,
            ) from error
        if not claimed:
            continue
        refreshed = db.get(WarehouseLocation, int(row.id), populate_existing=True)
        if refreshed is None:
            continue
        refreshed_context = load_warehouse_location_projection_contexts(
            db, [refreshed]
        ).get(int(refreshed.id), {})
        if (
            _current_map_temporary_finished_anchor_issue(
                refreshed, refreshed_context
            )
            is None
            and operational_location_issue(
                db,
                refreshed,
                warehouse_types={"finished", "shared"},
                pallet_storage_only=True,
                require_published=True,
                require_map_geometry=True,
                required_inventory_type="finished",
                require_empty=True,
                projection_context=refreshed_context,
            )
            is None
        ):
            return refreshed
    raise WarehouseInventoryError(
        "三楼右区 F34/F12 临时周转位置已满或尚未启用；请先将现有栈板归位后再收料。",
        409,
    )


def _location(
    db: Session,
    location_id: int,
    inventory_type: str,
    *,
    capacity_source_location_id: int | None = None,
    allow_raw_material_staging: bool = False,
    raw_material_staging_source_type: str | None = None,
    raw_material_staging_source_ref_type: str | None = None,
    raw_material_staging_source_ref_id: int | None = None,
) -> WarehouseLocation:
    location = db.get(WarehouseLocation, location_id)
    allowed = {
        "finished": {"finished", "shared"},
        "semi_finished": {"semi_finished", "shared"},
    }[inventory_type]
    if location is None:
        raise WarehouseInventoryError("库位不存在", 404)
    if getattr(location, "source_version", None) == "V11":
        if getattr(location, "warehouse_floor", None) != 3:
            raise WarehouseInventoryError(
                "V11 货位楼层无效，不能办理成品入库", 409
            )
    # 4F starts life as a scan-only planning floor.  Even a stale/manual
    # ``placed`` row must not become an inventory destination before the 4F
    # map and area policy are formally published.  Existing 1F/3F legacy
    # compatibility remains unchanged when no space ledger exists.
    require_published_location = (
        has_space_ledger(db)
        or getattr(location, "warehouse_floor", None) == 4
    )
    if inventory_type == "finished":
        if getattr(location, "placement_status", None) == "unplaced":
            raise WarehouseInventoryError(
                "该库位尚未完成空间放置，不能办理成品库存业务",
                409,
            )
        projection_context = (
            load_warehouse_location_projection_contexts(db, [location]).get(
                int(location.id), {}
            )
            if require_published_location
            else None
        )
        issue = operational_location_issue(
            db,
            location,
            warehouse_types=allowed,
            require_published=require_published_location,
            require_map_geometry=require_published_location,
            required_inventory_type=(
                "finished" if require_published_location else None
            ),
            capacity_source_location_id=capacity_source_location_id,
            projection_context=projection_context,
        )
        if issue:
            raise WarehouseInventoryError(f"{issue}，不能办理成品库存业务", 409)
        return location
    if not location.is_active:
        raise WarehouseInventoryError("该库位已停用，不能入库")
    if getattr(location, "placement_status", None) == "unplaced":
        raise WarehouseInventoryError(
            "该库位尚未完成空间放置，不能入库；请先补齐楼层、区域和存储方式",
            409,
        )
    projection_context = (
        load_warehouse_location_projection_contexts(db, [location]).get(
            int(location.id), {}
        )
        if require_published_location
        else None
    )
    raw_staging_inventory_type: str | None = None
    if allow_raw_material_staging and _raw_material_staging_write_authorized(
        source_type=raw_material_staging_source_type,
        source_ref_type=raw_material_staging_source_ref_type,
        source_ref_id=raw_material_staging_source_ref_id,
    ):
        context = projection_context or {}
        if _is_raw_material_staging_location(
            db,
            location,
            projection_context=context,
        ):
            raw_staging_inventory_type = (
                _raw_material_staging_policy_inventory_type(context)
                if require_published_location
                else "semi_finished"
            )
    issue = operational_location_issue(
        db,
        location,
        warehouse_types=(
            RAW_MATERIAL_STAGING_WAREHOUSE_TYPES
            if raw_staging_inventory_type is not None
            else allowed
        ),
        require_published=require_published_location,
        require_map_geometry=require_published_location,
        required_inventory_type=(
            (
                raw_staging_inventory_type or "semi_finished"
                if require_published_location
                else None
            )
        ),
        projection_context=projection_context,
        capacity_source_location_id=capacity_source_location_id,
    )
    if issue:
        raise WarehouseInventoryError(f"{issue}，不能办理半成品库存业务", 409)
    return location


def _idempotent_lot(db: Session, key: str | None) -> InventoryLot | None:
    if not key:
        return None
    movement = db.scalar(
        select(InventoryMovement).where(InventoryMovement.idempotency_key == key)
    )
    return db.get(InventoryLot, movement.inventory_lot_id) if movement else None


def _balances(lot: InventoryLot) -> dict[str, int]:
    return {
        "available": lot.quantity_available,
        "reserved": lot.quantity_reserved,
        "consumed": lot.quantity_consumed,
        "damaged": lot.quantity_damaged,
        "scrapped": lot.quantity_scrapped,
    }


def inventory_fifo_order_columns() -> tuple:
    """Order trustworthy/estimated dates first and unknown technical dates last."""
    return (
        case(
            (InventoryLot.stock_date_accuracy == "unknown", 1),
            else_=0,
        ),
        InventoryLot.stock_date,
        InventoryLot.id,
    )


def inventory_fifo_sort_key(lot: InventoryLot) -> tuple:
    """Mirror the SQL FIFO rule for lots already loaded into Python."""
    return (
        1 if (lot.stock_date_accuracy or "unknown") == "unknown" else 0,
        lot.stock_date,
        lot.id,
    )


def _movement(
    db: Session,
    *,
    lot: InventoryLot,
    movement_type: str,
    quantity: int,
    before: dict[str, int],
    operator_id: int | None,
    reason: str | None = None,
    remarks: str | None = None,
    idempotency_key: str | None = None,
    reservation_id: int | None = None,
    related_order_id: int | None = None,
    related_order_item_id: int | None = None,
    related_delivery_id: int | None = None,
    reversal_of_movement_id: int | None = None,
) -> InventoryMovement:
    after = _balances(lot)
    row = InventoryMovement(
        movement_number=_number("IM"),
        inventory_lot_id=lot.id,
        movement_type=movement_type,
        quantity=abs(quantity),
        unit=lot.unit,
        before_available=before["available"],
        after_available=after["available"],
        before_reserved=before["reserved"],
        after_reserved=after["reserved"],
        before_consumed=before["consumed"],
        after_consumed=after["consumed"],
        before_damaged=before["damaged"],
        after_damaged=after["damaged"],
        before_scrapped=before["scrapped"],
        after_scrapped=after["scrapped"],
        reason=reason,
        remarks=remarks,
        operator_id=operator_id,
        idempotency_key=idempotency_key,
        reservation_id=reservation_id,
        related_order_id=related_order_id,
        related_order_item_id=related_order_item_id,
        related_delivery_id=related_delivery_id,
        reversal_of_movement_id=reversal_of_movement_id,
    )
    db.add(row)
    return row


def record_location_transfer_without_quantity_change(
    db: Session,
    *,
    lot: InventoryLot,
    operator_id: int | None,
    idempotency_key: str,
    remarks: str,
) -> InventoryMovement:
    """Record a physical location change without altering inventory balances."""
    before = _balances(lot)
    return _movement(
        db,
        lot=lot,
        movement_type="location_transfer",
        quantity=0,
        before=before,
        operator_id=operator_id,
        remarks=remarks,
        idempotency_key=idempotency_key,
    )


def _transfer_key(*parts: object, max_length: int = 100) -> str:
    raw = ":".join(str(part).strip() for part in parts)
    if len(raw) <= max_length:
        return raw
    digest = sha256(raw.encode("utf-8")).hexdigest()[:24]
    return f"{raw[: max_length - 25]}:{digest}"


def _lot_location_transfer_hash(
    *,
    lot_id: int,
    expected_version: int,
    quantity: int,
    location_id: int,
    expected_source_location_id: int | None = None,
    expected_source_address_version: int | None = None,
    expected_source_layout_version: int | None = None,
    expected_source_map_revision: str | None = None,
    expected_target_layout_version: int | None = None,
    expected_target_address_version: int | None = None,
    expected_target_map_revision: str | None = None,
    ground_secondary_location_id: int | None = None,
    ground_capacity_quantity: int | None = None,
) -> str:
    payload = {
        "expected_version": expected_version,
        "expected_source_location_id": expected_source_location_id,
        "expected_source_address_version": expected_source_address_version,
        "expected_source_layout_version": expected_source_layout_version,
        "expected_source_map_revision": expected_source_map_revision,
        "location_id": location_id,
        "expected_target_layout_version": expected_target_layout_version,
        "expected_target_address_version": expected_target_address_version,
        "expected_target_map_revision": expected_target_map_revision,
        "lot_id": lot_id,
        "quantity": quantity,
        "ground_secondary_location_id": ground_secondary_location_id,
        "ground_capacity_quantity": ground_capacity_quantity,
    }
    return sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()


def _legacy_lot_location_transfer_hashes(
    *,
    lot_id: int,
    expected_version: int,
    quantity: int,
    location_id: int,
    expected_target_layout_version: int | None,
    ground_secondary_location_id: int | None,
    ground_capacity_quantity: int | None,
) -> set[str]:
    """Recognize immutable transfers written before the full mobile snapshot token.

    Old rows cannot prove address/map versions that were never stored.  Their
    stable source/target/quantity/version facts still make an already-completed
    retry safe; every new row is written with the full request hash above.
    """

    base = {
        "expected_version": expected_version,
        "location_id": location_id,
        "lot_id": lot_id,
        "quantity": quantity,
        "ground_secondary_location_id": ground_secondary_location_id,
        "ground_capacity_quantity": ground_capacity_quantity,
    }
    variants = [base, {**base, "expected_target_layout_version": expected_target_layout_version}]
    return {
        sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        for payload in variants
    }


def _replayed_finished_location_transfer(
    db: Session,
    *,
    repeated: InventoryLotTransfer,
    lot_id: int,
    location_id: int,
    quantity: int,
    expected_version: int,
    request_hash: str,
    compatible_legacy_hashes: set[str],
    expected_source_location_id: int | None,
) -> FinishedLotLocationTransferResult:
    same_stored_facts = bool(
        int(repeated.source_lot_id) == int(lot_id)
        and int(repeated.target_location_id) == int(location_id)
        and int(repeated.quantity) == int(quantity)
        and int(repeated.source_version_before) == int(expected_version)
        and (
            expected_source_location_id is None
            or int(repeated.source_location_id) == int(expected_source_location_id)
        )
    )
    if not same_stored_facts or repeated.request_hash not in {
        request_hash,
        *compatible_legacy_hashes,
    }:
        raise WarehouseInventoryError("同一请求标识已用于其他库位转移", 409)
    source = db.get(InventoryLot, repeated.source_lot_id)
    target = db.get(InventoryLot, repeated.target_lot_id)
    if source is None or target is None:
        raise WarehouseInventoryError("已完成的库位转移记录不完整", 409)
    return FinishedLotLocationTransferResult(repeated, source, target, True)


def _transfer_finished_lot_location(
    db: Session,
    *,
    lot_id: int,
    expected_version: int,
    quantity: int,
    location_id: int,
    operator_id: int | None,
    idempotency_key: str,
    require_staging_source: bool,
    require_pending_source: bool = False,
    require_empty_target: bool = False,
    expected_source_location_id: int | None = None,
    expected_source_address_version: int | None = None,
    expected_source_layout_version: int | None = None,
    expected_source_map_revision: str | None = None,
    expected_target_layout_version: int | None = None,
    expected_target_address_version: int | None = None,
    expected_target_map_revision: str | None = None,
    ground_secondary_location_id: int | None = None,
    ground_capacity_quantity: int | None = None,
    reserved_plan: dict[int, int] | None = None,
) -> FinishedLotLocationTransferResult:
    """Move all or part of a finished lot without changing stock totals.

    Available pieces move first.  When the selected quantity also includes
    reserved pieces, their reservations are split onto the destination lot so
    order and delivery coverage remains unchanged.
    """

    key = idempotency_key.strip()
    if not key or len(key) > 120:
        raise WarehouseInventoryError("请求标识长度必须为1到120个字符")
    if quantity <= 0:
        raise WarehouseInventoryError("转入数量必须大于0")
    request_hash = _lot_location_transfer_hash(
        lot_id=lot_id,
        expected_version=expected_version,
        quantity=quantity,
        location_id=location_id,
        expected_source_location_id=expected_source_location_id,
        expected_source_address_version=expected_source_address_version,
        expected_source_layout_version=expected_source_layout_version,
        expected_source_map_revision=expected_source_map_revision,
        expected_target_layout_version=expected_target_layout_version,
        expected_target_address_version=expected_target_address_version,
        expected_target_map_revision=expected_target_map_revision,
        ground_secondary_location_id=ground_secondary_location_id,
        ground_capacity_quantity=ground_capacity_quantity,
    )
    compatible_legacy_hashes = _legacy_lot_location_transfer_hashes(
        lot_id=lot_id,
        expected_version=expected_version,
        quantity=quantity,
        location_id=location_id,
        expected_target_layout_version=expected_target_layout_version,
        ground_secondary_location_id=ground_secondary_location_id,
        ground_capacity_quantity=ground_capacity_quantity,
    )
    if require_pending_source:
        request_hash = sha256(("recount-placement:" + request_hash).encode()).hexdigest()
        compatible_legacy_hashes = ()
    if reserved_plan is not None:
        request_hash = sha256((request_hash + json.dumps(reserved_plan, sort_keys=True)).encode()).hexdigest()
        compatible_legacy_hashes = ()
    repeated = db.scalar(
        select(InventoryLotTransfer).where(
            InventoryLotTransfer.idempotency_key == key
        )
    )
    if repeated is not None:
        if require_pending_source and repeated.transferred_by != operator_id:
            raise WarehouseInventoryError("该归位请求已由其他操作员使用", 409)
        return _replayed_finished_location_transfer(
            db,
            repeated=repeated,
            lot_id=lot_id,
            location_id=location_id,
            quantity=quantity,
            expected_version=expected_version,
            request_hash=request_hash,
            compatible_legacy_hashes=compatible_legacy_hashes,
            expected_source_location_id=expected_source_location_id,
        )

    candidate_lot = db.get(InventoryLot, lot_id)
    if candidate_lot is None:
        raise WarehouseInventoryError("成品库存批次不存在", 404)
    claimed_source_location_id = (
        int(expected_source_location_id)
        if expected_source_location_id is not None
        else (
            int(candidate_lot.warehouse_location_id)
            if candidate_lot.warehouse_location_id is not None
            else 0
        )
    )
    if claimed_source_location_id <= 0:
        raise WarehouseInventoryError("成品库存缺少来源库位，请刷新后重试", 409)
    _claim_inventory_transfer_locations(
        db,
        source_location_id=claimed_source_location_id,
        target_location_id=location_id,
        expected_source_layout_version=expected_source_layout_version,
        expected_target_layout_version=expected_target_layout_version,
        require_pending_source=require_pending_source,
    )

    # A contender may have completed this key while this transaction waited on
    # the shared floor/location mutex. Recheck before reading or changing stock.
    repeated = db.scalar(
        select(InventoryLotTransfer)
        .where(InventoryLotTransfer.idempotency_key == key)
        .execution_options(populate_existing=True)
    )
    if repeated is not None:
        if require_pending_source and repeated.transferred_by != operator_id:
            raise WarehouseInventoryError("该归位请求已由其他操作员使用", 409)
        return _replayed_finished_location_transfer(
            db,
            repeated=repeated,
            lot_id=lot_id,
            location_id=location_id,
            quantity=quantity,
            expected_version=expected_version,
            request_hash=request_hash,
            compatible_legacy_hashes=compatible_legacy_hashes,
            expected_source_location_id=expected_source_location_id,
        )

    lot = db.get(InventoryLot, lot_id, populate_existing=True)
    if lot is None or lot.finished_detail is None:
        raise WarehouseInventoryError("成品库存批次不存在", 404)
    if int(lot.warehouse_location_id or 0) != claimed_source_location_id:
        raise WarehouseInventoryError("库存登记位置已变化，请刷新后重试", 409)
    source_location = db.get(
        WarehouseLocation,
        claimed_source_location_id,
        populate_existing=True,
    )
    staging_source = not bool(
        lot.inventory_type != "finished"
        or lot.status != "active"
        or lot.source_type not in {"production_completion", "transfer"}
        or lot.source_ref_type != "production_completion"
        or lot.source_ref_id is None
        or source_location is None
        or source_location.location_code != "F1-DISPATCH-01"
    )
    if lot.inventory_type != "finished" or lot.status != "active" or source_location is None:
        raise WarehouseInventoryError("只有有效成品库存批次可以移位", 409)
    if require_staging_source and not staging_source:
        raise WarehouseInventoryError("只有一楼待送区的有效成品批次可以转入库位", 409)
    if require_staging_source and lot.pallet_item is not None:
        raise WarehouseInventoryError("该待送批次已绑定物理栈板，请刷新后重试", 409)
    if require_pending_source and not is_pending_relocation_location(source_location):
        raise WarehouseInventoryError("来源已不是盘点待归位，请刷新后重试", 409)
    if not require_staging_source and not require_pending_source:
        source_issue = operational_location_issue(
            db,
            source_location,
            warehouse_types={"finished", "shared"},
        )
        if source_issue:
            raise WarehouseInventoryError(f"来源位置不可用：{source_issue}", 409)
    if int(lot.version) != expected_version:
        raise WarehouseInventoryError("库存已被其他人修改，请刷新后重试", 409)
    live_quantity = int(lot.quantity_available or 0) + int(lot.quantity_reserved or 0)
    if quantity > live_quantity:
        raise WarehouseInventoryError(f"当前只有 {live_quantity} 个可移位", 409)
    if int(lot.quantity_damaged or 0) > 0 or int(lot.quantity_scrapped or 0) > 0:
        raise WarehouseInventoryError("该批次仍有报损或报废数量，不能直接移位", 409)

    target_location = _location(db, location_id, "finished")
    if not require_pending_source:
        _validate_transfer_location_snapshot(
            db,
            location=source_location,
            role="来源",
            expected_address_version=expected_source_address_version,
            expected_layout_version=expected_source_layout_version,
            expected_map_revision=expected_source_map_revision,
        )
    _validate_transfer_location_snapshot(
        db,
        location=target_location,
        role="目标",
        expected_address_version=expected_target_address_version,
        expected_layout_version=expected_target_layout_version,
        expected_map_revision=expected_target_map_revision,
    )
    if target_location.id == source_location.id and reserved_plan is None:
        raise WarehouseInventoryError("目标位置不能与来源位置相同", 409)
    if require_staging_source and target_location.location_code == "F1-DISPATCH-01":
        raise WarehouseInventoryError("目标库位不能仍是一楼待送区", 409)
    if not require_staging_source:
        source_detail = lot.finished_detail
        assert source_detail is not None
        source_signature = (
            source_detail.owner_customer_id,
            source_detail.product_id,
            str(source_detail.inventory_code_snapshot or "").strip(),
            int(source_detail.length_mm or 0),
            int(source_detail.width_mm or 0),
            int(source_detail.height_mm or 0),
            str(lot.inventory_type or ""),
            str(lot.unit or ""),
        )
        target_lots = list(
            db.scalars(
                select(InventoryLot)
                .options(selectinload(InventoryLot.finished_detail))
                .where(
                    InventoryLot.warehouse_location_id == target_location.id,
                    InventoryLot.id != lot.id,
                    InventoryLot.status.in_(("active", "frozen")),
                    (
                        InventoryLot.quantity_available
                        + InventoryLot.quantity_reserved
                        + InventoryLot.quantity_damaged
                    )
                    > 0,
                )
                .order_by(InventoryLot.id)
            ).all()
        )
        if require_empty_target and target_lots:
            raise WarehouseInventoryError("目标货位已有货物，请选择空位", 409)
        for target_lot in target_lots:
            target_detail = target_lot.finished_detail
            if (
                target_lot.status != "active"
                or int(target_lot.quantity_damaged or 0) > 0
                or target_detail is None
            ):
                raise WarehouseInventoryError(
                    "目标货位已有冻结、损坏或待核对货物，不能直接混放；请先换空位或完成异常处理",
                    409,
                )
            target_signature = (
                target_detail.owner_customer_id,
                target_detail.product_id,
                str(target_detail.inventory_code_snapshot or "").strip(),
                int(target_detail.length_mm or 0),
                int(target_detail.width_mm or 0),
                int(target_detail.height_mm or 0),
                str(target_lot.inventory_type or ""),
                str(target_lot.unit or ""),
            )
            operator = db.get(User, operator_id) if operator_id is not None else None
            if target_signature != source_signature and (operator is None or operator.role != "admin"):
                raise WarehouseInventoryError(
                    "目标货位已有不同客户、存货编码或规格的货物，不能直接混放；同品可共位保留批次，异品请换空位",
                    409,
                )

    source_location_id = int(lot.warehouse_location_id)
    available_take = min(quantity, int(lot.quantity_available or 0))
    reserved_take = quantity - available_take
    if reserved_plan is not None:
        reserved_take = sum(reserved_plan.values())
        available_take = quantity - reserved_take
        if available_take < 0 or available_take > int(lot.quantity_available or 0):
            raise WarehouseInventoryError('实际集货数量超过本次可用库存', 409)
        for reservation_id, take in reserved_plan.items():
            selected = db.get(InventoryReservation, reservation_id)
            if (take <= 0 or selected is None or selected.inventory_lot_id != lot.id
                    or selected.status not in {'active', 'partial'} or selected.yield_factor != 1
                    or take > selected.reserved_stock_quantity - selected.consumed_stock_quantity - selected.released_stock_quantity):
                raise WarehouseInventoryError('集货预占来源或数量已变化，请刷新', 409)
    source_before = _balances(lot)
    now = utc_now_naive()

    split_full_pallet_lot = bool(
        not require_staging_source
        and quantity == live_quantity
        and lot.pallet_item is not None
    )
    if quantity == live_quantity and not split_full_pallet_lot:
        updated = db.execute(
            update(InventoryLot)
            .where(
                InventoryLot.id == lot.id,
                InventoryLot.version == expected_version,
                InventoryLot.warehouse_location_id == source_location_id,
                InventoryLot.quantity_available == source_before["available"],
                InventoryLot.quantity_reserved == source_before["reserved"],
            )
            .values(
                warehouse_location_id=target_location.id,
                version=InventoryLot.version + 1,
                last_movement_at=now,
            )
        )
        if updated.rowcount != 1:
            raise WarehouseInventoryError("待送批次数量或版本已变化，请刷新后重试", 409)
        db.flush()
        db.expire(lot)
        target_lot = db.get(InventoryLot, lot.id)
        assert target_lot is not None
    else:
        updated = db.execute(
            update(InventoryLot)
            .where(
                InventoryLot.id == lot.id,
                InventoryLot.version == expected_version,
                InventoryLot.warehouse_location_id == source_location_id,
                InventoryLot.quantity_available >= available_take,
                InventoryLot.quantity_reserved >= reserved_take,
            )
            .values(
                quantity_available=InventoryLot.quantity_available - available_take,
                quantity_reserved=InventoryLot.quantity_reserved - reserved_take,
                status=("closed" if quantity == live_quantity else lot.status),
                version=InventoryLot.version + 1,
                last_movement_at=now,
            )
        )
        if updated.rowcount != 1:
            raise WarehouseInventoryError("待送批次数量或版本已变化，请刷新后重试", 409)
        detail = lot.finished_detail
        target_lot = InventoryLot(
            lot_number=_number("FG"),
            inventory_type="finished",
            warehouse_location_id=target_location.id,
            quantity_available=available_take,
            quantity_reserved=reserved_take,
            quantity_consumed=0,
            quantity_damaged=0,
            quantity_scrapped=0,
            unit=lot.unit,
            status="active",
            source_type="transfer",
            source_ref_type=lot.source_ref_type,
            source_ref_id=lot.source_ref_id,
            stock_date=lot.stock_date,
            stock_date_accuracy=lot.stock_date_accuracy,
            stock_date_original_text=lot.stock_date_original_text,
            last_movement_at=now,
            created_by=operator_id,
            remarks=(
                f"由批次 {lot.lot_number} 位置移位拆分"
                if not require_staging_source
                else f"由一楼待送批次 {lot.lot_number} 部分转入"
            ),
            estimated_unit_cost_snapshot=lot.estimated_unit_cost_snapshot,
            estimated_square_price_snapshot=lot.estimated_square_price_snapshot,
            estimated_cost_area_m2_snapshot=lot.estimated_cost_area_m2_snapshot,
            cost_snapshot_source=lot.cost_snapshot_source,
            cost_snapshot_detail_json=lot.cost_snapshot_detail_json,
            cost_snapshot_at=lot.cost_snapshot_at,
        )
        target_lot.finished_detail = FinishedGoodsInventoryDetail(
            owner_customer_id=detail.owner_customer_id,
            owner_customer_name_snapshot=detail.owner_customer_name_snapshot,
            is_general=detail.is_general,
            product_id=detail.product_id,
            inventory_code_snapshot=detail.inventory_code_snapshot,
            product_name_snapshot=detail.product_name_snapshot,
            physical_basis_json=detail.physical_basis_json,
            box_type_snapshot=detail.box_type_snapshot,
            length_mm=detail.length_mm,
            width_mm=detail.width_mm,
            height_mm=detail.height_mm,
            material_code_snapshot=detail.material_code_snapshot,
            flute_type_snapshot=detail.flute_type_snapshot,
        )
        db.add(target_lot)
        db.flush()

        remaining_reserved = reserved_take
        reservations = list(
            db.scalars(
                select(InventoryReservation)
                .where(
                    InventoryReservation.inventory_lot_id == lot.id,
                    InventoryReservation.status.in_(("active", "partial")),
                )
                .order_by(InventoryReservation.id)
            )
        )
        for reservation in reservations:
            if remaining_reserved <= 0:
                break
            remaining = (
                int(reservation.reserved_stock_quantity or 0)
                - int(reservation.consumed_stock_quantity or 0)
                - int(reservation.released_stock_quantity or 0)
            )
            take = min(remaining_reserved, remaining)
            if reserved_plan is not None:
                take = min(take, reserved_plan.get(reservation.id, 0))
            if take <= 0:
                continue
            reservation.released_stock_quantity += take
            reservation.released_requirement_quantity += take
            reservation.released_by = operator_id
            reservation.released_at = now
            reservation.release_reason = "库存批次移动拆分"
            reservation.status = _finished_reservation_status(reservation)
            group_key = _transfer_key(
                reservation.reservation_group_key or f"reservation-{reservation.id}",
                "location-transfer",
                key,
            )
            db.add(
                InventoryReservation(
                    reservation_number=_number("RS"),
                    inventory_lot_id=target_lot.id,
                    reservation_type=reservation.reservation_type,
                    order_id=reservation.order_id,
                    order_item_id=reservation.order_item_id,
                    sales_order_item_bom_component_id=(
                        reservation.sales_order_item_bom_component_id
                    ),
                    requisition_item_id=reservation.requisition_item_id,
                    semi_requirement_id=reservation.semi_requirement_id,
                    match_rule_id=reservation.match_rule_id,
                    reserved_stock_quantity=take,
                    credited_requirement_quantity=take,
                    yield_factor=reservation.yield_factor,
                    status="active",
                    warning_codes=reservation.warning_codes,
                    warning_acknowledged_by=reservation.warning_acknowledged_by,
                    reserved_by=operator_id,
                    reserved_at=now,
                    reservation_group_key=group_key,
                    reservation_group_requested_quantity=take,
                    idempotency_key=_transfer_key("location-transfer", key, reservation.id),
                )
            )
            remaining_reserved -= take
        if remaining_reserved:
            raise WarehouseInventoryError("待送批次预占明细与库存余额不一致", 409)
        db.flush()
        db.expire(lot)
        lot = db.get(InventoryLot, lot.id)
        assert lot is not None

    if split_full_pallet_lot:
        source_item = db.scalar(
            select(InventoryPalletItem).where(
                InventoryPalletItem.inventory_lot_id == lot_id
            )
        )
        if source_item is not None:
            source_pallet = db.get(InventoryPallet, source_item.pallet_id)
            db.delete(source_item)
            db.flush()
            if source_pallet is not None:
                if not _pallet_has_physical_goods(db, source_pallet.id):
                    from app.services.floor3_locations import clear_pallet
                    from app.services.warehouse_ground_slots import (
                        release_ground_occupancy_for_pallet,
                    )

                    clear_pallet(
                        db,
                        pallet_id=source_pallet.id,
                        expected_version=source_pallet.version,
                        remarks="库存批次全部移出，释放空栈板",
                        operator_id=operator_id,
                        require_pending_source=require_pending_source,
                        idempotency_key=_transfer_key(
                            "location-transfer", key, "source-pallet-clear"
                        ),
                    )
                    release_ground_occupancy_for_pallet(
                        db,
                        pallet_id=int(source_pallet.id),
                        operator_id=operator_id,
                    )
    elif target_lot.id != lot.id:
        source_item = db.scalar(
            select(InventoryPalletItem).where(
                InventoryPalletItem.inventory_lot_id == lot_id
            )
        )
        if source_item is not None:
            source_item.quantity = (
                int(lot.quantity_available or 0)
                + int(lot.quantity_reserved or 0)
                + int(lot.quantity_damaged or 0)
            )
            source_pallet = db.get(InventoryPallet, source_item.pallet_id)
            if source_pallet is not None:
                source_pallet.version = int(source_pallet.version or 0) + 1
                source_pallet.updated_by = operator_id

    _ensure_finished_projection_postcondition(
        db,
        lot=target_lot,
        operator_id=operator_id,
        create_missing=True,
        require_empty_pallet=require_staging_source or require_empty_target,
        ground_secondary_location_id=ground_secondary_location_id,
        ground_capacity_quantity=ground_capacity_quantity,
    )

    transfer = InventoryLotTransfer(
        source_lot_id=lot_id,
        target_lot_id=target_lot.id,
        source_location_id=source_location_id,
        target_location_id=target_location.id,
        quantity=quantity,
        available_quantity=available_take,
        reserved_quantity=reserved_take,
        source_version_before=expected_version,
        source_version_after=expected_version + 1,
        idempotency_key=key,
        request_hash=request_hash,
        transferred_by=operator_id,
        transferred_at=now,
    )
    db.add(transfer)
    db.flush()
    _movement(
        db,
        lot=lot,
        movement_type="location_transfer",
        quantity=quantity,
        before=source_before,
        operator_id=operator_id,
        reason=(
            "一楼待送区转入正式库位"
            if require_staging_source
            else "正式库存位置移位"
        ),
        remarks=f"转入 {target_location.location_code}",
        idempotency_key=_transfer_key("location-transfer", key, "source"),
    )
    if target_lot.id != lot.id:
        _movement(
            db,
            lot=target_lot,
            movement_type="location_transfer",
            quantity=quantity,
            before={
                "available": 0,
                "reserved": 0,
                "consumed": 0,
                "damaged": 0,
                "scrapped": 0,
            },
            operator_id=operator_id,
            reason=(
                "一楼待送区转入正式库位"
                if require_staging_source
                else "正式库存位置移位"
            ),
            remarks=f"来自 {lot.lot_number}",
            idempotency_key=_transfer_key("location-transfer", key, "target"),
        )
    db.flush()
    from app.services.fixed_shelf import copy_lot_state
    copy_lot_state(db, lot, target_lot)
    db.flush()
    return FinishedLotLocationTransferResult(transfer, lot, target_lot, False)


def transfer_pending_finished_lot(
    db: Session, *, lot_id: int, expected_version: int, quantity: int,
    location_id: int, operator_id: int | None, idempotency_key: str,
    expected_target_layout_version: int,
    expected_target_address_version: int | None = None,
    expected_target_map_revision: str | None = None,
) -> FinishedLotLocationTransferResult:
    return _transfer_finished_lot_location(
        db, lot_id=lot_id, expected_version=expected_version, quantity=quantity,
        location_id=location_id, operator_id=operator_id, idempotency_key=idempotency_key,
        require_staging_source=False, require_pending_source=True,
        expected_target_layout_version=expected_target_layout_version,
        expected_target_address_version=expected_target_address_version,
        expected_target_map_revision=expected_target_map_revision,
    )


def transfer_staging_finished_lot(
    db: Session,
    *,
    lot_id: int,
    expected_version: int,
    quantity: int,
    location_id: int,
    operator_id: int | None,
    idempotency_key: str,
    expected_target_layout_version: int | None = None,
) -> FinishedLotLocationTransferResult:
    return _transfer_finished_lot_location(
        db,
        lot_id=lot_id,
        expected_version=expected_version,
        quantity=quantity,
        location_id=location_id,
        operator_id=operator_id,
        idempotency_key=idempotency_key,
        require_staging_source=True,
        expected_target_layout_version=expected_target_layout_version,
    )


def transfer_finished_lot_between_locations(
    db: Session,
    *,
    lot_id: int,
    expected_version: int,
    quantity: int,
    location_id: int,
    operator_id: int | None,
    idempotency_key: str,
    require_empty_target: bool = False,
    expected_source_location_id: int | None = None,
    expected_source_address_version: int | None = None,
    expected_source_layout_version: int | None = None,
    expected_source_map_revision: str | None = None,
    expected_target_layout_version: int | None = None,
    expected_target_address_version: int | None = None,
    expected_target_map_revision: str | None = None,
    ground_secondary_location_id: int | None = None,
    ground_capacity_quantity: int | None = None,
    reserved_plan: dict[int, int] | None = None,
) -> FinishedLotLocationTransferResult:
    return _transfer_finished_lot_location(
        db,
        lot_id=lot_id,
        expected_version=expected_version,
        quantity=quantity,
        location_id=location_id,
        operator_id=operator_id,
        idempotency_key=idempotency_key,
        require_staging_source=False,
        require_empty_target=require_empty_target,
        expected_source_location_id=expected_source_location_id,
        expected_source_address_version=expected_source_address_version,
        expected_source_layout_version=expected_source_layout_version,
        expected_source_map_revision=expected_source_map_revision,
        expected_target_layout_version=expected_target_layout_version,
        expected_target_address_version=expected_target_address_version,
        expected_target_map_revision=expected_target_map_revision,
        ground_secondary_location_id=ground_secondary_location_id,
        ground_capacity_quantity=ground_capacity_quantity,
        reserved_plan=reserved_plan,
    )


def _frozen_product_has_stock(db: Session, order_item_id: int, product_id: int) -> bool:
    from app.services.multilevel_bom_orders import read_order_graph
    from app.services.multilevel_bom_plan import BomPlanError
    try:
        graph = read_order_graph(db, order_item_id)
        if graph is None:
            return True
        node = next((node for node in graph.nodes if node.product_id == product_id), None)
        if node is None:
            raise BomPlanError("库存产品不属于订单冻结BOM")
        return node.source != "separate"
    except BomPlanError as error:
        raise WarehouseInventoryError(str(error), 409) from error


def manual_finished_in(
    db: Session,
    *,
    customer_id: int | None,
    product_id: int,
    location_id: int,
    quantity: int,
    stock_date: date,
    source_type: str,
    remarks: str | None,
    operator_id: int | None,
    idempotency_key: str | None,
    source_ref_type: str | None = None,
    source_ref_id: int | None = None,
    pallet_id: int | None = None,
    pallet_code: str | None = None,
    require_empty_pallet: bool = False,
    movement_reason: str = "手工成品入库",
    stock_date_accuracy: str = "exact",
    stock_date_original_text: str | None = None,
    is_general: bool = False,
    expected_layout_version: int | None = None,
    ground_secondary_location_id: int | None = None,
    ground_capacity_quantity: int | None = None,
    physical_basis_json: str | None = None,
) -> InventoryLot:
    existing = _idempotent_lot(db, idempotency_key)
    if existing:
        _ensure_finished_projection_postcondition(
            db,
            lot=existing,
            operator_id=operator_id,
            create_missing=False,
        )
        return existing
    if quantity <= 0:
        raise WarehouseInventoryError("入库数量必须大于0")
    from app.models.multilevel_bom import ProductBomProfile
    profile = db.get(ProductBomProfile, product_id)
    completion = None
    if source_ref_type == "production_completion" and source_ref_id is not None:
        from app.models.production import ProductionCompletion
        completion = db.get(ProductionCompletion, source_ref_id)
    if completion is not None:
        if not _frozen_product_has_stock(db, completion.order_item_id, product_id):
            raise WarehouseInventoryError("冻结规则为子件分存，不能生成父件库存", 409)
    elif profile is not None and profile.source == "separate":
        raise WarehouseInventoryError("组合父件只表示需求，请分别选择真实子件入库", 409)
    _claim_inventory_destination(
        db,
        location_id,
        expected_layout_version=expected_layout_version,
    )
    capacity_source_location_id: int | None = None
    if pallet_id is not None:
        supplied_pallet = db.get(InventoryPallet, int(pallet_id))
        if (
            supplied_pallet is not None
            and supplied_pallet.is_current
            and str(supplied_pallet.status or "").strip().lower() == "active"
            and supplied_pallet.location_id == int(location_id)
        ):
            capacity_source_location_id = int(location_id)
    location = _location(
        db,
        location_id,
        "finished",
        capacity_source_location_id=capacity_source_location_id,
    )
    customer = db.get(Customer, customer_id) if customer_id is not None else None
    product = db.get(Product, product_id)
    if not is_general and customer is None:
        raise WarehouseInventoryError("客户不存在", 404)
    if product is None or product.deleted_at is not None:
        raise WarehouseInventoryError("产品不存在", 404)
    if not is_general and product.customer_id != customer_id:
        raise WarehouseInventoryError("所选产品不属于该客户")
    date_accuracy, date_original_text = normalize_stock_date_metadata(
        stock_date=stock_date,
        stock_date_accuracy=stock_date_accuracy,
        stock_date_original_text=stock_date_original_text,
    )
    now = utc_now_naive()
    material_code = (
        product.material.code if product.material is not None else product.default_material_code
    ) or product.legacy_material_text
    lot = InventoryLot(
        lot_number=_number("FG"),
        inventory_type="finished",
        warehouse_location_id=location_id,
        quantity_available=quantity,
        unit="boxes",
        status="active",
        source_type=source_type,
        source_ref_type=source_ref_type,
        source_ref_id=source_ref_id,
        stock_date=stock_date,
        stock_date_accuracy=date_accuracy,
        stock_date_original_text=date_original_text,
        last_movement_at=now,
        remarks=remarks,
        created_by=operator_id,
    )
    apply_cost_snapshot(
        lot,
        estimate_finished_product_cost(
            db,
            product=product,
            material_code=material_code,
            flute_type=product.flute_type,
        ),
        captured_at=now,
    )
    db.add(lot)
    db.flush()
    from app.services.finished_stock_identity import product_basis, order_product_basis
    if physical_basis_json is None and completion is not None:
        physical_basis_json = order_product_basis(db, completion.order_item_id, product_id)
    lot.finished_detail = FinishedGoodsInventoryDetail(
        owner_customer_id=customer.id if customer is not None and not is_general else None,
        owner_customer_name_snapshot=(
            customer.name if customer is not None and not is_general else None
        ),
        is_general=is_general,
        product_id=product.id,
        inventory_code_snapshot=product.product_code,
        product_name_snapshot=product.product_name,
        box_type_snapshot=product.box_style,
        length_mm=round(product.length_mm) if product.length_mm is not None else None,
        width_mm=round(product.width_mm) if product.width_mm is not None else None,
        height_mm=round(product.height_mm) if product.height_mm is not None else None,
        material_code_snapshot=material_code,
        flute_type_snapshot=product.flute_type,
        physical_basis_json=physical_basis_json or product_basis(product),
    )
    _movement(
        db,
        lot=lot,
        movement_type="manual_in",
        quantity=quantity,
        before={key: 0 for key in _balances(lot)},
        operator_id=operator_id,
        reason=movement_reason,
        remarks=remarks,
        idempotency_key=idempotency_key,
    )
    _ensure_finished_projection_postcondition(
        db,
        lot=lot,
        operator_id=operator_id,
        create_missing=True,
        pallet_id=pallet_id,
        pallet_code=pallet_code,
        require_empty_pallet=require_empty_pallet,
        ground_secondary_location_id=ground_secondary_location_id,
        ground_capacity_quantity=ground_capacity_quantity,
    )
    from app.services.fixed_shelf import on_finished_in
    on_finished_in(db, lot)
    db.flush()
    return lot


def active_finished_reserved_qty(db: Session, order_item_id: int) -> int:
    """Return canonical order coverage: delivered plus unconsumed reservation.

    Consumed reservation credit normally becomes ``delivered_quantity``.  The
    two facts are kept independent because formal receipt reconciliation and
    legacy imports can adjust delivery without mutating reservation history.
    Adding delivered to *remaining* credit avoids both omission and double
    counting.
    """

    delivered_quantity = int(
        db.scalar(
            select(OrderItem.delivered_quantity).where(
                OrderItem.id == int(order_item_id)
            )
        )
        or 0
    )
    rows = db.scalars(
        select(InventoryReservation).where(
            InventoryReservation.order_item_id == order_item_id,
            InventoryReservation.reservation_type == "finished_order",
            InventoryReservation.sales_order_item_bom_component_id.is_(None),
            InventoryReservation.status != "cancelled",
        )
    ).all()
    remaining_reserved = sum(
        max(
            int(row.credited_requirement_quantity or 0)
            - int(row.consumed_requirement_quantity or 0)
            - int(row.released_requirement_quantity or 0),
            0,
        )
        for row in rows
    )
    return max(delivered_quantity, 0) + remaining_reserved


def active_finished_reservations_by_item_ids(
    db: Session, order_item_ids: list[int]
) -> dict[int, int]:
    if not order_item_ids:
        return {}
    delivered_by_item_id = {
        int(order_item_id): max(int(delivered_quantity or 0), 0)
        for order_item_id, delivered_quantity in db.execute(
            select(OrderItem.id, OrderItem.delivered_quantity).where(
                OrderItem.id.in_(order_item_ids)
            )
        )
    }
    rows = db.scalars(
        select(InventoryReservation).where(
            InventoryReservation.order_item_id.in_(order_item_ids),
            InventoryReservation.reservation_type == "finished_order",
            InventoryReservation.sales_order_item_bom_component_id.is_(None),
            InventoryReservation.status != "cancelled",
        )
    ).all()
    result: dict[int, int] = dict(delivered_by_item_id)
    for row in rows:
        if row.order_item_id is None:
            continue
        result[row.order_item_id] = result.get(row.order_item_id, 0) + max(
            int(row.credited_requirement_quantity or 0)
            - int(row.consumed_requirement_quantity or 0)
            - int(row.released_requirement_quantity or 0),
            0,
        )
    return result


def requisition_finished_inventory_coverage_by_item_ids(
    db: Session,
    order_item_ids: list[int],
) -> dict[int, int]:
    """Return finished-stock coverage that can replace supplier material.

    A finished lot produced by the same order item is an output of material
    already reported for that order, not an independent stock deduction.
    Counting both its consumed reservation and the supplier report hides the
    still-unreported remainder after a partial production/delivery cycle.
    Pre-existing stock and production output from another order remain valid
    coverage.
    """

    clean_ids = sorted(
        {int(order_item_id) for order_item_id in order_item_ids if order_item_id}
    )
    if not clean_ids:
        return {}
    rows = db.execute(
        select(
            InventoryReservation,
            ProductionCompletion.order_item_id.label(
                "source_completion_order_item_id"
            ),
        )
        .join(
            InventoryLot,
            InventoryLot.id == InventoryReservation.inventory_lot_id,
        )
        .outerjoin(
            ProductionCompletion,
            and_(
                InventoryLot.source_ref_type == "production_completion",
                InventoryLot.source_ref_id == ProductionCompletion.id,
            ),
        )
        .where(
            InventoryReservation.order_item_id.in_(clean_ids),
            InventoryReservation.reservation_type == "finished_order",
            InventoryReservation.sales_order_item_bom_component_id.is_(None),
            InventoryReservation.status != "cancelled",
        )
        .order_by(InventoryReservation.id)
    ).all()
    result: dict[int, int] = {}
    for reservation, source_completion_order_item_id in rows:
        order_item_id = int(reservation.order_item_id or 0)
        if order_item_id <= 0:
            continue
        if (
            source_completion_order_item_id is not None
            and int(source_completion_order_item_id) == order_item_id
        ):
            continue
        result[order_item_id] = result.get(order_item_id, 0) + max(
            int(reservation.credited_requirement_quantity or 0)
            - int(reservation.released_requirement_quantity or 0),
            0,
        )
    return result


def requisition_finished_inventory_coverage_qty(
    db: Session,
    order_item_id: int,
) -> int:
    return requisition_finished_inventory_coverage_by_item_ids(
        db,
        [order_item_id],
    ).get(int(order_item_id), 0)


def active_finished_component_reserved_qty(db: Session, snapshot_id: int) -> int:
    """Return only un-released formal finished stock bound to one BOM snapshot.

    This intentionally excludes ordinary parent-order reservations: a component
    lot must never make the parent product look like it has already been covered.
    """
    rows = db.scalars(
        select(InventoryReservation).where(
            InventoryReservation.sales_order_item_bom_component_id == snapshot_id,
            InventoryReservation.reservation_type == "finished_order",
            InventoryReservation.status != "cancelled",
        )
    ).all()
    return sum(
        max(
            int(row.credited_requirement_quantity or 0)
            - int(row.released_requirement_quantity or 0),
            0,
        )
        for row in rows
    )


def component_effective_required_piece_qty(
    db: Session, snapshot: SalesOrderItemBomComponent
) -> int:
    """Immutable snapshot quantity plus append-only order-specific adjustments."""
    delta = db.scalar(
        select(func.coalesce(func.sum(
            SalesOrderItemBomDemandAdjustment.delta_required_piece_quantity
        ), 0)).where(
            SalesOrderItemBomDemandAdjustment.sales_order_item_bom_component_id
            == snapshot.id
        )
    )
    return max(int(snapshot.required_piece_quantity or 0) + int(delta or 0), 0)


def component_inventory_coverage(
    db: Session,
    snapshot_id: int,
    *,
    component_type: str = "whole",
) -> dict[str, int]:
    """One authoritative component coverage view for reserve and requisition.

    Formal finished stock is one complete component product, so one reserved A3
    component covers one cover and one base.  Semi-finished stock is a physical
    board and must remain scoped to whole/cover/base.  Historical `whole`
    requirements continue to cover the complete legacy snapshot.
    """
    component = (component_type or "").strip().lower()
    if component not in {"whole", "cover", "base"}:
        raise WarehouseInventoryError(
            "组合组件库存类型仅允许 whole、cover 或 base"
        )
    snapshot = db.get(SalesOrderItemBomComponent, snapshot_id)
    if snapshot is None:
        raise WarehouseInventoryError("组件快照不存在", 404)
    physical_pieces_per_component = 1
    if component == "whole":
        frozen_value = int(snapshot.snapshot_component_pieces_per_box or 0)
        if frozen_value > 0:
            physical_pieces_per_component = frozen_value
        elif (
            snapshot.snapshot_component_splice_mode or ""
        ).strip().lower() == "double":
            physical_pieces_per_component = 2
    finished = (
        active_finished_component_reserved_qty(db, snapshot_id)
        * physical_pieces_per_component
    )
    # `semi_requirement_id` was the only link before the explicit snapshot
    # field existed. Keep that historical link in the one coverage view so an
    # older, still-active reservation cannot be counted a second time by a
    # later finished-goods reservation.
    semi_rows = db.scalars(
        select(InventoryReservation)
        .outerjoin(
            OrderItemSemiRequirement,
            OrderItemSemiRequirement.id == InventoryReservation.semi_requirement_id,
        )
        .where(
            InventoryReservation.reservation_type == "semi_order",
            InventoryReservation.status != "cancelled",
            (
                or_(
                    OrderItemSemiRequirement.id.is_(None),
                    OrderItemSemiRequirement.component_type == "whole",
                )
                if component == "whole"
                else or_(
                    OrderItemSemiRequirement.id.is_(None),
                    OrderItemSemiRequirement.component_type.in_(
                        [component, "whole"]
                    ),
                )
            ),
            or_(
                InventoryReservation.sales_order_item_bom_component_id == snapshot_id,
                OrderItemSemiRequirement.sales_order_item_bom_component_id
                == snapshot_id,
            ),
        )
    ).all()
    semi = sum(
        max(
            int(row.credited_requirement_quantity or 0)
            - int(row.released_requirement_quantity or 0),
            0,
        )
        for row in semi_rows
    )
    return {
        "finished_piece_quantity": finished,
        "semi_piece_quantity": semi,
        "total_piece_quantity": finished + semi,
    }


def finished_inventory_candidates_for_bom_component(
    db: Session, *, order_item_id: int, bom_snapshot_id: int
) -> list[InventoryLot]:
    """Candidates for an internal component, limited to the owning customer.

    General stock is included solely so the caller can display its explicit
    warning; it is never silently selected by the reservation service.
    """
    from app.services.fixed_shelf_staging import held_for_staging_expression
    snapshot = db.get(SalesOrderItemBomComponent, bom_snapshot_id)
    item = db.get(OrderItem, order_item_id)
    if snapshot is None or item is None or snapshot.sales_order_item_id != item.id:
        raise WarehouseInventoryError("组件快照不属于当前订单明细", 409)
    order = db.get(Order, item.order_id)
    if order is None:
        raise WarehouseInventoryError("订单不存在", 404)
    if not _frozen_product_has_stock(db, item.id, snapshot.component_product_id):
        return []
    rows = db.scalars(
        select(InventoryLot)
        .join(FinishedGoodsInventoryDetail,
              FinishedGoodsInventoryDetail.inventory_lot_id == InventoryLot.id)
        .where(
            InventoryLot.inventory_type == "finished",
            InventoryLot.status == "active",
            InventoryLot.quantity_available > 0,
            FinishedGoodsInventoryDetail.product_id == snapshot.component_product_id,
            ~held_for_staging_expression(),
            or_(
                FinishedGoodsInventoryDetail.owner_customer_id == order.customer_id,
                FinishedGoodsInventoryDetail.is_general.is_(True),
            ),
        )
        .order_by(FinishedGoodsInventoryDetail.is_general, InventoryLot.stock_date, InventoryLot.id)
    ).all()
    from app.services.finished_stock_identity import order_product_basis
    expected = order_product_basis(db, item.id, snapshot.component_product_id)
    return [lot for lot in rows if expected is None or lot.finished_detail.physical_basis_json == expected]


def reserve_finished_inventory_for_bom_component(
    db: Session,
    *,
    order_item_id: int,
    bom_snapshot_id: int,
    inventory_lot_id: int,
    quantity: int,
    expected_version: int,
    operator_id: int | None,
    idempotency_key: str,
    warning_acknowledged_codes: list[str],
) -> InventoryReservation:
    """Reserve formal component stock without changing parent-order coverage."""
    existing = db.scalar(select(InventoryReservation).where(
        InventoryReservation.idempotency_key == idempotency_key
    ))
    if existing is not None:
        if (
            existing.order_item_id != order_item_id
            or existing.inventory_lot_id != inventory_lot_id
            or existing.sales_order_item_bom_component_id != bom_snapshot_id
            or int(existing.reserved_stock_quantity or 0) != quantity
        ):
            raise WarehouseInventoryError("该请求标识已用于其他组件库存预占", 409)
        return existing
    if quantity <= 0:
        raise WarehouseInventoryError("组件成品库存抵扣数量必须大于0")
    row = db.execute(select(OrderItem, Order).join(Order, Order.id == OrderItem.order_id)
                     .where(OrderItem.id == order_item_id)).one_or_none()
    snapshot = db.get(SalesOrderItemBomComponent, bom_snapshot_id)
    if row is None or snapshot is None or snapshot.sales_order_item_id != order_item_id:
        raise WarehouseInventoryError("组件快照不属于当前订单明细", 409)
    item, order = row
    if not _frozen_product_has_stock(db, item.id, snapshot.component_product_id):
        raise WarehouseInventoryError("组合需求父件不能预占实体库存，请分别预占真实子件", 409)
    from app.services.production_workflow import has_production_completion_facts, lock_order_rows_for_production_transition, ProductionWorkflowError
    try:
        lock_order_rows_for_production_transition(db, [order.id])
    except ProductionWorkflowError as error:
        raise WarehouseInventoryError(str(error), error.status_code) from error
    if item.requisition_status != "未报料" or item.material_status == "received" or item.delivered_quantity > 0:
        raise WarehouseInventoryError("订单已进入后续流程，不能新增组件成品库存抵扣", 409)
    if has_production_completion_facts(db, [item.id]):
        raise WarehouseInventoryError("订单明细已完成生产，不能再新增组件成品库存抵扣", 409)
    lot = db.get(InventoryLot, inventory_lot_id)
    if lot is None or lot.finished_detail is None or lot.inventory_type != "finished" or lot.status != "active":
        raise WarehouseInventoryError("组件成品库存批次当前不可预占", 409)
    from app.services.bom_subkits import require_free_subkit_stock, SubkitError
    try:
        require_free_subkit_stock(db, lot)
    except SubkitError as error:
        raise WarehouseInventoryError(str(error), error.status_code) from error
    from app.services.fixed_shelf_staging import staging_owner
    if staging_owner(db, lot.id):
        raise WarehouseInventoryError("该批次已为送货单集货，不能重复预占", 409)
    detail = lot.finished_detail
    if detail.product_id != snapshot.component_product_id:
        raise WarehouseInventoryError("库存产品与组件不一致", 409)
    from app.services.finished_stock_identity import matching_component_basis
    if not matching_component_basis(db, snapshot, lot):
        raise WarehouseInventoryError("库存缺少匹配的冻结规格、单位或工艺依据，请先核实该批次身份", 409)
    warnings: list[str] = []
    if detail.is_general:
        warnings.append("GENERAL_FINISHED_STOCK")
        if "GENERAL_FINISHED_STOCK" not in warning_acknowledged_codes:
            raise WarehouseInventoryError("通用组件成品库存必须人工确认后才能使用", 409)
    elif detail.owner_customer_id != order.customer_id:
        raise WarehouseInventoryError("客户专用组件库存不能用于其他客户订单", 409)
    physical_pieces_per_component = int(
        snapshot.snapshot_component_pieces_per_box or 0
    )
    if physical_pieces_per_component <= 0:
        physical_pieces_per_component = (
            2
            if (snapshot.snapshot_component_splice_mode or "")
            .strip()
            .lower()
            == "double"
            else 1
        )
    coverage = component_inventory_coverage(db, bom_snapshot_id)
    remaining_physical_pieces = max(
        component_effective_required_piece_qty(db, snapshot)
        * physical_pieces_per_component
        - int(coverage["total_piece_quantity"]),
        0,
    )
    remaining_component_units = (
        remaining_physical_pieces // physical_pieces_per_component
    )
    if quantity > remaining_component_units:
        raise WarehouseInventoryError(
            f"组件成品抵扣数量不能超过剩余 {remaining_component_units}",
            409,
        )
    if lot.version != expected_version or quantity > int(lot.quantity_available or 0):
        raise WarehouseInventoryError("库存数量或版本已变化，请刷新后重试", 409)
    before = _balances(lot)
    now = utc_now_naive()
    result = db.execute(update(InventoryLot).where(
        InventoryLot.id == lot.id, InventoryLot.version == expected_version,
        InventoryLot.quantity_available >= quantity, InventoryLot.inventory_type == "finished",
        InventoryLot.status == "active",
    ).values(quantity_available=InventoryLot.quantity_available - quantity,
             quantity_reserved=InventoryLot.quantity_reserved + quantity,
             version=InventoryLot.version + 1, last_movement_at=now))
    if result.rowcount != 1:
        raise WarehouseInventoryError("库存数量或版本已变化，请刷新后重试", 409)
    reservation = InventoryReservation(
        reservation_number=_number("BRS"), inventory_lot_id=lot.id,
        reservation_type="finished_order", order_id=order.id, order_item_id=item.id,
        sales_order_item_bom_component_id=snapshot.id,
        reserved_stock_quantity=quantity, credited_requirement_quantity=quantity,
        yield_factor=1, status="active", warning_codes=json.dumps(warnings, ensure_ascii=False),
        warning_acknowledged_by=operator_id if warnings else None,
        reserved_by=operator_id, reserved_at=now, idempotency_key=idempotency_key,
    )
    db.add(reservation); db.flush(); db.expire(lot)
    refreshed = db.get(InventoryLot, lot.id)
    assert refreshed is not None
    _movement(db, lot=refreshed, movement_type="reserve", quantity=quantity, before=before,
              operator_id=operator_id, reason="组合 BOM 组件成品库存抵扣",
              idempotency_key=idempotency_key, reservation_id=reservation.id,
              related_order_id=order.id, related_order_item_id=item.id)
    db.flush()
    return reservation


def _finished_reservation_pre_requisition(item: OrderItem) -> bool:
    return item.requisition_status == "未报料" or (
        item.supply_mode_snapshot == "external_purchase"
        and item.requisition_status == "外购包材待确认"
    )


def finished_inventory_candidates(db: Session, order_item_id: int) -> list[InventoryLot]:
    from app.services.fixed_shelf_staging import held_for_staging_expression
    row = db.execute(
        select(OrderItem, Order)
        .join(Order, Order.id == OrderItem.order_id)
        .where(OrderItem.id == order_item_id)
    ).one_or_none()
    if row is None:
        raise WarehouseInventoryError("订单明细不存在", 404)
    item, order = row
    if not _frozen_product_has_stock(db, item.id, item.product_id):
        return []
    from app.services.production_workflow import has_production_completion_facts

    if has_production_completion_facts(db, [item.id]):
        raise WarehouseInventoryError(
            "订单明细已完成生产，不能再新增成品库存抵扣", 409
        )
    if not _finished_reservation_pre_requisition(item):
        raise WarehouseInventoryError("订单已进入报料，请先取消报料后再抵扣成品库存", 409)
    rows = db.scalars(
        select(InventoryLot)
        .join(
            FinishedGoodsInventoryDetail,
            FinishedGoodsInventoryDetail.inventory_lot_id == InventoryLot.id,
        )
        .where(
            InventoryLot.inventory_type == "finished",
            InventoryLot.status == "active",
            InventoryLot.quantity_available > 0,
            FinishedGoodsInventoryDetail.product_id == item.product_id,
            ~held_for_staging_expression(),
            or_(
                FinishedGoodsInventoryDetail.owner_customer_id == order.customer_id,
                FinishedGoodsInventoryDetail.is_general.is_(True),
            ),
        )
        .order_by(
            FinishedGoodsInventoryDetail.is_general,
            *inventory_fifo_order_columns(),
        )
    ).all()
    from app.services.finished_stock_identity import order_product_basis
    expected = order_product_basis(db, item.id, item.product_id)
    return [lot for lot in rows if expected is None or lot.finished_detail.physical_basis_json == expected]


def finished_inventory_candidates_for_product(
    db: Session,
    *,
    customer_id: int,
    product_id: int,
) -> list[InventoryLot]:
    product = db.get(Product, product_id)
    if product is None or product.deleted_at is not None:
        raise WarehouseInventoryError("产品不存在", 404)
    if product.customer_id != customer_id:
        raise WarehouseInventoryError("产品不属于所选客户", 409)
    from app.services.fixed_shelf_staging import held_for_staging_expression
    return db.scalars(
        select(InventoryLot)
        .join(
            FinishedGoodsInventoryDetail,
            FinishedGoodsInventoryDetail.inventory_lot_id == InventoryLot.id,
        )
        .where(
            InventoryLot.inventory_type == "finished",
            InventoryLot.status == "active",
            InventoryLot.quantity_available > 0,
            ~held_for_staging_expression(),
            FinishedGoodsInventoryDetail.product_id == product_id,
            FinishedGoodsInventoryDetail.owner_customer_id == customer_id,
            FinishedGoodsInventoryDetail.is_general.is_(False),
            FinishedGoodsInventoryDetail.inventory_code_snapshot
            == product.product_code,
        )
        .order_by(*inventory_fifo_order_columns())
    ).all()


def has_unconsumed_inventory_reservations(
    db: Session,
    order_item_id: int,
) -> bool:
    return (
        db.scalar(
            select(InventoryReservation.id)
            .where(
                InventoryReservation.order_item_id == order_item_id,
                InventoryReservation.status != "cancelled",
                InventoryReservation.reserved_stock_quantity
                > InventoryReservation.consumed_stock_quantity
                + InventoryReservation.released_stock_quantity,
            )
            .limit(1)
        )
        is not None
    )


def reserve_finished_inventory(
    db: Session,
    *,
    order_item_id: int,
    inventory_lot_id: int,
    quantity: int,
    expected_version: int,
    operator_id: int | None,
    idempotency_key: str,
    warning_acknowledged_codes: list[str],
) -> InventoryReservation:
    existing = db.scalar(
        select(InventoryReservation).where(
            InventoryReservation.idempotency_key == idempotency_key
        )
    )
    if existing:
        if (
            existing.order_item_id != order_item_id
            or existing.inventory_lot_id != inventory_lot_id
        ):
            raise WarehouseInventoryError("该请求标识已用于其他库存预占", 409)
        return existing
    if quantity <= 0:
        raise WarehouseInventoryError("成品库存抵扣数量必须大于0")
    row = db.execute(
        select(OrderItem, Order)
        .join(Order, Order.id == OrderItem.order_id)
        .where(OrderItem.id == order_item_id)
    ).one_or_none()
    if row is None:
        raise WarehouseInventoryError("订单明细不存在", 404)
    item, order = row
    if not _frozen_product_has_stock(db, item.id, item.product_id):
        raise WarehouseInventoryError("本单子件分存，请分别选择子件库存抵扣", 409)
    # Use the same order-row lock as production completion and order terminal
    # transitions, then re-read under that lock before creating a reservation.
    from app.services.production_workflow import (
        ProductionWorkflowError,
        has_production_completion_facts,
        lock_order_rows_for_production_transition,
    )

    try:
        lock_order_rows_for_production_transition(db, [order.id])
    except ProductionWorkflowError as error:
        raise WarehouseInventoryError(str(error), error.status_code) from error
    row = db.execute(
        select(OrderItem, Order)
        .join(Order, Order.id == OrderItem.order_id)
        .where(OrderItem.id == order_item_id)
        .execution_options(populate_existing=True)
    ).one_or_none()
    if row is None:
        raise WarehouseInventoryError("订单明细不存在或已被删除", 409)
    item, order = row
    if has_production_completion_facts(db, [item.id]):
        raise WarehouseInventoryError(
            "订单明细已完成生产，不能再新增成品库存抵扣", 409
        )
    if not _finished_reservation_pre_requisition(item):
        raise WarehouseInventoryError("订单已进入报料，请先取消报料后再抵扣成品库存", 409)
    if item.material_status == "received" or item.delivered_quantity > 0:
        raise WarehouseInventoryError("订单明细已进入后续流程，不能新增成品库存抵扣", 409)
    active_semi_reservation = db.scalar(
        select(InventoryReservation.id)
        .where(
            InventoryReservation.order_item_id == item.id,
            InventoryReservation.reservation_type == "semi_order",
            InventoryReservation.status != "cancelled",
            InventoryReservation.reserved_stock_quantity
            > InventoryReservation.consumed_stock_quantity
            + InventoryReservation.released_stock_quantity,
        )
        .limit(1)
    )
    if active_semi_reservation is not None:
        raise WarehouseInventoryError(
            "订单已有半成品库存预占，请先取消半成品抵扣后再改用成品库存",
            409,
        )
    lot = db.get(InventoryLot, inventory_lot_id)
    if lot is None or lot.finished_detail is None:
        raise WarehouseInventoryError("成品库存批次不存在", 404)
    detail = lot.finished_detail
    if lot.inventory_type != "finished" or lot.status != "active":
        raise WarehouseInventoryError("该库存批次当前不可预占", 409)
    from app.services.bom_subkits import require_free_subkit_stock, SubkitError
    try:
        require_free_subkit_stock(db, lot)
    except SubkitError as error:
        raise WarehouseInventoryError(str(error), error.status_code) from error
    from app.services.fixed_shelf_staging import staging_owner
    if staging_owner(db, lot.id):
        raise WarehouseInventoryError('该批次已集货待送，不能再抵扣其他订单', 409)
    if detail.product_id != item.product_id:
        raise WarehouseInventoryError("库存产品与订单产品不一致")
    from app.services.finished_stock_identity import order_product_basis
    expected_basis = order_product_basis(db, item.id, item.product_id)
    if expected_basis is not None and detail.physical_basis_json != expected_basis:
        raise WarehouseInventoryError("库存缺少匹配的冻结规格、单位或工艺依据，请先核实该批次身份", 409)
    if not detail.is_general and detail.owner_customer_id != order.customer_id:
        raise WarehouseInventoryError("客户专用库存不能用于其他客户订单")
    warning_codes: list[str] = []
    if detail.is_general:
        warning_codes.append("GENERAL_FINISHED_STOCK")
        if "GENERAL_FINISHED_STOCK" not in warning_acknowledged_codes:
            raise WarehouseInventoryError("通用库存必须人工确认后才能用于该客户订单")
    already_reserved = active_finished_reserved_qty(db, item.id)
    remaining_requirement = max(item.quantity - already_reserved, 0)
    if quantity > remaining_requirement:
        raise WarehouseInventoryError(
            f"抵扣数量不能超过订单剩余可抵扣数量 {remaining_requirement}"
        )
    if lot.version != expected_version:
        raise WarehouseInventoryError("库存已被其他人修改，请刷新候选后重试", 409)
    if quantity > lot.quantity_available:
        raise WarehouseInventoryError("抵扣数量不能超过库存可用数量", 409)
    before = _balances(lot)
    now = utc_now_naive()
    result = db.execute(
        update(InventoryLot)
        .where(
            InventoryLot.id == lot.id,
            InventoryLot.version == expected_version,
            InventoryLot.inventory_type == "finished",
            InventoryLot.status == "active",
            InventoryLot.quantity_available >= quantity,
        )
        .values(
            quantity_available=InventoryLot.quantity_available - quantity,
            quantity_reserved=InventoryLot.quantity_reserved + quantity,
            version=InventoryLot.version + 1,
            last_movement_at=now,
        )
    )
    if result.rowcount != 1:
        raise WarehouseInventoryError("库存数量或版本已变化，请刷新候选后重试", 409)
    reservation = InventoryReservation(
        reservation_number=_number("RS"),
        inventory_lot_id=lot.id,
        reservation_type="finished_order",
        order_id=order.id,
        order_item_id=item.id,
        reserved_stock_quantity=quantity,
        credited_requirement_quantity=quantity,
        yield_factor=1,
        status="active",
        warning_codes=json.dumps(warning_codes, ensure_ascii=False),
        warning_acknowledged_by=operator_id if warning_codes else None,
        reserved_by=operator_id,
        reserved_at=now,
        idempotency_key=idempotency_key,
    )
    db.add(reservation)
    db.flush()
    db.expire(lot)
    lot = db.get(InventoryLot, lot.id)
    assert lot is not None
    _movement(
        db,
        lot=lot,
        movement_type="reserve",
        quantity=quantity,
        before=before,
        operator_id=operator_id,
        reason="成品库存抵扣订单",
        idempotency_key=idempotency_key,
        reservation_id=reservation.id,
        related_order_id=order.id,
        related_order_item_id=item.id,
    )
    db.flush()
    # N029 tasks are optional for legacy orders.  Refresh only an existing
    # task after the reservation write has succeeded.
    from app.services.production_workflow import refresh_existing_production_task

    refresh_existing_production_task(db, item.id)
    return reservation


def reserve_completed_finished_inventory(
    db: Session,
    *,
    order_item_id: int,
    inventory_lot_id: int,
    quantity: int,
    expected_version: int,
    operator_id: int | None,
    idempotency_key: str,
) -> InventoryReservation:
    """Reserve a production-completion lot for its original order item.

    This is intentionally separate from the operator-facing reservation flow:
    production may complete after requisition or material receipt, but every
    ownership, product, quantity, version and idempotency invariant still
    applies.
    """
    existing = db.scalar(
        select(InventoryReservation).where(
            InventoryReservation.idempotency_key == idempotency_key
        )
    )
    if existing is not None:
        if (
            existing.reservation_type != "finished_order"
            or existing.order_item_id != order_item_id
            or existing.inventory_lot_id != inventory_lot_id
            or int(existing.reserved_stock_quantity or 0) != quantity
            or int(existing.credited_requirement_quantity or 0) != quantity
        ):
            raise WarehouseInventoryError("该请求标识已用于其他库存预占", 409)
        return existing
    if quantity <= 0:
        raise WarehouseInventoryError("生产完工预占数量必须大于0")
    row = db.execute(
        select(OrderItem, Order)
        .join(Order, Order.id == OrderItem.order_id)
        .where(OrderItem.id == order_item_id)
    ).one_or_none()
    if row is None:
        raise WarehouseInventoryError("订单明细不存在", 404)
    item, order = row
    lot = db.get(InventoryLot, inventory_lot_id)
    if lot is None or lot.finished_detail is None:
        raise WarehouseInventoryError("生产完工成品库存批次不存在", 404)
    detail = lot.finished_detail
    if (
        lot.inventory_type != "finished"
        or lot.status != "active"
        or lot.source_type not in {"production_surplus", "production_completion"}
        or lot.source_ref_type != "production_completion"
        or lot.source_ref_id is None
    ):
        raise WarehouseInventoryError("该库存批次不是有效的生产完工入库", 409)
    from app.models.production import ProductionCompletion

    completion = db.get(ProductionCompletion, lot.source_ref_id)
    if (
        completion is None
        or completion.order_item_id != item.id
        or int(
            completion.actual_output_quantity
            if lot.source_type == "production_completion"
            else (completion.stock_quantity or completion.quantity or 0)
        )
        != int(lot.quantity_available or 0)
    ):
        raise WarehouseInventoryError("完工库存与生产完工事实不一致", 409)
    if detail.product_id != item.product_id:
        raise WarehouseInventoryError("完工库存产品与订单产品不一致", 409)
    if detail.is_general or detail.owner_customer_id != order.customer_id:
        raise WarehouseInventoryError("完工库存客户与订单客户不一致", 409)
    if lot.version != expected_version:
        raise WarehouseInventoryError("库存已被其他人修改，请刷新后重试", 409)
    if int(lot.quantity_available or 0) < quantity:
        raise WarehouseInventoryError("完工库存不足以建立订单预占", 409)
    before = _balances(lot)
    now = utc_now_naive()
    result = db.execute(
        update(InventoryLot)
        .where(
            InventoryLot.id == lot.id,
            InventoryLot.version == expected_version,
            InventoryLot.inventory_type == "finished",
            InventoryLot.status == "active",
            InventoryLot.quantity_available >= quantity,
        )
        .values(
            quantity_available=InventoryLot.quantity_available - quantity,
            quantity_reserved=InventoryLot.quantity_reserved + quantity,
            version=InventoryLot.version + 1,
            last_movement_at=now,
        )
    )
    if result.rowcount != 1:
        raise WarehouseInventoryError("库存数量或版本已变化，请刷新后重试", 409)
    reservation = InventoryReservation(
        reservation_number=_number("PRS"),
        inventory_lot_id=lot.id,
        reservation_type="finished_order",
        order_id=order.id,
        order_item_id=item.id,
        reserved_stock_quantity=quantity,
        credited_requirement_quantity=quantity,
        yield_factor=1,
        status="active",
        warning_codes="[]",
        reserved_by=operator_id,
        reserved_at=now,
        idempotency_key=idempotency_key,
    )
    db.add(reservation)
    db.flush()
    db.expire(lot)
    refreshed_lot = db.get(InventoryLot, lot.id)
    assert refreshed_lot is not None
    _movement(
        db,
        lot=refreshed_lot,
        movement_type="reserve",
        quantity=quantity,
        before=before,
        operator_id=operator_id,
        reason="生产完工自动预占",
        idempotency_key=idempotency_key,
        reservation_id=reservation.id,
        related_order_id=order.id,
        related_order_item_id=item.id,
    )
    db.flush()
    return reservation


def reserve_finished_surplus_for_delivery(
    db: Session,
    *,
    order_item_id: int,
    quantity: int,
    operator_id: int | None,
    operation_key: str,
) -> list[InventoryReservation]:
    """Atomically reserve customer-owned surplus solely for over-delivery.

    These reservations never credit the order requirement and therefore are
    excluded from normal order coverage.  They exist only to make the
    additional physical stock consumption auditable and reversible.
    """
    if quantity <= 0:
        return []
    prefix = f"{operation_key}-surplus-"
    repeated = db.scalars(
        select(InventoryReservation)
        .where(
            InventoryReservation.idempotency_key.like(f"{prefix}%"),
            InventoryReservation.reservation_type == "finished_surplus_delivery",
            InventoryReservation.order_item_id == order_item_id,
        )
        .order_by(InventoryReservation.id)
    ).all()
    if repeated:
        if sum(int(row.reserved_stock_quantity) for row in repeated) != quantity:
            raise WarehouseInventoryError("超量送货库存幂等内容不一致", 409)
        return list(repeated)
    row = db.execute(
        select(OrderItem, Order)
        .join(Order, Order.id == OrderItem.order_id)
        .where(OrderItem.id == order_item_id)
    ).one_or_none()
    if row is None:
        raise WarehouseInventoryError("订单明细不存在", 404)
    item, order = row
    lots = db.scalars(
        select(InventoryLot)
        .join(
            FinishedGoodsInventoryDetail,
            FinishedGoodsInventoryDetail.inventory_lot_id == InventoryLot.id,
        )
        .where(
            InventoryLot.inventory_type == "finished",
            InventoryLot.status == "active",
            InventoryLot.quantity_available > 0,
            InventoryLot.source_type.in_(
                ("production_surplus", "production_completion", "transfer")
            ),
            FinishedGoodsInventoryDetail.product_id == item.product_id,
            FinishedGoodsInventoryDetail.is_general.is_(False),
            FinishedGoodsInventoryDetail.owner_customer_id == order.customer_id,
        )
        .order_by(
            case(
                (
                    (InventoryLot.source_ref_type == "production_completion")
                    & (
                        InventoryLot.source_ref_id.in_(
                            select(ProductionCompletion.id).where(
                                ProductionCompletion.order_item_id == item.id,
                                ProductionCompletion.status == "posted",
                            )
                        )
                    ),
                    0,
                ),
                else_=1,
            ),
            *inventory_fifo_order_columns(),
        )
    ).all()
    remaining = quantity
    reservations: list[InventoryReservation] = []
    now = utc_now_naive()
    for lot in lots:
        if remaining <= 0:
            break
        take = min(int(lot.quantity_available or 0), remaining)
        if take <= 0:
            continue
        before = _balances(lot)
        expected_version = int(lot.version)
        updated = db.execute(
            update(InventoryLot)
            .where(
                InventoryLot.id == lot.id,
                InventoryLot.version == expected_version,
                InventoryLot.quantity_available >= take,
            )
            .values(
                quantity_available=InventoryLot.quantity_available - take,
                quantity_reserved=InventoryLot.quantity_reserved + take,
                version=InventoryLot.version + 1,
                last_movement_at=now,
            )
        )
        if updated.rowcount != 1:
            raise WarehouseInventoryError("余货库存已变化，请刷新后重试", 409)
        reservation = InventoryReservation(
            reservation_number=_number("ODS"),
            inventory_lot_id=lot.id,
            reservation_type="finished_surplus_delivery",
            order_id=order.id,
            order_item_id=item.id,
            reserved_stock_quantity=take,
            credited_requirement_quantity=take,
            yield_factor=1,
            status="active",
            warning_codes="[]",
            reserved_by=operator_id,
            reserved_at=now,
            idempotency_key=f"{prefix}{lot.id}",
        )
        db.add(reservation)
        db.flush()
        db.expire(lot)
        refreshed = db.get(InventoryLot, lot.id)
        assert refreshed is not None
        _movement(
            db,
            lot=refreshed,
            movement_type="reserve",
            quantity=take,
            before=before,
            operator_id=operator_id,
            reason="授权超量送货预占客户专用余货",
            idempotency_key=f"{prefix}{lot.id}",
            reservation_id=reservation.id,
            related_order_id=order.id,
            related_order_item_id=item.id,
        )
        reservations.append(reservation)
        remaining -= take
    if remaining > 0:
        raise WarehouseInventoryError(
            f"客户专用成品余货不足，超量送货还缺 {remaining}",
            409,
        )
    return reservations


def _finished_reservation_status(reservation: InventoryReservation) -> str:
    consumed = int(reservation.consumed_stock_quantity or 0)
    released = int(reservation.released_stock_quantity or 0)
    remaining = int(reservation.reserved_stock_quantity) - consumed - released
    if remaining > 0:
        return "active" if consumed == 0 and released == 0 else "partial"
    if consumed == reservation.reserved_stock_quantity:
        return "consumed"
    if released == reservation.reserved_stock_quantity:
        return "released"
    return "partial"


def _finished_idempotent_mutation(
    db: Session,
    *,
    idempotency_key: str,
    movement_type: str,
    reservation_id: int,
) -> FinishedReservationMutation | None:
    movement = db.scalar(
        select(InventoryMovement).where(
            InventoryMovement.idempotency_key == idempotency_key
        )
    )
    if movement is None:
        return None
    if movement.movement_type != movement_type or movement.reservation_id != reservation_id:
        raise WarehouseInventoryError("该请求标识已用于其他库存操作", 409)
    reservation = db.get(InventoryReservation, reservation_id)
    if reservation is None:
        raise WarehouseInventoryError("库存预占记录不存在", 404)
    consume_movement_id = (
        movement.reversal_of_movement_id
        if movement_type == "reverse_consume"
        else movement.id
    )
    allocation = db.scalar(
        select(DeliveryInventoryAllocation).where(
            DeliveryInventoryAllocation.consume_movement_id == consume_movement_id
        )
    )
    return FinishedReservationMutation(reservation, movement, allocation)


def consume_available_semi_finished_lot(
    db: Session,
    *,
    lot_id: int,
    quantity: int,
    expected_version: int,
    operator_id: int | None,
    idempotency_key: str,
    related_order_id: int,
    related_order_item_id: int,
    reason: str,
) -> InventoryMovement:
    """Consume unreserved semi-finished sheets with one auditable CAS update."""

    repeated = db.scalar(
        select(InventoryMovement).where(
            InventoryMovement.idempotency_key == idempotency_key
        )
    )
    if repeated is not None:
        if (
            repeated.inventory_lot_id != int(lot_id)
            or repeated.movement_type != "consume"
            or repeated.reservation_id is not None
            or int(repeated.quantity or 0) != int(quantity)
            or repeated.related_order_item_id != int(related_order_item_id)
        ):
            raise WarehouseInventoryError(
                "该操作标识已用于其它库存扣减，请刷新后重试。", 409
            )
        return repeated
    if int(quantity) <= 0:
        raise WarehouseInventoryError("备库片料扣减数量必须大于0。", 400)
    lot = db.get(InventoryLot, int(lot_id))
    if lot is None:
        raise WarehouseInventoryError("备库片料批次不存在，不能转为成品。", 409)
    if lot.inventory_type != "semi_finished" or lot.status != "active":
        raise WarehouseInventoryError("备库片料已停用或类型不正确，不能转为成品。", 409)
    if int(lot.version) != int(expected_version):
        raise WarehouseInventoryError("备库片料数量已变化，请刷新后重试。", 409)
    if int(lot.quantity_available or 0) < int(quantity):
        raise WarehouseInventoryError(
            f"备库片料不足，本次需要 {int(quantity)} 片，当前可用 {int(lot.quantity_available or 0)} 片。",
            409,
        )
    before = _balances(lot)
    now = utc_now_naive()
    changed = db.execute(
        update(InventoryLot)
        .where(
            InventoryLot.id == int(lot_id),
            InventoryLot.version == int(expected_version),
            InventoryLot.status == "active",
            InventoryLot.inventory_type == "semi_finished",
            InventoryLot.quantity_available >= int(quantity),
        )
        .values(
            quantity_available=InventoryLot.quantity_available - int(quantity),
            quantity_consumed=InventoryLot.quantity_consumed + int(quantity),
            version=InventoryLot.version + 1,
            last_movement_at=now,
        )
    )
    if changed.rowcount != 1:
        raise WarehouseInventoryError("备库片料数量已变化，请刷新后重试。", 409)
    db.flush()
    db.expire(lot)
    refreshed = db.get(InventoryLot, int(lot_id))
    if refreshed is None:
        raise WarehouseInventoryError("备库片料扣减后批次引用丢失，操作已停止。", 409)
    movement = _movement(
        db,
        lot=refreshed,
        movement_type="consume",
        quantity=int(quantity),
        before=before,
        operator_id=operator_id,
        reason=reason,
        idempotency_key=idempotency_key,
        related_order_id=int(related_order_id),
        related_order_item_id=int(related_order_item_id),
    )
    db.flush()
    return movement


def restore_consumed_semi_finished_lot(
    db: Session,
    *,
    lot_id: int,
    quantity: int,
    expected_version: int,
    original_consume_movement_id: int,
    operator_id: int | None,
    idempotency_key: str,
    related_order_id: int,
    related_order_item_id: int,
    reason: str,
) -> InventoryMovement:
    """Restore a traced reserve conversion without inventing new material."""

    repeated = db.scalar(
        select(InventoryMovement).where(
            InventoryMovement.idempotency_key == idempotency_key
        )
    )
    if repeated is not None:
        if (
            repeated.inventory_lot_id != int(lot_id)
            or repeated.movement_type != "reverse_consume"
            or repeated.reversal_of_movement_id != int(original_consume_movement_id)
            or int(repeated.quantity or 0) != int(quantity)
            or repeated.related_order_item_id != int(related_order_item_id)
        ):
            raise WarehouseInventoryError(
                "该操作标识已用于其它备库恢复，请刷新后重试。", 409
            )
        return repeated
    if int(quantity) <= 0:
        raise WarehouseInventoryError("备库恢复数量必须大于0。", 400)
    original = db.get(InventoryMovement, int(original_consume_movement_id))
    if (
        original is None
        or original.inventory_lot_id != int(lot_id)
        or original.movement_type != "consume"
        or original.related_order_item_id != int(related_order_item_id)
    ):
        raise WarehouseInventoryError(
            "备库转换的原扣减流水不完整，不能自动恢复。", 409
        )
    already_reversed = int(
        db.scalar(
            select(func.coalesce(func.sum(InventoryMovement.quantity), 0)).where(
                InventoryMovement.movement_type == "reverse_consume",
                InventoryMovement.reversal_of_movement_id == original.id,
            )
        )
        or 0
    )
    if already_reversed + int(quantity) > int(original.quantity or 0):
        raise WarehouseInventoryError(
            "备库转换恢复数量超过原扣减数量，操作已停止。", 409
        )
    lot = db.get(InventoryLot, int(lot_id))
    if lot is None:
        raise WarehouseInventoryError("备库片料批次不存在，不能恢复。", 409)
    if lot.inventory_type != "semi_finished" or lot.status != "active":
        raise WarehouseInventoryError("备库片料已停用或类型不正确，不能恢复。", 409)
    if int(lot.version) != int(expected_version):
        raise WarehouseInventoryError("备库片料数量已变化，请刷新后重试。", 409)
    if int(lot.quantity_consumed or 0) < int(quantity):
        raise WarehouseInventoryError("备库片料已消耗数量不足，不能自动恢复。", 409)
    before = _balances(lot)
    now = utc_now_naive()
    changed = db.execute(
        update(InventoryLot)
        .where(
            InventoryLot.id == int(lot_id),
            InventoryLot.version == int(expected_version),
            InventoryLot.status == "active",
            InventoryLot.inventory_type == "semi_finished",
            InventoryLot.quantity_consumed >= int(quantity),
        )
        .values(
            quantity_available=InventoryLot.quantity_available + int(quantity),
            quantity_consumed=InventoryLot.quantity_consumed - int(quantity),
            version=InventoryLot.version + 1,
            last_movement_at=now,
        )
    )
    if changed.rowcount != 1:
        raise WarehouseInventoryError("备库片料数量已变化，请刷新后重试。", 409)
    db.flush()
    db.expire(lot)
    refreshed = db.get(InventoryLot, int(lot_id))
    if refreshed is None:
        raise WarehouseInventoryError("备库片料恢复后批次引用丢失，操作已停止。", 409)
    movement = _movement(
        db,
        lot=refreshed,
        movement_type="reverse_consume",
        quantity=int(quantity),
        before=before,
        operator_id=operator_id,
        reason=reason,
        idempotency_key=idempotency_key,
        related_order_id=int(related_order_id),
        related_order_item_id=int(related_order_item_id),
        reversal_of_movement_id=int(original.id),
    )
    db.flush()
    return movement


def consume_finished_reservation(
    db: Session,
    *,
    reservation_id: int,
    stock_quantity: int,
    expected_version: int,
    operator_id: int | None,
    idempotency_key: str,
    delivery_item_id: int,
) -> FinishedReservationMutation:
    repeated = _finished_idempotent_mutation(
        db,
        idempotency_key=idempotency_key,
        movement_type="consume",
        reservation_id=reservation_id,
    )
    if repeated is not None:
        return repeated
    if stock_quantity <= 0:
        raise WarehouseInventoryError("成品消耗数量必须大于0")
    with db.begin_nested():
        reservation = db.get(InventoryReservation, reservation_id)
        if reservation is None:
            raise WarehouseInventoryError("库存预占记录不存在", 404)
        if reservation.reservation_type not in {
            "finished_order",
            "finished_surplus_delivery",
        }:
            raise WarehouseInventoryError("该记录不是成品订单预占")
        remaining = (
            int(reservation.reserved_stock_quantity)
            - int(reservation.consumed_stock_quantity or 0)
            - int(reservation.released_stock_quantity or 0)
        )
        if stock_quantity > remaining:
            raise WarehouseInventoryError("消耗数量不能超过未消耗成品预占余额", 409)
        delivery_item = db.get(DeliveryItem, delivery_item_id)
        if delivery_item is None:
            raise WarehouseInventoryError("送货明细不存在", 404)
        if delivery_item.order_item_id != reservation.order_item_id:
            raise WarehouseInventoryError("送货明细与成品预占订单不一致", 409)
        order_item = db.get(OrderItem, reservation.order_item_id)
        order = db.get(Order, reservation.order_id)
        lot = db.get(InventoryLot, reservation.inventory_lot_id)
        if order_item is None or order is None or lot is None or lot.finished_detail is None:
            raise WarehouseInventoryError("成品预占关联数据不完整", 409)
        detail = lot.finished_detail
        if detail.product_id != order_item.product_id:
            raise WarehouseInventoryError("成品库存与订单产品不一致", 409)
        if not detail.is_general and detail.owner_customer_id != order.customer_id:
            raise WarehouseInventoryError("其他客户专用成品库存不能用于当前订单", 409)
        if lot.version != expected_version:
            raise WarehouseInventoryError("库存已被其他人修改，请刷新后重试", 409)
        if lot.quantity_reserved < stock_quantity:
            raise WarehouseInventoryError("成品库存预占余额异常，请联系管理员", 409)
        before = _balances(lot)
        now = utc_now_naive()
        result = db.execute(
            update(InventoryLot)
            .where(
                InventoryLot.id == lot.id,
                InventoryLot.version == expected_version,
                InventoryLot.quantity_reserved >= stock_quantity,
            )
            .values(
                quantity_reserved=InventoryLot.quantity_reserved - stock_quantity,
                quantity_consumed=InventoryLot.quantity_consumed + stock_quantity,
                version=InventoryLot.version + 1,
                last_movement_at=now,
            )
        )
        if result.rowcount != 1:
            raise WarehouseInventoryError("成品库存数量或版本已变化，请重试", 409)
        reservation.consumed_stock_quantity += stock_quantity
        reservation.consumed_requirement_quantity += stock_quantity
        reservation.consumed_by = operator_id
        reservation.consumed_at = now
        reservation.status = _finished_reservation_status(reservation)
        db.flush()
        db.expire(lot)
        refreshed_lot = db.get(InventoryLot, lot.id)
        assert refreshed_lot is not None
        movement = _movement(
            db,
            lot=refreshed_lot,
            movement_type="consume",
            quantity=stock_quantity,
            before=before,
            operator_id=operator_id,
            reason="送货出库消耗成品预占",
            idempotency_key=idempotency_key,
            reservation_id=reservation.id,
            related_order_id=reservation.order_id,
            related_order_item_id=reservation.order_item_id,
            related_delivery_id=delivery_item.delivery_id,
        )
        db.flush()
        allocation = DeliveryInventoryAllocation(
            delivery_item_id=delivery_item.id,
            reservation_id=reservation.id,
            consume_movement_id=movement.id,
            consumed_stock_quantity=stock_quantity,
            credited_requirement_quantity=stock_quantity,
            reversed_stock_quantity=0,
            reversed_requirement_quantity=0,
            status="active",
            created_by=operator_id,
        )
        db.add(allocation)
        db.flush()
        from app.services.material_cost_lineage import (
            freeze_delivery_inventory_material_cost,
        )

        freeze_delivery_inventory_material_cost(
            db,
            allocation=allocation,
            lot=refreshed_lot,
            operator_id=operator_id,
        )
    return FinishedReservationMutation(reservation, movement, allocation)


def reverse_finished_consumption(
    db: Session,
    *,
    reservation_id: int,
    stock_quantity: int,
    expected_version: int,
    operator_id: int | None,
    idempotency_key: str,
    allocation_id: int,
) -> FinishedReservationMutation:
    repeated = _finished_idempotent_mutation(
        db,
        idempotency_key=idempotency_key,
        movement_type="reverse_consume",
        reservation_id=reservation_id,
    )
    if repeated is not None:
        return repeated
    if stock_quantity <= 0:
        raise WarehouseInventoryError("成品逆转数量必须大于0")
    location_id = db.scalar(
        select(InventoryLot.warehouse_location_id)
        .join(
            InventoryReservation,
            InventoryReservation.inventory_lot_id == InventoryLot.id,
        )
        .where(InventoryReservation.id == reservation_id)
    )
    if location_id is None:
        raise WarehouseInventoryError("成品库存预占关联库位不存在", 409)
    current_layout_version = db.scalar(
        select(Floor3LocationLayout.version).where(
            Floor3LocationLayout.location_id == int(location_id)
        )
    )
    _claim_inventory_destination(
        db,
        int(location_id),
        expected_layout_version=(
            int(current_layout_version) if current_layout_version is not None else None
        ),
    )
    with db.begin_nested():
        reservation = db.get(InventoryReservation, reservation_id)
        allocation = db.get(DeliveryInventoryAllocation, allocation_id)
        if reservation is None or reservation.reservation_type not in {
            "finished_order",
            "finished_surplus_delivery",
        }:
            raise WarehouseInventoryError("成品库存预占记录不存在", 404)
        if allocation is None or allocation.reservation_id != reservation.id:
            raise WarehouseInventoryError("送货成品库存分配记录不存在", 404)
        allocation_remaining = (
            int(allocation.consumed_stock_quantity)
            - int(allocation.reversed_stock_quantity or 0)
        )
        if stock_quantity > allocation_remaining:
            raise WarehouseInventoryError("逆转数量不能超过该送货成品分配余额", 409)
        if stock_quantity > int(reservation.consumed_stock_quantity or 0):
            raise WarehouseInventoryError("逆转数量不能超过累计成品消耗", 409)
        lot = db.get(InventoryLot, reservation.inventory_lot_id)
        if lot is None or lot.version != expected_version:
            raise WarehouseInventoryError("成品库存版本已变化，请重试", 409)
        if lot.quantity_consumed < stock_quantity:
            raise WarehouseInventoryError("成品库存累计消耗余额异常", 409)
        before = _balances(lot)
        now = utc_now_naive()
        result = db.execute(
            update(InventoryLot)
            .where(
                InventoryLot.id == lot.id,
                InventoryLot.version == expected_version,
                InventoryLot.quantity_consumed >= stock_quantity,
            )
            .values(
                quantity_reserved=InventoryLot.quantity_reserved + stock_quantity,
                quantity_consumed=InventoryLot.quantity_consumed - stock_quantity,
                version=InventoryLot.version + 1,
                last_movement_at=now,
            )
        )
        if result.rowcount != 1:
            raise WarehouseInventoryError("成品库存数量或版本已变化，请重试", 409)
        reservation.consumed_stock_quantity -= stock_quantity
        reservation.consumed_requirement_quantity -= stock_quantity
        if reservation.consumed_stock_quantity == 0:
            reservation.consumed_by = None
            reservation.consumed_at = None
        reservation.status = _finished_reservation_status(reservation)
        allocation.reversed_stock_quantity += stock_quantity
        allocation.reversed_requirement_quantity += stock_quantity
        allocation.reversed_by = operator_id
        allocation.reversed_at = now
        allocation.status = (
            "reversed"
            if allocation.reversed_stock_quantity == allocation.consumed_stock_quantity
            else "partial"
        )
        db.flush()
        db.expire(lot)
        refreshed_lot = db.get(InventoryLot, lot.id)
        assert refreshed_lot is not None
        movement = _movement(
            db,
            lot=refreshed_lot,
            movement_type="reverse_consume",
            quantity=stock_quantity,
            before=before,
            operator_id=operator_id,
            reason="撤销送货出库成品消耗",
            idempotency_key=idempotency_key,
            reservation_id=reservation.id,
            related_order_id=reservation.order_id,
            related_order_item_id=reservation.order_item_id,
            related_delivery_id=db.get(DeliveryItem, allocation.delivery_item_id).delivery_id,
            reversal_of_movement_id=allocation.consume_movement_id,
        )
        db.flush()
    return FinishedReservationMutation(reservation, movement, allocation)


def release_finished_reservation(
    db: Session,
    *,
    reservation_id: int,
    operator_id: int | None,
    release_reason: str | None,
    idempotency_key: str,
    allow_downstream: bool = False,
    allow_production_reversal: bool = False,
) -> InventoryReservation:
    repeated = db.scalar(
        select(InventoryMovement).where(
            InventoryMovement.idempotency_key == idempotency_key
        )
    )
    if repeated:
        if repeated.movement_type != "release_reserve":
            raise WarehouseInventoryError(
                "该请求标识已用于其他库存操作，请重新提交", 409
            )
        reservation = db.get(InventoryReservation, repeated.reservation_id)
        if reservation is None or reservation.id != reservation_id:
            raise WarehouseInventoryError("该请求标识已用于其他释放操作", 409)
        return reservation
    reservation = db.get(InventoryReservation, reservation_id)
    if reservation is None:
        raise WarehouseInventoryError("库存预占记录不存在", 404)
    if reservation.reservation_type != "finished_order":
        raise WarehouseInventoryError("该记录不是成品订单预占")
    lot = db.get(InventoryLot, reservation.inventory_lot_id)
    if lot is None:
        raise WarehouseInventoryError("关联库存批次不存在", 409)
    if lot.source_ref_type == "production_completion" and not allow_production_reversal:
        raise WarehouseInventoryError(
            "生产完工自动预占属于完工事实，当前不允许手工释放", 409
        )
    from app.services.production_workflow import has_production_completion_facts

    if not allow_production_reversal and reservation.order_item_id is not None and has_production_completion_facts(
        db, [reservation.order_item_id]
    ):
        raise WarehouseInventoryError(
            "该订单明细已有生产完工事实，不能释放成品预占", 409
        )
    remaining = (
        int(reservation.reserved_stock_quantity)
        - int(reservation.consumed_stock_quantity or 0)
        - int(reservation.released_stock_quantity or 0)
    )
    if remaining <= 0:
        raise WarehouseInventoryError("该预占已释放或已消耗，不能重复释放", 409)
    reason = (release_reason or "").strip() or "取消成品库存抵扣（系统记录）"
    item = db.get(OrderItem, reservation.order_item_id) if reservation.order_item_id else None
    if item is not None and not allow_downstream:
        if not _finished_reservation_pre_requisition(item):
            raise WarehouseInventoryError("请先取消报料，再取消成品库存抵扣", 409)
        if db.scalar(
            select(DeliveryItem.id)
            .where(DeliveryItem.order_item_id == item.id)
            .limit(1)
        ):
            raise WarehouseInventoryError("订单已进入送货流程，不能取消成品库存抵扣", 409)
    quantity = remaining
    if lot.quantity_reserved < quantity:
        raise WarehouseInventoryError("库存预占余额异常，请联系管理员处理", 409)
    before = _balances(lot)
    now = utc_now_naive()
    result = db.execute(
        update(InventoryLot)
        .where(
            InventoryLot.id == lot.id,
            InventoryLot.version == lot.version,
            InventoryLot.quantity_reserved >= quantity,
        )
        .values(
            quantity_available=InventoryLot.quantity_available + quantity,
            quantity_reserved=InventoryLot.quantity_reserved - quantity,
            version=InventoryLot.version + 1,
            last_movement_at=now,
        )
    )
    if result.rowcount != 1:
        raise WarehouseInventoryError("库存数量已变化，请刷新后重试", 409)
    reservation.released_stock_quantity += quantity
    reservation.released_requirement_quantity += quantity
    reservation.status = _finished_reservation_status(reservation)
    reservation.released_by = operator_id
    reservation.released_at = now
    reservation.release_reason = reason
    db.flush()
    db.expire(lot)
    lot = db.get(InventoryLot, lot.id)
    assert lot is not None
    _movement(
        db,
        lot=lot,
        movement_type="release_reserve",
        quantity=quantity,
        before=before,
        operator_id=operator_id,
        reason=reason,
        idempotency_key=idempotency_key,
        reservation_id=reservation.id,
        related_order_id=reservation.order_id,
        related_order_item_id=reservation.order_item_id,
    )
    db.flush()
    from app.services.production_workflow import refresh_existing_production_task

    refresh_existing_production_task(db, reservation.order_item_id)
    return reservation


def release_finished_surplus_delivery_reservation(
    db: Session,
    *,
    reservation_id: int,
    stock_quantity: int,
    expected_version: int,
    operator_id: int | None,
    idempotency_key: str,
) -> InventoryReservation:
    """Return a cancelled delivery-only surplus reservation to availability."""
    repeated = db.scalar(
        select(InventoryMovement).where(
            InventoryMovement.idempotency_key == idempotency_key
        )
    )
    if repeated is not None:
        if repeated.movement_type != "release_reserve":
            raise WarehouseInventoryError(
                "该请求标识已用于其他库存操作，请重新提交", 409
            )
        reservation = db.get(InventoryReservation, repeated.reservation_id)
        if reservation is None or reservation.id != reservation_id:
            raise WarehouseInventoryError("该请求标识已用于其他释放操作", 409)
        return reservation
    if stock_quantity <= 0:
        raise WarehouseInventoryError("余货预占释放数量必须大于0")
    reservation = db.get(InventoryReservation, reservation_id)
    if (
        reservation is None
        or reservation.reservation_type != "finished_surplus_delivery"
    ):
        raise WarehouseInventoryError("超量送货余货预占记录不存在", 404)
    remaining = (
        int(reservation.reserved_stock_quantity or 0)
        - int(reservation.consumed_stock_quantity or 0)
        - int(reservation.released_stock_quantity or 0)
    )
    if stock_quantity > remaining:
        raise WarehouseInventoryError("余货预占释放数量超过未使用余额", 409)
    lot = db.get(InventoryLot, reservation.inventory_lot_id)
    if lot is None or int(lot.version or 0) != expected_version:
        raise WarehouseInventoryError("余货库存版本已变化，请刷新后重试", 409)
    if int(lot.quantity_reserved or 0) < stock_quantity:
        raise WarehouseInventoryError("余货库存预占余额异常，请联系管理员处理", 409)
    before = _balances(lot)
    now = utc_now_naive()
    updated = db.execute(
        update(InventoryLot)
        .where(
            InventoryLot.id == lot.id,
            InventoryLot.version == expected_version,
            InventoryLot.quantity_reserved >= stock_quantity,
        )
        .values(
            quantity_available=InventoryLot.quantity_available + stock_quantity,
            quantity_reserved=InventoryLot.quantity_reserved - stock_quantity,
            version=InventoryLot.version + 1,
            last_movement_at=now,
        )
    )
    if updated.rowcount != 1:
        raise WarehouseInventoryError("余货库存数量已变化，请刷新后重试", 409)
    reservation.released_stock_quantity += stock_quantity
    reservation.released_requirement_quantity += stock_quantity
    reservation.released_by = operator_id
    reservation.released_at = now
    reservation.release_reason = "撤销送货，释放本次超量送货余货"
    reservation.status = _finished_reservation_status(reservation)
    db.flush()
    db.expire(lot)
    refreshed = db.get(InventoryLot, lot.id)
    assert refreshed is not None
    _movement(
        db,
        lot=refreshed,
        movement_type="release_reserve",
        quantity=stock_quantity,
        before=before,
        operator_id=operator_id,
        reason=reservation.release_reason,
        idempotency_key=idempotency_key,
        reservation_id=reservation.id,
        related_order_id=reservation.order_id,
        related_order_item_id=reservation.order_item_id,
    )
    db.flush()
    return reservation


def release_active_finished_reservations_for_items(
    db: Session,
    *,
    order_item_ids: list[int],
    operator_id: int | None,
    reason: str,
    idempotency_prefix: str,
    allow_downstream: bool = False,
) -> list[InventoryReservation]:
    if not order_item_ids:
        return []
    rows = db.scalars(
        select(InventoryReservation)
        .where(
            InventoryReservation.order_item_id.in_(order_item_ids),
            InventoryReservation.reservation_type == "finished_order",
            InventoryReservation.status != "cancelled",
            InventoryReservation.reserved_stock_quantity
            > InventoryReservation.consumed_stock_quantity
            + InventoryReservation.released_stock_quantity,
        )
        .order_by(InventoryReservation.id)
    ).all()
    return [
        release_finished_reservation(
            db,
            reservation_id=row.id,
            operator_id=operator_id,
            release_reason=reason,
            idempotency_key=f"{idempotency_prefix}-{row.id}",
            allow_downstream=allow_downstream,
        )
        for row in rows
    ]


def manual_semi_finished_in(
    db: Session,
    *,
    location_id: int,
    quantity: int,
    stock_date: date,
    source_type: str,
    material_code: str,
    layer_count: int,
    flute_type: str,
    board_length_mm: int,
    board_width_mm: int,
    sheet_type: str,
    component_type: str = "whole",
    pieces_per_box: int = 1,
    stock_yield_per_sheet: int = 1,
    supplier_name: str | None,
    customer_id: int | None,
    crease_type: str | None,
    crease_left_mm: int | None,
    crease_middle_mm: int | None,
    crease_right_mm: int | None,
    cutting_note: str | None,
    remarks: str | None,
    operator_id: int | None,
    idempotency_key: str | None,
    source_ref_type: str | None = None,
    source_ref_id: int | None = None,
    material_id: int | None = None,
    movement_reason: str = "手工半成品入库",
    stock_date_accuracy: str = "exact",
    stock_date_original_text: str | None = None,
    allow_raw_material_staging: bool = False,
    expected_layout_version: int | None = None,
    customer_generic_eligible: bool = False,
    internal_name: str | None = None,
    capacity_source_location_id: int | None = None,
    material_is_unknown: bool = False,
) -> InventoryLot:
    existing = _idempotent_lot(db, idempotency_key)
    if existing:
        return existing
    if quantity <= 0 or board_length_mm <= 0 or board_width_mm <= 0:
        raise WarehouseInventoryError("数量和纸板长宽必须大于0")
    component = component_type.strip().lower()
    if component not in {"whole", "cover", "base"}:
        raise WarehouseInventoryError("半成品组件仅允许整片、天地盖盖片或底片")
    if pieces_per_box <= 0 or stock_yield_per_sheet <= 0:
        raise WarehouseInventoryError("每箱片数和每库存张产出片数必须大于0")
    _claim_inventory_destination(
        db,
        location_id,
        expected_layout_version=expected_layout_version,
    )
    material = db.get(Material, material_id) if material_id else None
    if material_id and (material is None or not material.is_active):
        raise WarehouseInventoryError("材质主数据不存在或已停用", 404)
    if material is not None:
        material_code = material.code
        if material.layer_count is not None:
            layer_count = material.layer_count
    if material_is_unknown and (material_id is not None or material_code != "未知"):
        raise WarehouseInventoryError("未知材质不得冒用供应商材质代码")
    code_error = None if material_is_unknown else seven_layer_code_error(material_code, layer_count)
    if code_error:
        raise WarehouseInventoryError(code_error)
    flute = flute_type.strip().upper()
    if (
        layer_count not in SEMI_FINISHED_FLUTES_BY_LAYER
        or flute not in SEMI_FINISHED_FLUTES_BY_LAYER[layer_count]
    ):
        raise WarehouseInventoryError(
            "三层仅支持A/B/E楞，五层仅支持AB/BE楞，七层仅支持AAA/ABC楞"
        )
    if sheet_type not in {"raw_board", "net_sheet", "creased_sheet"}:
        raise WarehouseInventoryError("片料类型无效")
    _location(
        db,
        location_id,
        "semi_finished",
        capacity_source_location_id=capacity_source_location_id,
        allow_raw_material_staging=allow_raw_material_staging,
        raw_material_staging_source_type=source_type,
        raw_material_staging_source_ref_type=source_ref_type,
        raw_material_staging_source_ref_id=source_ref_id,
    )
    customer = db.get(Customer, customer_id) if customer_id else None
    if customer_id and customer is None:
        raise WarehouseInventoryError("客户不存在", 404)
    normalized_internal_name = (internal_name or "").strip() or None
    if normalized_internal_name and len(normalized_internal_name) > 200:
        raise WarehouseInventoryError("库存内部名称不能超过200个字符")
    if customer_generic_eligible and customer is None:
        raise WarehouseInventoryError("客户通用半成品库存必须指定客户", 409)
    date_accuracy, date_original_text = normalize_stock_date_metadata(
        stock_date=stock_date,
        stock_date_accuracy=stock_date_accuracy,
        stock_date_original_text=stock_date_original_text,
    )
    now = utc_now_naive()
    lot = InventoryLot(
        lot_number=_number("SI"),
        inventory_type="semi_finished",
        warehouse_location_id=location_id,
        quantity_available=quantity,
        unit="sheets",
        status="active",
        source_type=source_type,
        source_ref_type=source_ref_type,
        source_ref_id=source_ref_id,
        stock_date=stock_date,
        stock_date_accuracy=date_accuracy,
        stock_date_original_text=date_original_text,
        last_movement_at=now,
        remarks=remarks,
        created_by=operator_id,
    )
    apply_cost_snapshot(
        lot,
        estimate_semi_finished_cost(
            db,
            material_id=material.id if material else None,
            material_code=material_code,
            supplier_name=supplier_name,
            layer_count=layer_count,
            flute_type=flute,
            board_length_mm=board_length_mm,
            board_width_mm=board_width_mm,
        ),
        captured_at=now,
    )
    db.add(lot)
    db.flush()
    lot.semi_finished_detail = SemiFinishedInventoryDetail(
        supplier_name=(supplier_name or "").strip() or None,
        owner_customer_id=customer.id if customer else None,
        owner_customer_name_snapshot=customer.name if customer else None,
        customer_generic_eligible=bool(customer_generic_eligible),
        internal_name=normalized_internal_name,
        material_id=material.id if material else None,
        material_code_snapshot=material_code.strip(),
        normalized_material_code=normalize_material_code(material_code),
        layer_count=layer_count,
        flute_type=flute,
        board_length_mm=board_length_mm,
        board_width_mm=board_width_mm,
        component_type=component,
        pieces_per_box=pieces_per_box,
        stock_yield_per_sheet=stock_yield_per_sheet,
        sheet_type=sheet_type,
        crease_type=crease_type,
        crease_left_mm=crease_left_mm,
        crease_middle_mm=crease_middle_mm,
        crease_right_mm=crease_right_mm,
        cutting_note=cutting_note,
    )
    _movement(
        db,
        lot=lot,
        movement_type="manual_in",
        quantity=quantity,
        before={key: 0 for key in _balances(lot)},
        operator_id=operator_id,
        reason=movement_reason,
        remarks=remarks,
        idempotency_key=idempotency_key,
    )
    db.flush()
    return lot


SEMI_FINISHED_ASSIGN_CUSTOMER_REASON = "指定半成品归属客户"
SEMI_FINISHED_UNASSIGN_CUSTOMER_REASON = "取消半成品归属客户"
SEMI_FINISHED_VOID_REASON = "作废误录半成品库存批次"


def _editable_semi_finished_lot(
    db: Session, lot_id: int, expected_version: int
) -> InventoryLot:
    lot = db.get(InventoryLot, lot_id)
    if lot is None or lot.inventory_type != "semi_finished" or lot.semi_finished_detail is None:
        raise WarehouseInventoryError("半成品库存批次不存在", 404)
    if lot.version != expected_version:
        raise WarehouseInventoryError("库存批次已被其他操作更新，请刷新后重试", 409)
    if lot.status == "closed":
        raise WarehouseInventoryError("已关闭的半成品库存批次不能编辑", 409)
    return lot


def semi_finished_lot_allowed_product_ids(
    db: Session, inventory_lot_id: int
) -> tuple[int, ...]:
    lot = db.get(InventoryLot, inventory_lot_id)
    if lot is None or lot.inventory_type != "semi_finished":
        raise WarehouseInventoryError("半成品库存批次不存在", 404)
    return tuple(
        db.scalars(
            select(SemiFinishedLotAllowedProduct.product_id)
            .where(SemiFinishedLotAllowedProduct.inventory_lot_id == lot.id)
            .order_by(SemiFinishedLotAllowedProduct.product_id)
        ).all()
    )


def _lot_product_binding_has_blocking_usage(
    db: Session,
    *,
    lot_id: int,
    product_id: int | None = None,
) -> bool:
    filters = [InventoryReservation.inventory_lot_id == lot_id]
    if product_id is not None:
        filters.append(InventoryReservation.order_item_id == OrderItem.id)
        filters.append(OrderItem.product_id == product_id)
    return db.scalar(
        select(InventoryReservation.id)
        .select_from(InventoryReservation)
        .join(
            OrderItem,
            OrderItem.id == InventoryReservation.order_item_id,
            isouter=product_id is None,
        )
        .where(
            *filters,
            or_(
                InventoryReservation.reserved_stock_quantity
                > InventoryReservation.consumed_stock_quantity
                + InventoryReservation.released_stock_quantity,
                InventoryReservation.consumed_stock_quantity > 0,
            ),
        )
        .limit(1)
    ) is not None


def replace_semi_finished_lot_allowed_products(
    db: Session,
    *,
    inventory_lot_id: int,
    product_ids: list[int],
    expected_version: int,
    operator_id: int | None,
) -> InventoryLot:
    """Replace one lot's hard bindings without changing shared match memory."""

    lot = _editable_semi_finished_lot(db, inventory_lot_id, expected_version)
    detail = lot.semi_finished_detail
    assert detail is not None
    if detail.owner_customer_id is None:
        raise WarehouseInventoryError("请先为半成品库存指定归属客户", 409)

    desired = set(dict.fromkeys(int(value) for value in product_ids))
    products = (
        db.scalars(
            select(Product).where(
                Product.id.in_(desired),
                Product.customer_id == detail.owner_customer_id,
                Product.is_active.is_(True),
                Product.deleted_at.is_(None),
            )
        ).all()
        if desired
        else []
    )
    if len(products) != len(desired):
        raise WarehouseInventoryError(
            "半成品库存只能绑定同一客户的启用成品款号", 409
        )

    current = set(
        db.scalars(
            select(SemiFinishedLotAllowedProduct.product_id).where(
                SemiFinishedLotAllowedProduct.inventory_lot_id == lot.id
            )
        ).all()
    )
    removed = current - desired
    for product_id in removed:
        if _lot_product_binding_has_blocking_usage(
            db, lot_id=lot.id, product_id=product_id
        ):
            raise WarehouseInventoryError(
                "该款号已有活跃预占或净消耗，不能移除批次硬绑定", 409
            )
    if current == desired:
        return lot

    now = utc_now_naive()
    updated = db.execute(
        update(InventoryLot)
        .where(
            InventoryLot.id == lot.id,
            InventoryLot.inventory_type == "semi_finished",
            InventoryLot.version == expected_version,
        )
        .values(version=expected_version + 1, last_movement_at=now)
        .execution_options(synchronize_session=False)
    )
    if updated.rowcount != 1:
        raise WarehouseInventoryError("库存批次已被其他操作更新，请刷新后重试", 409)
    if removed:
        db.execute(
            delete(SemiFinishedLotAllowedProduct).where(
                SemiFinishedLotAllowedProduct.inventory_lot_id == lot.id,
                SemiFinishedLotAllowedProduct.product_id.in_(removed),
            )
        )
    for product_id in desired - current:
        db.add(
            SemiFinishedLotAllowedProduct(
                inventory_lot_id=lot.id,
                product_id=product_id,
                confirmed_by=operator_id,
                confirmed_at=now,
            )
        )
    db.flush()
    db.expire_all()
    refreshed = db.get(InventoryLot, lot.id)
    assert refreshed is not None
    return refreshed


def edit_semi_finished_lot_customer(
    db: Session, *, lot_id: int, customer_id: int | None,
    expected_version: int, operator_id: int | None,
) -> InventoryLot:
    lot = _editable_semi_finished_lot(db, lot_id, expected_version)
    detail = lot.semi_finished_detail
    assert detail is not None
    current_customer_id = detail.owner_customer_id
    if current_customer_id is None:
        if customer_id is None:
            raise WarehouseInventoryError("当前批次尚未指定客户，无需取消归属", 409)
        if (
            lot.quantity_reserved
            or lot.quantity_consumed
            or _lot_product_binding_has_blocking_usage(db, lot_id=lot.id)
        ):
            raise WarehouseInventoryError(
                "批次已有预占或消耗记录，不能指定客户归属", 409
            )
        customer = db.get(Customer, customer_id)
        if customer is None or not customer.is_active or customer.status != "active":
            raise WarehouseInventoryError("客户不存在或已停用", 404)
        next_customer_id = customer.id
        next_customer_name = customer.name
        reason = SEMI_FINISHED_ASSIGN_CUSTOMER_REASON
    else:
        if customer_id is not None:
            if customer_id == current_customer_id:
                raise WarehouseInventoryError("半成品库存批次归属客户未变化", 409)
            raise WarehouseInventoryError("如需更换客户，请先取消当前归属，再重新指定客户", 409)
        if (
            lot.quantity_reserved
            or lot.quantity_consumed
            or _lot_product_binding_has_blocking_usage(db, lot_id=lot.id)
        ):
            raise WarehouseInventoryError(
                "批次已有预占或消耗记录，不能取消客户归属", 409
            )
        next_customer_id = None
        next_customer_name = None
        reason = SEMI_FINISHED_UNASSIGN_CUSTOMER_REASON

    before = _balances(lot)
    now = utc_now_naive()
    updated_lot = db.execute(update(InventoryLot).where(
        InventoryLot.id == lot_id,
        InventoryLot.inventory_type == "semi_finished",
        InventoryLot.version == expected_version,
    ).values(version=expected_version + 1, last_movement_at=now).execution_options(
        synchronize_session=False
    ))
    if updated_lot.rowcount != 1:
        raise WarehouseInventoryError("库存批次已被其他操作更新，请刷新后重试", 409)
    detail.owner_customer_id = next_customer_id
    detail.owner_customer_name_snapshot = next_customer_name
    if next_customer_id is None:
        db.execute(
            delete(SemiFinishedLotAllowedProduct).where(
                SemiFinishedLotAllowedProduct.inventory_lot_id == lot.id
            )
        )
    db.flush()
    db.expire_all()
    refreshed = db.get(InventoryLot, lot_id)
    assert refreshed is not None
    _movement(
        db, lot=refreshed, movement_type="adjust", quantity=0, before=before,
        operator_id=operator_id, reason=reason, remarks=json.dumps({
            "before_customer_id": current_customer_id,
            "after_customer_id": next_customer_id,
        }, ensure_ascii=False),
    )
    db.flush()
    return refreshed


def void_semi_finished_lot(
    db: Session, *, lot_id: int, expected_version: int,
    reason: str | None, operator_id: int | None,
) -> InventoryLot:
    normalized_reason = (reason or "").strip() or "删除误录半成品批次（系统记录）"
    lot = _editable_semi_finished_lot(db, lot_id, expected_version)
    if any((lot.quantity_reserved, lot.quantity_consumed, lot.quantity_damaged, lot.quantity_scrapped)):
        raise WarehouseInventoryError("批次已有预占、消耗、报损或报废，不能删除", 409)
    if lot.source_ref_type is not None or lot.source_ref_id is not None:
        raise WarehouseInventoryError("批次关联来料或补库业务，不能删除", 409)
    reservation_id = db.scalar(select(InventoryReservation.id).where(
        InventoryReservation.inventory_lot_id == lot.id
    ).limit(1))
    if reservation_id is not None:
        raise WarehouseInventoryError("批次已有订单或报料预占记录，不能删除", 409)
    before = _balances(lot)
    now = utc_now_naive()
    audit_note = f"作废原因：{normalized_reason}"
    lot_remarks = "\n".join(part for part in ((lot.remarks or "").strip(), audit_note) if part)
    updated = db.execute(update(InventoryLot).where(
        InventoryLot.id == lot.id,
        InventoryLot.inventory_type == "semi_finished",
        InventoryLot.version == expected_version,
        InventoryLot.status.in_(("active", "frozen")),
    ).values(
        quantity_available=0, status="closed", version=expected_version + 1,
        last_movement_at=now, remarks=lot_remarks,
    ).execution_options(synchronize_session=False))
    if updated.rowcount != 1:
        raise WarehouseInventoryError("库存批次已被其他操作更新，请刷新后重试", 409)

    db.expire_all()
    refreshed = db.get(InventoryLot, lot.id)
    assert refreshed is not None
    _movement(
        db, lot=refreshed, movement_type="adjust", quantity=before["available"],
        before=before, operator_id=operator_id,
        reason=f"{SEMI_FINISHED_VOID_REASON}：{normalized_reason}",
        remarks="保留原始入库流水并关闭批次",
    )
    db.flush()
    return refreshed


FINISHED_LOT_EDIT_REASON = "编辑成品库存批次"


def _finished_lot_edit_request(
    *,
    expected_version: int,
    is_general: bool,
    customer_id: int | None,
    product_id: int,
    quantity_available: int,
    location_id: int,
    expected_layout_version: int | None = None,
    stock_date: date,
    confirm_stock_date_exact: bool,
) -> dict[str, object]:
    return {
        "expected_version": expected_version,
        "is_general": is_general,
        "customer_id": customer_id,
        "product_id": product_id,
        "quantity_available": quantity_available,
        "location_id": location_id,
        "expected_layout_version": expected_layout_version,
        "stock_date": stock_date.isoformat(),
        "confirm_stock_date_exact": confirm_stock_date_exact,
    }


def _idempotent_finished_lot_edit(
    db: Session,
    *,
    lot_id: int,
    idempotency_key: str,
    request_payload: dict[str, object],
) -> InventoryLot | None:
    movement = db.scalar(
        select(InventoryMovement).where(
            InventoryMovement.idempotency_key == idempotency_key
        )
    )
    if movement is None:
        return None
    try:
        audit = json.loads(movement.remarks or "")
    except (TypeError, json.JSONDecodeError):
        audit = None
    if (
        movement.inventory_lot_id != lot_id
        or movement.movement_type != "adjust"
        or movement.reason != FINISHED_LOT_EDIT_REASON
        or not isinstance(audit, dict)
        or audit.get("action") != FINISHED_LOT_EDIT_REASON
        or audit.get("request") != request_payload
    ):
        raise WarehouseInventoryError("幂等键已用于不同的库存业务", 409)
    lot = db.get(InventoryLot, lot_id)
    if lot is None:
        raise WarehouseInventoryError("库存批次不存在", 404)
    return lot


def _finished_product_snapshot(
    db: Session,
    *,
    product_id: int,
    is_general: bool,
    customer_id: int | None,
) -> tuple[Product, Customer, str | None]:
    product = db.get(Product, product_id)
    if product is None or not product.is_active or product.deleted_at is not None:
        raise WarehouseInventoryError("所选产品不存在或已停用", 409)
    if not is_general:
        if customer_id is None:
            raise WarehouseInventoryError("客户专用库存必须选择客户")
        if product.customer_id != customer_id:
            raise WarehouseInventoryError("所选产品不属于当前客户", 409)
    customer = db.get(Customer, product.customer_id)
    if customer is None:
        raise WarehouseInventoryError("所选产品缺少有效客户", 409)
    material_code = (
        product.material.code
        if product.material is not None
        else product.default_material_code
    ) or product.legacy_material_text
    return product, customer, material_code


def _edit_change(
    changes: dict[str, dict[str, object]],
    field: str,
    before: object,
    after: object,
) -> None:
    if before != after:
        changes[field] = {"before": before, "after": after}


def edit_finished_lot(
    db: Session,
    *,
    lot_id: int,
    expected_version: int,
    is_general: bool,
    customer_id: int | None,
    product_id: int,
    quantity_available: int,
    location_id: int,
    expected_layout_version: int | None = None,
    stock_date: date,
    operator_id: int | None,
    idempotency_key: str,
    confirm_stock_date_exact: bool = False,
) -> InventoryLot:
    """Edit one formal finished-goods lot and its physical projection atomically."""
    idempotency_key = idempotency_key.strip()
    if not idempotency_key:
        raise WarehouseInventoryError("幂等键不能为空")
    if quantity_available < 0:
        raise WarehouseInventoryError("可用库存不能小于0")
    request_payload = _finished_lot_edit_request(
        expected_version=expected_version,
        is_general=is_general,
        customer_id=customer_id,
        product_id=product_id,
        quantity_available=quantity_available,
        location_id=location_id,
        expected_layout_version=expected_layout_version,
        stock_date=stock_date,
        confirm_stock_date_exact=confirm_stock_date_exact,
    )
    existing = _idempotent_finished_lot_edit(
        db,
        lot_id=lot_id,
        idempotency_key=idempotency_key,
        request_payload=request_payload,
    )
    if existing is not None:
        return existing

    _claim_inventory_destination(
        db,
        location_id,
        expected_layout_version=expected_layout_version,
    )

    lot = db.get(InventoryLot, lot_id)
    if (
        lot is None
        or lot.inventory_type != "finished"
        or lot.finished_detail is None
    ):
        raise WarehouseInventoryError("成品库存批次不存在", 404)
    if lot.version != expected_version:
        raise WarehouseInventoryError(
            "库存已被其他人修改，请刷新后重试", 409
        )
    if lot.status != "active":
        raise WarehouseInventoryError("只有正常状态的成品库存可以编辑", 409)

    target_location = _location(db, location_id, "finished")
    product, product_customer, material_code = _finished_product_snapshot(
        db,
        product_id=product_id,
        is_general=is_general,
        customer_id=customer_id,
    )
    detail = lot.finished_detail
    owner_customer_id = None if is_general else product_customer.id
    owner_customer_name = product_customer.name
    identity_changed = (
        detail.product_id != product.id
        or detail.owner_customer_id != owner_customer_id
        or detail.is_general != is_general
    )
    if identity_changed and (
        lot.quantity_reserved > 0 or lot.quantity_consumed > 0
    ):
        raise WarehouseInventoryError(
            "批次已有预占或消耗，不能修改产品、客户或通用归属", 409
        )
    target_snapshots: dict[str, object] = {
        "inventory_code_snapshot": product.product_code,
        "product_name_snapshot": product.product_name,
        "box_type_snapshot": product.box_style,
        "length_mm": round(product.length_mm) if product.length_mm is not None else None,
        "width_mm": round(product.width_mm) if product.width_mm is not None else None,
        "height_mm": round(product.height_mm) if product.height_mm is not None else None,
        "material_code_snapshot": material_code,
        "flute_type_snapshot": product.flute_type,
    }
    physical_changed = any(getattr(detail, field) != target_snapshots[field] for field in (
        "box_type_snapshot", "length_mm", "width_mm", "height_mm",
        "material_code_snapshot", "flute_type_snapshot"))
    if physical_changed and (lot.quantity_reserved > 0 or lot.quantity_consumed > 0):
        # Editing quantity/location must not silently replace physical facts
        # from a subsequently changed product master.
        for field in ("box_type_snapshot", "length_mm", "width_mm", "height_mm",
                      "material_code_snapshot", "flute_type_snapshot"):
            target_snapshots[field] = getattr(detail, field)
    elif physical_changed or identity_changed:
        target_snapshots["physical_basis_json"] = None

    changes: dict[str, dict[str, object]] = {}
    _edit_change(changes, "is_general", detail.is_general, is_general)
    _edit_change(
        changes,
        "owner_customer_id",
        detail.owner_customer_id,
        owner_customer_id,
    )
    _edit_change(
        changes,
        "owner_customer_name_snapshot",
        detail.owner_customer_name_snapshot,
        owner_customer_name,
    )
    _edit_change(changes, "product_id", detail.product_id, product.id)
    for field, value in target_snapshots.items():
        _edit_change(changes, field, getattr(detail, field), value)
    _edit_change(
        changes,
        "quantity_available",
        lot.quantity_available,
        quantity_available,
    )
    _edit_change(
        changes,
        "warehouse_location_id",
        lot.warehouse_location_id,
        target_location.id,
    )
    _edit_change(
        changes,
        "stock_date",
        lot.stock_date.isoformat(),
        stock_date.isoformat(),
    )
    stock_date_changed = lot.stock_date != stock_date
    stock_date_confirmed = stock_date_changed or confirm_stock_date_exact
    if stock_date_confirmed:
        _edit_change(
            changes,
            "stock_date_accuracy",
            lot.stock_date_accuracy,
            "exact",
        )
        _edit_change(
            changes,
            "stock_date_original_text",
            lot.stock_date_original_text,
            stock_date.isoformat(),
        )

    before = _balances(lot)
    location_changed = lot.warehouse_location_id != target_location.id
    pallet_item = lot.pallet_item
    pallet = pallet_item.pallet if pallet_item is not None else None
    pallet_moved = False
    if pallet_item is not None:
        if pallet is None or not pallet.is_current or pallet.location_id is None:
            raise WarehouseInventoryError("成品库存绑定的真实栈板状态无效", 409)
        if pallet.location_id != lot.warehouse_location_id:
            raise WarehouseInventoryError("成品库存与真实栈板货位不一致", 409)

    if location_changed and target_location.source_version == "V11":
        occupied = db.scalar(
            select(InventoryPallet).where(
                InventoryPallet.location_id == target_location.id,
                InventoryPallet.is_current.is_(True),
            )
        )
        if occupied is not None and (pallet is None or occupied.id != pallet.id):
            raise WarehouseInventoryError("目标三楼货位已被其它真实栈板占用", 409)

    now = utc_now_naive()
    stock_date_values = (
        {
            "stock_date_accuracy": "exact",
            "stock_date_original_text": stock_date.isoformat(),
        }
        if stock_date_confirmed
        else {}
    )
    if location_changed and pallet is not None:
        active_ground_occupancy = db.scalar(
            select(WarehouseGroundOccupancy.id)
            .where(
                WarehouseGroundOccupancy.pallet_id == int(pallet.id),
                WarehouseGroundOccupancy.status == "active",
            )
            .limit(1)
        )
        if active_ground_occupancy is not None:
            raise WarehouseInventoryError(
                "地堆栈板不能通过批次编辑改库位，请使用仓库地图转位流程",
                409,
            )
        from app.services.floor3_locations import Floor3LocationError, move_pallet

        move_key = f"finished-edit:{sha256(idempotency_key.encode('utf-8')).hexdigest()}"
        try:
            move_pallet(
                db,
                pallet_id=pallet.id,
                expected_version=pallet.version,
                to_location_id=target_location.id,
                remarks=FINISHED_LOT_EDIT_REASON,
                operator_id=operator_id,
                idempotency_key=move_key,
                expected_target_layout_version=expected_layout_version,
            )
        except Floor3LocationError as error:
            raise WarehouseInventoryError(str(error), error.status_code) from error
        pallet_moved = True
        result = db.execute(
            update(InventoryLot)
            .where(
                InventoryLot.id == lot.id,
                InventoryLot.version == expected_version + 1,
                InventoryLot.warehouse_location_id == target_location.id,
            )
            .values(
                quantity_available=quantity_available,
                stock_date=stock_date,
                last_movement_at=now,
                **stock_date_values,
            )
        )
    else:
        result = db.execute(
            update(InventoryLot)
            .where(
                InventoryLot.id == lot.id,
                InventoryLot.version == expected_version,
            )
            .values(
                quantity_available=quantity_available,
                warehouse_location_id=target_location.id,
                stock_date=stock_date,
                version=expected_version + 1,
                last_movement_at=now,
                **stock_date_values,
            )
        )
    if result.rowcount != 1:
        raise WarehouseInventoryError(
            "库存已被其他人修改，请刷新后重试", 409
        )

    detail.owner_customer_id = owner_customer_id
    detail.owner_customer_name_snapshot = owner_customer_name
    detail.is_general = is_general
    detail.product_id = product.id
    for field, value in target_snapshots.items():
        setattr(detail, field, value)

    if pallet_item is not None:
        projection_values = {
            "customer_id": product_customer.id,
            "product_id": product.id,
            "inventory_code": product.product_code,
            "customer_name_snapshot": product_customer.name,
            "product_name": product.product_name,
            "quantity": max(quantity_available + lot.quantity_reserved, 1),
            "match_status": "matched",
        }
        projection_changed = any(
            getattr(pallet_item, field) != value
            for field, value in projection_values.items()
        )
        for field, value in projection_values.items():
            setattr(pallet_item, field, value)
        if projection_changed and not pallet_moved and pallet is not None:
            pallet_result = db.execute(
                update(InventoryPallet)
                .where(
                    InventoryPallet.id == pallet.id,
                    InventoryPallet.version == pallet.version,
                )
                .values(version=pallet.version + 1, updated_by=operator_id)
            )
            if pallet_result.rowcount != 1:
                raise WarehouseInventoryError(
                    "真实栈板已被其他操作更新，请刷新后重试", 409
                )

    db.flush()
    _ensure_finished_projection_postcondition(
        db,
        lot=lot,
        operator_id=operator_id,
        create_missing=True,
    )

    db.refresh(lot)
    audit = {
        "action": FINISHED_LOT_EDIT_REASON,
        "request": request_payload,
        "changed_fields": list(changes),
        "changes": changes,
    }
    _movement(
        db,
        lot=lot,
        movement_type="adjust",
        quantity=quantity_available - before["available"],
        before=before,
        operator_id=operator_id,
        reason=FINISHED_LOT_EDIT_REASON,
        remarks=json.dumps(audit, ensure_ascii=False, sort_keys=True),
        idempotency_key=idempotency_key,
    )
    db.flush()
    return lot


def mutate_lot(
    db: Session,
    *,
    lot_id: int,
    operation: str,
    expected_version: int,
    operator_id: int | None,
    quantity: int = 0,
    reason: str | None = None,
    idempotency_key: str | None = None,
) -> InventoryLot:
    existing = _idempotent_lot(db, idempotency_key)
    if existing:
        if existing.inventory_type == "finished":
            _ensure_finished_projection_postcondition(
                db,
                lot=existing,
                operator_id=operator_id,
                create_missing=False,
            )
        return existing
    if operation == "adjust" and quantity > 0:
        location_id = db.scalar(
            select(InventoryLot.warehouse_location_id).where(
                InventoryLot.id == lot_id
            )
        )
        if location_id is None:
            raise WarehouseInventoryError("库存批次或关联库位不存在", 404)
        _claim_inventory_destination(db, int(location_id))
    lot = db.get(InventoryLot, lot_id)
    if lot is None:
        raise WarehouseInventoryError("库存批次不存在", 404)
    if lot.version != expected_version:
        raise WarehouseInventoryError("库存已被其他人修改，请刷新后重试", 409)
    if lot.status == "closed":
        raise WarehouseInventoryError("已关闭库存不能操作", 409)
    if operation in {"adjust", "damage", "scrap", "transfer_to_general"} and lot.status != "active":
        raise WarehouseInventoryError("冻结库存不能执行数量或归属调整", 409)
    before = _balances(lot)
    values: dict[str, object] = {
        "version": lot.version + 1,
        "last_movement_at": utc_now_naive(),
    }
    movement_quantity = quantity
    if operation == "freeze":
        if lot.status != "active":
            raise WarehouseInventoryError("只有正常库存可以冻结", 409)
        values["status"] = "frozen"
        movement_quantity = 0
    elif operation == "unfreeze":
        if lot.status != "frozen":
            raise WarehouseInventoryError("只有冻结库存可以解冻", 409)
        values["status"] = "active"
        movement_quantity = 0
    elif operation == "adjust":
        if quantity == 0:
            raise WarehouseInventoryError("调整数量不能为0")
        if lot.quantity_available + quantity < 0:
            raise WarehouseInventoryError("调整后可用库存不能小于0")
        values["quantity_available"] = lot.quantity_available + quantity
    elif operation in {"damage", "scrap"}:
        if quantity <= 0:
            raise WarehouseInventoryError("数量必须大于0")
        if quantity > lot.quantity_available:
            raise WarehouseInventoryError("数量不能大于当前可用库存")
        values["quantity_available"] = lot.quantity_available - quantity
        values[f"quantity_{'damaged' if operation == 'damage' else 'scrapped'}"] = (
            lot.quantity_damaged + quantity
            if operation == "damage"
            else lot.quantity_scrapped + quantity
        )
    elif operation == "transfer_to_general":
        if lot.inventory_type != "finished" or lot.finished_detail is None:
            raise WarehouseInventoryError("只有成品库存可以转为通用库存")
        if lot.finished_detail.is_general:
            raise WarehouseInventoryError("该批次已经是通用库存", 409)
        movement_quantity = 0
    else:
        raise WarehouseInventoryError("不支持的库存操作")
    result = db.execute(
        update(InventoryLot)
        .where(InventoryLot.id == lot.id, InventoryLot.version == expected_version)
        .values(**values)
    )
    if result.rowcount != 1:
        raise WarehouseInventoryError("库存已被其他人修改，请刷新后重试", 409)
    if operation == "transfer_to_general":
        lot.finished_detail.is_general = True
        lot.finished_detail.owner_customer_id = None
    db.flush()
    db.expire(lot)
    lot = db.get(InventoryLot, lot.id)
    assert lot is not None
    if lot.inventory_type == "finished":
        _ensure_finished_projection_postcondition(
            db,
            lot=lot,
            operator_id=operator_id,
            create_missing=True,
        )
    _movement(
        db,
        lot=lot,
        movement_type=operation,
        quantity=movement_quantity,
        before=before,
        operator_id=operator_id,
        reason=(reason or "").strip() or None,
        idempotency_key=idempotency_key,
    )
    db.flush()
    return lot
