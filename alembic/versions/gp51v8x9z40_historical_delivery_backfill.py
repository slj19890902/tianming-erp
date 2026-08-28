"""add historical delivery backfill audit and optimistic version

Revision ID: gp51v8x9z40
Revises: go50v8x9z39
Create Date: 2026-08-28
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "gp51v8x9z40"
down_revision = "go50v8x9z39"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "sales_deliveries",
        sa.Column(
            "is_historical_backfill",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "sales_deliveries",
        sa.Column("backfilled_by", sa.Integer(), nullable=True),
    )
    op.add_column(
        "sales_deliveries",
        sa.Column("backfilled_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "sales_deliveries",
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
    )
    with op.batch_alter_table("sales_deliveries", recreate="always") as batch:
        batch.create_foreign_key(
            "fk_sales_deliveries_backfilled_by_users",
            "users",
            ["backfilled_by"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.create_check_constraint("ck_sales_deliveries_version", "version >= 1")
        batch.create_check_constraint(
            "ck_sales_deliveries_historical_backfill_audit",
            "(is_historical_backfill = 0 AND backfilled_by IS NULL "
            "AND backfilled_at IS NULL) OR "
            "(is_historical_backfill = 1 AND backfilled_by IS NOT NULL "
            "AND backfilled_at IS NOT NULL)",
        )


def downgrade() -> None:
    connection = op.get_bind()
    historical_facts = int(
        connection.execute(
            sa.text(
                "SELECT COUNT(*) FROM sales_deliveries "
                "WHERE is_historical_backfill = 1 OR backfilled_by IS NOT NULL "
                "OR backfilled_at IS NOT NULL OR version <> 1"
            )
        ).scalar_one()
        or 0
    )
    delivery_mutations = int(
        connection.execute(
            sa.text(
                "SELECT COUNT(*) FROM finance_idempotency_records "
                "WHERE resource_type = 'delivery'"
            )
        ).scalar_one()
        or 0
    )
    if historical_facts or delivery_mutations:
        raise RuntimeError(
            "Refusing destructive downgrade: historical delivery or delivery "
            "idempotency facts exist."
        )

    with op.batch_alter_table("sales_deliveries", recreate="always") as batch:
        batch.drop_constraint(
            "ck_sales_deliveries_historical_backfill_audit",
            type_="check",
        )
        batch.drop_constraint("ck_sales_deliveries_version", type_="check")
        batch.drop_constraint(
            "fk_sales_deliveries_backfilled_by_users",
            type_="foreignkey",
        )
        batch.drop_column("version")
        batch.drop_column("backfilled_at")
        batch.drop_column("backfilled_by")
        batch.drop_column("is_historical_backfill")
