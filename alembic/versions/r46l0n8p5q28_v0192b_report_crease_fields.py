"""v0.19.2-B: report/crease fields for products and sales_order_items

Revision ID: r46l0n8p5q28
Revises: q35k9m2n4p17
Create Date: 2026-06-26

products 新增 7 列：
  report_length_mm    INTEGER  NULL  — 报料长（mm）
  report_width_mm     INTEGER  NULL  — 报料宽（mm）
  crease_type         VARCHAR  NULL  — 压线类型：毛片 / 净料 / 压线
  crease_left_mm      INTEGER  NULL  — 第一段压线（mm）
  crease_middle_mm    INTEGER  NULL  — 中间压线（mm）
  crease_right_mm     INTEGER  NULL  — 第三段压线（mm）
  report_notes        TEXT     NULL  — 报料备注

sales_order_items 新增 7 列（快照，历史保留 NULL，不批量回填）：
  snapshot_report_length_mm   INTEGER  NULL
  snapshot_report_width_mm    INTEGER  NULL
  snapshot_crease_type        VARCHAR  NULL
  snapshot_crease_left_mm     INTEGER  NULL
  snapshot_crease_middle_mm   INTEGER  NULL
  snapshot_crease_right_mm    INTEGER  NULL
  snapshot_report_notes       TEXT     NULL
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "r46l0n8p5q28"
down_revision = "q35k9m2n4p17"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("products") as batch_op:
        batch_op.add_column(sa.Column("report_length_mm", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("report_width_mm", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("crease_type", sa.String(20), nullable=True))
        batch_op.add_column(sa.Column("crease_left_mm", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("crease_middle_mm", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("crease_right_mm", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("report_notes", sa.Text(), nullable=True))

    with op.batch_alter_table("sales_order_items") as batch_op:
        batch_op.add_column(sa.Column("snapshot_report_length_mm", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("snapshot_report_width_mm", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("snapshot_crease_type", sa.String(20), nullable=True))
        batch_op.add_column(sa.Column("snapshot_crease_left_mm", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("snapshot_crease_middle_mm", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("snapshot_crease_right_mm", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("snapshot_report_notes", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("sales_order_items") as batch_op:
        batch_op.drop_column("snapshot_report_notes")
        batch_op.drop_column("snapshot_crease_right_mm")
        batch_op.drop_column("snapshot_crease_middle_mm")
        batch_op.drop_column("snapshot_crease_left_mm")
        batch_op.drop_column("snapshot_crease_type")
        batch_op.drop_column("snapshot_report_width_mm")
        batch_op.drop_column("snapshot_report_length_mm")

    with op.batch_alter_table("products") as batch_op:
        batch_op.drop_column("report_notes")
        batch_op.drop_column("crease_right_mm")
        batch_op.drop_column("crease_middle_mm")
        batch_op.drop_column("crease_left_mm")
        batch_op.drop_column("crease_type")
        batch_op.drop_column("report_width_mm")
        batch_op.drop_column("report_length_mm")
