from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
import re
from threading import RLock
import unicodedata

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.models.mold_tool import MoldTool
from app.models.printing_plate import PrintingPlate
from app.models.production import ProductionCompletion
from app.models.warehouse_inventory import (
    InventoryLocationMovement,
    InventoryLot,
    InventoryPallet,
    WarehouseArea,
    WarehouseAreaStoragePolicy,
    WarehouseFloor,
    WarehouseLocation,
    WarehouseLocationAddressMutation,
    WarehouseLocationAlias,
)


WAREHOUSE_LOCATION_ADDRESS_LOCK = RLock()
MANAGED_ADDRESS_KINDS = {"rack_slot", "ground_slot"}
MEASURED_MAP_LOCATION_SOURCES = {"V11", "TWIN_V1"}
LEGACY_V11_RIGHT_AREA_CODES = frozenset(
    {
        "A1",
        "A2",
        "AB1",
        "AB2",
        "B1",
        "B2",
        "C1",
        "C2",
        "CD1",
        "D1",
        "D2",
        "DE1",
        "E1",
        "E2",
        "E3",
        "E4",
        "F1",
        "F12",
        "F2",
        "F3",
        "F34",
        "F4",
    }
)
_V11_SIDE_NAMES = {
    "L": "左侧",
    "R": "右侧",
    "M": "中间",
    "U": "上侧",
    "D": "下侧",
}


class WarehouseLocationAddressError(RuntimeError):
    def __init__(self, code: str, message: str, *, status_code: int = 409):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class PublishedMeasuredMapReadiness:
    position_status: str
    issue: str | None
    map_feature_id: str | None = None
    published_map_revision: str | None = None


def _policy_value(
    policy: WarehouseAreaStoragePolicy | Mapping[str, object] | None,
    field: str,
) -> object | None:
    if policy is None:
        return None
    if isinstance(policy, Mapping):
        return policy.get(field)
    return getattr(policy, field, None)


def published_measured_map_readiness(
    location: WarehouseLocation,
    *,
    floor: WarehouseFloor | None,
    area: WarehouseArea | None,
    policy: WarehouseAreaStoragePolicy | Mapping[str, object] | None,
    published_floor_identity: Mapping[str, object] | None,
    has_geometry: bool,
    ground_layout: Mapping[str, object] | None = None,
    require_geometry: bool = True,
) -> PublishedMeasuredMapReadiness:
    """Classify one stable location against the current published measured map."""

    if not location.is_active:
        return PublishedMeasuredMapReadiness("disabled", "该库位已停用")
    if location.placement_status != "placed":
        return PublishedMeasuredMapReadiness(
            "unplaced",
            "该库位尚未完成正式平面图布局",
        )
    if location.warehouse_floor is None or not str(location.area_code or "").strip():
        return PublishedMeasuredMapReadiness(
            "unlocated",
            "该库位尚未登记楼层和区域",
        )
    if floor is None:
        return PublishedMeasuredMapReadiness(
            "area_only",
            "该库位所属楼层尚未建立台账",
        )
    if floor.construction_status != "enabled":
        return PublishedMeasuredMapReadiness(
            "area_only",
            "该库位所属楼层尚未启用",
        )
    if area is None:
        return PublishedMeasuredMapReadiness(
            "area_only",
            "该库位所属区域尚未建立台账",
        )
    if area.construction_status != "enabled":
        return PublishedMeasuredMapReadiness(
            "area_only",
            "该库位所属区域尚未启用",
        )
    if published_floor_identity is None:
        return PublishedMeasuredMapReadiness(
            "area_only",
            "当前运行地图不可用",
        )
    current_revision = str(
        published_floor_identity.get("revision") or ""
    ).strip()
    if not current_revision:
        return PublishedMeasuredMapReadiness(
            "area_only",
            "当前运行地图缺少正式版本",
        )

    source_version = str(location.source_version or "").strip().upper()
    # Publishing an area policy does not rewrite the stable identity of the
    # seeded V11 locations inside that area.  Policy-backed V11 locations use
    # the strict current-policy binding checks below, while policy-free V11
    # locations retain the unique-area compatibility projection.
    legacy_v11 = bool(
        source_version == "V11"
        and int(location.warehouse_floor or 0) == 3
    )
    if legacy_v11 and policy is None:
        area_code = str(location.area_code or "").strip().upper()
        zone_ids_by_area = {
            str(key).strip().upper(): tuple(
                str(value).strip()
                for value in values
                if str(value).strip()
            )
            for key, values in dict(
                published_floor_identity.get("zone_ids_by_area", {})
            ).items()
            if str(key).strip() and isinstance(values, (list, tuple, set, frozenset))
        }
        feature_ids = zone_ids_by_area.get(area_code, ())
        if len(feature_ids) != 1:
            return PublishedMeasuredMapReadiness(
                "area_only",
                "该 V11 库位所属区域无法唯一对应当前运行地图要素",
            )
        feature_id = feature_ids[0]
    else:
        if not legacy_v11 and source_version != "TWIN_V1":
            return PublishedMeasuredMapReadiness(
                "area_only",
                "该库位不是当前实测地图生成的正式位置",
            )
        if policy is None or _policy_value(policy, "status") != "published":
            return PublishedMeasuredMapReadiness(
                "area_only",
                "该库位所属区域尚未发布",
            )
        policy_revision = str(
            _policy_value(policy, "published_map_revision") or ""
        ).strip()
        if not policy_revision:
            return PublishedMeasuredMapReadiness(
                "area_only",
                "该库位所属区域缺少已发布地图版本",
            )
        if policy_revision != current_revision:
            return PublishedMeasuredMapReadiness(
                "area_only",
                "该库位所属区域的发布版本不是当前运行地图版本",
            )
        feature_id = str(_policy_value(policy, "map_feature_id") or "").strip()
        if not feature_id:
            return PublishedMeasuredMapReadiness(
                "area_only",
                "该库位所属区域缺少已发布地图要素",
            )
        zones_by_id = {
            str(key).strip(): str(value or "").strip().upper()
            for key, value in dict(
                published_floor_identity.get("zones_by_id", {})
            ).items()
            if str(key).strip()
        }
        if zones_by_id.get(feature_id) != str(area.area_code or "").strip().upper():
            return PublishedMeasuredMapReadiness(
                "area_only",
                "该库位所属区域与当前运行地图要素不一致",
            )
    if not legacy_v11 and str(location.storage_type or "").strip().lower() in {
        "ground",
        "temporary_aisle",
    }:
        ground_status = str((ground_layout or {}).get("status") or "")
        ground_revision = str(
            (ground_layout or {}).get("published_map_revision") or ""
        ).strip()
        ground_area_id = (ground_layout or {}).get("area_id")
        ground_location_id = (ground_layout or {}).get("location_id")
        if (
            ground_status != "published"
            or ground_revision != current_revision
            or int(ground_area_id or 0) != int(area.id or 0)
            or int(ground_location_id or 0) != int(location.id or 0)
        ):
            return PublishedMeasuredMapReadiness(
                "area_only",
                "该地堆库位缺少当前运行地图的已发布排位",
            )
    if require_geometry and not has_geometry:
        return PublishedMeasuredMapReadiness(
            "area_only",
            "该库位缺少已确认的地图几何位置",
        )
    return PublishedMeasuredMapReadiness(
        "mapped",
        None,
        map_feature_id=feature_id,
        published_map_revision=current_revision,
    )


