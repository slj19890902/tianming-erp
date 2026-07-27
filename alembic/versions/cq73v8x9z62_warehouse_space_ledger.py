"""add warehouse floor and area progress ledgers

Revision ID: cq73v8x9z62
Revises: cs75v8x9z64
Create Date: 2026-07-26
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "cq73v8x9z62"
down_revision: Union[str, Sequence[str], None] = "cs75v8x9z64"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

CONSTRUCTION_STATUS_SQL = (
    "construction_status IN "
    "('not_started','ledger_building','ledger_complete',"
    "'layout_building','layout_complete','enabled')"
)


def upgrade() -> None:
    op.create_table(
        "warehouse_floors",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("floor_code", sa.String(length=30), nullable=False),
        sa.Column("floor_name", sa.String(length=100), nullable=False),
        sa.Column("floor_number", sa.Integer(), nullable=False),
        sa.Column(
            "construction_status",
            sa.String(length=30),
            nullable=False,
            server_default="not_started",
        ),
        sa.Column("remarks", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "floor_number >= 1 AND floor_number <= 99",
            name="ck_warehouse_floors_number",
        ),
        sa.CheckConstraint(
            CONSTRUCTION_STATUS_SQL,
            name="ck_warehouse_floors_construction_status",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("floor_code", name="uq_warehouse_floors_code"),
        sa.UniqueConstraint("floor_number", name="uq_warehouse_floors_number"),
    )
    op.create_table(
        "warehouse_areas",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("floor_id", sa.Integer(), nullable=False),
        sa.Column("area_code", sa.String(length=30), nullable=False),
        sa.Column("area_name", sa.String(length=100), nullable=False),
        sa.Column(
            "planned_location_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "planned_pallet_capacity",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "construction_status",
            sa.String(length=30),
            nullable=False,
            server_default="ledger_building",
        ),
        sa.Column("remarks", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "planned_location_count >= 0",
            name="ck_warehouse_areas_planned_location_count",
        ),
        sa.CheckConstraint(
            "planned_pallet_capacity >= 0",
            name="ck_warehouse_areas_planned_pallet_capacity",
        ),
        sa.CheckConstraint(
            CONSTRUCTION_STATUS_SQL,
            name="ck_warehouse_areas_construction_status",
        ),
        sa.ForeignKeyConstraint(
            ["floor_id"], ["warehouse_floors.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "floor_id", "area_code", name="uq_warehouse_areas_floor_code"
        ),
    )
    op.create_index(
        "ix_warehouse_areas_floor_id",
        "warehouse_areas",
        ["floor_id"],
        unique=False,
    )


def downgrade() -> None:
    connection = op.get_bind()
    floor_count = int(
        connection.execute(sa.text("SELECT COUNT(*) FROM warehouse_floors")).scalar()
        or 0
    )
    area_count = int(
        connection.execute(sa.text("SELECT COUNT(*) FROM warehouse_areas")).scalar()
        or 0
    )
    if floor_count or area_count:
        raise RuntimeError(
            "warehouse space ledger contains floor or area facts; "
            "refusing destructive downgrade"
        )
    op.drop_index("ix_warehouse_areas_floor_id", table_name="warehouse_areas")
    op.drop_table("warehouse_areas")
    op.drop_table("warehouse_floors")
