"""P1-102 anonymous finished-inventory coverage audit.

The caller must explicitly provide both the SQLite database and the current
runtime twin JSON.  SQLite is opened through URI ``mode=ro`` and is additionally
protected by ``PRAGMA query_only=ON``.  The report contains only warehouse
location/area identifiers and aggregate quantities; it never selects customer
or product facts.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime
import hashlib
import json
from math import isfinite
from pathlib import Path
import sqlite3
from typing import Any, Iterable, Mapping
from urllib.parse import quote


AUDIT_SQL = """
SELECT
    lot.id AS lot_id,
    lot.quantity_available,
    lot.quantity_reserved,
    lot.quantity_damaged,
    location.id AS location_id,
    location.location_code,
    location.is_active AS location_is_active,
    location.warehouse_floor,
    location.area_code AS location_area_code,
    location.storage_type,
    location.source_version,
    location.placement_status,
    floor.id AS floor_id,
    floor.construction_status AS floor_construction_status,
    area.id AS area_id,
    area.area_code AS formal_area_code,
    area.construction_status AS area_construction_status,
    policy.id AS policy_id,
    policy.map_feature_id,
    policy.status AS policy_status,
    policy.published_map_revision AS policy_published_map_revision,
    layout.location_id AS geometry_location_id,
    ground_plan.status AS ground_plan_status,
    ground_plan.published_map_revision AS ground_plan_published_map_revision,
    ground_plan.area_id AS ground_plan_area_id,
    ground_slot.location_id AS ground_slot_location_id
FROM inventory_lots AS lot
LEFT JOIN warehouse_locations AS location
  ON location.id = lot.warehouse_location_id
LEFT JOIN warehouse_floors AS floor
  ON floor.floor_number = location.warehouse_floor
LEFT JOIN warehouse_areas AS area
  ON area.floor_id = floor.id
 AND upper(trim(area.area_code)) = upper(trim(location.area_code))
LEFT JOIN warehouse_area_storage_policies AS policy
  ON policy.area_id = area.id
LEFT JOIN floor3_location_layouts AS layout
  ON layout.location_id = location.id
LEFT JOIN warehouse_ground_layout_slots AS ground_slot
  ON ground_slot.id = (
      SELECT candidate_slot.id
      FROM warehouse_ground_layout_slots AS candidate_slot
      JOIN warehouse_ground_layout_plans AS candidate_plan
        ON candidate_plan.id = candidate_slot.plan_id
      WHERE candidate_slot.location_id = location.id
        AND candidate_plan.status = 'published'
        AND candidate_plan.published_map_revision = CASE location.warehouse_floor
              WHEN 1 THEN :runtime_revision_1f
              WHEN 3 THEN :runtime_revision_3f
              ELSE NULL
            END
      ORDER BY candidate_plan.id DESC, candidate_slot.id DESC
      LIMIT 1
  )
LEFT JOIN warehouse_ground_layout_plans AS ground_plan
  ON ground_plan.id = ground_slot.plan_id
WHERE lot.inventory_type = 'finished'
  AND lot.status IN ('active', 'frozen')
  AND (
        lot.quantity_available
      + lot.quantity_reserved
      + lot.quantity_damaged
  ) > 0
