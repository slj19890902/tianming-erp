"""Phase 11 material requisition workflow.

Revision ID: b71c4a9e2d10
Revises: f4b2c9d7a110
Create Date: 2026-06-14
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b71c4a9e2d10"
down_revision: Union[str, Sequence[str], None] = "f4b2c9d7a110"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


ORDER_ITEM_COLUMNS = (
    sa.Column("snapshot_product_code", sa.String(length=150), nullable=True),
    sa.Column(
        "inventory_deducted_qty",
        sa.Integer(),
        server_default=sa.text("0"),
        nullable=False,
    ),
    sa.Column("requisition_qty", sa.Integer(), nullable=True),
    sa.Column(
        "requisition_status",
        sa.String(length=30),
        server_default="未报料",
        nullable=False,
    ),
    sa.Column(
        "special_process",
        sa.String(length=30),
        server_default="无",
        nullable=False,
    ),
    sa.Column("requisition_spec", sa.String(length=150), nullable=True),
    sa.Column(
        "cardboard_len",
        sa.Numeric(precision=12, scale=2),
        nullable=True,
    ),
    sa.Column(
        "cardboard_width",
        sa.Numeric(precision=12, scale=2),
        nullable=True,
    ),
    sa.Column("requisition_date", sa.Date(), nullable=True),
    sa.Column("supplier_delivery_time", sa.DateTime(), nullable=True),
    sa.Column("supplier_order_number", sa.String(length=100), nullable=True),
    sa.Column("requisition_remark", sa.Text(), nullable=True),
)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "sales_order_items" in tables:
        existing = {
            column["name"]
            for column in inspector.get_columns("sales_order_items")
        }
        for column in ORDER_ITEM_COLUMNS:
            if column.name not in existing:
                op.add_column("sales_order_items", column)
        indexes = {
            index["name"]
            for index in sa.inspect(bind).get_indexes("sales_order_items")
        }
        if "ix_sales_order_items_requisition_status" not in indexes:
            op.create_index(
                "ix_sales_order_items_requisition_status",
                "sales_order_items",
                ["requisition_status"],
            )

    if "sales_deliveries" in tables:
        delivery_columns = {
            column["name"]
            for column in inspector.get_columns("sales_deliveries")
        }
        if "printed_by" not in delivery_columns:
            op.add_column(
                "sales_deliveries",
                sa.Column("printed_by", sa.Integer(), nullable=True),
            )
        if "printed_at" not in delivery_columns:
            op.add_column(
                "sales_deliveries",
                sa.Column("printed_at", sa.DateTime(), nullable=True),
            )

    if "requisition_daily_sequences" not in tables:
        op.create_table(
            "requisition_daily_sequences",
            sa.Column("sequence_date", sa.Date(), nullable=False),
            sa.Column("last_value", sa.Integer(), nullable=False),
            sa.PrimaryKeyConstraint("sequence_date"),
        )

    if "material_requisitions" not in tables:
        op.create_table(
            "material_requisitions",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("requisition_number", sa.String(length=40), nullable=False),
            sa.Column("requisition_date", sa.Date(), nullable=False),
            sa.Column("supplier_name", sa.String(length=200), nullable=True),
            sa.Column(
                "status",
                sa.String(length=30),
                server_default="已报料",
                nullable=False,
            ),
            sa.Column("created_by", sa.Integer(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(),
                server_default=sa.text("CURRENT_TIMESTAMP"),
                nullable=False,
            ),
            sa.ForeignKeyConstraint(
                ["created_by"],
                ["users.id"],
                ondelete="SET NULL",
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "requisition_number",
                name="uq_material_requisitions_number",
            ),
        )
        op.create_index(
            "ix_material_requisitions_date",
            "material_requisitions",
            ["requisition_date"],
        )
        op.create_index(
            "ix_material_requisitions_status",
            "material_requisitions",
            ["status"],
        )

    if "material_requisition_items" not in tables:
        op.create_table(
            "material_requisition_items",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("requisition_id", sa.Integer(), nullable=False),
            sa.Column("order_item_id", sa.Integer(), nullable=False),
            sa.Column(
                "inventory_deducted_qty",
                sa.Integer(),
                server_default=sa.text("0"),
                nullable=False,
            ),
            sa.Column("requisition_qty", sa.Integer(), nullable=False),
            sa.Column(
                "cardboard_len",
                sa.Numeric(precision=12, scale=2),
                nullable=False,
            ),
            sa.Column(
                "cardboard_width",
                sa.Numeric(precision=12, scale=2),
                nullable=False,
            ),
            sa.Column(
                "special_process",
                sa.String(length=30),
                server_default="无",
                nullable=False,
            ),
            sa.Column("material_snapshot", sa.String(length=250), nullable=True),
            sa.Column(
                "product_code_snapshot",
                sa.String(length=150),
                nullable=True,
            ),
            sa.Column(
                "product_name_snapshot",
                sa.String(length=250),
                nullable=False,
            ),
            sa.Column(
                "specification_snapshot",
                sa.String(length=150),
                nullable=True,
            ),
            sa.Column("remark", sa.Text(), nullable=True),
            sa.Column(
                "status",
                sa.String(length=30),
                server_default="有效",
                nullable=False,
            ),
            sa.Column(
                "created_at",
                sa.DateTime(),
                server_default=sa.text("CURRENT_TIMESTAMP"),
                nullable=False,
            ),
            sa.ForeignKeyConstraint(
                ["order_item_id"],
                ["sales_order_items.id"],
                ondelete="RESTRICT",
            ),
            sa.ForeignKeyConstraint(
                ["requisition_id"],
                ["material_requisitions.id"],
                ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_material_requisition_items_requisition_id",
            "material_requisition_items",
            ["requisition_id"],
        )
        op.create_index(
            "ix_material_requisition_items_order_item_id",
            "material_requisition_items",
            ["order_item_id"],
        )


def downgrade() -> None:
    # Phase 11 keeps requisition history and order-item workflow data.
    return
