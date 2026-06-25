"""v0.19.2-A: add sales_order_items.snapshot_production_notes

Revision ID: n02h4e5f8g91
Revises: m91g3c4d7e74
Create Date: 2026-06-25

新增：
  sales_order_items.snapshot_production_notes  TEXT  NULL
  (行级生产/印刷/打勾/摆放/日文警示等说明，历史订单默认 NULL，不回填)
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "n02h4e5f8g91"
down_revision = "m91g3c4d7e74"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("sales_order_items") as batch_op:
        batch_op.add_column(
            sa.Column("snapshot_production_notes", sa.Text, nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("sales_order_items") as batch_op:
        batch_op.drop_column("snapshot_production_notes")