@dataclass(frozen=True)
class AddressChangeCommand:
    action_kind: str
    area_id: int | None = None
    location_id: int | None = None
    current_rack_code: str | None = None
    new_zone_code: str | None = None
    new_subzone_no: int | None = None
    new_address_kind: str | None = None
    new_rack_code: str | None = None
    new_level_no: int | None = None
    new_ground_row_no: int | None = None
    new_slot_no: int | None = None


def normalize_location_alias(value: str | None) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).strip()
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized.upper()


def canonical_area_code(zone_code: str, subzone_no: int) -> str:
    zone = normalize_location_alias(zone_code)
    if not re.fullmatch(r"[A-G]", zone):
        raise WarehouseLocationAddressError(
            "WAREHOUSE_ADDRESS_ZONE_INVALID",
            "大区只能使用 A～G。",
            status_code=422,
        )
    if not 1 <= int(subzone_no) <= 99:
        raise WarehouseLocationAddressError(
            "WAREHOUSE_ADDRESS_SUBZONE_INVALID",
            "子区编号必须在 1～99 之间。",
            status_code=422,
        )
    return f"{zone}{int(subzone_no):02d}"


def _rack_code(value: str | None) -> str:
    normalized = normalize_location_alias(value)
    if not re.fullmatch(r"[A-Z]", normalized):
        raise WarehouseLocationAddressError(
            "WAREHOUSE_ADDRESS_RACK_INVALID",
            "货架编号只能使用 A～Z。",
            status_code=422,
        )
    return normalized


def _two_digit(value: int | None, *, label: str) -> int:
    number = int(value or 0)
    if not 1 <= number <= 99:
        raise WarehouseLocationAddressError(
            "WAREHOUSE_ADDRESS_NUMBER_INVALID",
            f"{label}必须在 1～99 之间。",
            status_code=422,
        )
    return number


def _floor_code(floor_number: int) -> str:
    return f"{int(floor_number)}F"


def _floor_name(floor_number: int) -> str:
    known = {1: "一楼", 2: "二楼", 3: "三楼", 4: "四楼"}
    return known.get(int(floor_number), f"{int(floor_number)}楼")


def _area_human_name(area: WarehouseArea) -> str | None:
    if area.address_zone_code and area.address_subzone_no:
        return f"{area.address_zone_code}{int(area.address_subzone_no)}区"
    return None


def _area_projection_value(
    area: WarehouseArea | Mapping[str, object] | None,
    *keys: str,
) -> object | None:
    if area is None:
        return None
    if isinstance(area, Mapping):
        for key in keys:
            value = area.get(key)
            if value not in (None, ""):
                return value
        return None
    for key in keys:
        value = getattr(area, key, None)
        if value not in (None, ""):
            return value
    return None


def _is_generic_legacy_area_name(name: str, area_code: str) -> bool:
    compact = re.sub(r"\s+", "", name).upper()
    code = area_code.upper()
    return compact in {f"{code}区", f"三楼{code}区"}


