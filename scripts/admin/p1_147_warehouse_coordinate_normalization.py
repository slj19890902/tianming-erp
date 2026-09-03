from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
import shutil
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from statistics import median
from typing import Any, Iterable


TASK_CODE = "P1-147"
AUTHORITY_CODE = "published_measured_geometry_plus_visible_percent_layout"
SCHEMA_VERSION = 3
SCOPE_FLOOR_CODE = "3F"
CHANGE_TOLERANCE_MM = Decimal("0.500")
COORDINATE_QUANTUM = Decimal("0.001")
APPLY_TOKEN = "APPLY-P1-147-ISOLATED-COPY"
ROLLBACK_TOKEN = "ROLLBACK-P1-147-ISOLATED-COPY"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_FORMAL_DATABASE = (PROJECT_ROOT / "data" / "carton_erp.sqlite3").resolve(
    strict=False
)
KNOWN_FACTORY_FORMAL_DATABASE = Path(
    r"D:\纸箱厂erp软件搭建\data\carton_erp.sqlite3"
).resolve(strict=False)
PROTECTED_TABLES = (
    "inventory_lots",
    "inventory_pallets",
    "inventory_pallet_items",
    "warehouse_ground_occupancies",
    "warehouse_ground_occupancy_slots",
    "warehouse_floors",
    "warehouse_areas",
    "warehouse_area_storage_policies",
    "warehouse_locations",
    "floor3_location_layouts",
    "warehouse_ground_layout_plans",
)
MUTABLE_TRIGGERS = {
    "trg_ground_layout_slots_immutable_update": (
        "warehouse_ground_layout_slots",
        "published ground layout slot is immutable",
    ),
}


class CoordinateNormalizationError(RuntimeError):
    pass


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _q(value: float | int | str | Decimal) -> Decimal:
    return Decimal(str(value)).quantize(COORDINATE_QUANTUM)


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        is not None
    )


def _required_tables(connection: sqlite3.Connection) -> None:
    required = {
        "alembic_version",
        "warehouse_floors",
        "warehouse_areas",
        "warehouse_area_storage_policies",
        "warehouse_locations",
        "floor3_location_layouts",
        "warehouse_ground_layout_plans",
        "warehouse_ground_layout_slots",
        "operation_logs",
    }
    missing = sorted(table for table in required if not _table_exists(connection, table))
    if missing:
        raise CoordinateNormalizationError(
            "数据库缺少 P1-147 必需表：" + ", ".join(missing)
        )


def _assert_no_wal(database: Path) -> None:
    sidecars = [Path(f"{database}-wal"), Path(f"{database}-shm")]
    present = [str(path) for path in sidecars if path.exists()]
    if present:
        raise CoordinateNormalizationError(
            "检测到 SQLite WAL/SHM 旁路文件，副本可能仍在使用，拒绝继续："
            + ", ".join(present)
        )


def _assert_isolated_database(database: Path, confirmed: bool) -> None:
    if not confirmed:
        raise CoordinateNormalizationError("必须显式提供 --confirm-isolated-copy。")
    resolved = database.resolve(strict=True)
    if resolved in {DEFAULT_FORMAL_DATABASE, KNOWN_FACTORY_FORMAL_DATABASE}:
        raise CoordinateNormalizationError("拒绝在正式 carton_erp.sqlite3 上执行修正或复演。")
    marker_text = str(resolved.parent).lower() + " " + resolved.name.lower()
    if not any(
        marker in marker_text
        for marker in ("uat", "copy", "replica", "snapshot", "rehearsal", "p1-147")
    ):
        raise CoordinateNormalizationError(
            "路径未体现 UAT/copy/replica/snapshot/rehearsal/P1-147 隔离标记，拒绝继续。"
        )
    _assert_no_wal(resolved)


@contextmanager
def readonly_database(database: Path) -> Iterable[sqlite3.Connection]:
    _assert_no_wal(database)
    uri = database.resolve(strict=True).as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA foreign_keys=ON")
        yield connection
    finally:
        connection.close()


def _check_database(connection: sqlite3.Connection) -> dict[str, Any]:
    integrity = [row[0] for row in connection.execute("PRAGMA integrity_check")]
    foreign_keys = [tuple(row) for row in connection.execute("PRAGMA foreign_key_check")]
    return {
        "integrity_check": integrity,
        "foreign_key_violation_count": len(foreign_keys),
        "foreign_key_violations": foreign_keys[:100],
        "ok": integrity == ["ok"] and not foreign_keys,
    }


def _floor_revision_map(map_document: dict[str, Any]) -> dict[str, str]:
    return {
        str(code).upper(): str((floor or {}).get("revision") or "").strip()
        for code, floor in (map_document.get("floors") or {}).items()
    }


def _scope_floor_document(
    map_document: dict[str, Any], floor_code: str = SCOPE_FLOOR_CODE
) -> dict[str, Any]:
    for code, floor in (map_document.get("floors") or {}).items():
        if str(code).strip().upper() == floor_code:
            return floor or {}
    raise CoordinateNormalizationError(f"正式地图缺少 {floor_code} 楼层。")


def _explicit_floor_code_alias(first: str, second: str) -> bool:
    first = str(first or "").strip().upper()
    second = str(second or "").strip().upper()
    first_prefix = re.fullmatch(r"F([1-9]\d*)", first)
    first_suffix = re.fullmatch(r"([1-9]\d*)F", first)
    second_prefix = re.fullmatch(r"F([1-9]\d*)", second)
    second_suffix = re.fullmatch(r"([1-9]\d*)F", second)
    return bool(
        (
            first_prefix
            and second_suffix
            and first_prefix.group(1) == second_suffix.group(1)
        )
        or (
            first_suffix
            and second_prefix
            and first_suffix.group(1) == second_prefix.group(1)
        )
    )


def _optional_positive_id(value: Any) -> tuple[str, int | None]:
    if value is None or (isinstance(value, str) and not value.strip()):
        return "absent", None
    if isinstance(value, bool):
        return "invalid", None
    if isinstance(value, int):
        return ("valid", value) if value > 0 else ("invalid", None)
    if isinstance(value, str) and re.fullmatch(r"[1-9]\d*", value.strip()):
        return "valid", int(value.strip())
    return "invalid", None


def _map_identity(
    source: sqlite3.Row, feature: dict[str, Any]
) -> tuple[str | None, str | None]:
    database_floor_code = str(source["floor_code"] or "").strip().upper()
    map_floor_code = str(feature["floor_code"] or "").strip().upper()
    floor_id_state, map_floor_id = _optional_positive_id(
        feature.get("formal_floor_id")
    )
    area_id_state, map_area_id = _optional_positive_id(feature.get("formal_area_id"))
    if "invalid" in (floor_id_state, area_id_state):
        return "formal_identity_invalid", None
    if floor_id_state != area_id_state:
        return "formal_identity_incomplete", None
    if floor_id_state == "valid":
        if map_floor_id != int(source["floor_id"]):
            return "cross_floor_binding", None
        if map_area_id != int(source["area_id"]):
            return "cross_area_binding", None
        if map_floor_code == database_floor_code:
            return None, None
        if _explicit_floor_code_alias(map_floor_code, database_floor_code):
            return None, "stable_ids"
        return "floor_code_identity_conflict", None
    if map_floor_code == database_floor_code:
        return None, None
    if _explicit_floor_code_alias(map_floor_code, database_floor_code):
        return None, "explicit_code_fallback"
    return "cross_floor_binding", None


def _load_map(path: Path) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    normalized_path = str(path.resolve(strict=True)).lower()
    if "layout_drafts" in normalized_path or ".draft." in path.name.lower():
        raise CoordinateNormalizationError("拒绝把未发布地图草稿作为坐标权威。")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CoordinateNormalizationError(f"无法读取正式地图：{path}") from error
    if document.get("schema_version") != 1 or not isinstance(document.get("floors"), dict):
        raise CoordinateNormalizationError("正式地图 schema_version/floors 不受支持。")
    features: dict[str, dict[str, Any]] = {}
    for floor_code, floor in document["floors"].items():
        revision = str((floor or {}).get("revision") or "").strip()
        if not revision:
            raise CoordinateNormalizationError(f"正式地图 {floor_code} 缺少 revision。")
        for feature in (floor or {}).get("features") or []:
            feature_id = str(feature.get("id") or "").strip()
            if feature.get("feature_kind") == "zone" and feature_id:
                if feature_id in features:
                    raise CoordinateNormalizationError(f"正式地图 feature id 重复：{feature_id}")
                features[feature_id] = {
                    **feature,
                    "floor_code": str(floor_code).upper(),
                    "floor_revision": revision,
                }
    return document, features


def zone_layout_frame(points: list[Any]) -> dict[str, Any] | None:
    """Python twin of warehouseInventory.mjs::zoneLayoutFrame."""

    if not isinstance(points, list) or len(points) != 4:
        return None
    try:
        normalized = [(float(point[0]), float(point[1])) for point in points]
    except (TypeError, ValueError, IndexError):
        return None
    if not all(math.isfinite(value) for point in normalized for value in point):
        return None
    area2 = sum(
        point[0] * normalized[(index + 1) % 4][1]
        - point[1] * normalized[(index + 1) % 4][0]
        for index, point in enumerate(normalized)
    )
    if abs(area2) < 1:
        return None
    if area2 > 0:
        anchor, right_point, down_point, opposite = (
            normalized[3],
            normalized[2],
            normalized[0],
            normalized[1],
        )
    else:
        anchor, right_point, down_point, opposite = (
            normalized[0],
            normalized[1],
            normalized[3],
            normalized[2],
        )
    right = (right_point[0] - anchor[0], right_point[1] - anchor[1])
    down = (down_point[0] - anchor[0], down_point[1] - anchor[1])
    width, height = math.hypot(*right), math.hypot(*down)
    if width < 1 or height < 1:
        return None
    orthogonality = abs((right[0] * down[0] + right[1] * down[1]) / (width * height))
    expected = (anchor[0] + right[0] + down[0], anchor[1] + right[1] + down[1])
    closure = math.hypot(opposite[0] - expected[0], opposite[1] - expected[1]) / max(
        width, height
    )
    if orthogonality > 1e-4 or closure > 1e-4:
        return None
    return {
        "anchor": anchor,
        "right": right,
        "down": down,
        "width": width,
        "height": height,
        "rotation_deg": math.degrees(math.atan2(right[1], right[0])),
    }


