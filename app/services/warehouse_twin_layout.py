from __future__ import annotations

import json
import os
from functools import lru_cache
from math import isfinite
from pathlib import Path


_PROJECT_ROOT = Path(__file__).resolve().parents[2]
TWIN_LAYOUT_PATH = (
    _PROJECT_ROOT / "static" / "factory_maps" / "twin_layout_v1.json"
)
TWIN_LAYOUT_RUNTIME_PATH = Path(
    os.getenv(
        "ERP_TWIN_LAYOUT_RUNTIME_PATH",
        str(_PROJECT_ROOT / "data" / "layout_runtime" / "twin_layout_v1.json"),
    )
).resolve(strict=False)


class WarehouseTwinLayoutNotFoundError(LookupError):
    pass


def _zone_inside_measured_bounds(feature: dict, bounds: dict) -> bool:
    points = feature.get("points") or []
    if len(points) < 3:
        return False
    try:
        min_x = float(bounds["min_x"])
        min_y = float(bounds["min_y"])
        max_x = float(bounds["max_x"])
        max_y = float(bounds["max_y"])
        return all(
            min_x <= float(point[0]) <= max_x
            and min_y <= float(point[1]) <= max_y
            for point in points
        )
    except (KeyError, TypeError, ValueError, IndexError):
        return False


def keep_measured_floor_features(floor: dict) -> dict:
    """Keep 1F operational zones inside the authoritative measured envelope.

    Historical source files may retain superseded projected zones so their
    identities are auditable. They must not become a second visible map or a
    source of formal warehouse areas.
    """

    result = dict(floor)
    features = list(floor.get("features") or [])
    if str(floor.get("floor_code") or "").upper() != "1F":
        result["features"] = features
        return result
    bounds = floor.get("bounds_mm") or {}
    kept: list[dict] = []
    excluded: list[dict] = []
    for feature in features:
        if feature.get("feature_kind") != "zone" or _zone_inside_measured_bounds(
            feature, bounds
        ):
            kept.append(feature)
            continue
        excluded.append(
            {
                "id": str(feature.get("id") or ""),
                "feature_code": str(feature.get("feature_code") or ""),
                "name": str(feature.get("name") or ""),
                "reason": "outside_measured_bounds",
            }
        )
    result["features"] = kept
    result["excluded_out_of_bounds_zones"] = excluded
    return result


def resolve_warehouse_twin_layout_path(path: Path | None = None) -> Path:
    """Use the published runtime copy when present; never mask a damaged copy."""

    if path is not None:
        return path
    if TWIN_LAYOUT_RUNTIME_PATH.exists():
        return TWIN_LAYOUT_RUNTIME_PATH
    if os.getenv("ERP_UAT_ROOT"):
        # A UAT run must never fall back to a shared code-tree layout.
        return TWIN_LAYOUT_RUNTIME_PATH
    return TWIN_LAYOUT_PATH


def load_warehouse_twin_floor(
    floor_code: str,
    *,
    path: Path | None = None,
) -> dict:
    normalized = str(floor_code or "").strip().upper()
    if normalized not in {"1F", "3F"}:
        raise WarehouseTwinLayoutNotFoundError(f"尚未配置 {normalized or floor_code} 数字孪生平面")
    target = resolve_warehouse_twin_layout_path(path)
    if not target.is_file():
        raise WarehouseTwinLayoutNotFoundError("数字孪生平面资产尚未导出")
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        if path is None and target == TWIN_LAYOUT_RUNTIME_PATH:
            raise ValueError(
                "数字孪生运行地图损坏，已停止读取；请恢复运行地图后重试"
            ) from error
        raise ValueError("数字孪生平面资产无法读取") from error
    if payload.get("schema_version") != 1:
        raise ValueError("数字孪生平面资产版本不受支持")
    floor = (payload.get("floors") or {}).get(normalized)
    if floor is None or floor.get("floor_code") != normalized:
        raise WarehouseTwinLayoutNotFoundError(f"数字孪生平面缺少 {normalized}")
    measured_floor = keep_measured_floor_features(floor)
    return {
        **measured_floor,
        "generated_at": payload.get("generated_at"),
        "projection_notice": "仅投影已确认或人工候选空间；正式库存数量仍以 ERP 库存账为准。",
    }


@lru_cache(maxsize=8)
def _published_floor_identity_cached(
    path_text: str,
    modified_ns: int,
    file_size: int,
    floor_code: str,
) -> dict:
    del modified_ns, file_size
    floor = load_warehouse_twin_floor(floor_code, path=Path(path_text))

    def valid_zone_geometry(feature: dict) -> bool:
        points = feature.get("points") or []
        if not isinstance(points, list) or len(points) < 3:
            return False
        try:
            normalized = [
                (float(point[0]), float(point[1]))
                for point in points
                if isinstance(point, (list, tuple)) and len(point) >= 2
            ]
        except (TypeError, ValueError, OverflowError):
            return False
        if len(normalized) != len(points) or any(
            not isfinite(x) or not isfinite(y) for x, y in normalized
        ):
            return False
        if len(set(normalized)) < 3:
            return False
        twice_area = abs(
            sum(
                x1 * y2 - x2 * y1
                for (x1, y1), (x2, y2) in zip(
                    normalized,
                    normalized[1:] + normalized[:1],
                    strict=True,
                )
            )
        )
        return twice_area > 0

    zones = [
        feature
        for feature in floor.get("features") or []
        if feature.get("feature_kind") == "zone"
        and valid_zone_geometry(feature)
    ]
    feature_ids = [
        str(feature.get("id") or "").strip()
        for feature in zones
        if str(feature.get("id") or "").strip()
    ]
    if len(feature_ids) != len(set(feature_ids)):
        raise ValueError(f"数字孪生运行地图 {floor_code} 存在重复区域标识")
    zones_by_id = {
        str(feature.get("id") or "").strip(): str(
            feature.get("erp_area_code") or ""
        ).strip().upper()
        for feature in zones
        if str(feature.get("id") or "").strip()
    }
    zone_ids_by_area: dict[str, list[str]] = {}
    for feature_id, area_code in zones_by_id.items():
        if area_code:
            zone_ids_by_area.setdefault(area_code, []).append(feature_id)
    return {
        "floor_code": floor_code,
        "revision": str(floor.get("revision") or "").strip(),
        "feature_ids": frozenset(feature_ids),
        "erp_area_codes": frozenset(
            area_code for area_code in zones_by_id.values() if area_code
        ),
        "zones_by_id": zones_by_id,
        "zone_ids_by_area": {
            area_code: tuple(sorted(ids))
            for area_code, ids in zone_ids_by_area.items()
        },
    }


def load_warehouse_twin_published_floor_identity(floor_number: int) -> dict:
    """Return the current measured-map identity without caching stale files."""

    floor_code = f"{int(floor_number)}F"
    target = resolve_warehouse_twin_layout_path()
    stat = target.stat()
    return _published_floor_identity_cached(
        str(target),
        int(stat.st_mtime_ns),
        int(stat.st_size),
        floor_code,
    )
