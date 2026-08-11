"""freeze order supply mode and support direct external product requirements

Revision ID: ec11v8x9z00
Revises: eb10v8x9z99
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "ec11v8x9z00"
down_revision = "eb10v8x9z99"
branch_labels = None
depends_on = None


_ORDER_PROFILE_CHECK = (
    "((supply_mode_snapshot = 'external_purchase' "
    "AND external_packaging_category_code_snapshot IS NOT NULL "
    "AND external_packaging_specification_json_snapshot IS NOT NULL "
    "AND external_packaging_specification_summary_snapshot IS NOT NULL "
    "AND external_packaging_purchase_unit_snapshot IS NOT NULL "
    "AND external_packaging_candidate_snapshot_json IS NOT NULL "
    "AND external_packaging_product_version_snapshot IS NOT NULL) OR "
    "(supply_mode_snapshot <> 'external_purchase' "
    "AND external_packaging_category_code_snapshot IS NULL "
    "AND external_packaging_specification_json_snapshot IS NULL "
    "AND external_packaging_specification_summary_snapshot IS NULL "
    "AND external_packaging_purchase_unit_snapshot IS NULL "
    "AND external_packaging_candidate_snapshot_json IS NULL "
    "AND external_packaging_product_version_snapshot IS NULL))"
)


def upgrade() -> None:
    with op.batch_alter_table("sales_order_items") as batch:
        batch.add_column(
            sa.Column(
                "supply_mode_snapshot",
                sa.String(length=30),
                nullable=False,
                server_default="corrugated_production",
            )
        )
        batch.add_column(sa.Column("external_packaging_category_code_snapshot", sa.String(length=50), nullable=True))
        batch.add_column(sa.Column("external_packaging_specification_json_snapshot", sa.Text(), nullable=True))
        batch.add_column(sa.Column("external_packaging_specification_summary_snapshot", sa.String(length=500), nullable=True))
        batch.add_column(sa.Column("external_packaging_purchase_unit_snapshot", sa.String(length=20), nullable=True))
        batch.add_column(sa.Column("external_packaging_candidate_snapshot_json", sa.Text(), nullable=True))
        batch.add_column(sa.Column("external_packaging_product_version_snapshot", sa.Integer(), nullable=True))
        batch.create_check_constraint(
            "ck_sales_order_items_supply_mode_snapshot",
            "supply_mode_snapshot IN ('corrugated_production','external_purchase','mixed_bom')",
        )
        batch.create_check_constraint(
            "ck_sales_order_items_external_profile_snapshot",
            _ORDER_PROFILE_CHECK,
        )

    with op.batch_alter_table("sales_order_item_external_components") as batch:
        batch.add_column(
            sa.Column(
                "source_kind",
                sa.String(length=30),
                nullable=False,
                server_default="bound_component",
            )
        )
        batch.alter_column(
            "source_component_set_id",
            existing_type=sa.Integer(),
            nullable=True,
        )
        batch.alter_column(
            "source_component_id",
            existing_type=sa.Integer(),
            nullable=True,
        )
        batch.create_check_constraint(
            "ck_sales_order_item_external_component_source_kind",
            "source_kind IN ('bound_component','direct_product')",
        )
        batch.create_check_constraint(
            "ck_sales_order_item_external_component_source",
            "((source_kind = 'bound_component' AND source_component_set_id IS NOT NULL "
            "AND source_component_id IS NOT NULL) OR "
            "(source_kind = 'direct_product' AND source_component_set_id IS NULL "
            "AND source_component_id IS NULL))",
        )

    with op.batch_alter_table("sales_order_item_external_component_candidates") as batch:
        batch.alter_column(
            "source_candidate_id",
            existing_type=sa.Integer(),
            nullable=True,
        )


def downgrade() -> None:
    connection = op.get_bind()
    non_default = connection.execute(
        sa.text(
            "SELECT COUNT(*) FROM sales_order_items "
            "WHERE supply_mode_snapshot <> 'corrugated_production' "
            "OR external_packaging_category_code_snapshot IS NOT NULL "
            "OR external_packaging_specification_json_snapshot IS NOT NULL "
            "OR external_packaging_specification_summary_snapshot IS NOT NULL "
            "OR external_packaging_purchase_unit_snapshot IS NOT NULL "
            "OR external_packaging_candidate_snapshot_json IS NOT NULL "
            "OR external_packaging_product_version_snapshot IS NOT NULL"
        )
    ).scalar_one()
    direct_components = connection.execute(
        sa.text(
            "SELECT COUNT(*) FROM sales_order_item_external_components "
            "WHERE source_kind <> 'bound_component' "
            "OR source_component_set_id IS NULL OR source_component_id IS NULL"
        )
    ).scalar_one()
    direct_candidates = connection.execute(
        sa.text(
            "SELECT COUNT(*) FROM sales_order_item_external_component_candidates "
            "WHERE source_candidate_id IS NULL"
        )
    ).scalar_one()
    if int(non_default or 0) or int(direct_components or 0) or int(direct_candidates or 0):
        raise RuntimeError(
            "P1-40B 已存在订单供货方式或直接外购需求事实，拒绝降级删除不可恢复快照"
        )

    with op.batch_alter_table("sales_order_item_external_component_candidates") as batch:
        batch.alter_column(
            "source_candidate_id",
            existing_type=sa.Integer(),
            nullable=False,
        )

    with op.batch_alter_table("sales_order_item_external_components") as batch:
        batch.drop_constraint(
            "ck_sales_order_item_external_component_source",
            type_="check",
        )
        batch.drop_constraint(
            "ck_sales_order_item_external_component_source_kind",
            type_="check",
        )
        batch.alter_column(
            "source_component_id",
            existing_type=sa.Integer(),
            nullable=False,
        )
        batch.alter_column(
            "source_component_set_id",
            existing_type=sa.Integer(),
            nullable=False,
        )
        batch.drop_column("source_kind")

    with op.batch_alter_table("sales_order_items") as batch:
        batch.drop_constraint(
            "ck_sales_order_items_external_profile_snapshot",
            type_="check",
        )
        batch.drop_constraint(
            "ck_sales_order_items_supply_mode_snapshot",
            type_="check",
        )
        batch.drop_column("external_packaging_product_version_snapshot")
        batch.drop_column("external_packaging_candidate_snapshot_json")
        batch.drop_column("external_packaging_purchase_unit_snapshot")
        batch.drop_column("external_packaging_specification_summary_snapshot")
        batch.drop_column("external_packaging_specification_json_snapshot")
        batch.drop_column("external_packaging_category_code_snapshot")
        batch.drop_column("supply_mode_snapshot")