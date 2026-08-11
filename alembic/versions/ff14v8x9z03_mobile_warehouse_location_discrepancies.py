"""add mobile warehouse location discrepancy review facts

Revision ID: ff14v8x9z03
Revises: ee13v8x9z02
Create Date: 2026-08-12
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "ff14v8x9z03"
down_revision = "ee13v8x9z02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "warehouse_location_discrepancies",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "inventory_lot_id",
            sa.Integer(),
            sa.ForeignKey("inventory_lots.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "registered_location_id",
            sa.Integer(),
            sa.ForeignKey("warehouse_locations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "observed_location_id",
            sa.Integer(),
            sa.ForeignKey("warehouse_locations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("reported_lot_version", sa.Integer(), nullable=False),
        sa.Column("reported_quantity", sa.Integer(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "status", sa.String(20), nullable=False, server_default="open"
        ),
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
        sa.Column(
            "resolution_transfer_id",
            sa.Integer(),
            sa.ForeignKey("inventory_lot_transfers.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("resolution_note", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "reported_quantity > 0",
            name="ck_warehouse_location_discrepancies_quantity",
        ),
        sa.CheckConstraint(
            "registered_location_id <> observed_location_id",
            name="ck_warehouse_location_discrepancies_locations",
        ),
        sa.CheckConstraint(
            "status IN ('open','resolved','cancelled')",
            name="ck_warehouse_location_discrepancies_status",
        ),
        sa.CheckConstraint(
            "version > 0",
            name="ck_warehouse_location_discrepancies_version",
        ),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_warehouse_location_discrepancies_idempotency",
        ),
    )
    op.create_index(
        "ix_warehouse_location_discrepancies_status_reported",
        "warehouse_location_discrepancies",
        ["status", "reported_at", "id"],
    )
    op.create_index(
        "ix_warehouse_location_discrepancies_lot_status",
        "warehouse_location_discrepancies",
        ["inventory_lot_id", "status"],
    )


def downgrade() -> None:
    connection = op.get_bind()
    count = int(
        connection.execute(
            sa.text("SELECT COUNT(*) FROM warehouse_location_discrepancies")
        ).scalar_one()
    )
    if count:
        raise RuntimeError(
            "P1-21F3 已存在位置不符上报或纠正事实，拒绝破坏性降级"
        )
    op.drop_index(
        "ix_warehouse_location_discrepancies_lot_status",
        table_name="warehouse_location_discrepancies",
    )
    op.drop_index(
        "ix_warehouse_location_discrepancies_status_reported",
        table_name="warehouse_location_discrepancies",
    )
    op.drop_table("warehouse_location_discrepancies")
