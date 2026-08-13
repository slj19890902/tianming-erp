from __future__ import annotations

import json
import math
import os
import shutil
import tempfile
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from threading import RLock
from typing import Any, Callable
from uuid import uuid4

from app.services.warehouse_twin_layout import (
    TWIN_LAYOUT_PATH as TWIN_LAYOUT_BASELINE_PATH,
    TWIN_LAYOUT_RUNTIME_PATH as DEFAULT_TWIN_LAYOUT_RUNTIME_PATH,
    keep_measured_floor_features,
)


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
_LAYOUT_EDIT_LOCK = RLock()
WAREHOUSE_TWIN_LAYOUT_TRANSACTION_LOCK = _LAYOUT_EDIT_LOCK
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
TWIN_LAYOUT_PATH = DEFAULT_TWIN_LAYOUT_RUNTIME_PATH
TWIN_LAYOUT_DRAFT_PATH = Path(
    os.getenv(
        "ERP_TWIN_LAYOUT_DRAFT_PATH",
        str(_PROJECT_ROOT / "data" / "layout_drafts" / "twin_layout_v1.draft.json"),
    )
).resolve(strict=False)
TWIN_LAYOUT_BACKUP_DIR = Path(
    os.getenv(
        "ERP_TWIN_LAYOUT_BACKUP_DIR",
        str(_PROJECT_ROOT / "data" / "layout_backups"),
    )
).resolve(strict=False)


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


@dataclass(frozen=True)
class LayoutDraftAction:
    value: dict[str, Any]
    applied: bool


@dataclass(frozen=True)
class LayoutDraftSnapshot:
    existed: bool
    content: bytes | None


@dataclass(frozen=True)
class LayoutPublishSnapshot:
    published_target: Path
    published_existed: bool
    published_content: bytes | None
    draft_target: Path
    draft_existed: bool
    draft_content: bytes | None


@dataclass(frozen=True)
class _PublishedLayoutPaths:
    source: Path
    target: Path


def _published_layout_paths(explicit_path: Path | None = None) -> _PublishedLayoutPaths:
    if explicit_path is not None:
        return _PublishedLayoutPaths(source=explicit_path, target=explicit_path)
    runtime_target = TWIN_LAYOUT_PATH
    if runtime_target.exists():
        return _PublishedLayoutPaths(source=runtime_target, target=runtime_target)
    if os.getenv("ERP_UAT_ROOT"):
        raise WarehouseTwinLayoutEditNotFoundError(
            "隔离 UAT 运行态地图不存在，拒绝回退共享代码树基线"
        )
    return _PublishedLayoutPaths(source=TWIN_LAYOUT_BASELINE_PATH, target=runtime_target)


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


def snapshot_warehouse_twin_layout_draft(
    *, draft_path: Path | None = None,
) -> LayoutDraftSnapshot:
    target = draft_path or TWIN_LAYOUT_DRAFT_PATH
    with _LAYOUT_EDIT_LOCK:
        if not target.is_file():
            return LayoutDraftSnapshot(existed=False, content=None)
        return LayoutDraftSnapshot(existed=True, content=target.read_bytes())


def restore_warehouse_twin_layout_draft(
    snapshot: LayoutDraftSnapshot,
    *, draft_path: Path | None = None,
) -> None:
    target = draft_path or TWIN_LAYOUT_DRAFT_PATH
    with _LAYOUT_EDIT_LOCK:
        if not snapshot.existed:
            target.unlink(missing_ok=True)
            return
        if snapshot.content is None:
            raise WarehouseTwinLayoutEditError('地图草稿快照内容缺失')
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f'.{target.name}.', suffix='.tmp', dir=target.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, 'wb') as handle:
                handle.write(snapshot.content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)


def snapshot_warehouse_twin_publish_state(
    *,
    published_path: Path | None = None,
    draft_path: Path | None = None,
) -> LayoutPublishSnapshot:
    published_target = _published_layout_paths(published_path).target
    draft_target = draft_path or TWIN_LAYOUT_DRAFT_PATH
    with _LAYOUT_EDIT_LOCK:
        return LayoutPublishSnapshot(
            published_target=published_target,
            published_existed=published_target.is_file(),
            published_content=(published_target.read_bytes() if published_target.is_file() else None),
            draft_target=draft_target,
            draft_existed=draft_target.is_file(),
            draft_content=(draft_target.read_bytes() if draft_target.is_file() else None),
        )


