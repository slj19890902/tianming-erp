from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from threading import Lock
from typing import Any, Callable
from uuid import uuid4

from app.services.warehouse_twin_layout import TWIN_LAYOUT_PATH


ALLOWED_INVENTORY_TYPES = {
    "finished",
    "semi_finished",
    "raw_material",
    "mold",
    "print_plate",
    "temporary_turnover",
}
ALLOWED_STORAGE_LAYOUTS = {"rack", "pallet_ground", "mixed"}
ALLOWED_ACCESS_SIDES = {"north", "south", "east", "west", "both"}
_LAYOUT_EDIT_LOCK = Lock()


class WarehouseTwinLayoutEditError(ValueError):
    pass


class WarehouseTwinLayoutEditNotFoundError(WarehouseTwinLayoutEditError):
    pass


class WarehouseTwinLayoutEditConflictError(WarehouseTwinLayoutEditError):
    pass


@dataclass(frozen=True)
class LayoutMutation:
    value: dict[str, Any]
    floor_revision: str
    applied: bool


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _floor_revision(floor: dict[str, Any]) -> str:
    revision_source = {key: value for key, value in floor.items() if key != "revision"}
    rendered = json.dumps(
        revision_source,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(rendered).hexdigest()[:16]


def _read_document(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise WarehouseTwinLayoutEditNotFoundError("数字孪生平面资产尚未导出")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise WarehouseTwinLayoutEditError("数字孪生平面资产无法读取") from error
    if payload.get("schema_version") != 1 or not isinstance(payload.get("floors"), dict):
        raise WarehouseTwinLayoutEditError("数字孪生平面资产版本不受支持")
    return payload


def _write_document(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _normalize_floor_code(floor_code: str) -> str:
    normalized = str(floor_code or "").strip().upper()
    if normalized not in {"1F", "3F"}:
        raise WarehouseTwinLayoutEditNotFoundError(
            f"尚未配置 {normalized or floor_code} 数字孪生平面"
        )
    return normalized


def _find_receipt(floor: dict[str, Any], operation_key: str, action: str) -> dict[str, Any] | None:
    for receipt in floor.get("layout_edit_receipts") or []:
        if receipt.get("operation_key") != operation_key:
            continue
        if receipt.get("action") != action:
            raise WarehouseTwinLayoutEditConflictError("该操作键已用于其他布局操作")
        return receipt
    return None


def _remember_receipt(
    floor: dict[str, Any], *, operation_key: str, action: str, result: dict[str, Any]
) -> None:
    receipts = list(floor.get("layout_edit_receipts") or [])
    receipts.append(
        {
            "operation_key": operation_key,
            "action": action,
            "result": result,
            "applied_at": _utc_iso(),
        }
    )
    floor["layout_edit_receipts"] = receipts[-100:]


def _apply_mutation(
    floor_code: str,
    *,
    expected_revision: str,
    operation_key: str,
    action: str,
    mutate: Callable[[dict[str, Any]], dict[str, Any]],
    path: Path | None = None,
) -> LayoutMutation:
    normalized = _normalize_floor_code(floor_code)
    normalized_key = str(operation_key or "").strip()
    if len(normalized_key) < 8 or len(normalized_key) > 120:
        raise WarehouseTwinLayoutEditError("布局操作键长度必须为 8 至 120 个字符")
    target = path or TWIN_LAYOUT_PATH
    with _LAYOUT_EDIT_LOCK:
        document = _read_document(target)
        floor = document["floors"].get(normalized)
        if not isinstance(floor, dict):
            raise WarehouseTwinLayoutEditNotFoundError(f"数字孪生平面缺少 {normalized}")
        receipt = _find_receipt(floor, normalized_key, action)
        if receipt is not None:
            return LayoutMutation(
                value=dict(receipt.get("result") or {}),
                floor_revision=str(floor.get("revision") or ""),
                applied=False,
            )
        current_revision = str(floor.get("revision") or "")
        if not expected_revision or expected_revision != current_revision:
            raise WarehouseTwinLayoutEditConflictError("布局已被其他操作更新，请刷新后重试")
        result = mutate(floor)
        _remember_receipt(
            floor,
            operation_key=normalized_key,
            action=action,
            result=result,
        )
        floor["layout_edited_at"] = _utc_iso()
        floor["revision"] = _floor_revision(floor)
        document["generated_at"] = floor["layout_edited_at"]
        _write_document(target, document)
        return LayoutMutation(
            value=result,
            floor_revision=str(floor["revision"]),
            applied=True,
        )


def _feature(floor: dict[str, Any], feature_id: str) -> dict[str, Any]:
    for feature in floor.get("features") or []:
        if feature.get("id") == feature_id:
            return feature
    raise WarehouseTwinLayoutEditNotFoundError("区域不存在或已被删除")


def _rack(floor: dict[str, Any], rack_id: str) -> dict[str, Any]:
    for rack in floor.get("racks") or []:
        if rack.get("id") == rack_id:
            return rack
    raise WarehouseTwinLayoutEditNotFoundError("货架不存在或已被删除")


def _ensure_version(entity: dict[str, Any], expected_version: int, label: str) -> None:
    if int(entity.get("version") or 1) != int(expected_version):
        raise WarehouseTwinLayoutEditConflictError(f"{label}已被其他操作更新，请刷新后重试")


def _validate_level_heights(levels: int, height_mm: float, values: list[float]) -> list[float]:
    heights = [float(value) for value in values]
    if len(heights) != max(0, levels - 1):
        raise WarehouseTwinLayoutEditError("层板高度数量必须等于层数减一")
    if heights and (heights != sorted(set(heights)) or heights[0] <= 0 or heights[-1] >= height_mm):
        raise WarehouseTwinLayoutEditError("层板高度必须从低到高、不能重复且小于货架总高度")
    return heights


def _validate_rack_values(values: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(values)
    for key, upper in (("width_mm", 200_000), ("depth_mm", 200_000), ("height_mm", 100_000)):
        value = float(normalized[key])
        if value <= 0 or value > upper:
            raise WarehouseTwinLayoutEditError("货架长宽高必须为有效毫米数值")
        normalized[key] = value
    for key in ("x_mm", "y_mm"):
        value = float(normalized[key])
        if abs(value) > 10_000_000:
            raise WarehouseTwinLayoutEditError("货架坐标超出允许范围")
        normalized[key] = value
    levels = int(normalized["levels"])
    if levels < 1 or levels > 20:
        raise WarehouseTwinLayoutEditError("货架层数必须为 1 至 20")
    normalized["levels"] = levels
    normalized["level_heights_mm"] = _validate_level_heights(
        levels, normalized["height_mm"], list(normalized.get("level_heights_mm") or [])
    )
    cargo_rows = int(normalized["cargo_rows"])
    if cargo_rows < 3 or cargo_rows > 5:
        raise WarehouseTwinLayoutEditError("每层货位数必须为 3 至 5")
    normalized["cargo_rows"] = cargo_rows
    bays = int(normalized.get("bays") or 1)
    if bays < 1 or bays > 50:
        raise WarehouseTwinLayoutEditError("结构格数必须为 1 至 50")
    normalized["bays"] = bays
    rotation = int(normalized.get("rotation_deg") or 0) % 360
    if rotation not in {0, 90, 180, 270}:
        raise WarehouseTwinLayoutEditError("货架只允许按 90 度旋转")
    normalized["rotation_deg"] = rotation
    access_side = str(normalized.get("access_side") or "south")
    if access_side not in ALLOWED_ACCESS_SIDES:
        raise WarehouseTwinLayoutEditError("货架操作面方向无效")
    normalized["access_side"] = access_side
    aisle = float(normalized.get("min_aisle_width_mm") or 0)
    if aisle < 0 or aisle > 20_000:
        raise WarehouseTwinLayoutEditError("最小通道宽度超出允许范围")
    normalized["min_aisle_width_mm"] = aisle
    name = str(normalized.get("name") or "").strip()
    if not name or len(name) > 160:
        raise WarehouseTwinLayoutEditError("货架名称不能为空且不能超过160个字符")
    normalized["name"] = name
    return normalized


def _feature_area_code(feature: dict[str, Any]) -> str:
    explicit = str(feature.get("erp_area_code") or "").strip().upper()
    if explicit:
        return explicit
    code = str(feature.get("feature_code") or "").strip().upper()
    marker = "ZONE-3F-ERP-"
    return code[len(marker):] if code.startswith(marker) else code.replace("ZONE-", "", 1)


def _next_rack_code(floor: dict[str, Any], area_code: str) -> str:
    prefix = f"RACK-{floor.get('floor_code')}-{area_code}-EDIT-"
    used = {str(item.get("rack_code") or "") for item in floor.get("racks") or []}
    sequence = 1
    while f"{prefix}{sequence:03d}" in used:
        sequence += 1
    return f"{prefix}{sequence:03d}"


def create_warehouse_twin_rack(
    floor_code: str,
    *,
    expected_revision: str,
    operation_key: str,
    area_feature_id: str,
    values: dict[str, Any],
    path: Path | None = None,
) -> LayoutMutation:
    normalized_values = _validate_rack_values(values)

    def mutate(floor: dict[str, Any]) -> dict[str, Any]:
        zone = _feature(floor, area_feature_id)
        if zone.get("feature_kind") != "zone":
            raise WarehouseTwinLayoutEditError("只能在仓储区域内新增货架")
        area_code = _feature_area_code(zone)
        rack = {
            "id": str(uuid4()),
            "layout_id": floor.get("layout_id"),
            "rack_code": _next_rack_code(floor, area_code),
            **normalized_values,
            "z_mm": 0.0,
            "color": str(normalized_values.get("color") or "#38bdf8"),
            "source": "manual",
            "status": "candidate",
            "is_locked": False,
            "version": 1,
            "area_feature_id": area_feature_id,
            "area_code": area_code,
        }
        floor.setdefault("racks", []).append(rack)
        return rack

    return _apply_mutation(
        floor_code,
        expected_revision=expected_revision,
        operation_key=operation_key,
        action="rack.create",
        mutate=mutate,
        path=path,
    )


def update_warehouse_twin_rack(
    floor_code: str,
    rack_id: str,
    *,
    expected_revision: str,
    expected_version: int,
    operation_key: str,
    values: dict[str, Any],
    path: Path | None = None,
) -> LayoutMutation:
    normalized_values = _validate_rack_values(values)

    def mutate(floor: dict[str, Any]) -> dict[str, Any]:
        rack = _rack(floor, rack_id)
        _ensure_version(rack, expected_version, "货架")
        if rack.get("is_locked"):
            raise WarehouseTwinLayoutEditConflictError("货架已确认并锁定，必须先解除锁定")
        for key, value in normalized_values.items():
            rack[key] = value
        rack["status"] = "candidate"
        rack["version"] = int(rack.get("version") or 1) + 1
        return dict(rack)

    return _apply_mutation(
        floor_code,
        expected_revision=expected_revision,
        operation_key=operation_key,
        action="rack.update",
        mutate=mutate,
        path=path,
    )


def delete_warehouse_twin_rack(
    floor_code: str,
    rack_id: str,
    *,
    expected_revision: str,
    expected_version: int,
    operation_key: str,
    path: Path | None = None,
) -> LayoutMutation:
    def mutate(floor: dict[str, Any]) -> dict[str, Any]:
        rack = _rack(floor, rack_id)
        _ensure_version(rack, expected_version, "货架")
        if rack.get("is_locked"):
            raise WarehouseTwinLayoutEditConflictError("货架已确认并锁定，必须先解除锁定")
        floor["racks"] = [item for item in floor.get("racks") or [] if item.get("id") != rack_id]
        retired = dict(rack)
        retired["retired_at"] = _utc_iso()
        retired["retired_reason"] = "管理员在二维库位布局中删除；正式库存与库位未改变"
        floor.setdefault("retired_racks", []).append(retired)
        return {
            "id": rack_id,
            "rack_code": rack.get("rack_code"),
            "area_code": rack.get("area_code"),
            "deleted": True,
            "inventory_changed": False,
        }

    return _apply_mutation(
        floor_code,
        expected_revision=expected_revision,
        operation_key=operation_key,
        action="rack.delete",
        mutate=mutate,
        path=path,
    )


def update_warehouse_twin_zone_policy(
    floor_code: str,
    feature_id: str,
    *,
    expected_revision: str,
    expected_version: int,
    operation_key: str,
    allowed_inventory_types: list[str],
    storage_layout: str,
    path: Path | None = None,
) -> LayoutMutation:
    normalized_types = list(dict.fromkeys(str(value).strip() for value in allowed_inventory_types))
    if not normalized_types or any(value not in ALLOWED_INVENTORY_TYPES for value in normalized_types):
        raise WarehouseTwinLayoutEditError("区域至少选择一种有效存放类型")
    if storage_layout not in ALLOWED_STORAGE_LAYOUTS:
        raise WarehouseTwinLayoutEditError("区域展示形式必须是货架、栈板地堆或混合")

    def mutate(floor: dict[str, Any]) -> dict[str, Any]:
        feature = _feature(floor, feature_id)
        if feature.get("feature_kind") != "zone":
            raise WarehouseTwinLayoutEditError("只有仓储区域可以设置存放策略")
        _ensure_version(feature, expected_version, "区域")
        feature["allowed_inventory_types"] = normalized_types
        feature["storage_layout"] = storage_layout
        feature["version"] = int(feature.get("version") or 1) + 1
        return dict(feature)

    return _apply_mutation(
        floor_code,
        expected_revision=expected_revision,
        operation_key=operation_key,
        action="zone.policy.update",
        mutate=mutate,
        path=path,
    )
