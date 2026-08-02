"""allow one delivery draft to contain order and unordered finished lines

Revision ID: di91v8x9z80
Revises: dh90v8x9z79
Create Date: 2026-08-02
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "di91v8x9z80"
down_revision: Union[str, Sequence[str], None] = "dh90v8x9z79"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("sales_deliveries", recreate="always") as batch_op:
        batch_op.drop_constraint("ck_sales_deliveries_source_mode", type_="check")
        batch_op.create_check_constraint(
            "ck_sales_deliveries_source_mode",
            "source_mode IN ('order', 'unordered_finished', 'mixed')",
        )


def downgrade() -> None:
    connection = op.get_bind()
    mixed_fact = connection.execute(
        sa.text(
            """
            SELECT id
            FROM sales_deliveries
            WHERE source_mode = 'mixed'
            LIMIT 1
            """
        )
    ).first()
    if mixed_fact is not None:
        raise RuntimeError(
            "存在订单待送与无订单成品库存混合送货事实，禁止降级；"
            "请恢复 di91v8x9z80 升级前的完整备份"
        )
    with op.batch_alter_table("sales_deliveries", recreate="always") as batch_op:
        batch_op.drop_constraint("ck_sales_deliveries_source_mode", type_="check")
        batch_op.create_check_constraint(
            "ck_sales_deliveries_source_mode",
            "source_mode IN ('order', 'unordered_finished')",
        )
