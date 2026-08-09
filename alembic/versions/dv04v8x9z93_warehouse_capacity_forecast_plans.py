"""add operator-confirmed warehouse capacity forecast plans

Revision ID: dv04v8x9z93
Revises: du03v8x9z92
Create Date: 2026-08-09
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "dv04v8x9z93"
down_revision: Union[str, Sequence[str], None] = "du03v8x9z92"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "warehouse_capacity_forecast_plans",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("source_type", sa.String(length=30), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("source_number_snapshot", sa.String(length=100), nullable=False),
        sa.Column("source_label_snapshot", sa.String(length=250), nullable=False),
        sa.Column("effect", sa.String(length=20), nullable=False),
        sa.Column("floor_id", sa.Integer(), nullable=True),
        sa.Column("scope_key", sa.String(length=50), nullable=False),
        sa.Column("planned_date", sa.Date(), nullable=False),
        sa.Column("pallet_slots", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="active", nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("last_operation_key", sa.String(length=64), nullable=False),
        sa.Column("last_request_hash", sa.String(length=64), nullable=False),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("updated_by", sa.Integer(), nullable=True),
        sa.Column("cancelled_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "source_type IN ('supplier_requisition','production_task','delivery')",
            name="ck_capacity_forecast_plans_source_type",
        ),
        sa.CheckConstraint(
            "effect IN ('inflow','outflow','no_storage')",
            name="ck_capacity_forecast_plans_effect",
        ),
        sa.CheckConstraint(
            "status IN ('active','cancelled')",
            name="ck_capacity_forecast_plans_status",
        ),
        sa.CheckConstraint("source_id > 0", name="ck_capacity_forecast_plans_source_id"),
        sa.CheckConstraint("version >= 1", name="ck_capacity_forecast_plans_version"),
        sa.CheckConstraint(
            "(effect = 'no_storage' AND floor_id IS NULL AND pallet_slots = 0 AND scope_key = 'none') OR "
            "(effect IN ('inflow','outflow') AND floor_id IS NOT NULL AND pallet_slots > 0 AND scope_key <> 'none')",
            name="ck_capacity_forecast_plans_effect_consistency",
        ),
        sa.CheckConstraint(
            "(status = 'active' AND cancelled_by IS NULL AND cancelled_at IS NULL) OR "
            "(status = 'cancelled' AND cancelled_at IS NOT NULL)",
            name="ck_capacity_forecast_plans_cancel_consistency",
        ),
        sa.ForeignKeyConstraint(["floor_id"], ["warehouse_floors.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["cancelled_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_type", "source_id", "scope_key",
            name="uq_capacity_forecast_plans_source_scope",
        ),
    )
    op.create_index(
        "ix_capacity_forecast_plans_date_status",
        "warehouse_capacity_forecast_plans",
        ["planned_date", "status"],
    )
    op.create_index(
        "ix_capacity_forecast_plans_floor_date",
        "warehouse_capacity_forecast_plans",
        ["floor_id", "planned_date"],
    )


def downgrade() -> None:
    connection = op.get_bind()
    fact_count = connection.execute(
        sa.text("SELECT COUNT(*) FROM warehouse_capacity_forecast_plans")
    ).scalar_one()
    if int(fact_count or 0):
        raise RuntimeError(
            "已有仓储容量预测计划事实，禁止破坏性降级；请恢复 dv04v8x9z93 升级前完整备份。"
        )
    op.drop_index(
        "ix_capacity_forecast_plans_floor_date",
        table_name="warehouse_capacity_forecast_plans",
    )
    op.drop_index(
        "ix_capacity_forecast_plans_date_status",
        table_name="warehouse_capacity_forecast_plans",
    )
    op.drop_table("warehouse_capacity_forecast_plans")