def _restore_file_bytes_unlocked(path: Path, *, existed: bool, content: bytes | None) -> None:
    if not existed:
        path.unlink(missing_ok=True)
        return
    if content is None:
        raise WarehouseTwinLayoutEditError('地图文件快照内容缺失')
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f'.{path.name}.', suffix='.tmp', dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, 'wb') as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def restore_warehouse_twin_publish_state(
    snapshot: LayoutPublishSnapshot,
    *,
    backup_name: str | None = None,
    backup_dir: Path | None = None,
) -> None:
    with _LAYOUT_EDIT_LOCK:
        _restore_file_bytes_unlocked(
            snapshot.published_target,
            existed=snapshot.published_existed,
            content=snapshot.published_content,
        )
        _restore_file_bytes_unlocked(
            snapshot.draft_target,
            existed=snapshot.draft_existed,
            content=snapshot.draft_content,
        )
        normalized_backup_name = Path(str(backup_name or '')).name
        if normalized_backup_name and normalized_backup_name == str(backup_name):
            (backup_dir or TWIN_LAYOUT_BACKUP_DIR).joinpath(normalized_backup_name).unlink(
                missing_ok=True
            )


def _path_sha256(path: Path) -> str:
    if not path.is_file():
        raise WarehouseTwinLayoutEditNotFoundError("正式仓库地图尚未生成")
    return sha256(path.read_bytes()).hexdigest()


def _new_draft_document(published_path: Path) -> dict[str, Any]:
    published = _read_document(published_path)
    now = _utc_iso()
    draft = deepcopy(published)
    draft["draft_meta"] = {
        "status": "draft",
        "created_at": now,
        "updated_at": now,
        "base_published_sha256": _path_sha256(published_path),
        "base_floor_revisions": {
            code: str(floor.get("revision") or "")
            for code, floor in published["floors"].items()
            if isinstance(floor, dict)
        },
    }
    return draft


def _active_draft_document_unlocked(
    *,
    published_path: Path,
    draft_path: Path,
    create: bool,
) -> dict[str, Any] | None:
    if draft_path.is_file():
        draft = _read_document(draft_path)
        meta = draft.get("draft_meta")
        if isinstance(meta, dict) and meta.get("status") in {"draft", "validated"}:
            if str(meta.get("base_published_sha256") or "") != _path_sha256(published_path):
                raise WarehouseTwinLayoutEditConflictError(
                    "正式地图已更新，当前草稿已过期；请放弃旧草稿后重新编辑"
                )
            return draft
    if not create:
        return None
    return _new_draft_document(published_path)


