"""v0.19.2-B: supplier flute price rules

Revision ID: p24j6g7h0i13
Revises: o13i5f6g9h02
Create Date: 2026-06-26

新增一张表 supplier_flute_price_rules，维护各供应商按层数/楞型的加价规则：
  最终材料平方价 = 基础平方报价 + price_delta。
并写入初始规则（三层 A 瓦加价、B/E +0、五层 AB/BE +0）。

仅新增表 + 种子数据，不改动 materials / sales_orders / products / legacy_* 任何既有字段。
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "p24j6g7h0i13"
down_revision = "o13i5f6g9h02"
branch_labels = None
depends_on = None


# 初始规则：三家供应商三层 A/B/E + 五层 AB/BE。
SEED_RULES = [
    ("苏州嘉林亿", 3, "A", "0.04"),
    ("苏州嘉林亿", 3, "B", "0"),
    ("苏州嘉林亿", 3, "E", "0"),
    ("昆山鸣朋", 3, "A", "0.05"),
    ("昆山鸣朋", 3, "B", "0"),
    ("昆山鸣朋", 3, "E", "0"),
    ("苏州佳丰", 3, "A", "0.05"),
    ("苏州佳丰", 3, "B", "0"),
    ("苏州佳丰", 3, "E", "0"),
    ("苏州嘉林亿", 5, "AB", "0"),
    ("苏州嘉林亿", 5, "BE", "0"),
    ("昆山鸣朋", 5, "AB", "0"),
    ("昆山鸣朋", 5, "BE", "0"),
    ("苏州佳丰", 5, "AB", "0"),
    ("苏州佳丰", 5, "BE", "0"),
]


def upgrade() -> None:
    table = op.create_table(
        "supplier_flute_price_rules",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("supplier_name", sa.String(200), nullable=False),
        sa.Column("layer_count", sa.Integer, nullable=False),
        sa.Column("flute_type", sa.String(20), nullable=False),
        sa.Column(
            "price_delta", sa.Numeric(12, 4), nullable=False, server_default="0"
        ),
        sa.Column("effective_date", sa.Date, nullable=True),
        sa.Column("remark", sa.Text, nullable=True),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default="1"),
        sa.Column(
            "created_at",
            sa.DateTime,
            server_default=sa.func.current_timestamp(),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime, nullable=True),
    )
    op.create_index(
        "ix_sfpr_supplier_layer_flute",
        "supplier_flute_price_rules",
        ["supplier_name", "layer_count", "flute_type"],
    )
    op.bulk_insert(
        table,
        [
            {
                "supplier_name": s,
                "layer_count": lc,
                "flute_type": ft,
                "price_delta": delta,
                "is_active": True,
            }
            for (s, lc, ft, delta) in SEED_RULES
        ],
    )


def downgrade() -> None:
    op.drop_index("ix_sfpr_supplier_layer_flute", "supplier_flute_price_rules")
    op.drop_table("supplier_flute_price_rules")
