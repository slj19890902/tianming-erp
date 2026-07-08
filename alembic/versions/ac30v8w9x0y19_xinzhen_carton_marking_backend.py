"""Xinzhen carton marking backend chain.

Revision ID: ac30v8w9x0y19
Revises: ab29u7v8w9x18
"""

from alembic import op
import sqlalchemy as sa


revision = "ac30v8w9x0y19"
down_revision = "ab29u7v8w9x18"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "xinzhen_resin_templates",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("template_code", sa.String(length=80), nullable=False),
        sa.Column("template_name", sa.String(length=200), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint(
            "customer_id",
            "template_code",
            name="uq_xinzhen_resin_templates_customer_code",
        ),
    )
    op.create_index(
        "ix_xinzhen_resin_templates_customer_id",
        "xinzhen_resin_templates",
        ["customer_id"],
    )

    op.create_table(
        "xinzhen_carton_marking_orders",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("order_number", sa.String(length=50), nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("external_po_no", sa.String(length=150), nullable=True),
        sa.Column("source_file_name", sa.String(length=255), nullable=False),
        sa.Column("header_total_carton_qty", sa.Integer(), nullable=True),
        sa.Column("total_carton_qty", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("layout_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "status",
            sa.String(length=30),
            nullable=False,
            server_default="needs_review",
        ),
        sa.Column("resin_template_id", sa.Integer(), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "status IN ('pending_receipt', 'needs_review')",
            name="ck_xinzhen_orders_status",
        ),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["resin_template_id"],
            ["xinzhen_resin_templates.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("order_number", name="uq_xinzhen_orders_order_number"),
    )
    op.create_index(
        "ix_xinzhen_orders_customer_id",
        "xinzhen_carton_marking_orders",
        ["customer_id"],
    )
    op.create_index(
        "ix_xinzhen_orders_external_po_no",
        "xinzhen_carton_marking_orders",
        ["external_po_no"],
    )

    op.create_table(
        "xinzhen_carton_marking_import_batches",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("batch_number", sa.String(length=50), nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("order_id", sa.Integer(), nullable=False),
        sa.Column("source_file_name", sa.String(length=255), nullable=False),
        sa.Column("source_file_hash", sa.String(length=128), nullable=False),
        sa.Column("parsed_layout_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("header_total_carton_qty", sa.Integer(), nullable=True),
        sa.Column("layout_total_carton_qty", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("mismatch_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "missing_common_box_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "missing_rubber_block_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "generated_changeover_step_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "import_status",
            sa.String(length=30),
            nullable=False,
            server_default="needs_review",
        ),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "import_status IN ('ready', 'needs_review')",
            name="ck_xinzhen_import_batches_status",
        ),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["xinzhen_carton_marking_orders.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint(
            "batch_number",
            name="uq_xinzhen_import_batches_batch_number",
        ),
    )
    op.create_index(
        "ix_xinzhen_import_batches_customer_id",
        "xinzhen_carton_marking_import_batches",
        ["customer_id"],
    )
    op.create_index(
        "ix_xinzhen_import_batches_order_id",
        "xinzhen_carton_marking_import_batches",
        ["order_id"],
    )

    op.create_table(
        "xinzhen_resin_template_slots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("template_id", sa.Integer(), nullable=False),
        sa.Column("slot_code", sa.String(length=20), nullable=False),
        sa.Column("slot_name", sa.String(length=120), nullable=False),
        sa.Column("field_name", sa.String(length=80), nullable=False),
        sa.Column("block_type", sa.String(length=40), nullable=False),
        sa.Column("display_order", sa.Integer(), nullable=False),
        sa.Column("is_variable", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.ForeignKeyConstraint(
            ["template_id"],
            ["xinzhen_resin_templates.id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "template_id",
            "slot_code",
            name="uq_xinzhen_resin_template_slots_template_slot_code",
        ),
    )
    op.create_index(
        "ix_xinzhen_resin_template_slots_template_id",
        "xinzhen_resin_template_slots",
        ["template_id"],
    )

    op.create_table(
        "xinzhen_rubber_type_blocks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("block_code", sa.String(length=80), nullable=True),
        sa.Column("block_text", sa.String(length=255), nullable=False),
        sa.Column("block_type", sa.String(length=40), nullable=False),
        sa.Column("storage_location_text", sa.String(length=255), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"], ondelete="RESTRICT"),
    )
    op.create_index(
        "ix_xinzhen_rubber_type_blocks_customer_type_text",
        "xinzhen_rubber_type_blocks",
        ["customer_id", "block_type", "block_text"],
    )

    op.create_table(
        "xinzhen_common_box_rules",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("dimension_hint_text", sa.String(length=80), nullable=True),
        sa.Column("product_size", sa.String(length=80), nullable=True),
        sa.Column("units_per_carton", sa.Integer(), nullable=True),
        sa.Column("vendor_style_no", sa.String(length=80), nullable=True),
        sa.Column("color", sa.String(length=80), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="RESTRICT"),
    )
    op.create_index(
        "ix_xinzhen_common_box_rules_customer_id",
        "xinzhen_common_box_rules",
        ["customer_id"],
    )
    op.create_index(
        "ix_xinzhen_common_box_rules_customer_size_units",
        "xinzhen_common_box_rules",
        ["customer_id", "product_size", "units_per_carton"],
    )

    op.create_table(
        "xinzhen_print_layouts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("import_batch_id", sa.Integer(), nullable=False),
        sa.Column("order_id", sa.Integer(), nullable=False),
        sa.Column("layout_code", sa.String(length=20), nullable=False),
        sa.Column("source_file_name", sa.String(length=255), nullable=False),
        sa.Column("source_sheet_name", sa.String(length=120), nullable=True),
        sa.Column("source_block_index", sa.Integer(), nullable=True),
        sa.Column("external_po_no", sa.String(length=150), nullable=True),
        sa.Column("customer_mark_name", sa.String(length=255), nullable=True),
        sa.Column("vendor_style_no", sa.String(length=120), nullable=True),
        sa.Column("color", sa.String(length=120), nullable=True),
        sa.Column("product_size", sa.String(length=120), nullable=True),
        sa.Column("units_per_carton", sa.Integer(), nullable=True),
        sa.Column("total_units", sa.Integer(), nullable=True),
        sa.Column("carton_qty", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("carton_no_start", sa.Integer(), nullable=True),
        sa.Column("carton_no_end", sa.Integer(), nullable=True),
        sa.Column("carton_no_range_text", sa.String(length=255), nullable=True),
        sa.Column("carton_qty_formula_text", sa.Text(), nullable=True),
        sa.Column("resin_template_id", sa.Integer(), nullable=False),
        sa.Column("common_box_id", sa.Integer(), nullable=True),
        sa.Column(
            "box_match_status",
            sa.String(length=20),
            nullable=False,
            server_default="missing",
        ),
        sa.Column("box_match_strategy", sa.String(length=40), nullable=True),
        sa.Column("common_box_dimension", sa.String(length=80), nullable=True),
        sa.Column("print_content_snapshot", sa.Text(), nullable=True),
        sa.Column("layout_hash", sa.String(length=64), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "print_status",
            sa.String(length=20),
            nullable=False,
            server_default="pending",
        ),
        sa.Column(
            "parse_status",
            sa.String(length=20),
            nullable=False,
            server_default="ok",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "box_match_status IN ('matched', 'missing')",
            name="ck_xinzhen_print_layouts_box_match_status",
        ),
        sa.CheckConstraint(
            "parse_status IN ('ok', 'needs_review')",
            name="ck_xinzhen_print_layouts_parse_status",
        ),
        sa.CheckConstraint(
            "print_status IN ('pending', 'printing', 'completed')",
            name="ck_xinzhen_print_layouts_print_status",
        ),
        sa.ForeignKeyConstraint(
            ["import_batch_id"],
            ["xinzhen_carton_marking_import_batches.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["xinzhen_carton_marking_orders.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["resin_template_id"],
            ["xinzhen_resin_templates.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["common_box_id"],
            ["products.id"],
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint(
            "import_batch_id",
            "layout_code",
            name="uq_xinzhen_print_layouts_batch_layout_code",
        ),
    )
    op.create_index(
        "ix_xinzhen_print_layouts_order_sort",
        "xinzhen_print_layouts",
        ["order_id", "sort_order"],
    )

    op.create_table(
        "xinzhen_print_layout_slot_values",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("print_layout_id", sa.Integer(), nullable=False),
        sa.Column("slot_code", sa.String(length=20), nullable=False),
        sa.Column("slot_name", sa.String(length=120), nullable=False),
        sa.Column("field_name", sa.String(length=80), nullable=False),
        sa.Column("block_type", sa.String(length=40), nullable=False),
        sa.Column("display_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("slot_value", sa.String(length=255), nullable=True),
        sa.Column("rubber_block_id", sa.Integer(), nullable=True),
        sa.Column("rubber_block_code", sa.String(length=80), nullable=True),
        sa.Column("rubber_block_storage_location", sa.String(length=255), nullable=True),
        sa.Column(
            "match_status",
            sa.String(length=20),
            nullable=False,
            server_default="missing",
        ),
        sa.CheckConstraint(
            "match_status IN ('matched', 'missing')",
            name="ck_xinzhen_print_layout_slot_values_match_status",
        ),
        sa.ForeignKeyConstraint(
            ["print_layout_id"],
            ["xinzhen_print_layouts.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["rubber_block_id"],
            ["xinzhen_rubber_type_blocks.id"],
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint(
            "print_layout_id",
            "slot_code",
            name="uq_xinzhen_print_layout_slot_values_layout_slot_code",
        ),
    )
    op.create_index(
        "ix_xinzhen_print_layout_slot_values_layout_id",
        "xinzhen_print_layout_slot_values",
        ["print_layout_id"],
    )

    op.create_table(
        "xinzhen_print_changeover_steps",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("order_id", sa.Integer(), nullable=False),
        sa.Column("from_layout_id", sa.Integer(), nullable=False),
        sa.Column("to_layout_id", sa.Integer(), nullable=False),
        sa.Column("step_no", sa.Integer(), nullable=False),
        sa.Column("changed_slot_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default="no_change",
        ),
        sa.CheckConstraint(
            "status IN ('ready', 'needs_review', 'no_change')",
            name="ck_xinzhen_print_changeover_steps_status",
        ),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["xinzhen_carton_marking_orders.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["from_layout_id"],
            ["xinzhen_print_layouts.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["to_layout_id"],
            ["xinzhen_print_layouts.id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "order_id",
            "step_no",
            name="uq_xinzhen_print_changeover_steps_order_step_no",
        ),
    )
    op.create_index(
        "ix_xinzhen_print_changeover_steps_order_id",
        "xinzhen_print_changeover_steps",
        ["order_id"],
    )

    op.create_table(
        "xinzhen_print_changeover_step_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("changeover_step_id", sa.Integer(), nullable=False),
        sa.Column("slot_code", sa.String(length=20), nullable=False),
        sa.Column("slot_name", sa.String(length=120), nullable=False),
        sa.Column("old_value", sa.String(length=255), nullable=True),
        sa.Column("new_value", sa.String(length=255), nullable=True),
        sa.Column("rubber_block_id", sa.Integer(), nullable=True),
        sa.Column("rubber_block_storage_location", sa.String(length=255), nullable=True),
        sa.Column("confirm_status", sa.String(length=20), nullable=False),
        sa.CheckConstraint(
            "confirm_status IN ('needs_change', 'missing')",
            name="ck_xinzhen_print_changeover_step_items_confirm_status",
        ),
        sa.ForeignKeyConstraint(
            ["changeover_step_id"],
            ["xinzhen_print_changeover_steps.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["rubber_block_id"],
            ["xinzhen_rubber_type_blocks.id"],
            ondelete="SET NULL",
        ),
    )
    op.create_index(
        "ix_xinzhen_print_changeover_step_items_step_id",
        "xinzhen_print_changeover_step_items",
        ["changeover_step_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_xinzhen_print_changeover_step_items_step_id",
        table_name="xinzhen_print_changeover_step_items",
    )
    op.drop_table("xinzhen_print_changeover_step_items")
    op.drop_index(
        "ix_xinzhen_print_changeover_steps_order_id",
        table_name="xinzhen_print_changeover_steps",
    )
    op.drop_table("xinzhen_print_changeover_steps")
    op.drop_index(
        "ix_xinzhen_print_layout_slot_values_layout_id",
        table_name="xinzhen_print_layout_slot_values",
    )
    op.drop_table("xinzhen_print_layout_slot_values")
    op.drop_index(
        "ix_xinzhen_print_layouts_order_sort",
        table_name="xinzhen_print_layouts",
    )
    op.drop_table("xinzhen_print_layouts")
    op.drop_index(
        "ix_xinzhen_common_box_rules_customer_size_units",
        table_name="xinzhen_common_box_rules",
    )
    op.drop_index(
        "ix_xinzhen_common_box_rules_customer_id",
        table_name="xinzhen_common_box_rules",
    )
    op.drop_table("xinzhen_common_box_rules")
    op.drop_index(
        "ix_xinzhen_rubber_type_blocks_customer_type_text",
        table_name="xinzhen_rubber_type_blocks",
    )
    op.drop_table("xinzhen_rubber_type_blocks")
    op.drop_index(
        "ix_xinzhen_resin_template_slots_template_id",
        table_name="xinzhen_resin_template_slots",
    )
    op.drop_table("xinzhen_resin_template_slots")
    op.drop_index(
        "ix_xinzhen_import_batches_order_id",
        table_name="xinzhen_carton_marking_import_batches",
    )
    op.drop_index(
        "ix_xinzhen_import_batches_customer_id",
        table_name="xinzhen_carton_marking_import_batches",
    )
    op.drop_table("xinzhen_carton_marking_import_batches")
    op.drop_index(
        "ix_xinzhen_orders_external_po_no",
        table_name="xinzhen_carton_marking_orders",
    )
    op.drop_index(
        "ix_xinzhen_orders_customer_id",
        table_name="xinzhen_carton_marking_orders",
    )
    op.drop_table("xinzhen_carton_marking_orders")
    op.drop_index(
        "ix_xinzhen_resin_templates_customer_id",
        table_name="xinzhen_resin_templates",
    )
    op.drop_table("xinzhen_resin_templates")