def _mark_draft_changed(document: dict[str, Any]) -> None:
    meta = document.get("draft_meta")
    if not isinstance(meta, dict):
        return
    meta["status"] = "draft"
    meta["updated_at"] = _utc_iso()
    for key in (
        "validated_at",
        "validated_floor_revisions",
        "validation_blockers",
        "validation_warnings",
    ):
        meta.pop(key, None)


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
    with _LAYOUT_EDIT_LOCK:
        if path is None:
            target = TWIN_LAYOUT_DRAFT_PATH
            published_paths = _published_layout_paths()
            document = _active_draft_document_unlocked(
                published_path=published_paths.source,
                draft_path=target,
                create=True,
            )
            assert document is not None
        else:
            target = path
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
        if path is None:
            meta = document.get("draft_meta") or {}
            base_floor_revisions = meta.get("base_floor_revisions") or {}
            changed_other_floors = [
                code
                for code, other_floor in (document.get("floors") or {}).items()
                if code != normalized
                and isinstance(other_floor, dict)
                and str(other_floor.get("revision") or "")
                != str(base_floor_revisions.get(code) or "")
            ]
            if changed_other_floors:
                raise WarehouseTwinLayoutEditConflictError(
                    "同一地图草稿只能规划一个楼层；请先发布或放弃其他楼层草稿"
                )
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
        if path is None:
            _mark_draft_changed(document)
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
    raw_level_cell_counts = normalized.get("level_cell_counts")
    if raw_level_cell_counts is not None:
        level_cell_counts = [int(value) for value in raw_level_cell_counts]
        if len(level_cell_counts) != levels:
            raise WarehouseTwinLayoutEditError("每层分格数量必须与货架层数一致")
        if any(value < 0 or value > 50 for value in level_cell_counts):
            raise WarehouseTwinLayoutEditError("每层分格数量必须为 0 至 50；0 表示尚未分格")
        normalized["level_cell_counts"] = level_cell_counts
        normalized["cell_plan_status"] = (
            "configured" if any(level_cell_counts) else "pending_admin_configuration"
        )
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
    floor_code = str(feature.get("floor_code") or "").strip().upper()
    for marker in (
        f"ZONE-{floor_code}-ERP-" if floor_code else "",
        f"ZONE-{floor_code}-" if floor_code else "",
        "ZONE-",
    ):
        if marker and code.startswith(marker):
            return code[len(marker):]
    return code


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
    erp_area_code: str | None = None,
    area_name: str | None = None,
    formal_area_id: int | None = None,
    formal_floor_id: int | None = None,
    path: Path | None = None,
) -> LayoutMutation:
    normalized_types = list(dict.fromkeys(str(value).strip() for value in allowed_inventory_types))
    if not normalized_types or any(value not in ALLOWED_INVENTORY_TYPES for value in normalized_types):
        raise WarehouseTwinLayoutEditError("区域至少选择一种有效存放类型")
    if storage_layout not in ALLOWED_STORAGE_LAYOUTS:
        raise WarehouseTwinLayoutEditError("区域展示形式必须是货架、栈板地堆或混合")
    normalized_area_code = str(erp_area_code or "").strip().upper() or None
    normalized_area_name = str(area_name or "").strip() or None
    if (formal_area_id is None) != (formal_floor_id is None):
        raise WarehouseTwinLayoutEditError("正式区域身份必须同时包含区域 ID 和楼层 ID")
    if formal_area_id is not None and (formal_area_id <= 0 or formal_floor_id <= 0):
        raise WarehouseTwinLayoutEditError("正式区域身份无效")
    if normalized_area_code is not None and len(normalized_area_code) > 30:
        raise WarehouseTwinLayoutEditError("正式区域编号最多 30 个字符")

    def mutate(floor: dict[str, Any]) -> dict[str, Any]:
        feature = _feature(floor, feature_id)
        if feature.get("feature_kind") != "zone":
            raise WarehouseTwinLayoutEditError("只有仓储区域可以设置存放策略")
        _ensure_version(feature, expected_version, "区域")
        feature["allowed_inventory_types"] = normalized_types
        feature["storage_layout"] = storage_layout
        if normalized_area_code is not None:
            for other in floor.get("features") or []:
                if (
                    other is not feature
                    and str(other.get("erp_area_code") or "").strip().upper()
                    == normalized_area_code
                ):
                    raise WarehouseTwinLayoutEditError(
                        f"正式区域编号 {normalized_area_code} 已绑定其他地图区域"
                    )
            feature["erp_area_code"] = normalized_area_code
            floor["erp_area_codes"] = sorted(
                {
                    str(item.get("erp_area_code") or "").strip().upper()
                    for item in floor.get("features") or []
                    if str(item.get("erp_area_code") or "").strip()
                }
            )
        if normalized_area_name is not None:
            feature["formal_area_name"] = normalized_area_name
        if formal_area_id is not None:
            feature["formal_area_id"] = int(formal_area_id)
            feature["formal_floor_id"] = int(formal_floor_id)
        else:
            feature.pop("formal_area_id", None)
            feature.pop("formal_floor_id", None)
        feature["version"] = int(feature.get("version") or 1) + 1
        return dict(feature)

    mutation = _apply_mutation(
        floor_code,
        expected_revision=expected_revision,
        operation_key=operation_key,
        action="zone.policy.update",
        mutate=mutate,
        path=path,
    )
    if not mutation.applied:
        same_types = set(mutation.value.get('allowed_inventory_types') or []) == set(normalized_types)
        same_layout = mutation.value.get('storage_layout') == storage_layout
        same_area = normalized_area_code is None or mutation.value.get('erp_area_code') == normalized_area_code
        same_name = normalized_area_name is None or mutation.value.get('formal_area_name') == normalized_area_name
        same_identity = (
            mutation.value.get('formal_area_id') == formal_area_id
            and mutation.value.get('formal_floor_id') == formal_floor_id
        )
        if not (same_types and same_layout and same_area and same_name and same_identity):
            raise WarehouseTwinLayoutEditConflictError('该操作键已用于不同的区域策略')
    return mutation


