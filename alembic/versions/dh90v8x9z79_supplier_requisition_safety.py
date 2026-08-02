"""add supplier requisition idempotency and source facts

Revision ID: dh90v8x9z79
Revises: dg89v8x9z78
Create Date: 2026-08-02
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "dh90v8x9z79"
down_revision = "dg89v8x9z78"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("supplier_requisition_orders") as batch:
        batch.add_column(sa.Column("request_key", sa.String(length=64), nullable=True))
        batch.create_unique_constraint(
            "uq_supplier_requisition_orders_request_key", ["request_key"]
        )
    with op.batch_alter_table("supplier_requisition_order_items") as batch:
        batch.add_column(sa.Column("source_key", sa.String(length=120), nullable=True))
        batch.add_column(sa.Column("report_length_mm", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("report_width_mm", sa.Integer(), nullable=True))
        batch.create_index(
            "ix_supplier_requisition_items_source_key", ["source_key"], unique=False
        )


def downgrade() -> None:
    connection = op.get_bind()
    request_facts = int(
        connection.execute(
            sa.text(
                "SELECT COUNT(*) FROM supplier_requisition_orders "
                "WHERE request_key IS NOT NULL"
            )
        ).scalar()
        or 0
    )
    source_facts = int(
        connection.execute(
            sa.text(
                "SELECT COUNT(*) FROM supplier_requisition_order_items "
                "WHERE source_key IS NOT NULL OR report_length_mm IS NOT NULL "
                "OR report_width_mm IS NOT NULL"
            )
        ).scalar()
        or 0
    )
    if request_facts or source_facts:
        raise RuntimeError("已有报料幂等或来源事实，拒绝破坏性降级")

    with op.batch_alter_table("supplier_requisition_order_items") as batch:
        batch.drop_index("ix_supplier_requisition_items_source_key")
        batch.drop_column("report_width_mm")
        batch.drop_column("report_length_mm")
        batch.drop_column("source_key")
    with op.batch_alter_table("supplier_requisition_orders") as batch:
        batch.drop_constraint(
            "uq_supplier_requisition_orders_request_key", type_="unique"
        )
        batch.drop_column("request_key")