def employee_area_name(
    area: WarehouseArea | Mapping[str, object] | None = None,
    *,
    area_code: str | None = None,
    floor_number: int | None = None,
    fallback_name: str | None = None,
) -> str:
    """Return the single employee-facing area name without changing its identity.

    The 22 measured V11 areas on the right side historically persisted only
    generic labels such as ``A1 区``.  Until an administrator publishes a real
    area name, their employee default is ``右区A1``.  A non-generic formal name
    always wins, so later area-planning edits flow through every projection.
    """

    code = str(
        area_code
        or _area_projection_value(area, "area_code", "erp_area_code")
        or ""
    ).strip().upper()
    projected_floor_number = int(
        floor_number
        or _area_projection_value(area, "floor_number", "warehouse_floor")
        or getattr(getattr(area, "floor", None), "floor_number", 0)
        or 0
    )
    uses_v11_right_default = bool(
        code in LEGACY_V11_RIGHT_AREA_CODES
        and projected_floor_number == 3
    )
    formal_name = str(
        _area_projection_value(area, "formal_area_name", "area_name") or ""
    ).strip()
    if formal_name and not (
        uses_v11_right_default
        and _is_generic_legacy_area_name(formal_name, code)
    ):
        return formal_name
    if uses_v11_right_default:
        return f"右区{code}"
    if formal_name:
        return formal_name
    fallback = str(
        fallback_name or _area_projection_value(area, "name") or ""
    ).strip()
    return fallback or code or "区域名称待完善"


def _location_sequence(location: WarehouseLocation) -> int | None:
    match = re.search(r"(\d+)$", str(location.location_code or "").strip())
    if match is None:
        return None
    number = int(match.group(1))
    return number if number > 0 else None


def _measured_map_location_name(
    location: WarehouseLocation,
    *,
    area: WarehouseArea | None,
    floor: WarehouseFloor | None,
) -> str | None:
    """Project one published-map location into the employee-facing address.

    V11 intentionally stored the code in ``location_name``.  The measured map
    already carries the missing floor/area meaning, so all consumers must use
    this one projection instead of inventing labels independently.
    """

    if str(location.source_version or "").strip().upper() != "V11":
        return None
    floor_number = int(
        (floor.floor_number if floor is not None else location.warehouse_floor) or 0
    )
    area_name = employee_area_name(
        area,
        area_code=location.area_code,
        floor_number=floor_number,
    )
    sequence = _location_sequence(location)
    if not floor_number or not area_name or sequence is None:
        return None

    floor_name = _floor_name(floor_number)
    normalized_area_name = re.sub(r"\s+", "", area_name).upper()
    has_floor_prefix = any(
        normalized_area_name.startswith(re.sub(r"\s+", "", marker).upper())
        for marker in (floor_name, _floor_code(floor_number))
    )
    prefix = area_name if has_floor_prefix else f"{floor_name} {area_name}"
    storage_type = str(location.storage_type or "").strip().lower()
    suffix_match = re.search(
        r"(?:^|-)([A-Z])?(\d+)$",
        str(location.location_code or "").strip().upper(),
    )
    side_code = str(
        location.side_code
        or (suffix_match.group(1) if suffix_match is not None else "")
        or ""
    ).strip().upper()
    side_name = _V11_SIDE_NAMES.get(side_code, "")
    if storage_type == "temporary_aisle" or side_code == "P":
        return f"{prefix}·临放第{sequence}位"
    if storage_type == "rack":
        level_no = int(location.level_no or 0)
        level = f"{level_no}层·" if level_no else ""
        return f"{prefix}·{level}{side_name}第{sequence}格"
    return f"{prefix}·{side_name}第{sequence}位"


def format_location_address(
    location: WarehouseLocation,
    *,
    area: WarehouseArea | None = None,
    floor: WarehouseFloor | None = None,
) -> tuple[str, str]:
    area = area or getattr(location, "address_area", None)
    if floor is None and area is not None:
        floor = getattr(area, "floor", None)
    floor_number = int(
        (floor.floor_number if floor is not None else location.warehouse_floor) or 0
    )
    if (
        location.address_kind == "rack_slot"
        and area is not None
        and area.address_zone_code
        and area.address_subzone_no
        and location.rack_code
        and location.level_no
        and location.slot_no
        and floor_number
    ):
        area_code = canonical_area_code(
            area.address_zone_code, area.address_subzone_no
        )
        code = (
            f"{_floor_code(floor_number)}-{area_code}-{location.rack_code}-"
            f"{int(location.level_no):02d}-{int(location.slot_no):02d}"
        )
        human = (
            f"{_floor_name(floor_number)} "
            f"{area.address_zone_code}{int(area.address_subzone_no)}区·"
            f"{location.rack_code}架·{int(location.level_no)}层·"
            f"{int(location.slot_no)}格"
        )
        return code, human
    if (
        location.address_kind == "ground_slot"
        and area is not None
        and area.address_zone_code
        and area.address_subzone_no
        and location.ground_row_no
        and location.slot_no
        and floor_number
    ):
        area_code = canonical_area_code(
            area.address_zone_code, area.address_subzone_no
        )
        code = (
            f"{_floor_code(floor_number)}-{area_code}-"
            f"P{int(location.ground_row_no):02d}-{int(location.slot_no):02d}"
        )
        human = (
            f"{_floor_name(floor_number)} "
            f"{area.address_zone_code}{int(area.address_subzone_no)}区·"
            f"第{int(location.ground_row_no)}排·{int(location.slot_no)}号位"
        )
        return code, human
    measured_name = _measured_map_location_name(
        location,
        area=area,
        floor=floor,
    )
    if measured_name:
        return location.location_code, measured_name
    return location.location_code, location.location_name


