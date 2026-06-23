"""Phase 8 finance closure without touching legacy finance tables.

Revision ID: e82d4a6f1b30
Revises: c74b9d1a5e20
Create Date: 2026-06-13
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e82d4a6f1b30"
down_revision: Union[str, Sequence[str], None] = "c74b9d1a5e20"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "statement_monthly_sequences" not in tables:
        op.create_table(
            "statement_monthly_sequences",
            sa.Column("statement_month", sa.String(length=7), nullable=False),
            sa.Column("last_value", sa.Integer(), nullable=False),
            sa.PrimaryKeyConstraint("statement_month"),
        )
    if "finance_return_receipts" not in tables:
        op.create_table(
            "finance_return_receipts",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("delivery_id", sa.Integer(), nullable=False),
            sa.Column("actual_received_date", sa.Date(), nullable=False),
            sa.Column("signed_by", sa.String(length=100), nullable=True),
            sa.Column(
                "status",
                sa.String(length=20),
                server_default=sa.text("'confirmed'"),
                nullable=False,
            ),
            sa.Column("created_by", sa.Integer(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(),
                server_default=sa.text("CURRENT_TIMESTAMP"),
                nullable=False,
            ),
            sa.CheckConstraint(
                "status IN ('confirmed', 'cancelled')",
                name="ck_finance_return_receipts_status",
            ),
            sa.ForeignKeyConstraint(
                ["created_by"], ["users.id"], ondelete="SET NULL"
            ),
            sa.ForeignKeyConstraint(
                ["delivery_id"],
                ["sales_deliveries.id"],
                ondelete="RESTRICT",
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "delivery_id",
                name="uq_finance_return_receipts_delivery",
            ),
        )
        op.create_index(
            "ix_finance_return_receipts_received_date",
            "finance_return_receipts",
            ["actual_received_date"],
        )
    if "finance_return_receipt_items" not in tables:
        op.create_table(
            "finance_return_receipt_items",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("return_receipt_id", sa.Integer(), nullable=False),
            sa.Column("delivery_item_id", sa.Integer(), nullable=False),
            sa.Column("actual_received_quantity", sa.Integer(), nullable=False),
            sa.Column("difference_reason", sa.Text(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(),
                server_default=sa.text("CURRENT_TIMESTAMP"),
                nullable=False,
            ),
            sa.CheckConstraint(
                "actual_received_quantity >= 0",
                name="ck_finance_return_receipt_items_quantity",
            ),
            sa.ForeignKeyConstraint(
                ["delivery_item_id"],
                ["sales_delivery_items.id"],
                ondelete="RESTRICT",
            ),
            sa.ForeignKeyConstraint(
                ["return_receipt_id"],
                ["finance_return_receipts.id"],
                ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "delivery_item_id",
                name="uq_finance_return_receipt_items_delivery_item",
            ),
        )
        op.create_index(
            "ix_finance_return_receipt_items_receipt_id",
            "finance_return_receipt_items",
            ["return_receipt_id"],
        )
    if "finance_statements" not in tables:
        op.create_table(
            "finance_statements",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("statement_number", sa.String(length=40), nullable=False),
            sa.Column("customer_id", sa.Integer(), nullable=False),
            sa.Column("statement_month", sa.String(length=7), nullable=False),
            sa.Column(
                "total_receivable",
                sa.Numeric(precision=14, scale=2),
                server_default=sa.text("0"),
                nullable=False,
            ),
            sa.Column(
                "total_gross_profit",
                sa.Numeric(precision=14, scale=2),
                server_default=sa.text("0"),
                nullable=False,
            ),
            sa.Column(
                "status",
                sa.String(length=20),
                server_default=sa.text("'unsettled'"),
                nullable=False,
            ),
            sa.Column("created_by", sa.Integer(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(),
                server_default=sa.text("CURRENT_TIMESTAMP"),
                nullable=False,
            ),
            sa.CheckConstraint(
                "status IN ('unsettled', 'settled')",
                name="ck_finance_statements_status",
            ),
            sa.ForeignKeyConstraint(
                ["created_by"], ["users.id"], ondelete="SET NULL"
            ),
            sa.ForeignKeyConstraint(
                ["customer_id"], ["customers.id"], ondelete="RESTRICT"
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "statement_number",
                name="uq_finance_statements_number",
            ),
        )
        op.create_index(
            "ix_finance_statements_customer_month",
            "finance_statements",
            ["customer_id", "statement_month"],
        )
    if "finance_statement_items" not in tables:
        op.create_table(
            "finance_statement_items",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("statement_id", sa.Integer(), nullable=False),
            sa.Column("return_receipt_item_id", sa.Integer(), nullable=False),
            sa.Column("actual_received_quantity", sa.Integer(), nullable=False),
            sa.Column(
                "unit_price_snapshot",
                sa.Numeric(precision=12, scale=4),
                nullable=False,
            ),
            sa.Column(
                "unit_cost_snapshot",
                sa.Numeric(precision=12, scale=4),
                nullable=False,
            ),
            sa.Column(
                "receivable_amount",
                sa.Numeric(precision=14, scale=2),
                nullable=False,
            ),
            sa.Column(
                "gross_profit_amount",
                sa.Numeric(precision=14, scale=2),
                nullable=False,
            ),
            sa.Column(
                "created_at",
                sa.DateTime(),
                server_default=sa.text("CURRENT_TIMESTAMP"),
                nullable=False,
            ),
            sa.ForeignKeyConstraint(
                ["return_receipt_item_id"],
                ["finance_return_receipt_items.id"],
                ondelete="RESTRICT",
            ),
            sa.ForeignKeyConstraint(
                ["statement_id"],
                ["finance_statements.id"],
                ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "return_receipt_item_id",
                name="uq_finance_statement_items_receipt_item",
            ),
        )
        op.create_index(
            "ix_finance_statement_items_statement_id",
            "finance_statement_items",
            ["statement_id"],
        )


def downgrade() -> None:
    # Finance history is retained to avoid destructive rollback.
    return
