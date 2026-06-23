"""Phase 12 UAT refinement.

Revision ID: d12f7a3c9b20
Revises: b71c4a9e2d10
Create Date: 2026-06-14
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d12f7a3c9b20"
down_revision: Union[str, Sequence[str], None] = "b71c4a9e2d10"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    customer_columns = {
        column["name"] for column in inspector.get_columns("customers")
    }
    if "delivery_method" not in customer_columns:
        op.add_column(
            "customers",
            sa.Column(
                "delivery_method",
                sa.String(length=20),
                nullable=False,
                server_default="配送",
            ),
        )

    connection.execute(
        sa.text(
            """
            UPDATE sales_order_items
            SET requisition_status = '已报料',
                supplier_delivery_time = NULL,
                supplier_order_number = NULL
            WHERE requisition_status = '供应商已排单'
            """
        )
    )
    connection.execute(
        sa.text(
            """
            UPDATE sales_order_items
            SET requisition_status = '已入库'
            WHERE material_status = 'received'
            """
        )
    )
    connection.execute(
        sa.text(
            """
            UPDATE materials
            SET flute_type = CASE
                WHEN flute_type IN ('BE', 'B', 'E') THEN flute_type
                WHEN flute_type LIKE '%B/E%'
                  OR flute_type LIKE '%E/B%' THEN 'BE'
                ELSE 'AB'
            END
            """
        )
    )
    connection.execute(
        sa.text(
            """
            UPDATE products
            SET production_process = NULL
            WHERE production_process LIKE '%�%'
               OR production_process LIKE '%锟%'
            """
        )
    )


def downgrade() -> None:
    # Additive safety migration: no destructive downgrade is provided.
    pass
