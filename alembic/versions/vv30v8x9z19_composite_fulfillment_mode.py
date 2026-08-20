"""freeze composite fulfilment mode and parent label policy

Revision ID: vv30v8x9z19
Revises: uu29v8x9z18
Create Date: 2026-08-19
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "vv30v8x9z19"
down_revision = "uu29v8x9z18"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("products") as batch:
        batch.add_column(
            sa.Column(
                "composite_fulfillment_mode",
                sa.String(length=30),
                nullable=True,
                server_default="component_delivery",
            )
        )

    connection = op.get_bind()
    connection.execute(
        sa.text(
            """
            UPDATE products
            SET composite_fulfillment_mode = CASE
                WHEN is_composite = 0 THEN 'component_delivery'
                WHEN combination_mode = 'component_priced' THEN 'component_delivery'
                WHEN EXISTS (
                    SELECT 1
                    FROM product_bom_components AS relation
                    WHERE relation.parent_product_id = products.id
                      AND relation.show_on_delivery = 1
                ) THEN 'component_delivery'
                WHEN EXISTS (
                    SELECT 1
                    FROM product_bom_components AS relation
                    JOIN products AS component
                      ON component.id = relation.component_product_id
                    WHERE relation.parent_product_id = products.id
                      AND component.production_label_enabled = 1
                ) THEN 'component_delivery'
                WHEN production_label_enabled = 1 THEN 'parent_delivery'
                ELSE 'parent_delivery'
            END
            """
        )
    )

    with op.batch_alter_table("production_packaging_label_print_jobs") as batch:
        batch.alter_column(
            "supplier_order_id", existing_type=sa.Integer(), nullable=True
        )
        batch.add_column(
            sa.Column(
                "material_requisition_id",
                sa.Integer(),
                nullable=True,
            )
        )
        batch.create_foreign_key(
            "fk_production_packaging_label_print_jobs_material_requisition_id",
            "material_requisitions",
            ["material_requisition_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.create_check_constraint(
            "ck_production_packaging_label_print_jobs_source",
            "((supplier_order_id IS NOT NULL AND material_requisition_id IS NULL) OR "
            "(supplier_order_id IS NULL AND material_requisition_id IS NOT NULL))",
        )
        batch.create_index(
            "ix_production_packaging_label_print_jobs_requisition_created",
            ["material_requisition_id", "created_at"],
            unique=False,
        )
    with op.batch_alter_table("products") as batch:
        batch.alter_column(
            "composite_fulfillment_mode",
            existing_type=sa.String(length=30),
            nullable=False,
            server_default="component_delivery",
        )
        batch.create_check_constraint(
            "ck_products_composite_fulfillment_mode",
            "composite_fulfillment_mode IN ('parent_delivery','component_delivery')",
        )

    with op.batch_alter_table("sales_order_items") as batch:
        batch.add_column(sa.Column("composite_fulfillment_mode_snapshot", sa.String(length=30), nullable=True))
        batch.add_column(sa.Column("parent_production_label_enabled_snapshot", sa.Boolean(), nullable=True))
        batch.add_column(sa.Column("parent_production_label_units_per_label_snapshot", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("parent_production_label_template_version_snapshot", sa.String(length=40), nullable=True))
        batch.add_column(sa.Column("parent_production_label_product_version_snapshot", sa.Integer(), nullable=True))
        batch.create_check_constraint(
            "ck_sales_order_items_composite_fulfillment_mode_snapshot",
            "composite_fulfillment_mode_snapshot IS NULL OR "
            "composite_fulfillment_mode_snapshot IN ('parent_delivery','component_delivery')",
        )

    connection.execute(
        sa.text(
            """
            UPDATE sales_order_items
            SET composite_fulfillment_mode_snapshot = CASE
                    WHEN combination_role = 'priced_component' THEN 'component_delivery'
                    WHEN combination_role = 'set_parent'
                      OR EXISTS (
                        SELECT 1
                        FROM sales_order_item_bom_components AS snapshot
                        WHERE snapshot.sales_order_item_id = sales_order_items.id
                      ) THEN COALESCE(
                        (SELECT product.composite_fulfillment_mode
                         FROM products AS product
                         WHERE product.id = sales_order_items.product_id),
                        'component_delivery'
                    )
                    ELSE NULL
                END,
                parent_production_label_enabled_snapshot = CASE
                    WHEN combination_role <> 'priced_component'
                     AND (
                        combination_role = 'set_parent'
                        OR EXISTS (
                            SELECT 1
                            FROM sales_order_item_bom_components AS snapshot
                            WHERE snapshot.sales_order_item_id = sales_order_items.id
                        )
                     )
                     AND COALESCE((
                        SELECT product.composite_fulfillment_mode
                        FROM products AS product
                        WHERE product.id = sales_order_items.product_id
                     ), 'component_delivery') = 'parent_delivery'
                    THEN COALESCE(
                        (SELECT product.production_label_enabled
                         FROM products AS product
                         WHERE product.id = sales_order_items.product_id), 0)
                    ELSE NULL END,
                parent_production_label_units_per_label_snapshot = CASE
                    WHEN combination_role <> 'priced_component'
                     AND (
                        combination_role = 'set_parent'
                        OR EXISTS (
                            SELECT 1
                            FROM sales_order_item_bom_components AS snapshot
                            WHERE snapshot.sales_order_item_id = sales_order_items.id
                        )
                     )
                     AND COALESCE((
                        SELECT product.composite_fulfillment_mode
                        FROM products AS product
                        WHERE product.id = sales_order_items.product_id
                     ), 'component_delivery') = 'parent_delivery'
                    THEN (
                        SELECT product.production_label_units_per_label
                        FROM products AS product
                        WHERE product.id = sales_order_items.product_id)
                    ELSE NULL END,
                parent_production_label_template_version_snapshot = CASE
                    WHEN combination_role <> 'priced_component'
                     AND (
                        combination_role = 'set_parent'
                        OR EXISTS (
                            SELECT 1
                            FROM sales_order_item_bom_components AS snapshot
                            WHERE snapshot.sales_order_item_id = sales_order_items.id
                        )
                     ) THEN 'current_40x30_v2'
                    ELSE NULL END,
                parent_production_label_product_version_snapshot = CASE
                    WHEN combination_role <> 'priced_component'
                     AND (
                        combination_role = 'set_parent'
                        OR EXISTS (
                            SELECT 1
                            FROM sales_order_item_bom_components AS snapshot
                            WHERE snapshot.sales_order_item_id = sales_order_items.id
                        )
                     ) THEN (
                        SELECT product.version FROM products AS product
                        WHERE product.id = sales_order_items.product_id)
                    ELSE NULL END
            """
        )
    )


def downgrade() -> None:
    connection = op.get_bind()
    composite_jobs = int(
        connection.execute(
            sa.text(
                "SELECT COUNT(*) FROM production_packaging_label_print_jobs "
                "WHERE material_requisition_id IS NOT NULL"
            )
        ).scalar_one()
    )
    if composite_jobs:
        raise RuntimeError("cannot downgrade after composite requisition label jobs exist")
    changed_facts = int(
        connection.execute(
            sa.text(
                """
                SELECT COUNT(*)
                FROM sales_order_items AS item
                LEFT JOIN products AS product ON product.id = item.product_id
                WHERE (
                    item.combination_role IN ('set_parent','priced_component')
                    OR EXISTS (
                        SELECT 1
                        FROM sales_order_item_bom_components AS snapshot
                        WHERE snapshot.sales_order_item_id = item.id
                    )
                  )
                  AND (
                    item.composite_fulfillment_mode_snapshot <> CASE
                        WHEN item.combination_role = 'priced_component' THEN 'component_delivery'
                        ELSE COALESCE(product.composite_fulfillment_mode, 'component_delivery') END
                    OR COALESCE(item.parent_production_label_enabled_snapshot, 0)
                       <> CASE WHEN item.combination_role <> 'priced_component'
                                AND (
                                    item.combination_role = 'set_parent'
                                    OR EXISTS (
                                        SELECT 1
                                        FROM sales_order_item_bom_components AS snapshot
                                        WHERE snapshot.sales_order_item_id = item.id
                                    )
                                )
                                AND COALESCE(product.composite_fulfillment_mode, 'component_delivery') = 'parent_delivery'
                               THEN COALESCE(product.production_label_enabled, 0) ELSE 0 END
                    OR COALESCE(item.parent_production_label_units_per_label_snapshot, -1)
                       <> CASE WHEN item.combination_role <> 'priced_component'
                                AND (
                                    item.combination_role = 'set_parent'
                                    OR EXISTS (
                                        SELECT 1
                                        FROM sales_order_item_bom_components AS snapshot
                                        WHERE snapshot.sales_order_item_id = item.id
                                    )
                                )
                                AND COALESCE(product.composite_fulfillment_mode, 'component_delivery') = 'parent_delivery'
                               THEN COALESCE(product.production_label_units_per_label, -1) ELSE -1 END
                  )
                """
            )
        ).scalar_one()
    )
    if changed_facts:
        raise RuntimeError("cannot downgrade after order-specific composite fulfilment facts changed")

    with op.batch_alter_table("production_packaging_label_print_jobs") as batch:
        batch.drop_index("ix_production_packaging_label_print_jobs_requisition_created")
        batch.drop_constraint(
            "ck_production_packaging_label_print_jobs_source", type_="check"
        )
        batch.drop_constraint(
            "fk_production_packaging_label_print_jobs_material_requisition_id",
            type_="foreignkey",
        )
        batch.drop_column("material_requisition_id")
        batch.alter_column(
            "supplier_order_id", existing_type=sa.Integer(), nullable=False
        )

    with op.batch_alter_table("sales_order_items") as batch:
        batch.drop_constraint("ck_sales_order_items_composite_fulfillment_mode_snapshot", type_="check")
        batch.drop_column("parent_production_label_product_version_snapshot")
        batch.drop_column("parent_production_label_template_version_snapshot")
        batch.drop_column("parent_production_label_units_per_label_snapshot")
        batch.drop_column("parent_production_label_enabled_snapshot")
        batch.drop_column("composite_fulfillment_mode_snapshot")

    with op.batch_alter_table("products") as batch:
        batch.drop_constraint("ck_products_composite_fulfillment_mode", type_="check")
        batch.drop_column("composite_fulfillment_mode")