def geometry_class(points: list[Any]) -> tuple[str, float | None]:
    frame = zone_layout_frame(points)
    if frame is None:
        return "non_rectangular", None
    rotation = (float(frame["rotation_deg"]) + 360) % 360
    axis_aligned = any(abs(rotation - value) <= 0.01 for value in (0, 90, 180, 270, 360))
    return ("axis_aligned_rectangle" if axis_aligned else "rotated_rectangle"), rotation


def _point_in_polygon(point: tuple[float, float], polygon: list[tuple[float, float]]) -> bool:
    x, y = point
    inside = False
    for index, first in enumerate(polygon):
        second = polygon[(index + 1) % len(polygon)]
        cross = (x - first[0]) * (second[1] - first[1]) - (y - first[1]) * (
            second[0] - first[0]
        )
        if abs(cross) <= 0.001 and min(first[0], second[0]) - 0.001 <= x <= max(
            first[0], second[0]
        ) + 0.001 and min(first[1], second[1]) - 0.001 <= y <= max(
            first[1], second[1]
        ) + 0.001:
            return True
        if (first[1] > y) != (second[1] > y):
            crossing_x = (second[0] - first[0]) * (y - first[1]) / (
                second[1] - first[1]
            ) + first[0]
            if x < crossing_x:
                inside = not inside
    return inside


def _target_footprint_inside_zone(
    points: list[Any], *, x_mm: Decimal, y_mm: Decimal, width_mm: int, depth_mm: int
) -> bool:
    try:
        polygon = [(float(point[0]), float(point[1])) for point in points]
    except (TypeError, ValueError, IndexError):
        return False
    if len(polygon) < 3:
        return False
    x, y = float(x_mm), float(y_mm)
    return all(
        _point_in_polygon(point, polygon)
        for point in (
            (x, y),
            (x + width_mm, y),
            (x + width_mm, y + depth_mm),
            (x, y + depth_mm),
        )
    )


def visible_geometry(
    points: list[Any], layout: sqlite3.Row, *, slot_width: int, slot_depth: int
) -> dict[str, Any]:
    frame = zone_layout_frame(points)
    left = float(layout["left_pct"])
    top = float(layout["top_pct"])
    width_pct = float(layout["width_pct"])
    height_pct = float(layout["height_pct"])
    if frame is not None:
        horizontal = (left + width_pct / 2) / 100
        vertical = (top + height_pct / 2) / 100
        center_x = (
            frame["anchor"][0]
            + frame["right"][0] * horizontal
            + frame["down"][0] * vertical
        )
        center_y = (
            frame["anchor"][1]
            + frame["right"][1] * horizontal
            + frame["down"][1] * vertical
        )
        mapped_width = frame["width"] * width_pct / 100
        mapped_depth = frame["height"] * height_pct / 100
        kind, rotation = geometry_class(points)
        if kind == "rotated_rectangle":
            # The ledger has no rotation column. Preserve the visible centre, but
            # force field approval before this candidate can ever be applied.
            target_x = center_x - slot_width / 2
            target_y = center_y - slot_depth / 2
        else:
            target_x = center_x - mapped_width / 2
            target_y = center_y - mapped_depth / 2
    else:
        xs = [float(point[0]) for point in points]
        ys = [float(point[1]) for point in points]
        min_x, max_x = min(xs), max(xs)
        min_y, max_y = min(ys), max(ys)
        span_x, span_y = max_x - min_x, max_y - min_y
        mapped_width = span_x * width_pct / 100
        mapped_depth = span_y * height_pct / 100
        target_x = min_x + span_x * left / 100
        target_y = max_y - span_y * top / 100 - mapped_depth
        center_x = target_x + mapped_width / 2
        center_y = target_y + mapped_depth / 2
        kind, rotation = "non_rectangular", None
    return {
        "geometry_class": kind,
        "zone_rotation_deg": None if rotation is None else round(rotation, 6),
        "visible_center_x_mm": _q(center_x),
        "visible_center_y_mm": _q(center_y),
        "mapped_width_mm": _q(mapped_width),
        "mapped_depth_mm": _q(mapped_depth),
        "target_x_mm": _q(target_x),
        "target_y_mm": _q(target_y),
    }


def _query_rows(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        connection.execute(
            """
            SELECT
              s.id AS ground_slot_id,s.plan_id,s.location_id,s.route_sequence,
              s.row_no,s.slot_no,s.x_mm,s.y_mm,s.width_mm,s.depth_mm,
              p.status AS plan_status,p.target_slot_count,p.numbering_origin,
              p.row_direction,p.slot_direction,p.row_start_no,p.slot_start_no,
              p.draft_map_revision,p.published_map_revision,p.preview_fingerprint,
              p.version AS plan_version,p.updated_by AS plan_updated_by,
              p.updated_at AS plan_updated_at,
              a.id AS area_id,a.area_code,a.area_name,a.address_version AS area_version,
              f.id AS floor_id,f.floor_code,f.floor_name,f.floor_number,
              policy.id AS policy_id,policy.map_feature_id,policy.status AS policy_status,
              policy.storage_layout,policy.published_map_revision AS policy_map_revision,
              policy.version AS policy_version,
              l.location_code,l.location_name,l.address_version AS location_address_version,
              l.placement_status,l.storage_type,l.is_active,
              fl.id AS layout_id,fl.left_pct,fl.top_pct,fl.width_pct,fl.height_pct,
              fl.z_index,fl.version AS layout_version,fl.source_type,fl.layout_kind,
              fl.updated_at AS layout_updated_at
            FROM warehouse_ground_layout_slots AS s
            JOIN warehouse_ground_layout_plans AS p ON p.id=s.plan_id
            JOIN warehouse_areas AS a ON a.id=p.area_id
            JOIN warehouse_floors AS f ON f.id=a.floor_id
            JOIN warehouse_area_storage_policies AS policy ON policy.area_id=a.id
            JOIN warehouse_locations AS l ON l.id=s.location_id
            JOIN floor3_location_layouts AS fl ON fl.location_id=l.id
            WHERE p.status='published' AND policy.status='published'
            ORDER BY f.floor_number,a.area_code,s.route_sequence,s.id
            """
        )
    )


def _inventory_evidence(
    connection: sqlite3.Connection, location_ids: list[int]
) -> dict[int, dict[str, Any]]:
    result = {
        location_id: {
            "direct_lots": [],
            "direct_quantity": 0,
            "pallet_codes": set(),
            "occupancy_ids": set(),
            "occupancy_lots": [],
            "occupancy_quantity": 0,
        }
        for location_id in location_ids
    }
    if not location_ids:
        return result
    placeholders = ",".join("?" for _ in location_ids)
    if _table_exists(connection, "inventory_lots"):
        for row in connection.execute(
            f"""
            SELECT id,warehouse_location_id,lot_number,status,unit,
                   quantity_available,quantity_reserved,quantity_damaged
            FROM inventory_lots
            WHERE warehouse_location_id IN ({placeholders})
              AND status IN ('active','frozen')
              AND quantity_available+quantity_reserved+quantity_damaged>0
            ORDER BY warehouse_location_id,lot_number,id
            """,
            location_ids,
        ):
            quantity = int(row["quantity_available"] or 0) + int(
                row["quantity_reserved"] or 0
            ) + int(row["quantity_damaged"] or 0)
            item = result[int(row["warehouse_location_id"])]
            item["direct_quantity"] += quantity
            item["direct_lots"].append(
                {
                    "lot_id": int(row["id"]),
                    "lot_number": row["lot_number"],
                    "status": row["status"],
                    "quantity": quantity,
                    "unit": row["unit"],
                }
            )
    if _table_exists(connection, "inventory_pallets"):
        for row in connection.execute(
            f"""
            SELECT id,location_id,pallet_code
            FROM inventory_pallets
            WHERE location_id IN ({placeholders}) AND is_current=1
            ORDER BY location_id,pallet_code,id
            """,
            location_ids,
        ):
            result[int(row["location_id"])]["pallet_codes"].add(row["pallet_code"])
    occupancy_tables = {
        "warehouse_ground_occupancies",
        "warehouse_ground_occupancy_slots",
        "inventory_pallets",
        "inventory_pallet_items",
        "inventory_lots",
    }
    if all(_table_exists(connection, table) for table in occupancy_tables):
        for row in connection.execute(
            f"""
            SELECT os.location_id,o.id AS occupancy_id,p.pallet_code,
                   lot.id AS lot_id,lot.lot_number,lot.status AS lot_status,lot.unit,
                   lot.quantity_available,lot.quantity_reserved,lot.quantity_damaged
            FROM warehouse_ground_occupancy_slots AS os
            JOIN warehouse_ground_occupancies AS o ON o.id=os.occupancy_id
            JOIN inventory_pallets AS p ON p.id=o.pallet_id
            LEFT JOIN inventory_pallet_items AS pi ON pi.pallet_id=p.id
            LEFT JOIN inventory_lots AS lot ON lot.id=pi.inventory_lot_id
            WHERE os.location_id IN ({placeholders})
              AND os.status='active' AND o.status='active'
            ORDER BY os.location_id,o.id,lot.lot_number,lot.id
            """,
            location_ids,
        ):
            item = result[int(row["location_id"])]
            item["occupancy_ids"].add(int(row["occupancy_id"]))
            item["pallet_codes"].add(row["pallet_code"])
            if row["lot_id"] is None or row["lot_status"] not in {"active", "frozen"}:
                continue
            quantity = int(row["quantity_available"] or 0) + int(
                row["quantity_reserved"] or 0
            ) + int(row["quantity_damaged"] or 0)
            if quantity <= 0:
                continue
            evidence_key = (int(row["occupancy_id"]), int(row["lot_id"]))
            if any(entry["evidence_key"] == evidence_key for entry in item["occupancy_lots"]):
                continue
            item["occupancy_quantity"] += quantity
            item["occupancy_lots"].append(
                {
                    "evidence_key": evidence_key,
                    "lot_id": int(row["lot_id"]),
                    "lot_number": row["lot_number"],
                    "status": row["lot_status"],
                    "quantity": quantity,
                    "unit": row["unit"],
                }
            )
    for item in result.values():
        item["pallet_codes"] = sorted(value for value in item["pallet_codes"] if value)
        item["occupancy_ids"] = sorted(item["occupancy_ids"])
        for entry in item["occupancy_lots"]:
            entry.pop("evidence_key", None)
    return result


