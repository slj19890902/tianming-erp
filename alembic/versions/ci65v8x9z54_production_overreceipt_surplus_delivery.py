"""production overreceipt, surplus inventory, and over-delivery facts

Revision ID: ci65v8x9z54
Revises: cg63v8x9z52
Create Date: 2026-07-23
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "ci65v8x9z54"
down_revision: Union[str, Sequence[str], None] = "cg63v8x9z52"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("production_tasks") as batch:
        batch.add_column(
            sa.Column("ordered_quantity_snapshot", sa.Integer(), server_default="0", nullable=False)
        )
        batch.add_column(
            sa.Column("material_received_quantity", sa.Integer(), server_default="0", nullable=False)
        )
        batch.add_column(
            sa.Column("material_input_quantity", sa.Integer(), server_default="0", nullable=False)
        )
        batch.add_column(
            sa.Column("output_factor", sa.Integer(), server_default="1", nullable=False)
        )

    op.execute(
        """
        UPDATE production_tasks
        SET ordered_quantity_snapshot = COALESCE(
            (SELECT quantity FROM sales_order_items WHERE id = production_tasks.order_item_id),
            planned_quantity
        ),
        material_received_quantity = planned_quantity,
        material_input_quantity = planned_quantity,
        output_factor = 1
        """
    )

    with op.batch_alter_table("production_completions") as batch:
        batch.drop_index("uq_production_completions_task_active")
        batch.drop_constraint(
            "ck_production_completions_initial_disposition", type_="check"
        )
        batch.drop_constraint(
            "ck_production_completions_disposition_targets", type_="check"
        )
        batch.add_column(
            sa.Column(
                "completion_type",
                sa.String(length=20),
                server_default="primary",
                nullable=False,
            )
        )
        batch.add_column(sa.Column("material_input_quantity", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("planned_output_quantity", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("actual_output_quantity", sa.Integer(), nullable=True))
        batch.add_column(
            sa.Column("defective_quantity", sa.Integer(), server_default="0", nullable=False)
        )
        batch.add_column(
            sa.Column("order_reserved_quantity", sa.Integer(), server_default="0", nullable=False)
        )
        batch.add_column(
            sa.Column("direct_delivery_quantity", sa.Integer(), server_default="0", nullable=False)
        )
        batch.add_column(
            sa.Column("stock_quantity", sa.Integer(), server_default="0", nullable=False)
        )
        batch.add_column(
            sa.Column("surplus_finished_quantity", sa.Integer(), server_default="0", nullable=False)
        )

    op.execute(
        """
        UPDATE production_completions
        SET material_input_quantity = quantity,
            planned_output_quantity = quantity,
            actual_output_quantity = quantity,
            order_reserved_quantity = MIN(
                quantity,
                COALESCE(
                    (SELECT quantity FROM sales_order_items
                     WHERE id = production_completions.order_item_id),
                    quantity
                )
            ),
            direct_delivery_quantity =
                CASE WHEN initial_disposition = 'direct' THEN quantity ELSE 0 END,
            stock_quantity =
                CASE WHEN initial_disposition = 'stock' THEN quantity ELSE 0 END,
            surplus_finished_quantity = MAX(
                quantity - COALESCE(
                    (SELECT quantity FROM sales_order_items
                     WHERE id = production_completions.order_item_id),
                    quantity
                ),
                0
            )
        """
    )

    with op.batch_alter_table("production_completions") as batch:
        batch.alter_column("material_input_quantity", nullable=False)
        batch.alter_column("planned_output_quantity", nullable=False)
        batch.alter_column("actual_output_quantity", nullable=False)
        batch.create_index(
            "uq_production_completions_task_primary_active",
            ["task_id"],
            unique=True,
            sqlite_where=sa.text("status = 'posted' AND completion_type = 'primary'"),
            postgresql_where=sa.text("status = 'posted' AND completion_type = 'primary'"),
        )
        batch.create_check_constraint(
            "ck_production_completions_initial_disposition",
            "initial_disposition IN ('direct','stock','split')",
        )
        batch.create_check_constraint(
            "ck_production_completions_disposition_targets",
            "((initial_disposition = 'direct' AND warehouse_location_id IS NULL "
            "AND inventory_lot_id IS NULL AND stock_quantity = 0) "
            "OR (initial_disposition = 'stock' AND warehouse_location_id IS NOT NULL "
            "AND direct_delivery_quantity = 0) "
            "OR (initial_disposition = 'split' AND warehouse_location_id IS NOT NULL "
            "AND direct_delivery_quantity > 0 AND stock_quantity > 0))",
        )
        batch.create_check_constraint(
            "ck_production_completions_type",
            "completion_type IN ('primary','supplemental')",
        )
        batch.create_check_constraint(
            "ck_production_completions_output_quantities",
            "material_input_quantity > 0 AND planned_output_quantity > 0 "
            "AND actual_output_quantity > 0 AND defective_quantity >= 0",
        )
        batch.create_check_constraint(
            "ck_production_completions_quantity_conservation",
            "quantity = actual_output_quantity "
            "AND direct_delivery_quantity + stock_quantity = actual_output_quantity "
            "AND order_reserved_quantity >= 0 "
            "AND order_reserved_quantity <= actual_output_quantity "
            "AND surplus_finished_quantity = actual_output_quantity - order_reserved_quantity",
        )

    with op.batch_alter_table("sales_delivery_items") as batch:
        batch.add_column(
            sa.Column("ordered_quantity_snapshot", sa.Integer(), server_default="0", nullable=False)
        )
        batch.add_column(
            sa.Column("order_remaining_snapshot", sa.Integer(), server_default="0", nullable=False)
        )
        batch.add_column(
            sa.Column("over_delivery_quantity", sa.Integer(), server_default="0", nullable=False)
        )
        batch.add_column(
            sa.Column(
                "over_delivery_confirmed_by",
                sa.Integer(),
                nullable=True,
            )
        )
        batch.add_column(sa.Column("over_delivery_reason", sa.Text(), nullable=True))
        batch.create_foreign_key(
            "fk_sales_delivery_items_over_delivery_confirmed_by_users",
            "users",
            ["over_delivery_confirmed_by"],
            ["id"],
            ondelete="SET NULL",
        )

    op.execute(
        """
        UPDATE sales_delivery_items
        SET ordered_quantity_snapshot = COALESCE(
                (SELECT quantity FROM sales_order_items
                 WHERE id = sales_delivery_items.order_item_id),
                delivered_quantity
            ),
            order_remaining_snapshot = COALESCE(
                (SELECT MAX(quantity - delivered_quantity, 0)
                 FROM sales_order_items
                 WHERE id = sales_delivery_items.order_item_id),
                delivered_quantity
            )
        """
    )

    with op.batch_alter_table("inventory_reservations") as batch:
        batch.drop_constraint("ck_inventory_reservations_type", type_="check")
        batch.create_check_constraint(
            "ck_inventory_reservations_type",
            "reservation_type IN "
            "('finished_order','finished_surplus_delivery','semi_requisition','semi_order')",
        )


def downgrade() -> None:
    supplemental_count = int(
        op.get_bind()
        .execute(
            sa.text(
                "SELECT COUNT(*) FROM production_completions "
                "WHERE completion_type = 'supplemental' OR surplus_finished_quantity > 0"
            )
        )
        .scalar_one()
    )
    over_delivery_count = int(
        op.get_bind()
        .execute(
            sa.text(
                "SELECT COUNT(*) FROM sales_delivery_items "
                "WHERE over_delivery_quantity > 0"
            )
        )
        .scalar_one()
    )
    if supplemental_count or over_delivery_count:
        raise RuntimeError(
            "已有补充生产、余货或超量送货事实，禁止破坏性降级；请恢复升级前完整备份。"
        )

    with op.batch_alter_table("inventory_reservations") as batch:
        batch.drop_constraint("ck_inventory_reservations_type", type_="check")
        batch.create_check_constraint(
            "ck_inventory_reservations_type",
            "reservation_type IN ('finished_order','semi_requisition','semi_order')",
        )

    with op.batch_alter_table("sales_delivery_items") as batch:
        batch.drop_constraint(
            "fk_sales_delivery_items_over_delivery_confirmed_by_users",
            type_="foreignkey",
        )
        batch.drop_column("over_delivery_reason")
        batch.drop_column("over_delivery_confirmed_by")
        batch.drop_column("over_delivery_quantity")
        batch.drop_column("order_remaining_snapshot")
        batch.drop_column("ordered_quantity_snapshot")

    with op.batch_alter_table("production_completions") as batch:
        batch.drop_constraint(
            "ck_production_completions_quantity_conservation", type_="check"
        )
        batch.drop_constraint(
            "ck_production_completions_output_quantities", type_="check"
        )
        batch.drop_constraint("ck_production_completions_type", type_="check")
        batch.drop_constraint(
            "ck_production_completions_disposition_targets", type_="check"
        )
        batch.drop_constraint(
            "ck_production_completions_initial_disposition", type_="check"
        )
        batch.drop_index("uq_production_completions_task_primary_active")
        batch.create_index(
            "uq_production_completions_task_active",
            ["task_id"],
            unique=True,
            sqlite_where=sa.text("status = 'posted'"),
            postgresql_where=sa.text("status = 'posted'"),
        )
        batch.create_check_constraint(
            "ck_production_completions_initial_disposition",
            "initial_disposition IN ('direct','stock')",
        )
        batch.create_check_constraint(
            "ck_production_completions_disposition_targets",
            "((initial_disposition = 'direct' AND warehouse_location_id IS NULL "
            "AND inventory_lot_id IS NULL) OR (initial_disposition = 'stock' "
            "AND warehouse_location_id IS NOT NULL))",
        )
        batch.drop_column("surplus_finished_quantity")
        batch.drop_column("stock_quantity")
        batch.drop_column("direct_delivery_quantity")
        batch.drop_column("order_reserved_quantity")
        batch.drop_column("defective_quantity")
        batch.drop_column("actual_output_quantity")
        batch.drop_column("planned_output_quantity")
        batch.drop_column("material_input_quantity")
        batch.drop_column("completion_type")

    with op.batch_alter_table("production_tasks") as batch:
        batch.drop_column("output_factor")
        batch.drop_column("material_input_quantity")
        batch.drop_column("material_received_quantity")
        batch.drop_column("ordered_quantity_snapshot")
