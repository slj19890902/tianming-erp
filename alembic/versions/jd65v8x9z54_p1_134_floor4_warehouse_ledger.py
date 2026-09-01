"""register the fourth-floor warehouse ledger shell

Revision ID: jd65v8x9z54
Revises: jc64v8x9z53
Create Date: 2026-09-01

This migration creates only the formal 4F floor identity required by the
warehouse-map activation gates.  Areas, locations, customer preferences and
inventory remain operator-created facts.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "jd65v8x9z54"
down_revision = "jc64v8x9z53"
branch_labels = None
depends_on = None


FLOOR_CODE = "4F"
FLOOR_NAME = "四楼"
FLOOR_NUMBER = 4
FLOOR_STATUS = "enabled"
SEED_REMARK = (
    "P1-134 自动建立四楼基础台账；区域、货位、容量和客户默认存放区域"
    "必须由管理员按已标定并发布的实测地图确认。"
)


def _floor_candidates(connection: sa.Connection) -> list[dict]:
    return [
        dict(row)
        for row in connection.execute(
            sa.text(
                """
                SELECT *
                FROM warehouse_floors
                WHERE UPPER(TRIM(floor_code)) = :floor_code
                   OR floor_number = :floor_number
                ORDER BY id
                """
            ),
            {"floor_code": FLOOR_CODE, "floor_number": FLOOR_NUMBER},
        ).mappings()
    ]


def _is_compatible_floor(row: dict) -> bool:
    return (
        str(row.get("floor_code") or "").strip().upper() == FLOOR_CODE
        and str(row.get("floor_name") or "").strip() == FLOOR_NAME
        and int(row.get("floor_number") or 0) == FLOOR_NUMBER
        and str(row.get("construction_status") or "") == FLOOR_STATUS
        and int(row.get("planning_reference_pallet_capacity") or 0) >= 0
    )


def upgrade() -> None:
    connection = op.get_bind()
    candidates = _floor_candidates(connection)
    if len(candidates) > 1:
        raise RuntimeError("4F 编码和四楼编号指向不同楼层，拒绝自动建立楼层台账")
    if candidates:
        if not _is_compatible_floor(candidates[0]):
            raise RuntimeError("现有四楼台账与已启用的 4F 定义冲突，拒绝自动改写")
        return

    connection.execute(
        sa.text(
            """
            INSERT INTO warehouse_floors (
                floor_code, floor_name, floor_number,
                construction_status, planning_reference_pallet_capacity,
                remarks
            ) VALUES (
                :floor_code, :floor_name, :floor_number,
                :construction_status, 0, :remarks
            )
            """
        ),
        {
            "floor_code": FLOOR_CODE,
            "floor_name": FLOOR_NAME,
            "floor_number": FLOOR_NUMBER,
            "construction_status": FLOOR_STATUS,
            "remarks": SEED_REMARK,
        },
    )


def downgrade() -> None:
    connection = op.get_bind()
    candidates = _floor_candidates(connection)
    if not candidates:
        return
    if len(candidates) > 1:
        raise RuntimeError("四楼楼层身份存在冲突，禁止破坏性降级")

    floor = candidates[0]
    if floor.get("remarks") != SEED_REMARK:
        return
    if not _is_compatible_floor(floor) or int(
        floor.get("planning_reference_pallet_capacity") or 0
    ) != 0:
        raise RuntimeError("四楼基础台账已被人工修改，禁止破坏性降级")

    floor_id = int(floor["id"])
    dependent_counts = {
        "areas": int(
            connection.scalar(
                sa.text("SELECT COUNT(*) FROM warehouse_areas WHERE floor_id=:id"),
                {"id": floor_id},
            )
            or 0
        ),
        "locations": int(
            connection.scalar(
                sa.text(
                    "SELECT COUNT(*) FROM warehouse_locations "
                    "WHERE warehouse_floor=:floor_number"
                ),
                {"floor_number": FLOOR_NUMBER},
            )
            or 0
        ),
        "capacity_plans": int(
            connection.scalar(
                sa.text(
                    "SELECT COUNT(*) FROM warehouse_capacity_forecast_plans "
                    "WHERE floor_id=:id"
                ),
                {"id": floor_id},
            )
            or 0
        ),
    }
    if any(dependent_counts.values()):
        raise RuntimeError(
            "四楼已建立区域、货位或容量计划，禁止破坏性降级"
        )

    connection.execute(
        sa.text("DELETE FROM warehouse_floors WHERE id=:id"),
        {"id": floor_id},
    )