def _logical_table_hash(connection: sqlite3.Connection, table: str) -> dict[str, Any]:
    if not _table_exists(connection, table):
        return {"present": False, "row_count": 0, "sha256": None}
    columns = [row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')]
    order = " ORDER BY " + ",".join(f'"{column}"' for column in columns) if columns else ""
    digest = hashlib.sha256()
    digest.update(canonical_json({"columns": columns}).encode("utf-8"))
    row_count = 0
    for row in connection.execute(f'SELECT * FROM "{table}"{order}'):
        digest.update(b"\n")
        digest.update(canonical_json(list(row)).encode("utf-8"))
        row_count += 1
    return {
        "present": True,
        "row_count": row_count,
        "sha256": digest.hexdigest(),
    }


def protected_snapshot(connection: sqlite3.Connection) -> dict[str, Any]:
    return {table: _logical_table_hash(connection, table) for table in PROTECTED_TABLES}


def _published_counts(
    connection: sqlite3.Connection, *, plans: bool
) -> dict[str, int]:
    counted_table = "warehouse_ground_layout_plans AS p"
    counted_value = "p.id"
    joins = ""
    if not plans:
        counted_table = "warehouse_ground_layout_slots AS s"
        counted_value = "s.id"
        joins = "JOIN warehouse_ground_layout_plans AS p ON p.id=s.plan_id"
    return {
        str(row[0]).strip().upper(): int(row[1])
        for row in connection.execute(
            f"""
            SELECT f.floor_code,count({counted_value})
            FROM {counted_table}
            {joins}
            JOIN warehouse_areas AS a ON a.id=p.area_id
            JOIN warehouse_floors AS f ON f.id=a.floor_id
            WHERE p.status='published'
            GROUP BY f.id,f.floor_code,f.floor_number
            ORDER BY f.floor_number,f.id
            """
        )
    }


def _out_of_scope_findings(
    source_rows: list[sqlite3.Row],
    feature_by_id: dict[str, dict[str, Any]],
    inventory: dict[int, dict[str, Any]],
    *,
    scope_floor_code: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    findings: list[dict[str, Any]] = []
    floor_alias_matches: list[dict[str, Any]] = []
    for source in source_rows:
        database_floor_code = str(source["floor_code"] or "").strip().upper()
        if database_floor_code == scope_floor_code:
            continue
        feature_id = str(source["map_feature_id"] or "").strip()
        feature = feature_by_id.get(feature_id)
        issue_code: str | None = None
        map_floor_code: str | None = None
        map_revision: str | None = None
        match_basis: str | None = None
        raw_map_floor_id: Any = None
        raw_map_area_id: Any = None
        map_floor_id_state = "unavailable"
        normalized_map_floor_id: int | None = None
        map_area_id_state = "unavailable"
        normalized_map_area_id: int | None = None
        if feature is None:
            issue_code = "map_feature_missing"
        else:
            map_floor_code = str(feature["floor_code"] or "").strip().upper()
            map_revision = str(feature["floor_revision"] or "")
            raw_map_floor_id = feature.get("formal_floor_id")
            raw_map_area_id = feature.get("formal_area_id")
            map_floor_id_state, normalized_map_floor_id = _optional_positive_id(
                raw_map_floor_id
            )
            map_area_id_state, normalized_map_area_id = _optional_positive_id(
                raw_map_area_id
            )
            issue_code, match_basis = _map_identity(source, feature)

            if issue_code is None and match_basis is not None:
                evidence = inventory[int(source["location_id"])]
                quantity = max(
                    int(evidence["direct_quantity"]),
                    int(evidence["occupancy_quantity"]),
                )
                floor_alias_matches.append(
                    {
                        "match_basis": match_basis,
                        "database_floor_id": int(source["floor_id"]),
                        "map_formal_floor_id": raw_map_floor_id,
                        "map_floor_id_state": map_floor_id_state,
                        "normalized_map_floor_id": normalized_map_floor_id,
                        "database_floor_code": database_floor_code,
                        "map_floor_code": map_floor_code,
                        "database_area_id": int(source["area_id"]),
                        "map_formal_area_id": raw_map_area_id,
                        "map_area_id_state": map_area_id_state,
                        "normalized_map_area_id": normalized_map_area_id,
                        "area_code": source["area_code"],
                        "plan_id": int(source["plan_id"]),
                        "location_id": int(source["location_id"]),
                        "location_code": source["location_code"],
                        "map_feature_id": feature_id,
                        "inventory_quantity": quantity,
                        "occupied": quantity > 0 or bool(evidence["occupancy_ids"]),
                        "pallet_codes": evidence["pallet_codes"],
                    }
                )

            if issue_code is None and (
                str(source["policy_map_revision"] or "") != map_revision
                and str(source["published_map_revision"] or "") != map_revision
            ):
                issue_code = "policy_and_plan_revision_mismatch"
            elif (
                issue_code is None
                and str(source["policy_map_revision"] or "") != map_revision
            ):
                issue_code = "policy_revision_mismatch"
            elif (
                issue_code is None
                and str(source["published_map_revision"] or "") != map_revision
            ):
                issue_code = "plan_revision_mismatch"
        if issue_code is None:
            continue
        evidence = inventory[int(source["location_id"])]
        quantity = max(
            int(evidence["direct_quantity"]), int(evidence["occupancy_quantity"])
        )
        findings.append(
            {
                "issue_code": issue_code,
                "database_floor_id": int(source["floor_id"]),
                "map_formal_floor_id": raw_map_floor_id,
                "map_floor_id_state": map_floor_id_state,
                "normalized_map_floor_id": normalized_map_floor_id,
                "database_floor_code": database_floor_code,
                "map_floor_code": map_floor_code,
                "database_area_id": int(source["area_id"]),
                "map_formal_area_id": raw_map_area_id,
                "map_area_id_state": map_area_id_state,
                "normalized_map_area_id": normalized_map_area_id,
                "area_code": source["area_code"],
                "plan_id": int(source["plan_id"]),
                "location_id": int(source["location_id"]),
                "location_code": source["location_code"],
                "map_feature_id": feature_id,
                "policy_map_revision": source["policy_map_revision"],
                "plan_map_revision": source["published_map_revision"],
                "current_map_revision": map_revision,
                "inventory_quantity": quantity,
                "occupied": quantity > 0 or bool(evidence["occupancy_ids"]),
                "pallet_codes": evidence["pallet_codes"],
            }
        )
    return findings, floor_alias_matches


def _coordinate_pattern_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    changed = [row for row in rows if row["change_required"]]
    deviations = sorted(Decimal(row["deviation_mm"]) for row in changed)
    mirror_groups: list[dict[str, Any]] = []
    for area_code in sorted({str(row["area_code"]) for row in rows}):
        group = [row for row in rows if str(row["area_code"]) == area_code]
        current_y_values = {Decimal(row["current_y_mm"]) for row in group}
        if len(group) < 2 or len(current_y_values) < 2:
            continue
        intercepts = [
            Decimal(row["current_y_mm"]) + Decimal(row["target_y_mm"])
            for row in group
        ]
        intercept = median(intercepts)
        residual = max(abs(value - intercept) for value in intercepts)
        mirror_groups.append(
            {
                "area_code": area_code,
                "row_count": len(group),
                "mirror_intercept_mm": format(_q(intercept), "f"),
                "max_residual_mm": format(_q(residual), "f"),
            }
        )
    max_abs_delta_x = max(
        (abs(Decimal(row["delta_x_mm"])) for row in rows), default=Decimal("0")
    )
    max_mirror_residual = max(
        (
            Decimal(group["max_residual_mm"])
            for group in mirror_groups
        ),
        default=Decimal("0"),
    )
    return {
        "max_abs_delta_x_mm": format(_q(max_abs_delta_x), "f"),
        "changed_deviation_min_mm": (
            format(_q(deviations[0]), "f") if deviations else None
        ),
        "changed_deviation_median_mm": (
            format(_q(median(deviations)), "f") if deviations else None
        ),
        "changed_deviation_max_mm": (
            format(_q(deviations[-1]), "f") if deviations else None
        ),
        "multi_row_mirror_group_count": len(mirror_groups),
        "max_mirror_residual_mm": format(_q(max_mirror_residual), "f"),
        "pattern": (
            "consistent_with_reversed_y_axis_or_origin"
            if changed
            and max_abs_delta_x <= Decimal("0.010")
            and mirror_groups
            and max_mirror_residual <= Decimal("0.010")
            else "not_classified"
        ),
        "mirror_groups": mirror_groups,
    }


def build_audit(
    database: Path,
    map_path: Path,
    *,
    scope_floor_code: str = SCOPE_FLOOR_CODE,
) -> dict[str, Any]:
    scope_floor_code = str(scope_floor_code or "").strip().upper()
    if scope_floor_code != SCOPE_FLOOR_CODE:
        raise CoordinateNormalizationError(
            f"P1-147 仅允许 {SCOPE_FLOOR_CODE} 专项，拒绝审计 {scope_floor_code or '<empty>'}。"
        )
    database = database.resolve(strict=True)
    map_path = map_path.resolve(strict=True)
    _assert_isolated_database(database, True)
    map_document, feature_by_id = _load_map(map_path)
    scope_floor_document = _scope_floor_document(map_document, scope_floor_code)
    with readonly_database(database) as connection:
        _required_tables(connection)
        checks = _check_database(connection)
        if not checks["ok"]:
            raise CoordinateNormalizationError("数据库完整性或外键检查未通过。")
        source_rows = _query_rows(connection)
        published_slots_by_floor = _published_counts(connection, plans=False)
        published_plans_by_floor = _published_counts(connection, plans=True)
        inventory = _inventory_evidence(
            connection, [int(row["location_id"]) for row in source_rows]
        )
        protected = protected_snapshot(connection)
        alembic_rows = [
            row[0] for row in connection.execute("SELECT version_num FROM alembic_version")
        ]

    warehouse_published_slot_count = sum(published_slots_by_floor.values())
    warehouse_published_plan_count = sum(published_plans_by_floor.values())
    joined_rows_by_floor: dict[str, int] = {}
    for source in source_rows:
        code = str(source["floor_code"] or "").strip().upper()
        joined_rows_by_floor[code] = joined_rows_by_floor.get(code, 0) + 1
    scoped_sources = [
        source
        for source in source_rows
        if str(source["floor_code"] or "").strip().upper() == scope_floor_code
    ]
    out_of_scope_sources = [
        source
        for source in source_rows
        if str(source["floor_code"] or "").strip().upper() != scope_floor_code
    ]
    out_of_scope_findings, floor_alias_matches = _out_of_scope_findings(
        source_rows,
        feature_by_id,
        inventory,
        scope_floor_code=scope_floor_code,
    )

    input_quality_findings: list[str] = []
    hard_blockers: list[str] = []
    if len(source_rows) != warehouse_published_slot_count:
        input_quality_findings.append(
            f"全仓已发布货位 {warehouse_published_slot_count} 行，完整关联查询仅返回 {len(source_rows)} 行。"
        )
    scope_published_slot_count = published_slots_by_floor.get(scope_floor_code, 0)
    scope_published_plan_count = published_plans_by_floor.get(scope_floor_code, 0)
    if len(scoped_sources) != scope_published_slot_count:
        hard_blockers.append(
            f"{scope_floor_code} 已发布货位 {scope_published_slot_count} 行，完整关联查询仅返回 {len(scoped_sources)} 行。"
        )
    if len({int(row["plan_id"]) for row in scoped_sources}) != scope_published_plan_count:
        hard_blockers.append(
            f"{scope_floor_code} 已发布规划 {scope_published_plan_count} 个，完整关联查询仅覆盖 "
            f"{len({int(row['plan_id']) for row in scoped_sources})} 个。"
        )
    if len({int(row["ground_slot_id"]) for row in scoped_sources}) != len(scoped_sources):
        hard_blockers.append(f"{scope_floor_code} 完整关联查询出现重复 ground slot id。")
    if len({int(row["location_id"]) for row in scoped_sources}) != len(scoped_sources):
        hard_blockers.append(f"{scope_floor_code} 已发布货位不是一行一个唯一 location id。")

    rows: list[dict[str, Any]] = []
    for source in scoped_sources:
        feature_id = str(source["map_feature_id"] or "").strip()
        feature = feature_by_id.get(feature_id)
        if feature is None:
            hard_blockers.append(
                f"location {source['location_id']} 的正式区域 {feature_id!r} 不在正式地图中。"
            )
            continue
        floor_code = str(source["floor_code"] or "").strip().upper()
        map_revision = str(feature["floor_revision"])
        identity_issue, match_basis = _map_identity(source, feature)
        if identity_issue is None and match_basis is not None:
            identity_issue = "scope_floor_code_alias_not_supported"
        if identity_issue is not None:
            hard_blockers.append(
                f"location {source['location_id']} 的正式地图身份无效（{identity_issue}）："
                f"数据库 floor={source['floor_id']}/{floor_code}, area={source['area_id']}；"
                f"地图 floor={feature.get('formal_floor_id')}/{feature['floor_code']}, "
                f"area={feature.get('formal_area_id')}。"
            )
            continue
        if (
            str(source["policy_map_revision"] or "") != map_revision
            or str(source["published_map_revision"] or "") != map_revision
        ):
            hard_blockers.append(
                f"location {source['location_id']} 的 policy/plan 地图版本与 {floor_code} 正式地图不一致。"
            )
            continue
        geometry = visible_geometry(
            feature.get("points") or [],
            source,
            slot_width=int(source["width_mm"]),
            slot_depth=int(source["depth_mm"]),
        )
        current_x, current_y = _q(source["x_mm"]), _q(source["y_mm"])
        delta_x = geometry["target_x_mm"] - current_x
        delta_y = geometry["target_y_mm"] - current_y
        deviation = _q(math.hypot(float(delta_x), float(delta_y)))
        width_delta = abs(
            geometry["mapped_width_mm"] - Decimal(int(source["width_mm"]))
        )
        depth_delta = abs(
            geometry["mapped_depth_mm"] - Decimal(int(source["depth_mm"]))
        )
        unsafe_reason = None
        if geometry["geometry_class"] == "rotated_rectangle":
            unsafe_reason = "rotated_rectangle_has_no_ground_slot_rotation_contract"
        elif width_delta > Decimal("2.000") or depth_delta > Decimal("2.000"):
            unsafe_reason = "visible_size_does_not_match_ground_slot_contract"
        elif not _target_footprint_inside_zone(
            feature.get("points") or [],
            x_mm=geometry["target_x_mm"],
            y_mm=geometry["target_y_mm"],
            width_mm=int(source["width_mm"]),
            depth_mm=int(source["depth_mm"]),
        ):
            unsafe_reason = "target_footprint_crosses_published_zone"
        change_required = (
            abs(delta_x) > CHANGE_TOLERANCE_MM
            or abs(delta_y) > CHANGE_TOLERANCE_MM
        )
        if not change_required:
            candidate_disposition = "unchanged"
        elif unsafe_reason:
            candidate_disposition = "geometry_blocked"
        elif geometry["geometry_class"] == "axis_aligned_rectangle":
            candidate_disposition = "rehearsal_candidate"
        else:
            candidate_disposition = "field_confirmation_required"
        execution_eligible = candidate_disposition == "rehearsal_candidate"
        evidence = inventory[int(source["location_id"])]
        row = {
            "floor_code": floor_code,
            "floor_name": source["floor_name"],
            "area_id": int(source["area_id"]),
            "area_code": source["area_code"],
            "area_name": source["area_name"],
            "map_feature_id": feature_id,
            "map_revision": map_revision,
            "geometry_class": geometry["geometry_class"],
            "zone_rotation_deg": geometry["zone_rotation_deg"],
            "ground_slot_id": int(source["ground_slot_id"]),
            "plan_id": int(source["plan_id"]),
            "location_id": int(source["location_id"]),
            "location_code": source["location_code"],
            "location_name": source["location_name"],
            "route_sequence": int(source["route_sequence"]),
            "row_no": int(source["row_no"]),
            "slot_no": int(source["slot_no"]),
            "current_x_mm": format(current_x, "f"),
            "current_y_mm": format(current_y, "f"),
            "visible_center_x_mm": format(geometry["visible_center_x_mm"], "f"),
            "visible_center_y_mm": format(geometry["visible_center_y_mm"], "f"),
            "target_x_mm": format(geometry["target_x_mm"], "f"),
            "target_y_mm": format(geometry["target_y_mm"], "f"),
            "delta_x_mm": format(delta_x, "f"),
            "delta_y_mm": format(delta_y, "f"),
            "deviation_mm": format(deviation, "f"),
            "width_mm": int(source["width_mm"]),
            "depth_mm": int(source["depth_mm"]),
            "mapped_width_mm": format(geometry["mapped_width_mm"], "f"),
            "mapped_depth_mm": format(geometry["mapped_depth_mm"], "f"),
            "left_pct": float(source["left_pct"]),
            "top_pct": float(source["top_pct"]),
            "width_pct": float(source["width_pct"]),
            "height_pct": float(source["height_pct"]),
            "layout_id": int(source["layout_id"]),
            "layout_version": int(source["layout_version"]),
            "layout_source_type": source["source_type"],
            "layout_kind": source["layout_kind"],
            "layout_updated_at": source["layout_updated_at"],
            "plan_version": int(source["plan_version"]),
            "plan_preview_fingerprint": source["preview_fingerprint"],
            "plan_updated_at": source["plan_updated_at"],
            "policy_id": int(source["policy_id"]),
            "policy_version": int(source["policy_version"]),
            "location_address_version": int(source["location_address_version"]),
            "location_placement_status": source["placement_status"],
            "location_storage_type": source["storage_type"],
            "location_is_active": int(source["is_active"]),
            "area_version": int(source["area_version"]),
            "pallet_codes": evidence["pallet_codes"],
            "occupied_batch_lots": evidence["occupancy_lots"]
            or evidence["direct_lots"],
            "direct_inventory_lots": evidence["direct_lots"],
            "direct_inventory_quantity": evidence["direct_quantity"],
            "occupancy_ids": evidence["occupancy_ids"],
            "occupancy_inventory_quantity": evidence["occupancy_quantity"],
            "inventory_quantity": max(
                int(evidence["direct_quantity"]), int(evidence["occupancy_quantity"])
            ),
            "change_required": change_required,
            "candidate_disposition": candidate_disposition,
            "execution_eligible": execution_eligible,
            "unsafe_reason": unsafe_reason,
        }
        rows.append(row)

    plan_groups: list[dict[str, Any]] = []
    scoped_source_by_plan = {
        int(source["plan_id"]): source for source in scoped_sources
    }
    for plan_id in sorted({row["plan_id"] for row in rows}):
        group = [row for row in rows if row["plan_id"] == plan_id]
        source = scoped_source_by_plan[plan_id]
        if len(group) != int(source["target_slot_count"]):
            hard_blockers.append(
                f"plan {plan_id} 声明 {source['target_slot_count']} 个位置，实际可审计 {len(group)} 个。"
            )
        execution_count = sum(bool(row["execution_eligible"]) for row in group)
        if not execution_count:
            continue
        plan_groups.append(
            {
                "plan_id": plan_id,
                "area_id": int(source["area_id"]),
                "floor_code": str(source["floor_code"] or "").strip().upper(),
                "area_code": source["area_code"],
                "expected_version": int(source["plan_version"]),
                "expected_preview_fingerprint": source["preview_fingerprint"],
                "expected_updated_at": source["plan_updated_at"],
                "policy_id": int(source["policy_id"]),
                "policy_version": int(source["policy_version"]),
                "map_revision": source["published_map_revision"],
                "row_count": len(group),
                "changed_row_count": sum(bool(row["change_required"]) for row in group),
                "execution_candidate_row_count": execution_count,
                "field_confirmation_row_count": sum(
                    row["candidate_disposition"] == "field_confirmation_required"
                    for row in group
                ),
                "geometry_blocked_row_count": sum(
                    row["candidate_disposition"] == "geometry_blocked"
                    for row in group
                ),
            }
        )

    change_rows = [row for row in rows if row["change_required"]]
    unchanged_rows = [row for row in rows if not row["change_required"]]
    execution_rows = [row for row in rows if row["execution_eligible"]]
    field_confirmation_rows = [
        row
        for row in rows
        if row["candidate_disposition"] == "field_confirmation_required"
    ]
    geometry_blocked_rows = [
        row for row in rows if row["candidate_disposition"] == "geometry_blocked"
    ]
    out_of_scope_issue_counts: dict[str, int] = {}
    for finding in out_of_scope_findings:
        issue = str(finding["issue_code"])
        out_of_scope_issue_counts[issue] = out_of_scope_issue_counts.get(issue, 0) + 1

    blockers = list(hard_blockers)
    if field_confirmation_rows:
        blockers.append(
            f"{len(field_confirmation_rows)} 个不越界非矩形位置须现场确认，已排除在当前执行集之外。"
        )
    if geometry_blocked_rows:
        blockers.append(
            f"{len(geometry_blocked_rows)} 个位置越界、旋转或尺寸契约不一致，已排除在当前执行集之外。"
        )
    blockers.append(
        "正式坐标写入仍须现场抽样确认、老板选择权威坐标方案，并提供与计划哈希绑定的独立批准文件。"
    )
    follow_up_items: list[str] = []
    revision_issue_count = sum(
        count
        for issue, count in out_of_scope_issue_counts.items()
        if "revision_mismatch" in issue
    )
    if revision_issue_count:
        follow_up_items.append(
            f"非 3F plan/policy revision 问题 {revision_issue_count} 行，另建修复闭环。"
        )

    occupied = lambda candidate_rows: sum(
        int(row["inventory_quantity"]) > 0 or bool(row["occupancy_ids"])
        for row in candidate_rows
    )
    execution_location_ids = sorted(int(row["location_id"]) for row in execution_rows)
    scope_floor_revision = str(scope_floor_document.get("revision") or "").strip()
    summary = {
        "warehouse_published_slot_count": warehouse_published_slot_count,
        "warehouse_joined_source_row_count": len(source_rows),
        "warehouse_published_plan_count": warehouse_published_plan_count,
        "published_slots_by_floor": published_slots_by_floor,
        "published_plans_by_floor": published_plans_by_floor,
        "joined_source_rows_by_floor": joined_rows_by_floor,
        "scope_floor_code": scope_floor_code,
        "scope_published_slot_count": scope_published_slot_count,
        "scope_joined_source_row_count": len(scoped_sources),
        "scope_audited_slot_count": len(rows),
        "scope_published_plan_count": scope_published_plan_count,
        "scope_changed_slot_count": len(change_rows),
        "scope_unchanged_slot_count": len(unchanged_rows),
        "scope_occupied_slot_count": occupied(rows),
        "scope_changed_occupied_slot_count": occupied(change_rows),
        "rehearsal_candidate_slot_count": len(execution_rows),
        "rehearsal_candidate_occupied_slot_count": occupied(execution_rows),
        "field_confirmation_slot_count": len(field_confirmation_rows),
        "field_confirmation_occupied_slot_count": occupied(field_confirmation_rows),
        "geometry_blocked_slot_count": len(geometry_blocked_rows),
        "geometry_blocked_occupied_slot_count": occupied(geometry_blocked_rows),
        "execution_plan_count": len(plan_groups),
        "out_of_scope_source_slot_count": len(out_of_scope_sources),
        "out_of_scope_issue_count": len(out_of_scope_findings),
        "out_of_scope_occupied_issue_count": sum(
            bool(finding["occupied"]) for finding in out_of_scope_findings
        ),
        "out_of_scope_issue_counts": out_of_scope_issue_counts,
        "floor_alias_matches": len(floor_alias_matches),
        "geometry_counts": {
            kind: sum(row["geometry_class"] == kind for row in rows)
            for kind in (
                "axis_aligned_rectangle",
                "rotated_rectangle",
                "non_rectangular",
            )
        },
    }
    result = {
        "schema_version": SCHEMA_VERSION,
        "task": TASK_CODE,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "read_only_audit",
        "authority_candidate": AUTHORITY_CODE,
        "scope": {
            "floor_code": scope_floor_code,
            "scope_kind": "three_floor_coordinate_normalization_only",
            "warehouse_input_is_reported_separately": True,
        },
        "database": {
            "path": str(database),
            "size": database.stat().st_size,
            "sha256": file_sha256(database),
            "alembic_versions": alembic_rows,
        },
        "published_map": {
            "path": str(map_path),
            "sha256": file_sha256(map_path),
            "floor_revisions": _floor_revision_map(map_document),
            "scope_floor_code": scope_floor_code,
            "scope_floor_revision": scope_floor_revision,
            "scope_floor_sha256": canonical_hash(scope_floor_document),
        },
        "checks": checks,
        "input_quality_findings": input_quality_findings,
        "coordinate_pattern": _coordinate_pattern_summary(rows),
        "protected_snapshot": protected,
        "summary": summary,
        "execution_candidate": {
            "row_count": len(execution_rows),
            "plan_count": len(plan_groups),
            "location_ids_sha256": canonical_hash(execution_location_ids),
        },
        "execution_gate": {
            "rehearsal_ready": not hard_blockers and bool(execution_rows),
            "formal_apply_ready": False,
            "formal_apply_requires": [
                "field_sampling_confirmed",
                "owner_authority_choice_confirmed",
                "hash_bound_approval_file",
                "fresh_isolated_copy_rehearsal",
            ],
        },
        "apply_blockers": sorted(set(blockers)),
        "hard_apply_blockers": sorted(set(hard_blockers)),
        "follow_up_items": follow_up_items,
        "out_of_scope": {
            "source_slot_count": len(out_of_scope_sources),
            "issue_count": len(out_of_scope_findings),
            "issue_counts": out_of_scope_issue_counts,
            "findings": out_of_scope_findings,
            "floor_alias_matches": floor_alias_matches,
        },
        "plans": plan_groups,
        "rows": rows,
    }
    result["plan_sha256"] = canonical_hash(result)
    return result


def _sample_rows(plan: dict[str, Any]) -> list[dict[str, Any]]:
    rows = plan["rows"]
    samples: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()

    def add(
        category: str,
        candidates: list[dict[str, Any]],
        limit: int | None = 5,
    ) -> None:
        ordered = sorted(
            candidates,
            key=lambda row: (
                -float(row["deviation_mm"]),
                -int(bool(row["inventory_quantity"])),
                row["location_code"],
            ),
        )
        selected = ordered if limit is None else ordered[:limit]
        for row in selected:
            key = (category, int(row["location_id"]))
            if key not in seen:
                samples.append({"sample_category": category, **row})
                seen.add(key)

    for prefix in ("A", "B", "D", "E"):
        add(
            f"area_family_{prefix}",
            [
                row
                for row in rows
                if (
                    len(str(row["area_code"] or "")) > 1
                    and str(row["area_code"] or "").upper()[0] == prefix
                    and str(row["area_code"] or "")[1].isdigit()
                )
                and row["change_required"]
            ],
        )
    add(
        "rotated_zones",
        [
            row
            for row in rows
            if row["geometry_class"] == "rotated_rectangle"
            and row["change_required"]
        ],
        limit=None,
    )
    add(
        "non_rectangular_in_bounds_field_confirmation",
        [
            row
            for row in rows
            if row["candidate_disposition"] == "field_confirmation_required"
        ],
        limit=None,
    )
    add(
        "geometry_blocked_full_review",
        [
            row
            for row in rows
            if row["candidate_disposition"] == "geometry_blocked"
        ],
        limit=None,
    )
    return samples


CSV_COLUMNS = (
    "floor_code",
    "area_code",
    "area_name",
    "location_id",
    "location_code",
    "location_name",
    "geometry_class",
    "zone_rotation_deg",
    "current_x_mm",
    "current_y_mm",
    "visible_center_x_mm",
    "visible_center_y_mm",
    "target_x_mm",
    "target_y_mm",
    "delta_x_mm",
    "delta_y_mm",
    "deviation_mm",
    "width_mm",
    "depth_mm",
    "pallet_codes",
    "occupied_batch_lots",
    "inventory_quantity",
    "change_required",
    "candidate_disposition",
    "execution_eligible",
    "unsafe_reason",
)


def _csv_value(value: Any) -> Any:
    return canonical_json(value) if isinstance(value, (list, dict)) else value


def write_audit_outputs(plan: dict[str, Any], output_dir: Path) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    plan_path = output_dir / "p1_147_coordinate_correction_plan.json"
    csv_path = output_dir / "p1_147_coordinate_audit.csv"
    sample_path = output_dir / "p1_147_field_samples.csv"
    geometry_path = output_dir / "p1_147_zone_geometry.csv"
    candidate_path = output_dir / "p1_147_rehearsal_candidates.csv"
    out_of_scope_path = output_dir / "p1_147_out_of_scope_findings.csv"
    floor_alias_path = output_dir / "p1_151_floor_alias_matches.csv"
    summary_path = output_dir / "p1_147_summary.md"
    plan_path.write_text(
        json.dumps(_json_safe(plan), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(
            {key: _csv_value(value) for key, value in row.items()} for row in plan["rows"]
        )
    sample_columns = ("sample_category",) + CSV_COLUMNS
    with sample_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=sample_columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(
            {key: _csv_value(value) for key, value in row.items()}
            for row in _sample_rows(plan)
        )
    with candidate_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(
            {key: _csv_value(value) for key, value in row.items()}
            for row in plan["rows"]
            if row["execution_eligible"]
        )
    out_of_scope_columns = (
        "issue_code",
        "database_floor_code",
        "map_floor_code",
        "area_code",
        "plan_id",
        "location_id",
        "location_code",
        "map_feature_id",
        "policy_map_revision",
        "plan_map_revision",
        "current_map_revision",
        "inventory_quantity",
        "occupied",
        "pallet_codes",
        "database_floor_id",
        "map_formal_floor_id",
        "map_floor_id_state",
        "normalized_map_floor_id",
        "database_area_id",
        "map_formal_area_id",
        "map_area_id_state",
        "normalized_map_area_id",
    )
    with out_of_scope_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=out_of_scope_columns)
        writer.writeheader()
        writer.writerows(
            {key: _csv_value(value) for key, value in finding.items()}
            for finding in plan["out_of_scope"]["findings"]
        )
    floor_alias_columns = (
        "match_basis",
        "database_floor_id",
        "map_formal_floor_id",
        "map_floor_id_state",
        "normalized_map_floor_id",
        "database_floor_code",
        "map_floor_code",
        "database_area_id",
        "map_formal_area_id",
        "map_area_id_state",
        "normalized_map_area_id",
        "area_code",
        "plan_id",
        "location_id",
        "location_code",
        "map_feature_id",
        "inventory_quantity",
        "occupied",
        "pallet_codes",
    )
    with floor_alias_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=floor_alias_columns)
        writer.writeheader()
        writer.writerows(
            {key: _csv_value(value) for key, value in match.items()}
            for match in plan["out_of_scope"]["floor_alias_matches"]
        )
    geometry_rows: dict[tuple[str, str], dict[str, Any]] = {}
    for row in plan["rows"]:
        key = (row["floor_code"], row["map_feature_id"])
        geometry_rows[key] = {
            "floor_code": row["floor_code"],
            "area_code": row["area_code"],
            "area_name": row["area_name"],
            "map_feature_id": row["map_feature_id"],
            "map_revision": row["map_revision"],
            "geometry_class": row["geometry_class"],
            "zone_rotation_deg": row["zone_rotation_deg"],
        }
    with geometry_path.open("w", encoding="utf-8-sig", newline="") as handle:
        columns = (
            "floor_code",
            "area_code",
            "area_name",
            "map_feature_id",
            "map_revision",
            "geometry_class",
            "zone_rotation_deg",
        )
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(geometry_rows.values())
    summary = plan["summary"]
    blocker_lines = "\n".join(f"- {item}" for item in plan["apply_blockers"])
    follow_up_lines = "\n".join(f"- {item}" for item in plan["follow_up_items"])
    if not follow_up_lines:
        follow_up_lines = "- 未发现跨楼层后续问题。"
    pattern = plan["coordinate_pattern"]
    summary_path.write_text(
        "\n".join(
            [
                "# P1-147 仓库历史坐标归一只读审计",
                "",
                f"- 数据库 SHA-256：`{plan['database']['sha256']}`",
                f"- 正式地图 SHA-256：`{plan['published_map']['sha256']}`",
                f"- 全仓输入：{summary['warehouse_published_slot_count']} 个已发布货位 / "
                f"{summary['warehouse_published_plan_count']} 个已发布规划",
                f"- 专项范围：{summary['scope_floor_code']}，"
                f"{summary['scope_audited_slot_count']} 个货位 / "
                f"{summary['scope_published_plan_count']} 个规划",
                f"- 坐标待归一：{summary['scope_changed_slot_count']}；"
                f"坐标一致：{summary['scope_unchanged_slot_count']}",
                f"- 受控复演候选：{summary['rehearsal_candidate_slot_count']}；"
                f"当前占用：{summary['rehearsal_candidate_occupied_slot_count']}",
                f"- 非矩形现场确认：{summary['field_confirmation_slot_count']}；"
                f"当前占用：{summary['field_confirmation_occupied_slot_count']}",
                f"- 几何阻断并排除：{summary['geometry_blocked_slot_count']}；"
                f"当前占用：{summary['geometry_blocked_occupied_slot_count']}",
                f"- 范围外问题：{summary['out_of_scope_issue_count']}；"
                f"当前占用：{summary['out_of_scope_occupied_issue_count']}",
                f"- 已识别同楼层别名：{summary['floor_alias_matches']}",
                f"- 候选权威：`{AUTHORITY_CODE}`",
                "",
                "## 差异模式",
                "",
                f"- 最大横向绝对偏差：{pattern['max_abs_delta_x_mm']} mm",
                f"- 纵向镜像分组：{pattern['multi_row_mirror_group_count']}；"
                f"最大拟合残差：{pattern['max_mirror_residual_mm']} mm",
                f"- 模式判断：`{pattern['pattern']}`",
                "",
                "## 写入门禁",
                "",
                blocker_lines,
                "- 本审计不发布或丢弃任何地图草稿，不修改数据库。",
                "- 当前执行集只含 3F 轴对齐且不越界的候选；其他行不会被 rehearse/apply/rollback 更新。",
                "- `apply` 还要求独立批准文件、源 SHA、CAS、逐行审计和隔离路径确认。",
                "",
                "## 独立后续闭环",
                "",
                follow_up_lines,
                "",
            ]
        ),
        encoding="utf-8",
    )
    return {
        "plan": plan_path,
        "audit_csv": csv_path,
        "samples_csv": sample_path,
        "geometry_csv": geometry_path,
        "rehearsal_candidates_csv": candidate_path,
        "out_of_scope_findings_csv": out_of_scope_path,
        "floor_alias_matches_csv": floor_alias_path,
        "summary": summary_path,
    }


def load_plan(path: Path) -> dict[str, Any]:
    plan = json.loads(path.read_text(encoding="utf-8"))
    expected = plan.pop("plan_sha256", None)
    actual = canonical_hash(plan)
    plan["plan_sha256"] = expected
    if expected != actual:
        raise CoordinateNormalizationError("候选计划哈希不匹配，文件可能被修改。")
    if plan.get("task") != TASK_CODE or plan.get("schema_version") != SCHEMA_VERSION:
        raise CoordinateNormalizationError("候选计划不是受支持的 P1-147 格式。")
    _validate_plan_contract(plan)
    return plan


def _execution_rows(plan: dict[str, Any]) -> list[dict[str, Any]]:
    return [row for row in plan["rows"] if row.get("execution_eligible") is True]


def _validate_plan_contract(plan: dict[str, Any]) -> None:
    scope = plan.get("scope") or {}
    if scope.get("floor_code") != SCOPE_FLOOR_CODE:
        raise CoordinateNormalizationError("候选计划未严格限定为 3F 专项。")
    rows = plan.get("rows")
    plans = plan.get("plans")
    summary = plan.get("summary") or {}
    candidate = plan.get("execution_candidate") or {}
    out_of_scope = plan.get("out_of_scope") or {}
    if not isinstance(rows, list) or not isinstance(plans, list):
        raise CoordinateNormalizationError("候选计划 rows/plans 结构无效。")
    if any(str(row.get("floor_code") or "").upper() != SCOPE_FLOOR_CODE for row in rows):
        raise CoordinateNormalizationError("候选计划 rows 混入非 3F 数据。")
    if any(str(item.get("floor_code") or "").upper() != SCOPE_FLOOR_CODE for item in plans):
        raise CoordinateNormalizationError("候选计划 plans 混入非 3F 数据。")
    location_ids = [int(row["location_id"]) for row in rows]
    ground_slot_ids = [int(row["ground_slot_id"]) for row in rows]
    if len(location_ids) != len(set(location_ids)) or len(ground_slot_ids) != len(
        set(ground_slot_ids)
    ):
        raise CoordinateNormalizationError("候选计划 3F 行主键不唯一。")
    execution_rows = _execution_rows(plan)
    for row in execution_rows:
        if (
            not row.get("change_required")
            or row.get("candidate_disposition") != "rehearsal_candidate"
            or row.get("geometry_class") != "axis_aligned_rectangle"
            or row.get("unsafe_reason")
        ):
            raise CoordinateNormalizationError("候选执行集包含未通过几何门禁的行。")
    execution_location_ids = sorted(int(row["location_id"]) for row in execution_rows)
    expected_plan_ids = sorted({int(row["plan_id"]) for row in execution_rows})
    actual_plan_ids = sorted(int(item["plan_id"]) for item in plans)
    if expected_plan_ids != actual_plan_ids:
        raise CoordinateNormalizationError("候选执行集与 plan CAS 集合不一致。")
    if int(summary.get("scope_audited_slot_count", -1)) != len(rows):
        raise CoordinateNormalizationError("候选计划 3F 审计分母与 rows 不一致。")
    if int(summary.get("rehearsal_candidate_slot_count", -1)) != len(
        execution_rows
    ):
        raise CoordinateNormalizationError("候选计划复演分子与执行集不一致。")
    if int(candidate.get("row_count", -1)) != len(execution_rows):
        raise CoordinateNormalizationError("候选计划 execution_candidate 行数不一致。")
    if int(candidate.get("plan_count", -1)) != len(plans):
        raise CoordinateNormalizationError("候选计划 execution_candidate 规划数不一致。")
    if candidate.get("location_ids_sha256") != canonical_hash(execution_location_ids):
        raise CoordinateNormalizationError("候选计划执行 location id 集合哈希不一致。")
    floor_alias_matches = out_of_scope.get("floor_alias_matches")
    if not isinstance(floor_alias_matches, list) or int(
        summary.get("floor_alias_matches", -1)
    ) != len(floor_alias_matches):
        raise CoordinateNormalizationError("候选计划楼层别名审计结构不一致。")


def _validate_approval(plan: dict[str, Any], approval_path: Path | None) -> dict[str, Any]:
    if plan.get("hard_apply_blockers"):
        raise CoordinateNormalizationError(
            "候选计划仍有结构性阻断，不能通过批准文件绕过："
            + "; ".join(plan["hard_apply_blockers"])
        )
    if approval_path is None:
        raise CoordinateNormalizationError("正式 apply 必须提供独立 --approval 文件。")
    approval = json.loads(approval_path.read_text(encoding="utf-8"))
    required = {
        "task": TASK_CODE,
        "plan_sha256": plan["plan_sha256"],
        "authority": AUTHORITY_CODE,
        "field_sampling_status": "confirmed",
        "scope_floor_code": SCOPE_FLOOR_CODE,
        "approved_candidate_count": plan["execution_candidate"]["row_count"],
        "candidate_location_ids_sha256": plan["execution_candidate"][
            "location_ids_sha256"
        ],
    }
    if any(approval.get(key) != value for key, value in required.items()):
        raise CoordinateNormalizationError("批准文件与任务、计划哈希、权威方案或抽样状态不一致。")
    if not str(approval.get("approved_by") or "").strip() or not str(
        approval.get("approved_at") or ""
    ).strip():
        raise CoordinateNormalizationError("批准文件缺少 approved_by/approved_at。")
    changed_classes = {
        row["geometry_class"] for row in _execution_rows(plan)
    }
    confirmed_classes = set(approval.get("confirmed_geometry_classes") or [])
    if not changed_classes.issubset(confirmed_classes):
        raise CoordinateNormalizationError("批准文件未覆盖全部待修正几何类别。")
    return approval


def _trigger_sql(connection: sqlite3.Connection) -> dict[str, str]:
    result: dict[str, str] = {}
    for name, (table, message) in MUTABLE_TRIGGERS.items():
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (name,)
        ).fetchone()
        if row is None or not row[0]:
            raise CoordinateNormalizationError(f"缺少受控修正所需保护触发器：{name}")
        normalized = " ".join(str(row[0]).lower().split())
        if table not in normalized or "before update" not in normalized or message not in normalized:
            raise CoordinateNormalizationError(f"保护触发器定义不符合预期：{name}")
        result[name] = str(row[0])
    return result


def _restore_triggers(connection: sqlite3.Connection, triggers: dict[str, str]) -> None:
    for name, sql in triggers.items():
        connection.execute(sql)
        if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name=?", (name,)
        ).fetchone() is None:
            raise CoordinateNormalizationError(f"保护触发器恢复失败：{name}")


