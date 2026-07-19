"""add generic delivery pick task snapshots

Revision ID: cc59v8x9z48
Revises: be58v8x9z49
Create Date: 2026-07-18
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "cc59v8x9z48"
down_revision: str | None = "be58v8x9z49"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "delivery_pick_tasks",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("delivery_id", sa.Integer(), nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=24), server_default="pushed", nullable=False),
        sa.Column("snapshot_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("submitted_by", sa.Integer(), nullable=True),
        sa.Column("applied_by", sa.Integer(), nullable=True),
        sa.Column("submitted_at", sa.DateTime(), nullable=True),
        sa.Column("applied_at", sa.DateTime(), nullable=True),
        sa.Column("dispatched_at", sa.DateTime(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "status IN ('pushed', 'driver_confirmed', 'exception', 'applied', 'dispatched')",
            name="ck_delivery_pick_tasks_status",
        ),
        sa.CheckConstraint(
            "snapshot_version > 0",
            name="ck_delivery_pick_tasks_snapshot_version",
        ),
        sa.ForeignKeyConstraint(
            ["customer_id"], ["customers.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["delivery_id"], ["sales_deliveries.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["submitted_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["applied_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("delivery_id", name="uq_delivery_pick_tasks_delivery_id"),
    )
    op.create_index(
        "ix_delivery_pick_tasks_customer_id",
        "delivery_pick_tasks",
        ["customer_id"],
        unique=False,
    )
    op.create_index(
        "ix_delivery_pick_tasks_status",
        "delivery_pick_tasks",
        ["status"],
        unique=False,
    )
    op.create_index(
        "ix_delivery_pick_tasks_customer_status",
        "delivery_pick_tasks",
        ["customer_id", "status", "id"],
        unique=False,
    )

    op.create_table(
        "delivery_pick_task_items",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("task_id", sa.Integer(), nullable=False),
        sa.Column("delivery_item_id", sa.Integer(), nullable=True),
        sa.Column("order_item_id", sa.Integer(), nullable=False),
        sa.Column("original_quantity", sa.Integer(), nullable=False),
        sa.Column("picked_quantity", sa.Integer(), server_default="0", nullable=False),
        sa.Column("status", sa.String(length=20), server_default="pending", nullable=False),
        sa.Column("product_code_snapshot", sa.String(length=100), nullable=True),
        sa.Column("product_name_snapshot", sa.String(length=255), nullable=True),
        sa.Column("specification_snapshot", sa.String(length=255), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'picked', 'partial', 'no_stock')",
            name="ck_delivery_pick_task_items_status",
        ),
        sa.CheckConstraint(
            "original_quantity > 0", name="ck_delivery_pick_task_items_original"
        ),
        sa.CheckConstraint(
            "picked_quantity >= 0", name="ck_delivery_pick_task_items_picked"
        ),
        sa.ForeignKeyConstraint(
            ["delivery_item_id"], ["sales_delivery_items.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["order_item_id"], ["sales_order_items.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["task_id"], ["delivery_pick_tasks.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "task_id",
            "delivery_item_id",
            name="uq_delivery_pick_task_item_snapshot",
        ),
    )
    op.create_index(
        "ix_delivery_pick_task_items_task_id",
        "delivery_pick_task_items",
        ["task_id"],
        unique=False,
    )
    op.create_index(
        "ix_delivery_pick_task_items_delivery_item_id",
        "delivery_pick_task_items",
        ["delivery_item_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_delivery_pick_task_items_delivery_item_id",
        table_name="delivery_pick_task_items",
    )
    op.drop_index(
        "ix_delivery_pick_task_items_task_id",
        table_name="delivery_pick_task_items",
    )
    op.drop_table("delivery_pick_task_items")
    op.drop_index(
        "ix_delivery_pick_tasks_customer_status",
        table_name="delivery_pick_tasks",
    )
    op.drop_index("ix_delivery_pick_tasks_status", table_name="delivery_pick_tasks")
    op.drop_index(
        "ix_delivery_pick_tasks_customer_id", table_name="delivery_pick_tasks"
    )
    op.drop_table("delivery_pick_tasks")
