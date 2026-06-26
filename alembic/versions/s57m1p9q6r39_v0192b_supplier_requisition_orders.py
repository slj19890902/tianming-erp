"""v0.19.2-B: supplier_requisition_orders + supplier_requisition_order_items

Revision ID: s57m1p9q6r39
Revises: r46l0n8p5q28
Create Date: 2026-06-26
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "s57m1p9q6r39"
down_revision = "r46l0n8p5q28"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "supplier_requisition_orders",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("order_number", sa.String(40), nullable=False, unique=True),
        sa.Column("supplier_name", sa.String(100), nullable=True),
        sa.Column("material_id", sa.Integer, sa.ForeignKey("materials.id"), nullable=True),
        sa.Column("layer_count", sa.Integer, nullable=True),
        sa.Column("flute_type", sa.String(20), nullable=True),
        # 报料尺寸（宽×长输出格式）
        sa.Column("report_length_mm", sa.Integer, nullable=True),
        sa.Column("report_width_mm", sa.Integer, nullable=True),
        # 压线
        sa.Column("crease_type", sa.String(20), nullable=True),
        sa.Column("crease_left_mm", sa.Integer, nullable=True),
        sa.Column("crease_middle_mm", sa.Integer, nullable=True),
        sa.Column("crease_right_mm", sa.Integer, nullable=True),
        # 汇总
        sa.Column("total_quantity", sa.Integer, nullable=False, default=0),
        sa.Column("stock_deduction_qty", sa.Integer, nullable=False, default=0),
        sa.Column("requisition_qty", sa.Integer, nullable=False, default=0),
        sa.Column("remark", sa.Text, nullable=True),
        # 状态：draft / confirmed / voided
        sa.Column("status", sa.String(20), nullable=False, default="confirmed"),
        sa.Column("created_by", sa.Integer, sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=True, server_default=sa.func.now()),
        sa.Column("voided_at", sa.DateTime, nullable=True),
    )

    op.create_table(
        "supplier_requisition_order_items",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("supplier_order_id", sa.Integer,
                  sa.ForeignKey("supplier_requisition_orders.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("order_item_id", sa.Integer,
                  sa.ForeignKey("sales_order_items.id", ondelete="SET NULL"),
                  nullable=True),
        sa.Column("order_number", sa.String(40), nullable=True),
        sa.Column("product_code", sa.String(100), nullable=True),
        sa.Column("product_name", sa.String(200), nullable=True),
        sa.Column("quantity", sa.Integer, nullable=False),
        sa.Column("stock_deduction_qty", sa.Integer, nullable=False, default=0),
        sa.Column("requisition_qty", sa.Integer, nullable=False, default=0),
        sa.Column("customer_name", sa.String(100), nullable=True),
        sa.Column("delivery_date", sa.Date, nullable=True),
    )


def downgrade() -> None:
    op.drop_table("supplier_requisition_order_items")
    op.drop_table("supplier_requisition_orders")
