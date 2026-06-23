"""Phase 6 WMS receiver attribution.

Revision ID: a6e12f7b4c90
Revises: 91c8f28d2e65
Create Date: 2026-06-13
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a6e12f7b4c90"
down_revision: Union[str, Sequence[str], None] = "91c8f28d2e65"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {
        column["name"]
        for column in inspector.get_columns("sales_order_items")
    }
    if "material_received_by" not in columns:
        op.add_column(
            "sales_order_items",
            sa.Column("material_received_by", sa.Integer(), nullable=True),
        )

    indexes = {
        index["name"]
        for index in sa.inspect(bind).get_indexes("sales_order_items")
    }
    if "ix_sales_order_items_material_received_by" not in indexes:
        op.create_index(
            "ix_sales_order_items_material_received_by",
            "sales_order_items",
            ["material_received_by"],
            unique=False,
        )


def downgrade() -> None:
    # WMS receiving history is retained to avoid destructive rollback.
    return
