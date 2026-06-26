"""v0.19.2-B Hotfix: order_item layer/flute/material_id/supplier/weight/drawing

Revision ID: q35k9m2n4p17
Revises: p24j6g7h0i13
Create Date: 2026-06-26

sales_order_items 新增 6 列：
  layer_count           INTEGER   NULL  — 常用箱层数快照
  flute_type            VARCHAR   NULL  — 常用箱实际楞型快照
  material_id           INTEGER   NULL  — 材质 FK（SET NULL）
  snapshot_supplier_name VARCHAR  NULL  — 供应商名快照
  snapshot_weight       VARCHAR   NULL  — 纯克重结构快照（150/130/130）
  drawing_file          VARCHAR   NULL  — 订单级图纸路径（不影响常用箱图纸）

均 nullable，历史数据全部保留 NULL，不批量回填。
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "q35k9m2n4p17"
down_revision = "p24j6g7h0i13"
branch_labels = None
depends_on = None

_TABLE = "sales_order_items"


def upgrade() -> None:
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.add_column(sa.Column("layer_count", sa.Integer, nullable=True))
        batch_op.add_column(sa.Column("flute_type", sa.String(20), nullable=True))
        batch_op.add_column(sa.Column("material_id", sa.Integer, nullable=True))
        batch_op.add_column(sa.Column("snapshot_supplier_name", sa.String(200), nullable=True))
        batch_op.add_column(sa.Column("snapshot_weight", sa.String(200), nullable=True))
        batch_op.add_column(sa.Column("drawing_file", sa.String(500), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table(_TABLE) as batch_op:
        for col in ("layer_count", "flute_type", "material_id",
                    "snapshot_supplier_name", "snapshot_weight", "drawing_file"):
            batch_op.drop_column(col)
