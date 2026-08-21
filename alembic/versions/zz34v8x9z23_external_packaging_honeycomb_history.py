"""honeycomb packaging profile, quantity conversion and purchase history

Revision ID: zz34v8x9z23
Revises: yy33v8x9z22
Create Date: 2026-08-21
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "zz34v8x9z23"
down_revision = "yy33v8x9z22"
branch_labels = None
depends_on = None


def _ensure_cancellation_immutable_guards() -> None:
    if op.get_bind().dialect.name != "sqlite":
        return
    table = "external_packaging_purchase_cancellations"
    for action in ("UPDATE", "DELETE"):
        op.execute(
            f"""
            CREATE TRIGGER IF NOT EXISTS trg_{table}_immutable_{action.lower()}
            BEFORE {action} ON {table}
            FOR EACH ROW
            BEGIN
                SELECT RAISE(ABORT, '{table} rows are immutable');
            END
            """
        )


def _drop_purchase_item_immutable_guards() -> None:
    if op.get_bind().dialect.name != "sqlite":
        return
    for action in ("update", "delete"):
        op.execute(
            f"DROP TRIGGER IF EXISTS "
            f"trg_external_packaging_purchase_items_immutable_{action}"
        )


def _ensure_purchase_item_immutable_guards() -> None:
    if op.get_bind().dialect.name != "sqlite":
        return
    table = "external_packaging_purchase_items"
    for action in ("UPDATE", "DELETE"):
        op.execute(
            f"""
            CREATE TRIGGER IF NOT EXISTS trg_{table}_immutable_{action.lower()}
            BEFORE {action} ON {table}
            FOR EACH ROW
            BEGIN
                SELECT RAISE(ABORT, '{table} rows are immutable');
            END
            """
        )


def upgrade() -> None:
    connection = op.get_bind()

    with op.batch_alter_table("supplier_supply_categories") as batch:
        batch.drop_constraint("ck_supplier_supply_categories_code", type_="check")
        batch.create_check_constraint(
            "ck_supplier_supply_categories_code",
            "category_code IN ('corrugated_board','paper_corner_guard','coated_board',"
            "'printed_folding_carton','epe_cushion','hollow_board','honeycomb_board',"
            "'other_packaging')",
        )
    with op.batch_alter_table("external_packaging_products") as batch:
        batch.drop_constraint("ck_external_packaging_products_category", type_="check")
        batch.create_check_constraint(
            "ck_external_packaging_products_category",
            "category_code IN ('paper_corner_guard','coated_board',"
            "'printed_folding_carton','epe_cushion','hollow_board','honeycomb_board',"
            "'other_packaging')",
        )

    with op.batch_alter_table("sales_order_items") as batch:
        batch.add_column(
            sa.Column(
                "external_packaging_order_quantity_basis_snapshot",
                sa.Numeric(18, 6),
                nullable=True,
            )
        )
        batch.add_column(
            sa.Column(
                "external_packaging_purchase_quantity_basis_snapshot",
                sa.Numeric(18, 6),
                nullable=True,
            )
        )
        batch.add_column(
            sa.Column(
                "external_packaging_quantity_per_finished_unit_snapshot",
                sa.Numeric(18, 6),
                nullable=True,
            )
        )
        batch.create_check_constraint(
            "ck_sales_order_items_external_packaging_quantity_snapshot",
            "external_packaging_quantity_per_finished_unit_snapshot IS NULL OR "
            "external_packaging_quantity_per_finished_unit_snapshot > 0",
        )
        batch.create_check_constraint(
            "ck_sales_order_items_external_order_basis_snapshot",
            "external_packaging_order_quantity_basis_snapshot IS NULL OR "
            "external_packaging_order_quantity_basis_snapshot > 0",
        )
        batch.create_check_constraint(
            "ck_sales_order_items_external_purchase_basis_snapshot",
            "external_packaging_purchase_quantity_basis_snapshot IS NULL OR "
            "external_packaging_purchase_quantity_basis_snapshot > 0",
        )
    connection.execute(
        sa.text(
            "UPDATE sales_order_items "
            "SET external_packaging_order_quantity_basis_snapshot = 1, "
            "external_packaging_purchase_quantity_basis_snapshot = 1, "
            "external_packaging_quantity_per_finished_unit_snapshot = 1 "
            "WHERE supply_mode_snapshot = 'external_purchase'"
        )
    )
    with op.batch_alter_table("sales_order_items") as batch:
        batch.create_check_constraint(
            "ck_sales_order_items_external_purchase_ratio_snapshot",
            "((supply_mode_snapshot = 'external_purchase' "
            "AND external_packaging_order_quantity_basis_snapshot IS NOT NULL "
            "AND external_packaging_purchase_quantity_basis_snapshot IS NOT NULL "
            "AND external_packaging_quantity_per_finished_unit_snapshot IS NOT NULL) OR "
            "(supply_mode_snapshot <> 'external_purchase' "
            "AND external_packaging_order_quantity_basis_snapshot IS NULL "
            "AND external_packaging_purchase_quantity_basis_snapshot IS NULL "
            "AND external_packaging_quantity_per_finished_unit_snapshot IS NULL))",
        )
    _drop_purchase_item_immutable_guards()
    with op.batch_alter_table("external_packaging_purchase_items") as batch:
        batch.add_column(
            sa.Column(
                "specification_json_snapshot",
                sa.Text(),
                nullable=True,
            )
        )
    connection.execute(
        sa.text(
            "UPDATE external_packaging_purchase_items AS item "
            "SET specification_json_snapshot = COALESCE(("
            "SELECT component.specification_json "
            "FROM sales_order_item_external_components AS component "
            "WHERE component.id = item.order_component_id), '{}')"
        )
    )
    with op.batch_alter_table("external_packaging_purchase_items") as batch:
        batch.alter_column(
            "specification_json_snapshot",
            existing_type=sa.Text(),
            nullable=False,
        )
        batch.drop_constraint(
            "uq_external_packaging_purchase_component", type_="unique"
        )
        batch.create_unique_constraint(
            "uq_external_packaging_purchase_order_component",
            ["purchase_order_id", "order_component_id"],
        )
    _ensure_purchase_item_immutable_guards()

    with op.batch_alter_table("external_packaging_purchase_batches") as batch:
        batch.drop_constraint(
            "uq_external_packaging_purchase_batch_order", type_="unique"
        )

    with op.batch_alter_table("external_packaging_purchase_cancellations") as batch:
        batch.drop_constraint(
            "ck_external_packaging_purchase_cancellation_source", type_="check"
        )
        batch.create_check_constraint(
            "ck_external_packaging_purchase_cancellation_source",
            "source IN ('order_workflow_rollback','order_status_cancelled',"
            "'order_status_dead','authorized_data_repair','manual_purchase_cancel')",
        )
    _ensure_cancellation_immutable_guards()


def downgrade() -> None:
    connection = op.get_bind()
    blockers = {
        "honeycomb products": "SELECT COUNT(*) FROM external_packaging_products WHERE category_code='honeycomb_board'",
        "honeycomb supplier categories": "SELECT COUNT(*) FROM supplier_supply_categories WHERE category_code='honeycomb_board'",
        "non-default order purchase conversions": (
            "SELECT COUNT(*) FROM sales_order_items "
            "WHERE supply_mode_snapshot='external_purchase' "
            "AND (external_packaging_order_quantity_basis_snapshot <> 1 "
            "OR external_packaging_purchase_quantity_basis_snapshot <> 1)"
        ),
        "manual cancellations": (
            "SELECT COUNT(*) FROM external_packaging_purchase_cancellations "
            "WHERE source='manual_purchase_cancel'"
        ),
        "replacement purchase batches": (
            "SELECT COUNT(*) FROM (SELECT sales_order_id FROM external_packaging_purchase_batches "
            "GROUP BY sales_order_id HAVING COUNT(*) > 1)"
        ),
    }
    for label, statement in blockers.items():
        if int(connection.execute(sa.text(statement)).scalar_one() or 0):
            raise RuntimeError(f"cannot downgrade after {label} exist")

    with op.batch_alter_table("external_packaging_purchase_cancellations") as batch:
        batch.drop_constraint(
            "ck_external_packaging_purchase_cancellation_source", type_="check"
        )
        batch.create_check_constraint(
            "ck_external_packaging_purchase_cancellation_source",
            "source IN ('order_workflow_rollback','order_status_cancelled',"
            "'order_status_dead','authorized_data_repair')",
        )
    _ensure_cancellation_immutable_guards()
    with op.batch_alter_table("external_packaging_purchase_batches") as batch:
        batch.create_unique_constraint(
            "uq_external_packaging_purchase_batch_order", ["sales_order_id"]
        )
    _drop_purchase_item_immutable_guards()
    with op.batch_alter_table("external_packaging_purchase_items") as batch:
        batch.drop_constraint(
            "uq_external_packaging_purchase_order_component", type_="unique"
        )
        batch.create_unique_constraint(
            "uq_external_packaging_purchase_component", ["order_component_id"]
        )
        batch.drop_column("specification_json_snapshot")
    _ensure_purchase_item_immutable_guards()
    with op.batch_alter_table("sales_order_items") as batch:
        batch.drop_constraint(
            "ck_sales_order_items_external_purchase_ratio_snapshot", type_="check"
        )
        batch.drop_constraint(
            "ck_sales_order_items_external_purchase_basis_snapshot", type_="check"
        )
        batch.drop_constraint(
            "ck_sales_order_items_external_order_basis_snapshot", type_="check"
        )
        batch.drop_constraint(
            "ck_sales_order_items_external_packaging_quantity_snapshot", type_="check"
        )
        batch.drop_column("external_packaging_quantity_per_finished_unit_snapshot")
        batch.drop_column("external_packaging_purchase_quantity_basis_snapshot")
        batch.drop_column("external_packaging_order_quantity_basis_snapshot")
    with op.batch_alter_table("external_packaging_products") as batch:
        batch.drop_constraint("ck_external_packaging_products_category", type_="check")
        batch.create_check_constraint(
            "ck_external_packaging_products_category",
            "category_code IN ('paper_corner_guard','coated_board',"
            "'printed_folding_carton','epe_cushion','hollow_board','other_packaging')",
        )
    with op.batch_alter_table("supplier_supply_categories") as batch:
        batch.drop_constraint("ck_supplier_supply_categories_code", type_="check")
        batch.create_check_constraint(
            "ck_supplier_supply_categories_code",
            "category_code IN ('corrugated_board','paper_corner_guard','coated_board',"
            "'printed_folding_carton','epe_cushion','hollow_board','other_packaging')",
        )
