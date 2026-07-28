"""preserve cancelled delivery history by voiding instead of deleting

Revision ID: cu77v8x9z66
Revises: cr74v8x9z63
Create Date: 2026-07-28
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "cu77v8x9z66"
down_revision: Union[str, Sequence[str], None] = "cr74v8x9z63"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table(
        "sales_deliveries",
        recreate="always",
    ) as batch_op:
        batch_op.drop_constraint("ck_sales_deliveries_status", type_="check")
        batch_op.add_column(sa.Column("ever_dispatched_at", sa.DateTime()))
        batch_op.add_column(sa.Column("voided_by", sa.Integer()))
        batch_op.add_column(sa.Column("voided_at", sa.DateTime()))
        batch_op.create_foreign_key(
            "fk_sales_deliveries_voided_by_users",
            "users",
            ["voided_by"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_check_constraint(
            "ck_sales_deliveries_status",
            "status IN ('pending', 'dispatched', 'voided')",
        )

    # Only derive metadata from current foreign-key facts.  No delivery is
    # automatically voided and no order or inventory quantity is changed.
    op.execute(
        """
        UPDATE sales_deliveries
        SET ever_dispatched_at = COALESCE(
            dispatched_at,
            (
                SELECT MIN(dia.created_at)
                FROM sales_delivery_items AS di
                JOIN delivery_inventory_allocations AS dia
                  ON dia.delivery_item_id = di.id
                WHERE di.delivery_id = sales_deliveries.id
            ),
            (
                SELECT MIN(bda.created_at)
                FROM sales_delivery_items AS di
                JOIN bom_component_direct_delivery_allocations AS bda
                  ON bda.delivery_item_id = di.id
                WHERE di.delivery_id = sales_deliveries.id
            ),
            (
                SELECT MIN(rr.created_at)
                FROM finance_return_receipts AS rr
                WHERE rr.delivery_id = sales_deliveries.id
            ),
            (
                SELECT MIN(im.created_at)
                FROM inventory_movements AS im
                WHERE im.related_delivery_id = sales_deliveries.id
            ),
            created_at
        )
        WHERE status = 'dispatched'
           OR EXISTS (
                SELECT 1
                FROM sales_delivery_items AS di
                JOIN delivery_inventory_allocations AS dia
                  ON dia.delivery_item_id = di.id
                WHERE di.delivery_id = sales_deliveries.id
           )
           OR EXISTS (
                SELECT 1
                FROM sales_delivery_items AS di
                JOIN bom_component_direct_delivery_allocations AS bda
                  ON bda.delivery_item_id = di.id
                WHERE di.delivery_id = sales_deliveries.id
           )
           OR EXISTS (
                SELECT 1
                FROM finance_return_receipts AS rr
                WHERE rr.delivery_id = sales_deliveries.id
           )
           OR EXISTS (
                SELECT 1
                FROM inventory_movements AS im
                WHERE im.related_delivery_id = sales_deliveries.id
           )
        """
    )


def downgrade() -> None:
    connection = op.get_bind()
    voided_fact = connection.execute(
        sa.text(
            """
            SELECT 1
            FROM sales_deliveries
            WHERE status = 'voided'
               OR voided_at IS NOT NULL
               OR voided_by IS NOT NULL
            LIMIT 1
            """
        )
    ).first()
    if voided_fact is not None:
        raise RuntimeError("存在已作废送货单，禁止破坏性降级；请恢复迁移前数据库备份")

    with op.batch_alter_table(
        "sales_deliveries",
        recreate="always",
    ) as batch_op:
        batch_op.drop_constraint("ck_sales_deliveries_status", type_="check")
        batch_op.drop_constraint(
            "fk_sales_deliveries_voided_by_users",
            type_="foreignkey",
        )
        batch_op.drop_column("voided_at")
        batch_op.drop_column("voided_by")
        batch_op.drop_column("ever_dispatched_at")
        batch_op.create_check_constraint(
            "ck_sales_deliveries_status",
            "status IN ('pending', 'dispatched')",
        )
