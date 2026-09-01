from __future__ import annotations

from copy import deepcopy
import json
import re
from typing import Iterable

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.models.warehouse_inventory import WarehouseArea, WarehouseFloor
from app.services.mold_location import ONE_FLOOR_MOLD_RACKS, describe_mold_location


class ArchivedWarehouseAreaTargetError(ValueError):
    """A current business write targets an area that was durably archived."""


def _normalized(value: object) -> str:
    return str(value or "").strip().upper()


def archived_area_tombstones_for_floor(
    db: Session,
    *,
    floor_code: str,
) -> list[dict]:
    """Load durable archive identities, including objects frozen in the snapshot.

    The frozen object identities remain authoritative after the derived JSON map
    has been cleaned.  Business writes must therefore never infer archive state
    only by comparing the raw and overlaid map.
    """

    floor = db.scalar(
        select(WarehouseFloor).where(
            func.upper(WarehouseFloor.floor_code) == _normalized(floor_code)
        )
    )
    if floor is None:
        alias_match = re.fullmatch(r"(?:F)?(\d+)(?:F)?", _normalized(floor_code))
        if alias_match is not None:
            floor = db.scalar(
                select(WarehouseFloor).where(
                    WarehouseFloor.floor_number == int(alias_match.group(1))
                )
            )
    if floor is None:
        return []
    areas = list(
        db.scalars(
            select(WarehouseArea)
            .where(WarehouseArea.floor_id == floor.id)
            .options(selectinload(WarehouseArea.storage_policy))
            .order_by(WarehouseArea.id)
        ).all()
    )
    tombstones: list[dict] = []
    for area in areas:
        policy = area.storage_policy
        if area.construction_status != "archived" and not (
            policy is not None and policy.status == "archived"
        ):
            continue
        feature_ids = {
            str(policy.map_feature_id or "").strip() if policy is not None else ""
        } - {""}
        feature_codes: set[str] = set()
        area_codes = {_normalized(area.area_code)} - {""}
        rack_ids: set[str] = set()
        mold_rack_codes: set[str] = set()
        try:
            snapshot = json.loads(
                policy.archive_feature_snapshot_json or "{}"
                if policy is not None
                else "{}"
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            snapshot = {}
        object_groups = []
        if isinstance(snapshot, dict):
            for key in ("effective_layout_objects", "published_layout_objects"):
                value = snapshot.get(key)
                if isinstance(value, dict):
                    object_groups.append(value)
            legacy_feature = snapshot.get("feature")
            if isinstance(legacy_feature, dict):
                object_groups.append({"features": [legacy_feature]})
        for objects in object_groups:
            for feature in objects.get("features") or []:
                feature_id = str(feature.get("id") or "").strip()
                if feature_id:
                    feature_ids.add(feature_id)
                feature_code = _normalized(feature.get("feature_code"))
                if feature_code:
                    feature_codes.add(feature_code)
                feature_area_code = _normalized(feature.get("erp_area_code"))
                if feature_area_code:
                    area_codes.add(feature_area_code)
            for rack in objects.get("racks") or []:
                rack_id = str(rack.get("id") or "").strip()
                if rack_id:
                    rack_ids.add(rack_id)
                rack_area_code = _normalized(rack.get("area_code"))
                if rack_area_code:
                    area_codes.add(rack_area_code)
                mold_rack_code = _normalized(rack.get("mold_rack_code"))
                if mold_rack_code:
                    mold_rack_codes.add(mold_rack_code)
        tombstones.append(
            {
                "feature_id": str(policy.map_feature_id or "").strip()
                if policy is not None
                else None,
                "area_code": area.area_code,
                "area_id": area.id,
                "feature_ids": sorted(feature_ids),
                "feature_codes": sorted(feature_codes),
                "area_codes": sorted(area_codes),
                "rack_ids": sorted(rack_ids),
                "mold_rack_codes": sorted(mold_rack_codes),
            }
        )
    return tombstones


def archived_area_identity_sets(
    db: Session,
    *,
    floor_code: str,
) -> dict[str, set[str]]:
    rows = archived_area_tombstones_for_floor(db, floor_code=floor_code)
    return {
        key: {
            str(value).strip().upper() if key != "feature_ids" and key != "rack_ids" else str(value).strip()
            for row in rows
            for value in row.get(key) or []
            if str(value or "").strip()
        }
        for key in (
            "feature_ids",
            "feature_codes",
            "area_codes",
            "rack_ids",
            "mold_rack_codes",
        )
    }


def warehouse_asset_location_floor(
    *,
    asset_kind: str,
    location_text: str,
) -> str | None:
    if asset_kind == "printing_plate":
        return "1F"
    if asset_kind == "mold":
        floor = str(describe_mold_location(location_text).get("floor") or "").upper()
        return floor or None
    return None


def assert_warehouse_asset_location_not_archived(
    db: Session,
    *,
    asset_kind: str,
    location_text: str,
) -> None:
    """Fail closed when an asset write names a durable archived map target."""

    floor_code = warehouse_asset_location_floor(
        asset_kind=asset_kind,
        location_text=location_text,
    )
    if not floor_code:
        return
    identities = archived_area_identity_sets(db, floor_code=floor_code)
    if not any(identities.values()):
        return
    if asset_kind == "printing_plate":
        target_code = "ZONE-1F-PLATE-002"
        if target_code in identities["feature_codes"] or target_code in identities["feature_ids"]:
            raise ArchivedWarehouseAreaTargetError(
                "该挂板区域已经归档，不能新增、启用或移入挂板。"
            )
        return

    guide = describe_mold_location(location_text)
    if guide.get("kind") == "archive_area":
        area_code = _normalized(guide.get("area"))
        if area_code in identities["area_codes"] or area_code in identities["feature_codes"]:
            raise ArchivedWarehouseAreaTargetError(
                "该模具封存区域已经归档，不能再移入模具。"
            )
        return
    rack_number = guide.get("rack")
    if rack_number is None:
        return
    rack_code = f"R{int(rack_number):02d}"
    confirmed_zone_code = next(
        (
            _normalized(item.get("zone_code"))
            for item in ONE_FLOOR_MOLD_RACKS
            if int(item.get("rack") or 0) == int(rack_number)
        ),
        "",
    )
    if (
        rack_code in identities["mold_rack_codes"]
        or confirmed_zone_code in identities["feature_codes"]
        or confirmed_zone_code in identities["feature_ids"]
    ):
        raise ArchivedWarehouseAreaTargetError(
            "该模具货架所在区域已经归档，不能新增、恢复或移入模具。"
        )


def partition_archived_area_layout(
    floor_layout: dict,
    *,
    tombstones: Iterable[dict],
) -> dict[str, list[dict]]:
    """Partition one floor by durable formal-area archive identities.

    Old measured layouts are not uniform: recent racks point at a zone by
    ``area_feature_id`` while legacy racks only carry the ERP ``area_code``.
    Pallets likewise use either a zone id or a zone/feature code.  Treat the
    formal feature id and ERP area code as one tombstone identity so none of
    those legacy children can leak back into an operational projection.
    """

    tombstone_rows = [dict(item or {}) for item in tombstones]
    feature_ids = {
        value
        for item in tombstone_rows
        for value in (
            [str(item.get("feature_id") or "").strip()]
            + [str(raw or "").strip() for raw in item.get("feature_ids") or []]
        )
        if value
    }
    feature_codes = {
        _normalized(value)
        for item in tombstone_rows
        for value in item.get("feature_codes") or []
        if _normalized(value)
    }
    area_codes = {
        value
        for item in tombstone_rows
        for value in (
            [_normalized(item.get("area_code"))]
            + [_normalized(raw) for raw in item.get("area_codes") or []]
        )
        if value
    }
    rack_ids = {
        str(value or "").strip()
        for item in tombstone_rows
        for value in item.get("rack_ids") or []
        if str(value or "").strip()
    }

    removed_features: list[dict] = []
    retained_features: list[dict] = []
    for raw in floor_layout.get("features") or []:
        feature = dict(raw)
        feature_id = str(feature.get("id") or "").strip()
        feature_area_code = _normalized(feature.get("erp_area_code"))
        feature_code = _normalized(feature.get("feature_code"))
        if (
            feature_id in feature_ids
            or feature_code in feature_codes
            or (feature_area_code and feature_area_code in area_codes)
        ):
            removed_features.append(feature)
            feature_ids.add(feature_id)
            if feature_code:
                feature_codes.add(feature_code)
            if feature_area_code:
                area_codes.add(feature_area_code)
            continue
        retained_features.append(feature)

    layout_identity_codes = feature_codes | area_codes
    removed_racks: list[dict] = []
    retained_racks: list[dict] = []
    for raw in floor_layout.get("racks") or []:
        rack = dict(raw)
        if (
            str(rack.get("id") or "").strip() in rack_ids
            or
            str(rack.get("area_feature_id") or "").strip() in feature_ids
            or _normalized(rack.get("area_code")) in layout_identity_codes
        ):
            removed_racks.append(rack)
        else:
            retained_racks.append(rack)

    removed_pallets: list[dict] = []
    retained_pallets: list[dict] = []
    for raw in floor_layout.get("pallets") or []:
        pallet = dict(raw)
        if (
            str(pallet.get("zone_id") or "").strip() in feature_ids
            or _normalized(pallet.get("zone_code")) in layout_identity_codes
        ):
            removed_pallets.append(pallet)
        else:
            retained_pallets.append(pallet)

    return {
        "features": retained_features,
        "racks": retained_racks,
        "pallets": retained_pallets,
        "removed_features": removed_features,
        "removed_racks": removed_racks,
        "removed_pallets": removed_pallets,
    }


def filter_archived_area_layout(
    floor_layout: dict,
    *,
    tombstones: Iterable[dict],
) -> dict:
    partition = partition_archived_area_layout(
        floor_layout,
        tombstones=tombstones,
    )
    return {
        **floor_layout,
        "features": partition["features"],
        "racks": partition["racks"],
        "pallets": partition["pallets"],
    }


def retire_archived_area_layout_objects(
    floor_layout: dict,
    *,
    tombstones: Iterable[dict],
    retired_at: str,
) -> dict[str, list[dict]]:
    """Remove tombstoned objects from a draft and preserve their audit facts."""

    partition = partition_archived_area_layout(
        floor_layout,
        tombstones=tombstones,
    )
    floor_layout["features"] = partition["features"]
    floor_layout["racks"] = partition["racks"]
    floor_layout["pallets"] = partition["pallets"]
    reason = "正式区域已归档；从当前规划投影剔除，历史几何仍保留在归档快照"
    for source_key, target_key in (
        ("removed_features", "retired_features"),
        ("removed_racks", "retired_racks"),
        ("removed_pallets", "retired_pallets"),
    ):
        existing = list(floor_layout.get(target_key) or [])
        existing_ids = {
            str(item.get("id") or "").strip()
            for item in existing
            if str(item.get("id") or "").strip()
        }
        for raw in partition[source_key]:
            item = deepcopy(raw)
            identity = str(item.get("id") or "").strip()
            if identity and identity in existing_ids:
                continue
            item["retired_at"] = retired_at
            item["retired_reason"] = reason
            existing.append(item)
            if identity:
                existing_ids.add(identity)
        floor_layout[target_key] = existing
    return partition