def _normalize_zone_points(points: list[list[float]]) -> list[list[float]]:
    if len(points) < 3 or len(points) > 64:
        raise WarehouseTwinLayoutEditError('区域边界点数量无效')
    normalized: list[list[float]] = []
    for point in points:
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            raise WarehouseTwinLayoutEditError('区域边界点必须是二维坐标')
        try:
            x_mm, y_mm = float(point[0]), float(point[1])
        except (TypeError, ValueError, OverflowError) as error:
            raise WarehouseTwinLayoutEditError('区域边界坐标必须是数值') from error
        if not math.isfinite(x_mm) or not math.isfinite(y_mm):
            raise WarehouseTwinLayoutEditError('区域边界坐标必须是有限数值')
        if abs(x_mm) > 10_000_000 or abs(y_mm) > 10_000_000:
            raise WarehouseTwinLayoutEditError('区域边界坐标超出允许范围')
        normalized.append([round(x_mm, 3), round(y_mm, 3)])
    if len({tuple(point) for point in normalized}) != len(normalized):
        raise WarehouseTwinLayoutEditError('区域边界点不能重复')
    area_mm2 = abs(sum(
        normalized[index][0] * normalized[(index + 1) % len(normalized)][1]
        - normalized[(index + 1) % len(normalized)][0] * normalized[index][1]
        for index in range(len(normalized))
    )) / 2
    if area_mm2 <= 0:
        raise WarehouseTwinLayoutEditError('区域边界必须形成有效面积')
    return normalized


def _zone_area_mm2(points: list[list[float]]) -> float:
    return round(abs(sum(
        points[index][0] * points[(index + 1) % len(points)][1]
        - points[(index + 1) % len(points)][0] * points[index][1]
        for index in range(len(points))
    )) / 2, 3)


def _segments_intersect(a: list[float], b: list[float], c: list[float], d: list[float]) -> bool:
    def cross(p: list[float], q: list[float], r: list[float]) -> float:
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])
    def on_segment(p: list[float], q: list[float], r: list[float]) -> bool:
        return min(p[0], r[0]) <= q[0] <= max(p[0], r[0]) and min(p[1], r[1]) <= q[1] <= max(p[1], r[1])
    values = (cross(a, b, c), cross(a, b, d), cross(c, d, a), cross(c, d, b))
    if values[0] * values[1] < 0 and values[2] * values[3] < 0:
        return True
    return ((values[0] == 0 and on_segment(a, c, b))
            or (values[1] == 0 and on_segment(a, d, b))
            or (values[2] == 0 and on_segment(c, a, d))
            or (values[3] == 0 and on_segment(c, b, d)))


def _reject_self_intersection(points: list[list[float]]) -> None:
    count = len(points)
    for left in range(count):
        for right in range(left + 1, count):
            if right in {left, left + 1} or (left == 0 and right == count - 1):
                continue
            if _segments_intersect(points[left], points[(left + 1) % count], points[right], points[(right + 1) % count]):
                raise WarehouseTwinLayoutEditError('区域边界不能自相交')


