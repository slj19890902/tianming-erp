"""Tianhua order-aware pre-delivery matching.

Revision ID: y17s4t5u6v05
Revises: z28t5u6v7w16
"""

from alembic import op
import sqlalchemy as sa


revision = "y17s4t5u6v05"
down_revision = "z28t5u6v7w16"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tianhua_pre_delivery_import_batches",
        sa.Column("pre_delivery_date", sa.Date()),
    )
    op.execute(
        """
        UPDATE tianhua_pre_delivery_import_batches
        SET pre_delivery_date=date(created_at, '+1 day')
        WHERE pre_delivery_date IS NULL
        """
    )

    with op.batch_alter_table(
        "tianhua_pre_delivery_import_items",
        recreate="always",
    ) as batch_op:
        for column in (
            sa.Column("image_order_no", sa.String(length=150)),
            sa.Column("order_id", sa.Integer()),
            sa.Column("customer_order_no", sa.String(length=150)),
            sa.Column("match_reason", sa.Text()),
            sa.Column("match_score", sa.Integer()),
            sa.Column(
                "candidate_count",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
        ):
            batch_op.add_column(column)
        batch_op.create_foreign_key(
            "fk_tianhua_import_item_order_id",
            "sales_orders",
            ["order_id"],
            ["id"],
        )
    op.execute(
        """
        UPDATE tianhua_pre_delivery_import_items
        SET order_id=(
            SELECT order_id FROM sales_order_items
            WHERE sales_order_items.id=
                tianhua_pre_delivery_import_items.order_item_id
        ),
        customer_order_no=(
            SELECT customer_po FROM sales_orders
            WHERE sales_orders.id=(
                SELECT order_id FROM sales_order_items
                WHERE sales_order_items.id=
                    tianhua_pre_delivery_import_items.order_item_id
            )
        )
        WHERE order_item_id IS NOT NULL
        """
    )

    with op.batch_alter_table(
        "tianhua_pre_delivery_draft_items",
        recreate="always",
    ) as batch_op:
        for column in (
            sa.Column("order_id", sa.Integer()),
            sa.Column("order_number", sa.String(length=64)),
            sa.Column("customer_order_no", sa.String(length=150)),
        ):
            batch_op.add_column(column)
        batch_op.create_foreign_key(
            "fk_tianhua_draft_item_order_id",
            "sales_orders",
            ["order_id"],
            ["id"],
        )
    op.execute(
        """
        UPDATE tianhua_pre_delivery_draft_items
        SET order_id=(
            SELECT order_id FROM sales_order_items
            WHERE sales_order_items.id=
                tianhua_pre_delivery_draft_items.order_item_id
        ),
        order_number=(
            SELECT order_number FROM sales_orders
            WHERE sales_orders.id=(
                SELECT order_id FROM sales_order_items
                WHERE sales_order_items.id=
                    tianhua_pre_delivery_draft_items.order_item_id
            )
        ),
        customer_order_no=(
            SELECT customer_po FROM sales_orders
            WHERE sales_orders.id=(
                SELECT order_id FROM sales_order_items
                WHERE sales_order_items.id=
                    tianhua_pre_delivery_draft_items.order_item_id
            )
        )
        WHERE order_item_id IS NOT NULL
        """
    )


def downgrade() -> None:
    with op.batch_alter_table("tianhua_pre_delivery_draft_items") as batch_op:
        batch_op.drop_constraint(
            "fk_tianhua_draft_item_order_id",
            type_="foreignkey",
        )
        batch_op.drop_column("customer_order_no")
        batch_op.drop_column("order_number")
        batch_op.drop_column("order_id")
    with op.batch_alter_table("tianhua_pre_delivery_import_items") as batch_op:
        batch_op.drop_constraint(
            "fk_tianhua_import_item_order_id",
            type_="foreignkey",
        )
        for column in (
            "candidate_count",
            "match_score",
            "match_reason",
            "customer_order_no",
            "order_id",
            "image_order_no",
        ):
            batch_op.drop_column(column)
    op.drop_column(
        "tianhua_pre_delivery_import_batches",
        "pre_delivery_date",
    )
