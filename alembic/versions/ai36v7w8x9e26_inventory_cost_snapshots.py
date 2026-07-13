"""add inventory material-cost snapshots

Revision ID: ai36v7w8x9e26
Revises: af33v7w8x9b23
Create Date: 2026-07-13
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "ai36v7w8x9e26"
down_revision = "af33v7w8x9b23"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("inventory_lots") as batch_op:
        batch_op.add_column(
            sa.Column("estimated_unit_cost_snapshot", sa.Numeric(14, 4), nullable=True)
        )
        batch_op.add_column(
            sa.Column(
                "estimated_square_price_snapshot", sa.Numeric(12, 4), nullable=True
            )
        )
        batch_op.add_column(
            sa.Column(
                "estimated_cost_area_m2_snapshot", sa.Numeric(14, 6), nullable=True
            )
        )
        batch_op.add_column(
            sa.Column("cost_snapshot_source", sa.String(80), nullable=True)
        )
        batch_op.add_column(
            sa.Column("cost_snapshot_detail_json", sa.Text(), nullable=True)
        )
        batch_op.add_column(sa.Column("cost_snapshot_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("inventory_lots") as batch_op:
        batch_op.drop_column("cost_snapshot_at")
        batch_op.drop_column("cost_snapshot_detail_json")
        batch_op.drop_column("cost_snapshot_source")
        batch_op.drop_column("estimated_cost_area_m2_snapshot")
        batch_op.drop_column("estimated_square_price_snapshot")
        batch_op.drop_column("estimated_unit_cost_snapshot")
