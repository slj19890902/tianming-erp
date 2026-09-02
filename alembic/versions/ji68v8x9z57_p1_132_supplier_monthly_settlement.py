"""add supplier 20th monthly settlement facts

Revision ID: ji68v8x9z57
Revises: jh67v8x9z56
Create Date: 2026-09-02
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "ji68v8x9z57"
down_revision = "jh67v8x9z56"
branch_labels = None
depends_on = None


_RECEIPT_REVERSE_TRIGGER = "trg_supplier_statement_receipt_reverse_guard"


def _create_sqlite_guards() -> None:
    if op.get_bind().dialect.name != "sqlite":
        return
    op.execute(f"DROP TRIGGER IF EXISTS {_RECEIPT_REVERSE_TRIGGER}")
    op.execute(
        f"""
        CREATE TRIGGER {_RECEIPT_REVERSE_TRIGGER}
        BEFORE UPDATE OF status ON incoming_receipt_items
        FOR EACH ROW
        WHEN OLD.status = 'posted'
         AND NEW.status = 'reversed'
         AND EXISTS (
             SELECT 1
             FROM supplier_monthly_statement_lines AS line
             JOIN supplier_monthly_statements AS statement
               ON statement.id = line.statement_id
             WHERE line.incoming_receipt_item_id = OLD.id
               AND line.active_guard = 1
               AND statement.active_guard = 1
               AND statement.status IN (
                   'confirmed_pending_invoice',
                   'invoiced_pending_payment',
                   'partial_payment',
                   'paid'
               )
         )
        BEGIN
            SELECT RAISE(
                ABORT,
                'receipt belongs to a confirmed supplier monthly statement'
            );
        END
        """
    )


def _assert_safe_downgrade() -> None:
    bind = op.get_bind()
    tables = (
        "supplier_monthly_payments",
        "supplier_monthly_invoices",
        "supplier_monthly_adjustments",
        "supplier_monthly_statement_lines",
        "supplier_monthly_statements",
    )
    used = [
        table
        for table in tables
        if int(
            bind.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
            or 0
        )
    ]
    if used:
        raise RuntimeError(
            "P1-132 supplier settlement downgrade blocked: facts would be lost: "
            + ", ".join(used)
        )


def upgrade() -> None:
    op.create_table(
        "supplier_monthly_statements",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("statement_number", sa.String(60), nullable=False),
        sa.Column("supplier_id", sa.Integer(), nullable=False),
        sa.Column("supplier_name_snapshot", sa.String(200), nullable=False),
        sa.Column("settlement_month", sa.String(7), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("tax_basis", sa.String(20), nullable=False),
        sa.Column("status", sa.String(40), nullable=False, server_default="draft"),
        sa.Column("active_guard", sa.Integer(), nullable=True, server_default="1"),
        sa.Column("erp_amount", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column(
            "adjustment_amount", sa.Numeric(18, 2), nullable=False, server_default="0"
        ),
        sa.Column(
            "adjusted_amount", sa.Numeric(18, 2), nullable=False, server_default="0"
        ),
        sa.Column("supplier_statement_number", sa.String(120), nullable=True),
        sa.Column("supplier_statement_date", sa.Date(), nullable=True),
        sa.Column("supplier_statement_amount", sa.Numeric(18, 2), nullable=True),
        sa.Column("confirmed_amount", sa.Numeric(18, 2), nullable=True),
        sa.Column(
            "invoice_allocated_amount",
            sa.Numeric(18, 2),
            nullable=False,
            server_default="0",
        ),
        sa.Column("paid_amount", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("finance_payable_id", sa.Integer(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("generated_by", sa.Integer(), nullable=True),
        sa.Column(
            "generated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.Column("reviewed_by", sa.Integer(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(), nullable=True),
        sa.Column("confirmed_by", sa.Integer(), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(), nullable=True),
        sa.Column("reopened_by", sa.Integer(), nullable=True),
        sa.Column("reopened_at", sa.DateTime(), nullable=True),
        sa.Column("voided_by", sa.Integer(), nullable=True),
        sa.Column("voided_at", sa.DateTime(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "status IN ('draft','difference','confirmed_pending_invoice',"
            "'invoiced_pending_payment','partial_payment','paid','voided')",
            name="ck_supplier_monthly_statements_status",
        ),
        sa.CheckConstraint(
            "tax_basis IN ('tax_inclusive','tax_exclusive')",
            name="ck_supplier_monthly_statements_tax_basis",
        ),
        sa.CheckConstraint(
            "period_start <= period_end", name="ck_supplier_monthly_statements_period"
        ),
        sa.CheckConstraint(
            "erp_amount >= 0 AND adjusted_amount >= 0 "
            "AND invoice_allocated_amount >= 0 AND paid_amount >= 0",
            name="ck_supplier_monthly_statements_amounts",
        ),
        sa.CheckConstraint(
            "supplier_statement_amount IS NULL OR supplier_statement_amount >= 0",
            name="ck_supplier_monthly_statements_supplier_amount",
        ),
        sa.CheckConstraint(
            "confirmed_amount IS NULL OR confirmed_amount > 0",
            name="ck_supplier_monthly_statements_confirmed_amount",
        ),
        sa.CheckConstraint("version >= 1", name="ck_supplier_monthly_statements_version"),
        sa.CheckConstraint(
            "active_guard IS NULL OR active_guard = 1",
            name="ck_supplier_monthly_statements_active_guard",
        ),
        sa.ForeignKeyConstraint(
            ["supplier_id"], ["supplier_master_records.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["finance_payable_id"], ["finance_payables.id"], ondelete="SET NULL"
        ),
        *(
            sa.ForeignKeyConstraint([column], ["users.id"], ondelete="SET NULL")
            for column in (
                "generated_by",
                "reviewed_by",
                "confirmed_by",
                "reopened_by",
                "voided_by",
            )
        ),
        sa.UniqueConstraint(
            "statement_number", name="uq_supplier_monthly_statements_number"
        ),
        sa.UniqueConstraint(
            "supplier_id",
            "settlement_month",
            "currency",
            "tax_basis",
            "active_guard",
            name="uq_supplier_monthly_statements_active_period",
        ),
    )
    op.create_index(
        "ix_supplier_monthly_statements_month_status",
        "supplier_monthly_statements",
        ["settlement_month", "status"],
    )
    op.create_index(
        "ix_supplier_monthly_statements_supplier",
        "supplier_monthly_statements",
        ["supplier_id", "period_end"],
    )

    op.create_table(
        "supplier_monthly_statement_lines",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("statement_id", sa.Integer(), nullable=False),
        sa.Column("source_type", sa.String(30), nullable=False),
        sa.Column("source_key", sa.String(100), nullable=False),
        sa.Column("incoming_receipt_item_id", sa.Integer(), nullable=True),
        sa.Column("external_receipt_item_id", sa.Integer(), nullable=True),
        sa.Column("purchase_document_number", sa.String(80), nullable=False),
        sa.Column("receipt_number", sa.String(80), nullable=False),
        sa.Column("receipt_date", sa.Date(), nullable=False),
        sa.Column("category_label", sa.String(80), nullable=False),
        sa.Column("specification_snapshot", sa.String(500), nullable=True),
        sa.Column("material_or_product_snapshot", sa.String(250), nullable=False),
        sa.Column("received_quantity", sa.Numeric(18, 6), nullable=False),
        sa.Column("quantity_unit", sa.String(20), nullable=False),
        sa.Column("frozen_unit_price", sa.Numeric(20, 6), nullable=False),
        sa.Column("price_unit", sa.String(30), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("tax_basis", sa.String(20), nullable=False),
        sa.Column("tax_rate", sa.Numeric(8, 6), nullable=False),
        sa.Column("erp_amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("tax_amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("source_link", sa.String(255), nullable=False),
        sa.Column("active_guard", sa.Integer(), nullable=True, server_default="1"),
        sa.Column("released_at", sa.DateTime(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.CheckConstraint(
            "source_type IN ('paperboard','external_packaging')",
            name="ck_supplier_monthly_statement_lines_source_type",
        ),
        sa.CheckConstraint(
            "((source_type = 'paperboard' AND incoming_receipt_item_id IS NOT NULL "
            "AND external_receipt_item_id IS NULL) OR "
            "(source_type = 'external_packaging' AND incoming_receipt_item_id IS NULL "
            "AND external_receipt_item_id IS NOT NULL))",
            name="ck_supplier_monthly_statement_lines_source_link",
        ),
        sa.CheckConstraint(
            "received_quantity > 0 AND frozen_unit_price > 0 "
            "AND erp_amount >= 0 AND tax_amount >= 0",
            name="ck_supplier_monthly_statement_lines_amounts",
        ),
        sa.CheckConstraint(
            "tax_basis IN ('tax_inclusive','tax_exclusive')",
            name="ck_supplier_monthly_statement_lines_tax_basis",
        ),
        sa.CheckConstraint(
            "active_guard IS NULL OR active_guard = 1",
            name="ck_supplier_monthly_statement_lines_active_guard",
        ),
        sa.ForeignKeyConstraint(
            ["statement_id"], ["supplier_monthly_statements.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["incoming_receipt_item_id"], ["incoming_receipt_items.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["external_receipt_item_id"],
            ["external_packaging_receipt_items.id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "statement_id",
            "source_key",
            name="uq_supplier_monthly_statement_lines_statement_source",
        ),
        sa.UniqueConstraint(
            "source_key",
            "active_guard",
            name="uq_supplier_monthly_statement_lines_active_source",
        ),
    )
    op.create_index(
        "ix_supplier_monthly_statement_lines_statement",
        "supplier_monthly_statement_lines",
        ["statement_id", "active_guard"],
    )
    op.create_index(
        "ix_supplier_monthly_statement_lines_receipt_date",
        "supplier_monthly_statement_lines",
        ["receipt_date"],
    )

    op.create_table(
        "supplier_monthly_adjustments",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("statement_id", sa.Integer(), nullable=False),
        sa.Column("statement_line_id", sa.Integer(), nullable=True),
        sa.Column("difference_type", sa.String(30), nullable=False),
        sa.Column("amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.CheckConstraint(
            "difference_type IN ('price','quantity','tax_rounding','other')",
            name="ck_supplier_monthly_adjustments_type",
        ),
        sa.CheckConstraint("amount <> 0", name="ck_supplier_monthly_adjustments_amount"),
        sa.ForeignKeyConstraint(
            ["statement_id"], ["supplier_monthly_statements.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["statement_line_id"],
            ["supplier_monthly_statement_lines.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
    )
    op.create_index(
        "ix_supplier_monthly_adjustments_statement",
        "supplier_monthly_adjustments",
        ["statement_id"],
    )

    op.create_table(
        "supplier_monthly_invoices",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("statement_id", sa.Integer(), nullable=False),
        sa.Column("supplier_id", sa.Integer(), nullable=False),
        sa.Column("invoice_number", sa.String(120), nullable=False),
        sa.Column("invoice_date", sa.Date(), nullable=False),
        sa.Column("received_date", sa.Date(), nullable=False),
        sa.Column("invoice_total_amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("allocated_amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("tax_amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("attachment_original_name", sa.String(255), nullable=True),
        sa.Column("attachment_stored_name", sa.String(255), nullable=True),
        sa.Column("attachment_content_hash", sa.String(64), nullable=True),
        sa.Column("attachment_size", sa.Integer(), nullable=True),
        sa.Column("attached_by", sa.Integer(), nullable=True),
        sa.Column("attached_at", sa.DateTime(), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.CheckConstraint(
            "invoice_total_amount > 0 AND allocated_amount > 0 "
            "AND tax_amount >= 0 AND tax_amount <= allocated_amount",
            name="ck_supplier_monthly_invoices_amounts",
        ),
        sa.ForeignKeyConstraint(
            ["statement_id"], ["supplier_monthly_statements.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["supplier_id"], ["supplier_master_records.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["attached_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint(
            "statement_id",
            "invoice_number",
            name="uq_supplier_monthly_invoices_statement_number",
        ),
    )
    op.create_index(
        "ix_supplier_monthly_invoices_statement",
        "supplier_monthly_invoices",
        ["statement_id"],
    )
    op.create_index(
        "ix_supplier_monthly_invoices_supplier_number",
        "supplier_monthly_invoices",
        ["supplier_id", "invoice_number"],
    )

    op.create_table(
        "supplier_monthly_payments",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("statement_id", sa.Integer(), nullable=False),
        sa.Column("payment_date", sa.Date(), nullable=False),
        sa.Column("amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("reference", sa.String(200), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.CheckConstraint("amount > 0", name="ck_supplier_monthly_payments_amount"),
        sa.ForeignKeyConstraint(
            ["statement_id"], ["supplier_monthly_statements.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
    )
    op.create_index(
        "ix_supplier_monthly_payments_statement",
        "supplier_monthly_payments",
        ["statement_id"],
    )
    _create_sqlite_guards()


def downgrade() -> None:
    _assert_safe_downgrade()
    if op.get_bind().dialect.name == "sqlite":
        op.execute(f"DROP TRIGGER IF EXISTS {_RECEIPT_REVERSE_TRIGGER}")
    op.drop_index(
        "ix_supplier_monthly_payments_statement",
        table_name="supplier_monthly_payments",
    )
    op.drop_table("supplier_monthly_payments")
    op.drop_index(
        "ix_supplier_monthly_invoices_supplier_number",
        table_name="supplier_monthly_invoices",
    )
    op.drop_index(
        "ix_supplier_monthly_invoices_statement",
        table_name="supplier_monthly_invoices",
    )
    op.drop_table("supplier_monthly_invoices")
    op.drop_index(
        "ix_supplier_monthly_adjustments_statement",
        table_name="supplier_monthly_adjustments",
    )
    op.drop_table("supplier_monthly_adjustments")
    op.drop_index(
        "ix_supplier_monthly_statement_lines_receipt_date",
        table_name="supplier_monthly_statement_lines",
    )
    op.drop_index(
        "ix_supplier_monthly_statement_lines_statement",
        table_name="supplier_monthly_statement_lines",
    )
    op.drop_table("supplier_monthly_statement_lines")
    op.drop_index(
        "ix_supplier_monthly_statements_supplier",
        table_name="supplier_monthly_statements",
    )
    op.drop_index(
        "ix_supplier_monthly_statements_month_status",
        table_name="supplier_monthly_statements",
    )
    op.drop_table("supplier_monthly_statements")
