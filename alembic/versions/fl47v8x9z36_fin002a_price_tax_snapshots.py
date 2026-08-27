"""freeze FIN-002A customer price-tax interpretation

Revision ID: fl47v8x9z36
Revises: fk46v8x9z35
Create Date: 2026-08-27

The migration adds nullable snapshots only. Existing prices and finance
amounts are deliberately not rewritten or guessed from customer names.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "fl47v8x9z36"
down_revision = "fk46v8x9z35"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("sales_order_items") as batch:
        batch.add_column(
            sa.Column("price_tax_mode_snapshot", sa.String(30), nullable=True)
        )
        batch.add_column(
            sa.Column("tax_rate_snapshot", sa.Numeric(6, 4), nullable=True)
        )
        batch.create_check_constraint(
            "ck_sales_order_items_price_tax_mode_snapshot",
            "price_tax_mode_snapshot IS NULL OR "
            "price_tax_mode_snapshot IN ('tax_inclusive','tax_exclusive')",
        )
        batch.create_check_constraint(
            "ck_sales_order_items_tax_rate_snapshot",
            "tax_rate_snapshot IS NULL OR "
            "(tax_rate_snapshot >= 0 AND tax_rate_snapshot <= 1)",
        )

    with op.batch_alter_table("finance_statement_items") as batch:
        batch.add_column(
            sa.Column("price_tax_mode_snapshot", sa.String(30), nullable=True)
        )
        batch.add_column(
            sa.Column("tax_rate_snapshot", sa.Numeric(6, 4), nullable=True)
        )
        batch.create_check_constraint(
            "ck_finance_statement_items_price_tax_mode_snapshot",
            "price_tax_mode_snapshot IS NULL OR "
            "price_tax_mode_snapshot IN ('tax_inclusive','tax_exclusive')",
        )
        batch.create_check_constraint(
            "ck_finance_statement_items_tax_rate_snapshot",
            "tax_rate_snapshot IS NULL OR "
            "(tax_rate_snapshot >= 0 AND tax_rate_snapshot <= 1)",
        )

    with op.batch_alter_table("customer_invoice_item_rules") as batch:
        batch.drop_constraint(
            "ck_customer_invoice_item_rules_spec_source", type_="check"
        )
        batch.create_check_constraint(
            "ck_customer_invoice_item_rules_spec_source",
            "spec_source IN "
            "('product_snapshot','product_code_snapshot','blank')",
        )


def downgrade() -> None:
    connection = op.get_bind()
    order_fact_count = int(
        connection.scalar(
            sa.text(
                "SELECT COUNT(*) FROM sales_order_items "
                "WHERE price_tax_mode_snapshot IS NOT NULL "
                "OR tax_rate_snapshot IS NOT NULL"
            )
        )
        or 0
    )
    statement_fact_count = int(
        connection.scalar(
            sa.text(
                "SELECT COUNT(*) FROM finance_statement_items "
                "WHERE price_tax_mode_snapshot IS NOT NULL "
                "OR tax_rate_snapshot IS NOT NULL"
            )
        )
        or 0
    )
    product_code_rule_count = int(
        connection.scalar(
            sa.text(
                "SELECT COUNT(*) FROM customer_invoice_item_rules "
                "WHERE spec_source = 'product_code_snapshot'"
            )
        )
        or 0
    )
    if order_fact_count or statement_fact_count or product_code_rule_count:
        raise RuntimeError(
            "cannot downgrade FIN-002A after price-tax snapshots or "
            "product-code invoice rules exist"
        )

    with op.batch_alter_table("customer_invoice_item_rules") as batch:
        batch.drop_constraint(
            "ck_customer_invoice_item_rules_spec_source", type_="check"
        )
        batch.create_check_constraint(
            "ck_customer_invoice_item_rules_spec_source",
            "spec_source IN ('product_snapshot','blank')",
        )

    with op.batch_alter_table("finance_statement_items") as batch:
        batch.drop_constraint(
            "ck_finance_statement_items_tax_rate_snapshot", type_="check"
        )
        batch.drop_constraint(
            "ck_finance_statement_items_price_tax_mode_snapshot", type_="check"
        )
        batch.drop_column("tax_rate_snapshot")
        batch.drop_column("price_tax_mode_snapshot")

    with op.batch_alter_table("sales_order_items") as batch:
        batch.drop_constraint(
            "ck_sales_order_items_tax_rate_snapshot", type_="check"
        )
        batch.drop_constraint(
            "ck_sales_order_items_price_tax_mode_snapshot", type_="check"
        )
        batch.drop_column("tax_rate_snapshot")
        batch.drop_column("price_tax_mode_snapshot")
