"""Stock warning and replenishment order foundation.

Revision ID: ad31v7w8x9z21
Revises: ac30v7w8x9y20
Create Date: 2026-07-10
"""

from alembic import op
import sqlalchemy as sa


revision = "ad31v7w8x9z21"
down_revision = "ac30v7w8x9y20"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("inventory_lots") as batch_op:
        batch_op.drop_constraint("ck_inventory_lots_source_type", type_="check")
        batch_op.create_check_constraint(
            "ck_inventory_lots_source_type",
            "source_type IN ('manual','production_surplus','purchase_surplus',"
            "'stocktake','transfer','replenishment')",
        )

    op.create_table(
        "inventory_stock_policies",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("policy_name", sa.String(200), nullable=False),
        sa.Column("target_inventory_type", sa.String(30), nullable=False),
        sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id", ondelete="RESTRICT")),
        sa.Column("customer_id", sa.Integer(), sa.ForeignKey("customers.id", ondelete="RESTRICT")),
        sa.Column("material_code_snapshot", sa.String(100)),
        sa.Column("normalized_material_code", sa.String(100)),
        sa.Column("layer_count", sa.Integer()),
        sa.Column("flute_type", sa.String(20)),
        sa.Column("report_length_mm", sa.Integer()),
        sa.Column("report_width_mm", sa.Integer()),
        sa.Column("sheet_type", sa.String(30), nullable=False, server_default="raw_board"),
        sa.Column("component_type", sa.String(20), nullable=False, server_default="whole"),
        sa.Column("pieces_per_box", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("stock_yield_per_sheet", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("warning_quantity", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("target_quantity", sa.Integer(), nullable=False),
        sa.Column("default_location_id", sa.Integer(), sa.ForeignKey("warehouse_locations.id", ondelete="SET NULL")),
        sa.Column("supplier_name", sa.String(200)),
        sa.Column("remark", sa.Text()),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("updated_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
        sa.Column("updated_at", sa.DateTime()),
        sa.CheckConstraint("target_inventory_type IN ('finished','semi_finished')", name="ck_inventory_stock_policies_target_type"),
        sa.CheckConstraint("warning_quantity >= 0 AND target_quantity > 0 AND target_quantity >= warning_quantity", name="ck_inventory_stock_policies_quantities"),
        sa.CheckConstraint("component_type IN ('whole','cover','base')", name="ck_inventory_stock_policies_component"),
        sa.CheckConstraint("pieces_per_box > 0 AND stock_yield_per_sheet > 0", name="ck_inventory_stock_policies_conversion"),
    )
    op.create_index("ix_inventory_stock_policies_active_type", "inventory_stock_policies", ["active", "target_inventory_type"])
    op.create_index("ix_inventory_stock_policies_product", "inventory_stock_policies", ["product_id", "active"])
    op.create_index(
        "ix_inventory_stock_policies_semi_signature",
        "inventory_stock_policies",
        ["customer_id", "report_length_mm", "report_width_mm", "normalized_material_code", "flute_type", "component_type"],
    )

    op.create_table(
        "stock_replenishment_orders",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("order_number", sa.String(40), nullable=False),
        sa.Column("supplier_name", sa.String(200)),
        sa.Column("customer_id", sa.Integer(), sa.ForeignKey("customers.id", ondelete="SET NULL")),
        sa.Column("source_type", sa.String(30), nullable=False),
        sa.Column("status", sa.String(30), nullable=False, server_default="draft"),
        sa.Column("remark", sa.Text()),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("confirmed_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("stocked_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
        sa.Column("confirmed_at", sa.DateTime()),
        sa.Column("stocked_at", sa.DateTime()),
        sa.Column("voided_at", sa.DateTime()),
        sa.UniqueConstraint("order_number", name="uq_stock_replenishment_orders_number"),
        sa.CheckConstraint("source_type IN ('stock_warning','customer_request','manual_history')", name="ck_stock_replenishment_orders_source"),
        sa.CheckConstraint("status IN ('draft','confirmed','partially_stocked','stocked','voided')", name="ck_stock_replenishment_orders_status"),
    )
    op.create_index("ix_stock_replenishment_orders_status", "stock_replenishment_orders", ["status", "created_at"])

    op.create_table(
        "stock_replenishment_order_items",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("replenishment_order_id", sa.Integer(), sa.ForeignKey("stock_replenishment_orders.id", ondelete="CASCADE"), nullable=False),
        sa.Column("stock_policy_id", sa.Integer(), sa.ForeignKey("inventory_stock_policies.id", ondelete="SET NULL")),
        sa.Column("target_inventory_type", sa.String(30), nullable=False),
        sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id", ondelete="RESTRICT")),
        sa.Column("customer_id", sa.Integer(), sa.ForeignKey("customers.id", ondelete="SET NULL")),
        sa.Column("product_code_snapshot", sa.String(150)),
        sa.Column("product_name_snapshot", sa.String(250), nullable=False),
        sa.Column("material_code_snapshot", sa.String(100)),
        sa.Column("normalized_material_code", sa.String(100)),
        sa.Column("layer_count", sa.Integer()),
        sa.Column("flute_type", sa.String(20)),
        sa.Column("report_length_mm", sa.Integer()),
        sa.Column("report_width_mm", sa.Integer()),
        sa.Column("crease_type", sa.String(20)),
        sa.Column("crease_left_mm", sa.Integer()),
        sa.Column("crease_middle_mm", sa.Integer()),
        sa.Column("crease_right_mm", sa.Integer()),
        sa.Column("sheet_type", sa.String(30), nullable=False, server_default="raw_board"),
        sa.Column("component_type", sa.String(20), nullable=False, server_default="whole"),
        sa.Column("pieces_per_box", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("stock_yield_per_sheet", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("stocked_quantity", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("location_id", sa.Integer(), sa.ForeignKey("warehouse_locations.id", ondelete="SET NULL")),
        sa.Column("inventory_lot_id", sa.Integer(), sa.ForeignKey("inventory_lots.id", ondelete="SET NULL")),
        sa.Column("historical_workbook", sa.String(260)),
        sa.Column("historical_sheet", sa.String(150)),
        sa.Column("historical_row", sa.Integer()),
        sa.Column("historical_search_text", sa.Text()),
        sa.Column("remark", sa.Text()),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
        sa.Column("stocked_at", sa.DateTime()),
        sa.CheckConstraint("target_inventory_type IN ('finished','semi_finished')", name="ck_stock_replenishment_items_target_type"),
        sa.CheckConstraint("quantity > 0 AND stocked_quantity >= 0 AND stocked_quantity <= quantity", name="ck_stock_replenishment_items_quantities"),
        sa.CheckConstraint("component_type IN ('whole','cover','base')", name="ck_stock_replenishment_items_component"),
        sa.CheckConstraint("pieces_per_box > 0 AND stock_yield_per_sheet > 0", name="ck_stock_replenishment_items_conversion"),
    )
    op.create_index("ix_stock_replenishment_items_order", "stock_replenishment_order_items", ["replenishment_order_id"])
    op.create_index("ix_stock_replenishment_items_policy", "stock_replenishment_order_items", ["stock_policy_id"])
    op.create_index("ix_stock_replenishment_items_lot", "stock_replenishment_order_items", ["inventory_lot_id"])


def downgrade() -> None:
    op.drop_table("stock_replenishment_order_items")
    op.drop_table("stock_replenishment_orders")
    op.drop_table("inventory_stock_policies")
    op.execute(
        "UPDATE inventory_lots SET source_type='purchase_surplus' "
        "WHERE source_type='replenishment'"
    )
    with op.batch_alter_table("inventory_lots") as batch_op:
        batch_op.drop_constraint("ck_inventory_lots_source_type", type_="check")
        batch_op.create_check_constraint(
            "ck_inventory_lots_source_type",
            "source_type IN ('manual','production_surplus','purchase_surplus',"
            "'stocktake','transfer')",
        )
