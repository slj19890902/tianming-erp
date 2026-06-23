"""Expand order statuses for closure and unfinished reporting.

Revision ID: h48d9f6c1e32
Revises: g37c8e5b0d21
Create Date: 2026-06-22
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "h48d9f6c1e32"
down_revision: Union[str, Sequence[str], None] = "g37c8e5b0d21"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Schema-only change. It does not update existing orders or legacy tables.
    with op.batch_alter_table("sales_orders", recreate="always") as batch:
        batch.drop_constraint("ck_sales_orders_status", type_="check")
        batch.create_check_constraint(
            "ck_sales_orders_status",
            "status IN ("
            "'pending_confirmation', 'pending_production', 'production', "
            "'pending_delivery', 'partially_delivered', 'pending_reconciliation', "
            "'pending_invoice', 'pending_payment', 'delivered', 'completed', "
            "'archived', 'closed', 'dead', 'cancelled'"
            ")",
        )


def downgrade() -> None:
    # Destructive status rollback is intentionally disabled.
    pass
