"""preserve dispatched delivery item revisions

Revision ID: jc64v8x9z53
Revises: jb63v8x9z52
Create Date: 2026-09-01

Existing rows remain the current revision.  Future controlled edits may retain
superseded rows so inventory, receipt and cost foreign keys keep pointing to
the immutable facts that originally created them.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "jc64v8x9z53"
down_revision = "jb63v8x9z52"
branch_labels = None
depends_on = None


TABLE = "sales_delivery_items"


def upgrade() -> None:
    connection = op.get_bind()
    recreate = "always" if connection.dialect.name == "sqlite" else "auto"
    with op.batch_alter_table(TABLE, recreate=recreate) as batch_op:
        batch_op.drop_constraint(
            "uq_sales_delivery_items_order_item",
            type_="unique",
        )
        batch_op.add_column(
            sa.Column(
                "revision_number",
                sa.Integer(),
                nullable=False,
                server_default="1",
            )
        )
        batch_op.add_column(
            sa.Column(
                "is_current",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            )
        )
        batch_op.create_check_constraint(
            "ck_sales_delivery_items_revision_number",
            "revision_number >= 1",
        )
        batch_op.create_index(
            "ix_sales_delivery_items_delivery_current",
            ["delivery_id", "is_current"],
        )
    op.create_index(
        "uq_sales_delivery_items_current_order_item",
        TABLE,
        ["delivery_id", "order_item_id"],
        unique=True,
        sqlite_where=sa.text("is_current = 1 AND order_item_id IS NOT NULL"),
        postgresql_where=sa.text("is_current = true AND order_item_id IS NOT NULL"),
    )


def downgrade() -> None:
    connection = op.get_bind()
    historical_rows = int(
        connection.execute(
            sa.text(
                f"SELECT COUNT(*) FROM {TABLE} "
                "WHERE is_current = 0 OR revision_number <> 1"
            )
        ).scalar_one()
    )
    if historical_rows:
        raise RuntimeError(
            "P1-137B downgrade blocked: delivery revision history exists. "
            "Restore the verified pre-migration database backup instead."
        )

    op.drop_index(
        "uq_sales_delivery_items_current_order_item",
        table_name=TABLE,
    )
    recreate = "always" if connection.dialect.name == "sqlite" else "auto"
    with op.batch_alter_table(TABLE, recreate=recreate) as batch_op:
        batch_op.drop_index("ix_sales_delivery_items_delivery_current")
        batch_op.drop_constraint(
            "ck_sales_delivery_items_revision_number",
            type_="check",
        )
        batch_op.drop_column("is_current")
        batch_op.drop_column("revision_number")
        batch_op.create_unique_constraint(
            "uq_sales_delivery_items_order_item",
            ["delivery_id", "order_item_id"],
        )
