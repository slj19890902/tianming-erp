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
from app.services.warehouse_area_tombstones import (
    retire_archived_area_layout_objects,
)


ALLOWED_INVENTORY_TYPES = {
    "finished",
    "semi_finished",
    "raw_material",
    "mold",
    "print_plate",
    "temporary_turnover",
}
ALLOWED_STORAGE_LAYOUTS = {"rack", "pallet_ground", "mixed", "functional"}
ALLOWED_ACCESS_SIDES = {"north", "south", "east", "west", "both"}
FLOOR4_CALIBRATION_MAX_RESIDUAL_MM = 50.0
FLOOR4_CALIBRATION_RMSE_MM = 30.0
FLOOR4_DOORWAY_MIN_EDGE_MM = 300.0
FLOOR4_DOORWAY_MAX_EDGE_MM = 20_000.0
FLOOR4_CANONICAL_FREIGHT_ELEVATOR_ID = "LIFT-002-4F-CANONICAL"
FLOOR4_SPATIAL_BOUNDS_POLYGON_KEY = "spatial_bounds_polygon_mm"
FLOOR4_STALE_CALIBRATION_NAME_MARKERS = (
    "（待现场三点标定）",
    "(待现场三点标定)",
)
FLOOR4_STALE_CALIBRATION_WARNING_MARKERS = (
    "待现场三点标定",
    "完成现场三点标定前不得作为正式位置或库存地图",
    "本资产未生成四楼货梯几何",
    "未生成货梯",
)
FLOOR4_STALE_EMPTY_WAREHOUSE_WARNING_MARKERS = (
    "本资产未生成区域、货架、栈板、库存或正式库位",
)
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
class LayoutOneStepDraftContext:
    """A suspended advanced draft that must survive a one-step zone publish."""

    draft_snapshot: LayoutDraftSnapshot
    had_active_draft: bool
    published_floor_revision: str
    published_feature_version: int


@dataclass(frozen=True)
class LayoutNoGoRemovalContext:
    """A suspended advanced draft around one scoped no-go removal publish."""

    draft_snapshot: LayoutDraftSnapshot
    had_active_draft: bool
    published_floor_revision: str
    removed_feature_ids: tuple[str, ...]


_ZONE_POLICY_FIELDS = (
    "allowed_inventory_types",
    "storage_layout",
    "erp_area_code",
    "formal_area_name",
    "formal_area_id",
    "formal_floor_id",
    "max_rack_count",
)


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


def begin_warehouse_twin_one_step_publish(
    floor_code: str,
    feature_id: str,
    *,
    expected_effective_revision: str,
    expected_published_revision: str,
    geometry_only: bool = False,
    published_path: Path | None = None,
    draft_path: Path | None = None,
) -> LayoutOneStepDraftContext:
    """Suspend an advanced draft before an isolated zone publish.

    The simplified confirmation owns only the selected zone's formal storage
    policy.  Any unpublished rack, geometry, or other-floor edits remain in the
    snapshot and are rebased after the selected policy has been published.
    """

    normalized = _normalize_floor_code(floor_code)
    published_source = _published_layout_paths(published_path).source
    draft_target = draft_path or TWIN_LAYOUT_DRAFT_PATH
    with _LAYOUT_EDIT_LOCK:
        published = _read_document(published_source)
        published_floor = published["floors"].get(normalized)
        if not isinstance(published_floor, dict):
            raise WarehouseTwinLayoutEditNotFoundError(
                f"数字孪生平面缺少 {normalized}"
            )
        published_revision = str(published_floor.get("revision") or "")
        if published_revision != str(expected_published_revision or ""):
            raise WarehouseTwinLayoutEditConflictError(
                "正式地图已更新，请刷新后重新确认"
            )
        draft_snapshot = snapshot_warehouse_twin_layout_draft(
            draft_path=draft_target
        )
        active_draft = _active_draft_document_unlocked(
            published_path=published_source,
            draft_path=draft_target,
            create=False,
        )
        effective_floor = published_floor
        if active_draft is not None:
            candidate = active_draft["floors"].get(normalized)
            if not isinstance(candidate, dict):
                raise WarehouseTwinLayoutEditError(f"布局草稿缺少 {normalized}")
            effective_floor = candidate
        if str(effective_floor.get("revision") or "") != str(
            expected_effective_revision or ""
        ):
            raise WarehouseTwinLayoutEditConflictError(
                "地图或区域已被其他操作更新，请刷新后重新确认"
            )

        published_feature = next(
            (
                item
                for item in published_floor.get("features") or []
                if str(item.get("id") or "") == feature_id
            ),
            None,
        )
        draft_feature: dict[str, Any] | None = None
        if active_draft is not None:
            candidate_feature = next(
                (
                    item
                    for item in effective_floor.get("features") or []
                    if str(item.get("id") or "") == feature_id
                ),
                None,
            )
            if not isinstance(candidate_feature, dict) or candidate_feature.get(
                "feature_kind"
            ) != "zone":
                raise WarehouseTwinLayoutEditConflictError(
                    "当前区域已在高级维护草稿中删除或改变类型；请刷新地图后重新确认"
                )
            draft_feature = candidate_feature

        if published_feature is not None and (
            not isinstance(published_feature, dict)
            or published_feature.get("feature_kind") != "zone"
        ):
            raise WarehouseTwinLayoutEditConflictError(
                "正式地图中的同名对象不是区域；请刷新地图后重新确认"
            )
        if published_feature is None and draft_feature is None:
            raise WarehouseTwinLayoutEditNotFoundError("区域不存在或已被删除")

        one_step_floor_revision = published_revision
        one_step_feature = published_feature
        if geometry_only:
            if active_draft is None or published_feature is None or draft_feature is None:
                raise WarehouseTwinLayoutEditConflictError("当前区域没有可应用的已保存调整")
            isolated = _new_draft_document(published_source)
            isolated_floor = isolated["floors"][normalized]
            isolated_feature = deepcopy(published_feature)
            for key in ("points", "ground_location_draft"):
                if key in draft_feature:
                    isolated_feature[key] = deepcopy(draft_feature[key])
            isolated_feature["version"] = max(int(draft_feature.get("version") or 1), int(published_feature.get("version") or 1))
            isolated_floor["features"] = [isolated_feature if f.get("id") == feature_id else f
                                          for f in isolated_floor["features"]]
            isolated_floor["layout_edited_at"] = _utc_iso()
            isolated_floor["revision"] = _floor_revision(isolated_floor)
            isolated["generated_at"] = isolated_floor["layout_edited_at"]
            _mark_draft_changed(isolated, normalized)
            _write_document(draft_target, isolated)
            one_step_floor_revision = str(isolated_floor["revision"])
            one_step_feature = isolated_feature
        elif active_draft is not None and published_feature is None:
            # A newly drawn zone exists only in the administrator's advanced
            # draft.  Build a disposable draft from the published map and
            # inject only that selected zone.  Publishing the full advanced
            # draft here would also publish unrelated aisles, racks or other
            # floor edits without an explicit review.
            isolated = _new_draft_document(published_source)
            isolated_floor = isolated["floors"].get(normalized)
            if not isinstance(isolated_floor, dict) or draft_feature is None:
                raise WarehouseTwinLayoutEditError(f"布局草稿缺少 {normalized}")
            isolated_features = list(isolated_floor.get("features") or [])
            if any(
                str(item.get("id") or "") == feature_id
                for item in isolated_features
            ):
                raise WarehouseTwinLayoutEditConflictError(
                    "正式地图已出现同一对象，请刷新后重新确认"
                )
            isolated_feature = deepcopy(draft_feature)
            isolated_features.append(isolated_feature)
            isolated_floor["features"] = isolated_features
            isolated_floor["erp_area_codes"] = sorted(
                {
                    str(item.get("erp_area_code") or "").strip().upper()
                    for item in isolated_features
                    if str(item.get("erp_area_code") or "").strip()
                }
            )

            receipts = list(isolated_floor.get("layout_edit_receipts") or [])
            receipt_keys = {
                (
                    str(receipt.get("operation_key") or ""),
                    str(receipt.get("action") or ""),
                )
                for receipt in receipts
            }
            for receipt in effective_floor.get("layout_edit_receipts") or []:
                result = receipt.get("result") or {}
                if str(result.get("id") or "") != feature_id:
                    continue
                key = (
                    str(receipt.get("operation_key") or ""),
                    str(receipt.get("action") or ""),
                )
                if key in receipt_keys:
                    continue
                receipt_keys.add(key)
                receipts.append(deepcopy(receipt))
            isolated_floor["layout_edit_receipts"] = receipts[-100:]
            isolated_floor["layout_edited_at"] = _utc_iso()
            isolated_floor["revision"] = _floor_revision(isolated_floor)
            isolated["generated_at"] = isolated_floor["layout_edited_at"]
            _mark_draft_changed(isolated, normalized)
            _write_document(draft_target, isolated)
            one_step_floor_revision = str(isolated_floor["revision"])
            one_step_feature = isolated_feature
        elif active_draft is not None:
            draft_target.unlink(missing_ok=True)

        assert isinstance(one_step_feature, dict)

        return LayoutOneStepDraftContext(
            draft_snapshot=draft_snapshot,
            had_active_draft=active_draft is not None,
            published_floor_revision=one_step_floor_revision,
            published_feature_version=int(one_step_feature.get("version") or 1),
        )


