"""add customer-generic replenishment identity

Revision ID: fk46v8x9z35
Revises: fj45v8x9z34
Create Date: 2026-08-27
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "fk46v8x9z35"
down_revision = "fj45v8x9z34"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("stock_replenishment_order_items") as batch:
        batch.add_column(sa.Column("reference_product_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("internal_name", sa.String(length=200), nullable=True))
        batch.create_foreign_key(
            "fk_stock_replenishment_items_reference_product_id",
            "products",
            ["reference_product_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch.create_index(
            "ix_stock_replenishment_items_reference_product",
            ["reference_product_id"],
            unique=False,
        )
    with op.batch_alter_table("semi_finished_inventory_details") as batch:
        batch.add_column(
            sa.Column(
                "customer_generic_eligible",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch.add_column(sa.Column("internal_name", sa.String(length=200), nullable=True))
        batch.create_index(
            "ix_semi_inventory_customer_generic",
            ["owner_customer_id", "customer_generic_eligible"],
            unique=False,
        )


def downgrade() -> None:
    connection = op.get_bind()
    replenishment_facts = int(
        connection.execute(
            sa.text(
                "SELECT COUNT(*) FROM stock_replenishment_order_items "
                "WHERE reference_product_id IS NOT NULL OR internal_name IS NOT NULL"
            )
        ).scalar_one()
        or 0
    )
    generic_lots = int(
        connection.execute(
            sa.text(
                "SELECT COUNT(*) FROM semi_finished_inventory_details "
                "WHERE customer_generic_eligible = 1 OR internal_name IS NOT NULL"
            )
        ).scalar_one()
        or 0
    )
    if replenishment_facts or generic_lots:
        raise RuntimeError(
            "Refusing destructive downgrade: customer-generic replenishment facts exist."
        )
    with op.batch_alter_table("semi_finished_inventory_details") as batch:
        batch.drop_index("ix_semi_inventory_customer_generic")
        batch.drop_column("internal_name")
        batch.drop_column("customer_generic_eligible")
    with op.batch_alter_table("stock_replenishment_order_items") as batch:
        batch.drop_index("ix_stock_replenishment_items_reference_product")
        batch.drop_constraint(
            "fk_stock_replenishment_items_reference_product_id",
            type_="foreignkey",
        )
        batch.drop_column("internal_name")
        batch.drop_column("reference_product_id")
