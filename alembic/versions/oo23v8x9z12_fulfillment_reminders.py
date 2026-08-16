"""add internal fulfillment reminders originating from return receipts

Revision ID: oo23v8x9z12
Revises: nn22v8x9z11
Create Date: 2026-08-16
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "oo23v8x9z12"
down_revision = "nn22v8x9z11"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "fulfillment_reminders",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("source_return_receipt_id", sa.Integer(), nullable=True),
        sa.Column("source_return_receipt_id_snapshot", sa.Integer(), nullable=False),
        sa.Column("source_delivery_id_snapshot", sa.Integer(), nullable=False),
        sa.Column("source_delivery_number_snapshot", sa.String(length=40), nullable=False),
        sa.Column("source_received_date_snapshot", sa.Date(), nullable=False),
        sa.Column("source_valid", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("customer_name_snapshot", sa.String(length=200), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=True),
        sa.Column("product_id_snapshot", sa.Integer(), nullable=True),
        sa.Column("product_code_snapshot", sa.String(length=150), nullable=True),
        sa.Column("product_name_snapshot", sa.String(length=250), nullable=True),
        sa.Column("scope_type", sa.String(length=20), nullable=False),
        sa.Column("reminder_type", sa.String(length=30), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("suggested_quantity", sa.Numeric(14, 3), nullable=True),
        sa.Column("cadence", sa.String(length=20), nullable=False),
        sa.Column("remind_on", sa.Date(), nullable=True),
        sa.Column("status", sa.String(length=20), server_default="active", nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("created_by_name_snapshot", sa.String(length=100), nullable=False),
        sa.Column("updated_by", sa.Integer(), nullable=True),
        sa.Column("resolved_by", sa.Integer(), nullable=True),
        sa.Column("cancelled_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint("scope_type IN ('receipt','customer','product')", name="ck_fulfillment_reminders_scope_type"),
        sa.CheckConstraint("reminder_type IN ('replenishment','delivery_attention','production_attention','other')", name="ck_fulfillment_reminders_type"),
        sa.CheckConstraint("cadence IN ('one_time','continuous')", name="ck_fulfillment_reminders_cadence"),
        sa.CheckConstraint("status IN ('active','resolved','cancelled')", name="ck_fulfillment_reminders_status"),
        sa.CheckConstraint("suggested_quantity IS NULL OR suggested_quantity > 0", name="ck_fulfillment_reminders_suggested_quantity"),
        sa.CheckConstraint("version >= 1", name="ck_fulfillment_reminders_version"),
        sa.CheckConstraint("(scope_type = 'product' AND product_id_snapshot IS NOT NULL AND product_code_snapshot IS NOT NULL AND product_name_snapshot IS NOT NULL) OR (scope_type <> 'product' AND product_id_snapshot IS NULL AND product_id IS NULL AND product_code_snapshot IS NULL AND product_name_snapshot IS NULL)", name="ck_fulfillment_reminders_product_scope"),
        sa.ForeignKeyConstraint(["source_return_receipt_id"], ["finance_return_receipts.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["resolved_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["cancelled_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_fulfillment_reminders_customer_status_due_id", "fulfillment_reminders", ["customer_id", "status", "remind_on", "id"])
    op.create_index("ix_fulfillment_reminders_product_status_due_id", "fulfillment_reminders", ["product_id_snapshot", "status", "remind_on", "id"])
    op.create_index("ix_fulfillment_reminders_source_receipt", "fulfillment_reminders", ["source_return_receipt_id_snapshot", "status", "id"])

    op.create_table(
        "fulfillment_reminder_mutations",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("reminder_id", sa.Integer(), nullable=True),
        sa.Column("return_receipt_id_snapshot", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=120), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("action", sa.String(length=30), nullable=False),
        sa.Column("actor_id", sa.Integer(), nullable=True),
        sa.Column("actor_name_snapshot", sa.String(length=100), nullable=False),
        sa.Column("response_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False),
        sa.CheckConstraint("action IN ('create','update','resolve','cancel','receipt_create_bundle')", name="ck_fulfillment_reminder_mutations_action"),
        sa.ForeignKeyConstraint(["reminder_id"], ["fulfillment_reminders.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key", name="uq_fulfillment_reminder_mutations_key"),
    )
    op.create_index("ix_fulfillment_reminder_mutations_reminder_created", "fulfillment_reminder_mutations", ["reminder_id", "created_at", "id"])
    op.create_index("ix_fulfillment_reminder_mutations_receipt_created", "fulfillment_reminder_mutations", ["return_receipt_id_snapshot", "created_at", "id"])


def downgrade() -> None:
    connection = op.get_bind()
    reminder_count = int(
        connection.execute(sa.text("SELECT COUNT(*) FROM fulfillment_reminders")).scalar_one()
        or 0
    )
    mutation_count = int(
        connection.execute(sa.text("SELECT COUNT(*) FROM fulfillment_reminder_mutations")).scalar_one()
        or 0
    )
    if reminder_count or mutation_count:
        raise RuntimeError(
            "P1-65 已存在回单履约备忘或幂等回执事实，拒绝破坏性降级；"
            "请恢复升级前完整备份。"
        )
    op.drop_index("ix_fulfillment_reminder_mutations_receipt_created", table_name="fulfillment_reminder_mutations")
    op.drop_index("ix_fulfillment_reminder_mutations_reminder_created", table_name="fulfillment_reminder_mutations")
    op.drop_table("fulfillment_reminder_mutations")
    op.drop_index("ix_fulfillment_reminders_source_receipt", table_name="fulfillment_reminders")
    op.drop_index("ix_fulfillment_reminders_product_status_due_id", table_name="fulfillment_reminders")
    op.drop_index("ix_fulfillment_reminders_customer_status_due_id", table_name="fulfillment_reminders")
    op.drop_table("fulfillment_reminders")
