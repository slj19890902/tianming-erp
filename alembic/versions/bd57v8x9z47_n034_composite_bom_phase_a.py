"""N034 Phase A: composite products and immutable BOM order snapshots.

Revision ID: bd57v8x9z47
Revises: bc56v8x9z46

Composite BOM rows are internal planning data.  Sales orders remain one parent
product item; this revision adds neither child sales-order items nor any
delivery, accounting, production, inventory, or ledger quantity mutation.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "bd57v8x9z47"
down_revision: Union[str, Sequence[str], None] = "bc56v8x9z46"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


PRODUCT_BOM_TABLE = "product_bom_components"
ORDER_BOM_SNAPSHOT_TABLE = "sales_order_item_bom_components"
REQUISITION_SOURCE_TABLE = "requisition_item_bom_sources"
SNAPSHOT_UPDATE_TRIGGER = "trg_sales_order_item_bom_components_immutable_update"
SNAPSHOT_SOURCE_TRIGGER = "trg_sales_order_item_bom_components_source_unlink_only"
SNAPSHOT_IMMUTABLE_FUNCTION = "n034_immutable_sales_order_item_bom_component"
DOWNGRADE_BLOCKED_MESSAGE = (
    "N034 复合产品 BOM 或订单快照事实已产生，禁止破坏性降级；"
    "请停止服务并恢复 bd57 升级前的完整数据库备份。"
)


def _create_snapshot_immutability_guard() -> None:
    connection = op.get_bind()
    dialect = connection.dialect.name
    if dialect == "sqlite":
        fact_columns = [
            column["name"]
            for column in sa.inspect(connection).get_columns(ORDER_BOM_SNAPSHOT_TABLE)
            if column["name"] != "product_bom_component_id"
        ]
        update_columns = ", ".join(f'"{column}"' for column in fact_columns)
        op.execute(
            f"""
            CREATE TRIGGER {SNAPSHOT_UPDATE_TRIGGER}
            BEFORE UPDATE OF {update_columns} ON {ORDER_BOM_SNAPSHOT_TABLE}
            FOR EACH ROW
            BEGIN
                SELECT RAISE(
                    ABORT,
                    'sales_order_item_bom_components snapshots are immutable'
                );
            END
            """
        )
        op.execute(
            f"""
            CREATE TRIGGER {SNAPSHOT_SOURCE_TRIGGER}
            BEFORE UPDATE OF product_bom_component_id ON {ORDER_BOM_SNAPSHOT_TABLE}
            FOR EACH ROW
            WHEN NOT (
                OLD.product_bom_component_id IS NOT NULL
                AND NEW.product_bom_component_id IS NULL
            )
            BEGIN
                SELECT RAISE(
                    ABORT,
                    'snapshot source may only be unlinked'
                );
            END
            """
        )
    elif dialect == "postgresql":
        op.execute(
            f"""
            CREATE OR REPLACE FUNCTION {SNAPSHOT_IMMUTABLE_FUNCTION}()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF OLD.product_bom_component_id IS NOT NULL
                   AND NEW.product_bom_component_id IS NULL
                   AND to_jsonb(NEW) - 'product_bom_component_id'
                       = to_jsonb(OLD) - 'product_bom_component_id'
                THEN
                    RETURN NEW;
                END IF;
                RAISE EXCEPTION 'sales_order_item_bom_components snapshots are immutable';
            END;
            $$
            """
        )
        op.execute(
            f"""
            CREATE TRIGGER {SNAPSHOT_UPDATE_TRIGGER}
            BEFORE UPDATE ON {ORDER_BOM_SNAPSHOT_TABLE}
            FOR EACH ROW EXECUTE FUNCTION {SNAPSHOT_IMMUTABLE_FUNCTION}()
            """
        )


def _drop_snapshot_immutability_guard() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        op.execute(f"DROP TRIGGER IF EXISTS {SNAPSHOT_SOURCE_TRIGGER}")
        op.execute(f"DROP TRIGGER IF EXISTS {SNAPSHOT_UPDATE_TRIGGER}")
    elif dialect == "postgresql":
        op.execute(
            f"DROP TRIGGER IF EXISTS {SNAPSHOT_UPDATE_TRIGGER} "
            f"ON {ORDER_BOM_SNAPSHOT_TABLE}"
        )
        op.execute(f"DROP FUNCTION IF EXISTS {SNAPSHOT_IMMUTABLE_FUNCTION}()")


def _assert_safe_downgrade(connection: sa.Connection) -> None:
    rows_with_facts = sum(
        int(
            connection.execute(sa.text(f"SELECT COUNT(*) FROM {table_name}")).scalar_one()
            or 0
        )
        for table_name in (
            PRODUCT_BOM_TABLE,
            ORDER_BOM_SNAPSHOT_TABLE,
            REQUISITION_SOURCE_TABLE,
        )
    )
    flagged_products = connection.execute(
        sa.text(
            """
            SELECT COUNT(*)
            FROM products
            WHERE is_composite IS TRUE OR is_internal_component IS TRUE
            """
        )
    ).scalar_one()
    if rows_with_facts or flagged_products:
        raise RuntimeError(DOWNGRADE_BLOCKED_MESSAGE)


def upgrade() -> None:
    with op.batch_alter_table("products") as batch_op:
        batch_op.add_column(
            sa.Column(
                "is_composite",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch_op.add_column(
            sa.Column(
                "is_internal_component",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )

    op.create_index("ix_products_is_composite", "products", ["is_composite"])
    op.create_index(
        "ix_products_is_internal_component",
        "products",
        ["is_internal_component"],
    )

    op.create_table(
        PRODUCT_BOM_TABLE,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("parent_product_id", sa.Integer(), nullable=False),
        sa.Column("component_product_id", sa.Integer(), nullable=False),
        sa.Column("quantity_per_set", sa.Numeric(14, 4), nullable=False),
        sa.Column("display_order", sa.Integer(), nullable=False),
        sa.Column("internal_component_code", sa.String(150), nullable=False),
        sa.Column(
            "is_die_cut",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("die_cut_path", sa.Text(), nullable=True),
        sa.Column("mold_tool_id", sa.Integer(), nullable=True),
        sa.Column("mold_max_yield_per_sheet", sa.Integer(), nullable=True),
        sa.Column(
            "spare_sheet_quantity",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "display_mode",
            sa.String(30),
            nullable=False,
            server_default="internal_only",
        ),
        sa.Column(
            "is_required",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column("remark", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["parent_product_id"], ["products.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["component_product_id"], ["products.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["mold_tool_id"], ["mold_tools.id"], ondelete="RESTRICT"
        ),
        sa.CheckConstraint(
            "parent_product_id <> component_product_id",
            name="ck_product_bom_components_distinct_products",
        ),
        sa.CheckConstraint(
            "quantity_per_set > 0",
            name="ck_product_bom_components_quantity_per_set",
        ),
        sa.CheckConstraint(
            "display_order >= 0",
            name="ck_product_bom_components_display_order",
        ),
        sa.CheckConstraint(
            "length(trim(internal_component_code)) > 0",
            name="ck_product_bom_components_internal_component_code",
        ),
        sa.CheckConstraint(
            "mold_max_yield_per_sheet IS NULL OR mold_max_yield_per_sheet > 0",
            name="ck_product_bom_components_mold_yield",
        ),
        sa.CheckConstraint(
            "spare_sheet_quantity >= 0",
            name="ck_product_bom_components_spare_sheets",
        ),
        sa.CheckConstraint(
            "display_mode IN ('internal_only', 'show_on_delivery', 'show_on_all_docs')",
            name="ck_product_bom_components_display_mode",
        ),
        sa.CheckConstraint(
            "is_die_cut IS TRUE OR (mold_tool_id IS NULL "
            "AND mold_max_yield_per_sheet IS NULL)",
            name="ck_product_bom_components_non_die_cut_mold_fields",
        ),
        sa.CheckConstraint(
            "is_die_cut IS FALSE OR mold_tool_id IS NOT NULL",
            name="ck_product_bom_components_die_cut_mold_required",
        ),
        sa.UniqueConstraint(
            "parent_product_id",
            "component_product_id",
            name="uq_product_bom_components_parent_component",
        ),
        sa.UniqueConstraint(
            "parent_product_id",
            "display_order",
            name="uq_product_bom_components_parent_display_order",
        ),
    )
    op.create_index(
        "ix_product_bom_components_parent_display_order",
        PRODUCT_BOM_TABLE,
        ["parent_product_id", "display_order"],
    )
    op.create_index(
        "ix_product_bom_components_component_product_id",
        PRODUCT_BOM_TABLE,
        ["component_product_id"],
    )
    op.create_index(
        "ix_product_bom_components_mold_tool_id",
        PRODUCT_BOM_TABLE,
        ["mold_tool_id"],
    )

    op.create_table(
        ORDER_BOM_SNAPSHOT_TABLE,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("sales_order_item_id", sa.Integer(), nullable=False),
        sa.Column("product_bom_component_id", sa.Integer(), nullable=True),
        sa.Column("component_product_id", sa.Integer(), nullable=False),
        sa.Column("order_set_quantity", sa.Integer(), nullable=False),
        sa.Column("quantity_per_set", sa.Numeric(14, 4), nullable=False),
        sa.Column("required_piece_quantity", sa.Numeric(14, 4), nullable=False),
        sa.Column("display_order", sa.Integer(), nullable=False),
        sa.Column("internal_component_code", sa.String(150), nullable=False),
        sa.Column("is_die_cut", sa.Boolean(), nullable=False),
        sa.Column("snapshot_die_cut_path", sa.Text(), nullable=True),
        sa.Column("snapshot_mold_tool_id", sa.Integer(), nullable=True),
        sa.Column("snapshot_mold_tool_code", sa.String(120), nullable=True),
        sa.Column("snapshot_mold_tool_name", sa.String(250), nullable=True),
        sa.Column("mold_max_yield_per_sheet", sa.Integer(), nullable=True),
        sa.Column("spare_sheet_quantity", sa.Integer(), nullable=False),
        sa.Column("display_mode", sa.String(30), nullable=False),
        sa.Column("is_required", sa.Boolean(), nullable=False),
        sa.Column("remark", sa.Text(), nullable=True),
        sa.Column("snapshot_component_product_code", sa.String(150), nullable=False),
        sa.Column("snapshot_component_product_name", sa.String(250), nullable=False),
        sa.Column("snapshot_component_spec", sa.String(150), nullable=True),
        sa.Column("snapshot_component_material", sa.String(250), nullable=True),
        sa.Column("snapshot_component_material_id", sa.Integer(), nullable=True),
        sa.Column("snapshot_component_supplier_name", sa.String(200), nullable=True),
        sa.Column("snapshot_component_layer_count", sa.Integer(), nullable=True),
        sa.Column("snapshot_component_flute_type", sa.String(20), nullable=True),
        sa.Column("snapshot_component_box_category", sa.String(20), nullable=False),
        sa.Column("snapshot_component_box_style", sa.String(150), nullable=True),
        sa.Column("snapshot_component_production_process", sa.Text(), nullable=True),
        sa.Column("snapshot_component_report_length_mm", sa.Integer(), nullable=True),
        sa.Column("snapshot_component_report_width_mm", sa.Integer(), nullable=True),
        sa.Column("snapshot_component_crease_type", sa.String(20), nullable=True),
        sa.Column("snapshot_component_crease_left_mm", sa.Integer(), nullable=True),
        sa.Column("snapshot_component_crease_middle_mm", sa.Integer(), nullable=True),
        sa.Column("snapshot_component_crease_right_mm", sa.Integer(), nullable=True),
        sa.Column("snapshot_component_report_notes", sa.Text(), nullable=True),
        sa.Column("snapshot_component_base_report_length_mm", sa.Integer(), nullable=True),
        sa.Column("snapshot_component_base_report_width_mm", sa.Integer(), nullable=True),
        sa.Column("snapshot_component_base_crease_type", sa.String(20), nullable=True),
        sa.Column("snapshot_component_base_crease_left_mm", sa.Integer(), nullable=True),
        sa.Column("snapshot_component_base_crease_middle_mm", sa.Integer(), nullable=True),
        sa.Column("snapshot_component_base_crease_right_mm", sa.Integer(), nullable=True),
        sa.Column("snapshot_component_base_report_notes", sa.Text(), nullable=True),
        sa.Column("snapshot_component_splice_mode", sa.String(20), nullable=True),
        sa.Column("snapshot_component_pieces_per_box", sa.Integer(), nullable=True),
        sa.Column("snapshot_component_flap_mm", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.ForeignKeyConstraint(
            ["sales_order_item_id"], ["sales_order_items.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["product_bom_component_id"],
            [f"{PRODUCT_BOM_TABLE}.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["component_product_id"], ["products.id"], ondelete="RESTRICT"
        ),
        sa.CheckConstraint(
            "quantity_per_set > 0",
            name="ck_sales_order_item_bom_components_quantity_per_set",
        ),
        sa.CheckConstraint(
            "order_set_quantity > 0",
            name="ck_sales_order_item_bom_components_order_set_quantity",
        ),
        sa.CheckConstraint(
            "required_piece_quantity > 0",
            name="ck_sales_order_item_bom_components_required_piece_quantity",
        ),
        sa.CheckConstraint(
            "required_piece_quantity = order_set_quantity * quantity_per_set",
            name="ck_sales_order_item_bom_components_required_piece_formula",
        ),
        sa.CheckConstraint(
            "display_order >= 0",
            name="ck_sales_order_item_bom_components_display_order",
        ),
        sa.CheckConstraint(
            "length(trim(internal_component_code)) > 0",
            name="ck_sales_order_item_bom_components_internal_component_code",
        ),
        sa.CheckConstraint(
            "mold_max_yield_per_sheet IS NULL OR mold_max_yield_per_sheet > 0",
            name="ck_sales_order_item_bom_components_mold_yield",
        ),
        sa.CheckConstraint(
            "spare_sheet_quantity >= 0",
            name="ck_sales_order_item_bom_components_spare_sheets",
        ),
        sa.CheckConstraint(
            "display_mode IN ('internal_only', 'show_on_delivery', 'show_on_all_docs')",
            name="ck_sales_order_item_bom_components_display_mode",
        ),
        sa.CheckConstraint(
            "is_die_cut IS TRUE OR (snapshot_mold_tool_id IS NULL "
            "AND mold_max_yield_per_sheet IS NULL)",
            name="ck_sales_order_item_bom_components_non_die_cut_mold_fields",
        ),
        sa.CheckConstraint(
            "is_die_cut IS FALSE OR snapshot_mold_tool_id IS NOT NULL",
            name="ck_sales_order_item_bom_components_die_cut_mold_required",
        ),
        sa.UniqueConstraint(
            "sales_order_item_id",
            "display_order",
            name="uq_sales_order_item_bom_components_item_display_order",
        ),
        sa.UniqueConstraint(
            "sales_order_item_id",
            "product_bom_component_id",
            name="uq_sales_order_item_bom_components_item_source",
        ),
    )
    op.create_index(
        "ix_sales_order_item_bom_components_item_display_order",
        ORDER_BOM_SNAPSHOT_TABLE,
        ["sales_order_item_id", "display_order"],
    )
    op.create_index(
        "ix_sales_order_item_bom_components_component_product_id",
        ORDER_BOM_SNAPSHOT_TABLE,
        ["component_product_id"],
    )
    op.create_index(
        "ix_sales_order_item_bom_components_source_component_id",
        ORDER_BOM_SNAPSHOT_TABLE,
        ["product_bom_component_id"],
    )
    _create_snapshot_immutability_guard()

    op.create_table(
        REQUISITION_SOURCE_TABLE,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("requisition_item_id", sa.Integer(), nullable=False),
        sa.Column("sales_order_item_bom_component_id", sa.Integer(), nullable=False),
        sa.Column("order_set_quantity", sa.Integer(), nullable=False),
        sa.Column("quantity_per_set", sa.Numeric(14, 4), nullable=False),
        sa.Column("required_piece_quantity", sa.Numeric(14, 4), nullable=False),
        sa.Column("mold_max_yield_per_sheet", sa.Integer(), nullable=True),
        sa.Column("actual_yield_per_sheet", sa.Numeric(14, 4), nullable=True),
        sa.Column("spare_sheet_quantity", sa.Integer(), nullable=False),
        sa.Column("calculated_purchase_quantity", sa.Numeric(14, 4), nullable=False),
        sa.Column("direction_note", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.ForeignKeyConstraint(
            ["requisition_item_id"],
            ["material_requisition_items.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["sales_order_item_bom_component_id"],
            [f"{ORDER_BOM_SNAPSHOT_TABLE}.id"],
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "order_set_quantity > 0",
            name="ck_requisition_item_bom_sources_order_set_quantity",
        ),
        sa.CheckConstraint(
            "quantity_per_set > 0",
            name="ck_requisition_item_bom_sources_quantity_per_set",
        ),
        sa.CheckConstraint(
            "required_piece_quantity > 0",
            name="ck_requisition_item_bom_sources_required_piece_quantity",
        ),
        sa.CheckConstraint(
            "required_piece_quantity = order_set_quantity * quantity_per_set",
            name="ck_requisition_item_bom_sources_required_piece_formula",
        ),
        sa.CheckConstraint(
            "mold_max_yield_per_sheet IS NULL OR mold_max_yield_per_sheet > 0",
            name="ck_requisition_item_bom_sources_mold_yield",
        ),
        sa.CheckConstraint(
            "actual_yield_per_sheet IS NULL OR actual_yield_per_sheet > 0",
            name="ck_requisition_item_bom_sources_actual_yield",
        ),
        sa.CheckConstraint(
            "actual_yield_per_sheet IS NULL OR mold_max_yield_per_sheet IS NOT NULL",
            name="ck_requisition_item_bom_sources_actual_yield_has_maximum",
        ),
        sa.CheckConstraint(
            "actual_yield_per_sheet IS NULL OR "
            "actual_yield_per_sheet <= mold_max_yield_per_sheet",
            name="ck_requisition_item_bom_sources_actual_yield_within_maximum",
        ),
        sa.CheckConstraint(
            "spare_sheet_quantity >= 0",
            name="ck_requisition_item_bom_sources_spare_sheets",
        ),
        sa.CheckConstraint(
            "calculated_purchase_quantity >= 0",
            name="ck_requisition_item_bom_sources_calculated_purchase_quantity",
        ),
        sa.CheckConstraint(
            "calculated_purchase_quantity >= spare_sheet_quantity",
            name="ck_requisition_item_bom_sources_purchase_covers_spares",
        ),
        sa.CheckConstraint(
            "direction_note IS NULL OR length(trim(direction_note)) > 0",
            name="ck_requisition_item_bom_sources_direction_note",
        ),
        sa.UniqueConstraint(
            "requisition_item_id",
            "sales_order_item_bom_component_id",
            name="uq_requisition_item_bom_sources_item_snapshot",
        ),
    )
    op.create_index(
        "ix_requisition_item_bom_sources_requisition_item_id",
        REQUISITION_SOURCE_TABLE,
        ["requisition_item_id"],
    )
    op.create_index(
        "ix_requisition_item_bom_sources_snapshot_id",
        REQUISITION_SOURCE_TABLE,
        ["sales_order_item_bom_component_id"],
    )


def downgrade() -> None:
    connection = op.get_bind()
    _assert_safe_downgrade(connection)

    op.drop_index(
        "ix_requisition_item_bom_sources_snapshot_id",
        table_name=REQUISITION_SOURCE_TABLE,
    )
    op.drop_index(
        "ix_requisition_item_bom_sources_requisition_item_id",
        table_name=REQUISITION_SOURCE_TABLE,
    )
    op.drop_table(REQUISITION_SOURCE_TABLE)

    _drop_snapshot_immutability_guard()
    op.drop_index(
        "ix_sales_order_item_bom_components_source_component_id",
        table_name=ORDER_BOM_SNAPSHOT_TABLE,
    )
    op.drop_index(
        "ix_sales_order_item_bom_components_component_product_id",
        table_name=ORDER_BOM_SNAPSHOT_TABLE,
    )
    op.drop_index(
        "ix_sales_order_item_bom_components_item_display_order",
        table_name=ORDER_BOM_SNAPSHOT_TABLE,
    )
    op.drop_table(ORDER_BOM_SNAPSHOT_TABLE)

    op.drop_index(
        "ix_product_bom_components_mold_tool_id",
        table_name=PRODUCT_BOM_TABLE,
    )
    op.drop_index(
        "ix_product_bom_components_component_product_id",
        table_name=PRODUCT_BOM_TABLE,
    )
    op.drop_index(
        "ix_product_bom_components_parent_display_order",
        table_name=PRODUCT_BOM_TABLE,
    )
    op.drop_table(PRODUCT_BOM_TABLE)

    op.drop_index("ix_products_is_internal_component", table_name="products")
    op.drop_index("ix_products_is_composite", table_name="products")
    with op.batch_alter_table("products") as batch_op:
        batch_op.drop_column("is_internal_component")
        batch_op.drop_column("is_composite")
