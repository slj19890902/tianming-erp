"""link incoming receipt facts to stock replenishment sources

Revision ID: ct76v8x9z65
Revises: cr74v8x9z63
Create Date: 2026-07-27
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "ct76v8x9z65"
down_revision: Union[str, Sequence[str], None] = "cr74v8x9z63"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


DOWNGRADE_BLOCKED_MESSAGE = (
    "补库来源来料收货事实或关联库存批次已存在，禁止破坏性降级；"
    "请保留当前数据库并恢复迁移前完整备份。"
)


def upgrade() -> None:
    with op.batch_alter_table("incoming_receipt_items") as batch_op:
        batch_op.add_column(
            sa.Column("stock_replenishment_item_id", sa.Integer(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("received_inventory_lot_id", sa.Integer(), nullable=True)
        )
        batch_op.alter_column(
            "order_id",
            existing_type=sa.Integer(),
            existing_nullable=False,
            nullable=True,
        )
        batch_op.alter_column(
            "order_item_id",
            existing_type=sa.Integer(),
            existing_nullable=False,
            nullable=True,
        )
        batch_op.create_foreign_key(
            "fk_incoming_receipt_items_stock_replenishment_item",
            "stock_replenishment_order_items",
            ["stock_replenishment_item_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.create_foreign_key(
            "fk_incoming_receipt_items_received_inventory_lot",
            "inventory_lots",
            ["received_inventory_lot_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_check_constraint(
            "ck_incoming_receipt_items_exactly_one_source",
            "(order_id IS NOT NULL AND order_item_id IS NOT NULL "
            "AND stock_replenishment_item_id IS NULL) "
            "OR (order_id IS NULL AND order_item_id IS NULL "
            "AND requisition_id IS NULL AND requisition_item_id IS NULL "
            "AND supplier_order_id IS NULL AND supplier_order_item_id IS NULL "
            "AND stock_replenishment_item_id IS NOT NULL)",
        )
        batch_op.create_index(
            "ix_incoming_receipt_items_stock_replenishment_item",
            ["stock_replenishment_item_id", "status"],
        )
        batch_op.create_index(
            "ix_incoming_receipt_items_received_inventory_lot",
            ["received_inventory_lot_id"],
        )


def downgrade() -> None:
    connection = op.get_bind()
    linked_fact_count = int(
        connection.execute(
            sa.text(
                """
                SELECT COUNT(*)
                FROM incoming_receipt_items
                WHERE stock_replenishment_item_id IS NOT NULL
                   OR received_inventory_lot_id IS NOT NULL
                """
            )
        ).scalar()
        or 0
    )
    if linked_fact_count:
        raise RuntimeError(DOWNGRADE_BLOCKED_MESSAGE)
    linked_inventory_count = int(
        connection.execute(
            sa.text(
                """
                SELECT COUNT(*)
                FROM inventory_lots
                WHERE source_ref_type = 'stock_replenishment_receipt'
                """
            )
        ).scalar()
        or 0
    )
    if linked_inventory_count:
        raise RuntimeError(DOWNGRADE_BLOCKED_MESSAGE)

    with op.batch_alter_table("incoming_receipt_items") as batch_op:
        batch_op.drop_index(
            "ix_incoming_receipt_items_received_inventory_lot"
        )
        batch_op.drop_index(
            "ix_incoming_receipt_items_stock_replenishment_item"
        )
        batch_op.drop_constraint(
            "ck_incoming_receipt_items_exactly_one_source", type_="check"
        )
        batch_op.drop_constraint(
            "fk_incoming_receipt_items_received_inventory_lot",
            type_="foreignkey",
        )
        batch_op.drop_constraint(
            "fk_incoming_receipt_items_stock_replenishment_item",
            type_="foreignkey",
        )
        batch_op.alter_column(
            "order_item_id",
            existing_type=sa.Integer(),
            existing_nullable=True,
            nullable=False,
        )
        batch_op.alter_column(
            "order_id",
            existing_type=sa.Integer(),
            existing_nullable=True,
            nullable=False,
        )
        batch_op.drop_column("received_inventory_lot_id")
        batch_op.drop_column("stock_replenishment_item_id")
