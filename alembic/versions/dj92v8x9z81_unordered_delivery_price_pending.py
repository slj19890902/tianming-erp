"""allow unordered finished delivery drafts to keep a pending price

Revision ID: dj92v8x9z81
Revises: di91v8x9z80
Create Date: 2026-08-04
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "dj92v8x9z81"
down_revision: Union[str, Sequence[str], None] = "di91v8x9z80"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _replace_delivery_constraints(
    *,
    allow_pending_price: bool,
    allow_unordered_pick: bool,
) -> None:
    price_clause = (
        "(unit_price_snapshot IS NULL OR unit_price_snapshot > 0)"
        if allow_pending_price
        else "unit_price_snapshot > 0"
    )
    with op.batch_alter_table("sales_delivery_items", recreate="always") as batch_op:
        batch_op.drop_constraint(
            "ck_sales_delivery_items_source_reference", type_="check"
        )
        batch_op.create_check_constraint(
            "ck_sales_delivery_items_source_reference",
            "(source_type = 'order' AND order_item_id IS NOT NULL "
            "AND product_id IS NULL AND unit_price_snapshot IS NULL) OR "
            "(source_type = 'unordered_finished' AND order_item_id IS NULL "
            "AND product_id IS NOT NULL AND product_code_snapshot IS NOT NULL "
            "AND product_name_snapshot IS NOT NULL AND unit_snapshot IS NOT NULL "
            f"AND {price_clause} AND price_source IS NOT NULL)",
        )
    with op.batch_alter_table("delivery_pick_task_items", recreate="always") as batch_op:
        batch_op.alter_column(
            "order_item_id",
            existing_type=sa.Integer(),
            nullable=allow_unordered_pick,
        )


def upgrade() -> None:
    _replace_delivery_constraints(
        allow_pending_price=True,
        allow_unordered_pick=True,
    )


def downgrade() -> None:
    connection = op.get_bind()
    pending_price_fact = connection.execute(
        sa.text(
            """
            SELECT id
            FROM sales_delivery_items
            WHERE source_type = 'unordered_finished'
              AND unit_price_snapshot IS NULL
            LIMIT 1
            """
        )
    ).first()
    if pending_price_fact is not None:
        raise RuntimeError(
            "存在单价待补的无订单成品送货事实，禁止降级；"
            "请恢复 dj92v8x9z81 升级前的完整备份"
        )
    unordered_pick_fact = connection.execute(
        sa.text(
            """
            SELECT id
            FROM delivery_pick_task_items
            WHERE order_item_id IS NULL
            LIMIT 1
            """
        )
    ).first()
    if unordered_pick_fact is not None:
        raise RuntimeError(
            "存在无订单成品库存手机拿货事实，禁止降级；"
            "请恢复 dj92v8x9z81 升级前的完整备份"
        )
    _replace_delivery_constraints(
        allow_pending_price=False,
        allow_unordered_pick=False,
    )
