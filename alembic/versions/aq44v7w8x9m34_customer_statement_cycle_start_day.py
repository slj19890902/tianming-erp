"""Add customer statement cycle start day.

Revision ID: aq44v7w8x9m34
Revises: an41v7w8x9j31
Create Date: 2026-07-15
"""

from alembic import op
import sqlalchemy as sa


revision = "aq44v7w8x9m34"
down_revision = "an41v7w8x9j31"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "customers",
        sa.Column(
            "statement_cycle_start_day",
            sa.Integer(),
            sa.CheckConstraint(
                "statement_cycle_start_day BETWEEN 1 AND 28",
                name="ck_customers_statement_cycle_start_day",
            ),
            nullable=False,
            server_default=sa.text("20"),
        ),
    )


def downgrade() -> None:
    op.drop_column("customers", "statement_cycle_start_day")
