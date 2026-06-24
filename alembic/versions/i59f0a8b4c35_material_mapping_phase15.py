"""Phase 15: material_code_mapping_candidates + legacy_customer_material_code.

Revision ID: i59f0a8b4c35
Revises: h48d9f6c1e32
Create Date: 2026-06-23
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "i59f0a8b4c35"
down_revision: Union[str, Sequence[str], None] = "h48d9f6c1e32"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. 创建材质代码映射候选表
    op.create_table(
        "material_code_mapping_candidates",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("old_code", sa.String(100), nullable=False),
        sa.Column("new_code", sa.String(100), nullable=True),
        sa.Column("new_supplier", sa.String(100), nullable=True),
        sa.Column("old_supplier", sa.String(100), nullable=True),
        sa.Column("layer_count", sa.String(20), nullable=True),
        sa.Column("weight_structure", sa.String(500), nullable=True),
        sa.Column("new_price", sa.Numeric(12, 4), nullable=True),
        sa.Column("old_price", sa.Numeric(12, 4), nullable=True),
        sa.Column("confidence_label", sa.String(300), nullable=True),
        sa.Column("confidence_level", sa.String(20), nullable=False, server_default="低可信"),
        sa.Column("source_file", sa.String(500), nullable=True),
        sa.Column("source_row_number", sa.Integer, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime,
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("review_status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("review_note", sa.Text, nullable=True),
        sa.CheckConstraint(
            "confidence_level IN ('高可信', '中可信', '低可信')",
            name="ck_mat_mapping_confidence_level",
        ),
        sa.CheckConstraint(
            "review_status IN ('pending', 'approved', 'rejected')",
            name="ck_mat_mapping_review_status",
        ),
    )
    op.create_index(
        "ix_mat_mapping_old_code", "material_code_mapping_candidates", ["old_code"]
    )
    op.create_index(
        "ix_mat_mapping_confidence_level",
        "material_code_mapping_candidates",
        ["confidence_level"],
    )
    op.create_index(
        "ix_mat_mapping_review_status",
        "material_code_mapping_candidates",
        ["review_status"],
    )

    # 2. 在 products 表增加 legacy_customer_material_code（保留旧客户料号）
    with op.batch_alter_table("products") as batch:
        batch.add_column(
            sa.Column("legacy_customer_material_code", sa.String(150), nullable=True)
        )


def downgrade() -> None:
    op.drop_table("material_code_mapping_candidates")
    with op.batch_alter_table("products") as batch:
        batch.drop_column("legacy_customer_material_code")
