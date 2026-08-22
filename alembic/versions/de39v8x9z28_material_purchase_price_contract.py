"""store currency and tax terms for a material purchase-price contract

Revision ID: de39v8x9z28
Revises: dd38v8x9z27
Create Date: 2026-08-22
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "de39v8x9z28"
down_revision = "dd38v8x9z27"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing master records remain readable.  A receipt is blocked until its
    # selected material has a complete price contract, rather than inventing
    # currency or tax facts for historical master data.
    op.add_column("materials", sa.Column("purchase_currency", sa.String(3), nullable=True))
    op.add_column(
        "materials", sa.Column("purchase_tax_included", sa.Boolean(), nullable=True)
    )
    op.add_column(
        "materials", sa.Column("purchase_tax_rate", sa.Numeric(8, 6), nullable=True)
    )


def downgrade() -> None:
    connection = op.get_bind()
    configured_contracts = int(
        connection.execute(
            sa.text(
                "SELECT COUNT(*) FROM materials "
                "WHERE purchase_currency IS NOT NULL "
                "OR purchase_tax_included IS NOT NULL "
                "OR purchase_tax_rate IS NOT NULL"
            )
        ).scalar_one()
        or 0
    )
    if configured_contracts:
        raise RuntimeError(
            "Refusing destructive downgrade: material purchase-price contracts exist."
        )
    with op.batch_alter_table("materials", recreate="always") as batch_op:
        batch_op.drop_column("purchase_tax_rate")
        batch_op.drop_column("purchase_tax_included")
        batch_op.drop_column("purchase_currency")
