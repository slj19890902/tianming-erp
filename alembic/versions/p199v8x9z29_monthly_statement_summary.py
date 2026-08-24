"""add explicit monthly-summary and separate statement generation modes

Revision ID: p199v8x9z29
Revises: de39v8x9z28
Create Date: 2026-08-24
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "p199v8x9z29"
down_revision = "de39v8x9z28"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing statements retain their exact historical grouping.  Only new
    # requests explicitly written by the updated application become a monthly
    # summary or a separately generated statement.
    op.add_column(
        "finance_statements",
        sa.Column(
            "generation_mode",
            sa.String(20),
            nullable=False,
            server_default="legacy",
        ),
    )
    with op.batch_alter_table("finance_statements", recreate="always") as batch_op:
        batch_op.create_check_constraint(
            "ck_finance_statements_generation_mode",
            "generation_mode IN ('legacy', 'monthly_summary', 'separate')",
        )
    op.create_index(
        "uq_finance_statements_active_monthly_summary",
        "finance_statements",
        ["customer_id", "statement_month"],
        unique=True,
        sqlite_where=sa.text(
            "generation_mode = 'monthly_summary' "
            "AND confirmation_status <> 'cancelled'"
        ),
        postgresql_where=sa.text(
            "generation_mode = 'monthly_summary' "
            "AND confirmation_status <> 'cancelled'"
        ),
    )


def downgrade() -> None:
    connection = op.get_bind()
    generated_facts = int(
        connection.execute(
            sa.text(
                "SELECT COUNT(*) FROM finance_statements "
                "WHERE generation_mode <> 'legacy'"
            )
        ).scalar_one()
        or 0
    )
    if generated_facts:
        raise RuntimeError(
            "Refusing destructive downgrade: monthly-summary or separately "
            "generated statement facts exist."
        )

    op.drop_index(
        "uq_finance_statements_active_monthly_summary",
        table_name="finance_statements",
    )
    with op.batch_alter_table("finance_statements", recreate="always") as batch_op:
        batch_op.drop_constraint(
            "ck_finance_statements_generation_mode",
            type_="check",
        )
        batch_op.drop_column("generation_mode")
