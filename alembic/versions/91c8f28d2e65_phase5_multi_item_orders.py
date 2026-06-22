"""Phase 5 multi-item orders without touching legacy orders.

Revision ID: 91c8f28d2e65
Revises: 6dd634401138
Create Date: 2026-06-13
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "91c8f28d2e65"
down_revision: Union[str, Sequence[str], None] = "6dd634401138"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    table_names = set(sa.inspect(bind).get_table_names())

    if "order_daily_sequences" not in table_names:
        op.create_table(
            "order_daily_sequences",
            sa.Column("sequence_date", sa.Date(), nullable=False),
            sa.Column("last_value", sa.Integer(), nullable=False),
            sa.PrimaryKeyConstraint("sequence_date"),
        )

    if "sales_orders" not in table_names:
        op.create_table(
            "sales_orders",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("order_number", sa.String(length=40), nullable=False),
            sa.Column("customer_id", sa.Integer(), nullable=False),
            sa.Column("customer_po", sa.String(length=150), nullable=True),
            sa.Column("order_date", sa.Date(), nullable=False),
            sa.Column("delivery_date", sa.Date(), nullable=True),
            sa.Column(
                "status",
                sa.String(length=30),
                server_default=sa.text("'pending_production'"),
                nullable=False,
            ),
            sa.Column(
                "payment_status",
                sa.String(length=20),
                server_default=sa.text("'unpaid'"),
                nullable=False,
            ),
            sa.Column(
                "total_amount",
                sa.Numeric(precision=14, scale=2),
                server_default=sa.text("0"),
                nullable=False,
            ),
            sa.Column("remark", sa.Text(), nullable=True),
            sa.Column("created_by", sa.Integer(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(),
                server_default=sa.text("CURRENT_TIMESTAMP"),
                nullable=False,
            ),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.CheckConstraint(
                "status IN ("
                "'pending_production', 'production', 'pending_delivery', "
                "'partially_delivered', 'delivered', 'cancelled'"
                ")",
                name="ck_sales_orders_status",
            ),
            sa.CheckConstraint(
                "payment_status IN ('unpaid', 'paid')",
                name="ck_sales_orders_payment_status",
            ),
            sa.ForeignKeyConstraint(
                ["created_by"],
                ["users.id"],
                ondelete="SET NULL",
            ),
            sa.ForeignKeyConstraint(
                ["customer_id"],
                ["customers.id"],
                ondelete="RESTRICT",
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "order_number",
                name="uq_sales_orders_order_number",
            ),
        )
        op.create_index(
            "ix_sales_orders_customer_id",
            "sales_orders",
            ["customer_id"],
            unique=False,
        )
        op.create_index(
            "ix_sales_orders_order_date",
            "sales_orders",
            ["order_date"],
            unique=False,
        )
        op.create_index(
            "ix_sales_orders_status",
            "sales_orders",
            ["status"],
            unique=False,
        )

    if "sales_order_items" not in table_names:
        op.create_table(
            "sales_order_items",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("order_id", sa.Integer(), nullable=False),
            sa.Column("product_id", sa.Integer(), nullable=False),
            sa.Column("quantity", sa.Integer(), nullable=False),
            sa.Column(
                "unit_price",
                sa.Numeric(precision=12, scale=4),
                nullable=False,
            ),
            sa.Column(
                "subtotal",
                sa.Numeric(precision=14, scale=2),
                nullable=False,
            ),
            sa.Column(
                "material_status",
                sa.String(length=20),
                server_default=sa.text("'pending'"),
                nullable=False,
            ),
            sa.Column("material_received_at", sa.DateTime(), nullable=True),
            sa.Column(
                "snapshot_product_name",
                sa.String(length=250),
                nullable=False,
            ),
            sa.Column("snapshot_spec", sa.String(length=150), nullable=True),
            sa.Column("snapshot_material", sa.String(length=250), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(),
                server_default=sa.text("CURRENT_TIMESTAMP"),
                nullable=False,
            ),
            sa.CheckConstraint(
                "material_status IN ('pending', 'received')",
                name="ck_sales_order_items_material_status",
            ),
            sa.CheckConstraint(
                "quantity > 0",
                name="ck_sales_order_items_quantity",
            ),
            sa.CheckConstraint(
                "subtotal >= 0",
                name="ck_sales_order_items_subtotal",
            ),
            sa.CheckConstraint(
                "unit_price >= 0",
                name="ck_sales_order_items_unit_price",
            ),
            sa.ForeignKeyConstraint(
                ["order_id"],
                ["sales_orders.id"],
                ondelete="CASCADE",
            ),
            sa.ForeignKeyConstraint(
                ["product_id"],
                ["products.id"],
                ondelete="RESTRICT",
            ),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_sales_order_items_material_status",
            "sales_order_items",
            ["material_status"],
            unique=False,
        )
        op.create_index(
            "ix_sales_order_items_order_id",
            "sales_order_items",
            ["order_id"],
            unique=False,
        )
        op.create_index(
            "ix_sales_order_items_product_id",
            "sales_order_items",
            ["product_id"],
            unique=False,
        )


def downgrade() -> None:
    # The legacy orders table and Phase 5 data are intentionally retained.
    return