def _operation_seen(connection: sqlite3.Connection, batch_id: str, action_code: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM operation_logs WHERE batch_id=? AND action_code=? LIMIT 1",
            (batch_id, action_code),
        ).fetchone()
        is not None
    )


def _assert_actor(connection: sqlite3.Connection, actor_user_id: int) -> str | None:
    row = connection.execute("SELECT * FROM users WHERE id=?", (actor_user_id,)).fetchone()
    if row is None:
        raise CoordinateNormalizationError("actor-user-id 不存在。")
    keys = set(row.keys())
    for key in ("full_name", "display_name", "username"):
        if key in keys and row[key]:
            return str(row[key])
    return None


def _decimal_matches(actual: Any, expected: Any, tolerance: str = "0.0005") -> bool:
    return abs(Decimal(str(actual)) - Decimal(str(expected))) <= Decimal(tolerance)


def _row_contract_matches(
    connection: sqlite3.Connection,
    row: dict[str, Any],
    *,
    target: bool,
) -> bool:
    actual = connection.execute(
        """
        SELECT
          s.plan_id,s.location_id,s.route_sequence,s.row_no,s.slot_no,
          s.x_mm,s.y_mm,s.width_mm,s.depth_mm,
          p.area_id,p.status AS plan_status,p.published_map_revision,
          p.preview_fingerprint,p.version AS plan_version,p.updated_at AS plan_updated_at,
          a.address_version AS area_version,
          f.floor_code,
          policy.id AS policy_id,policy.status AS policy_status,
          policy.map_feature_id,policy.published_map_revision AS policy_map_revision,
          policy.version AS policy_version,
          l.location_code,l.address_version AS location_address_version,
          l.placement_status,l.storage_type,l.is_active,
          fl.id AS layout_id,fl.location_id AS layout_location_id,
          fl.left_pct,fl.top_pct,fl.width_pct,fl.height_pct,
          fl.version AS layout_version,fl.source_type,fl.layout_kind,
          fl.updated_at AS layout_updated_at
        FROM warehouse_ground_layout_slots AS s
        JOIN warehouse_ground_layout_plans AS p ON p.id=s.plan_id
        JOIN warehouse_areas AS a ON a.id=p.area_id
        JOIN warehouse_floors AS f ON f.id=a.floor_id
        JOIN warehouse_area_storage_policies AS policy ON policy.area_id=a.id
        JOIN warehouse_locations AS l ON l.id=s.location_id
        JOIN floor3_location_layouts AS fl ON fl.location_id=l.id
        WHERE s.id=?
        """,
        (row["ground_slot_id"],),
    ).fetchone()
    if actual is None:
        return False
    expected_x = row["target_x_mm"] if target and row["execution_eligible"] else row[
        "current_x_mm"
    ]
    expected_y = row["target_y_mm"] if target and row["execution_eligible"] else row[
        "current_y_mm"
    ]
    exact_expectations = {
        "plan_id": int(row["plan_id"]),
        "location_id": int(row["location_id"]),
        "route_sequence": int(row["route_sequence"]),
        "row_no": int(row["row_no"]),
        "slot_no": int(row["slot_no"]),
        "width_mm": int(row["width_mm"]),
        "depth_mm": int(row["depth_mm"]),
        "area_id": int(row["area_id"]),
        "plan_status": "published",
        "published_map_revision": row["map_revision"],
        "preview_fingerprint": row["plan_preview_fingerprint"],
        "plan_version": int(row["plan_version"]),
        "plan_updated_at": row["plan_updated_at"],
        "area_version": int(row["area_version"]),
        "floor_code": SCOPE_FLOOR_CODE,
        "policy_id": int(row["policy_id"]),
        "policy_status": "published",
        "map_feature_id": row["map_feature_id"],
        "policy_map_revision": row["map_revision"],
        "policy_version": int(row["policy_version"]),
        "location_code": row["location_code"],
        "location_address_version": int(row["location_address_version"]),
        "placement_status": row["location_placement_status"],
        "storage_type": row["location_storage_type"],
        "is_active": int(row["location_is_active"]),
        "layout_id": int(row["layout_id"]),
        "layout_location_id": int(row["location_id"]),
        "layout_version": int(row["layout_version"]),
        "source_type": row["layout_source_type"],
        "layout_kind": row["layout_kind"],
        "layout_updated_at": row["layout_updated_at"],
    }
    for key, expected in exact_expectations.items():
        if actual[key] != expected:
            return False
    for key, expected in (
        ("left_pct", row["left_pct"]),
        ("top_pct", row["top_pct"]),
        ("width_pct", row["width_pct"]),
        ("height_pct", row["height_pct"]),
    ):
        if not _decimal_matches(actual[key], expected, "0.000000001"):
            return False
    return _decimal_matches(actual["x_mm"], expected_x) and _decimal_matches(
        actual["y_mm"], expected_y
    )


