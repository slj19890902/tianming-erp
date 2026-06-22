"""Phase 9 finance closeout and disaster recovery metadata.

Revision ID: f4b2c9d7a110
Revises: e82d4a6f1b30
Create Date: 2026-06-14
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f4b2c9d7a110"
down_revision: Union[str, Sequence[str], None] = "e82d4a6f1b30"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "finance_statements" in tables:
        columns = {
            column["name"]
            for column in inspector.get_columns("finance_statements")
        }
        if "invoiced_amount" not in columns:
            op.add_column(
                "finance_statements",
                sa.Column(
                    "invoiced_amount",
                    sa.Numeric(precision=14, scale=2),
                    server_default=sa.text("0"),
                    nullable=False,
                ),
            )
        if "settled_amount" not in columns:
            op.add_column(
                "finance_statements",
                sa.Column(
                    "settled_amount",
                    sa.Numeric(precision=14, scale=2),
                    server_default=sa.text("0"),
                    nullable=False,
                ),
            )

    if "finance_invoices" not in tables:
        op.create_table(
            "finance_invoices",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("statement_id", sa.Integer(), nullable=False),
            sa.Column("invoice_number", sa.String(length=80), nullable=False),
            sa.Column("invoice_date", sa.Date(), nullable=False),
            sa.Column(
                "invoice_amount",
                sa.Numeric(precision=14, scale=2),
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
                "invoice_amount > 0",
                name="ck_finance_invoices_amount_positive",
            ),
            sa.ForeignKeyConstraint(
                ["created_by"], ["users.id"], ondelete="SET NULL"
            ),
            sa.ForeignKeyConstraint(
                ["statement_id"],
                ["finance_statements.id"],
                ondelete="RESTRICT",
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "invoice_number",
                name="uq_finance_invoices_number",
            ),
        )
        op.create_index(
            "ix_finance_invoices_statement_id",
            "finance_invoices",
            ["statement_id"],
        )

    if "finance_settlement_records" not in tables:
        op.create_table(
            "finance_settlement_records",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("statement_id", sa.Integer(), nullable=False),
            sa.Column(
                "settled_amount",
                sa.Numeric(precision=14, scale=2),
                nullable=False,
            ),
            sa.Column("settlement_date", sa.Date(), nullable=False),
            sa.Column("account", sa.String(length=120), nullable=False),
            sa.Column("created_by", sa.Integer(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(),
                server_default=sa.text("CURRENT_TIMESTAMP"),
                nullable=False,
            ),
            sa.CheckConstraint(
                "settled_amount > 0",
                name="ck_finance_settlement_records_amount_positive",
            ),
            sa.ForeignKeyConstraint(
                ["created_by"], ["users.id"], ondelete="SET NULL"
            ),
            sa.ForeignKeyConstraint(
                ["statement_id"],
                ["finance_statements.id"],
                ondelete="RESTRICT",
            ),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_finance_settlement_records_statement_id",
            "finance_settlement_records",
            ["statement_id"],
        )


def downgrade() -> None:
    # Financial history and cumulative balances are retained deliberately.
    return
