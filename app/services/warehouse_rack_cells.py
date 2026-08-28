from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
import string

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.core.time_contract import beijing_now_naive
from app.models.warehouse_inventory import (
    Floor3LocationLayout,
    InventoryLot,
    InventoryPallet,
    WarehouseArea,
    WarehouseAreaStoragePolicy,
    WarehouseFloor,
    WarehouseLocation,
)
from app.services.warehouse_area_activation import (
    AREA_LOCATION_SOURCE_VERSION,
    location_warehouse_type,
)


class WarehouseRackCellSyncError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 409):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class WarehouseRackCellSyncResult:
    created_location_ids: tuple[int, ...]
    enabled_location_ids: tuple[int, ...]
    disabled_location_ids: tuple[int, ...]
    updated_location_ids: tuple[int, ...]


def _occupied_location_ids(db: Session, location_ids: list[int]) -> set[int]:
    if not location_ids:
        return set()
    pallet_ids = set(
        db.scalars(
            select(InventoryPallet.location_id).where(
                InventoryPallet.location_id.in_(location_ids),
                InventoryPallet.is_current.is_(True),
            )
        ).all()
    )
    inventory_ids = set(
        db.scalars(
            select(InventoryLot.warehouse_location_id).where(
                InventoryLot.warehouse_location_id.in_(location_ids),
                InventoryLot.status.in_(("active", "frozen")),
                (
                    InventoryLot.quantity_available
                    + InventoryLot.quantity_reserved
                    + InventoryLot.quantity_damaged
                )
                > 0,
            )
        ).all()
    )
    return {
        int(location_id)
        for location_id in [*pallet_ids, *inventory_ids]
        if location_id is not None
    }


def _map_rack_letter(
    db: Session,
    *,
    area: WarehouseArea,
    map_rack_id: str,
) -> str:
    existing = db.scalar(
        select(WarehouseLocation.rack_code)
        .where(
            WarehouseLocation.address_area_id == area.id,
            WarehouseLocation.map_rack_id == map_rack_id,
            WarehouseLocation.rack_code.is_not(None),
        )
        .limit(1)
    )
    if existing:
        return str(existing).upper()
    used = {
        str(value).upper()
        for value in db.scalars(
            select(WarehouseLocation.rack_code)
            .where(
                WarehouseLocation.address_area_id == area.id,
                WarehouseLocation.rack_code.is_not(None),
            )
            .distinct()
        ).all()
        if value
    }
    for value in string.ascii_uppercase:
        if value not in used:
            return value
    raise WarehouseRackCellSyncError(
        f"{area.area_name} 已有 26 个正式货架身份，不能继续自动分配；请先整理空货架。"
    )


def _rack_geometry_percent(
    floor_layout: dict,
    rack: dict,
    *,
    slot_no: int,
    slot_count: int,
) -> dict[str, Decimal]:
    bounds = floor_layout.get("bounds_mm") or {}
    min_x = float(bounds.get("min_x") or 0)
    min_y = float(bounds.get("min_y") or 0)
    max_x = float(bounds.get("max_x") or 0)
    max_y = float(bounds.get("max_y") or 0)
    floor_width = max_x - min_x
    floor_depth = max_y - min_y
    if floor_width <= 0 or floor_depth <= 0:
        raise WarehouseRackCellSyncError("正式地图缺少有效毫米边界，不能同步货架层格。")
    rotation = int(round(float(rack.get("rotation_deg") or 0))) % 180
    width_mm = float(rack.get("width_mm") or 0)
    depth_mm = float(rack.get("depth_mm") or 0)
    if rotation == 90:
        width_mm, depth_mm = depth_mm, width_mm
    if width_mm <= 0 or depth_mm <= 0 or slot_count <= 0:
        raise WarehouseRackCellSyncError("货架长宽或逐层格数无效，不能同步正式层格。")
    rack_left = float(rack.get("x_mm") or 0) - width_mm / 2
    rack_top = float(rack.get("y_mm") or 0) - depth_mm / 2
    cell_width = width_mm / slot_count
    left = rack_left + (slot_no - 1) * cell_width
    values = {
        "left_pct": (left - min_x) / floor_width * 100,
        "top_pct": (rack_top - min_y) / floor_depth * 100,
        "width_pct": cell_width / floor_width * 100,
        "height_pct": depth_mm / floor_depth * 100,
    }
    if (
        values["left_pct"] < -0.001
        or values["top_pct"] < -0.001
        or values["left_pct"] + values["width_pct"] > 100.001
        or values["top_pct"] + values["height_pct"] > 100.001
    ):
        raise WarehouseRackCellSyncError(
            f"货架 {rack.get('name') or rack.get('rack_code') or rack.get('id')} 超出正式楼层边界。"
        )
    quant = Decimal("0.0001")
    return {
        key: Decimal(str(max(0.0, min(100.0, value)))).quantize(
            quant, rounding=ROUND_HALF_UP
        )
        for key, value in values.items()
    }