ORDER BY lot.id
"""


class CoverageAuditError(RuntimeError):
    """Raised when the read-only boundary or runtime map is invalid."""


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _valid_zone_geometry(feature: Mapping[str, Any]) -> bool:
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
    if len(normalized) != len(points):
        return False
    if any(not isfinite(x) or not isfinite(y) for x, y in normalized):
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


def _inside_measured_bounds(
    feature: Mapping[str, Any], bounds: Mapping[str, Any]
) -> bool:
    points = feature.get("points") or []
    if not isinstance(points, list) or len(points) < 3:
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
    except (KeyError, TypeError, ValueError, IndexError, OverflowError):
        return False


def _runtime_map_identities(path: Path) -> tuple[str, dict[str, dict[str, Any]]]:
    resolved = path.resolve(strict=True)
    payload_bytes = resolved.read_bytes()
    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CoverageAuditError("runtime twin JSON cannot be decoded") from error
    if payload.get("schema_version") != 1:
        raise CoverageAuditError("runtime twin JSON schema_version must be 1")
    floors = payload.get("floors")
    if not isinstance(floors, dict):
        raise CoverageAuditError("runtime twin JSON floors must be an object")

    identities: dict[str, dict[str, Any]] = {}
    for floor_code in ("1F", "3F"):
        floor = floors.get(floor_code)
        if floor is None:
            continue
        if not isinstance(floor, dict) or floor.get("floor_code") != floor_code:
            raise CoverageAuditError(f"runtime twin floor identity mismatch: {floor_code}")
        bounds = floor.get("bounds_mm") or {}
        zones: list[Mapping[str, Any]] = []
        for feature in floor.get("features") or []:
            if not isinstance(feature, dict) or feature.get("feature_kind") != "zone":
                continue
            if floor_code == "1F" and not _inside_measured_bounds(feature, bounds):
                continue
            if _valid_zone_geometry(feature):
                zones.append(feature)

        feature_ids = [
            str(feature.get("id") or "").strip()
            for feature in zones
            if str(feature.get("id") or "").strip()
        ]
        if len(feature_ids) != len(set(feature_ids)):
            raise CoverageAuditError(
                f"runtime twin floor {floor_code} has duplicate zone ids"
            )
        zones_by_id = {
            str(feature.get("id") or "").strip(): str(
                feature.get("erp_area_code") or ""
            )
            .strip()
            .upper()
            for feature in zones
            if str(feature.get("id") or "").strip()
        }
        zone_ids_by_area: dict[str, list[str]] = defaultdict(list)
        for feature_id, area_code in zones_by_id.items():
            if area_code:
                zone_ids_by_area[area_code].append(feature_id)
        identities[floor_code] = {
            "revision": str(floor.get("revision") or "").strip(),
            "zones_by_id": zones_by_id,
            "zone_ids_by_area": {
                area_code: tuple(sorted(ids))
                for area_code, ids in zone_ids_by_area.items()
            },
            "zone_count": len(zones_by_id),
        }
    return _sha256_bytes(payload_bytes), identities


def _open_readonly(database: Path) -> sqlite3.Connection:
    resolved = database.resolve(strict=True)
    uri_path = quote(resolved.as_posix(), safe="/:")
    connection = sqlite3.connect(f"file:{uri_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    if int(connection.execute("PRAGMA query_only").fetchone()[0]) != 1:
        connection.close()
        raise CoverageAuditError("SQLite query_only could not be enabled")
    if int(connection.total_changes) != 0:
        connection.close()
        raise CoverageAuditError("SQLite total_changes was non-zero at audit start")
    return connection


def _classification(
    row: Mapping[str, Any], identities: Mapping[str, Mapping[str, Any]]
) -> tuple[str, str]:
    if row["location_id"] is None:
        return "unlocated", "location_missing"
    if not bool(row["location_is_active"]):
        return "unlocated", "location_disabled"
    if str(row["placement_status"] or "") != "placed":
        return "unlocated", "location_unplaced"
    floor_number = int(row["warehouse_floor"] or 0)
    area_code = str(row["location_area_code"] or "").strip().upper()
    if floor_number <= 0 or not area_code:
        return "unlocated", "location_floor_area_missing"
    if row["floor_id"] is None:
        return "unlocated", "floor_missing"
    if str(row["floor_construction_status"] or "") != "enabled":
        return "unlocated", "floor_not_enabled"
    if row["area_id"] is None:
        return "unlocated", "area_missing"
    if str(row["area_construction_status"] or "") != "enabled":
        return "unlocated", "area_not_enabled"

    floor_code = f"{floor_number}F"
    identity = identities.get(floor_code)
    if identity is None:
        return "unlocated", "runtime_floor_missing"
    current_revision = str(identity.get("revision") or "").strip()
    if not current_revision:
        return "unlocated", "runtime_revision_missing"

    source_version = str(row["source_version"] or "").strip().upper()
    legacy_v11 = bool(
        row["policy_id"] is None and source_version == "V11" and floor_number == 3
    )
    if legacy_v11:
        feature_ids = tuple(
            (identity.get("zone_ids_by_area") or {}).get(area_code, ())
        )
        if len(feature_ids) != 1:
            return "unlocated", "v11_area_not_unique"
    else:
        if source_version != "TWIN_V1":
            return "unlocated", "source_not_twin_v1"
        if row["policy_id"] is None or str(row["policy_status"] or "") != "published":
            return "unlocated", "policy_not_published"
        policy_revision = str(row["policy_published_map_revision"] or "").strip()
        if not policy_revision:
            return "unlocated", "policy_revision_missing"
        if policy_revision != current_revision:
            return "unlocated", "policy_revision_stale"
        feature_id = str(row["map_feature_id"] or "").strip()
        if not feature_id:
            return "unlocated", "policy_feature_missing"
        formal_area_code = str(row["formal_area_code"] or "").strip().upper()
        if (identity.get("zones_by_id") or {}).get(feature_id) != formal_area_code:
            return "unlocated", "policy_feature_area_mismatch"
        if str(row["storage_type"] or "").strip().lower() in {
            "ground",
            "temporary_aisle",
        }:
            if (
                str(row["ground_plan_status"] or "") != "published"
                or str(row["ground_plan_published_map_revision"] or "").strip()
                != current_revision
                or int(row["ground_plan_area_id"] or 0) != int(row["area_id"] or 0)
                or int(row["ground_slot_location_id"] or 0)
                != int(row["location_id"] or 0)
            ):
                return "unlocated", "ground_layout_not_current"
    if row["geometry_location_id"] is None:
        return "unlocated", "geometry_missing"
    return "mapped", "mapped"


def _quantity_summary(rows: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    materialized = list(rows)
    return {
        "lot_count": len(materialized),
        "available_reserved_quantity": sum(
            int(row["quantity_available"] or 0)
            + int(row["quantity_reserved"] or 0)
            for row in materialized
        ),
        "physical_quantity_including_damaged": sum(
            int(row["quantity_available"] or 0)
            + int(row["quantity_reserved"] or 0)
            + int(row["quantity_damaged"] or 0)
            for row in materialized
        ),
    }


def collect(database: Path, runtime_map: Path) -> dict[str, Any]:
    database = database.resolve(strict=True)
    runtime_map = runtime_map.resolve(strict=True)
    query_started_at = datetime.now().astimezone().isoformat(timespec="seconds")
    database_stat_before = database.stat()
    database_sha_before = _sha256_file(database)
    map_sha256, identities = _runtime_map_identities(runtime_map)
    script_path = Path(__file__).resolve(strict=True)

    connection = _open_readonly(database)
    total_changes_before = int(connection.total_changes)
    try:
        connection.execute("BEGIN")
        alembic_heads = [
            str(row[0])
            for row in connection.execute(
                "SELECT version_num FROM alembic_version ORDER BY version_num"
            )
        ]
        runtime_revisions = {
            "runtime_revision_1f": (
                str((identities.get("1F") or {}).get("revision") or "").strip()
                or None
            ),
            "runtime_revision_3f": (
                str((identities.get("3F") or {}).get("revision") or "").strip()
                or None
            ),
        }
        source_rows = [
            dict(row)
            for row in connection.execute(AUDIT_SQL, runtime_revisions)
        ]
        connection.rollback()
        query_only = int(connection.execute("PRAGMA query_only").fetchone()[0])
        total_changes_after = int(connection.total_changes)
    finally:
        connection.close()
    if query_only != 1:
        raise CoverageAuditError("SQLite query_only was disabled during audit")
    if total_changes_before != 0 or total_changes_after != 0:
        raise CoverageAuditError("read-only audit changed SQLite total_changes")
    source_lot_ids = [int(row["lot_id"]) for row in source_rows]
    if len(source_lot_ids) != len(set(source_lot_ids)):
        raise CoverageAuditError("audit joins returned a finished lot more than once")

    classified_rows: list[dict[str, Any]] = []
    for row in source_rows:
        classification, reason_code = _classification(row, identities)
        classified_rows.append(
            {
                **row,
                "classification": classification,
                "reason_code": reason_code,
            }
        )
    mapped = [row for row in classified_rows if row["classification"] == "mapped"]
    unlocated = [
        row for row in classified_rows if row["classification"] == "unlocated"
    ]
    total_summary = _quantity_summary(classified_rows)
    mapped_summary = _quantity_summary(mapped)
    unlocated_summary = _quantity_summary(unlocated)

    aggregates: dict[tuple[str, str, str, str, str], list[dict[str, Any]]] = (
        defaultdict(list)
    )
    for row in classified_rows:
        floor_code = (
            f"{int(row['warehouse_floor'])}F"
            if row["warehouse_floor"] is not None
            else "UNLOCATED"
        )
        area_code = str(row["location_area_code"] or "").strip().upper()
        location_code = str(row["location_code"] or "").strip()
        key = (
            row["classification"],
            row["reason_code"],
            floor_code,
            area_code or "UNLOCATED",
            location_code or "UNBOUND",
        )
        aggregates[key].append(row)
    location_aggregates = []
    for key in sorted(aggregates):
        classification, reason_code, floor_code, area_code, location_code = key
        location_aggregates.append(
            {
                "classification": classification,
                "reason_code": reason_code,
                "floor_code": floor_code,
                "area_code": area_code,
                "location_code": location_code,
                **_quantity_summary(aggregates[key]),
            }
        )

    database_stat_after = database.stat()
    database_sha_after = _sha256_file(database)
    query_finished_at = datetime.now().astimezone().isoformat(timespec="seconds")
    return {
        "task_id": "P1-102",
        "query_started_at": query_started_at,
        "query_finished_at": query_finished_at,
        "audit_contract": {
            "scope": "positive finished inventory lots only",
            "available_reserved_quantity": "quantity_available + quantity_reserved",
            "physical_quantity_including_damaged": (
                "quantity_available + quantity_reserved + quantity_damaged"
            ),
            "anonymous": True,
            "business_identity_fields_selected": False,
            "each_lot_counted_once": True,
        },
        "database": {
            "path": str(database),
            "sha256_before": database_sha_before,
            "sha256_after": database_sha_after,
            "size_before": database_stat_before.st_size,
            "size_after": database_stat_after.st_size,
            "mtime_ns_before": database_stat_before.st_mtime_ns,
            "mtime_ns_after": database_stat_after.st_mtime_ns,
            "connection_mode": "SQLite URI mode=ro",
            "query_only": query_only,
            "total_changes_before": total_changes_before,
            "total_changes_after": total_changes_after,
            "file_bytes_unchanged_during_audit": (
                database_sha_before == database_sha_after
                and database_stat_before.st_size == database_stat_after.st_size
                and database_stat_before.st_mtime_ns == database_stat_after.st_mtime_ns
            ),
        },
        "runtime_map": {
            "path": str(runtime_map),
            "sha256": map_sha256,
            "revisions": {
                floor_code: identity["revision"]
                for floor_code, identity in sorted(identities.items())
            },
            "zone_counts": {
                floor_code: identity["zone_count"]
                for floor_code, identity in sorted(identities.items())
            },
        },
        "script": {
            "path": str(script_path),
            "sha256": _sha256_file(script_path),
            "sql_sha256": _sha256_bytes(AUDIT_SQL.strip().encode("utf-8")),
        },
        "alembic_heads": alembic_heads,
        "summary": {
            "total": total_summary,
            "mapped": mapped_summary,
            "unlocated": unlocated_summary,
            "all_located": not unlocated,
            "conservation": {
                "lot_count": (
                    total_summary["lot_count"]
                    == mapped_summary["lot_count"] + unlocated_summary["lot_count"]
                ),
                "available_reserved_quantity": (
                    total_summary["available_reserved_quantity"]
                    == mapped_summary["available_reserved_quantity"]
                    + unlocated_summary["available_reserved_quantity"]
                ),
                "physical_quantity_including_damaged": (
                    total_summary["physical_quantity_including_damaged"]
                    == mapped_summary["physical_quantity_including_damaged"]
                    + unlocated_summary["physical_quantity_including_damaged"]
                ),
            },
        },
        "location_aggregates": location_aggregates,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="P1-102 anonymous read-only finished map coverage audit"
    )
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--runtime-map", required=True, type=Path)
    arguments = parser.parse_args(argv)
    print(
        json.dumps(
            collect(arguments.database, arguments.runtime_map),
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
