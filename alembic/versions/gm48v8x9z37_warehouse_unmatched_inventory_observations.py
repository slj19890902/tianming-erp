"""add unmatched physical inventory observation facts

Revision ID: gm48v8x9z37
Revises: fl47v8x9z36
Create Date: 2026-08-27
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "gm48v8x9z37"
down_revision = "fl47v8x9z36"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "warehouse_unmatched_inventory_observations",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "observed_location_id",
            sa.Integer(),
            sa.ForeignKey("warehouse_locations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("observed_location_layout_version", sa.Integer(), nullable=False),
        sa.Column("customer_keyword", sa.String(120), nullable=True),
        sa.Column("inventory_keyword", sa.String(200), nullable=False),
        sa.Column("reported_quantity", sa.Integer(), nullable=True),
        sa.Column("reported_unit", sa.String(20), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="open"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("idempotency_key", sa.String(120), nullable=False),
        sa.Column(
            "reported_by",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "reported_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.Column(
            "resolved_by",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.Column("resolution_note", sa.Text(), nullable=True),
        sa.Column("resolution_idempotency_key", sa.String(120), nullable=True),
        sa.Column(
            "resolved_inventory_lot_id",
            sa.Integer(),
            sa.ForeignKey("inventory_lots.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.CheckConstraint(
            "reported_quantity IS NULL OR reported_quantity > 0",
            name="ck_warehouse_unmatched_observations_quantity",
        ),
        sa.CheckConstraint(
            "status IN ('open','resolved','cancelled')",
            name="ck_warehouse_unmatched_observations_status",
        ),
        sa.CheckConstraint(
            "version > 0",
            name="ck_warehouse_unmatched_observations_version",
        ),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_warehouse_unmatched_observations_idempotency",
        ),
        sa.UniqueConstraint(
            "resolution_idempotency_key",
            name="uq_warehouse_unmatched_observations_resolution_idempotency",
        ),
    )
    op.create_index(
        "ix_warehouse_unmatched_observations_location_status",
        "warehouse_unmatched_inventory_observations",
        ["observed_location_id", "status"],
    )
    op.create_index(
        "ix_warehouse_unmatched_observations_status_reported",
        "warehouse_unmatched_inventory_observations",
        ["status", "reported_at", "id"],
    )


def downgrade() -> None:
    connection = op.get_bind()
    count = int(
        connection.execute(
            sa.text(
                "SELECT COUNT(*) FROM warehouse_unmatched_inventory_observations"
            )
        ).scalar_one()
        or 0
    )
    if count:
        raise RuntimeError(
            "warehouse_unmatched_inventory_observations contains review facts; "
            "restore the pre-upgrade backup instead of downgrading"
        )
    op.drop_index(
        "ix_warehouse_unmatched_observations_status_reported",
        table_name="warehouse_unmatched_inventory_observations",
    )
    op.drop_index(
        "ix_warehouse_unmatched_observations_location_status",
        table_name="warehouse_unmatched_inventory_observations",
    )
    op.drop_table("warehouse_unmatched_inventory_observations")
