"""add long-term order and completion history lookup indexes

Revision ID: gt55v8x9z44
Revises: gs54v8x9z43
Create Date: 2026-08-29

This migration changes indexes only.  It does not backfill, rewrite, archive, or
delete any order, production, delivery, return, statement, invoice, or payment
fact.
"""

from __future__ import annotations

from alembic import op


revision = "gt55v8x9z44"
down_revision = "gs54v8x9z43"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_sales_orders_created_at_id",
        "sales_orders",
        ["created_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_sales_orders_updated_at_id",
        "sales_orders",
        ["updated_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_products_product_code",
        "products",
        ["product_code"],
        unique=False,
    )
    op.create_index(
        "ix_products_customer_material_code",
        "products",
        ["customer_material_code"],
        unique=False,
    )
    op.create_index(
        "ix_production_completions_completed_at_id",
        "production_completions",
        ["completed_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_production_completions_status_completed_at_id",
        "production_completions",
        ["status", "completed_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_production_completions_order_item_id",
        "production_completions",
        ["order_item_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_production_completions_order_item_id",
        table_name="production_completions",
    )
    op.drop_index(
        "ix_production_completions_status_completed_at_id",
        table_name="production_completions",
    )
    op.drop_index(
        "ix_production_completions_completed_at_id",
        table_name="production_completions",
    )
    op.drop_index("ix_products_customer_material_code", table_name="products")
    op.drop_index("ix_products_product_code", table_name="products")
    op.drop_index("ix_sales_orders_updated_at_id", table_name="sales_orders")
    op.drop_index("ix_sales_orders_created_at_id", table_name="sales_orders")