def employee_location_name(
    location: WarehouseLocation | None,
    *,
    area: WarehouseArea | None = None,
    floor: WarehouseFloor | None = None,
) -> str:
    if location is None:
        return "位置待确认"
    code, human = format_location_address(location, area=area, floor=floor)
    if human and normalize_location_alias(human) != normalize_location_alias(code):
        return human
    return "位置名称待完善"


def location_address_payload(
    location: WarehouseLocation,
    *,
    area: WarehouseArea | None = None,
    floor: WarehouseFloor | None = None,
    position_status: str | None = None,
) -> dict:
    area = area or getattr(location, "address_area", None)
    if floor is None and area is not None:
        floor = getattr(area, "floor", None)
    current_code, current_name = format_location_address(
        location,
        area=area,
        floor=floor,
    )
    if location.address_kind in MANAGED_ADDRESS_KINDS:
        projection_source = "structured_address"
    elif _measured_map_location_name(location, area=area, floor=floor):
        projection_source = (
            "published_measured_map"
            if position_status == "mapped"
            else "measured_map_name_unpublished"
        )
    elif current_name and normalize_location_alias(current_name) != normalize_location_alias(current_code):
        projection_source = "location_master"
    else:
        projection_source = "name_pending"
    return {
        "warehouse_floor": location.warehouse_floor,
        "area_code": location.area_code,
        "area_name": employee_area_name(
            area,
            area_code=location.area_code,
            floor_number=(
                floor.floor_number if floor is not None else location.warehouse_floor
            ),
        ),
        "area_master_name": area.area_name if area is not None else None,
        "address_kind": location.address_kind,
        "address_area_id": location.address_area_id,
        "address_zone_code": area.address_zone_code if area is not None else None,
        "address_subzone_no": area.address_subzone_no if area is not None else None,
        "rack_code": location.rack_code,
        "ground_row_no": location.ground_row_no,
        "level_no": location.level_no,
        "slot_no": location.slot_no,
        "address_version": int(location.address_version or 1),
        "current_address_code": current_code,
        "current_address_name": current_name,
        "employee_location_name": employee_location_name(
            location,
            area=area,
            floor=floor,
        ),
        "projection_source": projection_source,
    }


def _load_area(db: Session, area_id: int, *, lock: bool = False) -> WarehouseArea:
    query = (
        select(WarehouseArea)
        .options(selectinload(WarehouseArea.floor))
        .where(WarehouseArea.id == area_id)
    )
    if lock:
        query = query.with_for_update()
    area = db.scalar(query)
    if area is None:
        raise WarehouseLocationAddressError(
            "WAREHOUSE_ADDRESS_AREA_NOT_FOUND", "区域不存在。", status_code=404
        )
    return area


def _load_location(
    db: Session, location_id: int, *, lock: bool = False
) -> WarehouseLocation:
    query = (
        select(WarehouseLocation)
        .options(
            selectinload(WarehouseLocation.address_area).selectinload(
                WarehouseArea.floor
            ),
            selectinload(WarehouseLocation.floor3_layout),
        )
        .where(WarehouseLocation.id == location_id)
    )
    if lock:
        query = query.with_for_update()
    location = db.scalar(query)
    if location is None:
        raise WarehouseLocationAddressError(
            "WAREHOUSE_ADDRESS_LOCATION_NOT_FOUND", "位置不存在。", status_code=404
        )
    return location


def _structured_area(area: WarehouseArea) -> None:
    if not area.address_zone_code or not area.address_subzone_no:
        raise WarehouseLocationAddressError(
            "WAREHOUSE_ADDRESS_AREA_UNSTRUCTURED",
            "请先为该区域确认 A～G 大区和两位数子区。",
        )


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _command_payload(command: AddressChangeCommand) -> dict:
    return {
        "action_kind": command.action_kind,
        "area_id": command.area_id,
        "location_id": command.location_id,
        "current_rack_code": normalize_location_alias(command.current_rack_code)
        or None,
        "new_zone_code": normalize_location_alias(command.new_zone_code) or None,
        "new_subzone_no": command.new_subzone_no,
        "new_address_kind": normalize_location_alias(command.new_address_kind).lower()
        or None,
        "new_rack_code": normalize_location_alias(command.new_rack_code) or None,
        "new_level_no": command.new_level_no,
        "new_ground_row_no": command.new_ground_row_no,
        "new_slot_no": command.new_slot_no,
    }


def _current_entry(location: WarehouseLocation) -> dict:
    code, name = format_location_address(location)
    return {
        "location_id": location.id,
        "address_version": int(location.address_version or 1),
        "address_kind": location.address_kind,
        "area_id": location.address_area_id,
        "location_code": code,
        "location_name": name,
        "rack_code": location.rack_code,
        "ground_row_no": location.ground_row_no,
        "level_no": location.level_no,
        "slot_no": location.slot_no,
        "warehouse_floor": location.warehouse_floor,
        "area_code": location.area_code,
    }


