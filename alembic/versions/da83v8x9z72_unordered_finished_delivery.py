"""controlled delivery from customer-specific unordered finished goods

Revision ID: da83v8x9z72
Revises: cz82v8x9z71
Create Date: 2026-07-30
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "da83v8x9z72"
down_revision: Union[str, Sequence[str], None] = "cz82v8x9z71"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("sales_deliveries", recreate="always") as batch_op:
        batch_op.drop_constraint("ck_sales_deliveries_status", type_="check")
        batch_op.add_column(
            sa.Column(
                "source_mode",
                sa.String(length=30),
                nullable=False,
                server_default="order",
            )
        )
        batch_op.create_check_constraint(
            "ck_sales_deliveries_status",
            "status IN ('pending', 'dispatched', 'voided')",
        )
        batch_op.create_check_constraint(
            "ck_sales_deliveries_source_mode",
            "source_mode IN ('order', 'unordered_finished')",
        )

    with op.batch_alter_table("sales_delivery_items", recreate="always") as batch_op:
        batch_op.drop_constraint(
            "uq_sales_delivery_items_order_item", type_="unique"
        )
        batch_op.alter_column(
            "order_item_id",
            existing_type=sa.Integer(),
            nullable=True,
        )
        batch_op.add_column(
            sa.Column(
                "source_type",
                sa.String(length=30),
                nullable=False,
                server_default="order",
            )
        )
        batch_op.add_column(sa.Column("product_id", sa.Integer(), nullable=True))
        batch_op.add_column(
            sa.Column("product_code_snapshot", sa.String(length=150), nullable=True)
        )
        batch_op.add_column(
            sa.Column("product_name_snapshot", sa.String(length=250), nullable=True)
        )
        batch_op.add_column(
            sa.Column("specification_snapshot", sa.String(length=255), nullable=True)
        )
        batch_op.add_column(sa.Column("unit_snapshot", sa.String(length=20), nullable=True))
        batch_op.add_column(
            sa.Column("unit_price_snapshot", sa.Numeric(precision=12, scale=4), nullable=True)
        )
        batch_op.add_column(sa.Column("price_source", sa.String(length=50), nullable=True))
        batch_op.create_foreign_key(
            "fk_sales_delivery_items_product_id_products",
            "products",
            ["product_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.create_unique_constraint(
            "uq_sales_delivery_items_order_item", ["delivery_id", "order_item_id"]
        )
        batch_op.create_check_constraint(
            "ck_sales_delivery_items_source_type",
            "source_type IN ('order', 'unordered_finished')",
        )
        batch_op.create_check_constraint(
            "ck_sales_delivery_items_source_reference",
            "(source_type = 'order' AND order_item_id IS NOT NULL AND product_id IS NULL "
            "AND unit_price_snapshot IS NULL) OR "
            "(source_type = 'unordered_finished' AND order_item_id IS NULL "
            "AND product_id IS NOT NULL AND product_code_snapshot IS NOT NULL "
            "AND product_name_snapshot IS NOT NULL AND unit_snapshot IS NOT NULL "
            "AND unit_price_snapshot > 0 AND price_source IS NOT NULL)",
        )
        batch_op.create_index("ix_sales_delivery_items_product_id", ["product_id"])

    op.create_table(
        "unordered_finished_delivery_allocations",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("delivery_item_id", sa.Integer(), nullable=False),
        sa.Column("inventory_lot_id", sa.Integer(), nullable=False),
        sa.Column("planned_quantity", sa.Integer(), nullable=False),
        sa.Column("consumed_quantity", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("restored_quantity", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "status",
            sa.String(length=24),
            nullable=False,
            server_default="planned",
        ),
        sa.Column("lot_number_snapshot", sa.String(length=50), nullable=True),
        sa.Column("warehouse_location_id_snapshot", sa.Integer(), nullable=True),
        sa.Column("warehouse_location_code_snapshot", sa.String(length=100), nullable=True),
        sa.Column("pallet_code_snapshot", sa.String(length=100), nullable=True),
        sa.Column("consume_movement_id", sa.Integer(), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("dispatched_by", sa.Integer(), nullable=True),
        sa.Column("dispatched_at", sa.DateTime(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "planned_quantity > 0",
            name="ck_unordered_finished_delivery_allocations_planned_quantity",
        ),
        sa.CheckConstraint(
            "consumed_quantity >= 0 AND consumed_quantity <= planned_quantity "
            "AND restored_quantity >= 0 AND restored_quantity <= consumed_quantity",
            name="ck_unordered_finished_delivery_allocations_quantity_progress",
        ),
        sa.CheckConstraint(
            "status IN ('planned', 'dispatched', 'partial_restored', 'restored')",
            name="ck_unordered_finished_delivery_allocations_status",
        ),
        sa.ForeignKeyConstraint(
            ["delivery_item_id"], ["sales_delivery_items.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["inventory_lot_id"], ["inventory_lots.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["consume_movement_id"], ["inventory_movements.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["dispatched_by"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint(
            "delivery_item_id",
            "inventory_lot_id",
            name="uq_unordered_finished_delivery_allocations_item_lot",
        ),
        sa.UniqueConstraint(
            "consume_movement_id",
            name="uq_unordered_finished_delivery_allocations_consume_movement",
        ),
    )
    op.create_index(
        "ix_unordered_finished_delivery_allocations_item_status",
        "unordered_finished_delivery_allocations",
        ["delivery_item_id", "status"],
    )
    op.create_index(
        "ix_unordered_finished_delivery_allocations_lot_status",
        "unordered_finished_delivery_allocations",
        ["inventory_lot_id", "status"],
    )

    op.create_table(
        "unordered_finished_delivery_reversals",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("allocation_id", sa.Integer(), nullable=False),
        sa.Column("return_receipt_item_id", sa.Integer(), nullable=True),
        sa.Column("inventory_movement_id", sa.Integer(), nullable=False),
        sa.Column("reversal_kind", sa.String(length=30), nullable=False),
        sa.Column("reversal_quantity", sa.Integer(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default="active",
        ),
        sa.Column("reconsumed_movement_id", sa.Integer(), nullable=True),
        sa.Column("reconsumed_by", sa.Integer(), nullable=True),
        sa.Column("reconsumed_at", sa.DateTime(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=100), nullable=False),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "reversal_kind IN ('dispatch_cancel', 'receipt_short_return', "
            "'receipt_refusal_return')",
            name="ck_unordered_finished_delivery_reversals_kind",
        ),
        sa.CheckConstraint(
            "reversal_quantity > 0",
            name="ck_unordered_finished_delivery_reversals_quantity",
        ),
        sa.CheckConstraint(
            "(status = 'active' AND reconsumed_movement_id IS NULL "
            "AND reconsumed_at IS NULL) OR "
            "(status = 'reconsumed' AND reconsumed_movement_id IS NOT NULL "
            "AND reconsumed_at IS NOT NULL)",
            name="ck_unordered_finished_delivery_reversals_reconsumed",
        ),
        sa.ForeignKeyConstraint(
            ["allocation_id"],
            ["unordered_finished_delivery_allocations.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["return_receipt_item_id"],
            ["finance_return_receipt_items.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["inventory_movement_id"], ["inventory_movements.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["reconsumed_movement_id"], ["inventory_movements.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["reconsumed_by"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint(
            "inventory_movement_id",
            name="uq_unordered_finished_delivery_reversals_movement",
        ),
        sa.UniqueConstraint(
            "reconsumed_movement_id",
            name="uq_unordered_finished_delivery_reversals_reconsumed_movement",
        ),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_unordered_finished_delivery_reversals_idempotency",
        ),
    )
    op.create_index(
        "ix_unordered_finished_delivery_reversals_allocation",
        "unordered_finished_delivery_reversals",
        ["allocation_id"],
    )
    op.create_index(
        "ix_unordered_finished_delivery_reversals_receipt_item",
        "unordered_finished_delivery_reversals",
        ["return_receipt_item_id"],
    )


def downgrade() -> None:
    connection = op.get_bind()
    unordered_facts = connection.execute(
        sa.text(
            """
            SELECT 1
            FROM sales_deliveries
            WHERE source_mode = 'unordered_finished'
            UNION ALL
            SELECT 1
            FROM sales_delivery_items
            WHERE source_type = 'unordered_finished'
            UNION ALL
            SELECT 1
            FROM unordered_finished_delivery_allocations
            UNION ALL
            SELECT 1
            FROM unordered_finished_delivery_reversals
            LIMIT 1
            """
        )
    ).first()
    if unordered_facts is not None:
        raise RuntimeError(
            "存在无订单成品送货事实，禁止破坏性降级；请恢复迁移前数据库备份"
        )

    op.drop_index(
        "ix_unordered_finished_delivery_reversals_receipt_item",
        table_name="unordered_finished_delivery_reversals",
    )
    op.drop_index(
        "ix_unordered_finished_delivery_reversals_allocation",
        table_name="unordered_finished_delivery_reversals",
    )
    op.drop_table("unordered_finished_delivery_reversals")
    op.drop_index(
        "ix_unordered_finished_delivery_allocations_lot_status",
        table_name="unordered_finished_delivery_allocations",
    )
    op.drop_index(
        "ix_unordered_finished_delivery_allocations_item_status",
        table_name="unordered_finished_delivery_allocations",
    )
    op.drop_table("unordered_finished_delivery_allocations")

    with op.batch_alter_table("sales_delivery_items", recreate="always") as batch_op:
        batch_op.drop_constraint(
            "ck_sales_delivery_items_source_reference", type_="check"
        )
        batch_op.drop_constraint("ck_sales_delivery_items_source_type", type_="check")
        batch_op.drop_constraint(
            "fk_sales_delivery_items_product_id_products", type_="foreignkey"
        )
        batch_op.drop_index("ix_sales_delivery_items_product_id")
        batch_op.drop_column("price_source")
        batch_op.drop_column("unit_price_snapshot")
        batch_op.drop_column("unit_snapshot")
        batch_op.drop_column("specification_snapshot")
        batch_op.drop_column("product_name_snapshot")
        batch_op.drop_column("product_code_snapshot")
        batch_op.drop_column("product_id")
        batch_op.drop_column("source_type")
        batch_op.alter_column(
            "order_item_id",
            existing_type=sa.Integer(),
            nullable=False,
        )

    with op.batch_alter_table("sales_deliveries", recreate="always") as batch_op:
        batch_op.drop_constraint("ck_sales_deliveries_source_mode", type_="check")
        batch_op.drop_column("source_mode")
