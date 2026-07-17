"""Add the lightweight N029 production workflow tables.

Revision ID: ax51v8x9z41
Revises: aw50v8x9y0s40
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "ax51v8x9z41"
down_revision = "aw50v8x9y0s40"
branch_labels = None
depends_on = None


DOWNGRADE_BLOCKED_MESSAGE = (
    "N029 production-managed tasks or facts exist; downgrade would lose workflow state"
)


def upgrade() -> None:
    op.create_table(
        "production_tasks",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("order_item_id", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.String(30),
            server_default="waiting_material",
            nullable=False,
        ),
        sa.Column(
            "planned_quantity", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column(
            "finished_coverage_snapshot",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column("readiness_basis", sa.String(255), nullable=True),
        sa.Column("ready_at", sa.DateTime(), nullable=True),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.func.current_timestamp(),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "status IN ('waiting_material','pending','completed','not_required')",
            name="ck_production_tasks_status",
        ),
        sa.CheckConstraint(
            "planned_quantity >= 0",
            name="ck_production_tasks_planned_quantity_nonnegative",
        ),
        sa.CheckConstraint(
            "finished_coverage_snapshot >= 0",
            name="ck_production_tasks_finished_coverage_nonnegative",
        ),
        sa.CheckConstraint("version >= 1", name="ck_production_tasks_version"),
        sa.CheckConstraint(
            "((status IN ('waiting_material','not_required') AND planned_quantity = 0) "
            "OR (status IN ('pending','completed') AND planned_quantity > 0))",
            name="ck_production_tasks_status_quantity",
        ),
        sa.ForeignKeyConstraint(
            ["order_item_id"], ["sales_order_items.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "order_item_id", name="uq_production_tasks_order_item"
        ),
    )

    op.create_table(
        "production_completion_batches",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("idempotency_key", sa.String(120), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("item_count", sa.Integer(), nullable=False),
        sa.Column("completed_by", sa.Integer(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.func.current_timestamp(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(request_hash) = 64",
            name="ck_production_completion_batches_request_hash",
        ),
        sa.CheckConstraint(
            "item_count > 0", name="ck_production_completion_batches_item_count"
        ),
        sa.ForeignKeyConstraint(
            ["completed_by"], ["users.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "idempotency_key", name="uq_production_completion_batches_idempotency"
        ),
    )

    op.create_table(
        "production_completions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("batch_id", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.Integer(), nullable=False),
        sa.Column("order_item_id", sa.Integer(), nullable=False),
        sa.Column("expected_version", sa.Integer(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("initial_disposition", sa.String(20), nullable=False),
        sa.Column("warehouse_location_id", sa.Integer(), nullable=True),
        sa.Column("inventory_lot_id", sa.Integer(), nullable=True),
        sa.Column("remarks", sa.Text(), nullable=True),
        sa.Column("completed_by", sa.Integer(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.func.current_timestamp(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "expected_version >= 1",
            name="ck_production_completions_expected_version",
        ),
        sa.CheckConstraint(
            "quantity > 0", name="ck_production_completions_quantity"
        ),
        sa.CheckConstraint(
            "initial_disposition IN ('direct','stock')",
            name="ck_production_completions_initial_disposition",
        ),
        sa.CheckConstraint(
            "((initial_disposition = 'direct' AND warehouse_location_id IS NULL "
            "AND inventory_lot_id IS NULL) OR (initial_disposition = 'stock' "
            "AND warehouse_location_id IS NOT NULL))",
            name="ck_production_completions_disposition_targets",
        ),
        sa.ForeignKeyConstraint(
            ["batch_id"],
            ["production_completion_batches.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"], ["production_tasks.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["order_item_id"], ["sales_order_items.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["warehouse_location_id"],
            ["warehouse_locations.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["inventory_lot_id"], ["inventory_lots.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["completed_by"], ["users.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "order_item_id", name="uq_production_completions_order_item"
        ),
        sa.UniqueConstraint(
            "inventory_lot_id", name="uq_production_completions_inventory_lot"
        ),
    )

    op.create_table(
        "production_stock_transfers",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("completion_id", sa.Integer(), nullable=False),
        sa.Column("warehouse_location_id", sa.Integer(), nullable=False),
        sa.Column("inventory_lot_id", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(120), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("transferred_by", sa.Integer(), nullable=True),
        sa.Column("transferred_at", sa.DateTime(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.func.current_timestamp(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(request_hash) = 64",
            name="ck_production_stock_transfers_request_hash",
        ),
        sa.ForeignKeyConstraint(
            ["completion_id"], ["production_completions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["warehouse_location_id"],
            ["warehouse_locations.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["inventory_lot_id"], ["inventory_lots.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["transferred_by"], ["users.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "completion_id", name="uq_production_stock_transfers_completion"
        ),
        sa.UniqueConstraint(
            "inventory_lot_id", name="uq_production_stock_transfers_inventory_lot"
        ),
        sa.UniqueConstraint(
            "idempotency_key", name="uq_production_stock_transfers_idempotency"
        ),
    )


def downgrade() -> None:
    connection = op.get_bind()
    fact_count = connection.execute(
        sa.text(
            """
            SELECT
                (SELECT COUNT(*) FROM production_tasks)
              + (SELECT COUNT(*) FROM production_completion_batches)
              + (SELECT COUNT(*) FROM production_completions)
              + (SELECT COUNT(*) FROM production_stock_transfers)
            """
        )
    ).scalar_one()
    if int(fact_count or 0) > 0:
        raise RuntimeError(DOWNGRADE_BLOCKED_MESSAGE)

    op.drop_table("production_stock_transfers")
    op.drop_table("production_completions")
    op.drop_table("production_completion_batches")
    op.drop_table("production_tasks")
