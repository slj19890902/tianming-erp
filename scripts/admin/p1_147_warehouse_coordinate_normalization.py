from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import shutil
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable


TASK_CODE = "P1-147"
AUTHORITY_CODE = "published_measured_geometry_plus_visible_percent_layout"
SCHEMA_VERSION = 1
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
    "warehouse_locations",
    "floor3_location_layouts",
)
MUTABLE_TRIGGERS = {
    "trg_ground_plans_published_immutable": (
        "warehouse_ground_layout_plans",
        "published ground layout plan is immutable",
    ),
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


def ground_preview_fingerprint(
    *, area_id: int, policy_version: int, map_revision: str, plan: sqlite3.Row, rows: list[dict]
) -> str:
    configuration = {
        "target_slot_count": int(plan["target_slot_count"]),
        "numbering_origin": plan["numbering_origin"],
        "row_direction": plan["row_direction"],
        "slot_direction": plan["slot_direction"],
        "row_start_no": int(plan["row_start_no"]),
        "slot_start_no": int(plan["slot_start_no"]),
    }
    slots = []
    for row in sorted(rows, key=lambda value: (value["route_sequence"], value["ground_slot_id"])):
        slots.append(
            {
                "route_sequence": row["route_sequence"],
                "row_no": row["row_no"],
                "slot_no": row["slot_no"],
                "location_code": row["location_code"],
                "x_mm": Decimal(row["target_x_mm"]),
                "y_mm": Decimal(row["target_y_mm"]),
                "width_mm": row["width_mm"],
                "depth_mm": row["depth_mm"],
                "left_pct": row["left_pct"],
                "top_pct": row["top_pct"],
                "width_pct": row["width_pct"],
                "height_pct": row["height_pct"],
                "existing_location_id": row["location_id"],
                "existing_layout_version": row["layout_version"],
            }
        )
    return canonical_hash(
        {
            "rule_version": "P1-87",
            "area_id": area_id,
            "policy_version": policy_version,
            "map_revision": map_revision,
            "configuration": configuration,
            "slots": slots,
        }
    )


def build_audit(database: Path, map_path: Path) -> dict[str, Any]:
    database = database.resolve(strict=True)
    map_path = map_path.resolve(strict=True)
    _assert_isolated_database(database, True)
    map_document, feature_by_id = _load_map(map_path)
    with readonly_database(database) as connection:
        _required_tables(connection)
        checks = _check_database(connection)
        if not checks["ok"]:
            raise CoordinateNormalizationError("数据库完整性或外键检查未通过。")
        source_rows = _query_rows(connection)
        published_slot_count = int(
            connection.execute(
                """
                SELECT count(*)
                FROM warehouse_ground_layout_slots s
                JOIN warehouse_ground_layout_plans p ON p.id=s.plan_id
                WHERE p.status='published'
                """
            ).fetchone()[0]
        )
        published_plan_count = int(
            connection.execute(
                "SELECT count(*) FROM warehouse_ground_layout_plans WHERE status='published'"
            ).fetchone()[0]
        )
        inventory = _inventory_evidence(
            connection, [int(row["location_id"]) for row in source_rows]
        )
        protected = protected_snapshot(connection)
        alembic_rows = [row[0] for row in connection.execute("SELECT version_num FROM alembic_version")]
    rows: list[dict[str, Any]] = []
    blockers: list[str] = []
    hard_blockers: list[str] = []
    if len(source_rows) != published_slot_count:
        message = (
            f"发布地堆明细共 {published_slot_count} 行，但只有 {len(source_rows)} 行具备正式策略、"
            "货位与百分比布局完整关联"
        )
        blockers.append(message)
        hard_blockers.append(message)
    for source in source_rows:
        feature_id = str(source["map_feature_id"] or "").strip()
        feature = feature_by_id.get(feature_id)
        if feature is None:
            message = f"location {source['location_id']} 的正式区域 {feature_id!r} 不在正式地图中"
            blockers.append(message)
            hard_blockers.append(message)
            continue
        floor_code = str(source["floor_code"] or "").upper()
        map_revision = str(feature["floor_revision"])
        if feature["floor_code"] != floor_code:
            message = f"location {source['location_id']} 的区域跨楼层绑定"
            blockers.append(message)
            hard_blockers.append(message)
            continue
        if source["policy_map_revision"] != map_revision or source["published_map_revision"] != map_revision:
            message = f"location {source['location_id']} 的 policy/plan 地图版本与正式地图不一致"
            blockers.append(message)
            hard_blockers.append(message)
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
        width_delta = abs(geometry["mapped_width_mm"] - Decimal(int(source["width_mm"])))
        depth_delta = abs(geometry["mapped_depth_mm"] - Decimal(int(source["depth_mm"])))
        unsafe_reason = None
        if geometry["geometry_class"] == "rotated_rectangle":
            unsafe_reason = "rotated_rectangle_requires_explicit_field_sample"
            message = (
                f"location {source['location_id']} 位于旋转矩形区域；当前地堆表没有旋转字段，"
                "不能仅凭中心点自动修正"
            )
            blockers.append(message)
            hard_blockers.append(message)
        elif width_delta > Decimal("2.000") or depth_delta > Decimal("2.000"):
            unsafe_reason = "visible_size_does_not_match_ground_slot_contract"
            message = (
                f"location {source['location_id']} 的可见尺寸与地堆标准尺寸不一致，"
                "不能只修正坐标"
            )
            blockers.append(message)
            hard_blockers.append(message)
        elif not _target_footprint_inside_zone(
            feature.get("points") or [],
            x_mm=geometry["target_x_mm"],
            y_mm=geometry["target_y_mm"],
            width_mm=int(source["width_mm"]),
            depth_mm=int(source["depth_mm"]),
        ):
            unsafe_reason = "target_footprint_crosses_published_zone"
            message = (
                f"location {source['location_id']} 的候选标准矩形越出已发布区域边界，"
                "不能只修正坐标"
            )
            blockers.append(message)
            hard_blockers.append(message)
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
            "area_version": int(source["area_version"]),
            "pallet_codes": evidence["pallet_codes"],
            "occupied_batch_lots": evidence["occupancy_lots"] or evidence["direct_lots"],
            "direct_inventory_lots": evidence["direct_lots"],
            "direct_inventory_quantity": evidence["direct_quantity"],
            "occupancy_ids": evidence["occupancy_ids"],
            "occupancy_inventory_quantity": evidence["occupancy_quantity"],
            "inventory_quantity": max(
                int(evidence["direct_quantity"]), int(evidence["occupancy_quantity"])
            ),
            "change_required": abs(delta_x) > CHANGE_TOLERANCE_MM
            or abs(delta_y) > CHANGE_TOLERANCE_MM,
            "unsafe_reason": unsafe_reason,
        }
        rows.append(row)
    plan_groups: list[dict[str, Any]] = []
    for plan_id in sorted({row["plan_id"] for row in rows}):
        group = [row for row in rows if row["plan_id"] == plan_id]
        source = next(row for row in source_rows if int(row["plan_id"]) == plan_id)
        if len(group) != int(source["target_slot_count"]):
            message = (
                f"plan {plan_id} 声明 {source['target_slot_count']} 个位置，实际可审计 {len(group)} 个"
            )
            blockers.append(message)
            hard_blockers.append(message)
        target_fingerprint = ground_preview_fingerprint(
            area_id=int(source["area_id"]),
            policy_version=int(source["policy_version"]),
            map_revision=str(source["published_map_revision"]),
            plan=source,
            rows=group,
        )
        plan_group = {
                "plan_id": plan_id,
                "area_id": int(source["area_id"]),
                "floor_code": source["floor_code"],
                "area_code": source["area_code"],
                "expected_version": int(source["plan_version"]),
                "target_version": int(source["plan_version"]) + 1,
                "expected_preview_fingerprint": source["preview_fingerprint"],
                "target_preview_fingerprint": target_fingerprint,
                "expected_updated_at": source["plan_updated_at"],
                "policy_id": int(source["policy_id"]),
                "policy_version": int(source["policy_version"]),
                "map_revision": source["published_map_revision"],
                "row_count": len(group),
                "changed_row_count": sum(bool(row["change_required"]) for row in group),
            }
        if plan_group["changed_row_count"]:
            plan_groups.append(plan_group)
    change_rows = [row for row in rows if row["change_required"]]
    unsafe_rows = [row for row in change_rows if row["unsafe_reason"]]
    if unsafe_rows:
        blockers.append(
            f"{len(unsafe_rows)} 个待修正位置属于旋转或尺寸契约不一致区域，必须逐点现场确认"
        )
    result = {
        "schema_version": SCHEMA_VERSION,
        "task": TASK_CODE,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "read_only_audit",
        "authority_candidate": AUTHORITY_CODE,
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
        },
        "checks": checks,
        "protected_snapshot": protected,
        "summary": {
            "published_slot_count": len(rows),
            "changed_slot_count": len(change_rows),
            "unchanged_slot_count": len(rows) - len(change_rows),
            "unsafe_changed_slot_count": len(unsafe_rows),
            "plan_count": len(plan_groups),
            "published_plan_count": published_plan_count,
            "geometry_counts": {
                kind: sum(row["geometry_class"] == kind for row in rows)
                for kind in (
                    "axis_aligned_rectangle",
                    "rotated_rectangle",
                    "non_rectangular",
                )
            },
        },
        "apply_blockers": sorted(set(blockers)),
        "hard_apply_blockers": sorted(set(hard_blockers)),
        "plans": plan_groups,
        "rows": rows,
    }
    result["plan_sha256"] = canonical_hash(result)
    return result


