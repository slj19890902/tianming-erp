"""register the confirmed floor-three V11 layout in the space ledger

Revision ID: cr74v8x9z63
Revises: cq73v8x9z62
Create Date: 2026-07-26
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "cr74v8x9z63"
down_revision: Union[str, Sequence[str], None] = "cq73v8x9z62"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

FLOOR_CODE = "3F"
FLOOR_NAME = "三楼"
FLOOR_NUMBER = 3
FLOOR_STATUS = "enabled"
FLOOR_REMARKS = (
    "三楼 V11 既有布局兼容接入；保留原库位编号、location_id 和全部库存事实。"
)
TEMPORARY_AREAS = {"F12", "F34"}
SEED_EXPECTED_COUNT = 396

PREFLIGHT_ERROR = (
    "三楼 V11 台账或布局未通过兼容接入核对；升级已在写入楼层/区域台账前中止："
)
COLLISION_ERROR = (
    "三楼楼层台账已存在，无法证明可安全兼容接入；升级已在写入前中止。"
)
DOWNGRADE_ERROR = (
    "三楼通用台账已被修改、扩展或新增库位引用，禁止破坏性降级；"
    "请保留当前数据库并恢复升级前完整备份。"
)


def _seed_path() -> Path:
    return Path(__file__).resolve().parents[1] / "seed_data" / "floor3_locations_v11.json"


def _expected_seed_rows() -> dict[str, dict[str, Any]]:
    payload = json.loads(_seed_path().read_text(encoding="utf-8"))
    rows: dict[str, dict[str, Any]] = {}
    for group in payload["groups"]:
        for segment in group["segments"]:
            for number in range(segment["start"], segment["end"] + 1):
                code = group["code_template"].format(
                    area_code=group["area_code"],
                    side_code=segment.get("side_code") or "",
                    level_no=segment.get("level_no") or "",
                    number=number,
                )
                rows[code] = {
                    "warehouse_floor": int(payload["warehouse_floor"]),
                    "area_code": str(group["area_code"]),
                    "storage_type": str(group["storage_type"]),
                    "source_version": str(payload["source_version"]),
                }
    if len(rows) != SEED_EXPECTED_COUNT:
        raise RuntimeError(
            PREFLIGHT_ERROR
            + f"受控 V11 清单应为 {SEED_EXPECTED_COUNT} 个，实际为 {len(rows)} 个。"
        )
    return rows


def _current_v11_rows(connection: sa.Connection) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in connection.execute(
            sa.text(
                """
                SELECT location.id, location.location_code,
                       location.warehouse_floor, location.area_code,
                       location.storage_type, location.source_version,
                       location.placement_status, location.is_active,
                       location.sort_order,
                       layout.id AS layout_id
                FROM warehouse_locations AS location
                LEFT JOIN floor3_location_layouts AS layout
                  ON layout.location_id = location.id
                WHERE location.source_version = 'V11'
                ORDER BY location.sort_order, location.id
                """
            )
        ).mappings()
    ]


def _assert_v11_layout_is_complete(connection: sa.Connection) -> list[dict[str, Any]]:
    expected = _expected_seed_rows()
    current = _current_v11_rows(connection)
    current_by_code = {str(row["location_code"]): row for row in current}

    missing = sorted(set(expected) - set(current_by_code))
    if missing:
        raise RuntimeError(
            PREFLIGHT_ERROR + "缺少受控 V11 库位：" + ", ".join(missing[:20])
        )

    drifted: list[str] = []
    for code, expected_row in expected.items():
        actual = current_by_code[code]
        if any(actual[field] != value for field, value in expected_row.items()):
            drifted.append(code)
    if drifted:
        raise RuntimeError(
            PREFLIGHT_ERROR + "受控 V11 库位结构漂移：" + ", ".join(drifted[:20])
        )

    invalid = [
        str(row["location_code"])
        for row in current
        if row["warehouse_floor"] != FLOOR_NUMBER
        or not str(row["area_code"] or "").strip()
        or row["storage_type"] not in {"ground", "rack", "temporary_aisle"}
        or row["placement_status"] != "placed"
        or row["layout_id"] is None
    ]
    if invalid:
        raise RuntimeError(
            PREFLIGHT_ERROR
            + "存在未布局、字段不完整或不在三楼的 V11 库位："
            + ", ".join(invalid[:20])
        )

    distinct_layouts = int(
        connection.execute(
            sa.text(
                """
                SELECT COUNT(DISTINCT layout.location_id)
                FROM floor3_location_layouts AS layout
                JOIN warehouse_locations AS location
                  ON location.id = layout.location_id
                WHERE location.source_version = 'V11'
                """
            )
        ).scalar()
        or 0
    )
    if distinct_layouts != len(current):
        raise RuntimeError(
            PREFLIGHT_ERROR
            + f"V11 库位/唯一布局数量不一致：{len(current)}/{distinct_layouts}。"
        )
    return current


def _area_remarks(area_code: str) -> str:
    if area_code in TEMPORARY_AREAS:
        return "V11 过道临放位；不是普通长期货位，现有布局与业务门禁保持不变。"
    if area_code == "CD1":
        return (
            "三楼 V11 已确认布局兼容接入；当前登记值来自现有布局，"
            "未来扩容仍须现场复核。"
        )
    return "三楼 V11 已确认布局兼容接入；当前登记值来自现有布局。"


def _area_rows(current: list[dict[str, Any]]) -> list[dict[str, Any]]:
    areas: dict[str, dict[str, Any]] = {}
    for row in current:
        area_code = str(row["area_code"])
        item = areas.setdefault(
            area_code,
            {
                "area_code": area_code,
                "area_name": f"{area_code} 区",
                "planned_location_count": 0,
                "planned_pallet_capacity": 0,
                "construction_status": "enabled",
                "remarks": _area_remarks(area_code),
                "_sort_order": int(row["sort_order"] or 0),
            },
        )
        item["planned_location_count"] += 1
        if row["storage_type"] in {"ground", "temporary_aisle"}:
            item["planned_pallet_capacity"] += 1
        item["_sort_order"] = min(
            int(item["_sort_order"]), int(row["sort_order"] or 0)
        )
    return sorted(
        areas.values(),
        key=lambda row: (int(row["_sort_order"]), str(row["area_code"])),
    )


def _assert_floor_slot_available(connection: sa.Connection) -> None:
    collision_count = int(
        connection.execute(
            sa.text(
                """
                SELECT COUNT(*)
                FROM warehouse_floors
                WHERE UPPER(TRIM(floor_code)) = :floor_code
                   OR floor_number = :floor_number
                """
            ),
            {"floor_code": FLOOR_CODE, "floor_number": FLOOR_NUMBER},
        ).scalar()
        or 0
    )
    if collision_count:
        raise RuntimeError(COLLISION_ERROR)


def upgrade() -> None:
    connection = op.get_bind()
    current = _assert_v11_layout_is_complete(connection)
    _assert_floor_slot_available(connection)

    floors = sa.table(
        "warehouse_floors",
        sa.column("id", sa.Integer()),
        sa.column("floor_code", sa.String()),
        sa.column("floor_name", sa.String()),
        sa.column("floor_number", sa.Integer()),
        sa.column("construction_status", sa.String()),
        sa.column("remarks", sa.Text()),
    )
    areas = sa.table(
        "warehouse_areas",
        sa.column("floor_id", sa.Integer()),
        sa.column("area_code", sa.String()),
        sa.column("area_name", sa.String()),
        sa.column("planned_location_count", sa.Integer()),
        sa.column("planned_pallet_capacity", sa.Integer()),
        sa.column("construction_status", sa.String()),
        sa.column("remarks", sa.Text()),
    )

    connection.execute(
        sa.insert(floors).values(
            floor_code=FLOOR_CODE,
            floor_name=FLOOR_NAME,
            floor_number=FLOOR_NUMBER,
            construction_status=FLOOR_STATUS,
            remarks=FLOOR_REMARKS,
        )
    )
    floor_id = int(
        connection.execute(
            sa.select(floors.c.id).where(floors.c.floor_code == FLOOR_CODE)
        ).scalar_one()
    )

    values = []
    for area in _area_rows(current):
        area.pop("_sort_order", None)
        values.append({"floor_id": floor_id, **area})
    connection.execute(sa.insert(areas), values)


def _assert_compat_rows_unchanged(
    connection: sa.Connection,
    floor: dict[str, Any],
) -> None:
    if (
        floor["floor_code"] != FLOOR_CODE
        or floor["floor_name"] != FLOOR_NAME
        or floor["floor_number"] != FLOOR_NUMBER
        or floor["construction_status"] != FLOOR_STATUS
        or floor["remarks"] != FLOOR_REMARKS
        or floor["updated_at"] is not None
    ):
        raise RuntimeError(DOWNGRADE_ERROR)

    current = _assert_v11_layout_is_complete(connection)
    expected_areas = {
        str(row["area_code"]): row for row in _area_rows(current)
    }
    actual_areas = {
        str(row["area_code"]): dict(row)
        for row in connection.execute(
            sa.text(
                """
                SELECT area_code, area_name, planned_location_count,
                       planned_pallet_capacity, construction_status,
                       remarks, updated_at
                FROM warehouse_areas
                WHERE floor_id = :floor_id
                """
            ),
            {"floor_id": floor["id"]},
        ).mappings()
    }
    if set(actual_areas) != set(expected_areas):
        raise RuntimeError(DOWNGRADE_ERROR)
    for area_code, expected in expected_areas.items():
        actual = actual_areas[area_code]
        if (
            actual["area_name"] != expected["area_name"]
            or actual["planned_location_count"]
            != expected["planned_location_count"]
            or actual["planned_pallet_capacity"]
            != expected["planned_pallet_capacity"]
            or actual["construction_status"] != expected["construction_status"]
            or actual["remarks"] != expected["remarks"]
            or actual["updated_at"] is not None
        ):
            raise RuntimeError(DOWNGRADE_ERROR)

    new_location_count = int(
        connection.execute(
            sa.text(
                """
                SELECT COUNT(*)
                FROM warehouse_locations
                WHERE warehouse_floor = :floor_number
                  AND COALESCE(source_version, '') <> 'V11'
                """
            ),
            {"floor_number": FLOOR_NUMBER},
        ).scalar()
        or 0
    )
    if new_location_count:
        raise RuntimeError(DOWNGRADE_ERROR)


def downgrade() -> None:
    connection = op.get_bind()
    floors = [
        dict(row)
        for row in connection.execute(
            sa.text(
                """
                SELECT id, floor_code, floor_name, floor_number,
                       construction_status, remarks, updated_at
                FROM warehouse_floors
                WHERE UPPER(TRIM(floor_code)) = :floor_code
                   OR floor_number = :floor_number
                """
            ),
            {"floor_code": FLOOR_CODE, "floor_number": FLOOR_NUMBER},
        ).mappings()
    ]
    if len(floors) != 1:
        raise RuntimeError(DOWNGRADE_ERROR)
    floor = floors[0]
    _assert_compat_rows_unchanged(connection, floor)
    connection.execute(
        sa.text("DELETE FROM warehouse_areas WHERE floor_id = :floor_id"),
        {"floor_id": floor["id"]},
    )
    connection.execute(
        sa.text("DELETE FROM warehouse_floors WHERE id = :floor_id"),
        {"floor_id": floor["id"]},
    )
