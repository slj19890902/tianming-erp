"""add explicit composite business modes and order history fields

Revision ID: cp72v8x9z61
Revises: co71v8x9z60
Create Date: 2026-07-24
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "cp72v8x9z61"
down_revision: Union[str, Sequence[str], None] = "co71v8x9z60"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


PRODUCTS = "products"
ORDER_ITEMS = "sales_order_items"
SEMI_REQUIREMENTS = "order_item_semi_requirements"
MODES = "'parent_priced_set', 'component_priced'"
ROLES = "'standalone', 'set_parent', 'priced_component'"


def upgrade() -> None:
    with op.batch_alter_table(PRODUCTS) as batch:
        batch.add_column(
            sa.Column(
                "combination_mode",
                sa.String(length=30),
                server_default="parent_priced_set",
                nullable=False,
            )
        )
        batch.create_check_constraint(
            "ck_products_combination_mode",
            f"combination_mode IN ({MODES})",
        )

    with op.batch_alter_table(ORDER_ITEMS) as batch:
        batch.add_column(
            sa.Column("combination_mode_snapshot", sa.String(length=30), nullable=True)
        )
        batch.add_column(
            sa.Column(
                "combination_role",
                sa.String(length=30),
                server_default="standalone",
                nullable=False,
            )
        )
        batch.add_column(
            sa.Column("combination_group_key", sa.String(length=80), nullable=True)
        )
        batch.add_column(
            sa.Column("combination_parent_product_id", sa.Integer(), nullable=True)
        )
        batch.add_column(
            sa.Column(
                "combination_parent_name_snapshot", sa.String(length=250), nullable=True
            )
        )
        batch.add_column(
            sa.Column("combination_set_quantity_snapshot", sa.Integer(), nullable=True)
        )
        batch.add_column(
            sa.Column(
                "combination_quantity_per_set_snapshot", sa.Integer(), nullable=True
            )
        )
        batch.create_foreign_key(
            "fk_sales_order_items_combination_parent_product_id",
            "products",
            ["combination_parent_product_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch.create_check_constraint(
            "ck_sales_order_items_combination_mode_snapshot",
            f"combination_mode_snapshot IS NULL OR combination_mode_snapshot IN ({MODES})",
        )
        batch.create_check_constraint(
            "ck_sales_order_items_combination_role",
            f"combination_role IN ({ROLES})",
        )
        batch.create_check_constraint(
            "ck_sales_order_items_priced_component_source",
            "combination_role <> 'priced_component' OR "
            "(combination_mode_snapshot = 'component_priced' "
            "AND combination_group_key IS NOT NULL "
            "AND length(trim(combination_group_key)) > 0 "
            "AND combination_parent_product_id IS NOT NULL "
            "AND combination_parent_name_snapshot IS NOT NULL "
            "AND length(trim(combination_parent_name_snapshot)) > 0 "
            "AND combination_set_quantity_snapshot IS NOT NULL "
            "AND combination_set_quantity_snapshot > 0 "
            "AND combination_quantity_per_set_snapshot IS NOT NULL "
            "AND combination_quantity_per_set_snapshot > 0)",
        )
        batch.create_check_constraint(
            "ck_sales_order_items_standalone_without_combination_source",
            "combination_role <> 'standalone' OR "
            "(combination_mode_snapshot IS NULL "
            "AND combination_group_key IS NULL "
            "AND combination_parent_product_id IS NULL "
            "AND combination_parent_name_snapshot IS NULL "
            "AND combination_set_quantity_snapshot IS NULL "
            "AND combination_quantity_per_set_snapshot IS NULL)",
        )
        batch.create_check_constraint(
            "ck_sales_order_items_set_parent_source",
            "combination_role <> 'set_parent' OR "
            "(combination_mode_snapshot = 'parent_priced_set' "
            "AND combination_group_key IS NULL "
            "AND combination_parent_product_id IS NULL "
            "AND combination_parent_name_snapshot IS NULL "
            "AND combination_set_quantity_snapshot IS NULL "
            "AND combination_quantity_per_set_snapshot IS NULL)",
        )

    with op.batch_alter_table(SEMI_REQUIREMENTS) as batch:
        batch.drop_constraint(
            "uq_order_item_semi_requirements_item_component",
            type_="unique",
        )
    op.create_index(
        "uq_order_item_semi_requirements_regular_component",
        SEMI_REQUIREMENTS,
        ["order_item_id", "component_type"],
        unique=True,
        sqlite_where=sa.text("sales_order_item_bom_component_id IS NULL"),
        postgresql_where=sa.text("sales_order_item_bom_component_id IS NULL"),
    )
    op.create_index(
        "uq_order_item_semi_requirements_bom_component",
        SEMI_REQUIREMENTS,
        ["sales_order_item_bom_component_id"],
        unique=True,
        sqlite_where=sa.text("sales_order_item_bom_component_id IS NOT NULL"),
        postgresql_where=sa.text("sales_order_item_bom_component_id IS NOT NULL"),
    )


def downgrade() -> None:
    connection = op.get_bind()
    combination_provenance_count = int(
        connection.execute(
            sa.text(
                f"SELECT COUNT(*) FROM {ORDER_ITEMS} "
                "WHERE combination_mode_snapshot IS NOT NULL "
                "OR combination_role <> 'standalone' "
                "OR combination_group_key IS NOT NULL "
                "OR combination_parent_product_id IS NOT NULL "
                "OR combination_parent_name_snapshot IS NOT NULL "
                "OR combination_set_quantity_snapshot IS NOT NULL "
                "OR combination_quantity_per_set_snapshot IS NOT NULL"
            )
        ).scalar_one()
    )
    component_priced_product_count = int(
        connection.execute(
            sa.text(
                f"SELECT COUNT(*) FROM {PRODUCTS} "
                "WHERE combination_mode = 'component_priced'"
            )
        ).scalar_one()
    )
    bom_requirement_count = int(
        connection.execute(
            sa.text(
                f"SELECT COUNT(*) FROM {SEMI_REQUIREMENTS} "
                "WHERE sales_order_item_bom_component_id IS NOT NULL"
            )
        ).scalar_one()
    )
    if (
        combination_provenance_count
        or component_priced_product_count
        or bom_requirement_count
    ):
        raise RuntimeError(
            "已存在组合计价模板、订单或组件库存需求事实，"
            "禁止破坏性降级；请恢复升级前完整备份。"
        )

    op.drop_index(
        "uq_order_item_semi_requirements_bom_component",
        table_name=SEMI_REQUIREMENTS,
    )
    op.drop_index(
        "uq_order_item_semi_requirements_regular_component",
        table_name=SEMI_REQUIREMENTS,
    )
    with op.batch_alter_table(SEMI_REQUIREMENTS) as batch:
        batch.create_unique_constraint(
            "uq_order_item_semi_requirements_item_component",
            ["order_item_id", "component_type"],
        )

    with op.batch_alter_table(ORDER_ITEMS) as batch:
        batch.drop_constraint(
            "ck_sales_order_items_set_parent_source", type_="check"
        )
        batch.drop_constraint(
            "ck_sales_order_items_standalone_without_combination_source",
            type_="check",
        )
        batch.drop_constraint(
            "ck_sales_order_items_priced_component_source", type_="check"
        )
        batch.drop_constraint("ck_sales_order_items_combination_role", type_="check")
        batch.drop_constraint(
            "ck_sales_order_items_combination_mode_snapshot", type_="check"
        )
        batch.drop_constraint(
            "fk_sales_order_items_combination_parent_product_id",
            type_="foreignkey",
        )
        batch.drop_column("combination_quantity_per_set_snapshot")
        batch.drop_column("combination_set_quantity_snapshot")
        batch.drop_column("combination_parent_name_snapshot")
        batch.drop_column("combination_parent_product_id")
        batch.drop_column("combination_group_key")
        batch.drop_column("combination_role")
        batch.drop_column("combination_mode_snapshot")

    with op.batch_alter_table(PRODUCTS) as batch:
        batch.drop_constraint("ck_products_combination_mode", type_="check")
        batch.drop_column("combination_mode")
