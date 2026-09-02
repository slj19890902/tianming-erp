"""freeze composite-parent stock replenishment component plans

Revision ID: jp74v8x9z63
Revises: jo73v8x9z62
Create Date: 2026-09-03
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "jp74v8x9z63"
down_revision = "jo73v8x9z62"
branch_labels = None
depends_on = None


PLAN_TABLE = "stock_replenishment_bom_component_plans"
RESERVATION_TABLE = "inventory_reservations"
PLAN_UPDATE_TRIGGER = "trg_stock_replenishment_bom_plan_immutable_update"
PLAN_DELETE_TRIGGER = "trg_stock_replenishment_bom_plan_immutable_delete"
PLAN_IMMUTABLE_FUNCTION = "fn_stock_replenishment_bom_plan_immutable"


def _assert_legacy_semi_requisition_targets_are_valid() -> None:
    invalid = int(
        op.get_bind()
        .execute(
            sa.text(
                "SELECT COUNT(*) FROM inventory_reservations "
                "WHERE reservation_type = 'semi_requisition' "
                "AND requisition_item_id IS NULL"
            )
        )
        .scalar_one()
    )
    if invalid:
        raise RuntimeError(
            "P1-150 upgrade blocked: legacy semi_requisition reservation "
            "has no requisition target"
        )


def _drop_sqlite_reservation_triggers() -> list[str]:
    """Preserve every SQLite trigger affected by rebuilding reservations."""

    connection = op.get_bind()
    if connection.dialect.name != "sqlite":
        return []
    rows = connection.execute(
        sa.text(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type='trigger' AND sql IS NOT NULL "
            "AND (tbl_name = 'inventory_reservations' "
            "OR instr(lower(sql), 'inventory_reservations') > 0)"
        )
    ).all()
    trigger_sql: list[str] = []
    for name, sql in rows:
        trigger_sql.append(str(sql))
        op.execute(f'DROP TRIGGER IF EXISTS "{str(name).replace(chr(34), chr(34) * 2)}"')
    return trigger_sql


def _restore_sqlite_triggers(trigger_sql: list[str]) -> None:
    for statement in trigger_sql:
        op.execute(statement)


def _create_plan_immutability_guards() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        op.execute(
            f"""
            CREATE TRIGGER {PLAN_UPDATE_TRIGGER}
            BEFORE UPDATE ON {PLAN_TABLE}
            FOR EACH ROW
            BEGIN
                SELECT RAISE(ABORT, 'composite-parent replenishment plan is immutable');
            END
            """
        )
        op.execute(
            f"""
            CREATE TRIGGER {PLAN_DELETE_TRIGGER}
            BEFORE DELETE ON {PLAN_TABLE}
            FOR EACH ROW
            BEGIN
                SELECT RAISE(ABORT, 'composite-parent replenishment plan is immutable');
            END
            """
        )
    elif dialect == "postgresql":
        op.execute(
            f"""
            CREATE OR REPLACE FUNCTION {PLAN_IMMUTABLE_FUNCTION}()
            RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION 'composite-parent replenishment plan is immutable';
            END;
            $$ LANGUAGE plpgsql
            """
        )
        op.execute(
            f"CREATE TRIGGER {PLAN_UPDATE_TRIGGER} "
            f"BEFORE UPDATE ON {PLAN_TABLE} FOR EACH ROW "
            f"EXECUTE FUNCTION {PLAN_IMMUTABLE_FUNCTION}()"
        )
        op.execute(
            f"CREATE TRIGGER {PLAN_DELETE_TRIGGER} "
            f"BEFORE DELETE ON {PLAN_TABLE} FOR EACH ROW "
            f"EXECUTE FUNCTION {PLAN_IMMUTABLE_FUNCTION}()"
        )


def _drop_plan_immutability_guards() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        op.execute(f"DROP TRIGGER IF EXISTS {PLAN_UPDATE_TRIGGER}")
        op.execute(f"DROP TRIGGER IF EXISTS {PLAN_DELETE_TRIGGER}")
    elif dialect == "postgresql":
        op.execute(f"DROP TRIGGER IF EXISTS {PLAN_UPDATE_TRIGGER} ON {PLAN_TABLE}")
        op.execute(f"DROP TRIGGER IF EXISTS {PLAN_DELETE_TRIGGER} ON {PLAN_TABLE}")
        op.execute(f"DROP FUNCTION IF EXISTS {PLAN_IMMUTABLE_FUNCTION}()")


def upgrade() -> None:
    _assert_legacy_semi_requisition_targets_are_valid()

    op.create_table(
        PLAN_TABLE,
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("replenishment_order_id", sa.Integer(), nullable=False),
        sa.Column("replenishment_item_id", sa.Integer(), nullable=True),
        sa.Column("stock_policy_id", sa.Integer(), nullable=True),
        sa.Column("parent_product_id", sa.Integer(), nullable=False),
        sa.Column("parent_product_version", sa.Integer(), nullable=False),
        sa.Column("parent_product_code_snapshot", sa.String(150), nullable=True),
        sa.Column("parent_product_name_snapshot", sa.String(250), nullable=False),
        sa.Column("product_bom_component_id", sa.Integer(), nullable=False),
        sa.Column("component_product_id", sa.Integer(), nullable=False),
        sa.Column("component_product_version", sa.Integer(), nullable=False),
        sa.Column("component_product_code_snapshot", sa.String(150), nullable=True),
        sa.Column("component_product_name_snapshot", sa.String(250), nullable=False),
        sa.Column("display_order", sa.Integer(), nullable=False),
        sa.Column("parent_set_quantity", sa.Integer(), nullable=False),
        sa.Column("quantity_per_set", sa.Integer(), nullable=False),
        sa.Column("required_piece_quantity", sa.Integer(), nullable=False),
        sa.Column(
            "incoming_covered_piece_quantity",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "reserved_piece_quantity",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column("net_required_piece_quantity", sa.Integer(), nullable=False),
        sa.Column(
            "pieces_per_box", sa.Integer(), server_default="1", nullable=False
        ),
        sa.Column("yield_per_sheet", sa.Integer(), nullable=False),
        sa.Column("cutting_mode_snapshot", sa.String(30), nullable=False),
        sa.Column(
            "is_die_cut_snapshot",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
        sa.Column("mold_max_yield_per_sheet_snapshot", sa.Integer(), nullable=True),
        sa.Column(
            "spare_sheet_quantity", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column("purchase_sheet_quantity", sa.Integer(), nullable=False),
        sa.Column(
            "cutting_remainder_piece_quantity",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column("customer_id", sa.Integer(), nullable=True),
        sa.Column("supplier_name_snapshot", sa.String(200), nullable=True),
        sa.Column("procurement_route_snapshot", sa.String(30), nullable=True),
        sa.Column("material_id", sa.Integer(), nullable=True),
        sa.Column("material_code_snapshot", sa.String(100), nullable=True),
        sa.Column("normalized_material_code", sa.String(100), nullable=True),
        sa.Column("layer_count", sa.Integer(), nullable=True),
        sa.Column("flute_type", sa.String(20), nullable=True),
        sa.Column("report_length_mm", sa.Integer(), nullable=True),
        sa.Column("report_width_mm", sa.Integer(), nullable=True),
        sa.Column("crease_type", sa.String(20), nullable=True),
        sa.Column("crease_left_mm", sa.Integer(), nullable=True),
        sa.Column("crease_middle_mm", sa.Integer(), nullable=True),
        sa.Column("crease_right_mm", sa.Integer(), nullable=True),
        sa.Column("sheet_type", sa.String(30), server_default="raw_board", nullable=False),
        sa.Column("component_type", sa.String(20), server_default="whole", nullable=False),
        sa.Column("internal_component_code_snapshot", sa.String(150), nullable=False),
        sa.Column("bom_fingerprint", sa.String(64), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.func.current_timestamp(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "parent_product_version > 0 AND component_product_version > 0",
            name="ck_stock_replenishment_bom_plan_product_versions",
        ),
        sa.CheckConstraint(
            "parent_product_id <> component_product_id",
            name="ck_stock_replenishment_bom_plan_distinct_products",
        ),
        sa.CheckConstraint(
            "display_order >= 0",
            name="ck_stock_replenishment_bom_plan_display_order",
        ),
        sa.CheckConstraint(
            "parent_set_quantity > 0 AND quantity_per_set > 0",
            name="ck_stock_replenishment_bom_plan_parent_quantities",
        ),
        sa.CheckConstraint(
            "required_piece_quantity = parent_set_quantity * quantity_per_set",
            name="ck_stock_replenishment_bom_plan_required_formula",
        ),
        sa.CheckConstraint(
            "incoming_covered_piece_quantity >= 0 "
            "AND reserved_piece_quantity >= 0 "
            "AND incoming_covered_piece_quantity + reserved_piece_quantity "
            "<= required_piece_quantity",
            name="ck_stock_replenishment_bom_plan_coverages",
        ),
        sa.CheckConstraint(
            "net_required_piece_quantity = required_piece_quantity "
            "- incoming_covered_piece_quantity - reserved_piece_quantity",
            name="ck_stock_replenishment_bom_plan_net_formula",
        ),
        sa.CheckConstraint(
            "pieces_per_box > 0 AND yield_per_sheet > 0 "
            "AND spare_sheet_quantity >= 0",
            name="ck_stock_replenishment_bom_plan_conversions",
        ),
        sa.CheckConstraint(
            "length(trim(cutting_mode_snapshot)) > 0 "
            "AND (mold_max_yield_per_sheet_snapshot IS NULL "
            "OR mold_max_yield_per_sheet_snapshot > 0) "
            "AND (NOT is_die_cut_snapshot OR "
            "(mold_max_yield_per_sheet_snapshot IS NOT NULL "
            "AND yield_per_sheet <= mold_max_yield_per_sheet_snapshot))",
            name="ck_stock_replenishment_bom_plan_cutting_snapshot",
        ),
        sa.CheckConstraint(
            "(net_required_piece_quantity = 0 "
            "AND purchase_sheet_quantity = 0) OR "
            "(net_required_piece_quantity > 0 "
            "AND purchase_sheet_quantity > spare_sheet_quantity "
            "AND (purchase_sheet_quantity - spare_sheet_quantity) "
            "* yield_per_sheet >= net_required_piece_quantity "
            "AND (purchase_sheet_quantity - spare_sheet_quantity - 1) "
            "* yield_per_sheet < net_required_piece_quantity)",
            name="ck_stock_replenishment_bom_plan_purchase_formula",
        ),
        sa.CheckConstraint(
            "cutting_remainder_piece_quantity = "
            "CASE WHEN net_required_piece_quantity = 0 THEN 0 ELSE "
            "(purchase_sheet_quantity - spare_sheet_quantity) * yield_per_sheet END "
            "- net_required_piece_quantity",
            name="ck_stock_replenishment_bom_plan_remainder_formula",
        ),
        sa.CheckConstraint(
            "(purchase_sheet_quantity = 0 AND replenishment_item_id IS NULL) OR "
            "(purchase_sheet_quantity > 0 AND replenishment_item_id IS NOT NULL)",
            name="ck_stock_replenishment_bom_plan_purchase_item",
        ),
        sa.CheckConstraint(
            "component_type IN ('whole','cover','base')",
            name="ck_stock_replenishment_bom_plan_component_type",
        ),
        sa.CheckConstraint(
            "sheet_type IN ('raw_board','net_sheet','creased_sheet')",
            name="ck_stock_replenishment_bom_plan_sheet_type",
        ),
        sa.CheckConstraint(
            "procurement_route_snapshot IS NULL OR "
            "procurement_route_snapshot IN ('paperboard','external_packaging')",
            name="ck_stock_replenishment_bom_plan_procurement_route",
        ),
        sa.CheckConstraint(
            "length(bom_fingerprint) = 64 AND length(request_fingerprint) = 64",
            name="ck_stock_replenishment_bom_plan_fingerprints",
        ),
        sa.ForeignKeyConstraint(
            ["replenishment_order_id"],
            ["stock_replenishment_orders.id"],
            name="fk_stock_replenishment_bom_plan_order",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["replenishment_item_id"],
            ["stock_replenishment_order_items.id"],
            name="fk_stock_replenishment_bom_plan_item",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["stock_policy_id"],
            ["inventory_stock_policies.id"],
            name="fk_stock_replenishment_bom_plan_policy",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["parent_product_id"],
            ["products.id"],
            name="fk_stock_replenishment_bom_plan_parent_product",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["product_bom_component_id"],
            ["product_bom_components.id"],
            name="fk_stock_replenishment_bom_plan_bom_component",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["component_product_id"],
            ["products.id"],
            name="fk_stock_replenishment_bom_plan_component_product",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["customer_id"],
            ["customers.id"],
            name="fk_stock_replenishment_bom_plan_customer",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["material_id"],
            ["materials.id"],
            name="fk_stock_replenishment_bom_plan_material",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "replenishment_order_id",
            "product_bom_component_id",
            name="uq_stock_replenishment_bom_plan_order_component",
        ),
        sa.UniqueConstraint(
            "replenishment_item_id",
            name="uq_stock_replenishment_bom_plan_item",
        ),
    )
    op.create_index(
        "ix_stock_replenishment_bom_plan_order",
        PLAN_TABLE,
        ["replenishment_order_id", "display_order"],
    )
    op.create_index(
        "ix_stock_replenishment_bom_plan_policy", PLAN_TABLE, ["stock_policy_id"]
    )
    op.create_index(
        "ix_stock_replenishment_bom_plan_parent", PLAN_TABLE, ["parent_product_id"]
    )
    op.create_index(
        "ix_stock_replenishment_bom_plan_component",
        PLAN_TABLE,
        ["component_product_id"],
    )
    op.create_index(
        "ix_stock_replenishment_bom_plan_request",
        PLAN_TABLE,
        ["request_fingerprint"],
    )
    _create_plan_immutability_guards()

    external_trigger_sql = _drop_sqlite_reservation_triggers()
    with op.batch_alter_table(RESERVATION_TABLE, recreate="auto") as batch_op:
        batch_op.add_column(
            sa.Column(
                "stock_replenishment_bom_component_plan_id",
                sa.Integer(),
                nullable=True,
            )
        )
        batch_op.create_foreign_key(
            "fk_inventory_reservations_stock_replenishment_bom_plan",
            PLAN_TABLE,
            ["stock_replenishment_bom_component_plan_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.create_check_constraint(
            "ck_inventory_reservations_semi_requisition_target",
            "(reservation_type = 'semi_requisition' AND ("
            "(requisition_item_id IS NOT NULL AND "
            "stock_replenishment_bom_component_plan_id IS NULL) OR "
            "(requisition_item_id IS NULL AND "
            "stock_replenishment_bom_component_plan_id IS NOT NULL))) OR "
            "(reservation_type <> 'semi_requisition' AND "
            "stock_replenishment_bom_component_plan_id IS NULL)",
        )
    _restore_sqlite_triggers(external_trigger_sql)
    op.create_index(
        "ix_inventory_reservations_stock_replenishment_bom_plan",
        RESERVATION_TABLE,
        ["stock_replenishment_bom_component_plan_id", "status"],
    )


def downgrade() -> None:
    plan_count = int(
        op.get_bind()
        .execute(sa.text(f"SELECT COUNT(*) FROM {PLAN_TABLE}"))
        .scalar_one()
    )
    linked_reservation_count = int(
        op.get_bind()
        .execute(
            sa.text(
                "SELECT COUNT(*) FROM inventory_reservations "
                "WHERE stock_replenishment_bom_component_plan_id IS NOT NULL"
            )
        )
        .scalar_one()
    )
    if plan_count or linked_reservation_count:
        raise RuntimeError(
            "P1-150 downgrade blocked: composite-parent replenishment facts exist; "
            "restore the verified pre-upgrade backup instead"
        )

    op.drop_index(
        "ix_inventory_reservations_stock_replenishment_bom_plan",
        table_name=RESERVATION_TABLE,
    )
    external_trigger_sql = _drop_sqlite_reservation_triggers()
    with op.batch_alter_table(RESERVATION_TABLE, recreate="auto") as batch_op:
        batch_op.drop_constraint(
            "ck_inventory_reservations_semi_requisition_target",
            type_="check",
        )
        batch_op.drop_constraint(
            "fk_inventory_reservations_stock_replenishment_bom_plan",
            type_="foreignkey",
        )
        batch_op.drop_column("stock_replenishment_bom_component_plan_id")
    _restore_sqlite_triggers(external_trigger_sql)

    _drop_plan_immutability_guards()
    op.drop_index("ix_stock_replenishment_bom_plan_request", table_name=PLAN_TABLE)
    op.drop_index("ix_stock_replenishment_bom_plan_component", table_name=PLAN_TABLE)
    op.drop_index("ix_stock_replenishment_bom_plan_parent", table_name=PLAN_TABLE)
    op.drop_index("ix_stock_replenishment_bom_plan_policy", table_name=PLAN_TABLE)
    op.drop_index("ix_stock_replenishment_bom_plan_order", table_name=PLAN_TABLE)
    op.drop_table(PLAN_TABLE)
