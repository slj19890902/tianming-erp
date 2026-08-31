from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
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
    bound_legacy_location_ids: tuple[int, ...] = ()


def _rack_cell_counts(rack: dict) -> list[int] | None:
    counts = rack.get("level_cell_counts")
    levels = int(rack.get("levels") or 0)
    if not isinstance(counts, list) or len(counts) != levels or levels <= 0:
        return None
    try:
        normalized = [int(value) for value in counts]
    except (TypeError, ValueError):
        return None
    if any(value < 0 or value > 50 for value in normalized):
        return None
    return normalized


def _preview_rack_area(
    db: Session,
    *,
    floor: WarehouseFloor,
    rack: dict,
    features: dict[str, dict],
) -> WarehouseArea | None:
    """Resolve only an explicit formal area identity for a draft/published rack."""

    feature_id = str(rack.get("area_feature_id") or "").strip()
    feature = features.get(feature_id)
    if feature is None:
        return None
    raw_area_id = feature.get("formal_area_id")
    if raw_area_id not in (None, ""):
        try:
            area_id = int(raw_area_id)
        except (TypeError, ValueError):
            return None
        area = db.get(WarehouseArea, area_id)
        if area is None or area.floor_id != floor.id:
            return None
        raw_floor_id = feature.get("formal_floor_id")
        if raw_floor_id not in (None, ""):
            try:
                if int(raw_floor_id) != floor.id:
                    return None
            except (TypeError, ValueError):
                return None
        area_code = str(feature.get("erp_area_code") or "").strip().upper()
        if area_code and area.area_code.upper() != area_code:
            return None
        return area
    area_code = str(feature.get("erp_area_code") or "").strip().upper()
    if area_code:
        return db.scalar(
            select(WarehouseArea).where(
                WarehouseArea.floor_id == floor.id,
                func.upper(WarehouseArea.area_code) == area_code,
            )
        )
    policy = db.scalar(
        select(WarehouseAreaStoragePolicy)
        .options(selectinload(WarehouseAreaStoragePolicy.area))
        .where(WarehouseAreaStoragePolicy.map_feature_id == feature_id)
    )
    if policy is None or policy.area.floor_id != floor.id:
        return None
    return policy.area