def _entry_after(
    location: WarehouseLocation,
    *,
    area: WarehouseArea,
    address_kind: str,
    rack_code: str | None,
    ground_row_no: int | None,
    level_no: int | None,
    slot_no: int | None,
) -> dict:
    floor_number = int(area.floor.floor_number)
    area_code = canonical_area_code(
        area.address_zone_code or "", area.address_subzone_no or 0
    )
    if address_kind == "rack_slot":
        code = (
            f"{_floor_code(floor_number)}-{area_code}-{rack_code}-"
            f"{int(level_no or 0):02d}-{int(slot_no or 0):02d}"
        )
        name = (
            f"{_floor_name(floor_number)} "
            f"{area.address_zone_code}{int(area.address_subzone_no or 0)}区·"
            f"{rack_code}架·{int(level_no or 0)}层·{int(slot_no or 0)}格"
        )
    else:
        code = (
            f"{_floor_code(floor_number)}-{area_code}-"
            f"P{int(ground_row_no or 0):02d}-{int(slot_no or 0):02d}"
        )
        name = (
            f"{_floor_name(floor_number)} "
            f"{area.address_zone_code}{int(area.address_subzone_no or 0)}区·"
            f"第{int(ground_row_no or 0)}排·{int(slot_no or 0)}号位"
        )
    return {
        "location_id": location.id,
        "address_version": int(location.address_version or 1) + 1,
        "address_kind": address_kind,
        "area_id": area.id,
        "location_code": code,
        "location_name": name,
        "rack_code": rack_code,
        "ground_row_no": ground_row_no,
        "level_no": level_no,
        "slot_no": slot_no,
        "warehouse_floor": area.floor.floor_number,
        "area_code": area_code,
    }


def _location_impacts(db: Session, entries: list[dict]) -> dict:
    ids = [int(entry["location_id"]) for entry in entries]
    codes = [str(entry["location_code"]) for entry in entries]
    if not ids:
        return {
            "active_inventory_lots": 0,
            "current_pallets": 0,
            "inventory_movements": 0,
            "production_completions": 0,
            "molds_with_text_reference": 0,
            "printing_plates_with_text_reference": 0,
        }
    return {
        "active_inventory_lots": int(
            db.scalar(
                select(func.count(InventoryLot.id)).where(
                    InventoryLot.warehouse_location_id.in_(ids),
                    InventoryLot.status.in_(("active", "frozen")),
                )
            )
            or 0
        ),
        "current_pallets": int(
            db.scalar(
                select(func.count(InventoryPallet.id)).where(
                    InventoryPallet.location_id.in_(ids),
                    InventoryPallet.is_current.is_(True),
                )
            )
            or 0
        ),
        "inventory_movements": int(
            db.scalar(
                select(func.count(InventoryLocationMovement.id)).where(
                    or_(
                        InventoryLocationMovement.from_location_id.in_(ids),
                        InventoryLocationMovement.to_location_id.in_(ids),
                    )
                )
            )
            or 0
        ),
        "production_completions": int(
            db.scalar(
                select(func.count(ProductionCompletion.id)).where(
                    ProductionCompletion.warehouse_location_id.in_(ids)
                )
            )
            or 0
        ),
        "molds_with_text_reference": int(
            db.scalar(
                select(func.count(MoldTool.id)).where(MoldTool.rack_location.in_(codes))
            )
            or 0
        ),
        "printing_plates_with_text_reference": int(
            db.scalar(
                select(func.count(PrintingPlate.id)).where(
                    PrintingPlate.rack_location.in_(codes)
                )
            )
            or 0
        ),
    }


def _assert_proposed_codes_available(
    db: Session, before: list[dict], after: list[dict]
) -> None:
    target_by_token: dict[str, int] = {}
    for entry in after:
        for value in (entry["location_code"], entry["location_name"]):
            normalized = normalize_location_alias(str(value))
            existing_target = target_by_token.get(normalized)
            if existing_target is not None and existing_target != int(
                entry["location_id"]
            ):
                raise WarehouseLocationAddressError(
                    "WAREHOUSE_ADDRESS_DUPLICATE_PATH",
                    "本次调整会生成重复的完整位置地址或中文名称。",
                )
            target_by_token[normalized] = int(entry["location_id"])
    if not target_by_token:
        return
    # SQLite/PostgreSQL string functions cannot reproduce the full address
    # contract (NFKC, trim, repeated-whitespace collapse, then case-folding).
    # Address changes are rare admin operations and the location table is
    # intentionally small, so compare every current identity in Python using
    # the same normalizer that writes and resolves permanent aliases.
    current_rows = db.execute(
        select(
            WarehouseLocation.id,
            WarehouseLocation.location_code,
            WarehouseLocation.location_name,
        )
    ).all()
    for location_id, code, name in current_rows:
        for value in (code, name):
            normalized = normalize_location_alias(value)
            target_id = target_by_token.get(normalized)
            if target_id is not None and int(location_id) != target_id:
                raise WarehouseLocationAddressError(
                    "WAREHOUSE_ADDRESS_CURRENT_CONFLICT",
                    f"新地址 {value} 已被其他稳定位置占用。",
                )
    aliases = db.execute(
        select(
            WarehouseLocationAlias.location_id,
            WarehouseLocationAlias.alias_text,
            WarehouseLocationAlias.normalized_alias,
        ).where(WarehouseLocationAlias.normalized_alias.in_(tuple(target_by_token)))
    ).all()
    for location_id, alias_text, normalized in aliases:
        if int(location_id) != target_by_token[str(normalized)]:
            raise WarehouseLocationAddressError(
                "WAREHOUSE_ADDRESS_ALIAS_CONFLICT",
                f"新地址 {alias_text} 已是其他位置的永久旧码，禁止复用。",
            )