def _state_matches(connection: sqlite3.Connection, plan: dict[str, Any], target: bool) -> bool:
    return all(
        _row_contract_matches(connection, row, target=target) for row in plan["rows"]
    )


def _rollback_state_matches(connection: sqlite3.Connection, plan: dict[str, Any]) -> bool:
    return _state_matches(connection, plan, False)


def _insert_audit(
    connection: sqlite3.Connection,
    *,
    actor_user_id: int,
    actor_name: str | None,
    batch_id: str,
    action_code: str,
    row: dict[str, Any],
    direction: str,
) -> None:
    before = {
        "x_mm": row["current_x_mm"] if direction == "apply" else row["target_x_mm"],
        "y_mm": row["current_y_mm"] if direction == "apply" else row["target_y_mm"],
    }
    after = {
        "x_mm": row["target_x_mm"] if direction == "apply" else row["current_x_mm"],
        "y_mm": row["target_y_mm"] if direction == "apply" else row["current_y_mm"],
    }
    request_id = canonical_hash(
        {"batch_id": batch_id, "slot_id": row["ground_slot_id"], "direction": direction}
    )
    details = canonical_json(
        {
            "task": TASK_CODE,
            "authority": AUTHORITY_CODE,
            "direction": direction,
            "floor_code": row["floor_code"],
            "area_code": row["area_code"],
            "location_id": row["location_id"],
            "location_code": row["location_code"],
            "ground_slot_id": row["ground_slot_id"],
            "plan_id": row["plan_id"],
            "layout_id": row["layout_id"],
            "layout_version": row["layout_version"],
            "policy_id": row["policy_id"],
            "policy_version": row["policy_version"],
            "map_feature_id": row["map_feature_id"],
            "map_revision": row["map_revision"],
            "before": before,
            "after": after,
            "inventory_effect": "none",
        }
    )
    connection.execute(
        """
        INSERT INTO operation_logs
        (user_id,action,resource,details,username,entity_type,entity_id,description,
         extra_json,event_category,result,source,module_code,action_code,
         actor_user_id_snapshot,operator_name_snapshot,object_ref,request_id,batch_id,
         schema_version)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            actor_user_id,
            "UPDATE",
            "warehouse_ground_layout_slots",
            details,
            actor_name,
            "warehouse_ground_layout_slot",
            row["ground_slot_id"],
            f"P1-147 {direction} location {row['location_code']}",
            details,
            "warehouse_correction",
            "success",
            "admin_script",
            "warehouse",
            action_code,
            actor_user_id,
            actor_name,
            f"warehouse_location:{row['location_id']}",
            request_id,
            batch_id,
            SCHEMA_VERSION,
        ),
    )


def _mutate(
    connection: sqlite3.Connection,
    plan: dict[str, Any],
    *,
    actor_user_id: int,
    direction: str,
    rollback_after_validation: bool,
) -> dict[str, Any]:
    if direction not in {"apply", "rollback"}:
        raise ValueError(direction)
    action_code = (
        "P1_147_COORDINATE_NORMALIZE"
        if direction == "apply"
        else "P1_147_COORDINATE_ROLLBACK"
    )
    batch_id = canonical_hash(
        {"plan_sha256": plan["plan_sha256"], "direction": direction}
    )
    already_at_target = (
        _state_matches(connection, plan, True)
        if direction == "apply"
        else _rollback_state_matches(connection, plan)
    )
    if _operation_seen(connection, batch_id, action_code) and already_at_target:
        return {"status": "idempotent_replay", "batch_id": batch_id, "changed": 0}
    actor_name = _assert_actor(connection, actor_user_id)
    connection.execute("BEGIN IMMEDIATE")
    try:
        source_is_target_state = direction == "rollback"
        if not _state_matches(connection, plan, source_is_target_state):
            raise CoordinateNormalizationError(
                "计划内 3F 坐标、布局版本或规划版本已变化，CAS 前置校验失败，整批回滚。"
            )
        protected_before = protected_snapshot(connection)
        triggers = _trigger_sql(connection)
        for trigger_name in triggers:
            connection.execute(f'DROP TRIGGER "{trigger_name}"')
        changed = 0
        for row in plan["rows"]:
            if not row["execution_eligible"]:
                continue
            source_x = row["current_x_mm"] if direction == "apply" else row["target_x_mm"]
            source_y = row["current_y_mm"] if direction == "apply" else row["target_y_mm"]
            target_x = row["target_x_mm"] if direction == "apply" else row["current_x_mm"]
            target_y = row["target_y_mm"] if direction == "apply" else row["current_y_mm"]
            result = connection.execute(
                """
                UPDATE warehouse_ground_layout_slots
                SET x_mm=?,y_mm=?
                WHERE id=? AND plan_id=? AND location_id=? AND route_sequence=?
                  AND row_no=? AND slot_no=? AND width_mm=? AND depth_mm=?
                  AND abs(x_mm-?)<=0.0005 AND abs(y_mm-?)<=0.0005
                  AND EXISTS (
                    SELECT 1 FROM floor3_location_layouts fl
                    WHERE fl.id=? AND fl.location_id=? AND fl.version=?
                      AND fl.left_pct=? AND fl.top_pct=?
                      AND fl.width_pct=? AND fl.height_pct=?
                      AND fl.source_type=? AND fl.layout_kind=? AND fl.updated_at=?
                  )
                  AND EXISTS (
                    SELECT 1 FROM warehouse_locations l
                    WHERE l.id=? AND l.location_code=? AND l.address_version=?
                      AND l.placement_status=? AND l.storage_type=? AND l.is_active=?
                  )
                  AND EXISTS (
                    SELECT 1
                    FROM warehouse_ground_layout_plans p
                    JOIN warehouse_areas a ON a.id=p.area_id
                    JOIN warehouse_floors f ON f.id=a.floor_id
                    JOIN warehouse_area_storage_policies policy ON policy.area_id=p.area_id
                    WHERE p.id=warehouse_ground_layout_slots.plan_id
                      AND p.area_id=? AND p.status='published'
                      AND p.version=? AND p.preview_fingerprint=? AND p.updated_at=?
                      AND p.published_map_revision=?
                      AND a.id=? AND a.address_version=? AND f.floor_code=?
                      AND policy.id=? AND policy.version=?
                      AND policy.status='published'
                      AND policy.map_feature_id=?
                      AND policy.published_map_revision=?
                  )
                """,
                (
                    target_x,
                    target_y,
                    row["ground_slot_id"],
                    row["plan_id"],
                    row["location_id"],
                    row["route_sequence"],
                    row["row_no"],
                    row["slot_no"],
                    row["width_mm"],
                    row["depth_mm"],
                    source_x,
                    source_y,
                    row["layout_id"],
                    row["location_id"],
                    row["layout_version"],
                    row["left_pct"],
                    row["top_pct"],
                    row["width_pct"],
                    row["height_pct"],
                    row["layout_source_type"],
                    row["layout_kind"],
                    row["layout_updated_at"],
                    row["location_id"],
                    row["location_code"],
                    row["location_address_version"],
                    row["location_placement_status"],
                    row["location_storage_type"],
                    row["location_is_active"],
                    row["area_id"],
                    row["plan_version"],
                    row["plan_preview_fingerprint"],
                    row["plan_updated_at"],
                    row["map_revision"],
                    row["area_id"],
                    row["area_version"],
                    SCOPE_FLOOR_CODE,
                    row["policy_id"],
                    row["policy_version"],
                    row["map_feature_id"],
                    row["map_revision"],
                ),
            )
            if result.rowcount != 1:
                raise CoordinateNormalizationError(
                    f"slot {row['ground_slot_id']} CAS 失败，整批回滚。"
                )
            _insert_audit(
                connection,
                actor_user_id=actor_user_id,
                actor_name=actor_name,
                batch_id=batch_id,
                action_code=action_code,
                row=row,
                direction=direction,
            )
            changed += 1
        _restore_triggers(connection, triggers)
        protected_after = protected_snapshot(connection)
        if protected_after != protected_before:
            raise CoordinateNormalizationError("保护表摘要发生变化，整批回滚。")
        checks = _check_database(connection)
        if not checks["ok"]:
            raise CoordinateNormalizationError("修正后完整性/外键检查失败，整批回滚。")
        expected_target_state = direction == "apply"
        if not _state_matches(connection, plan, expected_target_state):
            raise CoordinateNormalizationError("修正后目标状态验证失败。")
        if rollback_after_validation:
            connection.rollback()
            if protected_snapshot(connection) != protected_before or not _state_matches(
                connection, plan, source_is_target_state
            ):
                raise CoordinateNormalizationError("复演事务回滚后的源状态验证失败。")
            return {
                "status": "rehearsed_and_rolled_back",
                "batch_id": batch_id,
                "changed": changed,
                "checks": checks,
            }
        connection.commit()
        return {
            "status": "applied" if direction == "apply" else "rolled_back",
            "batch_id": batch_id,
            "changed": changed,
            "checks": checks,
        }
    except Exception:
        connection.rollback()
        raise


def _verified_backup(database: Path, output_dir: Path, plan_sha: str) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target = output_dir / f"{database.stem}.before_p1_147_{stamp}_{plan_sha[:12]}.sqlite3"
    shutil.copy2(database, target)
    if target.stat().st_size != database.stat().st_size or file_sha256(target) != file_sha256(database):
        target.unlink(missing_ok=True)
        raise CoordinateNormalizationError("修正前备份大小或 SHA-256 校验失败。")
    with readonly_database(target) as connection:
        checks = _check_database(connection)
    if not checks["ok"]:
        target.unlink(missing_ok=True)
        raise CoordinateNormalizationError("修正前备份完整性或外键检查失败。")
    return {
        "path": str(target),
        "size": target.stat().st_size,
        "sha256": file_sha256(target),
        "checks": checks,
    }


def execute_plan(
    *,
    database: Path,
    plan_path: Path,
    actor_user_id: int,
    direction: str,
    confirm_isolated_copy: bool,
    token: str,
    approval_path: Path | None = None,
    backup_dir: Path | None = None,
    rehearse: bool = False,
    published_map: Path | None = None,
) -> dict[str, Any]:
    database = database.resolve(strict=True)
    _assert_isolated_database(database, confirm_isolated_copy)
    plan = load_plan(plan_path)
    if plan.get("hard_apply_blockers"):
        raise CoordinateNormalizationError(
            "候选计划仍有 3F 结构性阻断，rehearse/apply/rollback 均拒绝执行："
            + "; ".join(plan["hard_apply_blockers"])
        )
    if not _execution_rows(plan):
        raise CoordinateNormalizationError("候选计划没有通过门禁的 3F 执行行。")
    if published_map is None:
        raise CoordinateNormalizationError("受控操作必须提供当前 --published-map。")
    published_map = published_map.resolve(strict=True)
    map_document, _ = _load_map(published_map)
    scope_floor_document = _scope_floor_document(map_document, SCOPE_FLOOR_CODE)
    if (
        str(scope_floor_document.get("revision") or "")
        != plan["published_map"]["scope_floor_revision"]
        or canonical_hash(scope_floor_document)
        != plan["published_map"]["scope_floor_sha256"]
    ):
        raise CoordinateNormalizationError(
            "当前 3F 正式地图 revision/内容哈希与审计计划不一致。"
        )
    expected_token = APPLY_TOKEN if direction == "apply" else ROLLBACK_TOKEN
    if token != expected_token:
        raise CoordinateNormalizationError("受控操作确认 token 不匹配。")
    if direction == "apply" and not rehearse:
        _validate_approval(plan, approval_path)
    current_sha = file_sha256(database)
    backup = None
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        _required_tables(connection)
        action_code = (
            "P1_147_COORDINATE_NORMALIZE"
            if direction == "apply"
            else "P1_147_COORDINATE_ROLLBACK"
        )
        batch_id = canonical_hash(
            {"plan_sha256": plan["plan_sha256"], "direction": direction}
        )
        already_at_target = (
            _state_matches(connection, plan, True)
            if direction == "apply"
            else _rollback_state_matches(connection, plan)
        )
        if _operation_seen(connection, batch_id, action_code) and already_at_target:
            return {
                "status": "idempotent_replay",
                "batch_id": batch_id,
                "changed": 0,
                "backup": backup,
            }
        if direction == "apply" and current_sha != plan["database"]["sha256"]:
            raise CoordinateNormalizationError("数据库 SHA-256 与只读审计计划不一致。")
        if not rehearse:
            backup = _verified_backup(
                database,
                backup_dir or (plan_path.parent / "backups"),
                plan["plan_sha256"],
            )
        result = _mutate(
            connection,
            plan,
            actor_user_id=actor_user_id,
            direction=direction,
            rollback_after_validation=rehearse,
        )
        result["backup"] = backup
        result["database_sha256_after"] = file_sha256(database)
        if rehearse and result["database_sha256_after"] != current_sha:
            raise CoordinateNormalizationError(
                "复演虽已事务回滚，但数据库文件 SHA-256 发生变化，拒绝通过。"
            )
        return result
    finally:
        connection.close()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="P1-147 仓库历史坐标只读审计、隔离复演与受控修正。"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    audit = subparsers.add_parser("audit")
    audit.add_argument("--database", type=Path, required=True)
    audit.add_argument("--published-map", type=Path, required=True)
    audit.add_argument("--output-dir", type=Path, required=True)
    audit.add_argument(
        "--scope-floor",
        choices=(SCOPE_FLOOR_CODE,),
        default=SCOPE_FLOOR_CODE,
        help="P1-147 固定为 3F 专项；全仓数量只作输入透明度统计。",
    )
    for command in ("rehearse", "apply", "rollback"):
        child = subparsers.add_parser(command)
        child.add_argument("--database", type=Path, required=True)
        child.add_argument("--plan", type=Path, required=True)
        child.add_argument("--published-map", type=Path, required=True)
        child.add_argument("--actor-user-id", type=int, required=True)
        child.add_argument("--confirm-isolated-copy", action="store_true")
        child.add_argument("--token", required=True)
        if command == "apply":
            child.add_argument("--approval", type=Path, required=True)
            child.add_argument("--backup-dir", type=Path)
        elif command == "rollback":
            child.add_argument("--backup-dir", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "audit":
            plan = build_audit(
                args.database,
                args.published_map,
                scope_floor_code=args.scope_floor,
            )
            paths = write_audit_outputs(plan, args.output_dir)
            print(
                json.dumps(
                    {
                        "status": "audited",
                        "summary": plan["summary"],
                        "apply_blockers": plan["apply_blockers"],
                        "hard_apply_blockers": plan["hard_apply_blockers"],
                        "execution_gate": plan["execution_gate"],
                        "plan_sha256": plan["plan_sha256"],
                        "outputs": {key: str(value) for key, value in paths.items()},
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0 if plan["execution_gate"]["rehearsal_ready"] else 2
        direction = "rollback" if args.command == "rollback" else "apply"
        rehearse = args.command == "rehearse"
        result = execute_plan(
            database=args.database,
            plan_path=args.plan,
            actor_user_id=args.actor_user_id,
            direction=direction,
            confirm_isolated_copy=args.confirm_isolated_copy,
            token=args.token,
            approval_path=getattr(args, "approval", None),
            backup_dir=getattr(args, "backup_dir", None),
            rehearse=rehearse,
            published_map=args.published_map,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (CoordinateNormalizationError, OSError, sqlite3.Error, json.JSONDecodeError) as error:
        print(f"P1-147 blocked: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
