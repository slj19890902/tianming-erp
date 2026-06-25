"""v0.19.2-B: material price history + supplier price-adjustment batches

Revision ID: o13i5f6g9h02
Revises: n02h4e5f8g91
Create Date: 2026-06-25

新增两张表，用于供应商统一调价后保留历史价格、支持图表查看：
  material_price_adjustment_batches  一次「确认应用调价」一条批次
  material_price_history             每个被调价材质一条历史记录

仅新增表，不改动 materials / sales_orders / products / legacy_* 任何既有字段。
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "o13i5f6g9h02"
down_revision = "n02h4e5f8g91"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "material_price_adjustment_batches",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("supplier_name", sa.String(200), nullable=True),
        sa.Column("adjust_percent", sa.Numeric(8, 4), nullable=True),
        sa.Column("effective_date", sa.Date, nullable=True),
        sa.Column("affected_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("remark", sa.Text, nullable=True),
        sa.Column("operator", sa.String(100), nullable=True),
        sa.Column("backup_path", sa.Text, nullable=True),
        sa.Column("report_path", sa.Text, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime,
            server_default=sa.func.current_timestamp(),
            nullable=False,
        ),
    )
    op.create_table(
        "material_price_history",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "material_id",
            sa.Integer,
            sa.ForeignKey("materials.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("supplier_name", sa.String(200), nullable=True),
        sa.Column("material_code", sa.String(100), nullable=True),
        sa.Column("old_price", sa.Numeric(12, 4), nullable=True),
        sa.Column("new_price", sa.Numeric(12, 4), nullable=True),
        sa.Column("adjust_percent", sa.Numeric(8, 4), nullable=True),
        sa.Column("effective_date", sa.Date, nullable=True),
        sa.Column("adjust_reason", sa.Text, nullable=True),
        sa.Column("operator", sa.String(100), nullable=True),
        sa.Column(
            "batch_id",
            sa.Integer,
            sa.ForeignKey("material_price_adjustment_batches.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime,
            server_default=sa.func.current_timestamp(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_material_price_history_material_id",
        "material_price_history",
        ["material_id"],
    )
    op.create_index(
        "ix_material_price_history_batch_id",
        "material_price_history",
        ["batch_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_material_price_history_batch_id", "material_price_history")
    op.drop_index("ix_material_price_history_material_id", "material_price_history")
    op.drop_table("material_price_history")
    op.drop_table("material_price_adjustment_batches")