def build_address_change_preview(
    db: Session,
    command: AddressChangeCommand,
    *,
    lock: bool = False,
) -> dict:
    action = command.action_kind.strip().lower()
    if action not in {"area", "rack", "location"}:
        raise WarehouseLocationAddressError(
            "WAREHOUSE_ADDRESS_ACTION_INVALID", "地址调整类型无效。", status_code=422
        )
    before: list[dict] = []
    after: list[dict] = []
    area: WarehouseArea
    area_before: dict | None = None
    area_after: dict | None = None

    if action == "area":
        if not command.area_id:
            raise WarehouseLocationAddressError(
                "WAREHOUSE_ADDRESS_AREA_REQUIRED", "请选择要调整的区域。", status_code=422
            )
        area = _load_area(db, command.area_id, lock=lock)
        zone_code = normalize_location_alias(command.new_zone_code)
        subzone_no = _two_digit(command.new_subzone_no, label="子区编号")
        new_area_code = canonical_area_code(zone_code, subzone_no)
        area_before = {
            "area_id": area.id,
            "address_version": int(area.address_version or 1),
            "area_code": area.area_code,
            "address_zone_code": area.address_zone_code,
            "address_subzone_no": area.address_subzone_no,
        }
        area_after = {
            "area_id": area.id,
            "address_version": int(area.address_version or 1) + 1,
            "area_code": new_area_code,
            "address_zone_code": zone_code,
            "address_subzone_no": subzone_no,
        }
        duplicate_area = db.scalar(
            select(WarehouseArea.id).where(
                WarehouseArea.floor_id == area.floor_id,
                WarehouseArea.id != area.id,
                or_(
                    func.upper(WarehouseArea.area_code) == new_area_code,
                    (
                        (WarehouseArea.address_zone_code == zone_code)
                        & (WarehouseArea.address_subzone_no == subzone_no)
                    ),
                ),
            )
        )
        if duplicate_area is not None:
            raise WarehouseLocationAddressError(
                "WAREHOUSE_ADDRESS_AREA_CONFLICT",
                f"{_floor_name(area.floor.floor_number)} {zone_code}{subzone_no}区已存在。",
            )
        rows = db.scalars(
            select(WarehouseLocation)
            .options(selectinload(WarehouseLocation.address_area))
            .where(
                or_(
                    WarehouseLocation.address_area_id == area.id,
                    (
                        (WarehouseLocation.address_area_id.is_(None))
                        & (WarehouseLocation.warehouse_floor == area.floor.floor_number)
                        & (func.upper(WarehouseLocation.area_code) == area.area_code.upper())
                    ),
                )
            )
            .order_by(WarehouseLocation.id)
        ).all()
        original_zone, original_subzone = area.address_zone_code, area.address_subzone_no
        area.address_zone_code, area.address_subzone_no = zone_code, subzone_no
        try:
            for location in rows:
                current = _current_entry(location)
                before.append(current)
                if location.address_kind in MANAGED_ADDRESS_KINDS:
                    after.append(
                        _entry_after(
                            location,
                            area=area,
                            address_kind=location.address_kind,
                            rack_code=location.rack_code,
                            ground_row_no=location.ground_row_no,
                            level_no=location.level_no,
                            slot_no=location.slot_no,
                        )
                    )
                else:
                    transitional = dict(current)
                    transitional.update(
                        {
                            "address_version": int(location.address_version or 1) + 1,
                            "area_id": area.id,
                            "warehouse_floor": area.floor.floor_number,
                            "area_code": new_area_code,
                        }
                    )
                    after.append(transitional)
        finally:
            area.address_zone_code, area.address_subzone_no = (
                original_zone,
                original_subzone,
            )
    elif action == "rack":
        if not command.area_id:
            raise WarehouseLocationAddressError(
                "WAREHOUSE_ADDRESS_AREA_REQUIRED", "请选择货架所属区域。", status_code=422
            )
        area = _load_area(db, command.area_id, lock=lock)
        _structured_area(area)
        current_rack = _rack_code(command.current_rack_code)
        new_rack = _rack_code(command.new_rack_code)
        rows = db.scalars(
            select(WarehouseLocation)
            .where(
                WarehouseLocation.address_area_id == area.id,
                WarehouseLocation.address_kind == "rack_slot",
                WarehouseLocation.rack_code == current_rack,
            )
            .order_by(WarehouseLocation.level_no, WarehouseLocation.slot_no)
        ).all()
        if not rows:
            raise WarehouseLocationAddressError(
                "WAREHOUSE_ADDRESS_RACK_NOT_FOUND", "该区域没有找到所选货架。", status_code=404
            )
        for location in rows:
            before.append(_current_entry(location))
            after.append(
                _entry_after(
                    location,
                    area=area,
                    address_kind="rack_slot",
                    rack_code=new_rack,
                    ground_row_no=None,
                    level_no=location.level_no,
                    slot_no=location.slot_no,
                )
            )
    else:
        if not command.location_id or not command.area_id:
            raise WarehouseLocationAddressError(
                "WAREHOUSE_ADDRESS_LOCATION_REQUIRED",
                "请选择稳定位置和所属区域。",
                status_code=422,
            )
        area = _load_area(db, command.area_id, lock=lock)
        _structured_area(area)
        location = _load_location(db, command.location_id, lock=lock)
        address_kind = (command.new_address_kind or "rack_slot").strip().lower()
        before.append(_current_entry(location))
        if address_kind == "rack_slot":
            after.append(
                _entry_after(
                    location,
                    area=area,
                    address_kind=address_kind,
                    rack_code=_rack_code(command.new_rack_code),
                    ground_row_no=None,
                    level_no=_two_digit(command.new_level_no, label="层号"),
                    slot_no=_two_digit(command.new_slot_no, label="格号"),
                )
            )
        elif address_kind == "ground_slot":
            after.append(
                _entry_after(
                    location,
                    area=area,
                    address_kind=address_kind,
                    rack_code=None,
                    ground_row_no=_two_digit(command.new_ground_row_no, label="排号"),
                    level_no=None,
                    slot_no=_two_digit(command.new_slot_no, label="位置号"),
                )
            )
        else:
            raise WarehouseLocationAddressError(
                "WAREHOUSE_ADDRESS_KIND_INVALID",
                "位置地址类型只能是货架格或地堆排位。",
                status_code=422,
            )

    _assert_proposed_codes_available(db, before, after)
    changed = [
        new
        for old, new in zip(before, after, strict=True)
        if any(
            old.get(key) != new.get(key)
            for key in (
                "location_code",
                "location_name",
                "address_kind",
                "area_id",
                "rack_code",
                "ground_row_no",
                "level_no",
                "slot_no",
                "area_code",
            )
        )
    ]
    area_changed = area_before != area_after if area_before is not None else False
    if not changed and not area_changed:
        raise WarehouseLocationAddressError(
            "WAREHOUSE_ADDRESS_NO_CHANGE", "地址没有变化，无需保存。"
        )
    fingerprint_payload = {
        "command": _command_payload(command),
        "area_before": area_before,
        "area_after": area_after,
        "before": before,
        "after": after,
    }
    impacts = _location_impacts(db, before)
    warnings: list[str] = []
    if any(int(value) > 0 for value in impacts.values()):
        warnings.append(
            "所选位置已有库存、栈板、生产或历史引用；确认后只改变当前地址显示，稳定位置、数量和流水保持不变。"
        )
    return {
        "action_kind": action,
        "target_ref": (
            f"area:{command.area_id}"
            if action == "area"
            else (
                f"rack:{command.area_id}:{normalize_location_alias(command.current_rack_code)}"
                if action == "rack"
                else f"location:{command.location_id}"
            )
        ),
        "preview_fingerprint": _sha256(fingerprint_payload),
        "area_before": area_before,
        "area_after": area_after,
        "affected_count": len(before),
        "changed_count": len(changed),
        "before": before,
        "after": after,
        "impacts": impacts,
        "warnings": warnings,
        "confirmation_text": "确认保存当前地址并永久保留旧码别名？",
        "writes_inventory": False,
        "writes_movements": False,
    }


