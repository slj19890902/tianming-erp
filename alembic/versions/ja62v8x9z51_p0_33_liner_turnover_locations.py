"""enable the owner-confirmed linerboard turnover locations

Revision ID: ja62v8x9z51
Revises: iz61v8x9z50
Create Date: 2026-09-01

F34 and F12 retain their existing current-map logical anchors.  They are
temporary turnover positions, not fabricated measured 1200x1000 ground slots.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
import json

from alembic import op
import sqlalchemy as sa


revision = "ja62v8x9z51"
down_revision = "iz61v8x9z50"
branch_labels = None
depends_on = None


MAP_REVISION = "3994317ae14a7f18"
AREA_SPECS = {
    "F34": {
        "feature_id": "d068d43e-58a5-40d8-bb7f-0ea88d714e2e",
        "count": 3,
        "new_remarks": (
            "老板确认：衬板成品临时周转优先使用 F34；满位后转 F12；"
            "不是长期固定货位。"
        ),
    },
    "F12": {
        "feature_id": "0a1c6bf6-c0d9-4217-b9a0-2527db20b9d9",
        "count": 8,
        "new_remarks": (
            "老板确认：衬板成品在 F34 满位后临时周转到 F12；"
            "不是长期固定货位。"
        ),
    },
}
OLD_AREA_REMARKS = "V11 过道临放位；不是普通长期货位，现有布局与业务门禁保持不变。"
LOCATION_REMARKS = "P0-33：衬板成品临时周转位置；当前栈板必须保持待归位状态。"
CURRENT_MAP_MIGRATION_KEY_PREFIX = "p0-26-current-map-"


def _expected_codes() -> set[str]:
    return {
        *{f"F34-P{index:02d}" for index in range(1, 4)},
        *{f"F12-P{index:02d}" for index in range(1, 9)},
    }


def _rows(connection: sa.Connection) -> list[dict]:
    return [
        dict(row)
        for row in connection.execute(
            sa.text(
                """
                SELECT location.id, location.location_code,
                       upper(location.area_code) AS area_code,
                       location.warehouse_type, location.warehouse_floor,
                       location.storage_type, location.source_version,
                       location.placement_status, location.is_temporary,
                       location.is_active, location.address_kind,
                       location.address_area_id, location.remarks,
                       floor.construction_status AS floor_status,
                       area.id AS area_id,
                       area.construction_status AS area_status,
                       area.planned_location_count,
                       area.planned_pallet_capacity,
                       area.capacity_review_status,
                       area.capacity_eligible,
                       area.confirmed_pallet_capacity,
                       area.remarks AS area_remarks,
                       policy.map_feature_id, policy.allowed_inventory_types_json,
                       policy.storage_layout AS policy_storage_layout,
                       policy.status AS policy_status,
                       policy.published_map_revision,
                       layout.left_pct, layout.top_pct,
                       layout.width_pct, layout.height_pct,
                       layout.source_type AS layout_source_type,
                       layout.layout_kind
                FROM warehouse_locations AS location
                JOIN warehouse_areas AS area
                  ON area.id = location.address_area_id
                JOIN warehouse_floors AS floor
                  ON floor.id = area.floor_id
                JOIN warehouse_area_storage_policies AS policy
                  ON policy.area_id = area.id
                LEFT JOIN floor3_location_layouts AS layout
                  ON layout.location_id = location.id
                WHERE floor.floor_number = 3
                  AND upper(location.area_code) IN ('F34', 'F12')
                ORDER BY upper(location.area_code), location.location_code
                """
            )
        ).mappings()
    ]


def _decimal(value: object) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.0001"))


def _expected_layout(code: str) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    area_code, suffix = code.split("-P", 1)
    index = int(suffix)
    if area_code == "F12":
        return (
            Decimal("0.0000"),
            Decimal(index - 1) * Decimal("12.5000"),
            Decimal("100.0000"),
            Decimal("12.5000"),
        )
    layouts = {
        1: ("0.0000", "0.0000", "100.0000", "33.3333"),
        2: ("0.0000", "33.3333", "100.0000", "33.3334"),
        3: ("0.0000", "66.6667", "100.0000", "33.3333"),
    }
    return tuple(Decimal(value) for value in layouts[index])  # type: ignore[return-value]


def _count(connection: sa.Connection, sql: str, **params: object) -> int:
    return int(connection.execute(sa.text(sql), params).scalar() or 0)


def _has_formal_current_map_baseline(connection: sa.Connection) -> bool:
    """Separate audited factory copies from brand-new empty installations."""

    return bool(
        _count(
            connection,
            "SELECT COUNT(*) FROM warehouse_current_map_migration_snapshots "
            "WHERE migration_key LIKE :migration_key",
            migration_key=f"{CURRENT_MAP_MIGRATION_KEY_PREFIX}%",
        )
    )


def _assert_baseline(connection: sa.Connection, *, expected_active: bool) -> list[int]:
    rows = _rows(connection)
    codes = {str(row["location_code"]) for row in rows}
    if len(rows) != 11 or codes != _expected_codes():
        raise RuntimeError(
            "P0-33 升级前核对失败：F34/F12 必须恰好保留既有 3+8 个稳定临时位置。"
        )
    for row in rows:
        code = str(row["location_code"])
        area_code = str(row["area_code"])
        spec = AREA_SPECS[area_code]
        try:
            allowed_types = json.loads(str(row["allowed_inventory_types_json"]))
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise RuntimeError(
                f"P0-33 升级前核对失败：{area_code} 存放策略已损坏。"
            ) from error
        if not isinstance(allowed_types, list):
            raise RuntimeError(
                f"P0-33 升级前核对失败：{area_code} 存放策略不是受支持的清单。"
            )
        try:
            actual_layout = tuple(
                _decimal(row[field])
                for field in ("left_pct", "top_pct", "width_pct", "height_pct")
            )
        except (InvalidOperation, TypeError, ValueError) as error:
            raise RuntimeError(
                f"P0-33 升级前核对失败：{code} 布局坐标缺失或无效。"
            ) from error
        invalid = (
            int(row["warehouse_floor"] or 0) != 3
            or row["floor_status"] != "enabled"
            or row["area_status"] != "enabled"
            or row["warehouse_type"] != "finished"
            or row["storage_type"] != "temporary_aisle"
            or row["source_version"] != "CURRENT_MAP"
            or row["placement_status"] != "placed"
            or not bool(row["is_temporary"])
            or bool(row["is_active"]) is not expected_active
            or row["address_kind"] != "functional"
            or int(row["address_area_id"] or 0) != int(row["area_id"] or 0)
            or int(row["planned_location_count"] or 0) != int(spec["count"])
            or int(row["planned_pallet_capacity"] or 0) != int(spec["count"])
            or row["capacity_review_status"] != "confirmed"
            or not bool(row["capacity_eligible"])
            or int(row["confirmed_pallet_capacity"] or 0) != int(spec["count"])
            or row["policy_status"] != "published"
            or row["policy_storage_layout"] != "pallet_ground"
            or set(allowed_types) != {"finished"}
            or row["published_map_revision"] != MAP_REVISION
            or row["map_feature_id"] != spec["feature_id"]
            or row["layout_source_type"] != "manual"
            or row["layout_kind"] != "logical_anchor"
            or actual_layout != _expected_layout(code)
        )
        if invalid:
            raise RuntimeError(
                f"P0-33 升级前核对失败：{code} 不是预期的当前地图临时周转位置。"
            )
        expected_area_remarks = (
            str(spec["new_remarks"]) if expected_active else OLD_AREA_REMARKS
        )
        if str(row["area_remarks"] or "") != expected_area_remarks:
            raise RuntimeError(
                f"P0-33 升级前核对失败：{area_code} 区域说明已发生漂移。"
            )
        expected_location_remarks = LOCATION_REMARKS if expected_active else ""
        if str(row["remarks"] or "") != expected_location_remarks:
            raise RuntimeError(
                f"P0-33 升级前核对失败：{code} 位置说明已发生漂移。"
            )
    location_ids = [int(row["id"]) for row in rows]
    placeholders = ",".join(str(value) for value in location_ids)
    if _count(
        connection,
        f"SELECT COUNT(*) FROM warehouse_ground_layout_slots WHERE location_id IN ({placeholders})",
    ):
        raise RuntimeError(
            "P0-33 升级前核对失败：F34/F12 已存在地堆排位，不能按逻辑临时位置重复启用。"
        )
    return location_ids


def _assert_no_live_facts(connection: sa.Connection, location_ids: list[int]) -> None:
    placeholders = ",".join(str(value) for value in location_ids)
    checks = {
        "当前栈板": (
            "SELECT COUNT(*) FROM inventory_pallets "
            f"WHERE is_current=1 AND location_id IN ({placeholders})"
        ),
        "正库存批次": (
            "SELECT COUNT(*) FROM inventory_lots "
            f"WHERE warehouse_location_id IN ({placeholders}) "
            "AND status IN ('active','frozen') "
            "AND quantity_available+quantity_reserved+quantity_damaged>0"
        ),
        "活动地堆占用": (
            "SELECT COUNT(*) FROM warehouse_ground_occupancy_slots AS slot "
            "JOIN warehouse_ground_occupancies AS occupancy "
            "ON occupancy.id=slot.occupancy_id "
            f"WHERE slot.location_id IN ({placeholders}) "
            "AND slot.status='active' AND occupancy.status='active'"
        ),
    }
    failures = [name for name, sql in checks.items() if _count(connection, sql)]
    if failures:
        raise RuntimeError(
            "P0-33 迁移拒绝改动仍有业务事实的临时位置：" + "、".join(failures)
        )


def upgrade() -> None:
    connection = op.get_bind()
    # fg42 intentionally skips the factory-specific current-map data rewrite
    # on brand-new empty installations.  P0-33 must follow that same boundary:
    # do not turn legacy V11 aisle anchors into formal locations by guessing.
    if not _has_formal_current_map_baseline(connection):
        return
    location_ids = _assert_baseline(connection, expected_active=False)
    _assert_no_live_facts(connection, location_ids)
    connection.execute(
        sa.text(
            """
            UPDATE warehouse_locations
            SET is_active=1,
                remarks=:remarks,
                address_version=address_version+1,
                updated_at=CURRENT_TIMESTAMP
            WHERE id IN (%s)
            """
            % ",".join(str(value) for value in location_ids)
        ),
        {"remarks": LOCATION_REMARKS},
    )
    for area_code, spec in AREA_SPECS.items():
        connection.execute(
            sa.text(
                """
                UPDATE warehouse_areas
                SET remarks=:remarks, updated_at=CURRENT_TIMESTAMP
                WHERE upper(area_code)=:area_code
                  AND floor_id IN (
                    SELECT id FROM warehouse_floors WHERE floor_number=3
                  )
                """
            ),
            {"remarks": spec["new_remarks"], "area_code": area_code},
        )
    _assert_baseline(connection, expected_active=True)


def downgrade() -> None:
    connection = op.get_bind()
    if not _has_formal_current_map_baseline(connection):
        return
    location_ids = _assert_baseline(connection, expected_active=True)
    _assert_no_live_facts(connection, location_ids)
    connection.execute(
        sa.text(
            """
            UPDATE warehouse_locations
            SET is_active=0,
                remarks=NULL,
                address_version=address_version+1,
                updated_at=CURRENT_TIMESTAMP
            WHERE id IN (%s)
            """
            % ",".join(str(value) for value in location_ids)
        )
    )
    connection.execute(
        sa.text(
            """
            UPDATE warehouse_areas
            SET remarks=:remarks, updated_at=CURRENT_TIMESTAMP
            WHERE upper(area_code) IN ('F34','F12')
              AND floor_id IN (
                SELECT id FROM warehouse_floors WHERE floor_number=3
              )
            """
        ),
        {"remarks": OLD_AREA_REMARKS},
    )
    _assert_baseline(connection, expected_active=False)