def rebase_warehouse_twin_advanced_draft_after_one_step(
    context: LayoutOneStepDraftContext,
    floor_code: str,
    feature_id: str,
    *,
    geometry_only: bool = False,
    remaining_location_drafts: dict[str, Any] | None = None,
    published_feature_snapshot: dict[str, Any] | None = None,
    published_path: Path | None = None,
    draft_path: Path | None = None,
) -> bool:
    """Restore unrelated advanced edits on top of the newly published zone."""

    if not context.had_active_draft:
        return False
    if not context.draft_snapshot.existed or context.draft_snapshot.content is None:
        raise WarehouseTwinLayoutEditError("高级维护草稿快照缺失")

    normalized = _normalize_floor_code(floor_code)
    published_source = _published_layout_paths(published_path).source
    draft_target = draft_path or TWIN_LAYOUT_DRAFT_PATH
    with _LAYOUT_EDIT_LOCK:
        try:
            advanced = json.loads(context.draft_snapshot.content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise WarehouseTwinLayoutEditError("高级维护草稿快照无法读取") from error
        published = _read_document(published_source)
        advanced_floor = (advanced.get("floors") or {}).get(normalized)
        published_floor = (published.get("floors") or {}).get(normalized)
        if not isinstance(advanced_floor, dict) or not isinstance(
            published_floor, dict
        ):
            raise WarehouseTwinLayoutEditError(f"地图草稿缺少 {normalized}")
        published_feature = next(
            (
                item
                for item in published_floor.get("features") or []
                if str(item.get("id") or "") == feature_id
            ),
            None,
        )
        if published_feature_snapshot is not None:
            if (
                published_feature_snapshot.get("feature_kind") != "zone"
                or str(published_feature_snapshot.get("id") or "") != feature_id
            ):
                raise WarehouseTwinLayoutEditError("一次确认后的区域快照无效")
            # The one-step confirmation has already validated and published
            # this exact policy snapshot before formal-area synchronization.
            # Reusing it here prevents an unrelated second file read during
            # advanced-draft restoration from rolling that confirmation back.
            published_feature = published_feature_snapshot
        advanced_features = list(advanced_floor.get("features") or [])
        advanced_index = next(
            (
                index
                for index, item in enumerate(advanced_features)
                if str(item.get("id") or "") == feature_id
            ),
            None,
        )
        if not isinstance(published_feature, dict):
            raise WarehouseTwinLayoutEditError("一次确认后区域身份回读失败")
        if advanced_index is None:
            # The one-step flow may have published a newly measured zone from
            # its isolated draft while an older advanced draft is being kept
            # for unrelated work.  The published zone is authoritative here;
            # add that unchanged copy to the preserved draft instead of
            # rolling the whole formal confirmation back after it succeeded.
            advanced_features.append(deepcopy(published_feature))
            advanced_index = len(advanced_features) - 1
        # Keep any advanced geometry/name/subtype edits for later maintenance,
        # but consume the selected zone's storage-policy draft.  The freshly
        # published policy is authoritative for these fields.
        rebased_feature = deepcopy(advanced_features[advanced_index])
        for key in (("points", "ground_location_draft") if geometry_only else _ZONE_POLICY_FIELDS):
            if key in published_feature:
                rebased_feature[key] = deepcopy(published_feature[key])
            else:
                rebased_feature.pop(key, None)
        rebased_feature["version"] = max(
            int(rebased_feature.get("version") or 1),
            int(published_feature.get("version") or 1),
        )
        advanced_features[advanced_index] = rebased_feature
        for item in advanced_features:
            if item.get("id") in (remaining_location_drafts or {}):
                item["ground_location_draft"] = deepcopy(remaining_location_drafts[item["id"]])
        advanced_floor["features"] = advanced_features
        advanced_floor["erp_area_codes"] = sorted(
            {
                str(item.get("erp_area_code") or "").strip().upper()
                for item in advanced_features
                if str(item.get("erp_area_code") or "").strip()
            }
        )

        receipts: list[dict[str, Any]] = []
        receipt_keys: set[tuple[str, str]] = set()
        advanced_receipts = [
            receipt
            for receipt in (advanced_floor.get("layout_edit_receipts") or [])
            if not (
                receipt.get("action") == "zone.policy.update"
                and str((receipt.get("result") or {}).get("id") or "") == feature_id
            )
        ]
        for receipt in [
            *advanced_receipts,
            *(published_floor.get("layout_edit_receipts") or []),
        ]:
            key = (
                str(receipt.get("operation_key") or ""),
                str(receipt.get("action") or ""),
            )
            if key in receipt_keys:
                continue
            receipt_keys.add(key)
            receipts.append(deepcopy(receipt))
        advanced_floor["layout_edit_receipts"] = receipts[-100:]
        advanced_floor["revision"] = _floor_revision(advanced_floor)
        advanced["generated_at"] = _utc_iso()

        # If the selected policy was the only pending edit, do not leave a
        # meaningless global draft behind.  This keeps the simple workflow in
        # its final operational state after one click.
        def semantic_floors(document: dict[str, Any]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for code, floor in (document.get("floors") or {}).items():
                normalized_floor = deepcopy(floor)
                for key in ("revision", "layout_edited_at", "layout_edit_receipts"):
                    normalized_floor.pop(key, None)
                result[str(code)] = normalized_floor
            return result

        if semantic_floors(advanced) == semantic_floors(published):
            draft_target.unlink(missing_ok=True)
            return False

        meta = advanced.get("draft_meta")
        if not isinstance(meta, dict):
            raise WarehouseTwinLayoutEditError("高级维护草稿元数据缺失")
        meta["status"] = "draft"
        meta["updated_at"] = _utc_iso()
        meta["base_published_sha256"] = _path_sha256(published_source)
        meta["base_floor_revisions"] = {
            code: str(item.get("revision") or "")
            for code, item in published.get("floors", {}).items()
            if isinstance(item, dict)
        }
        for key in (
            "validated_at",
            "validated_floor_revisions",
            "validation_blockers",
            "validation_warnings",
            "published_at",
            "last_publish",
        ):
            meta.pop(key, None)
        _write_document(draft_target, advanced)
        return True


def begin_warehouse_twin_no_go_removal_publish(
    floor_code: str,
    feature_ids: list[str],
    *,
    expected_effective_revision: str,
    expected_published_revision: str,
    published_path: Path | None = None,
    draft_path: Path | None = None,
) -> LayoutNoGoRemovalContext:
    """Build an isolated draft that removes only explicit no-go features."""

    normalized = _normalize_floor_code(floor_code)
    published_source = _published_layout_paths(published_path).source
    draft_target = draft_path or TWIN_LAYOUT_DRAFT_PATH
    requested = tuple(dict.fromkeys(str(item or "").strip() for item in feature_ids))
    if not requested or any(not item for item in requested):
        raise WarehouseTwinLayoutEditError("禁放区清单不能为空")
    requested_set = set(requested)

    with _LAYOUT_EDIT_LOCK:
        published = _read_document(published_source)
        published_floor = published["floors"].get(normalized)
        if not isinstance(published_floor, dict):
            raise WarehouseTwinLayoutEditNotFoundError(
                f"数字孪生平面缺少 {normalized}"
            )
        published_revision = str(published_floor.get("revision") or "")
        if published_revision != str(expected_published_revision or ""):
            raise WarehouseTwinLayoutEditConflictError(
                "正式地图已更新，请刷新后重新清理禁放区"
            )
        draft_snapshot = snapshot_warehouse_twin_layout_draft(
            draft_path=draft_target
        )
        active_draft = _active_draft_document_unlocked(
            published_path=published_source,
            draft_path=draft_target,
            create=False,
        )
        effective_floor = published_floor
        if active_draft is not None:
            candidate = active_draft["floors"].get(normalized)
            if not isinstance(candidate, dict):
                raise WarehouseTwinLayoutEditError(f"布局草稿缺少 {normalized}")
            effective_floor = candidate
        if str(effective_floor.get("revision") or "") != str(
            expected_effective_revision or ""
        ):
            raise WarehouseTwinLayoutEditConflictError(
                "地图或草稿已被其他操作更新，请刷新后重新清理禁放区"
            )

        published_by_id = {
            str(item.get("id") or ""): item
            for item in published_floor.get("features") or []
        }
        missing = sorted(requested_set - set(published_by_id))
        if missing:
            raise WarehouseTwinLayoutEditNotFoundError(
                "正式地图中的禁放区已经变化，请刷新后重试"
            )
        wrong_kind = [
            item_id
            for item_id in requested
            if str(published_by_id[item_id].get("feature_kind") or "") != "no_go"
        ]
        if wrong_kind:
            raise WarehouseTwinLayoutEditConflictError(
                "清单包含非禁放区对象，已停止删除"
            )
        effective_by_id = {
            str(item.get("id") or ""): item
            for item in effective_floor.get("features") or []
        }
        changed_kind = [
            item_id
            for item_id in requested
            if item_id in effective_by_id
            and str(effective_by_id[item_id].get("feature_kind") or "") != "no_go"
        ]
        if changed_kind:
            raise WarehouseTwinLayoutEditConflictError(
                "管理员草稿中的对象类型已变化，请刷新后核对"
            )

        isolated = _new_draft_document(published_source)
        isolated_floor = isolated["floors"][normalized]
        isolated_floor["features"] = [
            item
            for item in isolated_floor.get("features") or []
            if str(item.get("id") or "") not in requested_set
        ]
        retired = list(isolated_floor.get("retired_features") or [])
        retired_ids = {str(item.get("id") or "") for item in retired}
        for item_id in requested:
            if item_id in retired_ids:
                continue
            item = deepcopy(published_by_id[item_id])
            item["retired_at"] = _utc_iso()
            item["retired_reason"] = (
                "管理员受控清理禁放区；库存、库位、区域和通道未改变"
            )
            retired.append(item)
        isolated_floor["retired_features"] = retired
        isolated_floor["layout_edited_at"] = _utc_iso()
        isolated_floor["revision"] = _floor_revision(isolated_floor)
        isolated["generated_at"] = isolated_floor["layout_edited_at"]
        _mark_draft_changed(isolated, normalized)
        _write_document(draft_target, isolated)
        return LayoutNoGoRemovalContext(
            draft_snapshot=draft_snapshot,
            had_active_draft=active_draft is not None,
            published_floor_revision=str(isolated_floor["revision"]),
            removed_feature_ids=requested,
        )


def rebase_warehouse_twin_advanced_draft_after_no_go_removal(
    context: LayoutNoGoRemovalContext,
    floor_code: str,
    *,
    published_path: Path | None = None,
    draft_path: Path | None = None,
) -> bool:
    """Remove the same no-go objects while restoring every unrelated draft."""

    if not context.had_active_draft:
        return False
    if not context.draft_snapshot.existed or context.draft_snapshot.content is None:
        raise WarehouseTwinLayoutEditError("高级维护草稿快照缺失")
    normalized = _normalize_floor_code(floor_code)
    published_source = _published_layout_paths(published_path).source
    draft_target = draft_path or TWIN_LAYOUT_DRAFT_PATH
    removed_ids = set(context.removed_feature_ids)

    with _LAYOUT_EDIT_LOCK:
        try:
            advanced = json.loads(context.draft_snapshot.content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise WarehouseTwinLayoutEditError("高级维护草稿快照无法读取") from error
        published = _read_document(published_source)
        advanced_floor = (advanced.get("floors") or {}).get(normalized)
        published_floor = (published.get("floors") or {}).get(normalized)
        if not isinstance(advanced_floor, dict) or not isinstance(
            published_floor, dict
        ):
            raise WarehouseTwinLayoutEditError(f"地图草稿缺少 {normalized}")

        advanced_floor["features"] = [
            item
            for item in advanced_floor.get("features") or []
            if str(item.get("id") or "") not in removed_ids
        ]
        retired = list(advanced_floor.get("retired_features") or [])
        retired_ids = {str(item.get("id") or "") for item in retired}
        published_retired = {
            str(item.get("id") or ""): item
            for item in published_floor.get("retired_features") or []
            if str(item.get("id") or "") in removed_ids
        }
        for item_id in context.removed_feature_ids:
            if item_id in retired_ids:
                continue
            item = published_retired.get(item_id)
            if not isinstance(item, dict):
                raise WarehouseTwinLayoutEditError(
                    "正式地图缺少已删除禁放区的归档记录"
                )
            retired.append(deepcopy(item))
            retired_ids.add(item_id)
        advanced_floor["retired_features"] = retired
        advanced_floor["erp_area_codes"] = sorted(
            {
                str(item.get("erp_area_code") or "").strip().upper()
                for item in advanced_floor["features"]
                if str(item.get("erp_area_code") or "").strip()
            }
        )
        advanced_floor["revision"] = _floor_revision(advanced_floor)
        advanced["generated_at"] = _utc_iso()

        def semantic_floors(document: dict[str, Any]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for code, floor in (document.get("floors") or {}).items():
                normalized_floor = deepcopy(floor)
                for key in ("revision", "layout_edited_at", "layout_edit_receipts"):
                    normalized_floor.pop(key, None)
                result[str(code)] = normalized_floor
            return result

        if semantic_floors(advanced) == semantic_floors(published):
            draft_target.unlink(missing_ok=True)
            return False
        meta = advanced.get("draft_meta")
        if not isinstance(meta, dict):
            raise WarehouseTwinLayoutEditError("高级维护草稿元数据缺失")
        meta["status"] = "draft"
        meta["updated_at"] = _utc_iso()
        meta["base_published_sha256"] = _path_sha256(published_source)
        meta["base_floor_revisions"] = {
            code: str(item.get("revision") or "")
            for code, item in published.get("floors", {}).items()
            if isinstance(item, dict)
        }
        for key in (
            "validated_at",
            "validated_floor_revisions",
            "validation_blockers",
            "validation_warnings",
            "published_at",
            "last_publish",
        ):
            meta.pop(key, None)
        _write_document(draft_target, advanced)
        return True


def begin_warehouse_twin_one_step_rack_publish(
    floor_code: str,
    rack_id: str,
    *,
    expected_effective_revision: str,
    expected_published_revision: str,
    published_path: Path | None = None,
    draft_path: Path | None = None,
) -> LayoutOneStepDraftContext:
    """Publish one saved rack without consuming unrelated administrator edits."""

    normalized = _normalize_floor_code(floor_code)
    published_source = _published_layout_paths(published_path).source
    draft_target = draft_path or TWIN_LAYOUT_DRAFT_PATH
    with _LAYOUT_EDIT_LOCK:
        published = _read_document(published_source)
        published_floor = published["floors"].get(normalized)
        if not isinstance(published_floor, dict):
            raise WarehouseTwinLayoutEditNotFoundError(f"数字孪生平面缺少 {normalized}")
        published_revision = str(published_floor.get("revision") or "")
        if published_revision != str(expected_published_revision or ""):
            raise WarehouseTwinLayoutEditConflictError("正式地图已更新，请刷新后重新确认")
        draft_snapshot = snapshot_warehouse_twin_layout_draft(draft_path=draft_target)
        active_draft = _active_draft_document_unlocked(
            published_path=published_source, draft_path=draft_target, create=False,
        )
        if active_draft is None:
            raise WarehouseTwinLayoutEditConflictError("当前货架没有可应用的已保存调整")
        effective_floor = active_draft["floors"].get(normalized)
        if not isinstance(effective_floor, dict):
            raise WarehouseTwinLayoutEditError(f"布局草稿缺少 {normalized}")
        if str(effective_floor.get("revision") or "") != str(expected_effective_revision or ""):
            raise WarehouseTwinLayoutEditConflictError("地图或货架已被其他操作更新，请刷新后重新确认")
        draft_rack = next(
            (item for item in effective_floor.get("racks") or [] if str(item.get("id") or "") == rack_id),
            None,
        )
        if not isinstance(draft_rack, dict):
            raise WarehouseTwinLayoutEditNotFoundError("货架不存在或已从草稿删除")
        published_rack = next(
            (item for item in published_floor.get("racks") or [] if str(item.get("id") or "") == rack_id),
            None,
        )
        if published_rack == draft_rack:
            raise WarehouseTwinLayoutEditConflictError("当前货架没有可应用的已保存调整")

        isolated = _new_draft_document(published_source)
        isolated_floor = isolated["floors"][normalized]
        isolated_racks = list(isolated_floor.get("racks") or [])
        index = next(
            (i for i, item in enumerate(isolated_racks) if str(item.get("id") or "") == rack_id),
            None,
        )
        if index is None:
            isolated_racks.append(deepcopy(draft_rack))
        else:
            isolated_racks[index] = deepcopy(draft_rack)
        isolated_floor["racks"] = isolated_racks
        isolated_floor["layout_edited_at"] = _utc_iso()
        isolated_floor["revision"] = _floor_revision(isolated_floor)
        isolated["generated_at"] = isolated_floor["layout_edited_at"]
        _mark_draft_changed(isolated, normalized)
        _write_document(draft_target, isolated)
        return LayoutOneStepDraftContext(
            draft_snapshot=draft_snapshot,
            had_active_draft=True,
            published_floor_revision=str(isolated_floor["revision"]),
            published_feature_version=int(draft_rack.get("version") or 1),
        )


def rebase_warehouse_twin_advanced_rack_after_one_step(
    context: LayoutOneStepDraftContext,
    floor_code: str,
    rack_id: str,
    *,
    published_path: Path | None = None,
    draft_path: Path | None = None,
) -> bool:
    """Consume one applied rack and restore every unrelated saved edit."""

    if not context.draft_snapshot.existed or context.draft_snapshot.content is None:
        raise WarehouseTwinLayoutEditError("高级维护草稿快照缺失")
    normalized = _normalize_floor_code(floor_code)
    published_source = _published_layout_paths(published_path).source
    draft_target = draft_path or TWIN_LAYOUT_DRAFT_PATH
    with _LAYOUT_EDIT_LOCK:
        try:
            advanced = json.loads(context.draft_snapshot.content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise WarehouseTwinLayoutEditError("高级维护草稿快照无法读取") from error
        published = _read_document(published_source)
        advanced_floor = (advanced.get("floors") or {}).get(normalized)
        published_floor = (published.get("floors") or {}).get(normalized)
        if not isinstance(advanced_floor, dict) or not isinstance(published_floor, dict):
            raise WarehouseTwinLayoutEditError(f"地图草稿缺少 {normalized}")
        published_rack = next(
            (item for item in published_floor.get("racks") or [] if str(item.get("id") or "") == rack_id),
            None,
        )
        advanced_racks = list(advanced_floor.get("racks") or [])
        advanced_index = next(
            (i for i, item in enumerate(advanced_racks) if str(item.get("id") or "") == rack_id),
            None,
        )
        if not isinstance(published_rack, dict) or advanced_index is None:
            raise WarehouseTwinLayoutEditError("货架应用后身份回读失败")
        advanced_racks[advanced_index] = deepcopy(published_rack)
        advanced_floor["racks"] = advanced_racks
        advanced_floor["revision"] = _floor_revision(advanced_floor)
        advanced["generated_at"] = _utc_iso()

        def semantic_floors(document: dict[str, Any]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for code, floor in (document.get("floors") or {}).items():
                normalized_floor = deepcopy(floor)
                for key in ("revision", "layout_edited_at", "layout_edit_receipts"):
                    normalized_floor.pop(key, None)
                result[str(code)] = normalized_floor
            return result

        if semantic_floors(advanced) == semantic_floors(published):
            draft_target.unlink(missing_ok=True)
            return False
        meta = advanced.get("draft_meta")
        if not isinstance(meta, dict):
            raise WarehouseTwinLayoutEditError("高级维护草稿元数据缺失")
        meta["status"] = "draft"
        meta["updated_at"] = _utc_iso()
        meta["base_published_sha256"] = _path_sha256(published_source)
        meta["base_floor_revisions"] = {
            code: str(item.get("revision") or "")
            for code, item in published.get("floors", {}).items()
            if isinstance(item, dict)
        }
        for key in (
            "validated_at", "validated_floor_revisions", "validation_blockers",
            "validation_warnings", "published_at", "last_publish",
        ):
            meta.pop(key, None)
        _write_document(draft_target, advanced)
        return True


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


def _dirty_floor_codes(document: dict[str, Any]) -> list[str]:
    meta = document.get("draft_meta") or {}
    base_floor_revisions = meta.get("base_floor_revisions") or {}
    return [
        str(code)
        for code, floor in (document.get("floors") or {}).items()
        if isinstance(floor, dict)
        and str(floor.get("revision") or "")
        != str(base_floor_revisions.get(code) or "")
    ]


def _floor_validation(
    document: dict[str, Any], floor_code: str, floor_revision: str
) -> dict[str, Any] | None:
    meta = document.get("draft_meta") or {}
    validations = meta.get("floor_validations") or {}
    validation = validations.get(floor_code)
    if (
        isinstance(validation, dict)
        and str(validation.get("revision") or "") == floor_revision
    ):
        return validation

    # Older single-floor drafts stored validation details globally.  Accept
    # those records until the draft is changed or validated again.
    validated_revisions = meta.get("validated_floor_revisions") or {}
    if (
        meta.get("status") == "validated"
        and str(validated_revisions.get(floor_code) or "") == floor_revision
    ):
        return {
            "revision": floor_revision,
            "validated_at": meta.get("validated_at"),
            "blockers": list(meta.get("validation_blockers") or []),
            "warnings": list(meta.get("validation_warnings") or []),
        }
    return None


def _refresh_draft_status(document: dict[str, Any]) -> None:
    meta = document.get("draft_meta")
    if not isinstance(meta, dict):
        return
    dirty_floors = _dirty_floor_codes(document)
    validated_revisions = meta.get("validated_floor_revisions") or {}
    all_dirty_floors_validated = bool(dirty_floors) and all(
        str(validated_revisions.get(code) or "")
        == str((document.get("floors") or {}).get(code, {}).get("revision") or "")
        for code in dirty_floors
    )
    meta["status"] = "validated" if all_dirty_floors_validated else "draft"


def _mark_draft_changed(document: dict[str, Any], floor_code: str) -> None:
    meta = document.get("draft_meta")
    if not isinstance(meta, dict):
        return
    meta["updated_at"] = _utc_iso()
    validated_revisions = dict(meta.get("validated_floor_revisions") or {})
    validated_revisions.pop(floor_code, None)
    if validated_revisions:
        meta["validated_floor_revisions"] = validated_revisions
    else:
        meta.pop("validated_floor_revisions", None)
    floor_validations = dict(meta.get("floor_validations") or {})
    floor_validations.pop(floor_code, None)
    if floor_validations:
        meta["floor_validations"] = floor_validations
    else:
        meta.pop("floor_validations", None)
    for key in ("validated_at", "validation_blockers", "validation_warnings"):
        meta.pop(key, None)
    _refresh_draft_status(document)


def _normalize_floor_code(floor_code: str) -> str:
    normalized = str(floor_code or "").strip().upper()
    if normalized not in {"1F", "3F", "4F"}:
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
            _mark_draft_changed(document, normalized)
        _write_document(target, document)
        return LayoutMutation(
            value=result,
            floor_revision=str(floor["revision"]),
            applied=True,
        )


def _calibration_point(value: Any, *, label: str) -> tuple[float, float]:
    if isinstance(value, dict):
        value = value.get("point_mm") or value.get("point") or value.get("coordinates")
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise WarehouseTwinLayoutEditError(f"{label}必须是二维坐标")
    try:
        point = (float(value[0]), float(value[1]))
    except (TypeError, ValueError) as error:
        raise WarehouseTwinLayoutEditError(f"{label}必须是有效数值坐标") from error
    if not all(math.isfinite(item) and abs(item) <= 10_000_000 for item in point):
        raise WarehouseTwinLayoutEditError(f"{label}超出允许范围")
    return point


def _three_calibration_points(values: Any, *, label: str) -> list[tuple[float, float]]:
    if not isinstance(values, list) or len(values) != 3:
        raise WarehouseTwinLayoutEditError(f"{label}必须正好包含三个点")
    return [
        _calibration_point(value, label=f"{label}第{index + 1}点")
        for index, value in enumerate(values)
    ]


def _signed_triangle_area2(points: list[tuple[float, float]]) -> float:
    (ax, ay), (bx, by), (cx, cy) = points
    return (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)


def _fit_floor4_rigid_transform(
    source_points: list[tuple[float, float]],
    target_points: list[tuple[float, float]],
) -> dict[str, Any]:
    source_area2 = _signed_triangle_area2(source_points)
    target_area2 = _signed_triangle_area2(target_points)
    if abs(source_area2) <= 1.0 or abs(target_area2) <= 1.0:
        raise WarehouseTwinLayoutEditError("三点标定不能共线或过于接近")
    if source_area2 * target_area2 < 0:
        raise WarehouseTwinLayoutEditError("三点顺序形成镜像，请按现场标定点顺序重新点选")

    source_center = (
        sum(point[0] for point in source_points) / 3.0,
        sum(point[1] for point in source_points) / 3.0,
    )
    target_center = (
        sum(point[0] for point in target_points) / 3.0,
        sum(point[1] for point in target_points) / 3.0,
    )
    covariance_cos = 0.0
    covariance_sin = 0.0
    for source, target in zip(source_points, target_points, strict=True):
        sx, sy = source[0] - source_center[0], source[1] - source_center[1]
        tx, ty = target[0] - target_center[0], target[1] - target_center[1]
        covariance_cos += sx * tx + sy * ty
        covariance_sin += sx * ty - sy * tx
    if math.hypot(covariance_cos, covariance_sin) <= 1e-9:
        raise WarehouseTwinLayoutEditError("三点标定无法得到稳定旋转角度")
    rotation_rad = math.atan2(covariance_sin, covariance_cos)
    cos_value = math.cos(rotation_rad)
    sin_value = math.sin(rotation_rad)
    translation = (
        target_center[0]
        - (cos_value * source_center[0] - sin_value * source_center[1]),
        target_center[1]
        - (sin_value * source_center[0] + cos_value * source_center[1]),
    )

    def apply(point: tuple[float, float]) -> tuple[float, float]:
        return (
            cos_value * point[0] - sin_value * point[1] + translation[0],
            sin_value * point[0] + cos_value * point[1] + translation[1],
        )

    residuals = [
        math.hypot(transformed[0] - target[0], transformed[1] - target[1])
        for transformed, target in (
            (apply(source), target)
            for source, target in zip(source_points, target_points, strict=True)
        )
    ]
    maximum = max(residuals)
    rmse = math.sqrt(sum(value * value for value in residuals) / len(residuals))
    if maximum > FLOOR4_CALIBRATION_MAX_RESIDUAL_MM or rmse > FLOOR4_CALIBRATION_RMSE_MM:
        raise WarehouseTwinLayoutEditError(
            f"三点标定误差过大（最大 {maximum:.1f} mm，均方根 {rmse:.1f} mm），请重新点选"
        )
    return {
        "rotation_rad": rotation_rad,
        "rotation_deg": math.degrees(rotation_rad),
        "translation_mm": translation,
        "residuals_mm": residuals,
        "max_residual_mm": maximum,
        "rmse_residual_mm": rmse,
        "apply": apply,
    }


def _fit_floor4_doorway_transform(
    source_points: list[tuple[float, float]],
    target_points: list[tuple[float, float]],
) -> dict[str, Any]:
    """Align a floor by doorway midpoint and heading without resizing it.

    Source points are doorway endpoint 1, doorway endpoint 2 and one point on
    the opposite/internal side.  Target points use the equivalent A, B and C
    corners of the 3F authority.  Width/depth are deliberately not fitted:
    each floor keeps its measured freight-elevator footprint.
    """

    source_start, source_end, source_inside = source_points
    target_start, target_end, target_inside = target_points

    def basis(
        start: tuple[float, float],
        end: tuple[float, float],
        inside: tuple[float, float],
        *,
        label: str,
    ) -> dict[str, Any]:
        dx, dy = end[0] - start[0], end[1] - start[1]
        edge = math.hypot(dx, dy)
        if edge < FLOOR4_DOORWAY_MIN_EDGE_MM:
            raise WarehouseTwinLayoutEditError(f"{label}门口两端距离过近")
        if edge > FLOOR4_DOORWAY_MAX_EDGE_MM:
            raise WarehouseTwinLayoutEditError(f"{label}门口宽度超出允许范围")
        unit = (dx / edge, dy / edge)
        midpoint = ((start[0] + end[0]) / 2.0, (start[1] + end[1]) / 2.0)
        interior = (inside[0] - midpoint[0], inside[1] - midpoint[1])
        along = interior[0] * unit[0] + interior[1] * unit[1]
        perpendicular = (
            interior[0] - along * unit[0],
            interior[1] - along * unit[1],
        )
        depth = math.hypot(*perpendicular)
        if depth < FLOOR4_DOORWAY_MIN_EDGE_MM:
            raise WarehouseTwinLayoutEditError(
                f"{label}内侧方向点与门口近乎共线，请点货梯内侧后沿"
            )
        if depth > FLOOR4_DOORWAY_MAX_EDGE_MM:
            raise WarehouseTwinLayoutEditError(f"{label}进深超出允许范围")
        normal = (perpendicular[0] / depth, perpendicular[1] / depth)
        return {
            "start": start,
            "end": end,
            "midpoint": midpoint,
            "unit": unit,
            "normal": normal,
            "edge_mm": edge,
            "depth_mm": depth,
            "inside_along_offset_mm": along,
        }

    source = basis(
        source_start, source_end, source_inside, label="4F 实测货梯"
    )
    target = basis(
        target_start, target_end, target_inside, label="3F 权威货梯"
    )
    source_cross = (
        source["unit"][0] * source["normal"][1]
        - source["unit"][1] * source["normal"][0]
    )
    target_cross = (
        target["unit"][0] * target["normal"][1]
        - target["unit"][1] * target["normal"][0]
    )
    source_heading = source["unit"]
    if source_cross * target_cross < 0:
        source_heading = (-source_heading[0], -source_heading[1])
    rotation_rad = math.atan2(
        source_heading[0] * target["unit"][1]
        - source_heading[1] * target["unit"][0],
        source_heading[0] * target["unit"][0]
        + source_heading[1] * target["unit"][1],
    )
    cos_value = math.cos(rotation_rad)
    sin_value = math.sin(rotation_rad)
    rotated_midpoint = (
        cos_value * source["midpoint"][0]
        - sin_value * source["midpoint"][1],
        sin_value * source["midpoint"][0]
        + cos_value * source["midpoint"][1],
    )
    translation = (
        target["midpoint"][0] - rotated_midpoint[0],
        target["midpoint"][1] - rotated_midpoint[1],
    )

    def apply(point: tuple[float, float]) -> tuple[float, float]:
        return (
            cos_value * point[0] - sin_value * point[1] + translation[0],
            sin_value * point[0] + cos_value * point[1] + translation[1],
        )

    source_back_offset = (
        source["normal"][0] * source["depth_mm"],
        source["normal"][1] * source["depth_mm"],
    )
    source_footprint = [
        source_start,
        source_end,
        (
            source_end[0] + source_back_offset[0],
            source_end[1] + source_back_offset[1],
        ),
        (
            source_start[0] + source_back_offset[0],
            source_start[1] + source_back_offset[1],
        ),
    ]
    transformed_footprint = [apply(point) for point in source_footprint]
    transformed_normal = (
        cos_value * source["normal"][0] - sin_value * source["normal"][1],
        sin_value * source["normal"][0] + cos_value * source["normal"][1],
    )
    heading_dot = max(
        -1.0,
        min(
            1.0,
            transformed_normal[0] * target["normal"][0]
            + transformed_normal[1] * target["normal"][1],
        ),
    )
    heading_residual_deg = math.degrees(math.acos(heading_dot))
    return {
        "rotation_rad": rotation_rad,
        "rotation_deg": math.degrees(rotation_rad),
        "translation_mm": translation,
        "residuals_mm": [0.0, 0.0, 0.0],
        "max_residual_mm": 0.0,
        "rmse_residual_mm": 0.0,
        "anchor_residual_mm": 0.0,
        "heading_residual_deg": heading_residual_deg,
        "door_width_mm": source["edge_mm"],
        "depth_mm": source["depth_mm"],
        "inside_along_offset_mm": source["inside_along_offset_mm"],
        "authority_door_width_mm": target["edge_mm"],
        "authority_depth_mm": target["depth_mm"],
        "source_footprint": source_footprint,
        "transformed_footprint": transformed_footprint,
        "apply": apply,
    }


def _rounded_point(point: tuple[float, float]) -> list[float]:
    return [round(point[0], 3), round(point[1], 3)]


def _transform_floor4_geometry_item(
    item: dict[str, Any], *, transform: dict[str, Any]
) -> None:
    apply = transform["apply"]
    points = item.get("points")
    if isinstance(points, list):
        converted: list[list[float]] = []
        for index, point in enumerate(points):
            converted.append(
                _rounded_point(_calibration_point(point, label=f"几何点{index + 1}"))
            )
        item["points"] = [_rounded_point(apply(tuple(point))) for point in converted]
    if "x_mm" in item or "y_mm" in item:
        if "x_mm" not in item or "y_mm" not in item:
            raise WarehouseTwinLayoutEditError("地图对象坐标必须同时包含 x_mm 和 y_mm")
        point = _calibration_point(
            [item.get("x_mm"), item.get("y_mm")], label="地图对象坐标"
        )
        x_value, y_value = apply(point)
        item["x_mm"] = round(x_value, 3)
        item["y_mm"] = round(y_value, 3)
    if isinstance(item.get("rotation_deg"), (int, float)):
        item["rotation_deg"] = round(
            (float(item["rotation_deg"]) + float(transform["rotation_deg"])) % 360,
            6,
        )
    geometry = item.get("geometry")
    if isinstance(geometry, dict):
        _transform_floor4_geometry_item(geometry, transform=transform)


def _transform_floor4_bounds(floor: dict[str, Any], *, transform: dict[str, Any]) -> None:
    bounds = floor.get("bounds_mm")
    if not isinstance(bounds, dict):
        return
    try:
        bounds_corners = [
            (float(bounds["min_x"]), float(bounds["min_y"])),
            (float(bounds["min_x"]), float(bounds["max_y"])),
            (float(bounds["max_x"]), float(bounds["min_y"])),
            (float(bounds["max_x"]), float(bounds["max_y"])),
        ]
    except (KeyError, TypeError, ValueError) as error:
        raise WarehouseTwinLayoutEditError("4F 地图边界格式无效") from error

    metadata = floor.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
        floor["metadata"] = metadata
    stored_polygon = metadata.get(FLOOR4_SPATIAL_BOUNDS_POLYGON_KEY)
    if stored_polygon is None:
        polygon = bounds_corners
    else:
        if not isinstance(stored_polygon, list) or len(stored_polygon) != 4:
            raise WarehouseTwinLayoutEditConflictError("4F 持久边界几何无效，拒绝再次标定")
        polygon = [
            _calibration_point(point, label=f"4F 持久边界第{index + 1}点")
            for index, point in enumerate(stored_polygon)
        ]
        polygon_bounds = {
            "min_x": round(min(point[0] for point in polygon), 3),
            "min_y": round(min(point[1] for point in polygon), 3),
            "max_x": round(max(point[0] for point in polygon), 3),
            "max_y": round(max(point[1] for point in polygon), 3),
        }
        current_bounds = {
            "min_x": round(bounds_corners[0][0], 3),
            "min_y": round(bounds_corners[0][1], 3),
            "max_x": round(bounds_corners[3][0], 3),
            "max_y": round(bounds_corners[3][1], 3),
        }
        if polygon_bounds != current_bounds:
            raise WarehouseTwinLayoutEditConflictError(
                "4F 持久边界与当前地图边界不一致，拒绝再次标定"
            )

    transformed = [transform["apply"](point) for point in polygon]
    rounded_polygon = [_rounded_point(point) for point in transformed]
    metadata[FLOOR4_SPATIAL_BOUNDS_POLYGON_KEY] = rounded_polygon
    floor["bounds_mm"] = {
        "min_x": min(point[0] for point in rounded_polygon),
        "min_y": min(point[1] for point in rounded_polygon),
        "max_x": max(point[0] for point in rounded_polygon),
        "max_y": max(point[1] for point in rounded_polygon),
    }


def _clear_floor4_stale_calibration_text(floor: dict[str, Any]) -> dict[str, Any]:
    name = floor.get("name")
    if isinstance(name, str):
        for marker in FLOOR4_STALE_CALIBRATION_NAME_MARKERS:
            name = name.replace(marker, "")
        floor["name"] = name.strip()

    warnings = floor.get("warnings")
    if warnings is None:
        return {"floor_name": floor.get("name"), "removed_warnings": []}
    if not isinstance(warnings, list) or not all(
        isinstance(item, str) for item in warnings
    ):
        raise WarehouseTwinLayoutEditError("4F warnings 列表格式无效")
    has_spatial_facts = bool(
        any(
            isinstance(item, dict) and item.get("feature_kind") == "zone"
            for item in (floor.get("features") or [])
        )
        or any(isinstance(item, dict) for item in (floor.get("racks") or []))
        or any(isinstance(item, dict) for item in (floor.get("pallets") or []))
        or any(
            str(value or "").strip()
            for value in (floor.get("erp_area_codes") or [])
        )
    )
    removed_warnings: list[str] = []
    for warning in warnings:
        calibration_warning = any(
            marker in warning
            for marker in FLOOR4_STALE_CALIBRATION_WARNING_MARKERS
        )
        empty_warehouse_warning = has_spatial_facts and any(
            marker in warning
            for marker in FLOOR4_STALE_EMPTY_WAREHOUSE_WARNING_MARKERS
        )
        if calibration_warning or empty_warehouse_warning:
            removed_warnings.append(warning)
    floor["warnings"] = [
        warning for warning in warnings if warning not in removed_warnings
    ]
    return {
        "floor_name": floor.get("name"),
        "removed_warnings": removed_warnings,
    }


def _authoritative_freight_elevator(document: dict[str, Any]) -> dict[str, Any]:
    floor = (document.get("floors") or {}).get("3F")
    if not isinstance(floor, dict):
        raise WarehouseTwinLayoutEditError("正式地图缺少 3F 货梯权威对象")
    matches = [
        item
        for item in floor.get("features") or []
        if isinstance(item, dict) and item.get("feature_code") == "LIFT-002"
    ]
    if len(matches) != 1:
        raise WarehouseTwinLayoutEditError("3F 必须且只能存在一个权威货梯 LIFT-002")
    authority = matches[0]
    if not authority.get("is_locked"):
        raise WarehouseTwinLayoutEditError("3F 权威货梯 LIFT-002 未锁定，拒绝标定")
    authority_points = authority.get("points")
    if not isinstance(authority_points, list) or len(authority_points) != 2:
        raise WarehouseTwinLayoutEditError("3F 权威货梯 LIFT-002 几何无效")
    for index, point in enumerate(authority_points):
        _calibration_point(point, label=f"3F 权威货梯端点{index + 1}")
    return authority


def _canonical_freight_elevator_target_points(
    authority: dict[str, Any],
) -> list[list[float]]:
    start, end = [
        _calibration_point(point, label=f"3F 权威货梯端点{index + 1}")
        for index, point in enumerate(authority.get("points") or [])
    ]
    try:
        width = float(authority.get("width_mm"))
    except (TypeError, ValueError) as error:
        raise WarehouseTwinLayoutEditError("3F 权威货梯 LIFT-002 宽度无效") from error
    if not math.isfinite(width) or width <= 0:
        raise WarehouseTwinLayoutEditError("3F 权威货梯 LIFT-002 宽度无效")
    dx, dy = end[0] - start[0], end[1] - start[1]
    length = math.hypot(dx, dy)
    if length <= 1.0:
        raise WarehouseTwinLayoutEditError("3F 权威货梯 LIFT-002 几何无效")
    offset = (-dy * width / (2.0 * length), dx * width / (2.0 * length))
    return [
        _rounded_point((start[0] + offset[0], start[1] + offset[1])),
        _rounded_point((end[0] - offset[0], end[1] - offset[1])),
        _rounded_point((end[0] + offset[0], end[1] + offset[1])),
    ]


def _doorway_target_points(authority: dict[str, Any]) -> list[list[float]]:
    corner_a, corner_c, corner_b = _canonical_freight_elevator_target_points(
        authority
    )
    return [corner_a, corner_b, corner_c]


def _floor4_measured_lift(
    authority: dict[str, Any],
    *,
    floor_layout_id: Any,
    transform: dict[str, Any],
    version: int,
) -> dict[str, Any]:
    footprint = [
        _rounded_point(point) for point in transform["transformed_footprint"]
    ]
    door_start, door_end, back_end, back_start = footprint
    centerline_start = (
        (door_start[0] + back_start[0]) / 2.0,
        (door_start[1] + back_start[1]) / 2.0,
    )
    centerline_end = (
        (door_end[0] + back_end[0]) / 2.0,
        (door_end[1] + back_end[1]) / 2.0,
    )
    lift = deepcopy(authority)
    lift.update(
        {
            "id": FLOOR4_CANONICAL_FREIGHT_ELEVATOR_ID,
            "layout_id": floor_layout_id,
            "name": "货梯（4F实测，跨楼层定位）",
            "points": [
                _rounded_point(centerline_start),
                _rounded_point(centerline_end),
            ],
            "width_mm": round(float(transform["depth_mm"]), 3),
            "area_mm2": round(
                float(transform["door_width_mm"])
                * float(transform["depth_mm"]),
                3,
            ),
            "source": "site_doorway_calibration",
            "status": "confirmed",
            "is_locked": True,
            "version": version,
            "measured_footprint_points": footprint,
            "measured_door_width_mm": round(
                float(transform["door_width_mm"]), 3
            ),
            "measured_depth_mm": round(float(transform["depth_mm"]), 3),
            "cross_floor_authority_feature_id": authority.get("id"),
        }
    )
    return lift


def calibrate_floor4_freight_elevator(
    floor_code: str,
    *,
    expected_revision: str,
    operation_key: str,
    source_points: list[list[float] | tuple[float, float]],
    calibration_mode: str = "corner_rigid",
    published_path: Path | None = None,
    draft_path: Path | None = None,
) -> LayoutMutation:
    normalized = _normalize_floor_code(floor_code)
    if normalized != "4F":
        raise WarehouseTwinLayoutEditError("三点货梯标定仅允许用于 4F 规划草稿")
    normalized_key = str(operation_key or "").strip()
    if len(normalized_key) < 8 or len(normalized_key) > 120:
        raise WarehouseTwinLayoutEditError("布局操作键长度必须为 8 至 120 个字符")
    normalized_source = _three_calibration_points(
        list(source_points), label="4F 现场源点"
    )
    normalized_mode = str(calibration_mode or "corner_rigid").strip().lower()
    if normalized_mode not in {"corner_rigid", "doorway_heading"}:
        raise WarehouseTwinLayoutEditError("不支持的4F货梯标定方式")
    published_source = _published_layout_paths(published_path).source
    draft_target = draft_path or TWIN_LAYOUT_DRAFT_PATH
    action = "floor4.freight_elevator.calibrate"
    with _LAYOUT_EDIT_LOCK:
        published = _read_document(published_source)
        authority = deepcopy(_authoritative_freight_elevator(published))
        document = _active_draft_document_unlocked(
            published_path=published_source,
            draft_path=draft_target,
            create=True,
        )
        assert document is not None
        floor = document["floors"].get("4F")
        if not isinstance(floor, dict):
            raise WarehouseTwinLayoutEditNotFoundError("数字孪生平面缺少 4F")
        receipt = _find_receipt(floor, normalized_key, action)
        if receipt is not None:
            receipt_result = receipt.get("result") or {}
            receipt_calibration = receipt_result.get("calibration") or {}
            if receipt_calibration.get("source_points") != [
                _rounded_point(point) for point in normalized_source
            ] or receipt_calibration.get("input_mode", "corner_rigid") != normalized_mode:
                raise WarehouseTwinLayoutEditConflictError(
                    "该操作键已用于不同的三点标定请求"
                )
            return LayoutMutation(
                value=dict(receipt_result),
                floor_revision=str(floor.get("revision") or ""),
                applied=False,
            )
        if str(floor.get("revision") or "") != str(expected_revision or ""):
            raise WarehouseTwinLayoutEditConflictError("布局已被其他操作更新，请刷新后重试")
        if floor.get("operational_status") != "planning_only":
            raise WarehouseTwinLayoutEditError("4F 不是仅规划草稿，拒绝重新标定")
        existing_metadata = floor.get("metadata")
        existing_calibration = (
            existing_metadata.get("calibration", {})
            if isinstance(existing_metadata, dict)
            else {}
        )
        recalibrating = existing_calibration.get("applied") is True
        features = floor.get("features")
        if not isinstance(features, list) or not all(
            isinstance(item, dict) for item in features
        ):
            raise WarehouseTwinLayoutEditError("4F features 几何列表无效")
        materialized_lifts = [
            item for item in features if item.get("feature_code") == "LIFT-002"
        ]
        previous_lift_version = 0
        if recalibrating:
            if len(materialized_lifts) != 1:
                raise WarehouseTwinLayoutEditConflictError(
                    "4F 再次标定前必须且只能存在一个物化货梯 LIFT-002"
                )
            materialized_lift = materialized_lifts[0]
            if (
                materialized_lift.get("id")
                != FLOOR4_CANONICAL_FREIGHT_ELEVATOR_ID
                or materialized_lift.get("is_locked") is not True
            ):
                raise WarehouseTwinLayoutEditConflictError(
                    "4F 现有货梯不是受控物化的 LIFT-002，拒绝再次标定"
                )
            canonical_state = floor.get("canonical_authority")
            if (
                not isinstance(canonical_state, dict)
                or canonical_state.get("feature_code") != "LIFT-002"
                or canonical_state.get("materialized_on_4f") is not True
                or canonical_state.get("status") != "applied"
            ):
                raise WarehouseTwinLayoutEditConflictError(
                    "4F 货梯权威物化状态不完整，拒绝再次标定"
                )
            try:
                previous_lift_version = int(materialized_lift.get("version") or 1)
            except (TypeError, ValueError) as error:
                raise WarehouseTwinLayoutEditConflictError(
                    "4F 物化货梯版本无效，拒绝再次标定"
                ) from error
        elif materialized_lifts:
            raise WarehouseTwinLayoutEditConflictError(
                "4F 未完成标定但已存在货梯 LIFT-002，拒绝重复物化"
            )

        metadata = floor.get("metadata") if isinstance(floor.get("metadata"), dict) else {}
        # The imported 4F scan predates the metadata container.  Bind the
        # normalized container before bounds calibration so the persistent
        # oriented polygon written there is not replaced later in this mutation.
        floor["metadata"] = metadata
        previous_calibration = deepcopy(existing_calibration) if recalibrating else None
        previous_floor_revision = str(floor.get("revision") or "")
        legacy_calibration = (
            floor.get("calibration") if isinstance(floor.get("calibration"), dict) else {}
        )
        normalized_target = _three_calibration_points(
            _doorway_target_points(authority)
            if normalized_mode == "doorway_heading"
            else _canonical_freight_elevator_target_points(authority),
            label="4F 标准目标点",
        )
        transform = (
            _fit_floor4_doorway_transform(normalized_source, normalized_target)
            if normalized_mode == "doorway_heading"
            else _fit_floor4_rigid_transform(normalized_source, normalized_target)
        )

        protected = {
            code: deepcopy(document["floors"].get(code)) for code in ("1F", "3F")
        }
        if recalibrating:
            floor["features"] = [
                item
                for item in features
                if item.get("feature_code") != "LIFT-002"
            ]
        for collection_name in (
            "structures",
            "features",
            "placements",
            "racks",
            "pallets",
            "assets",
        ):
            collection = floor.get(collection_name)
            if collection is None:
                continue
            if not isinstance(collection, list) or not all(
                isinstance(item, dict) for item in collection
            ):
                raise WarehouseTwinLayoutEditError(f"4F {collection_name} 几何列表无效")
            for item in collection:
                _transform_floor4_geometry_item(item, transform=transform)
        _transform_floor4_bounds(floor, transform=transform)

        lift = (
            _floor4_measured_lift(
                authority,
                floor_layout_id=floor.get("layout_id"),
                transform=transform,
                version=previous_lift_version + 1,
            )
            if normalized_mode == "doorway_heading"
            else deepcopy(authority)
        )
        if normalized_mode != "doorway_heading":
            lift["id"] = FLOOR4_CANONICAL_FREIGHT_ELEVATOR_ID
            lift["layout_id"] = floor.get("layout_id")
            lift["is_locked"] = True
            lift["status"] = "confirmed"
            lift["version"] = previous_lift_version + 1
        floor.setdefault("features", []).append(lift)
        now = _utc_iso()
        audit = {
            "event": "recalibration" if recalibrating else "calibration",
            "operation_key": normalized_key,
            "applied_at": now,
            "source_floor_code": "4F",
            "source_floor_revision": previous_floor_revision,
            "authority_floor_code": "3F",
            "authority_feature_code": "LIFT-002",
            "authority_feature_id": authority.get("id"),
            "authority_floor_revision": (published.get("floors") or {})
            .get("3F", {})
            .get("revision"),
        }
        if recalibrating:
            previous_audit = (
                previous_calibration.get("audit")
                if isinstance(previous_calibration, dict)
                and isinstance(previous_calibration.get("audit"), dict)
                else {}
            )
            audit.update(
                {
                    "previous_operation_key": previous_audit.get("operation_key"),
                    "previous_floor_revision": previous_floor_revision,
                }
            )
        calibration_result = {
            "status": "aligned",
            "applied": True,
            "method": (
                "doorway_heading_rigid_2d"
                if normalized_mode == "doorway_heading"
                else "three_point_rigid_2d"
            ),
            "input_mode": normalized_mode,
            "source_coordinate_basis": "current_floor4_geometry",
            "scale": 1.0,
            "mirror": False,
            "source_points": [_rounded_point(point) for point in normalized_source],
            "canonical_target_points": [_rounded_point(point) for point in normalized_target],
            "rotation_deg": round(float(transform["rotation_deg"]), 9),
            "translation_mm": _rounded_point(transform["translation_mm"]),
            "residuals_mm": [round(value, 6) for value in transform["residuals_mm"]],
            "max_residual_mm": round(float(transform["max_residual_mm"]), 6),
            "rmse_residual_mm": round(float(transform["rmse_residual_mm"]), 6),
            "thresholds_mm": {
                "max_residual": FLOOR4_CALIBRATION_MAX_RESIDUAL_MM,
                "rmse": FLOOR4_CALIBRATION_RMSE_MM,
            },
            "audit": audit,
        }
        if normalized_mode == "doorway_heading":
            calibration_result.update(
                {
                    "source_footprint_points": [
                        _rounded_point(point)
                        for point in transform["source_footprint"]
                    ],
                    "transformed_footprint_points": [
                        _rounded_point(point)
                        for point in transform["transformed_footprint"]
                    ],
                    "measured_door_width_mm": round(
                        float(transform["door_width_mm"]), 3
                    ),
                    "measured_depth_mm": round(
                        float(transform["depth_mm"]), 3
                    ),
                    "authority_door_width_mm": round(
                        float(transform["authority_door_width_mm"]), 3
                    ),
                    "authority_depth_mm": round(
                        float(transform["authority_depth_mm"]), 3
                    ),
                    "inside_along_offset_mm": round(
                        float(transform["inside_along_offset_mm"]), 3
                    ),
                    "anchor_residual_mm": round(
                        float(transform["anchor_residual_mm"]), 6
                    ),
                    "heading_residual_deg": round(
                        float(transform["heading_residual_deg"]), 9
                    ),
                    "dimension_policy": "floor_specific_measured_footprint",
                }
            )
        if recalibrating:
            recalibration_history = metadata.get("recalibration_history") or []
            if not isinstance(recalibration_history, list) or not all(
                isinstance(item, dict) for item in recalibration_history
            ):
                raise WarehouseTwinLayoutEditError("4F 再次标定审计历史无效")
            recalibration_history = list(recalibration_history)
            recalibration_history.append(
                {
                    "event": "recalibration",
                    "operation_key": normalized_key,
                    "applied_at": now,
                    "previous_floor_revision": previous_floor_revision,
                    "previous_calibration": previous_calibration,
                }
            )
            metadata["recalibration_history"] = recalibration_history[-20:]
        metadata["calibration"] = calibration_result
        floor["metadata"] = metadata
        floor["calibration"] = {
            **legacy_calibration,
            **calibration_result,
            "target_points": calibration_result["canonical_target_points"],
        }
        canonical_authority = (
            floor.get("canonical_authority")
            if isinstance(floor.get("canonical_authority"), dict)
            else {}
        )
        canonical_authority.update(
            {
                "floor_code": "3F",
                "feature_code": "LIFT-002",
                "feature_id": authority.get("id"),
                "materialized_on_4f": True,
                "status": "applied",
                "geometry_policy": (
                    "floor_specific_measured_footprint"
                    if normalized_mode == "doorway_heading"
                    else "authority_geometry_copy"
                ),
            }
        )
        floor["canonical_authority"] = canonical_authority
        floor["alignment_status"] = "aligned"
        floor["alignment_applied"] = True
        stale_text_cleanup = _clear_floor4_stale_calibration_text(floor)
        if any(document["floors"].get(code) != protected[code] for code in ("1F", "3F")):
            raise WarehouseTwinLayoutEditError("标定越过 4F 边界，已拒绝保存")

        result = {
            "calibration": deepcopy(calibration_result),
            "freight_elevator": deepcopy(lift),
            "recalibrated": recalibrating,
            "inventory_changed": False,
            "stale_calibration_text_cleanup": stale_text_cleanup,
        }
        _remember_receipt(
            floor,
            operation_key=normalized_key,
            action=action,
            result=result,
        )
        floor["layout_edited_at"] = now
        floor["revision"] = _floor_revision(floor)
        document["generated_at"] = now
        _mark_draft_changed(document, "4F")
        _write_document(draft_target, document)
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


def _next_feature_code(floor: dict[str, Any], feature_kind: str) -> str:
    prefix = "ZONE" if feature_kind == "zone" else "AISLE"
    stem = f"{prefix}-{floor.get('floor_code')}-EDIT-"
    used = {
        str(item.get("feature_code") or "").strip().upper()
        for collection in (
            floor.get("features") or [],
            floor.get("retired_features") or [],
        )
        for item in collection
    }
    sequence = 1
    while f"{stem}{sequence:03d}" in used:
        sequence += 1
    return f"{stem}{sequence:03d}"


def _ensure_points_within_floor(
    floor: dict[str, Any], points: list[list[float]], *, label: str
) -> None:
    bounds = floor.get("bounds_mm") or {}
    if not all(key in bounds for key in ("min_x", "min_y", "max_x", "max_y")):
        raise WarehouseTwinLayoutEditError(
            f"楼层实测边界不完整，不能{label}"
        )
    if any(
        point[0] < float(bounds["min_x"])
        or point[0] > float(bounds["max_x"])
        or point[1] < float(bounds["min_y"])
        or point[1] > float(bounds["max_y"])
        for point in points
    ):
        raise WarehouseTwinLayoutEditError(
            f"{label}不能超出本楼层实测地图范围"
        )


def create_warehouse_twin_feature(
    floor_code: str,
    *,
    expected_revision: str,
    operation_key: str,
    feature_kind: str,
    points: list[list[float]],
    width_mm: float | None = None,
    direction: str | None = None,
    path: Path | None = None,
) -> LayoutMutation:
    normalized_kind = str(feature_kind or "").strip()
    if normalized_kind == "zone":
        normalized_points = _normalize_zone_points(points)
        _reject_self_intersection(normalized_points)
        normalized_width = None
        normalized_direction = None
    elif normalized_kind == "aisle":
        normalized_points = _normalize_aisle_points(points)
        try:
            normalized_width = float(width_mm) if width_mm is not None else 0.0
        except (TypeError, ValueError, OverflowError) as error:
            raise WarehouseTwinLayoutEditError("通道宽度必须是有效毫米数值") from error
        if not math.isfinite(normalized_width) or normalized_width <= 0 or normalized_width > 20_000:
            raise WarehouseTwinLayoutEditError("通道宽度必须大于0且不超过20000毫米")
        normalized_direction = str(direction or "two_way").strip()
        if normalized_direction not in {"one_way", "two_way"}:
            raise WarehouseTwinLayoutEditError("通道方向无效")
    else:
        raise WarehouseTwinLayoutEditError("只允许新增区域或通道")

    def mutate(floor: dict[str, Any]) -> dict[str, Any]:
        _ensure_points_within_floor(
            floor,
            normalized_points,
            label="区域边界" if normalized_kind == "zone" else "通道",
        )
        code = _next_feature_code(floor, normalized_kind)
        area_mm2 = (
            _zone_area_mm2(normalized_points)
            if normalized_kind == "zone"
            else _aisle_area_mm2(normalized_points, float(normalized_width or 0))
        )
        feature = {
            "id": str(uuid4()),
            "feature_code": code,
            "name": "新区域（待设置）" if normalized_kind == "zone" else "新通道",
            "feature_kind": normalized_kind,
            "subtype": "unassigned" if normalized_kind == "zone" else "shared_secondary",
            "points": normalized_points,
            "width_mm": normalized_width,
            "direction": normalized_direction,
            "no_stacking": normalized_kind == "aisle",
            "storage_mode": "floor",
            "elevation_mm": 0.0,
            "storage_height_mm": 1_000.0 if normalized_kind == "zone" else 3_000.0,
            "color": "#60a5fa" if normalized_kind == "zone" else "#22c55e",
            "area_mm2": area_mm2,
            "source": "manual",
            "status": "candidate",
            "is_locked": False,
            "version": 1,
            "erp_area_code": None,
        }
        floor.setdefault("features", []).append(feature)
        return feature

    mutation = _apply_mutation(
        floor_code,
        expected_revision=expected_revision,
        operation_key=operation_key,
        action="feature.create",
        mutate=mutate,
        path=path,
    )
    if not mutation.applied:
        if (
            mutation.value.get("feature_kind") != normalized_kind
            or mutation.value.get("points") != normalized_points
            or mutation.value.get("width_mm") != normalized_width
            or mutation.value.get("direction") != normalized_direction
        ):
            raise WarehouseTwinLayoutEditConflictError(
                "该操作键已用于不同的地图对象"
            )
    return mutation


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
        maximum = zone.get("max_rack_count")
        if maximum is not None:
            try:
                maximum = int(maximum)
            except (TypeError, ValueError) as error:
                raise WarehouseTwinLayoutEditError("区域最大货架数无效") from error
            if maximum < 0 or maximum > 500:
                raise WarehouseTwinLayoutEditError("区域最大货架数必须是 0 至 500")
            existing_count = sum(
                1
                for item in floor.get("racks") or []
                if str(item.get("area_feature_id") or "") == area_feature_id
            )
            if existing_count >= maximum:
                raise WarehouseTwinLayoutEditConflictError(
                    f"该区域最大货架数为 {maximum}，请先调整区域设置或整理现有货架"
                )
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


def number_warehouse_twin_area_racks(
    floor_code: str, *, expected_revision: str, operation_key: str,
    area_feature_id: str, path: Path | None = None,
) -> LayoutMutation:
    """Number display names in the fixed XY plan; retain every physical identity."""
    def mutate(floor: dict[str, Any]) -> dict[str, Any]:
        zone = _feature(floor, area_feature_id)
        if zone.get("feature_kind") != "zone":
            raise WarehouseTwinLayoutEditError("请选择仓储区域")
        area_code = _feature_area_code(zone)
        racks = [r for r in floor.get("racks", []) if
                 r.get("area_feature_id") == area_feature_id or
                 (not r.get("area_feature_id") and r.get("area_code") == area_code)]
        if not racks:
            raise WarehouseTwinLayoutEditError("当前区域没有货架")
        if any(r.get("is_locked") or r.get("mold_rack_code") for r in racks):
            raise WarehouseTwinLayoutEditConflictError("区域含锁定货架或专项模具架，不能批量改号")
        racks.sort(key=lambda r: (float(r["x_mm"]), -float(r["y_mm"]), str(r["id"])))
        prefix = str(zone.get("formal_area_name") or zone.get("name") or area_code)[:80]
        for index, rack in enumerate(racks, 1):
            name = f"{prefix} {index:02d}号架"
            if rack.get("name") != name:
                rack["name"] = name
                rack["version"] = int(rack.get("version") or 1) + 1
                rack["status"] = "candidate"
        return {"area_feature_id": area_feature_id, "racks": [dict(r) for r in racks]}
    result = _apply_mutation(floor_code, expected_revision=expected_revision,
        operation_key=operation_key, action="rack.number_area", mutate=mutate, path=path)
    if result.value.get("area_feature_id") != area_feature_id:
        raise WarehouseTwinLayoutEditConflictError("该操作键已用于其他区域编号")
    return result


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


def delete_warehouse_twin_feature(
    floor_code: str,
    feature_id: str,
    *,
    expected_revision: str,
    expected_version: int,
    operation_key: str,
    archive_empty_children: bool = False,
    path: Path | None = None,
) -> LayoutMutation:
    def mutate(floor: dict[str, Any]) -> dict[str, Any]:
        feature = _feature(floor, feature_id)
        feature_kind = str(feature.get("feature_kind") or "")
        if feature_kind not in {"zone", "aisle"}:
            raise WarehouseTwinLayoutEditError("只有区域和通道可以从规划草稿删除")
        label = "区域" if feature_kind == "zone" else "通道"
        _ensure_version(feature, expected_version, label)
        if feature.get("is_locked") and not archive_empty_children:
            raise WarehouseTwinLayoutEditConflictError(
                f"{label}已确认并锁定，必须先解除锁定"
            )
        feature_area_code = str(feature.get("erp_area_code") or "").strip().upper()
        feature_code = str(feature.get("feature_code") or "").strip().upper()

        def rack_belongs_to_feature(rack: dict[str, Any]) -> bool:
            return (
                str(rack.get("area_feature_id") or "").strip() == feature_id
                or (
                    bool(feature_area_code)
                    and str(rack.get("area_code") or "").strip().upper()
                    == feature_area_code
                )
            )

        owned_racks = [
            rack
            for rack in floor.get("racks") or []
            if rack_belongs_to_feature(rack)
        ]
        owned_pallets = [
            pallet
            for pallet in floor.get("pallets") or []
            if str(pallet.get("zone_id") or "").strip() == feature_id
            or (
                str(pallet.get("zone_code") or "").strip().upper()
                in {feature_code, feature_area_code}
                - {""}
            )
        ]
        if feature_kind == "zone" and owned_pallets:
            raise WarehouseTwinLayoutEditConflictError(
                "区域内仍有地图栈板，请先完成移货或受控处置"
            )
        if feature_kind == "zone" and owned_racks and not archive_empty_children:
            raise WarehouseTwinLayoutEditConflictError(
                "区域内仍有货架，请先处理货架后再删除区域"
            )
        if archive_empty_children and owned_racks:
            retained_racks = [
                item
                for item in floor.get("racks") or []
                if not rack_belongs_to_feature(item)
            ]
            for rack in owned_racks:
                retired_rack = dict(rack)
                retired_rack["retired_at"] = _utc_iso()
                retired_rack["retired_reason"] = (
                    "正式空区域受控归档；库存、正式库位和历史身份未改变"
                )
                floor.setdefault("retired_racks", []).append(retired_rack)
            floor["racks"] = retained_racks
        floor["features"] = [
            item
            for item in floor.get("features") or []
            if item.get("id") != feature_id
        ]
        retired = dict(feature)
        retired["retired_at"] = _utc_iso()
        retired["retired_reason"] = (
            "管理员从二维规划草稿删除；正式区域、库存、库位和正式地图未改变"
        )
        floor.setdefault("retired_features", []).append(retired)
        result = {
            "id": feature_id,
            "feature_code": feature.get("feature_code"),
            "feature_kind": feature_kind,
            "deleted": True,
            "inventory_changed": False,
            "published_map_changed": False,
        }
        if archive_empty_children:
            result["archived_child_rack_count"] = len(owned_racks)
        return result

    return _apply_mutation(
        floor_code,
        expected_revision=expected_revision,
        operation_key=operation_key,
        action="feature.delete",
        mutate=mutate,
        path=path,
    )


def apply_warehouse_twin_archived_area_tombstones(
    floor_code: str,
    *,
    expected_revision: str,
    operation_key: str,
    tombstones: list[dict[str, Any]],
    path: Path | None = None,
) -> LayoutMutation:
    """Persist the DB-authoritative archive projection into an editable draft.

    The archive transaction itself never writes JSON.  This derived mutation is
    safe to retry and is applied before validation so discard/rebuild cannot
    revive an archived area or block an unrelated future map publication.
    """

    def mutate(floor: dict[str, Any]) -> dict[str, Any]:
        partition = retire_archived_area_layout_objects(
            floor,
            tombstones=tombstones,
            retired_at=_utc_iso(),
        )
        removed_feature_ids = [
            str(item.get("id") or "") for item in partition["removed_features"]
        ]
        if not any(
            partition[key]
            for key in ("removed_features", "removed_racks", "removed_pallets")
        ):
            raise WarehouseTwinLayoutEditConflictError(
                "归档区域投影已变化，请刷新地图后重新校验"
            )
        return {
            "floor_code": floor_code.strip().upper(),
            "archived_feature_ids": removed_feature_ids,
            "retired_feature_count": len(partition["removed_features"]),
            "retired_rack_count": len(partition["removed_racks"]),
            "retired_pallet_count": len(partition["removed_pallets"]),
            "inventory_changed": False,
            "published_map_changed": False,
        }

    return _apply_mutation(
        floor_code,
        expected_revision=expected_revision,
        operation_key=operation_key,
        action="formal_area_archive_tombstones.apply",
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
    max_rack_count: int | None = None,
    legacy_v11_name_only: bool = False,
    path: Path | None = None,
) -> LayoutMutation:
    normalized_types = list(dict.fromkeys(str(value).strip() for value in allowed_inventory_types))
    if not normalized_types or any(value not in ALLOWED_INVENTORY_TYPES for value in normalized_types):
        raise WarehouseTwinLayoutEditError("区域至少选择一种有效存放类型")
    if storage_layout not in ALLOWED_STORAGE_LAYOUTS:
        raise WarehouseTwinLayoutEditError(
            "区域展示形式必须是货架、栈板地堆、混合或无栈板功能区"
        )
    normalized_area_code = str(erp_area_code or "").strip().upper() or None
    normalized_area_name = str(area_name or "").strip() or None
    if (formal_area_id is None) != (formal_floor_id is None):
        raise WarehouseTwinLayoutEditError("正式区域身份必须同时包含区域 ID 和楼层 ID")
    if formal_area_id is not None and (formal_area_id <= 0 or formal_floor_id <= 0):
        raise WarehouseTwinLayoutEditError("正式区域身份无效")
    if normalized_area_code is not None and len(normalized_area_code) > 30:
        raise WarehouseTwinLayoutEditError("正式区域编号最多 30 个字符")
    if max_rack_count is not None and (
        isinstance(max_rack_count, bool)
        or not isinstance(max_rack_count, int)
        or max_rack_count < 0
        or max_rack_count > 500
    ):
        raise WarehouseTwinLayoutEditError("区域最大货架数必须是 0 至 500 的整数")

    def mutate(floor: dict[str, Any]) -> dict[str, Any]:
        feature = _feature(floor, feature_id)
        if feature.get("feature_kind") != "zone":
            raise WarehouseTwinLayoutEditError("只有仓储区域可以设置存放策略")
        _ensure_version(feature, expected_version, "区域")
        feature["allowed_inventory_types"] = normalized_types
        feature["storage_layout"] = storage_layout
        if storage_layout == "rack":
            if max_rack_count is not None:
                feature["max_rack_count"] = max_rack_count
        else:
            feature.pop("max_rack_count", None)
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
        if legacy_v11_name_only:
            feature["legacy_v11_name_only"] = True
        else:
            feature.pop("legacy_v11_name_only", None)
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
        same_legacy_name_only = bool(
            mutation.value.get('legacy_v11_name_only')
        ) is bool(legacy_v11_name_only)
        same_max_rack_count = (
            storage_layout != "rack"
            or max_rack_count is None
            or mutation.value.get("max_rack_count") == max_rack_count
        )
        if not (
            same_types
            and same_layout
            and same_area
            and same_name
            and same_identity
            and same_legacy_name_only
            and same_max_rack_count
        ):
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


def _normalize_aisle_points(points: list[list[float]]) -> list[list[float]]:
    if len(points) < 2 or len(points) > 64:
        raise WarehouseTwinLayoutEditError("通道至少需要起点和终点")
    normalized: list[list[float]] = []
    for point in points:
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            raise WarehouseTwinLayoutEditError("通道坐标必须是二维坐标")
        try:
            x_mm, y_mm = float(point[0]), float(point[1])
        except (TypeError, ValueError, OverflowError) as error:
            raise WarehouseTwinLayoutEditError("通道坐标必须是数值") from error
        if not math.isfinite(x_mm) or not math.isfinite(y_mm):
            raise WarehouseTwinLayoutEditError("通道坐标必须是有限数值")
        if abs(x_mm) > 10_000_000 or abs(y_mm) > 10_000_000:
            raise WarehouseTwinLayoutEditError("通道坐标超出允许范围")
        normalized.append([round(x_mm, 3), round(y_mm, 3)])
    if any(normalized[index] == normalized[index + 1] for index in range(len(normalized) - 1)):
        raise WarehouseTwinLayoutEditError("通道相邻坐标不能重复")
    return normalized


def _zone_area_mm2(points: list[list[float]]) -> float:
    return round(abs(sum(
        points[index][0] * points[(index + 1) % len(points)][1]
        - points[(index + 1) % len(points)][0] * points[index][1]
        for index in range(len(points))
    )) / 2, 3)


def _aisle_area_mm2(points: list[list[float]], width_mm: float) -> float:
    length_mm = sum(
        math.hypot(
            points[index + 1][0] - points[index][0],
            points[index + 1][1] - points[index][1],
        )
        for index in range(len(points) - 1)
    )
    if length_mm <= 0:
        raise WarehouseTwinLayoutEditError("通道长度必须大于0")
    return round(length_mm * width_mm, 3)


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


def update_warehouse_twin_feature_geometry(
    floor_code: str,
    feature_id: str,
    *,
    expected_revision: str,
    expected_version: int,
    operation_key: str,
    points: list[list[float]],
    expected_kind: str | None = None,
    action: str = "feature.geometry.update",
    path: Path | None = None,
    prepare_ground: Callable[[dict[str, Any]], dict | None] | None = None,
    request_hash: str | None = None,
) -> LayoutMutation:
    def mutate(floor: dict[str, Any]) -> dict[str, Any]:
        feature = _feature(floor, feature_id)
        feature_kind = str(feature.get("feature_kind") or "")
        if feature_kind not in {"zone", "aisle"}:
            raise WarehouseTwinLayoutEditError("只有区域和通道可以调整布局")
        if expected_kind is not None and feature_kind != expected_kind:
            raise WarehouseTwinLayoutEditError("只有仓储区域可以修改实测边界")
        normalized_points = (
            _normalize_zone_points(points)
            if feature_kind == "zone"
            else _normalize_aisle_points(points)
        )
        if feature_kind == "zone":
            _reject_self_intersection(normalized_points)
        _ensure_version(feature, expected_version, '区域' if feature_kind == 'zone' else '通道')
        # Published zones and aisles stay locked against direct deletion, but an
        # administrator may reposition them through the versioned layout draft.
        # The published map remains byte-for-byte unchanged until validation and
        # publish, so planning never mutates the employee map in place.
        _ensure_points_within_floor(
            floor,
            normalized_points,
            label="区域边界" if feature_kind == "zone" else "通道",
        )
        if prepare_ground is not None and feature_kind == 'zone':
            ground = prepare_ground(feature)
            if ground is not None:
                feature['ground_location_draft'] = ground
        if request_hash is not None:
            feature['geometry_request_hash'] = request_hash
        feature['points'] = normalized_points
        feature['area_mm2'] = (
            _zone_area_mm2(normalized_points)
            if feature_kind == "zone"
            else _aisle_area_mm2(
                normalized_points, float(feature.get("width_mm") or 0)
            )
        )
        feature['status'] = 'candidate'
        feature['version'] = int(feature.get('version') or 1) + 1
        return dict(feature)

    mutation = _apply_mutation(
        floor_code,
        expected_revision=expected_revision,
        operation_key=operation_key,
        action=action,
        mutate=mutate,
        path=path,
    )
    normalized_replay_points = [
        [round(float(point[0]), 3), round(float(point[1]), 3)]
        for point in points
    ]
    if not mutation.applied and mutation.value.get('points') != normalized_replay_points:
        raise WarehouseTwinLayoutEditConflictError('该操作键已用于不同的区域边界')
    if not mutation.applied and request_hash is not None and mutation.value.get('geometry_request_hash') != request_hash:
        raise WarehouseTwinLayoutEditConflictError('该操作键已用于不同的货位调整')
    return mutation


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
    return update_warehouse_twin_feature_geometry(
        floor_code,
        feature_id,
        expected_revision=expected_revision,
        expected_version=expected_version,
        operation_key=operation_key,
        points=points,
        expected_kind="zone",
        action="zone.geometry.update",
        path=path,
    )


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


def _floor4_calibration_publish_blockers(document: dict[str, Any]) -> list[str]:
    blockers: list[str] = []
    try:
        authority = _authoritative_freight_elevator(document)
    except WarehouseTwinLayoutEditError as error:
        return [str(error)]
    floor = (document.get("floors") or {}).get("4F")
    if not isinstance(floor, dict):
        return ["正式地图缺少 4F"]
    metadata = floor.get("metadata")
    calibration = metadata.get("calibration") if isinstance(metadata, dict) else None
    if not isinstance(calibration, dict):
        return ["4F 缺少三点货梯标定证据"]
    if calibration.get("status") != "aligned" or calibration.get("applied") is not True:
        blockers.append("4F 尚未完成三点货梯标定")
    method = str(calibration.get("method") or "")
    fitted: dict[str, Any] | None = None
    try:
        fixed_rigid_contract = (
            method in {"three_point_rigid_2d", "doorway_heading_rigid_2d"}
            and float(calibration.get("scale")) == 1.0
            and calibration.get("mirror") is False
        )
    except (TypeError, ValueError):
        fixed_rigid_contract = False
    if not fixed_rigid_contract:
        blockers.append("4F 标定方法必须为不缩放、不镜像的二维刚体拟合")
    floor3 = (document.get("floors") or {}).get("3F")
    authority_floor_revision = (
        str(floor3.get("revision") or "") if isinstance(floor3, dict) else ""
    )
    audit = calibration.get("audit")
    if not isinstance(audit, dict) or not all(
        str(audit.get(key) or "").strip()
        for key in ("operation_key", "applied_at")
    ):
        blockers.append("4F 标定审计证据不完整")
    elif (
        audit.get("source_floor_code") != "4F"
        or audit.get("authority_floor_code") != "3F"
        or audit.get("authority_feature_code") != "LIFT-002"
        or audit.get("authority_feature_id") != authority.get("id")
        or audit.get("authority_floor_revision") != authority_floor_revision
    ):
        blockers.append("4F 标定审计证据与当前 3F 权威对象不一致")
    try:
        expected_targets = (
            _doorway_target_points(authority)
            if method == "doorway_heading_rigid_2d"
            else _canonical_freight_elevator_target_points(authority)
        )
        if calibration.get("canonical_target_points") != expected_targets:
            blockers.append("4F 标定目标点与当前 3F 权威货梯几何不一致")
        source_points = _three_calibration_points(
            calibration.get("source_points"), label="4F 标定源点"
        )
        target_points = _three_calibration_points(
            expected_targets, label="4F 标定目标点"
        )
        fitted = (
            _fit_floor4_doorway_transform(source_points, target_points)
            if method == "doorway_heading_rigid_2d"
            else _fit_floor4_rigid_transform(source_points, target_points)
        )
        if float(calibration.get("rotation_deg")) != round(
            float(fitted["rotation_deg"]), 9
        ):
            blockers.append("4F 标定旋转证据与三点拟合不一致")
        if calibration.get("translation_mm") != _rounded_point(
            fitted["translation_mm"]
        ):
            blockers.append("4F 标定位移证据与三点拟合不一致")
        expected_residuals = [round(value, 6) for value in fitted["residuals_mm"]]
        if calibration.get("residuals_mm") != expected_residuals:
            blockers.append("4F 标定逐点残差证据与三点拟合不一致")
        if calibration.get("max_residual_mm") != round(
            float(fitted["max_residual_mm"]), 6
        ):
            blockers.append("4F 标定最大残差证据与三点拟合不一致")
        if calibration.get("rmse_residual_mm") != round(
            float(fitted["rmse_residual_mm"]), 6
        ):
            blockers.append("4F 标定均方根残差证据与三点拟合不一致")
        if calibration.get("thresholds_mm") != {
            "max_residual": FLOOR4_CALIBRATION_MAX_RESIDUAL_MM,
            "rmse": FLOOR4_CALIBRATION_RMSE_MM,
        }:
            blockers.append("4F 标定发布阈值证据与系统固定阈值不一致")
        if method == "doorway_heading_rigid_2d" and fitted is not None:
            doorway_evidence = {
                "source_footprint_points": [
                    _rounded_point(point) for point in fitted["source_footprint"]
                ],
                "transformed_footprint_points": [
                    _rounded_point(point)
                    for point in fitted["transformed_footprint"]
                ],
                "measured_door_width_mm": round(
                    float(fitted["door_width_mm"]), 3
                ),
                "measured_depth_mm": round(float(fitted["depth_mm"]), 3),
                "authority_door_width_mm": round(
                    float(fitted["authority_door_width_mm"]), 3
                ),
                "authority_depth_mm": round(
                    float(fitted["authority_depth_mm"]), 3
                ),
                "inside_along_offset_mm": round(
                    float(fitted["inside_along_offset_mm"]), 3
                ),
                "anchor_residual_mm": round(
                    float(fitted["anchor_residual_mm"]), 6
                ),
                "heading_residual_deg": round(
                    float(fitted["heading_residual_deg"]), 9
                ),
                "dimension_policy": "floor_specific_measured_footprint",
            }
            for key, expected_value in doorway_evidence.items():
                if calibration.get(key) != expected_value:
                    blockers.append(f"4F 货梯门口标定证据 {key} 不一致")
    except (TypeError, ValueError, WarehouseTwinLayoutEditError) as error:
        blockers.append(f"4F 标定证据无效：{error}")

    lifts = [
        item
        for item in floor.get("features") or []
        if isinstance(item, dict) and item.get("feature_code") == "LIFT-002"
    ]
    if len(lifts) != 1:
        blockers.append("4F 必须且只能物化一个货梯 LIFT-002")
    else:
        lift = lifts[0]
        if method == "doorway_heading_rigid_2d" and fitted is not None:
            try:
                expected_lift = _floor4_measured_lift(
                    authority,
                    floor_layout_id=floor.get("layout_id"),
                    transform=fitted,
                    version=int(lift.get("version") or 0),
                )
                measured_keys = {
                    "id",
                    "layout_id",
                    "feature_code",
                    "feature_kind",
                    "subtype",
                    "points",
                    "width_mm",
                    "area_mm2",
                    "source",
                    "status",
                    "is_locked",
                    "measured_footprint_points",
                    "measured_door_width_mm",
                    "measured_depth_mm",
                    "cross_floor_authority_feature_id",
                }
                if any(lift.get(key) != expected_lift.get(key) for key in measured_keys):
                    blockers.append("4F 货梯 LIFT-002 与本层实测门口几何不一致")
            except (TypeError, ValueError, WarehouseTwinLayoutEditError) as error:
                blockers.append(f"4F 货梯 LIFT-002 实测几何无效：{error}")
        elif method != "doorway_heading_rigid_2d":
            ignored = {"id", "layout_id", "version"}
            authority_contract = {
                key: value for key, value in authority.items() if key not in ignored
            }
            lift_contract = {key: lift.get(key) for key in authority_contract}
            if lift_contract != authority_contract:
                blockers.append("4F 货梯 LIFT-002 与 3F 权威对象不一致")
        if lift.get("is_locked") is not True:
            blockers.append("4F 货梯 LIFT-002 未锁定")
    canonical = floor.get("canonical_authority")
    if (
        not isinstance(canonical, dict)
        or canonical.get("feature_code") != "LIFT-002"
        or canonical.get("materialized_on_4f") is not True
        or canonical.get("status") != "applied"
    ):
        blockers.append("4F 货梯权威物化状态不完整")
    elif canonical.get("geometry_policy") != (
        "floor_specific_measured_footprint"
        if method == "doorway_heading_rigid_2d"
        else "authority_geometry_copy"
    ):
        blockers.append("4F 货梯权威几何策略与标定方式不一致")
    return blockers


def _validate_document_for_publish(
    document: dict[str, Any], *, publish_floor_code: str | None = None
) -> tuple[list[str], list[str]]:
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

    if publish_floor_code is None or str(publish_floor_code).upper() == "4F":
        if "4F" in floors:
            blockers.extend(_floor4_calibration_publish_blockers(document))
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
        dirty_floor_codes: list[str] = []
        has_draft = False
        if draft is not None:
            candidate = draft["floors"].get(normalized)
            if not isinstance(candidate, dict):
                raise WarehouseTwinLayoutEditError(f"布局草稿缺少 {normalized}")
            floor = candidate
            meta = dict(draft.get("draft_meta") or {})
            dirty_floor_codes = _dirty_floor_codes(draft)
            has_draft = normalized in dirty_floor_codes
        floor_revision = str(floor.get("revision") or "")
        validation = (
            _floor_validation(draft, normalized, floor_revision)
            if draft is not None and has_draft
            else None
        )
        floor_status = "none"
        if has_draft:
            floor_status = (
                "validated"
                if validation is not None and not list(validation.get("blockers") or [])
                else "draft"
            )
        visible_floor = keep_measured_floor_features(deepcopy(floor))
        return {
            **visible_floor,
            "generated_at": (draft if draft is not None else published).get("generated_at"),
            "projection_notice": (
                "当前为本楼层管理员布局草稿；正式库存数量仍以 ERP 库存账为准。"
                if has_draft
                else "当前楼层没有待发布草稿；正式库存数量仍以 ERP 库存账为准。"
            ),
            "draft_control": {
                "has_draft": has_draft,
                "has_other_floor_drafts": any(
                    code != normalized for code in dirty_floor_codes
                ),
                "dirty_floor_codes": dirty_floor_codes,
                "status": floor_status,
                "published_revision": str(published_floor.get("revision") or ""),
                "draft_revision": floor_revision if has_draft else None,
                "base_published_sha256": meta.get("base_published_sha256"),
                "created_at": meta.get("created_at"),
                "updated_at": meta.get("updated_at"),
                "validated_at": validation.get("validated_at") if validation else None,
                "blockers": list(validation.get("blockers") or []) if validation else [],
                "warnings": list(validation.get("warnings") or []) if validation else [],
            },
        }


def load_published_warehouse_twin_floor_for_edit(
    floor_code: str,
    *,
    published_path: Path | None = None,
) -> dict[str, Any]:
    """Read the exact published source used by the editor, ignoring its draft."""

    normalized = _normalize_floor_code(floor_code)
    published_source = _published_layout_paths(published_path).source
    with _LAYOUT_EDIT_LOCK:
        published = _read_document(published_source)
        floor = (published.get("floors") or {}).get(normalized)
        if not isinstance(floor, dict):
            raise WarehouseTwinLayoutEditNotFoundError(
                f"数字孪生平面缺少 {normalized}"
            )
        return {
            **keep_measured_floor_features(deepcopy(floor)),
            "generated_at": published.get("generated_at"),
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
    additional_warnings: list[str] | None = None,
    published_path: Path | None = None,
    draft_path: Path | None = None,
) -> LayoutDraftAction:
    normalized = _normalize_floor_code(floor_code)
    published_target = _published_layout_paths(published_path).source
    draft_target = draft_path or TWIN_LAYOUT_DRAFT_PATH
    with _LAYOUT_EDIT_LOCK:
        published = _read_document(published_target)
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
        if normalized not in _dirty_floor_codes(draft):
            raise WarehouseTwinLayoutEditNotFoundError("当前楼层没有可校验的布局草稿")

        # Validate only the requested floor on top of the current published
        # document.  Other floors may have independent drafts, but they must
        # neither be published nor block this floor's workflow.
        candidate = deepcopy(published)
        candidate["floors"][normalized] = deepcopy(floor)
        blockers, warnings = _validate_document_for_publish(
            candidate, publish_floor_code=normalized
        )
        warnings = list(dict.fromkeys([*warnings, *(additional_warnings or [])]))
        meta = draft["draft_meta"]
        now = _utc_iso()
        meta["updated_at"] = now
        meta["validation_blockers"] = blockers
        meta["validation_warnings"] = warnings
        validations = dict(meta.get("floor_validations") or {})
        validations[normalized] = {
            "revision": str(floor.get("revision") or ""),
            "validated_at": now if not blockers else None,
            "blockers": blockers,
            "warnings": warnings,
        }
        meta["floor_validations"] = validations
        validated_revisions = dict(meta.get("validated_floor_revisions") or {})
        if blockers:
            validated_revisions.pop(normalized, None)
            meta.pop("validated_at", None)
        else:
            meta["validated_at"] = now
            validated_revisions[normalized] = str(floor.get("revision") or "")
        if validated_revisions:
            meta["validated_floor_revisions"] = validated_revisions
        else:
            meta.pop("validated_floor_revisions", None)
        _refresh_draft_status(draft)
        floor_status = "draft" if blockers else "validated"
        _write_document(draft_target, draft)
        return LayoutDraftAction(
            value={
                "status": floor_status,
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
    additional_warnings: list[str] | None = None,
    mold_location_reassignment_count: int = 0,
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
            receipts = list(replay_meta.get("publish_receipts") or [])
            last_publish = replay_meta.get("last_publish") or {}
            if last_publish and not receipts:
                receipts.append(last_publish)
            for receipt in receipts:
                if receipt.get("operation_key") != normalized_key:
                    continue
                receipt_floor = str(
                    receipt.get("floor_code")
                    or (receipt.get("result") or {}).get("floor_code")
                    or ""
                ).upper()
                if receipt_floor and receipt_floor != normalized:
                    raise WarehouseTwinLayoutEditConflictError(
                        "该发布操作键已用于其他楼层"
                    )
                if (
                    receipt.get("expected_published_revision")
                    not in (None, expected_published_revision)
                    or receipt.get("expected_draft_revision")
                    not in (None, expected_draft_revision)
                ):
                    raise WarehouseTwinLayoutEditConflictError(
                        "该发布操作键已用于不同版本的地图草稿"
                    )
                return LayoutDraftAction(
                    value=dict(receipt.get("result") or {}), applied=False
                )

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
        dirty_floors = _dirty_floor_codes(draft)
        if normalized not in dirty_floors:
            raise WarehouseTwinLayoutEditNotFoundError("当前楼层没有可发布的布局草稿")
        validated_revisions = meta.get("validated_floor_revisions") or {}
        if (
            str(validated_revisions.get(normalized) or "")
            != expected_draft_revision
        ):
            raise WarehouseTwinLayoutEditConflictError("请先校验当前楼层布局草稿，再执行发布")

        candidate = deepcopy(published)
        candidate["floors"][normalized] = deepcopy(draft_floor)
        candidate["generated_at"] = draft.get("generated_at") or _utc_iso()
        blockers, warnings = _validate_document_for_publish(
            candidate, publish_floor_code=normalized
        )
        warnings = list(dict.fromkeys([*warnings, *(additional_warnings or [])]))
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

        candidate.pop("draft_meta", None)
        draft_before_publish = draft_target.read_bytes()
        try:
            _write_document(published_target, candidate)
            written = _read_document(published_target)
            written_blockers, _written_warnings = _validate_document_for_publish(
                written, publish_floor_code=normalized
            )
            if written != candidate or written_blockers:
                raise WarehouseTwinLayoutEditError("运行地图发布后内容校验失败")
            if published_source != published_target and _path_sha256(published_source) != old_sha256:
                raise WarehouseTwinLayoutEditError("静态地图基线发生变化，已停止发布")
            published_sha256 = _path_sha256(published_target)
            remaining_dirty_floors = [
                code for code in dirty_floors if code != normalized
            ]
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
                "mold_location_reassignment_count": max(
                    0, int(mold_location_reassignment_count)
                ),
                "mold_location_changed": bool(mold_location_reassignment_count),
                "published_at": _utc_iso(),
                "remaining_draft_floor_codes": remaining_dirty_floors,
            }
            receipt = {
                "operation_key": normalized_key,
                "floor_code": normalized,
                "expected_published_revision": expected_published_revision,
                "expected_draft_revision": expected_draft_revision,
                "result": result,
            }
            persisted_draft = deepcopy(draft)
            persisted_meta = persisted_draft["draft_meta"]
            publish_receipts = list(persisted_meta.get("publish_receipts") or [])
            publish_receipts.append(receipt)
            persisted_meta["publish_receipts"] = publish_receipts[-50:]
            persisted_meta["last_publish"] = receipt
            persisted_meta["published_at"] = result["published_at"]
            if remaining_dirty_floors:
                # Consume only this floor.  Other floors stay as independent
                # drafts and are rebased onto the newly published document.
                persisted_draft["floors"][normalized] = deepcopy(
                    written["floors"][normalized]
                )
                persisted_draft["generated_at"] = result["published_at"]
                persisted_meta["status"] = "draft"
                persisted_meta["updated_at"] = result["published_at"]
                persisted_meta["base_published_sha256"] = published_sha256
                persisted_meta["base_floor_revisions"] = {
                    code: str(item.get("revision") or "")
                    for code, item in written["floors"].items()
                    if isinstance(item, dict)
                }
                # A publish changes the base document.  Require every
                # remaining floor to be validated again against that base.
                for key in (
                    "validated_at",
                    "validated_floor_revisions",
                    "floor_validations",
                    "validation_blockers",
                    "validation_warnings",
                ):
                    persisted_meta.pop(key, None)
            else:
                persisted_meta["status"] = "published"
            _write_document(draft_target, persisted_draft)
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
    published_path: Path | None = None,
    draft_path: Path | None = None,
) -> LayoutDraftAction:
    normalized = _normalize_floor_code(floor_code)
    published_target = _published_layout_paths(published_path).source
    target = draft_path or TWIN_LAYOUT_DRAFT_PATH
    with _LAYOUT_EDIT_LOCK:
        if not target.is_file():
            return LayoutDraftAction(
                value={"status": "none", "floor_code": normalized, "inventory_changed": False},
                applied=False,
            )
        draft = _active_draft_document_unlocked(
            published_path=published_target,
            draft_path=target,
            create=False,
        )
        if draft is None:
            return LayoutDraftAction(
                value={"status": "none", "floor_code": normalized, "inventory_changed": False},
                applied=False,
            )
        floor = draft["floors"].get(normalized)
        if not isinstance(floor, dict):
            raise WarehouseTwinLayoutEditNotFoundError(f"布局草稿缺少 {normalized}")
        if str(floor.get("revision") or "") != str(expected_revision or ""):
            raise WarehouseTwinLayoutEditConflictError("布局草稿已更新，请刷新后再放弃")
        dirty_floors = _dirty_floor_codes(draft)
        if normalized not in dirty_floors:
            return LayoutDraftAction(
                value={"status": "none", "floor_code": normalized, "inventory_changed": False},
                applied=False,
            )
        remaining_dirty_floors = [code for code in dirty_floors if code != normalized]
        if remaining_dirty_floors:
            published = _read_document(published_target)
            published_floor = published["floors"].get(normalized)
            if not isinstance(published_floor, dict):
                raise WarehouseTwinLayoutEditNotFoundError(
                    f"数字孪生平面缺少 {normalized}"
                )
            draft["floors"][normalized] = deepcopy(published_floor)
            draft["generated_at"] = _utc_iso()
            meta = draft["draft_meta"]
            validated_revisions = dict(meta.get("validated_floor_revisions") or {})
            validated_revisions.pop(normalized, None)
            if validated_revisions:
                meta["validated_floor_revisions"] = validated_revisions
            else:
                meta.pop("validated_floor_revisions", None)
            floor_validations = dict(meta.get("floor_validations") or {})
            floor_validations.pop(normalized, None)
            if floor_validations:
                meta["floor_validations"] = floor_validations
            else:
                meta.pop("floor_validations", None)
            meta["updated_at"] = draft["generated_at"]
            for key in ("validated_at", "validation_blockers", "validation_warnings"):
                meta.pop(key, None)
            _refresh_draft_status(draft)
            _write_document(target, draft)
        else:
            target.unlink()
        return LayoutDraftAction(
            value={
                "status": "discarded",
                "floor_code": normalized,
                "remaining_draft_floor_codes": remaining_dirty_floors,
                "inventory_changed": False,
            },
            applied=True,
        )


def rebuild_stale_warehouse_twin_layout_draft(
    floor_code: str,
    *,
    expected_published_revision: str,
    published_path: Path | None = None,
    draft_path: Path | None = None,
) -> LayoutDraftAction:
    """Replace only a stale draft with a fresh snapshot of the published map.

    The ordinary draft loader deliberately fails closed when the published map
    changed underneath a saved draft.  This explicit recovery operation keeps
    that protection while giving an administrator one audited way to abandon
    the obsolete file.  It never changes the published map or inventory.
    """

    normalized = _normalize_floor_code(floor_code)
    published_target = _published_layout_paths(published_path).source
    target = draft_path or TWIN_LAYOUT_DRAFT_PATH
    with _LAYOUT_EDIT_LOCK:
        published = _read_document(published_target)
        published_floor = published["floors"].get(normalized)
        if not isinstance(published_floor, dict):
            raise WarehouseTwinLayoutEditNotFoundError(
                f"数字孪生平面缺少 {normalized}"
            )
        published_revision = str(published_floor.get("revision") or "")
        if published_revision != str(expected_published_revision or ""):
            raise WarehouseTwinLayoutEditConflictError(
                "正式地图版本已变化，请刷新后重新放弃旧草稿"
            )
        if not target.is_file():
            return LayoutDraftAction(
                value={
                    "status": "none",
                    "floor_code": normalized,
                    "published_revision": published_revision,
                    "inventory_changed": False,
                },
                applied=False,
            )

        stale_draft_sha256 = _path_sha256(target)
        draft = _read_document(target)
        meta = draft.get("draft_meta")
        current_published_sha256 = _path_sha256(published_target)
        base_published_sha256 = (
            str(meta.get("base_published_sha256") or "")
            if isinstance(meta, dict)
            else ""
        )
        if (
            isinstance(meta, dict)
            and meta.get("status") in {"draft", "validated"}
            and base_published_sha256 == current_published_sha256
        ):
            return LayoutDraftAction(
                value={
                    "status": "current",
                    "floor_code": normalized,
                    "published_revision": published_revision,
                    "inventory_changed": False,
                },
                applied=False,
            )

        replacement = _new_draft_document(published_target)
        replacement_meta = replacement["draft_meta"]
        replacement_meta["replaced_stale_draft_sha256"] = stale_draft_sha256
        replacement_meta["replacement_reason"] = "published_map_changed"
        _write_document(target, replacement)
        return LayoutDraftAction(
            value={
                "status": "rebuilt",
                "floor_code": normalized,
                "published_revision": published_revision,
                "draft_revision": published_revision,
                "stale_draft_sha256": stale_draft_sha256,
                "base_published_sha256": current_published_sha256,
                "inventory_changed": False,
            },
            applied=True,
        )