def _request_hash(command: AddressChangeCommand, preview_fingerprint: str) -> str:
    return _sha256(
        {
            "command": _command_payload(command),
            "preview_fingerprint": preview_fingerprint,
        }
    )


def _add_alias(
    db: Session,
    *,
    location_id: int,
    alias_text: str,
    alias_kind: str,
    actor_user_id: int,
) -> None:
    normalized = normalize_location_alias(alias_text)
    if not normalized:
        return
    current_rows = db.execute(
        select(
            WarehouseLocation.id,
            WarehouseLocation.location_code,
            WarehouseLocation.location_name,
        )
    ).all()
    for current_owner, current_code, current_name in current_rows:
        if normalized not in {
            normalize_location_alias(current_code),
            normalize_location_alias(current_name),
        }:
            continue
        if int(current_owner) != int(location_id):
            raise WarehouseLocationAddressError(
                "WAREHOUSE_ADDRESS_ALIAS_CONFLICT",
                f"旧码 {alias_text} 已被其他稳定位置占用。",
            )
    existing = db.scalar(
        select(WarehouseLocationAlias).where(
            WarehouseLocationAlias.normalized_alias == normalized
        )
    )
    if existing is not None:
        if int(existing.location_id) != int(location_id):
            raise WarehouseLocationAddressError(
                "WAREHOUSE_ADDRESS_ALIAS_CONFLICT",
                f"旧码 {alias_text} 已属于其他稳定位置。",
            )
        return
    db.add(
        WarehouseLocationAlias(
            location_id=location_id,
            alias_text=alias_text.strip(),
            normalized_alias=normalized,
            alias_kind=alias_kind,
            created_by=actor_user_id,
        )
    )


