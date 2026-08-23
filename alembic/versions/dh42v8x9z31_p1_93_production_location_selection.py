"""add one-time production-to-map location selection sessions

Revision ID: dh42v8x9z31
Revises: de39v8x9z28
Create Date: 2026-08-23
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "dh42v8x9z31"
down_revision = "dg41v8x9z30"
branch_labels = None
depends_on = None


def _assert_safe_downgrade() -> None:
    connection = op.get_bind()
    fact_count = int(
        connection.execute(
            sa.text("SELECT COUNT(*) FROM production_location_selection_sessions")
        ).scalar_one()
    )
    if fact_count:
        raise RuntimeError(
            "P1-93 location selection sessions exist; downgrade would lose "
            "a pending or completed production-to-stock confirmation trail"
        )


def upgrade() -> None:
    op.create_table(
        "production_location_selection_sessions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("token", sa.String(length=96), nullable=False),
        sa.Column("completion_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("create_idempotency_key", sa.String(length=120), nullable=False),
        sa.Column("create_request_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="open", nullable=False),
        sa.Column("selected_location_id", sa.Integer(), nullable=True),
        sa.Column("selected_layout_version", sa.Integer(), nullable=True),
        sa.Column(
            "selected_location_name_snapshot", sa.String(length=250), nullable=True
        ),
        sa.Column("transfer_id", sa.Integer(), nullable=True),
        sa.Column("confirm_idempotency_key", sa.String(length=120), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("selected_at", sa.DateTime(), nullable=True),
        sa.Column("consumed_at", sa.DateTime(), nullable=True),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.func.current_timestamp(),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "length(create_request_hash) = 64",
            name="ck_production_location_selection_sessions_create_hash",
        ),
        sa.CheckConstraint(
            "status IN ('open','selected','consumed')",
            name="ck_production_location_selection_sessions_status",
        ),
        sa.CheckConstraint(
            "version > 0",
            name="ck_production_location_selection_sessions_version",
        ),
        sa.CheckConstraint(
            "(status = 'open' AND selected_location_id IS NULL "
            "AND selected_layout_version IS NULL AND selected_at IS NULL "
            "AND transfer_id IS NULL AND confirm_idempotency_key IS NULL "
            "AND consumed_at IS NULL) OR "
            "(status = 'selected' AND selected_location_id IS NOT NULL "
            "AND selected_layout_version IS NOT NULL AND selected_at IS NOT NULL "
            "AND transfer_id IS NULL AND confirm_idempotency_key IS NULL "
            "AND consumed_at IS NULL) OR "
            "(status = 'consumed' AND selected_location_id IS NOT NULL "
            "AND selected_layout_version IS NOT NULL AND selected_at IS NOT NULL "
            "AND transfer_id IS NOT NULL AND confirm_idempotency_key IS NOT NULL "
            "AND consumed_at IS NOT NULL)",
            name="ck_production_location_selection_sessions_state",
        ),
        sa.ForeignKeyConstraint(
            ["completion_id"], ["production_completions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["customer_id"], ["customers.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["selected_location_id"],
            ["warehouse_locations.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["transfer_id"], ["production_stock_transfers.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "token", name="uq_production_location_selection_sessions_token"
        ),
        sa.UniqueConstraint(
            "create_idempotency_key",
            name="uq_production_location_selection_sessions_create_idempotency",
        ),
    )
    op.create_index(
        "ix_production_location_selection_sessions_completion",
        "production_location_selection_sessions",
        ["completion_id"],
        unique=False,
    )
    op.create_index(
        "ix_production_location_selection_sessions_owner_status",
        "production_location_selection_sessions",
        ["user_id", "status"],
        unique=False,
    )
    op.create_index(
        "ix_production_location_selection_sessions_expires_at",
        "production_location_selection_sessions",
        ["expires_at"],
        unique=False,
    )


def downgrade() -> None:
    _assert_safe_downgrade()
    op.drop_index(
        "ix_production_location_selection_sessions_expires_at",
        table_name="production_location_selection_sessions",
    )
    op.drop_index(
        "ix_production_location_selection_sessions_owner_status",
        table_name="production_location_selection_sessions",
    )
    op.drop_index(
        "ix_production_location_selection_sessions_completion",
        table_name="production_location_selection_sessions",
    )
    op.drop_table("production_location_selection_sessions")
