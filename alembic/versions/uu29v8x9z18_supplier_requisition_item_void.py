"""add durable supplier requisition item void facts

Revision ID: uu29v8x9z18
Revises: tt28v8x9z17
Create Date: 2026-08-18
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "uu29v8x9z18"
down_revision = "tt28v8x9z17"
branch_labels = None
depends_on = None


TABLE = "supplier_requisition_order_items"


def upgrade() -> None:
    with op.batch_alter_table(TABLE) as batch:
        batch.add_column(
            sa.Column("status", sa.String(length=20), nullable=False, server_default="active")
        )
        batch.add_column(
            sa.Column("version", sa.Integer(), nullable=False, server_default="1")
        )
        batch.add_column(sa.Column("voided_at", sa.DateTime(), nullable=True))
        batch.add_column(
            sa.Column(
                "voided_by",
                sa.Integer(),
                sa.ForeignKey(
                    "users.id",
                    ondelete="SET NULL",
                    name="fk_supplier_requisition_order_items_voided_by_users",
                ),
                nullable=True,
            )
        )
        batch.add_column(
            sa.Column("void_idempotency_key", sa.String(length=120), nullable=True)
        )
        batch.add_column(
            sa.Column("void_request_hash", sa.String(length=64), nullable=True)
        )
        batch.create_unique_constraint(
            "uq_supplier_requisition_order_items_void_idempotency",
            ["void_idempotency_key"],
        )
        batch.create_check_constraint(
            "ck_supplier_requisition_order_items_status",
            "status IN ('active','voided')",
        )
        batch.create_check_constraint(
            "ck_supplier_requisition_order_items_version",
            "version >= 1",
        )
        batch.create_check_constraint(
            "ck_supplier_requisition_order_items_void_fact",
            "((status = 'active' AND voided_at IS NULL AND voided_by IS NULL "
            "AND void_idempotency_key IS NULL AND void_request_hash IS NULL) OR "
            "(status = 'voided' AND voided_at IS NOT NULL "
            "AND void_idempotency_key IS NOT NULL "
            "AND length(void_request_hash) = 64))",
        )
        batch.create_index(
            "ix_supplier_requisition_order_items_order_status",
            ["supplier_order_id", "status", "id"],
            unique=False,
        )


def downgrade() -> None:
    connection = op.get_bind()
    facts = int(
        connection.execute(
            sa.text(
                f"SELECT COUNT(*) FROM {TABLE} "
                "WHERE status <> 'active' OR version <> 1 OR voided_at IS NOT NULL "
                "OR voided_by IS NOT NULL OR void_idempotency_key IS NOT NULL "
                "OR void_request_hash IS NOT NULL"
            )
        ).scalar_one()
    )
    if facts:
        raise RuntimeError(
            "cannot downgrade P1-73C after supplier requisition item void facts exist"
        )
    with op.batch_alter_table(TABLE) as batch:
        batch.drop_index("ix_supplier_requisition_order_items_order_status")
        batch.drop_constraint(
            "ck_supplier_requisition_order_items_void_fact", type_="check"
        )
        batch.drop_constraint(
            "ck_supplier_requisition_order_items_version", type_="check"
        )
        batch.drop_constraint(
            "ck_supplier_requisition_order_items_status", type_="check"
        )
        batch.drop_constraint(
            "uq_supplier_requisition_order_items_void_idempotency", type_="unique"
        )
        batch.drop_column("void_request_hash")
        batch.drop_column("void_idempotency_key")
        batch.drop_column("voided_by")
        batch.drop_column("voided_at")
        batch.drop_column("version")
        batch.drop_column("status")