def confirm_address_change(
    db: Session,
    command: AddressChangeCommand,
    *,
    preview_fingerprint: str,
    idempotency_key: str,
    actor_user_id: int,
) -> tuple[dict, bool]:
    key = idempotency_key.strip()
    if not key:
        raise WarehouseLocationAddressError(
            "WAREHOUSE_ADDRESS_IDEMPOTENCY_REQUIRED",
            "保存地址必须提供稳定操作键。",
            status_code=422,
        )
    submitted_hash = _request_hash(command, preview_fingerprint)
    with WAREHOUSE_LOCATION_ADDRESS_LOCK:
        existing = db.scalar(
            select(WarehouseLocationAddressMutation).where(
                WarehouseLocationAddressMutation.idempotency_key == key
            )
        )
        if existing is not None:
            if (
                int(existing.actor_user_id) != int(actor_user_id)
                or existing.request_hash != submitted_hash
            ):
                raise WarehouseLocationAddressError(
                    "WAREHOUSE_ADDRESS_IDEMPOTENCY_CONFLICT",
                    "该操作键已用于其他地址调整，请刷新后重试。",
                )
            response = json.loads(existing.response_json)
            response["replayed"] = True
            return response, True

        preview = build_address_change_preview(db, command, lock=True)
        if preview["preview_fingerprint"] != preview_fingerprint:
            raise WarehouseLocationAddressError(
                "WAREHOUSE_ADDRESS_STALE",
                "位置地址、占用或版本已变化，请刷新预览后重试。",
            )

        if command.action_kind == "area":
            area = _load_area(db, int(command.area_id or 0), lock=True)
            assert preview["area_after"] is not None
            area.area_code = preview["area_after"]["area_code"]
            area.address_zone_code = preview["area_after"]["address_zone_code"]
            area.address_subzone_no = preview["area_after"]["address_subzone_no"]
            area.address_version = preview["area_after"]["address_version"]
        else:
            area = _load_area(db, int(command.area_id or 0), lock=True)
            area.address_version = int(area.address_version or 1) + 1

        rows = {
            row.id: row
            for row in db.scalars(
                select(WarehouseLocation)
                .where(
                    WarehouseLocation.id.in_(
                        [entry["location_id"] for entry in preview["before"]]
                    )
                )
                .with_for_update()
            ).all()
        }
        for old, new in zip(preview["before"], preview["after"], strict=True):
            row = rows[int(old["location_id"])]
            if old["location_code"] != new["location_code"]:
                _add_alias(
                    db,
                    location_id=row.id,
                    alias_text=old["location_code"],
                    alias_kind="legacy_code",
                    actor_user_id=actor_user_id,
                )
            if old["location_name"] != new["location_name"]:
                _add_alias(
                    db,
                    location_id=row.id,
                    alias_text=old["location_name"],
                    alias_kind="legacy_name",
                    actor_user_id=actor_user_id,
                )
            row.location_code = new["location_code"]
            row.location_name = new["location_name"]
            row.address_kind = new["address_kind"]
            row.address_area_id = new["area_id"]
            row.rack_code = new["rack_code"]
            row.ground_row_no = new["ground_row_no"]
            row.level_no = new["level_no"]
            row.slot_no = new["slot_no"]
            row.warehouse_floor = new["warehouse_floor"]
            row.area_code = new["area_code"]
            row.address_version = new["address_version"]

        response = {
            **preview,
            "idempotency_key": key,
            "replayed": False,
            "stable_location_ids": [
                int(entry["location_id"]) for entry in preview["after"]
            ],
        }
        db.add(
            WarehouseLocationAddressMutation(
                idempotency_key=key,
                request_hash=submitted_hash,
                preview_fingerprint=preview_fingerprint,
                action_kind=preview["action_kind"],
                target_ref=preview["target_ref"],
                actor_user_id=actor_user_id,
                affected_location_ids_json=_canonical_json(
                    response["stable_location_ids"]
                ),
                before_json=_canonical_json(preview["before"]),
                after_json=_canonical_json(preview["after"]),
                response_json=_canonical_json(response),
            )
        )
        db.flush()
        return response, False


def resolve_location_address(db: Session, value: str) -> dict:
    normalized = normalize_location_alias(value)
    if not normalized:
        raise WarehouseLocationAddressError(
            "WAREHOUSE_ADDRESS_LOOKUP_REQUIRED",
            "请输入当前地址、旧码或中文位置名称。",
            status_code=422,
        )
    current_rows = db.scalars(
        select(WarehouseLocation)
        .options(
            selectinload(WarehouseLocation.address_area).selectinload(
                WarehouseArea.floor
            ),
            selectinload(WarehouseLocation.floor3_layout),
        )
    ).all()
    current = [
        location
        for location in current_rows
        if normalized
        in {
            normalize_location_alias(location.location_code),
            normalize_location_alias(location.location_name),
        }
    ]
    alias_rows = db.scalars(
        select(WarehouseLocationAlias)
        .options(
            selectinload(WarehouseLocationAlias.location)
            .selectinload(WarehouseLocation.address_area)
            .selectinload(WarehouseArea.floor)
        )
        .where(WarehouseLocationAlias.normalized_alias == normalized)
    ).all()
    locations = {row.id: row for row in current}
    matched_alias: str | None = None
    for alias in alias_rows:
        locations[alias.location_id] = alias.location
        matched_alias = alias.alias_text
    if not locations:
        raise WarehouseLocationAddressError(
            "WAREHOUSE_ADDRESS_NOT_FOUND", "没有找到对应位置。", status_code=404
        )
    if len(locations) != 1:
        raise WarehouseLocationAddressError(
            "WAREHOUSE_ADDRESS_AMBIGUOUS",
            "该文字对应多个位置，请使用完整地址或地图选择。",
        )
    location = next(iter(locations.values()))
    return {
        "location_id": location.id,
        "matched_alias": matched_alias,
        "matched_by": "legacy_alias" if matched_alias is not None else "current",
        "current": location_address_payload(location),
        "is_active": bool(location.is_active),
        "placement_status": location.placement_status or "unplaced",
        "layout_version": (
            int(location.floor3_layout.version)
            if location.floor3_layout is not None
            else None
        ),
    }


def location_alias_conflict(db: Session, code: str, *, location_id: int | None = None) -> bool:
    normalized = normalize_location_alias(code)
    alias = db.scalar(
        select(WarehouseLocationAlias).where(
            WarehouseLocationAlias.normalized_alias == normalized
        )
    )
    return bool(alias is not None and int(alias.location_id) != int(location_id or 0))
