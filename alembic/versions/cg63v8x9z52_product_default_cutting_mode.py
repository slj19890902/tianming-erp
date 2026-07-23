"""add product default cutting mode

Revision ID: cg63v8x9z52
Revises: cf62v8x9z51
Create Date: 2026-07-23
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "cg63v8x9z52"
down_revision: Union[str, Sequence[str], None] = "cf62v8x9z51"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("products") as batch:
        batch.add_column(
            sa.Column(
                "default_cutting_mode",
                sa.String(length=20),
                server_default="一开一",
                nullable=False,
            )
        )
        batch.create_check_constraint(
            "ck_products_default_cutting_mode",
            "default_cutting_mode IN ('一开一', '一开二', '一开三', '一开四', '一开五')",
        )


def downgrade() -> None:
    connection = op.get_bind()
    non_default_count = int(
        connection.execute(
            sa.text("SELECT COUNT(*) FROM products WHERE default_cutting_mode <> '一开一'")
        ).scalar_one()
    )
    if non_default_count:
        raise RuntimeError(
            "已有常用箱使用非默认开料方式，禁止破坏性降级；请恢复升级前完整备份。"
        )
    with op.batch_alter_table("products") as batch:
        batch.drop_constraint("ck_products_default_cutting_mode", type_="check")
        batch.drop_column("default_cutting_mode")
