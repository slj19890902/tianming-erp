"""add warehouse planning references and field-reviewed capacity

Revision ID: du03v8x9z92
Revises: dt02v8x9z91
Create Date: 2026-08-09
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "du03v8x9z92"
down_revision: Union[str, Sequence[str], None] = "dt02v8x9z91"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("warehouse_floors", recreate="always") as batch:
        batch.add_column(
            sa.Column(
                "planning_reference_pallet_capacity",
                sa.Integer(),
                server_default="0",
                nullable=False,
            )
        )
        batch.create_check_constraint(
            "ck_warehouse_floors_planning_reference_capacity",
            "planning_reference_pallet_capacity >= 0",
        )

    connection = op.get_bind()
    connection.execute(
        sa.text(
            "UPDATE warehouse_floors SET planning_reference_pallet_capacity = "
            "CASE floor_number WHEN 1 THEN 34 WHEN 3 THEN 250 ELSE 0 END"
        )
    )

    with op.batch_alter_table("warehouse_areas", recreate="always") as batch:
        batch.add_column(
            sa.Column(
                "capacity_review_status",
                sa.String(length=20),
                server_default="pending",
                nullable=False,
            )
        )
        batch.add_column(
            sa.Column(
                "capacity_eligible",
                sa.Boolean(),
                server_default=sa.false(),
                nullable=False,
            )
        )
        batch.add_column(sa.Column("confirmed_pallet_capacity", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("capacity_reviewed_by", sa.String(length=100), nullable=True))
        batch.add_column(sa.Column("capacity_reviewed_at", sa.DateTime(), nullable=True))
        batch.create_check_constraint(
            "ck_warehouse_areas_capacity_review_status",
            "capacity_review_status IN ('pending','confirmed','excluded')",
        )
        batch.create_check_constraint(
            "ck_warehouse_areas_confirmed_pallet_capacity",
            "confirmed_pallet_capacity IS NULL OR confirmed_pallet_capacity > 0",
        )
        batch.create_check_constraint(
            "ck_warehouse_areas_capacity_review_consistency",
            "(capacity_review_status = 'pending' AND capacity_eligible = 0 "
            "AND confirmed_pallet_capacity IS NULL "
            "AND capacity_reviewed_by IS NULL AND capacity_reviewed_at IS NULL) OR "
            "(capacity_review_status = 'confirmed' AND capacity_eligible = 1 "
            "AND confirmed_pallet_capacity IS NOT NULL "
            "AND capacity_reviewed_by IS NOT NULL AND capacity_reviewed_at IS NOT NULL) OR "
            "(capacity_review_status = 'excluded' AND capacity_eligible = 0 "
            "AND confirmed_pallet_capacity IS NULL "
            "AND capacity_reviewed_by IS NOT NULL AND capacity_reviewed_at IS NOT NULL)",
        )


def downgrade() -> None:
    connection = op.get_bind()
    reviewed_facts = connection.execute(
        sa.text(
            "SELECT COUNT(*) FROM warehouse_areas WHERE "
            "capacity_review_status <> 'pending' OR capacity_eligible = 1 OR "
            "confirmed_pallet_capacity IS NOT NULL OR capacity_reviewed_by IS NOT NULL OR "
            "capacity_reviewed_at IS NOT NULL"
        )
    ).scalar_one()
    customized_plans = connection.execute(
        sa.text(
            "SELECT COUNT(*) FROM warehouse_floors WHERE "
            "planning_reference_pallet_capacity <> "
            "CASE floor_number WHEN 1 THEN 34 WHEN 3 THEN 250 ELSE 0 END"
        )
    ).scalar_one()
    if int(reviewed_facts or 0) or int(customized_plans or 0):
        raise RuntimeError(
            "已有现场容量复核事实或已修改规划容量，禁止破坏性降级；"
            "请恢复 du03v8x9z92 升级前完整备份。"
        )

    with op.batch_alter_table("warehouse_areas", recreate="always") as batch:
        batch.drop_constraint(
            "ck_warehouse_areas_capacity_review_consistency", type_="check"
        )
        batch.drop_constraint(
            "ck_warehouse_areas_confirmed_pallet_capacity", type_="check"
        )
        batch.drop_constraint(
            "ck_warehouse_areas_capacity_review_status", type_="check"
        )
        batch.drop_column("capacity_reviewed_at")
        batch.drop_column("capacity_reviewed_by")
        batch.drop_column("confirmed_pallet_capacity")
        batch.drop_column("capacity_eligible")
        batch.drop_column("capacity_review_status")

    with op.batch_alter_table("warehouse_floors", recreate="always") as batch:
        batch.drop_constraint(
            "ck_warehouse_floors_planning_reference_capacity", type_="check"
        )
        batch.drop_column("planning_reference_pallet_capacity")
