"""add ordered customer finished-goods storage area preferences

Revision ID: ja62v8x9z51
Revises: iz61v8x9z50
Create Date: 2026-09-01

The relation stores stable formal warehouse-area identities only.  It does not
configure a real customer, publish a map, create inventory, or move stock.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "ja62v8x9z51"
down_revision = "iz61v8x9z50"
branch_labels = None
depends_on = None


TABLE = "customer_finished_storage_area_preferences"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("warehouse_area_id", sa.Integer(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.CheckConstraint(
            "priority > 0",
            name="ck_customer_finished_storage_preferences_priority",
        ),
        sa.ForeignKeyConstraint(
            ["customer_id"],
            ["customers.id"],
            name="fk_customer_finished_storage_preferences_customer",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["warehouse_area_id"],
            ["warehouse_areas.id"],
            name="fk_customer_finished_storage_preferences_area",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name="fk_customer_finished_storage_preferences_actor",
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint(
            "customer_id",
            "warehouse_area_id",
            name="uq_customer_finished_storage_preferences_area",
        ),
        sa.UniqueConstraint(
            "customer_id",
            "priority",
            name="uq_customer_finished_storage_preferences_priority",
        ),
    )
    op.create_index(
        "ix_customer_finished_storage_preferences_area",
        TABLE,
        ["warehouse_area_id"],
    )


def downgrade() -> None:
    connection = op.get_bind()
    preference_count = int(
        connection.scalar(sa.text(f"SELECT COUNT(*) FROM {TABLE}")) or 0
    )
    if preference_count:
        raise RuntimeError(
            "cannot downgrade P1-134: customer finished-storage preferences exist; "
            "refusing to delete operator-maintained customer routing facts"
        )
    op.drop_index(
        "ix_customer_finished_storage_preferences_area",
        table_name=TABLE,
    )
    op.drop_table(TABLE)
