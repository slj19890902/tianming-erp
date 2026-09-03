from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shutil
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.services.warehouse_floor1_candidate_planner import (  # noqa: E402
    Floor1CandidatePlanningError,
    _percent_round_trip_epsilon,
    validate_capacity_layout_slots_for_zone,
)
from app.services.warehouse_ground_slots import (  # noqa: E402
    ground_preview_fingerprint,
    number_ground_physical_slots,
)
from app.services.warehouse_pallet_standard import standard_pallet_contract  # noqa: E402


TASK_CODE = "P1-152"
SCHEMA_VERSION = 1
SCOPE_FLOOR_CODE = "4F"
SCOPE_AREA_CODES = ("EDIT-001", "EDIT-002")
EXPECTED_PLAN_COUNT = 2
EXPECTED_SLOT_COUNT = 24
GEOMETRY_TOLERANCE_MM = Decimal("0.001")
APPLY_TOKEN = "APPLY-P1-152-PLAN-REVISION"
ROLLBACK_TOKEN = "ROLLBACK-P1-152-PLAN-REVISION"
DEFAULT_FORMAL_DATABASE = (PROJECT_ROOT / "data" / "carton_erp.sqlite3").resolve(
    strict=False
)
KNOWN_FACTORY_FORMAL_DATABASE = Path(
    r"D:\纸箱厂erp软件搭建\data\carton_erp.sqlite3"
).resolve(strict=False)
MUTABLE_TRIGGER = "trg_ground_plans_published_immutable"
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
    "warehouse_ground_layout_slots",
)


class Floor4RevisionReconciliationError(RuntimeError):
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


def _q(value: Any) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.001"))


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
        "users",
        "operation_logs",
        "warehouse_floors",
        "warehouse_areas",
        "warehouse_area_storage_policies",
        "warehouse_locations",
        "floor3_location_layouts",
        "warehouse_ground_layout_plans",
        "warehouse_ground_layout_slots",
        "inventory_lots",
        "inventory_pallets",
        "inventory_pallet_items",
        "warehouse_ground_occupancies",
        "warehouse_ground_occupancy_slots",
    }
    missing = sorted(table for table in required if not _table_exists(connection, table))
    if missing:
        raise Floor4RevisionReconciliationError(
            "数据库缺少 P1-152 必需表：" + ", ".join(missing)
        )


def _assert_no_wal(database: Path) -> None:
    sidecars = [Path(f"{database}-wal"), Path(f"{database}-shm")]
    present = [str(path) for path in sidecars if path.exists()]
    if present:
        raise Floor4RevisionReconciliationError(
            "检测到 SQLite WAL/SHM 文件，数据库可能仍在使用，拒绝继续："
            + ", ".join(present)
        )


def _assert_isolated_database(database: Path) -> None:
    resolved = database.resolve(strict=True)
    if resolved in {DEFAULT_FORMAL_DATABASE, KNOWN_FACTORY_FORMAL_DATABASE}:
        raise Floor4RevisionReconciliationError(
            "只读计划必须从停服后备份的隔离副本生成，拒绝直接使用正式库。"
        )
    marker_text = f"{resolved.parent} {resolved.name}".lower()
    if not any(
        marker in marker_text
        for marker in ("uat", "copy", "replica", "snapshot", "rehearsal", "p1-152")
    ):
        raise Floor4RevisionReconciliationError(
            "路径未体现 UAT/copy/replica/snapshot/rehearsal/P1-152 隔离标记。"
        )
    _assert_no_wal(resolved)


