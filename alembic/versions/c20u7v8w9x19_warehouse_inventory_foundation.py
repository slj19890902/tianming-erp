"""Warehouse inventory foundation.

Revision ID: c20u7v8w9x19
Revises: b19t6u7v8w18
"""

from alembic import op
import sqlalchemy as sa


revision = "c20u7v8w9x19"
down_revision = "b19t6u7v8w18"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "warehouse_locations",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("location_code", sa.String(50), nullable=False),
        sa.Column("location_name", sa.String(100), nullable=False),
        sa.Column("warehouse_type", sa.String(30), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("remarks", sa.Text()),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
        sa.Column("updated_at", sa.DateTime()),
        sa.CheckConstraint(
            "warehouse_type IN ('finished','semi_finished','shared')",
            name="ck_warehouse_locations_type",
        ),
        sa.UniqueConstraint("location_code", name="uq_warehouse_locations_code"),
    )
    op.create_index(
        "ix_warehouse_locations_type_active",
        "warehouse_locations",
        ["warehouse_type", "is_active"],
    )

    op.create_table(
        "inventory_lots",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("lot_number", sa.String(50), nullable=False),
        sa.Column("inventory_type", sa.String(30), nullable=False),
        sa.Column(
            "warehouse_location_id",
            sa.Integer(),
            sa.ForeignKey("warehouse_locations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("quantity_available", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("quantity_reserved", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("quantity_consumed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("quantity_damaged", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("quantity_scrapped", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("unit", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("source_type", sa.String(30), nullable=False),
        sa.Column("source_ref_type", sa.String(50)),
        sa.Column("source_ref_id", sa.Integer()),
        sa.Column("stock_date", sa.Date(), nullable=False),
        sa.Column("last_movement_at", sa.DateTime(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("remarks", sa.Text()),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
        sa.Column("updated_at", sa.DateTime()),
        sa.CheckConstraint("inventory_type IN ('finished','semi_finished')", name="ck_inventory_lots_type"),
        sa.CheckConstraint("unit IN ('boxes','sheets')", name="ck_inventory_lots_unit"),
        sa.CheckConstraint("status IN ('active','frozen','closed')", name="ck_inventory_lots_status"),
        sa.CheckConstraint(
            "source_type IN ('manual','production_surplus','purchase_surplus','stocktake','transfer')",
            name="ck_inventory_lots_source_type",
        ),
        sa.CheckConstraint("quantity_available >= 0", name="ck_inventory_lots_available"),
        sa.CheckConstraint("quantity_reserved >= 0", name="ck_inventory_lots_reserved"),
        sa.CheckConstraint("quantity_consumed >= 0", name="ck_inventory_lots_consumed"),
        sa.CheckConstraint("quantity_damaged >= 0", name="ck_inventory_lots_damaged"),
        sa.CheckConstraint("quantity_scrapped >= 0", name="ck_inventory_lots_scrapped"),
        sa.UniqueConstraint("lot_number", name="uq_inventory_lots_number"),
    )
    op.create_index("ix_inventory_lots_type_status", "inventory_lots", ["inventory_type", "status"])
    op.create_index("ix_inventory_lots_location_status", "inventory_lots", ["warehouse_location_id", "status"])
    op.create_index("ix_inventory_lots_stock_date", "inventory_lots", ["stock_date"])
    op.create_index("ix_inventory_lots_last_movement", "inventory_lots", ["last_movement_at"])

    op.create_table(
        "finished_goods_inventory_details",
        sa.Column(
            "inventory_lot_id",
            sa.Integer(),
            sa.ForeignKey("inventory_lots.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("owner_customer_id", sa.Integer(), sa.ForeignKey("customers.id", ondelete="SET NULL")),
        sa.Column("owner_customer_name_snapshot", sa.String(200)),
        sa.Column("is_general", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("inventory_code_snapshot", sa.String(150), nullable=False),
        sa.Column("product_name_snapshot", sa.String(250), nullable=False),
        sa.Column("box_type_snapshot", sa.String(150)),
        sa.Column("length_mm", sa.Integer()),
        sa.Column("width_mm", sa.Integer()),
        sa.Column("height_mm", sa.Integer()),
        sa.Column("material_code_snapshot", sa.String(100)),
        sa.Column("flute_type_snapshot", sa.String(20)),
        sa.CheckConstraint("is_general = 1 OR owner_customer_id IS NOT NULL", name="ck_finished_inventory_owner"),
    )
    op.create_index("ix_finished_inventory_owner_product", "finished_goods_inventory_details", ["owner_customer_id", "product_id"])
    op.create_index("ix_finished_inventory_product", "finished_goods_inventory_details", ["product_id"])
    op.create_index("ix_finished_inventory_general", "finished_goods_inventory_details", ["is_general"])
    op.create_index("ix_finished_inventory_code", "finished_goods_inventory_details", ["inventory_code_snapshot"])

    op.create_table(
        "semi_finished_inventory_details",
        sa.Column(
            "inventory_lot_id",
            sa.Integer(),
            sa.ForeignKey("inventory_lots.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("supplier_name", sa.String(200)),
        sa.Column("owner_customer_id", sa.Integer(), sa.ForeignKey("customers.id", ondelete="SET NULL")),
        sa.Column("owner_customer_name_snapshot", sa.String(200)),
        sa.Column("material_code_snapshot", sa.String(100), nullable=False),
        sa.Column("layer_count", sa.Integer(), nullable=False),
        sa.Column("flute_type", sa.String(20), nullable=False),
        sa.Column("board_length_mm", sa.Integer(), nullable=False),
        sa.Column("board_width_mm", sa.Integer(), nullable=False),
        sa.Column("sheet_type", sa.String(30), nullable=False),
        sa.Column("crease_type", sa.String(20)),
        sa.Column("crease_left_mm", sa.Integer()),
        sa.Column("crease_middle_mm", sa.Integer()),
        sa.Column("crease_right_mm", sa.Integer()),
        sa.Column("cutting_note", sa.Text()),
        sa.CheckConstraint("layer_count IN (3,5)", name="ck_semi_inventory_layer"),
        sa.CheckConstraint(
            "(layer_count=3 AND flute_type IN ('A','B','E')) OR "
            "(layer_count=5 AND flute_type IN ('AB','BE'))",
            name="ck_semi_inventory_flute",
        ),
        sa.CheckConstraint("board_length_mm > 0", name="ck_semi_inventory_length"),
        sa.CheckConstraint("board_width_mm > 0", name="ck_semi_inventory_width"),
        sa.CheckConstraint(
            "sheet_type IN ('raw_board','net_sheet','creased_sheet')",
            name="ck_semi_inventory_sheet_type",
        ),
    )
    op.create_index("ix_semi_inventory_flute_size", "semi_finished_inventory_details", ["flute_type", "board_length_mm", "board_width_mm"])
    op.create_index("ix_semi_inventory_sheet_flute", "semi_finished_inventory_details", ["sheet_type", "flute_type"])
    op.create_index("ix_semi_inventory_owner", "semi_finished_inventory_details", ["owner_customer_id"])
    op.create_index("ix_semi_inventory_material", "semi_finished_inventory_details", ["material_code_snapshot"])

    op.create_table(
        "inventory_reservations",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("reservation_number", sa.String(50), nullable=False),
        sa.Column("inventory_lot_id", sa.Integer(), sa.ForeignKey("inventory_lots.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("reservation_type", sa.String(30), nullable=False),
        sa.Column("order_id", sa.Integer(), sa.ForeignKey("sales_orders.id", ondelete="SET NULL")),
        sa.Column("order_item_id", sa.Integer(), sa.ForeignKey("sales_order_items.id", ondelete="SET NULL")),
        sa.Column("requisition_item_id", sa.Integer(), sa.ForeignKey("material_requisition_items.id", ondelete="SET NULL")),
        sa.Column("reserved_stock_quantity", sa.Integer(), nullable=False),
        sa.Column("credited_requirement_quantity", sa.Integer()),
        sa.Column("yield_factor", sa.Integer()),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("warning_codes", sa.Text()),
        sa.Column("warning_acknowledged_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("reserved_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("reserved_at", sa.DateTime()),
        sa.Column("released_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("released_at", sa.DateTime()),
        sa.Column("consumed_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("consumed_at", sa.DateTime()),
        sa.Column("release_reason", sa.Text()),
        sa.Column("idempotency_key", sa.String(100)),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
        sa.Column("updated_at", sa.DateTime()),
        sa.CheckConstraint("reservation_type IN ('finished_order','semi_requisition')", name="ck_inventory_reservations_type"),
        sa.CheckConstraint("status IN ('active','released','consumed','cancelled')", name="ck_inventory_reservations_status"),
        sa.CheckConstraint("reserved_stock_quantity > 0", name="ck_inventory_reservations_quantity"),
        sa.UniqueConstraint("reservation_number", name="uq_inventory_reservations_number"),
        sa.UniqueConstraint("idempotency_key", name="uq_inventory_reservations_idempotency"),
    )
    op.create_index("ix_inventory_reservations_lot_status", "inventory_reservations", ["inventory_lot_id", "status"])
    op.create_index("ix_inventory_reservations_order_item", "inventory_reservations", ["order_item_id", "reservation_type", "status"])
    op.create_index("ix_inventory_reservations_requisition", "inventory_reservations", ["requisition_item_id"])

    op.create_table(
        "inventory_movements",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("movement_number", sa.String(50), nullable=False),
        sa.Column("inventory_lot_id", sa.Integer(), sa.ForeignKey("inventory_lots.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("movement_type", sa.String(30), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("unit", sa.String(20), nullable=False),
        sa.Column("before_available", sa.Integer(), nullable=False),
        sa.Column("after_available", sa.Integer(), nullable=False),
        sa.Column("before_reserved", sa.Integer(), nullable=False),
        sa.Column("after_reserved", sa.Integer(), nullable=False),
        sa.Column("before_consumed", sa.Integer(), nullable=False),
        sa.Column("after_consumed", sa.Integer(), nullable=False),
        sa.Column("before_damaged", sa.Integer(), nullable=False),
        sa.Column("after_damaged", sa.Integer(), nullable=False),
        sa.Column("before_scrapped", sa.Integer(), nullable=False),
        sa.Column("after_scrapped", sa.Integer(), nullable=False),
        sa.Column("reservation_id", sa.Integer(), sa.ForeignKey("inventory_reservations.id", ondelete="SET NULL")),
        sa.Column("related_order_id", sa.Integer(), sa.ForeignKey("sales_orders.id", ondelete="SET NULL")),
        sa.Column("related_order_item_id", sa.Integer(), sa.ForeignKey("sales_order_items.id", ondelete="SET NULL")),
        sa.Column("related_requisition_id", sa.Integer(), sa.ForeignKey("material_requisitions.id", ondelete="SET NULL")),
        sa.Column("related_supplier_order_id", sa.Integer(), sa.ForeignKey("supplier_requisition_orders.id", ondelete="SET NULL")),
        sa.Column("related_delivery_id", sa.Integer(), sa.ForeignKey("sales_deliveries.id", ondelete="SET NULL")),
        sa.Column("reversal_of_movement_id", sa.Integer(), sa.ForeignKey("inventory_movements.id", ondelete="SET NULL")),
        sa.Column("reason", sa.Text()),
        sa.Column("remarks", sa.Text()),
        sa.Column("operator_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("idempotency_key", sa.String(100)),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
        sa.CheckConstraint(
            "movement_type IN ('manual_in','adjust','freeze','unfreeze','damage','scrap',"
            "'transfer_to_general','reserve','release_reserve','consume')",
            name="ck_inventory_movements_type",
        ),
        sa.CheckConstraint("quantity >= 0", name="ck_inventory_movements_quantity"),
        sa.UniqueConstraint("movement_number", name="uq_inventory_movements_number"),
        sa.UniqueConstraint("idempotency_key", name="uq_inventory_movements_idempotency"),
    )
    op.create_index("ix_inventory_movements_lot_created", "inventory_movements", ["inventory_lot_id", "created_at"])
    op.create_index("ix_inventory_movements_reservation", "inventory_movements", ["reservation_id"])
    op.create_index("ix_inventory_movements_order_item", "inventory_movements", ["related_order_item_id"])
    op.create_index("ix_inventory_movements_supplier_order", "inventory_movements", ["related_supplier_order_id"])
    op.create_index("ix_inventory_movements_delivery", "inventory_movements", ["related_delivery_id"])
    op.create_index("ix_inventory_movements_type_created", "inventory_movements", ["movement_type", "created_at"])


def downgrade() -> None:
    op.drop_table("inventory_movements")
    op.drop_table("inventory_reservations")
    op.drop_table("semi_finished_inventory_details")
    op.drop_table("finished_goods_inventory_details")
    op.drop_table("inventory_lots")
    op.drop_table("warehouse_locations")