def update_warehouse_twin_zone_geometry(
    floor_code: str,
    feature_id: str,
    *,
    expected_revision: str,
    expected_version: int,
    operation_key: str,
    points: list[list[float]],
    path: Path | None = None,
) -> LayoutMutation:
    normalized_points = _normalize_zone_points(points)
    _reject_self_intersection(normalized_points)

    def mutate(floor: dict[str, Any]) -> dict[str, Any]:
        feature = _feature(floor, feature_id)
        if feature.get('feature_kind') != 'zone':
            raise WarehouseTwinLayoutEditError('只有仓储区域可以修改实测边界')
        _ensure_version(feature, expected_version, '区域')
        if feature.get('is_locked'):
            raise WarehouseTwinLayoutEditConflictError('区域已确认并锁定，必须先解除锁定')
        bounds = floor.get('bounds_mm') or {}
        if not all(key in bounds for key in ('min_x', 'min_y', 'max_x', 'max_y')):
            raise WarehouseTwinLayoutEditError('楼层实测边界不完整，不能修改区域')
        if any(
            point[0] < float(bounds.get('min_x', point[0]))
            or point[0] > float(bounds.get('max_x', point[0]))
            or point[1] < float(bounds.get('min_y', point[1]))
            or point[1] > float(bounds.get('max_y', point[1]))
            for point in normalized_points
        ):
            raise WarehouseTwinLayoutEditError('区域边界不能超出本楼层实测地图范围')
        feature['points'] = normalized_points
        feature['area_mm2'] = _zone_area_mm2(normalized_points)
        feature['status'] = 'candidate'
        feature['version'] = int(feature.get('version') or 1) + 1
        return dict(feature)

    mutation = _apply_mutation(
        floor_code,
        expected_revision=expected_revision,
        operation_key=operation_key,
        action='zone.geometry.update',
        mutate=mutate,
        path=path,
    )
    if not mutation.applied and mutation.value.get('points') != normalized_points:
        raise WarehouseTwinLayoutEditConflictError('该操作键已用于不同的区域边界')
    return mutation


