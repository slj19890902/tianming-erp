"""v0.20.3: company_config table

Revision ID: t68n0r1s7u50
Revises: s57m1p9q6r39
Create Date: 2026-06-27
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "t68n0r1s7u50"
down_revision = "s57m1p9q6r39"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "company_config",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("company_name", sa.String(100), nullable=False, server_default=""),
        sa.Column("short_name", sa.String(50), nullable=True),
        sa.Column("address", sa.String(200), nullable=True),
        sa.Column("phone", sa.String(50), nullable=True),
        sa.Column("fax", sa.String(50), nullable=True),
        sa.Column("tax_number", sa.String(50), nullable=True),
        sa.Column("bank_name", sa.String(100), nullable=True),
        sa.Column("bank_account", sa.String(50), nullable=True),
        sa.Column("contact_person", sa.String(50), nullable=True),
        sa.Column("contact_phone", sa.String(50), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    op.execute(
        "INSERT INTO company_config (id, company_name) VALUES (1, '')"
    )


def downgrade() -> None:
    op.drop_table("company_config")