def _sample_rows(plan: dict[str, Any]) -> list[dict[str, Any]]:
    rows = plan["rows"]
    samples: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()

    def add(category: str, candidates: list[dict[str, Any]], limit: int = 5) -> None:
        ordered = sorted(
            candidates,
            key=lambda row: (
                -float(row["deviation_mm"]),
                -int(bool(row["inventory_quantity"])),
                row["location_code"],
            ),
        )
        for row in ordered[:limit]:
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
        [row for row in rows if row["geometry_class"] == "rotated_rectangle"],
    )
    add(
        "non_rectangular_zones",
        [row for row in rows if row["geometry_class"] == "non_rectangular"],
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
    blocker_lines = "\n".join(f"- {item}" for item in plan["apply_blockers"]) or "- 无结构性阻断；仍须完成现场抽样并取得老板确认。"
    summary_path.write_text(
        "\n".join(
            [
                "# P1-147 仓库历史坐标归一只读审计",
                "",
                f"- 数据库 SHA-256：`{plan['database']['sha256']}`",
                f"- 正式地图 SHA-256：`{plan['published_map']['sha256']}`",
                f"- 发布地堆位置：{summary['published_slot_count']}",
                f"- 坐标待归一：{summary['changed_slot_count']}",
                f"- 坐标一致：{summary['unchanged_slot_count']}",
                f"- 高风险待现场确认：{summary['unsafe_changed_slot_count']}",
                f"- 候选权威：`{AUTHORITY_CODE}`",
                "",
                "## 写入门禁",
                "",
                blocker_lines,
                "- 本审计不发布或丢弃任何地图草稿，不修改数据库。",
                "- `apply` 还要求独立批准文件、源 SHA、CAS、逐行审计和隔离路径确认。",
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
    return plan


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
    }
    if any(approval.get(key) != value for key, value in required.items()):
        raise CoordinateNormalizationError("批准文件与任务、计划哈希、权威方案或抽样状态不一致。")
    if not str(approval.get("approved_by") or "").strip() or not str(
        approval.get("approved_at") or ""
    ).strip():
        raise CoordinateNormalizationError("批准文件缺少 approved_by/approved_at。")
    changed_classes = {
        row["geometry_class"] for row in plan["rows"] if row["change_required"]
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


def _state_matches(connection: sqlite3.Connection, plan: dict[str, Any], target: bool) -> bool:
    for row in plan["rows"]:
        if not row["change_required"]:
            continue
        actual = connection.execute(
            "SELECT x_mm,y_mm FROM warehouse_ground_layout_slots WHERE id=?",
            (row["ground_slot_id"],),
        ).fetchone()
        if actual is None:
            return False
        expected_x = Decimal(row["target_x_mm"] if target else row["current_x_mm"])
        expected_y = Decimal(row["target_y_mm"] if target else row["current_y_mm"])
        if abs(Decimal(str(actual[0])) - expected_x) > Decimal("0.0005") or abs(
            Decimal(str(actual[1])) - expected_y
        ) > Decimal("0.0005"):
            return False
    for item in plan["plans"]:
        actual = connection.execute(
            "SELECT version,preview_fingerprint FROM warehouse_ground_layout_plans WHERE id=?",
            (item["plan_id"],),
        ).fetchone()
        if actual is None:
            return False
        expected_version = item["target_version"] if target else item["expected_version"]
        expected_fingerprint = (
            item["target_preview_fingerprint"]
            if target
            else item["expected_preview_fingerprint"]
        )
        if int(actual[0]) != int(expected_version) or actual[1] != expected_fingerprint:
            return False
    return True


def _rollback_state_matches(connection: sqlite3.Connection, plan: dict[str, Any]) -> bool:
    for row in plan["rows"]:
        if not row["change_required"]:
            continue
        actual = connection.execute(
            "SELECT x_mm,y_mm FROM warehouse_ground_layout_slots WHERE id=?",
            (row["ground_slot_id"],),
        ).fetchone()
        if actual is None or abs(
            Decimal(str(actual[0])) - Decimal(row["current_x_mm"])
        ) > Decimal("0.0005") or abs(
            Decimal(str(actual[1])) - Decimal(row["current_y_mm"])
        ) > Decimal("0.0005"):
            return False
    for item in plan["plans"]:
        actual = connection.execute(
            "SELECT version,preview_fingerprint FROM warehouse_ground_layout_plans WHERE id=?",
            (item["plan_id"],),
        ).fetchone()
        if (
            actual is None
            or int(actual[0]) < int(item["target_version"]) + 1
            or actual[1] != item["expected_preview_fingerprint"]
        ):
            return False
    return True


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
    target_state = direction == "apply"
    already_at_target = (
        _state_matches(connection, plan, True)
        if direction == "apply"
        else _rollback_state_matches(connection, plan)
    )
    if _operation_seen(connection, batch_id, action_code) and already_at_target:
        return {"status": "idempotent_replay", "batch_id": batch_id, "changed": 0}
    actor_name = _assert_actor(connection, actor_user_id)
    protected_before = protected_snapshot(connection)
    triggers = _trigger_sql(connection)
    connection.execute("BEGIN IMMEDIATE")
    try:
        for trigger_name in triggers:
            connection.execute(f'DROP TRIGGER "{trigger_name}"')
        changed = 0
        for row in plan["rows"]:
            if not row["change_required"]:
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
                  )
                  AND EXISTS (
                    SELECT 1
                    FROM warehouse_ground_layout_plans p
                    JOIN warehouse_area_storage_policies policy ON policy.area_id=p.area_id
                    WHERE p.id=warehouse_ground_layout_slots.plan_id
                      AND policy.id=? AND policy.version=?
                      AND policy.status='published'
                      AND policy.map_feature_id=?
                      AND policy.published_map_revision=?
                      AND p.published_map_revision=?
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
                    row["policy_id"],
                    row["policy_version"],
                    row["map_feature_id"],
                    row["map_revision"],
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
        for item in plan["plans"]:
            source_version = (
                item["expected_version"] if direction == "apply" else item["target_version"]
            )
            target_version = int(source_version) + 1 if direction == "rollback" else item["target_version"]
            source_fingerprint = (
                item["expected_preview_fingerprint"]
                if direction == "apply"
                else item["target_preview_fingerprint"]
            )
            target_fingerprint = (
                item["target_preview_fingerprint"]
                if direction == "apply"
                else item["expected_preview_fingerprint"]
            )
            result = connection.execute(
                """
                UPDATE warehouse_ground_layout_plans
                SET preview_fingerprint=?,version=?,updated_by=?,updated_at=CURRENT_TIMESTAMP
                WHERE id=? AND area_id=? AND status='published'
                  AND version=? AND preview_fingerprint=?
                  AND published_map_revision=?
                  AND EXISTS (
                    SELECT 1 FROM warehouse_area_storage_policies policy
                    WHERE policy.id=? AND policy.area_id=warehouse_ground_layout_plans.area_id
                      AND policy.version=? AND policy.status='published'
                      AND policy.published_map_revision=?
                  )
                """,
                (
                    target_fingerprint,
                    target_version,
                    actor_user_id,
                    item["plan_id"],
                    item["area_id"],
                    source_version,
                    source_fingerprint,
                    item["map_revision"],
                    item["policy_id"],
                    item["policy_version"],
                    item["map_revision"],
                ),
            )
            if result.rowcount != 1:
                raise CoordinateNormalizationError(
                    f"plan {item['plan_id']} CAS 失败，整批回滚。"
                )
        _restore_triggers(connection, triggers)
        protected_after = protected_snapshot(connection)
        if protected_after != protected_before:
            raise CoordinateNormalizationError("保护表摘要发生变化，整批回滚。")
        checks = _check_database(connection)
        if not checks["ok"]:
            raise CoordinateNormalizationError("修正后完整性/外键检查失败，整批回滚。")
        expected_target_state = direction == "apply"
        if direction == "rollback":
            # Rollback deliberately advances plan.version while restoring the
            # original geometry/fingerprint, so verify slot state separately.
            for row in plan["rows"]:
                if not row["change_required"]:
                    continue
                actual = connection.execute(
                    "SELECT x_mm,y_mm FROM warehouse_ground_layout_slots WHERE id=?",
                    (row["ground_slot_id"],),
                ).fetchone()
                if actual is None or abs(Decimal(str(actual[0])) - Decimal(row["current_x_mm"])) > Decimal("0.0005") or abs(Decimal(str(actual[1])) - Decimal(row["current_y_mm"])) > Decimal("0.0005"):
                    raise CoordinateNormalizationError("回退后坐标验证失败。")
        elif not _state_matches(connection, plan, expected_target_state):
            raise CoordinateNormalizationError("修正后目标状态验证失败。")
        if rollback_after_validation:
            connection.rollback()
            if protected_snapshot(connection) != protected_before or not _state_matches(
                connection, plan, direction == "rollback"
            ):
                # For an apply rehearsal the rolled-back state is source(False);
                # for rollback rehearsal it is target(True).
                expected_after_rollback = direction == "rollback"
                if not _state_matches(connection, plan, expected_after_rollback):
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
    if published_map is None:
        raise CoordinateNormalizationError("受控操作必须提供当前 --published-map。")
    published_map = published_map.resolve(strict=True)
    if file_sha256(published_map) != plan["published_map"]["sha256"]:
        raise CoordinateNormalizationError("当前正式地图 SHA-256 与审计计划不一致。")
    map_document, _ = _load_map(published_map)
    if _floor_revision_map(map_document) != plan["published_map"]["floor_revisions"]:
        raise CoordinateNormalizationError("当前正式地图楼层 revision 与审计计划不一致。")
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
            plan = build_audit(args.database, args.published_map)
            paths = write_audit_outputs(plan, args.output_dir)
            print(
                json.dumps(
                    {
                        "status": "audited",
                        "summary": plan["summary"],
                        "apply_blockers": plan["apply_blockers"],
                        "plan_sha256": plan["plan_sha256"],
                        "outputs": {key: str(value) for key, value in paths.items()},
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
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