def _duplicate_values(items: list[dict[str, Any]], key: str) -> list[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for item in items:
        value = str(item.get(key) or "").strip()
        if not value:
            continue
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    return sorted(duplicates)


def _validate_document_for_publish(document: dict[str, Any]) -> tuple[list[str], list[str]]:
    blockers: list[str] = []
    warnings: list[str] = []
    floors = document.get("floors")
    if not isinstance(floors, dict) or not floors:
        return ["地图没有任何楼层"], warnings

    for code, floor in floors.items():
        if not isinstance(floor, dict):
            blockers.append(f"{code} 楼层数据格式错误")
            continue
        if str(floor.get("floor_code") or "").upper() != str(code).upper():
            blockers.append(f"{code} 的楼层编号与内容不一致")
        if str(floor.get("revision") or "") != _floor_revision(floor):
            blockers.append(f"{code} 的布局修订号校验失败")

        features = floor.get("features") or []
        racks = floor.get("racks") or []
        if not isinstance(features, list) or not all(isinstance(item, dict) for item in features):
            blockers.append(f"{code} 的区域列表格式错误")
            features = []
        if not isinstance(racks, list) or not all(isinstance(item, dict) for item in racks):
            blockers.append(f"{code} 的货架列表格式错误")
            racks = []

        for value in _duplicate_values(features, "id"):
            blockers.append(f"{code} 存在重复区域标识：{value}")
        for value in _duplicate_values(features, "feature_code"):
            blockers.append(f"{code} 存在重复区域编号：{value}")
        for value in _duplicate_values(racks, "id"):
            blockers.append(f"{code} 存在重复货架标识：{value}")
        for value in _duplicate_values(racks, "rack_code"):
            blockers.append(f"{code} 存在重复货架编号：{value}")

        zones = {
            str(item.get("id")): item
            for item in features
            if item.get("feature_kind") == "zone" and item.get("id")
        }
        for feature in zones.values():
            allowed = feature.get("allowed_inventory_types")
            if allowed is not None and (
                not isinstance(allowed, list)
                or not allowed
                or any(item not in ALLOWED_INVENTORY_TYPES for item in allowed)
            ):
                blockers.append(f"{code} 区域 {feature.get('name') or feature.get('id')} 的允许存放类型无效")
            storage_layout = feature.get("storage_layout")
            if storage_layout is not None and storage_layout not in ALLOWED_STORAGE_LAYOUTS:
                blockers.append(f"{code} 区域 {feature.get('name') or feature.get('id')} 的存储形式无效")

        for value in _duplicate_values(list(zones.values()), "erp_area_code"):
            blockers.append(f"{code} 存在重复正式区域绑定：{value}")

        for rack in racks:
            rack_label = str(rack.get("rack_code") or rack.get("name") or rack.get("id") or "未编号货架")
            if not rack.get("id") or not rack.get("rack_code"):
                blockers.append(f"{code} 存在缺少稳定标识的货架")
                continue
            try:
                _validate_rack_values(rack)
            except (KeyError, TypeError, ValueError, WarehouseTwinLayoutEditError) as error:
                blockers.append(f"{code} 货架 {rack_label} 参数无效：{error}")
            area_feature_id = str(rack.get("area_feature_id") or "").strip()
            if area_feature_id and area_feature_id not in zones:
                blockers.append(f"{code} 货架 {rack_label} 引用了不存在的仓储区域")
            if not area_feature_id:
                warnings.append(f"{code} 货架 {rack_label} 尚未绑定区域对象")
            counts = rack.get("level_cell_counts")
            if isinstance(counts, list) and counts and not any(int(value) for value in counts):
                warnings.append(f"{code} 货架 {rack_label} 尚未分格")

    return blockers, warnings


def load_warehouse_twin_layout_draft(
    floor_code: str,
    *,
    published_path: Path | None = None,
    draft_path: Path | None = None,
) -> dict[str, Any]:
    normalized = _normalize_floor_code(floor_code)
    published_target = _published_layout_paths(published_path).source
    draft_target = draft_path or TWIN_LAYOUT_DRAFT_PATH
    with _LAYOUT_EDIT_LOCK:
        published = _read_document(published_target)
        published_floor = published["floors"].get(normalized)
        if not isinstance(published_floor, dict):
            raise WarehouseTwinLayoutEditNotFoundError(f"数字孪生平面缺少 {normalized}")
        draft = _active_draft_document_unlocked(
            published_path=published_target,
            draft_path=draft_target,
            create=False,
        )
        floor = published_floor
        meta: dict[str, Any] = {}
        has_draft = draft is not None
        if draft is not None:
            candidate = draft["floors"].get(normalized)
            if not isinstance(candidate, dict):
                raise WarehouseTwinLayoutEditError(f"布局草稿缺少 {normalized}")
            floor = candidate
            meta = dict(draft.get("draft_meta") or {})
        visible_floor = keep_measured_floor_features(deepcopy(floor))
        return {
            **visible_floor,
            "generated_at": (draft if draft is not None else published).get("generated_at"),
            "projection_notice": "当前为管理员布局草稿；正式库存数量仍以 ERP 库存账为准。",
            "draft_control": {
                "has_draft": has_draft,
                "status": str(meta.get("status") or "none") if has_draft else "none",
                "published_revision": str(published_floor.get("revision") or ""),
                "draft_revision": str(floor.get("revision") or "") if has_draft else None,
                "base_published_sha256": meta.get("base_published_sha256"),
                "created_at": meta.get("created_at"),
                "updated_at": meta.get("updated_at"),
                "validated_at": meta.get("validated_at"),
                "blockers": list(meta.get("validation_blockers") or []),
                "warnings": list(meta.get("validation_warnings") or []),
            },
        }


def load_effective_warehouse_twin_floor_for_edit(
    floor_code: str,
    *,
    published_path: Path | None = None,
    draft_path: Path | None = None,
) -> dict[str, Any]:
    return load_warehouse_twin_layout_draft(
        floor_code,
        published_path=published_path,
        draft_path=draft_path,
    )


def validate_warehouse_twin_layout_draft(
    floor_code: str,
    *,
    expected_revision: str,
    published_path: Path | None = None,
    draft_path: Path | None = None,
) -> LayoutDraftAction:
    normalized = _normalize_floor_code(floor_code)
    published_target = _published_layout_paths(published_path).source
    draft_target = draft_path or TWIN_LAYOUT_DRAFT_PATH
    with _LAYOUT_EDIT_LOCK:
        draft = _active_draft_document_unlocked(
            published_path=published_target,
            draft_path=draft_target,
            create=False,
        )
        if draft is None:
            raise WarehouseTwinLayoutEditNotFoundError("当前没有可校验的布局草稿")
        floor = draft["floors"].get(normalized)
        if not isinstance(floor, dict):
            raise WarehouseTwinLayoutEditNotFoundError(f"布局草稿缺少 {normalized}")
        if str(floor.get("revision") or "") != str(expected_revision or ""):
            raise WarehouseTwinLayoutEditConflictError("布局草稿已更新，请刷新后重新校验")
        blockers, warnings = _validate_document_for_publish(draft)
        meta = draft["draft_meta"]
        now = _utc_iso()
        meta["updated_at"] = now
        meta["validation_blockers"] = blockers
        meta["validation_warnings"] = warnings
        if blockers:
            meta["status"] = "draft"
            meta.pop("validated_at", None)
            meta.pop("validated_floor_revisions", None)
        else:
            meta["status"] = "validated"
            meta["validated_at"] = now
            meta["validated_floor_revisions"] = {
                code: str(item.get("revision") or "")
                for code, item in draft["floors"].items()
                if isinstance(item, dict)
            }
        _write_document(draft_target, draft)
        return LayoutDraftAction(
            value={
                "status": meta["status"],
                "floor_code": normalized,
                "draft_revision": str(floor.get("revision") or ""),
                "blockers": blockers,
                "warnings": warnings,
                "validated_at": meta.get("validated_at"),
                "inventory_changed": False,
            },
            applied=True,
        )


def publish_warehouse_twin_layout_draft(
    floor_code: str,
    *,
    expected_published_revision: str,
    expected_draft_revision: str,
    operation_key: str,
    published_path: Path | None = None,
    draft_path: Path | None = None,
    backup_dir: Path | None = None,
) -> LayoutDraftAction:
    normalized = _normalize_floor_code(floor_code)
    normalized_key = str(operation_key or "").strip()
    if len(normalized_key) < 8 or len(normalized_key) > 120:
        raise WarehouseTwinLayoutEditError("发布操作键长度必须为 8 至 120 个字符")
    published_paths = _published_layout_paths(published_path)
    published_source = published_paths.source
    published_target = published_paths.target
    draft_target = draft_path or TWIN_LAYOUT_DRAFT_PATH
    backup_target = backup_dir or TWIN_LAYOUT_BACKUP_DIR
    with _LAYOUT_EDIT_LOCK:
        published = _read_document(published_source)
        if draft_target.is_file():
            replay_document = _read_document(draft_target)
            replay_meta = replay_document.get("draft_meta") or {}
            receipt = replay_meta.get("last_publish") or {}
            if replay_meta.get("status") == "published" and receipt.get("operation_key") == normalized_key:
                return LayoutDraftAction(value=dict(receipt.get("result") or {}), applied=False)

        draft = _active_draft_document_unlocked(
            published_path=published_source,
            draft_path=draft_target,
            create=False,
        )
        if draft is None:
            raise WarehouseTwinLayoutEditNotFoundError("当前没有可发布的布局草稿")
        published_floor = published["floors"].get(normalized)
        draft_floor = draft["floors"].get(normalized)
        if not isinstance(published_floor, dict) or not isinstance(draft_floor, dict):
            raise WarehouseTwinLayoutEditNotFoundError(f"地图缺少 {normalized}")
        if str(published_floor.get("revision") or "") != str(expected_published_revision or ""):
            raise WarehouseTwinLayoutEditConflictError("正式地图已更新，请刷新草稿后重新处理")
        if str(draft_floor.get("revision") or "") != str(expected_draft_revision or ""):
            raise WarehouseTwinLayoutEditConflictError("布局草稿已更新，请重新校验后发布")
        meta = draft["draft_meta"]
        base_floor_revisions = meta.get("base_floor_revisions") or {}
        dirty_floors = [
            code
            for code, item in draft["floors"].items()
            if isinstance(item, dict)
            and str(item.get("revision") or "")
            != str(base_floor_revisions.get(code) or "")
        ]
        if dirty_floors != [normalized]:
            raise WarehouseTwinLayoutEditConflictError(
                "每次只能发布一个楼层的地图草稿；请刷新并重新核对草稿范围"
            )
        validated_revisions = meta.get("validated_floor_revisions") or {}
        current_revisions = {
            code: str(item.get("revision") or "")
            for code, item in draft["floors"].items()
            if isinstance(item, dict)
        }
        if (
            meta.get("status") != "validated"
            or validated_revisions.get(normalized) != expected_draft_revision
            or validated_revisions != current_revisions
        ):
            raise WarehouseTwinLayoutEditConflictError("请先校验当前布局草稿，再执行发布")
        blockers, warnings = _validate_document_for_publish(draft)
        if blockers:
            raise WarehouseTwinLayoutEditError("布局草稿校验未通过：" + "；".join(blockers[:5]))

        backup_target.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
        old_sha256 = _path_sha256(published_source)
        backup_path = backup_target / f"{published_source.stem}.before_{stamp}_{old_sha256[:12]}.json"
        shutil.copy2(published_source, backup_path)
        if _path_sha256(backup_path) != old_sha256:
            backup_path.unlink(missing_ok=True)
            raise WarehouseTwinLayoutEditError("正式地图备份校验失败，已停止发布")

        candidate = deepcopy(draft)
        candidate.pop("draft_meta", None)
        draft_before_publish = draft_target.read_bytes()
        try:
            _write_document(published_target, candidate)
            written = _read_document(published_target)
            written_blockers, _written_warnings = _validate_document_for_publish(written)
            if written != candidate or written_blockers:
                raise WarehouseTwinLayoutEditError("运行地图发布后内容校验失败")
            if published_source != published_target and _path_sha256(published_source) != old_sha256:
                raise WarehouseTwinLayoutEditError("静态地图基线发生变化，已停止发布")
            published_sha256 = _path_sha256(published_target)
            result = {
                "status": "published",
                "floor_code": normalized,
                "published_revision": str(draft_floor.get("revision") or ""),
                "published_sha256": published_sha256,
                "backup_name": backup_path.name,
                "backup_sha256": old_sha256,
                "published_storage": "runtime",
                "warnings": warnings,
                "inventory_changed": False,
                "published_at": _utc_iso(),
            }
            meta["status"] = "published"
            meta["published_at"] = result["published_at"]
            meta["last_publish"] = {"operation_key": normalized_key, "result": result}
            _write_document(draft_target, draft)
        except Exception as error:
            try:
                if published_source == published_target:
                    shutil.copy2(backup_path, published_target)
                    if _path_sha256(published_target) != old_sha256:
                        raise WarehouseTwinLayoutEditError("运行地图发布失败且备份恢复校验失败")
                else:
                    published_target.unlink(missing_ok=True)
                _restore_file_bytes_unlocked(
                    draft_target,
                    existed=True,
                    content=draft_before_publish,
                )
                backup_path.unlink(missing_ok=True)
            except Exception as restore_error:
                raise WarehouseTwinLayoutEditError(
                    "运行地图发布失败且自动恢复失败，请停止编辑并人工恢复备份"
                ) from restore_error
            if isinstance(error, WarehouseTwinLayoutEditError):
                raise
            raise WarehouseTwinLayoutEditError("运行地图发布后校验失败，已恢复发布前版本") from error
        return LayoutDraftAction(value=result, applied=True)


def discard_warehouse_twin_layout_draft(
    floor_code: str,
    *,
    expected_revision: str,
    draft_path: Path | None = None,
) -> LayoutDraftAction:
    normalized = _normalize_floor_code(floor_code)
    target = draft_path or TWIN_LAYOUT_DRAFT_PATH
    with _LAYOUT_EDIT_LOCK:
        if not target.is_file():
            return LayoutDraftAction(
                value={"status": "none", "floor_code": normalized, "inventory_changed": False},
                applied=False,
            )
        draft = _read_document(target)
        meta = draft.get("draft_meta") or {}
        if meta.get("status") not in {"draft", "validated"}:
            return LayoutDraftAction(
                value={"status": "none", "floor_code": normalized, "inventory_changed": False},
                applied=False,
            )
        floor = draft["floors"].get(normalized)
        if not isinstance(floor, dict):
            raise WarehouseTwinLayoutEditNotFoundError(f"布局草稿缺少 {normalized}")
        if str(floor.get("revision") or "") != str(expected_revision or ""):
            raise WarehouseTwinLayoutEditConflictError("布局草稿已更新，请刷新后再放弃")
        target.unlink()
        return LayoutDraftAction(
            value={"status": "discarded", "floor_code": normalized, "inventory_changed": False},
            applied=True,
        )
