"""retire the decommissioned source-system extraction layer

Revision ID: gn49v8x9z38
Revises: gm48v8x9z37
Create Date: 2026-08-27
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "gn49v8x9z38"
down_revision = "gm48v8x9z37"
branch_labels = None
depends_on = None


_SOURCE_TABLES_IN_DROP_ORDER = (
    "migration_ruida_sales_item_map",
    "migration_ruida_sales_order_map",
    "legacy_ruida_order_items",
    "legacy_ruida_orders",
    "legacy_ruida_customers",
)


def _table_exists(connection, table_name: str) -> bool:
    return sa.inspect(connection).has_table(table_name)


def upgrade() -> None:
    connection = op.get_bind()
    named_business_orders = int(
        connection.execute(
            sa.text(
                """
                SELECT COUNT(*)
                FROM sales_orders
                WHERE UPPER(COALESCE(order_number, '')) LIKE '%RUIDA%'
                   OR UPPER(COALESCE(remark, '')) LIKE '%RUIDA%'
                   OR COALESCE(remark, '') LIKE '%瑞达%'
                """
            )
        ).scalar_one()
        or 0
    )
    if named_business_orders:
        raise RuntimeError(
            "formal sales orders still contain retired source-system identity; "
            "review them explicitly before removing the source layer"
        )

    for table_name in _SOURCE_TABLES_IN_DROP_ORDER:
        if _table_exists(connection, table_name):
            op.drop_table(table_name)


def downgrade() -> None:
    raise RuntimeError(
        "the retired source-system rows cannot be reconstructed by Alembic; "
        "restore the verified pre-upgrade database backup instead"
    )
