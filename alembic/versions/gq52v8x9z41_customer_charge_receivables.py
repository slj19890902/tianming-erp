"""add customer charge receivables to statements and FIN-001

Revision ID: gq52v8x9z41
Revises: gp51v8x9z40
Create Date: 2026-08-28

No historical charge is inferred.  Existing statement and invoice-task rows
remain delivery sourced.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "gq52v8x9z41"
down_revision = "gp51v8x9z40"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "finance_customer_charges",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "customer_id",
            sa.Integer(),
            sa.ForeignKey("customers.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "order_id",
            sa.Integer(),
            sa.ForeignKey("sales_orders.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "order_item_id",
            sa.Integer(),
            sa.ForeignKey("sales_order_items.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "mold_tool_id",
            sa.Integer(),
            sa.ForeignKey("mold_tools.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "printing_plate_id",
            sa.Integer(),
            sa.ForeignKey("printing_plates.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("charge_type", sa.String(30), nullable=False),
        sa.Column("display_name", sa.String(200), nullable=False),
        sa.Column("quantity", sa.Numeric(14, 4), nullable=False),
        sa.Column("unit", sa.String(40), nullable=False),
        sa.Column("unit_price", sa.Numeric(14, 4), nullable=False),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("price_tax_mode", sa.String(30), nullable=True),
        sa.Column("tax_rate", sa.Numeric(6, 4), nullable=True),
        sa.Column("tax_project_name", sa.String(200), nullable=True),
        sa.Column("tax_classification_code", sa.String(80), nullable=True),
        sa.Column("reconciliation_month", sa.String(7), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("status", sa.String(20), server_default="draft", nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("updated_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("confirmed_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(), nullable=True),
        sa.Column("cancelled_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "charge_type IN ('mold','printing_plate','sample','setup','freight','other')",
            name="ck_finance_customer_charges_type",
        ),
        sa.CheckConstraint(
            "status IN ('draft','confirmed','cancelled')",
            name="ck_finance_customer_charges_status",
        ),
        sa.CheckConstraint("quantity > 0", name="ck_finance_customer_charges_quantity"),
        sa.CheckConstraint("unit_price >= 0", name="ck_finance_customer_charges_unit_price"),
        sa.CheckConstraint("amount > 0", name="ck_finance_customer_charges_amount"),
        sa.CheckConstraint("version >= 1", name="ck_finance_customer_charges_version"),
        sa.CheckConstraint(
            "price_tax_mode IS NULL OR price_tax_mode IN ('tax_inclusive','tax_exclusive')",
            name="ck_finance_customer_charges_price_tax_mode",
        ),
        sa.CheckConstraint(
            "tax_rate IS NULL OR (tax_rate >= 0 AND tax_rate <= 1)",
            name="ck_finance_customer_charges_tax_rate",
        ),
        sa.CheckConstraint(
            "((status = 'draft' AND reconciliation_month IS NULL AND confirmed_by IS NULL AND confirmed_at IS NULL) OR "
            "(status = 'confirmed' AND reconciliation_month IS NOT NULL AND confirmed_by IS NOT NULL AND confirmed_at IS NOT NULL) OR "
            "(status = 'cancelled'))",
            name="ck_finance_customer_charges_confirmation_state",
        ),
    )
    op.create_index(
        "ix_finance_customer_charges_customer_month_status",
        "finance_customer_charges",
        ["customer_id", "reconciliation_month", "status"],
    )
    op.create_index(
        "ix_finance_customer_charges_order",
        "finance_customer_charges",
        ["order_id"],
    )
    op.create_index(
        "ix_finance_customer_charges_mold",
        "finance_customer_charges",
        ["mold_tool_id"],
    )
    op.create_index(
        "ix_finance_customer_charges_plate",
        "finance_customer_charges",
        ["printing_plate_id"],
    )

    with op.batch_alter_table("finance_statement_items", recreate="always") as batch:
        batch.alter_column(
            "return_receipt_item_id",
            existing_type=sa.Integer(),
            nullable=True,
        )
        batch.alter_column(
            "actual_received_quantity",
            existing_type=sa.Integer(),
            nullable=True,
        )
        batch.add_column(sa.Column("customer_charge_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("charge_quantity_snapshot", sa.Numeric(14, 4), nullable=True))
        batch.add_column(sa.Column("unit_snapshot", sa.String(40), nullable=True))
        batch.add_column(sa.Column("source_label_snapshot", sa.String(200), nullable=True))
        batch.create_foreign_key(
            "fk_finance_statement_items_customer_charge",
            "finance_customer_charges",
            ["customer_charge_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.create_unique_constraint(
            "uq_finance_statement_items_customer_charge", ["customer_charge_id"]
        )
        batch.create_check_constraint(
            "ck_finance_statement_items_single_source",
            "((return_receipt_item_id IS NOT NULL AND customer_charge_id IS NULL "
            "AND actual_received_quantity IS NOT NULL AND charge_quantity_snapshot IS NULL "
            "AND unit_snapshot IS NULL AND source_label_snapshot IS NULL) OR "
            "(return_receipt_item_id IS NULL AND customer_charge_id IS NOT NULL "
            "AND actual_received_quantity IS NULL AND charge_quantity_snapshot IS NOT NULL "
            "AND unit_snapshot IS NOT NULL AND source_label_snapshot IS NOT NULL))",
        )

    with op.batch_alter_table("finance_invoice_task_items", recreate="always") as batch:
        batch.add_column(
            sa.Column("source_type", sa.String(30), server_default="delivery", nullable=False)
        )
        batch.add_column(sa.Column("customer_charge_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_finance_invoice_task_items_customer_charge",
            "finance_customer_charges",
            ["customer_charge_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.create_check_constraint(
            "ck_finance_invoice_task_items_source_type",
            "source_type IN ('delivery','customer_charge')",
        )
        batch.create_check_constraint(
            "ck_finance_invoice_task_items_charge_source",
            "((source_type = 'delivery' AND customer_charge_id IS NULL) OR "
            "(source_type = 'customer_charge' AND customer_charge_id IS NOT NULL))",
        )


def downgrade() -> None:
    connection = op.get_bind()
    charge_count = int(
        connection.scalar(sa.text("SELECT COUNT(*) FROM finance_customer_charges")) or 0
    )
    statement_charge_count = int(
        connection.scalar(
            sa.text(
                "SELECT COUNT(*) FROM finance_statement_items "
                "WHERE customer_charge_id IS NOT NULL"
            )
        )
        or 0
    )
    task_charge_count = int(
        connection.scalar(
            sa.text(
                "SELECT COUNT(*) FROM finance_invoice_task_items "
                "WHERE source_type = 'customer_charge' OR customer_charge_id IS NOT NULL"
            )
        )
        or 0
    )
    if charge_count or statement_charge_count or task_charge_count:
        raise RuntimeError(
            "cannot downgrade P1-90 after customer charge, statement, or invoice-task facts exist"
        )

    with op.batch_alter_table("finance_invoice_task_items", recreate="always") as batch:
        batch.drop_constraint(
            "ck_finance_invoice_task_items_charge_source", type_="check"
        )
        batch.drop_constraint(
            "ck_finance_invoice_task_items_source_type", type_="check"
        )
        batch.drop_constraint(
            "fk_finance_invoice_task_items_customer_charge", type_="foreignkey"
        )
        batch.drop_column("customer_charge_id")
        batch.drop_column("source_type")

    with op.batch_alter_table("finance_statement_items", recreate="always") as batch:
        batch.drop_constraint("ck_finance_statement_items_single_source", type_="check")
        batch.drop_constraint(
            "uq_finance_statement_items_customer_charge", type_="unique"
        )
        batch.drop_constraint(
            "fk_finance_statement_items_customer_charge", type_="foreignkey"
        )
        batch.drop_column("source_label_snapshot")
        batch.drop_column("unit_snapshot")
        batch.drop_column("charge_quantity_snapshot")
        batch.drop_column("customer_charge_id")
        batch.alter_column(
            "actual_received_quantity",
            existing_type=sa.Integer(),
            nullable=False,
        )
        batch.alter_column(
            "return_receipt_item_id",
            existing_type=sa.Integer(),
            nullable=False,
        )

    op.drop_index(
        "ix_finance_customer_charges_plate", table_name="finance_customer_charges"
    )
    op.drop_index(
        "ix_finance_customer_charges_mold", table_name="finance_customer_charges"
    )
    op.drop_index(
        "ix_finance_customer_charges_order", table_name="finance_customer_charges"
    )
    op.drop_index(
        "ix_finance_customer_charges_customer_month_status",
        table_name="finance_customer_charges",
    )
    op.drop_table("finance_customer_charges")
