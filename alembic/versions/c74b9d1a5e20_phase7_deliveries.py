"""Phase 7 delivery transactions without touching legacy delivery tables.

Revision ID: c74b9d1a5e20
Revises: a6e12f7b4c90
Create Date: 2026-06-13
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c74b9d1a5e20"
down_revision: Union[str, Sequence[str], None] = "a6e12f7b4c90"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {
        column["name"]
        for column in inspector.get_columns("sales_order_items")
    }
    if "delivered_quantity" not in columns:
        op.add_column(
            "sales_order_items",
            sa.Column(
                "delivered_quantity",
                sa.Integer(),
                server_default=sa.text("0"),
                nullable=False,
            ),
        )
    if "is_force_closed" not in columns:
        op.add_column(
            "sales_order_items",
            sa.Column(
                "is_force_closed",
                sa.Boolean(),
                server_default=sa.text("0"),
                nullable=False,
            ),
        )

    tables = set(sa.inspect(bind).get_table_names())
    if "delivery_daily_sequences" not in tables:
        op.create_table(
            "delivery_daily_sequences",
            sa.Column("sequence_date", sa.Date(), nullable=False),
            sa.Column("last_value", sa.Integer(), nullable=False),
            sa.PrimaryKeyConstraint("sequence_date"),
        )
    if "sales_deliveries" not in tables:
        op.create_table(
            "sales_deliveries",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("delivery_number", sa.String(length=40), nullable=False),
            sa.Column("customer_id", sa.Integer(), nullable=False),
            sa.Column("delivery_date", sa.Date(), nullable=False),
            sa.Column("vehicle_number", sa.String(length=50), nullable=True),
            sa.Column(
                "status",
                sa.String(length=20),
                server_default=sa.text("'pending'"),
                nullable=False,
            ),
            sa.Column(
                "total_quantity",
                sa.Integer(),
                server_default=sa.text("0"),
                nullable=False,
            ),
            sa.Column("created_by", sa.Integer(), nullable=True),
            sa.Column("dispatched_by", sa.Integer(), nullable=True),
            sa.Column("dispatched_at", sa.DateTime(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(),
                server_default=sa.text("CURRENT_TIMESTAMP"),
                nullable=False,
            ),
            sa.CheckConstraint(
                "status IN ('pending', 'dispatched')",
                name="ck_sales_deliveries_status",
            ),
            sa.ForeignKeyConstraint(
                ["created_by"], ["users.id"], ondelete="SET NULL"
            ),
            sa.ForeignKeyConstraint(
                ["customer_id"], ["customers.id"], ondelete="RESTRICT"
            ),
            sa.ForeignKeyConstraint(
                ["dispatched_by"], ["users.id"], ondelete="SET NULL"
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "delivery_number",
                name="uq_sales_deliveries_delivery_number",
            ),
        )
        op.create_index(
            "ix_sales_deliveries_customer_id",
            "sales_deliveries",
            ["customer_id"],
        )
        op.create_index(
            "ix_sales_deliveries_delivery_date",
            "sales_deliveries",
            ["delivery_date"],
        )
        op.create_index(
            "ix_sales_deliveries_status",
            "sales_deliveries",
            ["status"],
        )
    if "sales_delivery_items" not in tables:
        op.create_table(
            "sales_delivery_items",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("delivery_id", sa.Integer(), nullable=False),
            sa.Column("order_item_id", sa.Integer(), nullable=False),
            sa.Column("delivered_quantity", sa.Integer(), nullable=False),
            sa.Column("remarks", sa.Text(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(),
                server_default=sa.text("CURRENT_TIMESTAMP"),
                nullable=False,
            ),
            sa.CheckConstraint(
                "delivered_quantity > 0",
                name="ck_sales_delivery_items_quantity",
            ),
            sa.ForeignKeyConstraint(
                ["delivery_id"],
                ["sales_deliveries.id"],
                ondelete="CASCADE",
            ),
            sa.ForeignKeyConstraint(
                ["order_item_id"],
                ["sales_order_items.id"],
                ondelete="RESTRICT",
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "delivery_id",
                "order_item_id",
                name="uq_sales_delivery_items_order_item",
            ),
        )
        op.create_index(
            "ix_sales_delivery_items_delivery_id",
            "sales_delivery_items",
            ["delivery_id"],
        )
        op.create_index(
            "ix_sales_delivery_items_order_item_id",
            "sales_delivery_items",
            ["order_item_id"],
        )


def downgrade() -> None:
    # Delivery history is retained to avoid destructive rollback.
    return