def preview_legacy_rack_cell_bindings(
    db: Session,
    *,
    floor_layout: dict,
) -> dict:
    """Return exact old-rack-to-map candidates without writing any fact.

    Candidate membership is structural: same formal area and every existing
    level/slot must exist on the target rack.  Names, geometry proximity and
    array order are deliberately ignored.  Ambiguous groups are returned for
    an administrator to choose; they are never silently selected.
    """

    floor_code = str(floor_layout.get("floor_code") or "").strip().upper()
    revision = str(floor_layout.get("revision") or "").strip()
    floor = db.scalar(
        select(WarehouseFloor).where(func.upper(WarehouseFloor.floor_code) == floor_code)
    )
    if floor is None:
        empty_facts = {"floor_code": floor_code, "revision": revision, "groups": []}
        return {
            **empty_facts,
            "fingerprint": hashlib.sha256(
                json.dumps(empty_facts, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
            "requires_confirmation": False,
            "unresolved_count": 0,
        }
    features = {
        str(item.get("id")): item
        for item in floor_layout.get("features") or []
        if item.get("feature_kind") == "zone" and item.get("id")
    }
    rack_targets_by_area: dict[int, list[dict]] = {}
    for rack in floor_layout.get("racks") or []:
        map_rack_id = str(rack.get("id") or "").strip()
        counts = _rack_cell_counts(rack)
        area = _preview_rack_area(
            db, floor=floor, rack=rack, features=features
        )
        if area is None:
            continue
        rack_targets_by_area.setdefault(area.id, [])
        if not map_rack_id or counts is None:
            continue
        cells = {
            (level_no, slot_no)
            for level_no, count in enumerate(counts, start=1)
            for slot_no in range(1, count + 1)
        }
        rack_targets_by_area[area.id].append(
            {
                "map_rack_id": map_rack_id,
                "rack_name": str(
                    rack.get("name") or rack.get("rack_code") or map_rack_id
                ).strip(),
                "cells": cells,
            }
        )

    area_ids = sorted(rack_targets_by_area)
    if not area_ids:
        legacy_rows: list[WarehouseLocation] = []
    else:
        legacy_rows = list(
            db.scalars(
                select(WarehouseLocation)
                .where(
                    WarehouseLocation.warehouse_floor == floor.floor_number,
                    WarehouseLocation.address_area_id.in_(area_ids),
                    WarehouseLocation.storage_type == "rack",
                    WarehouseLocation.address_kind == "rack_slot",
                    WarehouseLocation.map_rack_id.is_(None),
                    WarehouseLocation.is_active.is_(True),
                )
                .order_by(
                    WarehouseLocation.address_area_id,
                    WarehouseLocation.rack_code,
                    WarehouseLocation.level_no,
                    WarehouseLocation.slot_no,
                    WarehouseLocation.id,
                )
            ).all()
        )
    grouped: dict[tuple[int, str], list[WarehouseLocation]] = {}
    for row in legacy_rows:
        if (
            row.address_area_id is None
            or not row.rack_code
            or row.level_no is None
            or row.slot_no is None
        ):
            continue
        grouped.setdefault((int(row.address_area_id), str(row.rack_code)), []).append(row)

    all_target_ids = {
        target["map_rack_id"]
        for targets in rack_targets_by_area.values()
        for target in targets
    }
    existing_target_rows = (
        list(
            db.scalars(
                select(WarehouseLocation).where(
                    WarehouseLocation.map_rack_id.in_(sorted(all_target_ids))
                )
            ).all()
        )
        if all_target_ids
        else []
    )
    claimed_cells: dict[str, set[tuple[int, int]]] = {}
    for row in existing_target_rows:
        if row.level_no is None or row.slot_no is None or not row.map_rack_id:
            continue
        claimed_cells.setdefault(str(row.map_rack_id), set()).add(
            (int(row.level_no), int(row.slot_no))
        )

    groups: list[dict] = []
    for (area_id, rack_code), rows in sorted(grouped.items()):
        area = db.get(WarehouseArea, area_id)
        cells = {(int(row.level_no), int(row.slot_no)) for row in rows}
        occupied_ids = _occupied_location_ids(db, [row.id for row in rows])
        candidates = []
        for target in sorted(
            rack_targets_by_area.get(area_id, []), key=lambda item: item["map_rack_id"]
        ):
            if not cells.issubset(target["cells"]):
                continue
            if cells & claimed_cells.get(target["map_rack_id"], set()):
                continue
            candidates.append(
                {
                    "map_rack_id": target["map_rack_id"],
                    "rack_name": target["rack_name"],
                }
            )
        groups.append(
            {
                "binding_key": f"{area_id}:{rack_code}",
                "area_id": area_id,
                "area_code": area.area_code if area is not None else "",
                "area_name": area.area_name if area is not None else "",
                "legacy_rack_code": rack_code,
                "location_ids": [row.id for row in rows],
                "location_codes": [row.location_code for row in rows],
                "address_versions": [int(row.address_version or 0) for row in rows],
                "location_count": len(rows),
                "occupied_location_count": len(occupied_ids),
                "candidates": candidates,
                "suggested_map_rack_id": (
                    candidates[0]["map_rack_id"] if len(candidates) == 1 else None
                ),
                "blocking_reason": (
                    None
                    if candidates
                    else "现有层格超出货架范围，或目标层格已绑定其他位置"
                ),
            }
        )
    target_group_counts: dict[str, int] = {}
    for group in groups:
        for candidate in group["candidates"]:
            target = candidate["map_rack_id"]
            target_group_counts[target] = target_group_counts.get(target, 0) + 1
    for group in groups:
        if len(group["candidates"]) != 1:
            group["suggested_map_rack_id"] = None
            continue
        target = group["candidates"][0]["map_rack_id"]
        group["suggested_map_rack_id"] = (
            target if target_group_counts.get(target) == 1 else None
        )
    fingerprint_facts = {
        "floor_code": floor_code,
        "revision": revision,
        "groups": groups,
    }
    fingerprint = hashlib.sha256(
        json.dumps(
            fingerprint_facts,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return {
        **fingerprint_facts,
        "fingerprint": fingerprint,
        "requires_confirmation": bool(groups),
        "unresolved_count": sum(1 for group in groups if not group["candidates"]),
    }


def apply_confirmed_legacy_rack_cell_bindings(
    db: Session,
    *,
    floor_layout: dict,
    expected_fingerprint: str | None,
    bindings: list[dict] | None,
) -> tuple[int, ...]:
    """Bind existing location identities after one explicit admin confirmation."""

    preview = preview_legacy_rack_cell_bindings(db, floor_layout=floor_layout)
    groups = {item["binding_key"]: item for item in preview["groups"]}
    requested = list(bindings or [])
    if not groups and not requested:
        return ()
    if not groups and requested:
        # Idempotent map-publish replay: the same stable rows may already carry
        # the confirmed target.  This path never writes and accepts only an
        # exact area/rack identity that is already present.
        for item in requested:
            key = str(item.get("binding_key") or "").strip()
            target = str(item.get("map_rack_id") or "").strip()
            try:
                area_text, rack_code = key.split(":", 1)
                area_id = int(area_text)
            except (TypeError, ValueError):
                raise WarehouseRackCellSyncError(
                    "旧货位绑定确认内容无效，请刷新后重试。"
                )
            exists = db.scalar(
                select(WarehouseLocation.id).where(
                    WarehouseLocation.address_area_id == area_id,
                    WarehouseLocation.rack_code == rack_code,
                    WarehouseLocation.map_rack_id == target,
                    WarehouseLocation.is_active.is_(True),
                ).limit(1)
            )
            if exists is None:
                raise WarehouseRackCellSyncError(
                    "旧货位绑定预览已变化，请刷新后重新核对。"
                )
        return ()
    if groups and (not expected_fingerprint or not requested):
        raise WarehouseRackCellSyncError(
            "当前发布仍有旧货位尚未确认对应货架，请先逐架核对。"
        )
    if not expected_fingerprint or expected_fingerprint != preview["fingerprint"]:
        raise WarehouseRackCellSyncError(
            "旧货位绑定预览已变化，请刷新后重新核对。"
        )
    by_key: dict[str, str] = {}
    for item in requested:
        key = str(item.get("binding_key") or "").strip()
        target = str(item.get("map_rack_id") or "").strip()
        if not key or not target or key in by_key:
            raise WarehouseRackCellSyncError("旧货位绑定确认内容重复或不完整。")
        by_key[key] = target
    if set(by_key) != set(groups):
        raise WarehouseRackCellSyncError(
            "旧货位绑定确认必须完整覆盖本次预览的全部旧货架。"
        )
    reused_targets = {
        target for target in by_key.values() if list(by_key.values()).count(target) > 1
    }
    if reused_targets:
        raise WarehouseRackCellSyncError("同一地图货架不能同时绑定多个旧货架。")
    bound_ids: list[int] = []
    for key, target in sorted(by_key.items()):
        group = groups[key]
        allowed = {item["map_rack_id"] for item in group["candidates"]}
        if target not in allowed:
            raise WarehouseRackCellSyncError(
                f"{group['area_code']} {group['legacy_rack_code']}架的目标货架已不再适用。"
            )
        rows = list(
            db.scalars(
                select(WarehouseLocation)
                .where(WarehouseLocation.id.in_(group["location_ids"]))
                .order_by(WarehouseLocation.id)
            ).all()
        )
        if [row.id for row in rows] != sorted(group["location_ids"]):
            raise WarehouseRackCellSyncError("旧货位已变化，请刷新后重新核对。")
        for row in rows:
            if row.map_rack_id is not None or not row.is_active:
                raise WarehouseRackCellSyncError(
                    f"{row.location_code} 已被其他操作处理，请刷新后重试。"
                )
            row.map_rack_id = target
            row.updated_at = beijing_now_naive()
            bound_ids.append(row.id)
    db.flush()
    return tuple(bound_ids)


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
    expected_legacy_binding_fingerprint: str | None = None,
    confirmed_legacy_bindings: list[dict] | None = None,
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
    # Every publish recomputes the preview.  A caller cannot bypass an old
    # occupied rack merely by omitting the confirmation fields.
    bound_legacy_ids = set(
        apply_confirmed_legacy_rack_cell_bindings(
            db,
            floor_layout=floor_layout,
            expected_fingerprint=expected_legacy_binding_fingerprint,
            bindings=confirmed_legacy_bindings,
        )
    )
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
            unconfirmed_rack_cells = [
                row for row in generic_rows if row.address_kind == "rack_slot"
            ]
            if unconfirmed_rack_cells:
                raise WarehouseRackCellSyncError(
                    f"{area.area_name} 有 {len(unconfirmed_rack_cells)} 个旧货架层格尚未确认对应货架；"
                    "请先预览并由管理员一次确认绑定。"
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
            # Attaching an existing printed location to a stable map rack is
            # one address-identity change.  Fold all other address updates in
            # this same publish into the same single version increment.
            address_changed = row.id in bound_legacy_ids
            if not row.is_active:
                row.is_active = True
                enabled.append(row.id)
                changed = True
            identity_values = {
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
            }
            # A formal rack cell keeps its printed code/name for life.  Rack
            # renames and later republishes update the separate display/map
            # identity without invalidating labels already fixed on site.
            identity_values.pop("location_code")
            identity_values.pop("location_name")
            for key, value in identity_values.items():
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
        tuple(created),
        tuple(enabled),
        tuple(disabled),
        tuple(updated),
        tuple(sorted(bound_legacy_ids)),
    )