def _rack_area(
    db: Session,
    *,
    floor: WarehouseFloor,
    rack: dict,
    features: dict[str, dict],
) -> WarehouseArea | None:
    feature_id = str(rack.get("area_feature_id") or "").strip()
    feature = features.get(feature_id)
    if feature is None:
        return None
    area = db.scalar(
        select(WarehouseArea)
        .join(WarehouseAreaStoragePolicy)
        .options(
            selectinload(WarehouseArea.floor),
            selectinload(WarehouseArea.storage_policy),
        )
        .where(
            WarehouseArea.floor_id == floor.id,
            WarehouseAreaStoragePolicy.map_feature_id == feature_id,
            WarehouseAreaStoragePolicy.status == "published",
        )
    )
    if area is None or area.storage_policy is None:
        return None
    if area.storage_policy.storage_layout not in {"rack", "mixed"}:
        return None
    return area


def sync_published_rack_cells(
    db: Session,
    *,
    floor_layout: dict,
    operator_id: int,
) -> WarehouseRackCellSyncResult:
    """Project published map rack cells into stable warehouse locations.

    The function never moves inventory.  Empty obsolete cells are only disabled;
    occupied cells fail the enclosing map publish so the file and SQL transaction
    can be rolled back together.
    """

    floor_code = str(floor_layout.get("floor_code") or "").strip().upper()
    map_revision = str(floor_layout.get("revision") or "").strip()
    floor = db.scalar(
        select(WarehouseFloor).where(func.upper(WarehouseFloor.floor_code) == floor_code)
    )
    if floor is None:
        return WarehouseRackCellSyncResult((), (), (), ())
    features = {
        str(item.get("id")): item
        for item in floor_layout.get("features") or []
        if item.get("feature_kind") == "zone" and item.get("id")
    }
    racks = [
        item
        for item in floor_layout.get("racks") or []
        if item.get("id") and item.get("area_feature_id")
    ]
    created: list[int] = []
    enabled: list[int] = []
    disabled: list[int] = []
    updated: list[int] = []
    published_rack_ids: set[str] = set()
    next_sort = int(db.scalar(select(func.max(WarehouseLocation.sort_order))) or 0) + 1
    touched_area_ids: set[int] = set()
    precise_area_ids: set[int] = set()

    for rack in racks:
        map_rack_id = str(rack["id"]).strip()
        published_rack_ids.add(map_rack_id)
        area = _rack_area(db, floor=floor, rack=rack, features=features)
        if area is None:
            obsolete = list(
                db.scalars(
                    select(WarehouseLocation).where(
                        WarehouseLocation.map_rack_id == map_rack_id,
                        WarehouseLocation.warehouse_floor == floor.floor_number,
                        WarehouseLocation.is_active.is_(True),
                    )
                ).all()
            )
            occupied_obsolete = _occupied_location_ids(
                db, [row.id for row in obsolete]
            )
            if occupied_obsolete:
                raise WarehouseRackCellSyncError(
                    f"{rack.get('name') or rack.get('rack_code')} 已不在正式货架区，"
                    f"但仍有 {len(occupied_obsolete)} 个层格有货；请先移货。"
                )
            for row in obsolete:
                row.is_active = False
                row.updated_at = beijing_now_naive()
                disabled.append(row.id)
                if row.address_area_id:
                    touched_area_ids.add(row.address_area_id)
            # Legacy visual racks for molds, plates or unfinished planning
            # remain drawings until a published rack/mixed policy gives them
            # a formal stock meaning.
            continue
        touched_area_ids.add(area.id)
        policy = area.storage_policy
        assert policy is not None
        warehouse_type = location_warehouse_type(policy)
        if warehouse_type is None:
            # Mold, plate, raw-material and temporary racks retain their own
            # asset ledgers.  They must never become selectable carton stock
            # cells merely because their geometry is drawn on the same map.
            obsolete = list(
                db.scalars(
                    select(WarehouseLocation).where(
                        WarehouseLocation.map_rack_id == map_rack_id,
                        WarehouseLocation.warehouse_floor == floor.floor_number,
                        WarehouseLocation.is_active.is_(True),
                    )
                ).all()
            )
            occupied_obsolete = _occupied_location_ids(
                db, [row.id for row in obsolete]
            )
            if occupied_obsolete:
                raise WarehouseRackCellSyncError(
                    f"{area.area_name} {rack.get('name') or rack.get('rack_code')} 已改为非成品/半成品用途，"
                    f"但仍有 {len(occupied_obsolete)} 个层格有货；请先移货。"
                )
            for row in obsolete:
                row.is_active = False
                row.updated_at = beijing_now_naive()
                disabled.append(row.id)
            continue
        counts = rack.get("level_cell_counts")
        levels = int(rack.get("levels") or 0)
        if not isinstance(counts, list) or len(counts) != levels:
            raise WarehouseRackCellSyncError(
                f"货架 {rack.get('name') or rack.get('rack_code')} 的逐层格数未设置完整。"
            )
        normalized_counts = [int(value) for value in counts]
        if levels <= 0 or any(value < 0 or value > 50 for value in normalized_counts):
            raise WarehouseRackCellSyncError(
                f"货架 {rack.get('name') or rack.get('rack_code')} 的层格参数无效。"
            )
        desired = {
            (level_no, slot_no)
            for level_no, count in enumerate(normalized_counts, start=1)
            for slot_no in range(1, count + 1)
        }
        if area.id not in precise_area_ids:
            # Earlier area activation created planning-only rack anchors.  Once
            # the published map supplies real rack cells, keeping those empty
            # anchors active would expose duplicate destinations.  Never guess
            # how occupied anchors should map to the new rack: block instead.
            generic_rows = list(
                db.scalars(
                    select(WarehouseLocation).where(
                        WarehouseLocation.address_area_id == area.id,
                        WarehouseLocation.storage_type == "rack",
                        WarehouseLocation.map_rack_id.is_(None),
                        WarehouseLocation.source_version
                        == AREA_LOCATION_SOURCE_VERSION,
                        WarehouseLocation.is_active.is_(True),
                    )
                ).all()
            )
            occupied_generic = _occupied_location_ids(
                db, [row.id for row in generic_rows]
            )
            if occupied_generic:
                raise WarehouseRackCellSyncError(
                    f"{area.area_name} 有 {len(occupied_generic)} 个旧规划货位仍有货；"
                    "请先移货，再用正式货架层格替换。"
                )
            for row in generic_rows:
                row.is_active = False
                row.updated_at = beijing_now_naive()
                disabled.append(row.id)
            precise_area_ids.add(area.id)
        if not desired:
            # A zero-cell rack remains a visible planning object but must not
            # leave a selectable planning anchor or create a formal location.
            continue
        rack_letter = _map_rack_letter(db, area=area, map_rack_id=map_rack_id)
        rack_name = str(rack.get("name") or rack.get("rack_code") or "货架").strip()
        rows = list(
            db.scalars(
                select(WarehouseLocation)
                .options(selectinload(WarehouseLocation.floor3_layout))
                .where(
                    WarehouseLocation.map_rack_id == map_rack_id,
                    WarehouseLocation.warehouse_floor == floor.floor_number,
                )
                .order_by(WarehouseLocation.level_no, WarehouseLocation.slot_no)
            ).all()
        )
        rows_by_cell = {
            (int(row.level_no or 0), int(row.slot_no or 0)): row for row in rows
        }
        extras = [
            row
            for key, row in rows_by_cell.items()
            if key not in desired and row.is_active
        ]
        occupied_extras = _occupied_location_ids(db, [row.id for row in extras])
        if occupied_extras:
            raise WarehouseRackCellSyncError(
                f"{area.area_name} {rack_name} 有 {len(occupied_extras)} 个将被缩减的层格仍有货；请先移货。"
            )
        for row in extras:
            row.is_active = False
            row.updated_at = beijing_now_naive()
            disabled.append(row.id)

        for level_no, slot_no in sorted(desired):
            row = rows_by_cell.get((level_no, slot_no))
            geometry = _rack_geometry_percent(
                floor_layout,
                rack,
                slot_no=slot_no,
                slot_count=normalized_counts[level_no - 1],
            )
            location_code = (
                f"{floor_code}-{area.area_code}-{rack_letter}-"
                f"{level_no:02d}-{slot_no:02d}"
            )
            location_name = (
                f"{area.area_name}·{rack_name}·第{level_no}层·第{slot_no}格"
            )
            if row is None:
                row = WarehouseLocation(
                    location_code=location_code,
                    location_name=location_name,
                    warehouse_type=warehouse_type,
                    is_active=True,
                    warehouse_floor=floor.floor_number,
                    area_code=area.area_code,
                    storage_type="rack",
                    level_no=level_no,
                    sort_order=next_sort,
                    is_temporary=False,
                    source_version=AREA_LOCATION_SOURCE_VERSION,
                    address_kind="rack_slot",
                    address_area_id=area.id,
                    rack_code=rack_letter,
                    map_rack_id=map_rack_id,
                    rack_display_name=rack_name,
                    slot_no=slot_no,
                    address_version=1,
                    placement_status="placed",
                )
                row.floor3_layout = Floor3LocationLayout(
                    **geometry,
                    z_index=level_no,
                    version=1,
                    source_type="seeded",
                    layout_kind="physical_rack",
                    created_by=operator_id,
                    updated_by=operator_id,
                )
                db.add(row)
                db.flush()
                created.append(row.id)
                next_sort += 1
                continue
            changed = False
            address_changed = False
            if not row.is_active:
                row.is_active = True
                enabled.append(row.id)
                changed = True
            for key, value in {
                "location_code": location_code,
                "location_name": location_name,
                "warehouse_type": warehouse_type,
                "warehouse_floor": floor.floor_number,
                "area_code": area.area_code,
                "storage_type": "rack",
                "source_version": AREA_LOCATION_SOURCE_VERSION,
                "address_kind": "rack_slot",
                "address_area_id": area.id,
                "rack_code": rack_letter,
                "rack_display_name": rack_name,
                "level_no": level_no,
                "slot_no": slot_no,
                "placement_status": "placed",
            }.items():
                if getattr(row, key) != value:
                    setattr(row, key, value)
                    changed = True
                    if key in {
                        "location_code",
                        "location_name",
                        "warehouse_floor",
                        "area_code",
                        "address_kind",
                        "address_area_id",
                        "rack_code",
                        "rack_display_name",
                        "level_no",
                        "slot_no",
                    }:
                        address_changed = True
            if address_changed:
                row.address_version = int(row.address_version or 0) + 1
            layout = row.floor3_layout
            if layout is None:
                row.floor3_layout = Floor3LocationLayout(
                    **geometry,
                    z_index=level_no,
                    version=1,
                    source_type="seeded",
                    layout_kind="physical_rack",
                    created_by=operator_id,
                    updated_by=operator_id,
                )
                changed = True
            else:
                geometry_changed = any(
                    getattr(layout, key) != value for key, value in geometry.items()
                )
                if geometry_changed or layout.layout_kind != "physical_rack":
                    for key, value in geometry.items():
                        setattr(layout, key, value)
                    layout.z_index = level_no
                    layout.layout_kind = "physical_rack"
                    layout.source_type = "seeded"
                    layout.version += 1
                    layout.updated_by = operator_id
                    layout.updated_at = beijing_now_naive()
                    changed = True
            if changed:
                row.updated_at = beijing_now_naive()
                updated.append(row.id)

    linked_rows = list(
        db.scalars(
            select(WarehouseLocation).where(
                WarehouseLocation.warehouse_floor == floor.floor_number,
                WarehouseLocation.map_rack_id.is_not(None),
                WarehouseLocation.is_active.is_(True),
            )
        ).all()
    )
    retired = [
        row for row in linked_rows if str(row.map_rack_id or "") not in published_rack_ids
    ]
    occupied_retired = _occupied_location_ids(db, [row.id for row in retired])
    if occupied_retired:
        raise WarehouseRackCellSyncError(
            f"有 {len(occupied_retired)} 个已从地图移除的货架层格仍有货；请先恢复货架或移货。"
        )
    for row in retired:
        row.is_active = False
        row.updated_at = beijing_now_naive()
        disabled.append(row.id)
        if row.address_area_id:
            touched_area_ids.add(row.address_area_id)

    for area_id in touched_area_ids:
        active_count = int(
            db.scalar(
                select(func.count(WarehouseLocation.id)).where(
                    WarehouseLocation.address_area_id == area_id,
                    WarehouseLocation.is_active.is_(True),
                )
            )
            or 0
        )
        area = db.get(WarehouseArea, area_id)
        if area is not None:
            area.planned_location_count = active_count
            area.updated_at = beijing_now_naive()

    db.flush()
    return WarehouseRackCellSyncResult(
        tuple(created), tuple(enabled), tuple(disabled), tuple(updated)
    )
