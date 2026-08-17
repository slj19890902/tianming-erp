"""add immutable mold scan task trace

Revision ID: pp24v8x9z13
Revises: oo23v8x9z12
Create Date: 2026-08-17
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "pp24v8x9z13"
down_revision = "oo23v8x9z12"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mold_scan_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("mold_tool_id", sa.Integer(), nullable=False),
        sa.Column("mold_code_snapshot", sa.String(length=100), nullable=False),
        sa.Column("mold_location_snapshot", sa.String(length=250), nullable=False),
        sa.Column("mold_location_version_snapshot", sa.Integer(), nullable=False),
        sa.Column("production_task_id", sa.Integer(), nullable=True),
        sa.Column("production_task_id_snapshot", sa.Integer(), nullable=False),
        sa.Column("production_task_version_snapshot", sa.Integer(), nullable=False),
        sa.Column("production_task_status_snapshot", sa.String(length=30), nullable=False),
        sa.Column("linkage_basis_snapshot", sa.String(length=50), nullable=False),
        sa.Column("sales_order_id", sa.Integer(), nullable=True),
        sa.Column("sales_order_id_snapshot", sa.Integer(), nullable=False),
        sa.Column("sales_order_number_snapshot", sa.String(length=100), nullable=False),
        sa.Column("sales_order_item_id", sa.Integer(), nullable=True),
        sa.Column("sales_order_item_id_snapshot", sa.Integer(), nullable=False),
        sa.Column("customer_id_snapshot", sa.Integer(), nullable=False),
        sa.Column("customer_name_snapshot", sa.String(length=200), nullable=False),
        sa.Column("product_code_snapshot", sa.String(length=150), nullable=True),
        sa.Column("product_name_snapshot", sa.String(length=250), nullable=True),
        sa.Column("scanned_by", sa.Integer(), nullable=True),
        sa.Column("scanned_by_name_snapshot", sa.String(length=100), nullable=False),
        sa.Column("scanned_at", sa.DateTime(), nullable=False),
        sa.Column("source", sa.String(length=20), server_default="fixed_qr", nullable=False),
        sa.Column("idempotency_key", sa.String(length=120), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.CheckConstraint("length(request_hash) = 64", name="ck_mold_scan_events_request_hash"),
        sa.CheckConstraint("mold_location_version_snapshot >= 1", name="ck_mold_scan_events_location_version"),
        sa.CheckConstraint("production_task_version_snapshot >= 1", name="ck_mold_scan_events_task_version"),
        sa.CheckConstraint("source = 'fixed_qr'", name="ck_mold_scan_events_source"),
        sa.ForeignKeyConstraint(["mold_tool_id"], ["mold_tools.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["production_task_id"], ["production_tasks.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["sales_order_id"], ["sales_orders.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["sales_order_item_id"], ["sales_order_items.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["scanned_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key", name="uq_mold_scan_events_idempotency_key"),
    )
    op.create_index(
        "ix_mold_scan_events_mold_scanned_id",
        "mold_scan_events",
        ["mold_tool_id", "scanned_at", "id"],
    )
    op.create_index(
        "ix_mold_scan_events_task_scanned_id",
        "mold_scan_events",
        ["production_task_id_snapshot", "scanned_at", "id"],
    )
    op.create_index(
        "ix_mold_scan_events_order_scanned_id",
        "mold_scan_events",
        ["sales_order_id_snapshot", "scanned_at", "id"],
    )


def downgrade() -> None:
    connection = op.get_bind()
    row_count = int(
        connection.execute(sa.text("SELECT COUNT(*) FROM mold_scan_events")).scalar_one()
        or 0
    )
    if row_count:
        raise RuntimeError(
            "mold_scan_events 已包含正式扫码历史，禁止降级删除；请先备份并取得明确授权。"
        )
    op.drop_index("ix_mold_scan_events_order_scanned_id", table_name="mold_scan_events")
    op.drop_index("ix_mold_scan_events_task_scanned_id", table_name="mold_scan_events")
    op.drop_index("ix_mold_scan_events_mold_scanned_id", table_name="mold_scan_events")
    op.drop_table("mold_scan_events")
