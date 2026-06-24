"""v0.19.1: add sales_order_items.snapshot_customer_model

Revision ID: m91g3c4d7e74
Revises: l82f1b7d9a63
Create Date: 2026-06-24

新增：
  sales_order_items.snapshot_customer_model  TEXT  NULL
  (天华 TH型号 / 其他客户型号，历史订单默认 NULL，不回填)
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "m91g3c4d7e74"
down_revision = "l82f1b7d9a63"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("sales_order_items") as batch_op:
        batch_op.add_column(
            sa.Column("snapshot_customer_model", sa.Text, nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("sales_order_items") as batch_op:
        batch_op.drop_column("snapshot_customer_model")