def _assert_execution_target(
    database: Path,
    *,
    target_environment: str,
    confirm_isolated_copy: bool,
    confirm_formal_database: bool,
) -> None:
    resolved = database.resolve(strict=True)
    if target_environment == "isolated":
        if not confirm_isolated_copy or confirm_formal_database:
            raise Floor4RevisionReconciliationError(
                "隔离执行必须且只能提供 --confirm-isolated-copy。"
            )
        _assert_isolated_database(resolved)
        return
    if target_environment != "formal":
        raise Floor4RevisionReconciliationError("target-environment 只能是 isolated 或 formal。")
    if not confirm_formal_database or confirm_isolated_copy:
        raise Floor4RevisionReconciliationError(
            "正式执行必须且只能提供 --confirm-formal-database。"
        )
    if resolved != DEFAULT_FORMAL_DATABASE or resolved != KNOWN_FACTORY_FORMAL_DATABASE:
        raise Floor4RevisionReconciliationError(
            "正式执行仅允许工厂正式检出目录内的 data/carton_erp.sqlite3。"
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


def _logical_table_hash(connection: sqlite3.Connection, table: str) -> dict[str, Any]:
    columns = [row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')]
    order = " ORDER BY " + ",".join(f'"{column}"' for column in columns)
    digest = hashlib.sha256(canonical_json({"columns": columns}).encode("utf-8"))
    row_count = 0
    for row in connection.execute(f'SELECT * FROM "{table}"{order}'):
        digest.update(b"\n")
        digest.update(canonical_json(list(row)).encode("utf-8"))
        row_count += 1
    return {"row_count": row_count, "sha256": digest.hexdigest()}


def protected_snapshot(connection: sqlite3.Connection) -> dict[str, Any]:
    return {
        table: _logical_table_hash(connection, table) for table in PROTECTED_TABLES
    }


def _trigger_snapshot(connection: sqlite3.Connection) -> dict[str, str]:
    return {
        str(row[0]): str(row[1])
        for row in connection.execute(
            """
            SELECT name,sql FROM sqlite_master
            WHERE type='trigger'
              AND tbl_name IN ('warehouse_ground_layout_plans',
                               'warehouse_ground_layout_slots')
            ORDER BY name
            """
        )
    }


def _mutable_trigger_sql(connection: sqlite3.Connection) -> str:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
        (MUTABLE_TRIGGER,),
    ).fetchone()
    if row is None or not row[0]:
        raise Floor4RevisionReconciliationError(
            f"缺少保护触发器 {MUTABLE_TRIGGER}，拒绝受控修正。"
        )
    sql = str(row[0])
    if (
        "warehouse_ground_layout_plans" not in sql
        or "published ground layout plan is immutable" not in sql
    ):
        raise Floor4RevisionReconciliationError(
            f"保护触发器 {MUTABLE_TRIGGER} 定义不符合预期。"
        )
    return sql


def _load_map(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    resolved = path.resolve(strict=True)
    normalized = str(resolved).lower()
    if "layout_drafts" in normalized or ".draft." in resolved.name.lower():
        raise Floor4RevisionReconciliationError("拒绝把未发布地图草稿作为 P1-152 权威。")
    try:
        document = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise Floor4RevisionReconciliationError(f"无法读取正式地图：{resolved}") from error
    if document.get("schema_version") != 1 or not isinstance(document.get("floors"), dict):
        raise Floor4RevisionReconciliationError("正式地图 schema_version/floors 不受支持。")
    matches = [
        floor or {}
        for code, floor in document["floors"].items()
        if str(code).strip().upper() == SCOPE_FLOOR_CODE
    ]
    if len(matches) != 1:
        raise Floor4RevisionReconciliationError("正式地图必须且只能包含一个 4F 楼层。")
    floor = {"floor_code": SCOPE_FLOOR_CODE, **matches[0]}
    if not str(floor.get("revision") or "").strip():
        raise Floor4RevisionReconciliationError("正式 4F 地图缺少 revision。")
    return document, floor


def _optional_positive_id(value: Any) -> tuple[str, int | None]:
    if value is None or (isinstance(value, str) and not value.strip()):
        return "absent", None
    if isinstance(value, bool):
        return "invalid", None
    if isinstance(value, int):
        return ("valid", value) if value > 0 else ("invalid", None)
    if isinstance(value, str) and value.strip().isdigit() and not value.strip().startswith("0"):
        return "valid", int(value.strip())
    return "invalid", None


def _plan_sources(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    placeholders = ",".join("?" for _ in SCOPE_AREA_CODES)
    return list(
        connection.execute(
            f"""
            SELECT
              p.id AS plan_id,p.area_id,p.status AS plan_status,
              p.target_slot_count,p.numbering_origin,p.row_direction,p.slot_direction,
              p.row_start_no,p.slot_start_no,p.draft_map_revision,
              p.published_map_revision,p.preview_fingerprint,p.version AS plan_version,
              p.publish_idempotency_key,p.publish_request_hash,p.updated_by AS plan_updated_by,
              p.updated_at AS plan_updated_at,p.published_by,p.published_at,
              a.area_code,a.area_name,a.address_version AS area_version,
              a.planned_location_count,a.planned_pallet_capacity,
              f.id AS floor_id,f.floor_code,f.floor_name,f.floor_number,
              policy.id AS policy_id,policy.area_id AS policy_area_id,
              policy.map_feature_id,policy.allowed_inventory_types_json,
              policy.storage_layout,policy.status AS policy_status,
              policy.draft_map_revision AS policy_draft_map_revision,
              policy.published_map_revision AS policy_map_revision,
              policy.version AS policy_version,policy.updated_by AS policy_updated_by,
              policy.updated_at AS policy_updated_at
            FROM warehouse_ground_layout_plans AS p
            JOIN warehouse_areas AS a ON a.id=p.area_id
            JOIN warehouse_floors AS f ON f.id=a.floor_id
            JOIN warehouse_area_storage_policies AS policy ON policy.area_id=a.id
            WHERE upper(trim(f.floor_code))=?
              AND upper(trim(a.area_code)) IN ({placeholders})
            ORDER BY upper(trim(a.area_code)),p.id
            """,
            (SCOPE_FLOOR_CODE, *SCOPE_AREA_CODES),
        )
    )


def _slot_sources(connection: sqlite3.Connection, plan_id: int) -> list[sqlite3.Row]:
    return list(
        connection.execute(
            """
            SELECT
              s.id AS ground_slot_id,s.plan_id,s.location_id,s.route_sequence,
              s.row_no,s.slot_no,s.x_mm,s.y_mm,s.width_mm,s.depth_mm,
              l.location_code,l.location_name,l.warehouse_floor,l.area_code AS location_area_code,
              l.address_area_id,l.address_version AS location_address_version,
              l.placement_status,l.storage_type,l.is_active,l.sort_order,
              fl.id AS layout_id,fl.left_pct,fl.top_pct,fl.width_pct,fl.height_pct,
              fl.z_index,fl.version AS layout_version,fl.source_type,fl.layout_kind,
              fl.updated_at AS layout_updated_at
            FROM warehouse_ground_layout_slots AS s
            JOIN warehouse_locations AS l ON l.id=s.location_id
            JOIN floor3_location_layouts AS fl ON fl.location_id=l.id
            WHERE s.plan_id=?
            ORDER BY s.route_sequence,s.id
            """,
            (plan_id,),
        )
    )


def _area_location_ids(
    connection: sqlite3.Connection, *, floor_number: int, area_code: str
) -> list[int]:
    return [
        int(row[0])
        for row in connection.execute(
            """
            SELECT id FROM warehouse_locations
            WHERE warehouse_floor=? AND upper(trim(area_code))=?
              AND is_active=1 AND placement_status='placed' AND storage_type='ground'
            ORDER BY id
            """,
            (floor_number, area_code.upper()),
        )
    ]


def _inventory_evidence(
    connection: sqlite3.Connection, location_ids: list[int]
) -> dict[int, dict[str, int]]:
    evidence = {
        location_id: {
            "lot_count": 0,
            "lot_quantity": 0,
            "current_pallet_count": 0,
            "active_occupancy_count": 0,
        }
        for location_id in location_ids
    }
    if not location_ids:
        return evidence
    placeholders = ",".join("?" for _ in location_ids)
    for row in connection.execute(
        f"""
        SELECT warehouse_location_id,count(*)
        FROM inventory_lots
        WHERE warehouse_location_id IN ({placeholders})
        GROUP BY warehouse_location_id
        """,
        location_ids,
    ):
        evidence[int(row[0])]["lot_count"] = int(row[1] or 0)
    for row in connection.execute(
        f"""
        SELECT warehouse_location_id,
               sum(quantity_available+quantity_reserved+quantity_damaged)
        FROM inventory_lots
        WHERE warehouse_location_id IN ({placeholders})
          AND status IN ('active','frozen')
        GROUP BY warehouse_location_id
        """,
        location_ids,
    ):
        evidence[int(row[0])]["lot_quantity"] = int(row[1] or 0)
    for row in connection.execute(
        f"""
        SELECT location_id,count(*) FROM inventory_pallets
        WHERE location_id IN ({placeholders}) AND is_current=1
        GROUP BY location_id
        """,
        location_ids,
    ):
        evidence[int(row[0])]["current_pallet_count"] = int(row[1] or 0)
    for row in connection.execute(
        f"""
        SELECT os.location_id,count(DISTINCT os.occupancy_id)
        FROM warehouse_ground_occupancy_slots AS os
        JOIN warehouse_ground_occupancies AS o ON o.id=os.occupancy_id
        WHERE os.location_id IN ({placeholders})
          AND os.status='active' AND o.status='active'
        GROUP BY os.location_id
        """,
        location_ids,
    ):
        evidence[int(row[0])]["active_occupancy_count"] = int(row[1] or 0)
    return evidence


def _feature_for_plan(
    floor_layout: dict[str, Any], source: sqlite3.Row
) -> tuple[dict[str, Any] | None, str | None, str | None]:
    feature_id = str(source["map_feature_id"] or "").strip()
    matches = [
        row
        for row in floor_layout.get("features") or []
        if row.get("feature_kind") == "zone"
        and str(row.get("id") or "").strip() == feature_id
    ]
    if len(matches) != 1:
        return None, "map_feature_missing_or_duplicated", None
    feature = matches[0]
    if str(feature.get("erp_area_code") or "").strip().upper() != str(
        source["area_code"]
    ).strip().upper():
        return feature, "map_area_code_identity_mismatch", None
    floor_state, formal_floor_id = _optional_positive_id(feature.get("formal_floor_id"))
    area_state, formal_area_id = _optional_positive_id(feature.get("formal_area_id"))
    if "invalid" in {floor_state, area_state}:
        return feature, "formal_identity_invalid", None
    if floor_state != area_state:
        return feature, "formal_identity_incomplete", None
    if floor_state == "valid":
        if formal_floor_id != int(source["floor_id"]):
            return feature, "formal_floor_id_mismatch", None
        if formal_area_id != int(source["area_id"]):
            return feature, "formal_area_id_mismatch", None
        return feature, None, "stable_ids"
    if str(source["floor_code"] or "").strip().upper() != SCOPE_FLOOR_CODE:
        return feature, "floor_code_identity_mismatch", None
    return feature, None, "policy_feature_plus_exact_codes"


def _feature_business_contract(feature: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(feature.get("id") or "").strip(),
        "feature_kind": feature.get("feature_kind"),
        "erp_area_code": str(feature.get("erp_area_code") or "").strip().upper(),
        "points": feature.get("points") or [],
        "storage_mode": feature.get("storage_mode"),
        "no_stacking": bool(feature.get("no_stacking")),
        "elevation_mm": feature.get("elevation_mm"),
        "storage_height_mm": feature.get("storage_height_mm"),
        "allowed_inventory_types": sorted(
            str(value).strip()
            for value in feature.get("allowed_inventory_types") or []
            if str(value).strip()
        ),
        "storage_layout": str(feature.get("storage_layout") or "").strip(),
    }


def _creation_evidence(
    connection: sqlite3.Connection,
    source: sqlite3.Row,
    feature: dict[str, Any],
    location_ids: list[int],
) -> tuple[dict[str, Any] | None, list[str]]:
    issues: list[str] = []
    old_revision = str(source["published_map_revision"] or "").strip()
    feature_id = str(source["map_feature_id"] or "").strip()
    area_code = str(source["area_code"] or "").strip().upper()

    def parsed_logs(action: str) -> list[tuple[int, dict[str, Any]]]:
        result: list[tuple[int, dict[str, Any]]] = []
        for row in connection.execute(
            "SELECT id,details FROM operation_logs WHERE action=? ORDER BY id", (action,)
        ):
            try:
                details = json.loads(row["details"] or "{}")
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(details, dict):
                result.append((int(row["id"]), details))
        return result

    confirmations = [
        (log_id, details)
        for log_id, details in parsed_logs("TWIN_ZONE_ONE_STEP_CONFIRM")
        if str(details.get("floor_code") or "").strip().upper() == SCOPE_FLOOR_CODE
        and int(details.get("area_id") or 0) == int(source["area_id"])
        and str(details.get("area_code") or "").strip().upper() == area_code
        and int(details.get("ground_plan_id") or 0) == int(source["plan_id"])
        and str(details.get("published_revision") or "").strip() == old_revision
    ]
    if len(confirmations) != 1:
        issues.append("one_step_creation_log_missing_or_ambiguous")
    elif sorted(int(value) for value in confirmations[0][1].get("reflowed_location_ids") or []) != sorted(
        location_ids
    ):
        issues.append("one_step_creation_location_set_mismatch")

    policy_logs = [
        (log_id, details)
        for log_id, details in parsed_logs("TWIN_ZONE_POLICY_UPDATE")
        if str(details.get("floor_code") or "").strip().upper() == SCOPE_FLOOR_CODE
        and str((details.get("zone") or {}).get("id") or "").strip() == feature_id
        and str(details.get("formal_area_code") or "").strip().upper() == area_code
    ]
    matching_policy_logs = [
        (log_id, details)
        for log_id, details in policy_logs
        if _feature_business_contract(details.get("zone") or {})
        == _feature_business_contract(feature)
    ]
    if len(matching_policy_logs) != 1:
        issues.append("policy_feature_snapshot_missing_or_not_equivalent")

    publish_logs = [
        (log_id, details)
        for log_id, details in parsed_logs("TWIN_LAYOUT_PUBLISH")
        if str(details.get("floor_code") or "").strip().upper() == SCOPE_FLOOR_CODE
        and str(details.get("published_revision") or "").strip() == old_revision
        and any(
            str(row.get("area_code") or "").strip().upper() == area_code
            for row in details.get("formal_areas") or []
            if isinstance(row, dict)
        )
    ]
    if len(publish_logs) != 1:
        issues.append("source_revision_publish_log_missing_or_ambiguous")

    if issues:
        return None, issues
    confirmation_id, confirmation = confirmations[0]
    policy_log_id, policy_log = matching_policy_logs[0]
    publish_log_id, publish_log = publish_logs[0]
    evidence = {
        "policy_log_id": policy_log_id,
        "policy_feature_contract_sha256": canonical_hash(
            _feature_business_contract(policy_log["zone"])
        ),
        "publish_log_id": publish_log_id,
        "published_revision": publish_log["published_revision"],
        "published_sha256": publish_log.get("published_sha256"),
        "one_step_log_id": confirmation_id,
        "ground_plan_id": confirmation["ground_plan_id"],
        "location_ids_sha256": canonical_hash(sorted(location_ids)),
    }
    return evidence, []


def _configuration(source: sqlite3.Row) -> dict[str, Any]:
    return {
        "target_slot_count": int(source["target_slot_count"]),
        "numbering_origin": source["numbering_origin"],
        "row_direction": source["row_direction"],
        "slot_direction": source["slot_direction"],
        "row_start_no": int(source["row_start_no"]),
        "slot_start_no": int(source["slot_start_no"]),
    }


def _derive_slots(
    floor_layout: dict[str, Any],
    source: sqlite3.Row,
    slots: list[sqlite3.Row],
) -> list[dict[str, Any]]:
    layout_payloads = [
        {
            "location_id": int(row["location_id"]),
            "left_pct": float(row["left_pct"]),
            "top_pct": float(row["top_pct"]),
            "width_pct": float(row["width_pct"]),
            "height_pct": float(row["height_pct"]),
            "z_index": int(row["z_index"]),
            "expected_version": int(row["layout_version"]),
            "layout_kind": row["layout_kind"],
        }
        for row in slots
    ]
    measured = validate_capacity_layout_slots_for_zone(
        floor_layout,
        feature_id=str(source["map_feature_id"]),
        slots=layout_payloads,
    )
    feature = next(
        row
        for row in floor_layout.get("features") or []
        if row.get("feature_kind") == "zone"
        and str(row.get("id") or "") == str(source["map_feature_id"])
    )
    epsilon = _percent_round_trip_epsilon(feature.get("points") or [])
    pallet = standard_pallet_contract()
    width = int(pallet["width_mm"])
    depth = int(pallet["depth_mm"])
    matched: list[dict[str, Any]] = []
    for row, layout, actual in zip(slots, layout_payloads, measured, strict=True):
        actual_width = float(actual["width_mm"])
        actual_depth = float(actual["depth_mm"])
        standard = (
            abs(actual_width - width) <= epsilon
            and abs(actual_depth - depth) <= epsilon
        )
        rotated = (
            abs(actual_width - depth) <= epsilon
            and abs(actual_depth - width) <= epsilon
        )
        if not standard and not rotated:
            raise Floor4RevisionReconciliationError(
                f"location {row['location_id']} 无法按当前地图还原为标准栈板尺寸。"
            )
        matched.append(
            {
                **layout,
                **actual,
                "x_mm": _q(actual["x_mm"]),
                "y_mm": _q(actual["y_mm"]),
                "width_mm": width if standard else depth,
                "depth_mm": depth if standard else width,
                "location_code": row["location_code"],
                "existing_location_id": int(row["location_id"]),
                "existing_layout_version": int(row["layout_version"]),
            }
        )
    return number_ground_physical_slots(matched, **{
        key: value
        for key, value in _configuration(source).items()
        if key != "target_slot_count"
    })


def _slot_evidence(
    source: sqlite3.Row,
    slots: list[sqlite3.Row],
    derived: list[dict[str, Any]],
    inventory: dict[int, dict[str, int]],
) -> tuple[list[dict[str, Any]], list[str]]:
    derived_by_location = {
        int(row["existing_location_id"]): row for row in derived
    }
    evidence_rows: list[dict[str, Any]] = []
    issues: list[str] = []
    for row in slots:
        location_id = int(row["location_id"])
        actual = derived_by_location.get(location_id)
        row_issues: list[str] = []
        if actual is None:
            row_issues.append("derived_location_missing")
            actual = {}
        exact_fields = ("route_sequence", "row_no", "slot_no", "width_mm", "depth_mm")
        for field in exact_fields:
            if actual and int(row[field]) != int(actual[field]):
                row_issues.append(f"{field}_mismatch")
        for field in ("x_mm", "y_mm"):
            if actual and abs(_q(row[field]) - _q(actual[field])) > GEOMETRY_TOLERANCE_MM:
                row_issues.append(f"{field}_mismatch")
        if int(row["warehouse_floor"]) != int(source["floor_number"]):
            row_issues.append("location_floor_mismatch")
        if str(row["location_area_code"] or "").strip().upper() != str(
            source["area_code"]
        ).strip().upper():
            row_issues.append("location_area_mismatch")
        if row["address_area_id"] is not None:
            row_issues.append("address_area_id_must_remain_null")
        if (
            row["placement_status"] != "placed"
            or row["storage_type"] != "ground"
            or int(row["is_active"]) != 1
        ):
            row_issues.append("location_not_active_placed_ground")
        inventory_row = inventory[location_id]
        if any(int(value) != 0 for value in inventory_row.values()):
            row_issues.append("location_not_empty")
        if row_issues:
            issues.extend(
                f"location {location_id}: {issue}" for issue in row_issues
            )
        evidence_rows.append(
            {
                "plan_id": int(source["plan_id"]),
                "area_id": int(source["area_id"]),
                "area_code": source["area_code"],
                "ground_slot_id": int(row["ground_slot_id"]),
                "location_id": location_id,
                "location_code": row["location_code"],
                "layout_id": int(row["layout_id"]),
                "layout_version": int(row["layout_version"]),
                "route_sequence": int(row["route_sequence"]),
                "row_no": int(row["row_no"]),
                "slot_no": int(row["slot_no"]),
                "plan_x_mm": format(_q(row["x_mm"]), "f"),
                "plan_y_mm": format(_q(row["y_mm"]), "f"),
                "derived_x_mm": (
                    format(_q(actual["x_mm"]), "f") if actual else None
                ),
                "derived_y_mm": (
                    format(_q(actual["y_mm"]), "f") if actual else None
                ),
                "width_mm": int(row["width_mm"]),
                "depth_mm": int(row["depth_mm"]),
                "left_pct": float(row["left_pct"]),
                "top_pct": float(row["top_pct"]),
                "width_pct": float(row["width_pct"]),
                "height_pct": float(row["height_pct"]),
                "address_area_id": row["address_area_id"],
                **inventory_row,
                "equivalent": not row_issues,
                "issues": row_issues,
            }
        )
    return evidence_rows, issues


def _static_plan_contract(source: sqlite3.Row) -> dict[str, Any]:
    return {
        key: source[key]
        for key in (
            "plan_id",
            "area_id",
            "plan_status",
            "target_slot_count",
            "numbering_origin",
            "row_direction",
            "slot_direction",
            "row_start_no",
            "slot_start_no",
            "publish_idempotency_key",
            "publish_request_hash",
            "published_by",
            "published_at",
        )
    }


def build_audit(database: Path, map_path: Path) -> dict[str, Any]:
    database = database.resolve(strict=True)
    map_path = map_path.resolve(strict=True)
    _assert_isolated_database(database)
    _document, floor_layout = _load_map(map_path)
    current_revision = str(floor_layout["revision"]).strip()
    hard_blockers: list[str] = []
    plans: list[dict[str, Any]] = []
    all_slot_rows: list[dict[str, Any]] = []
    with readonly_database(database) as connection:
        _required_tables(connection)
        checks = _check_database(connection)
        if not checks["ok"]:
            raise Floor4RevisionReconciliationError("数据库完整性或外键检查未通过。")
        sources = _plan_sources(connection)
        protected = protected_snapshot(connection)
        triggers = _trigger_snapshot(connection)
        _mutable_trigger_sql(connection)
        alembic_heads = [
            str(row[0])
            for row in connection.execute(
                "SELECT version_num FROM alembic_version ORDER BY version_num"
            )
        ]
        if len(alembic_heads) != 1 or not all(
            head.strip() for head in alembic_heads
        ):
            hard_blockers.append(
                "Alembic 必须且只能有一个数据库 head，"
                f"实际 {len(alembic_heads)} 个：{','.join(alembic_heads) or '(empty)'}。"
            )
        if len(sources) != EXPECTED_PLAN_COUNT:
            hard_blockers.append(
                f"4F 指定区域应有 {EXPECTED_PLAN_COUNT} 个计划，实际 {len(sources)} 个。"
            )
        actual_areas = {str(row["area_code"]).strip().upper() for row in sources}
        if actual_areas != set(SCOPE_AREA_CODES):
            hard_blockers.append(
                "4F 指定计划区域集合不一致：" + ",".join(sorted(actual_areas))
            )
        for source in sources:
            plan_issues: list[str] = []
            area_code = str(source["area_code"]).strip().upper()
            slots = _slot_sources(connection, int(source["plan_id"]))
            inventory = _inventory_evidence(
                connection, [int(row["location_id"]) for row in slots]
            )
            feature, identity_issue, identity_basis = _feature_for_plan(
                floor_layout, source
            )
            if source["plan_status"] != "published":
                plan_issues.append("plan_not_published")
            if source["policy_area_id"] != source["area_id"]:
                plan_issues.append("policy_area_identity_mismatch")
            if source["policy_status"] != "published":
                plan_issues.append("policy_not_published")
            if source["storage_layout"] not in {"pallet_ground", "mixed"}:
                plan_issues.append("policy_storage_layout_invalid")
            if str(source["policy_map_revision"] or "").strip() != current_revision:
                plan_issues.append("policy_revision_not_current")
            if identity_issue:
                plan_issues.append(identity_issue)
            if len(slots) != int(source["target_slot_count"]):
                plan_issues.append("slot_count_does_not_match_plan")
            if len({int(row["ground_slot_id"]) for row in slots}) != len(slots):
                plan_issues.append("ground_slot_id_not_unique")
            location_ids = [int(row["location_id"]) for row in slots]
            if len(set(location_ids)) != len(location_ids):
                plan_issues.append("location_id_not_unique")
            if sorted(location_ids) != _area_location_ids(
                connection,
                floor_number=int(source["floor_number"]),
                area_code=area_code,
            ):
                plan_issues.append("plan_location_set_not_authoritative_area_set")
            derived: list[dict[str, Any]] = []
            slot_evidence: list[dict[str, Any]] = []
            if feature is not None:
                try:
                    derived = _derive_slots(floor_layout, source, slots)
                    slot_evidence, slot_issues = _slot_evidence(
                        source, slots, derived, inventory
                    )
                    plan_issues.extend(slot_issues)
                except (
                    Floor1CandidatePlanningError,
                    Floor4RevisionReconciliationError,
                    StopIteration,
                    ValueError,
                ) as error:
                    plan_issues.append(f"geometry_derivation_failed:{error}")
            all_slot_rows.extend(slot_evidence)
            configuration = _configuration(source)
            legacy_policy_versions: list[int] = []
            current_fingerprint: str | None = None
            if derived:
                current_fingerprint = ground_preview_fingerprint(
                    area_id=int(source["area_id"]),
                    policy_version=int(source["policy_version"]),
                    map_revision=current_revision,
                    configuration=configuration,
                    slots=derived,
                )
                for policy_version in range(1, int(source["policy_version"]) + 1):
                    candidate = ground_preview_fingerprint(
                        area_id=int(source["area_id"]),
                        policy_version=policy_version,
                        map_revision=str(source["published_map_revision"] or ""),
                        configuration=configuration,
                        slots=derived,
                    )
                    if candidate == source["preview_fingerprint"]:
                        legacy_policy_versions.append(policy_version)
            old_revisions_match = (
                str(source["draft_map_revision"] or "").strip()
                == str(source["published_map_revision"] or "").strip()
            )
            already_current = (
                old_revisions_match
                and str(source["published_map_revision"] or "").strip()
                == current_revision
                and source["preview_fingerprint"] == current_fingerprint
            )
            creation_evidence: dict[str, Any] | None = None
            if already_current:
                creation_evidence = {
                    "mode": "current_revision_and_fingerprint_reproduced",
                    "published_revision": current_revision,
                    "location_ids_sha256": canonical_hash(sorted(location_ids)),
                }
            elif feature is not None:
                creation_evidence, creation_issues = _creation_evidence(
                    connection, source, feature, location_ids
                )
                plan_issues.extend(creation_issues)
            if not old_revisions_match:
                plan_issues.append("plan_draft_and_published_revision_diverge")
            if already_current:
                disposition = "already_consistent"
            else:
                if str(source["published_map_revision"] or "").strip() == current_revision:
                    plan_issues.append("current_revision_has_unexpected_fingerprint")
                if len(legacy_policy_versions) != 1:
                    plan_issues.append("legacy_preview_fingerprint_not_reproducible")
                disposition = (
                    "reference_refresh_candidate" if not plan_issues else "blocked"
                )
            feature_hash = canonical_hash(feature) if feature is not None else None
            geometry_payload = {
                "floor_id": int(source["floor_id"]),
                "floor_code": SCOPE_FLOOR_CODE,
                "area_id": int(source["area_id"]),
                "area_code": area_code,
                "map_feature_id": source["map_feature_id"],
                "feature_points": (feature or {}).get("points"),
                "configuration": configuration,
                "slots": slot_evidence,
            }
            plan_record = {
                "plan_id": int(source["plan_id"]),
                "area_id": int(source["area_id"]),
                "area_code": area_code,
                "floor_id": int(source["floor_id"]),
                "floor_code": SCOPE_FLOOR_CODE,
                "policy_id": int(source["policy_id"]),
                "policy_version": int(source["policy_version"]),
                "policy_map_feature_id": source["map_feature_id"],
                "policy_map_revision": source["policy_map_revision"],
                "policy_binding_fingerprint": canonical_hash(
                    {
                        "policy_id": source["policy_id"],
                        "area_id": source["policy_area_id"],
                        "map_feature_id": source["map_feature_id"],
                        "allowed_inventory_types_json": source[
                            "allowed_inventory_types_json"
                        ],
                        "storage_layout": source["storage_layout"],
                        "status": source["policy_status"],
                        "version": source["policy_version"],
                    }
                ),
                "identity_basis": identity_basis,
                "map_feature_sha256": feature_hash,
                "map_feature_business_sha256": (
                    canonical_hash(_feature_business_contract(feature))
                    if feature is not None
                    else None
                ),
                "geometry_fingerprint": canonical_hash(geometry_payload),
                "creation_evidence": creation_evidence,
                "slot_count": len(slots),
                "location_ids": sorted(location_ids),
                "ground_slot_ids": sorted(
                    int(row["ground_slot_id"]) for row in slots
                ),
                "configuration": configuration,
                "static_plan_contract": _static_plan_contract(source),
                "source": {
                    "draft_map_revision": source["draft_map_revision"],
                    "published_map_revision": source["published_map_revision"],
                    "preview_fingerprint": source["preview_fingerprint"],
                    "version": int(source["plan_version"]),
                    "updated_by": source["plan_updated_by"],
                    "updated_at": source["plan_updated_at"],
                },
                "target": {
                    "draft_map_revision": current_revision,
                    "published_map_revision": current_revision,
                    "preview_fingerprint": current_fingerprint,
                    "version": int(source["plan_version"]) + 1,
                },
                "rollback": {
                    "draft_map_revision": source["draft_map_revision"],
                    "published_map_revision": source["published_map_revision"],
                    "preview_fingerprint": source["preview_fingerprint"],
                    "version": int(source["plan_version"]) + 2,
                },
                "legacy_preview_policy_versions": legacy_policy_versions,
                "disposition": disposition,
                "issues": sorted(set(plan_issues)),
                "slots": slot_evidence,
            }
            plans.append(plan_record)
            hard_blockers.extend(
                f"{area_code}/plan {source['plan_id']}: {issue}"
                for issue in sorted(set(plan_issues))
            )
    slot_count = sum(int(row["slot_count"]) for row in plans)
    if slot_count != EXPECTED_SLOT_COUNT:
        hard_blockers.append(
            f"4F 指定计划应有 {EXPECTED_SLOT_COUNT} 个位置，实际 {slot_count} 个。"
        )
    already_consistent = all(
        row["disposition"] == "already_consistent" for row in plans
    ) and len(plans) == EXPECTED_PLAN_COUNT
    candidates = [
        row for row in plans if row["disposition"] == "reference_refresh_candidate"
    ]
    ready = (
        not hard_blockers
        and len(candidates) == EXPECTED_PLAN_COUNT
        and slot_count == EXPECTED_SLOT_COUNT
    )
    plan: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "task_code": TASK_CODE,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "database": {
            "path": str(database),
            "sha256": file_sha256(database),
            "alembic_heads": alembic_heads,
            "checks": checks,
        },
        "published_map": {
            "path": str(map_path),
            "file_sha256": file_sha256(map_path),
            "scope_floor_code": SCOPE_FLOOR_CODE,
            "scope_floor_revision": current_revision,
            "scope_floor_sha256": canonical_hash(floor_layout),
        },
        "scope": {
            "area_codes": list(SCOPE_AREA_CODES),
            "expected_plan_count": EXPECTED_PLAN_COUNT,
            "expected_slot_count": EXPECTED_SLOT_COUNT,
            "actual_plan_count": len(plans),
            "actual_slot_count": slot_count,
        },
        "plans": plans,
        "slot_rows": all_slot_rows,
        "candidate_plan_ids_sha256": canonical_hash(
            sorted(int(row["plan_id"]) for row in candidates)
        ),
        "candidate_location_ids_sha256": canonical_hash(
            sorted(
                int(location_id)
                for row in candidates
                for location_id in row["location_ids"]
            )
        ),
        "protected_snapshot": protected,
        "trigger_snapshot": triggers,
        "hard_blockers": sorted(set(hard_blockers)),
        "execution_gate": {
            "ready": ready,
            "already_consistent": already_consistent,
            "candidate_plan_count": len(candidates),
            "candidate_slot_count": sum(row["slot_count"] for row in candidates),
            "formal_apply_ready": False,
            "reason": (
                "候选只完成技术门禁；正式写入仍须停服、新备份、同批地图和正式确认文件。"
                if ready
                else "存在身份、几何、数量、占用、策略或 fingerprint 阻断。"
            ),
        },
    }
    plan["plan_sha256"] = canonical_hash(plan)
    return plan


def write_audit_outputs(plan: dict[str, Any], output_dir: Path) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    plan_path = output_dir / "p1_152_floor4_revision_plan.json"
    csv_path = output_dir / "p1_152_floor4_revision_rows.csv"
    summary_path = output_dir / "p1_152_floor4_revision_summary.md"
    plan_path.write_text(
        json.dumps(plan, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    columns = [
        "area_code",
        "plan_id",
        "ground_slot_id",
        "location_id",
        "location_code",
        "route_sequence",
        "row_no",
        "slot_no",
        "plan_x_mm",
        "plan_y_mm",
        "derived_x_mm",
        "derived_y_mm",
        "width_mm",
        "depth_mm",
        "layout_id",
        "layout_version",
        "address_area_id",
        "lot_count",
        "lot_quantity",
        "current_pallet_count",
        "active_occupancy_count",
        "equivalent",
        "issues",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for row in plan["slot_rows"]:
            writer.writerow(
                {
                    key: canonical_json(row[key]) if key == "issues" else row[key]
                    for key in columns
                }
            )
    lines = [
        "# P1-152 四楼计划 revision 一致性审计",
        "",
        f"- 计划 SHA-256：`{plan['plan_sha256']}`",
        f"- 数据库 SHA-256：`{plan['database']['sha256']}`",
        f"- 4F revision：`{plan['published_map']['scope_floor_revision']}`",
        f"- 4F 内容 SHA-256：`{plan['published_map']['scope_floor_sha256']}`",
        f"- 指定计划：{plan['scope']['actual_plan_count']} 个；位置：{plan['scope']['actual_slot_count']} 个。",
        f"- 可执行：{'是' if plan['execution_gate']['ready'] else '否'}。",
        f"- 已一致：{'是' if plan['execution_gate']['already_consistent'] else '否'}。",
        "",
        "## 计划",
        "",
    ]
    for row in plan["plans"]:
        lines.append(
            f"- {row['area_code']} / plan {row['plan_id']}：{row['disposition']}；"
            f"{row['slot_count']} 个位置；旧 `{row['source']['published_map_revision']}` → "
            f"新 `{row['target']['published_map_revision']}`；几何 `{row['geometry_fingerprint']}`。"
        )
    if plan["hard_blockers"]:
        lines.extend(["", "## 阻断", ""])
        lines.extend(f"- {item}" for item in plan["hard_blockers"])
    summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"plan": plan_path, "csv": csv_path, "summary": summary_path}


def load_plan(path: Path) -> dict[str, Any]:
    plan = json.loads(path.resolve(strict=True).read_text(encoding="utf-8"))
    supplied = str(plan.pop("plan_sha256", ""))
    calculated = canonical_hash(plan)
    plan["plan_sha256"] = supplied
    if supplied != calculated:
        raise Floor4RevisionReconciliationError("P1-152 计划 SHA-256 校验失败。")
    _validate_plan_contract(plan)
    return plan


def _candidate_plans(plan: dict[str, Any]) -> list[dict[str, Any]]:
    if not plan["execution_gate"]["ready"] or plan["hard_blockers"]:
        return []
    return [
        row
        for row in plan["plans"]
        if row["disposition"] == "reference_refresh_candidate"
    ]


def _validate_plan_contract(plan: dict[str, Any]) -> None:
    if plan.get("schema_version") != SCHEMA_VERSION or plan.get("task_code") != TASK_CODE:
        raise Floor4RevisionReconciliationError("P1-152 计划版本或任务号不匹配。")
    if plan.get("scope", {}).get("area_codes") != list(SCOPE_AREA_CODES):
        raise Floor4RevisionReconciliationError("P1-152 计划区域范围不匹配。")
    planned_heads = plan.get("database", {}).get("alembic_heads") or []
    if len(planned_heads) != 1 or not all(
        isinstance(head, str) and head.strip() for head in planned_heads
    ):
        raise Floor4RevisionReconciliationError(
            "P1-152 计划必须记录且只能记录一个 Alembic head。"
        )
    candidates = _candidate_plans(plan)
    if candidates:
        if len(candidates) != EXPECTED_PLAN_COUNT:
            raise Floor4RevisionReconciliationError("P1-152 候选计划数量不匹配。")
        location_ids = sorted(
            int(location_id)
            for row in candidates
            for location_id in row["location_ids"]
        )
        if len(location_ids) != EXPECTED_SLOT_COUNT or len(set(location_ids)) != len(
            location_ids
        ):
            raise Floor4RevisionReconciliationError("P1-152 候选位置集合不匹配。")
        if canonical_hash(sorted(row["plan_id"] for row in candidates)) != plan.get(
            "candidate_plan_ids_sha256"
        ):
            raise Floor4RevisionReconciliationError("P1-152 候选计划 ID 哈希不匹配。")
        if canonical_hash(location_ids) != plan.get("candidate_location_ids_sha256"):
            raise Floor4RevisionReconciliationError("P1-152 候选位置 ID 哈希不匹配。")


def _approval_contract(
    plan: dict[str, Any], *, action: str, target_environment: str
) -> dict[str, Any]:
    return {
        "task_code": TASK_CODE,
        "action": action,
        "target_environment": target_environment,
        "plan_sha256": plan["plan_sha256"],
        "source_database_sha256": plan["database"]["sha256"],
        "scope_floor_sha256": plan["published_map"]["scope_floor_sha256"],
        "candidate_plan_ids_sha256": plan["candidate_plan_ids_sha256"],
        "candidate_location_ids_sha256": plan["candidate_location_ids_sha256"],
    }


def write_approval_template(
    plan: dict[str, Any], path: Path, *, action: str, target_environment: str
) -> Path:
    payload = _approval_contract(
        plan, action=action, target_environment=target_environment
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return path


def _validate_approval(
    plan: dict[str, Any],
    approval_path: Path | None,
    *,
    action: str,
    target_environment: str,
) -> None:
    if approval_path is None:
        raise Floor4RevisionReconciliationError("持久操作必须提供独立 approval 文件。")
    try:
        approval = json.loads(
            approval_path.resolve(strict=True).read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as error:
        raise Floor4RevisionReconciliationError("approval 文件不可读或不是 JSON。") from error
    if approval != _approval_contract(
        plan, action=action, target_environment=target_environment
    ):
        raise Floor4RevisionReconciliationError("approval 未精确绑定当前计划、环境或动作。")


def _actual_plan_row(
    connection: sqlite3.Connection, plan_id: int
) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT id AS plan_id,area_id,status AS plan_status,target_slot_count,
               numbering_origin,row_direction,slot_direction,row_start_no,slot_start_no,
               draft_map_revision,published_map_revision,preview_fingerprint,
               version AS plan_version,publish_idempotency_key,publish_request_hash,
               updated_by AS plan_updated_by,updated_at AS plan_updated_at,
               published_by,published_at
        FROM warehouse_ground_layout_plans WHERE id=?
        """,
        (plan_id,),
    ).fetchone()


def _plan_state_matches(
    connection: sqlite3.Connection,
    plan_row: dict[str, Any],
    state: str,
) -> bool:
    actual = _actual_plan_row(connection, int(plan_row["plan_id"]))
    if actual is None:
        return False
    static = plan_row["static_plan_contract"]
    for key, expected in static.items():
        if actual[key] != expected:
            return False
    expected = plan_row[state]
    for key, actual_key in (
        ("draft_map_revision", "draft_map_revision"),
        ("published_map_revision", "published_map_revision"),
        ("preview_fingerprint", "preview_fingerprint"),
        ("version", "plan_version"),
    ):
        if actual[actual_key] != expected[key]:
            return False
    if state == "source":
        return (
            actual["plan_updated_by"] == expected["updated_by"]
            and actual["plan_updated_at"] == expected["updated_at"]
        )
    return True


def _scope_state_matches(
    connection: sqlite3.Connection, plan: dict[str, Any], state: str
) -> bool:
    return all(
        _plan_state_matches(connection, row, state)
        for row in _candidate_plans(plan)
    )


def _operation_count(
    connection: sqlite3.Connection, *, batch_id: str, action_code: str
) -> int:
    return int(
        connection.execute(
            "SELECT count(*) FROM operation_logs WHERE batch_id=? AND action_code=?",
            (batch_id, action_code),
        ).fetchone()[0]
    )


def _actor_name(connection: sqlite3.Connection, actor_user_id: int) -> str | None:
    row = connection.execute("SELECT * FROM users WHERE id=?", (actor_user_id,)).fetchone()
    if row is None:
        raise Floor4RevisionReconciliationError("actor-user-id 不存在。")
    keys = set(row.keys())
    for key in ("full_name", "display_name", "username"):
        if key in keys and row[key]:
            return str(row[key])
    return None


def _insert_slot_audit(
    connection: sqlite3.Connection,
    *,
    actor_user_id: int,
    actor_name: str | None,
    batch_id: str,
    action_code: str,
    plan: dict[str, Any],
    plan_row: dict[str, Any],
    slot: dict[str, Any],
    direction: str,
) -> None:
    before_key, after_key = (
        ("source", "target") if direction == "apply" else ("target", "rollback")
    )
    details = canonical_json(
        {
            "task": TASK_CODE,
            "schema_version": SCHEMA_VERSION,
            "direction": direction,
            "plan_sha256": plan["plan_sha256"],
            "scope_floor_sha256": plan["published_map"]["scope_floor_sha256"],
            "area_code": plan_row["area_code"],
            "plan_id": plan_row["plan_id"],
            "ground_slot_id": slot["ground_slot_id"],
            "location_id": slot["location_id"],
            "location_code": slot["location_code"],
            "geometry_fingerprint": plan_row["geometry_fingerprint"],
            "before": plan_row[before_key],
            "after": plan_row[after_key],
            "inventory_effect": "none",
            "map_effect": "none",
        }
    )
    request_id = canonical_hash(
        {
            "batch_id": batch_id,
            "direction": direction,
            "ground_slot_id": slot["ground_slot_id"],
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
            "warehouse_ground_layout_plans",
            details,
            actor_name,
            "warehouse_ground_layout_plan",
            plan_row["plan_id"],
            f"P1-152 {direction} {plan_row['area_code']} / {slot['location_code']}",
            details,
            "warehouse_correction",
            "success",
            "admin_script",
            "warehouse",
            action_code,
            actor_user_id,
            actor_name,
            f"warehouse_location:{slot['location_id']}",
            request_id,
            batch_id,
            SCHEMA_VERSION,
        ),
    )


def _update_plan(
    connection: sqlite3.Connection,
    *,
    plan_row: dict[str, Any],
    actor_user_id: int,
    changed_at: str,
    direction: str,
) -> None:
    source_key, target_key = (
        ("source", "target") if direction == "apply" else ("target", "rollback")
    )
    source = plan_row[source_key]
    target = plan_row[target_key]
    result = connection.execute(
        """
        UPDATE warehouse_ground_layout_plans
        SET draft_map_revision=?,published_map_revision=?,preview_fingerprint=?,
            version=?,updated_by=?,updated_at=?
        WHERE id=? AND area_id=? AND status='published'
          AND draft_map_revision IS ? AND published_map_revision IS ?
          AND preview_fingerprint=? AND version=?
        """,
        (
            target["draft_map_revision"],
            target["published_map_revision"],
            target["preview_fingerprint"],
            target["version"],
            actor_user_id,
            changed_at,
            plan_row["plan_id"],
            plan_row["area_id"],
            source["draft_map_revision"],
            source["published_map_revision"],
            source["preview_fingerprint"],
            source["version"],
        ),
    )
    if result.rowcount != 1:
        raise Floor4RevisionReconciliationError(
            f"plan {plan_row['plan_id']} revision/fingerprint/version CAS 失败。"
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
        "P1_152_PLAN_REVISION_REFRESH"
        if direction == "apply"
        else "P1_152_PLAN_REVISION_ROLLBACK"
    )
    batch_id = canonical_hash(
        {"plan_sha256": plan["plan_sha256"], "direction": direction}
    )
    source_state = "source" if direction == "apply" else "target"
    target_state = "target" if direction == "apply" else "rollback"
    connection.execute("BEGIN IMMEDIATE")
    try:
        actor_name = _actor_name(connection, actor_user_id)
        if protected_snapshot(connection) != plan["protected_snapshot"]:
            raise Floor4RevisionReconciliationError(
                "P1-152 保护表已变化，CAS 前置校验失败。"
            )
        if _trigger_snapshot(connection) != plan["trigger_snapshot"]:
            raise Floor4RevisionReconciliationError("地堆计划保护触发器集合已变化。")
        if not _scope_state_matches(connection, plan, source_state):
            raise Floor4RevisionReconciliationError(
                "4F 计划 revision、fingerprint、version 或静态事实已变化，整批拒绝。"
            )
        trigger_sql = _mutable_trigger_sql(connection)
        connection.execute(f'DROP TRIGGER "{MUTABLE_TRIGGER}"')
        changed_at = datetime.now().astimezone().replace(tzinfo=None).isoformat(
            sep=" ", timespec="microseconds"
        )
        candidates = _candidate_plans(plan)
        for plan_row in candidates:
            _update_plan(
                connection,
                plan_row=plan_row,
                actor_user_id=actor_user_id,
                changed_at=changed_at,
                direction=direction,
            )
            for slot in plan_row["slots"]:
                _insert_slot_audit(
                    connection,
                    actor_user_id=actor_user_id,
                    actor_name=actor_name,
                    batch_id=batch_id,
                    action_code=action_code,
                    plan=plan,
                    plan_row=plan_row,
                    slot=slot,
                    direction=direction,
                )
        connection.execute(trigger_sql)
        if protected_snapshot(connection) != plan["protected_snapshot"]:
            raise Floor4RevisionReconciliationError(
                "受控修正改变了计划引用以外的保护表，整批回滚。"
            )
        if _trigger_snapshot(connection) != plan["trigger_snapshot"]:
            raise Floor4RevisionReconciliationError("保护触发器未原样恢复，整批回滚。")
        if not _scope_state_matches(connection, plan, target_state):
            raise Floor4RevisionReconciliationError("修正后的 4F 计划状态校验失败。")
        expected_logs = EXPECTED_SLOT_COUNT
        if _operation_count(
            connection, batch_id=batch_id, action_code=action_code
        ) != expected_logs:
            raise Floor4RevisionReconciliationError("逐位置审计日志数量不完整。")
        checks = _check_database(connection)
        if not checks["ok"]:
            raise Floor4RevisionReconciliationError("修正后完整性或外键检查失败。")
        if rollback_after_validation:
            connection.rollback()
            return {
                "status": "rehearsed_and_rolled_back",
                "batch_id": batch_id,
                "changed_plans": len(candidates),
                "affected_slots": EXPECTED_SLOT_COUNT,
                "checks": checks,
            }
        connection.commit()
        return {
            "status": "applied" if direction == "apply" else "rolled_back",
            "batch_id": batch_id,
            "changed_plans": len(candidates),
            "affected_slots": EXPECTED_SLOT_COUNT,
            "checks": checks,
        }
    except Exception:
        connection.rollback()
        raise


def _verified_backup(database: Path, output_dir: Path, plan_sha: str) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    target = output_dir / (
        f"{database.stem}.before_p1_152_{stamp}_{plan_sha[:12]}.sqlite3"
    )
    shutil.copy2(database, target)
    if target.stat().st_size != database.stat().st_size or file_sha256(target) != file_sha256(
        database
    ):
        target.unlink(missing_ok=True)
        raise Floor4RevisionReconciliationError("备份大小或 SHA-256 校验失败。")
    with readonly_database(target) as connection:
        checks = _check_database(connection)
    if not checks["ok"]:
        target.unlink(missing_ok=True)
        raise Floor4RevisionReconciliationError("备份完整性或外键检查失败。")
    return {
        "path": str(target),
        "size": target.stat().st_size,
        "sha256": file_sha256(target),
        "checks": checks,
    }


def _validate_current_floor(plan: dict[str, Any], map_path: Path) -> None:
    _document, floor_layout = _load_map(map_path)
    if (
        str(floor_layout.get("revision") or "")
        != plan["published_map"]["scope_floor_revision"]
        or canonical_hash(floor_layout)
        != plan["published_map"]["scope_floor_sha256"]
    ):
        raise Floor4RevisionReconciliationError(
            "当前 4F 地图 revision 或内容哈希与 P1-152 计划不一致。"
        )


def _validate_database_head(
    connection: sqlite3.Connection, plan: dict[str, Any]
) -> None:
    current_heads = [
        str(row[0])
        for row in connection.execute(
            "SELECT version_num FROM alembic_version ORDER BY version_num"
        )
    ]
    planned_heads = [str(value) for value in plan["database"]["alembic_heads"]]
    if (
        len(current_heads) != 1
        or not current_heads[0].strip()
        or current_heads != planned_heads
    ):
        raise Floor4RevisionReconciliationError(
            "数据库 Alembic head 与 P1-152 只读计划不一致："
            f"计划 {planned_heads}，当前 {current_heads}。"
        )


def execute_plan(
    *,
    database: Path,
    plan_path: Path,
    published_map: Path,
    actor_user_id: int,
    direction: str,
    target_environment: str,
    confirm_isolated_copy: bool,
    confirm_formal_database: bool,
    token: str,
    approval_path: Path | None = None,
    backup_dir: Path | None = None,
    rehearse: bool = False,
) -> dict[str, Any]:
    database = database.resolve(strict=True)
    if rehearse and target_environment != "isolated":
        raise Floor4RevisionReconciliationError(
            "P1-152 rehearse 只允许在隔离数据库副本执行。"
        )
    _assert_execution_target(
        database,
        target_environment=target_environment,
        confirm_isolated_copy=confirm_isolated_copy,
        confirm_formal_database=confirm_formal_database,
    )
    plan = load_plan(plan_path)
    candidates = _candidate_plans(plan)
    if len(candidates) != EXPECTED_PLAN_COUNT:
        raise Floor4RevisionReconciliationError("P1-152 计划未通过全范围执行门禁。")
    _validate_current_floor(plan, published_map.resolve(strict=True))
    expected_token = APPLY_TOKEN if direction == "apply" else ROLLBACK_TOKEN
    if token != expected_token:
        raise Floor4RevisionReconciliationError("受控操作确认 token 不匹配。")
    if rehearse and direction != "apply":
        raise Floor4RevisionReconciliationError("rehearse 只允许演练 apply。")
    if not rehearse:
        _validate_approval(
            plan,
            approval_path,
            action=direction,
            target_environment=target_environment,
        )
        if backup_dir is None:
            raise Floor4RevisionReconciliationError("持久操作必须显式提供 backup-dir。")
    current_sha = file_sha256(database)
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    backup = None
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        _required_tables(connection)
        _validate_database_head(connection, plan)
        action_code = (
            "P1_152_PLAN_REVISION_REFRESH"
            if direction == "apply"
            else "P1_152_PLAN_REVISION_ROLLBACK"
        )
        batch_id = canonical_hash(
            {"plan_sha256": plan["plan_sha256"], "direction": direction}
        )
        final_state = "target" if direction == "apply" else "rollback"
        expected_log_count = EXPECTED_SLOT_COUNT
        final_matches = _scope_state_matches(connection, plan, final_state)
        log_count = _operation_count(
            connection, batch_id=batch_id, action_code=action_code
        )
        if final_matches and log_count == expected_log_count:
            return {
                "status": "idempotent_replay",
                "batch_id": batch_id,
                "changed_plans": 0,
                "affected_slots": 0,
                "backup": None,
            }
        if final_matches or log_count:
            raise Floor4RevisionReconciliationError(
                "目标状态与逐位置审计日志不成对，拒绝冒充幂等执行。"
            )
        if direction == "apply":
            if current_sha != plan["database"]["sha256"]:
                raise Floor4RevisionReconciliationError(
                    "数据库 SHA-256 与只读 P1-152 计划不一致。"
                )
        else:
            apply_batch = canonical_hash(
                {"plan_sha256": plan["plan_sha256"], "direction": "apply"}
            )
            if not _scope_state_matches(connection, plan, "target") or _operation_count(
                connection,
                batch_id=apply_batch,
                action_code="P1_152_PLAN_REVISION_REFRESH",
            ) != EXPECTED_SLOT_COUNT:
                raise Floor4RevisionReconciliationError(
                    "rollback 只能针对该计划已完整 apply 的状态。"
                )
        if not rehearse:
            backup = _verified_backup(database, backup_dir, plan["plan_sha256"])
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
            raise Floor4RevisionReconciliationError(
                "事务复演已回滚，但数据库文件 SHA-256 发生变化。"
            )
        return result
    finally:
        connection.close()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="P1-152 四楼已发布地堆计划 revision 一致性审计与受控修正"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    audit = subparsers.add_parser("audit")
    audit.add_argument("--database", type=Path, required=True)
    audit.add_argument("--published-map", type=Path, required=True)
    audit.add_argument("--output-dir", type=Path, required=True)
    approval = subparsers.add_parser("approval-template")
    approval.add_argument("--plan", type=Path, required=True)
    approval.add_argument("--output", type=Path, required=True)
    approval.add_argument("--action", choices=("apply", "rollback"), required=True)
    approval.add_argument(
        "--target-environment", choices=("isolated", "formal"), required=True
    )
    for command in ("rehearse", "apply", "rollback"):
        child = subparsers.add_parser(command)
        child.add_argument("--database", type=Path, required=True)
        child.add_argument("--plan", type=Path, required=True)
        child.add_argument("--published-map", type=Path, required=True)
        child.add_argument("--actor-user-id", type=int, required=True)
        child.add_argument(
            "--target-environment",
            choices=(
                ("isolated",)
                if command == "rehearse"
                else ("isolated", "formal")
            ),
            required=True,
        )
        child.add_argument("--confirm-isolated-copy", action="store_true")
        child.add_argument("--confirm-formal-database", action="store_true")
        child.add_argument("--token", required=True)
        if command != "rehearse":
            child.add_argument("--approval", type=Path, required=True)
            child.add_argument("--backup-dir", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "audit":
            plan = build_audit(args.database, args.published_map)
            outputs = write_audit_outputs(plan, args.output_dir)
            print(
                json.dumps(
                    {
                        "plan_sha256": plan["plan_sha256"],
                        "ready": plan["execution_gate"]["ready"],
                        "already_consistent": plan["execution_gate"][
                            "already_consistent"
                        ],
                        "hard_blockers": plan["hard_blockers"],
                        "outputs": {key: str(value) for key, value in outputs.items()},
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0 if not plan["hard_blockers"] else 2
        if args.command == "approval-template":
            plan = load_plan(args.plan)
            path = write_approval_template(
                plan,
                args.output,
                action=args.action,
                target_environment=args.target_environment,
            )
            print(path)
            return 0
        direction = "rollback" if args.command == "rollback" else "apply"
        result = execute_plan(
            database=args.database,
            plan_path=args.plan,
            published_map=args.published_map,
            actor_user_id=args.actor_user_id,
            direction=direction,
            target_environment=args.target_environment,
            confirm_isolated_copy=args.confirm_isolated_copy,
            confirm_formal_database=args.confirm_formal_database,
            token=args.token,
            approval_path=getattr(args, "approval", None),
            backup_dir=getattr(args, "backup_dir", None),
            rehearse=args.command == "rehearse",
        )
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0
    except Floor4RevisionReconciliationError as error:
        print(str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
