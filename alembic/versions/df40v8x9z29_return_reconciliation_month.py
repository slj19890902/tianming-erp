"""add explicit return-receipt reconciliation month and finance idempotency

Revision ID: df40v8x9z29
Revises: de39v8x9z28
Create Date: 2026-08-22
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "df40v8x9z29"
down_revision = "de39v8x9z28"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Historical rows intentionally remain NULL.  The application preserves
    # their previous customer-cycle/delivery-date grouping rule.
    op.add_column(
        "finance_return_receipts",
        sa.Column("reconciliation_month", sa.String(7), nullable=True),
    )
    op.add_column(
        "finance_return_receipts",
        sa.Column(
            "version",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
    )
    op.create_index(
        "ix_finance_return_receipts_reconciliation_month",
        "finance_return_receipts",
        ["reconciliation_month"],
        unique=False,
    )
    with op.batch_alter_table(
        "finance_return_receipts",
        recreate="always",
    ) as batch_op:
        batch_op.create_check_constraint(
            "ck_finance_return_receipts_version",
            "version >= 1",
        )

    op.create_table(
        "finance_idempotency_records",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("idempotency_key", sa.String(120), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("action", sa.String(50), nullable=False),
        sa.Column("actor_user_id", sa.Integer(), nullable=True),
        sa.Column("resource_type", sa.String(30), nullable=False),
        sa.Column("resource_id", sa.Integer(), nullable=False),
        sa.Column("response_json", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["users.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_finance_idempotency_records_key",
        ),
    )
    op.create_index(
        "ix_finance_idempotency_records_resource",
        "finance_idempotency_records",
        ["resource_type", "resource_id"],
        unique=False,
    )


def downgrade() -> None:
    connection = op.get_bind()
    explicit_months = int(
        connection.execute(
            sa.text(
                "SELECT COUNT(*) FROM finance_return_receipts "
                "WHERE reconciliation_month IS NOT NULL"
            )
        ).scalar_one()
        or 0
    )
    idempotency_facts = int(
        connection.execute(
            sa.text("SELECT COUNT(*) FROM finance_idempotency_records")
        ).scalar_one()
        or 0
    )
    changed_versions = int(
        connection.execute(
            sa.text(
                "SELECT COUNT(*) FROM finance_return_receipts WHERE version <> 1"
            )
        ).scalar_one()
        or 0
    )
    if explicit_months or idempotency_facts or changed_versions:
        raise RuntimeError(
            "Refusing destructive downgrade: explicit reconciliation-month, "
            "version, or finance idempotency facts exist."
        )

    op.drop_index(
        "ix_finance_idempotency_records_resource",
        table_name="finance_idempotency_records",
    )
    op.drop_table("finance_idempotency_records")
    with op.batch_alter_table(
        "finance_return_receipts",
        recreate="always",
    ) as batch_op:
        batch_op.drop_constraint(
            "ck_finance_return_receipts_version",
            type_="check",
        )
        batch_op.drop_index(
            "ix_finance_return_receipts_reconciliation_month"
        )
        batch_op.drop_column("version")
        batch_op.drop_column("reconciliation_month")
