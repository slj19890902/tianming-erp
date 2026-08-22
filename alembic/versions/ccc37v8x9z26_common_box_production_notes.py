"""add independent common-box and BOM production notes

Revision ID: ccc37v8x9z26
Revises: bbb36v8x9z25
Create Date: 2026-08-22
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "ccc37v8x9z26"
down_revision = "bbb36v8x9z25"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "products",
        sa.Column("production_notes", sa.Text(), nullable=True),
    )
    op.add_column(
        "sales_order_item_bom_components",
        sa.Column("snapshot_component_production_notes", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    connection = op.get_bind()
    product_facts = int(
        connection.execute(
            sa.text(
                "SELECT COUNT(*) FROM products "
                "WHERE length(trim(coalesce(production_notes, ''))) > 0"
            )
        ).scalar_one()
        or 0
    )
    component_facts = int(
        connection.execute(
            sa.text(
                "SELECT COUNT(*) FROM sales_order_item_bom_components "
                "WHERE length(trim(coalesce(snapshot_component_production_notes, ''))) > 0"
            )
        ).scalar_one()
        or 0
    )
    if product_facts or component_facts:
        raise RuntimeError("拒绝破坏性降级：已有常用箱或订单组件生产备注事实")
    op.drop_column(
        "sales_order_item_bom_components",
        "snapshot_component_production_notes",
    )
    op.drop_column("products", "production_notes")
