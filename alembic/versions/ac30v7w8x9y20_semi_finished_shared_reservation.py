"""Semi-finished shared inventory matching and reservation foundation.

Revision ID: ac30v7w8x9y20
Revises: ab29u7v8w9x18
Create Date: 2026-07-10
"""

from alembic import op
import sqlalchemy as sa


revision = "ac30v7w8x9y20"
down_revision = "ab29u7v8w9x18"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "order_item_semi_requirements",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "order_item_id",
            sa.Integer(),
            sa.ForeignKey("sales_order_items.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "customer_id",
            sa.Integer(),
            sa.ForeignKey("customers.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("component_type", sa.String(20), nullable=False),
        sa.Column("board_length_mm", sa.Integer(), nullable=False),
        sa.Column("board_width_mm", sa.Integer(), nullable=False),
        sa.Column("material_code_snapshot", sa.String(100), nullable=False),
        sa.Column("normalized_material_code", sa.String(100), nullable=False),
        sa.Column("flute_type", sa.String(20), nullable=False),
        sa.Column("pieces_per_box", sa.Integer(), nullable=False),
        sa.Column("stock_yield_per_sheet", sa.Integer(), nullable=False),
        sa.Column("required_piece_quantity", sa.Integer(), nullable=False),
        sa.Column(
            "created_by",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "updated_by",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.Column("updated_at", sa.DateTime()),
        sa.CheckConstraint(
            "component_type IN ('whole','cover','base')",
            name="ck_order_item_semi_requirements_component",
        ),
        sa.CheckConstraint(
            "board_length_mm > 0 AND board_width_mm > 0",
            name="ck_order_item_semi_requirements_dimensions",
        ),
        sa.CheckConstraint(
            "pieces_per_box > 0 AND stock_yield_per_sheet > 0",
            name="ck_order_item_semi_requirements_conversion",
        ),
        sa.CheckConstraint(
            "required_piece_quantity > 0",
            name="ck_order_item_semi_requirements_quantity",
        ),
        sa.UniqueConstraint(
            "order_item_id",
            "component_type",
            name="uq_order_item_semi_requirements_item_component",
        ),
    )
    op.create_index(
        "ix_order_item_semi_requirements_signature",
        "order_item_semi_requirements",
        [
            "customer_id",
            "board_length_mm",
            "board_width_mm",
            "normalized_material_code",
            "flute_type",
            "component_type",
        ],
    )

    op.create_table(
        "semi_finished_match_rules",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "customer_id",
            sa.Integer(),
            sa.ForeignKey("customers.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("board_length_mm", sa.Integer(), nullable=False),
        sa.Column("board_width_mm", sa.Integer(), nullable=False),
        sa.Column("normalized_material_code", sa.String(100), nullable=False),
        sa.Column("flute_type", sa.String(20), nullable=False),
        sa.Column("component_type", sa.String(20), nullable=False),
        sa.Column("pieces_per_box", sa.Integer(), nullable=False),
        sa.Column("stock_yield_per_sheet", sa.Integer(), nullable=False),
        sa.Column(
            "active", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
        sa.Column(
            "created_by",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "updated_by",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.Column("updated_at", sa.DateTime()),
        sa.CheckConstraint(
            "component_type IN ('whole','cover','base')",
            name="ck_semi_finished_match_rules_component",
        ),
        sa.CheckConstraint(
            "board_length_mm > 0 AND board_width_mm > 0",
            name="ck_semi_finished_match_rules_dimensions",
        ),
        sa.CheckConstraint(
            "pieces_per_box > 0 AND stock_yield_per_sheet > 0",
            name="ck_semi_finished_match_rules_conversion",
        ),
        sa.UniqueConstraint(
            "customer_id",
            "board_length_mm",
            "board_width_mm",
            "normalized_material_code",
            "flute_type",
            "component_type",
            "pieces_per_box",
            "stock_yield_per_sheet",
            name="uq_semi_finished_match_rules_signature",
        ),
    )
    op.create_index(
        "ix_semi_finished_match_rules_active_component",
        "semi_finished_match_rules",
        ["active", "component_type"],
    )

    op.create_table(
        "semi_finished_match_rule_products",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "rule_id",
            sa.Integer(),
            sa.ForeignKey("semi_finished_match_rules.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "product_id",
            sa.Integer(),
            sa.ForeignKey("products.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "confirmed_by",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.Column("confirmed_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "rule_id",
            "product_id",
            name="uq_semi_finished_match_rule_products_rule_product",
        ),
    )
    op.create_index(
        "ix_semi_finished_match_rule_products_product",
        "semi_finished_match_rule_products",
        ["product_id", "rule_id"],
    )

    with op.batch_alter_table("semi_finished_inventory_details") as batch_op:
        batch_op.add_column(
            sa.Column("normalized_material_code", sa.String(100), nullable=True)
        )
        batch_op.add_column(
            sa.Column(
                "component_type",
                sa.String(20),
                nullable=False,
                server_default="whole",
            )
        )
        batch_op.add_column(
            sa.Column(
                "pieces_per_box", sa.Integer(), nullable=False, server_default="1"
            )
        )
        batch_op.add_column(
            sa.Column(
                "stock_yield_per_sheet",
                sa.Integer(),
                nullable=False,
                server_default="1",
            )
        )

    op.execute(
        "UPDATE semi_finished_inventory_details "
        "SET normalized_material_code = UPPER("
        "REPLACE(REPLACE(REPLACE(REPLACE(REPLACE("
        "TRIM(material_code_snapshot), ' ', ''), CHAR(9), ''), "
        "CHAR(10), ''), CHAR(13), ''), '　', ''))"
    )

    with op.batch_alter_table("semi_finished_inventory_details") as batch_op:
        batch_op.alter_column(
            "normalized_material_code",
            existing_type=sa.String(100),
            nullable=False,
        )
        batch_op.alter_column(
            "component_type",
            existing_type=sa.String(20),
            server_default=None,
        )
        batch_op.alter_column(
            "pieces_per_box", existing_type=sa.Integer(), server_default=None
        )
        batch_op.alter_column(
            "stock_yield_per_sheet",
            existing_type=sa.Integer(),
            server_default=None,
        )
        batch_op.create_check_constraint(
            "ck_semi_inventory_component",
            "component_type IN ('whole','cover','base')",
        )
        batch_op.create_check_constraint(
            "ck_semi_inventory_pieces_per_box", "pieces_per_box > 0"
        )
        batch_op.create_check_constraint(
            "ck_semi_inventory_stock_yield", "stock_yield_per_sheet > 0"
        )

    op.create_index(
        "ix_semi_inventory_shared_signature",
        "semi_finished_inventory_details",
        [
            "owner_customer_id",
            "board_length_mm",
            "board_width_mm",
            "normalized_material_code",
            "flute_type",
            "component_type",
            "pieces_per_box",
            "stock_yield_per_sheet",
        ],
    )

    with op.batch_alter_table("inventory_reservations") as batch_op:
        batch_op.drop_constraint(
            "ck_inventory_reservations_type", type_="check"
        )
        batch_op.drop_constraint(
            "ck_inventory_reservations_status", type_="check"
        )
        batch_op.add_column(sa.Column("semi_requirement_id", sa.Integer()))
        batch_op.add_column(sa.Column("match_rule_id", sa.Integer()))
        batch_op.add_column(
            sa.Column(
                "consumed_stock_quantity",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )
        batch_op.add_column(
            sa.Column(
                "released_stock_quantity",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )
        batch_op.add_column(
            sa.Column(
                "consumed_requirement_quantity",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )
        batch_op.add_column(
            sa.Column(
                "released_requirement_quantity",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )
        batch_op.add_column(
            sa.Column("reservation_group_key", sa.String(100), nullable=True)
        )
        batch_op.add_column(
            sa.Column(
                "reservation_group_requested_quantity", sa.Integer(), nullable=True
            )
        )
        batch_op.create_foreign_key(
            "fk_inventory_reservations_semi_requirement",
            "order_item_semi_requirements",
            ["semi_requirement_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_foreign_key(
            "fk_inventory_reservations_match_rule",
            "semi_finished_match_rules",
            ["match_rule_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_check_constraint(
            "ck_inventory_reservations_type",
            "reservation_type IN "
            "('finished_order','semi_requisition','semi_order')",
        )
        batch_op.create_check_constraint(
            "ck_inventory_reservations_status",
            "status IN ('active','partial','released','consumed','cancelled')",
        )
        batch_op.create_check_constraint(
            "ck_inventory_reservations_cumulative_quantities",
            "consumed_stock_quantity >= 0 AND released_stock_quantity >= 0 "
            "AND consumed_stock_quantity + released_stock_quantity "
            "<= reserved_stock_quantity",
        )
        batch_op.create_check_constraint(
            "ck_inventory_reservations_cumulative_requirement_quantities",
            "consumed_requirement_quantity >= 0 "
            "AND released_requirement_quantity >= 0 "
            "AND (credited_requirement_quantity IS NULL OR "
            "consumed_requirement_quantity + released_requirement_quantity "
            "<= credited_requirement_quantity)",
        )
        batch_op.create_unique_constraint(
            "uq_inventory_reservations_group_lot",
            ["reservation_group_key", "inventory_lot_id"],
        )

    op.create_index(
        "ix_inventory_reservations_semi_requirement",
        "inventory_reservations",
        ["semi_requirement_id", "status"],
    )
    op.create_index(
        "ix_inventory_reservations_match_rule",
        "inventory_reservations",
        ["match_rule_id"],
    )
    op.create_index(
        "ix_inventory_reservations_group",
        "inventory_reservations",
        ["reservation_group_key"],
    )

    with op.batch_alter_table("inventory_movements") as batch_op:
        batch_op.drop_constraint("ck_inventory_movements_type", type_="check")
        batch_op.create_check_constraint(
            "ck_inventory_movements_type",
            "movement_type IN "
            "('manual_in','adjust','freeze','unfreeze','damage','scrap',"
            "'transfer_to_general','reserve','release_reserve','consume',"
            "'reverse_consume')",
        )

    op.create_table(
        "delivery_inventory_allocations",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "delivery_item_id",
            sa.Integer(),
            sa.ForeignKey("sales_delivery_items.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "reservation_id",
            sa.Integer(),
            sa.ForeignKey("inventory_reservations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "consume_movement_id",
            sa.Integer(),
            sa.ForeignKey("inventory_movements.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("consumed_stock_quantity", sa.Integer(), nullable=False),
        sa.Column("credited_requirement_quantity", sa.Integer(), nullable=False),
        sa.Column(
            "reversed_stock_quantity",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "reversed_requirement_quantity",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "status", sa.String(20), nullable=False, server_default="active"
        ),
        sa.Column(
            "created_by",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "reversed_by",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.Column("reversed_at", sa.DateTime()),
        sa.CheckConstraint(
            "consumed_stock_quantity > 0 AND credited_requirement_quantity > 0",
            name="ck_delivery_inventory_allocations_quantities",
        ),
        sa.CheckConstraint(
            "reversed_stock_quantity >= 0 "
            "AND reversed_stock_quantity <= consumed_stock_quantity "
            "AND reversed_requirement_quantity >= 0 "
            "AND reversed_requirement_quantity <= credited_requirement_quantity",
            name="ck_delivery_inventory_allocations_reversed",
        ),
        sa.CheckConstraint(
            "status IN ('active','partial','reversed')",
            name="ck_delivery_inventory_allocations_status",
        ),
        sa.UniqueConstraint(
            "consume_movement_id",
            name="uq_delivery_inventory_allocations_consume_movement",
        ),
    )
    op.create_index(
        "ix_delivery_inventory_allocations_delivery_item",
        "delivery_inventory_allocations",
        ["delivery_item_id", "status"],
    )
    op.create_index(
        "ix_delivery_inventory_allocations_reservation",
        "delivery_inventory_allocations",
        ["reservation_id", "status"],
    )


def downgrade() -> None:
    op.drop_table("delivery_inventory_allocations")

    op.execute(
        "DELETE FROM inventory_movements WHERE movement_type = 'reverse_consume'"
    )
    with op.batch_alter_table("inventory_movements") as batch_op:
        batch_op.drop_constraint("ck_inventory_movements_type", type_="check")
        batch_op.create_check_constraint(
            "ck_inventory_movements_type",
            "movement_type IN "
            "('manual_in','adjust','freeze','unfreeze','damage','scrap',"
            "'transfer_to_general','reserve','release_reserve','consume')",
        )

    op.execute(
        "DELETE FROM inventory_reservations WHERE reservation_type = 'semi_order'"
    )
    op.drop_index(
        "ix_inventory_reservations_group", table_name="inventory_reservations"
    )
    op.drop_index(
        "ix_inventory_reservations_match_rule",
        table_name="inventory_reservations",
    )
    op.drop_index(
        "ix_inventory_reservations_semi_requirement",
        table_name="inventory_reservations",
    )
    with op.batch_alter_table("inventory_reservations") as batch_op:
        batch_op.drop_constraint(
            "uq_inventory_reservations_group_lot", type_="unique"
        )
        batch_op.drop_constraint(
            "ck_inventory_reservations_cumulative_quantities", type_="check"
        )
        batch_op.drop_constraint(
            "ck_inventory_reservations_cumulative_requirement_quantities",
            type_="check",
        )
        batch_op.drop_constraint(
            "ck_inventory_reservations_status", type_="check"
        )
        batch_op.drop_constraint(
            "ck_inventory_reservations_type", type_="check"
        )
        batch_op.drop_constraint(
            "fk_inventory_reservations_match_rule", type_="foreignkey"
        )
        batch_op.drop_constraint(
            "fk_inventory_reservations_semi_requirement", type_="foreignkey"
        )
        batch_op.create_check_constraint(
            "ck_inventory_reservations_type",
            "reservation_type IN ('finished_order','semi_requisition')",
        )
        batch_op.create_check_constraint(
            "ck_inventory_reservations_status",
            "status IN ('active','released','consumed','cancelled')",
        )
        batch_op.drop_column("reservation_group_requested_quantity")
        batch_op.drop_column("reservation_group_key")
        batch_op.drop_column("released_stock_quantity")
        batch_op.drop_column("consumed_stock_quantity")
        batch_op.drop_column("released_requirement_quantity")
        batch_op.drop_column("consumed_requirement_quantity")
        batch_op.drop_column("match_rule_id")
        batch_op.drop_column("semi_requirement_id")

    op.drop_index(
        "ix_semi_inventory_shared_signature",
        table_name="semi_finished_inventory_details",
    )
    with op.batch_alter_table("semi_finished_inventory_details") as batch_op:
        batch_op.drop_constraint(
            "ck_semi_inventory_stock_yield", type_="check"
        )
        batch_op.drop_constraint(
            "ck_semi_inventory_pieces_per_box", type_="check"
        )
        batch_op.drop_constraint(
            "ck_semi_inventory_component", type_="check"
        )
        batch_op.drop_column("stock_yield_per_sheet")
        batch_op.drop_column("pieces_per_box")
        batch_op.drop_column("component_type")
        batch_op.drop_column("normalized_material_code")

    op.drop_table("semi_finished_match_rule_products")
    op.drop_table("semi_finished_match_rules")
    op.drop_table("order_item_semi_requirements")
