"""add immutable receipt-purpose and actual-price facts

Revision ID: xx32v8x9z21
Revises: ww31v8x9z20
Create Date: 2026-08-20
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "xx32v8x9z21"
down_revision = "ww31v8x9z20"
branch_labels = None
depends_on = None


PRICE_TABLE = "purchase_receipt_facts"
VARIANCE_TABLE = "purchase_receipt_material_variances"
VARIANCE_APPROVAL_TABLE = "purchase_receipt_material_variance_approvals"
ALLOCATION_TABLE = "incoming_receipt_purpose_allocations"
REVERSAL_TABLE = "incoming_receipt_purpose_reversals"
REVERSAL_FACT_TABLE = "incoming_receipt_reversal_facts"
BATCH_FACT_TABLE = "incoming_receipt_batch_facts"
DOWNGRADE_BLOCKED_MESSAGE = "P1-81 已存在收料用途、实际价格或计划备库事实，拒绝破坏性降级"


def _drop_sqlite_triggers_referencing(connection, table_name: str) -> list[str]:
    if connection.dialect.name != "sqlite":
        return []
    rows = connection.execute(
        sa.text(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type='trigger' AND sql IS NOT NULL AND sql LIKE :needle"
        ),
        {"needle": f"%{table_name}%"},
    ).mappings().all()
    definitions: list[str] = []
    for row in rows:
        name = str(row["name"])
        if not name.replace("_", "").isalnum():
            raise RuntimeError(f"检测到无法安全处理的 SQLite 触发器名称：{name}")
        definitions.append(str(row["sql"]))
        connection.exec_driver_sql(f'DROP TRIGGER IF EXISTS "{name}"')
    return definitions


def _restore_sqlite_triggers(connection, definitions: list[str]) -> None:
    for definition in definitions:
        connection.exec_driver_sql(definition)


def _create_immutability_guards(table_name: str) -> None:
    dialect = op.get_bind().dialect.name
    update_trigger = f"trg_{table_name}_immutable_update"
    delete_trigger = f"trg_{table_name}_immutable_delete"
    trigger = f"trg_{table_name}_immutable"
    function = f"{table_name}_immutable"
    if dialect == "sqlite":
        op.execute(
            f"""
            CREATE TRIGGER {update_trigger}
            BEFORE UPDATE ON {table_name}
            FOR EACH ROW
            BEGIN
                SELECT RAISE(ABORT, '{table_name} rows are immutable');
            END
            """
        )
        op.execute(
            f"""
            CREATE TRIGGER {delete_trigger}
            BEFORE DELETE ON {table_name}
            FOR EACH ROW
            BEGIN
                SELECT RAISE(ABORT, '{table_name} rows are immutable');
            END
            """
        )
    elif dialect == "postgresql":
        op.execute(
            f"""
            CREATE OR REPLACE FUNCTION {function}()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                RAISE EXCEPTION '{table_name} rows are immutable';
            END;
            $$
            """
        )
        op.execute(
            f"""
            CREATE TRIGGER {trigger}
            BEFORE UPDATE OR DELETE ON {table_name}
            FOR EACH ROW EXECUTE FUNCTION {function}()
            """
        )


def _drop_immutability_guards(table_name: str) -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table_name}_immutable_delete")
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table_name}_immutable_update")
    elif dialect == "postgresql":
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table_name}_immutable ON {table_name}")
        op.execute(f"DROP FUNCTION IF EXISTS {table_name}_immutable()")


def _create_material_variance_tables() -> None:
    op.create_table(
        VARIANCE_TABLE,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("supplier_requisition_order_item_id", sa.Integer(), nullable=True),
        sa.Column("material_requisition_item_id", sa.Integer(), nullable=True),
        sa.Column("purchase_purpose_source_snapshot_id", sa.Integer(), nullable=False),
        sa.Column("source_key", sa.String(length=160), nullable=False),
        sa.Column("expected_source_version", sa.Integer(), nullable=False),
        sa.Column("purpose_snapshot_version", sa.Integer(), nullable=False),
        sa.Column("receipt_plan_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("expected_material_id", sa.Integer(), nullable=True),
        sa.Column("expected_material_code_snapshot", sa.String(length=200), nullable=False),
        sa.Column("actual_material_id", sa.Integer(), nullable=False),
        sa.Column("actual_material_code_snapshot", sa.String(length=200), nullable=False),
        sa.Column("actual_material_version", sa.Integer(), nullable=False),
        sa.Column("actual_material_layer_count_snapshot", sa.Integer(), nullable=True),
        sa.Column("actual_material_flute_type_snapshot", sa.String(length=50), nullable=True),
        sa.Column("actual_material_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=False),
        sa.Column("idempotency_key", sa.String(length=120), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("requested_by", sa.Integer(), nullable=False),
        sa.Column("requested_at", sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False),
        sa.ForeignKeyConstraint(["supplier_requisition_order_item_id"], ["supplier_requisition_order_items.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["material_requisition_item_id"], ["material_requisition_items.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["purchase_purpose_source_snapshot_id"], ["purchase_purpose_source_snapshots.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["expected_material_id"], ["materials.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["actual_material_id"], ["materials.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["requested_by"], ["users.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("idempotency_key", name="uq_purchase_material_variances_idem"),
        sa.CheckConstraint("((supplier_requisition_order_item_id IS NOT NULL AND material_requisition_item_id IS NULL) OR (supplier_requisition_order_item_id IS NULL AND material_requisition_item_id IS NOT NULL))", name="ck_purchase_material_variances_one_source"),
        sa.CheckConstraint("actual_material_code_snapshot <> expected_material_code_snapshot AND expected_source_version >= 1 AND purpose_snapshot_version >= 1", name="ck_purchase_material_variances_changed_material"),
        sa.CheckConstraint("length(trim(source_key)) > 0 AND length(trim(reason)) > 0 AND length(receipt_plan_fingerprint) = 64 AND length(actual_material_fingerprint) = 64 AND length(request_hash) = 64", name="ck_purchase_material_variances_frozen_text"),
    )
    op.create_index("ix_purchase_material_variances_source", VARIANCE_TABLE, ["supplier_requisition_order_item_id", "material_requisition_item_id"])
    _create_immutability_guards(VARIANCE_TABLE)

    op.create_table(
        VARIANCE_APPROVAL_TABLE,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("material_variance_id", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=120), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("confirmed_by", sa.Integer(), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False),
        sa.ForeignKeyConstraint(["material_variance_id"], [f"{VARIANCE_TABLE}.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["confirmed_by"], ["users.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("material_variance_id", name="uq_purchase_material_variance_approval"),
        sa.UniqueConstraint("idempotency_key", name="uq_purchase_material_variance_approval_idem"),
        sa.CheckConstraint("length(trim(idempotency_key)) > 0 AND length(request_hash) = 64", name="ck_purchase_material_variance_approval_frozen"),
    )
    _create_immutability_guards(VARIANCE_APPROVAL_TABLE)


def _create_price_table() -> None:
    op.create_table(
        PRICE_TABLE,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("supplier_requisition_order_item_id", sa.Integer(), nullable=True),
        sa.Column("material_requisition_item_id", sa.Integer(), nullable=True),
        sa.Column("purchase_purpose_source_snapshot_id", sa.Integer(), nullable=False),
        sa.Column("source_key", sa.String(length=160), nullable=False),
        sa.Column("receipt_fact_version", sa.Integer(), nullable=False),
        sa.Column("expected_source_version", sa.Integer(), nullable=False),
        sa.Column("purpose_snapshot_version", sa.Integer(), nullable=False),
        sa.Column("receipt_plan_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("actual_material_id", sa.Integer(), nullable=False),
        sa.Column("actual_material_code_snapshot", sa.String(length=200), nullable=False),
        sa.Column("actual_material_version", sa.Integer(), nullable=False),
        sa.Column("actual_material_layer_count_snapshot", sa.Integer(), nullable=True),
        sa.Column("actual_material_flute_type_snapshot", sa.String(length=50), nullable=True),
        sa.Column("actual_material_is_active_snapshot", sa.Boolean(), nullable=False),
        sa.Column("actual_material_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("expected_material_id", sa.Integer(), nullable=True),
        sa.Column("expected_material_code_snapshot", sa.String(length=200), nullable=False),
        sa.Column(
            "material_change_confirmed",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
        sa.Column("material_variance_approval_id", sa.Integer(), nullable=True),
        sa.Column("unit_price", sa.Numeric(20, 6), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("price_unit", sa.String(length=30), nullable=False),
        sa.Column(
            "tax_included", sa.Boolean(), server_default=sa.true(), nullable=False
        ),
        sa.Column("tax_rate", sa.Numeric(8, 6), nullable=False),
        sa.Column("idempotency_key", sa.String(length=120), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("created_by", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.func.current_timestamp(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["supplier_requisition_order_item_id"],
            ["supplier_requisition_order_items.id"],
            name="fk_purchase_receipt_facts_supplier_item",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["material_requisition_item_id"],
            ["material_requisition_items.id"],
            name="fk_purchase_receipt_facts_requisition_item",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["purchase_purpose_source_snapshot_id"],
            ["purchase_purpose_source_snapshots.id"],
            name="fk_purchase_receipt_facts_purpose_snapshot",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["actual_material_id"],
            ["materials.id"],
            name="fk_purchase_receipt_facts_actual_material",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["expected_material_id"],
            ["materials.id"],
            name="fk_purchase_receipt_facts_expected_material",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["material_variance_approval_id"],
            [f"{VARIANCE_APPROVAL_TABLE}.id"],
            name="fk_purchase_receipt_facts_material_variance_approval",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name="fk_purchase_receipt_facts_created_by_users",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "idempotency_key", name="uq_purchase_receipt_facts_idempotency_key"
        ),
        sa.UniqueConstraint(
            "supplier_requisition_order_item_id",
            "purchase_purpose_source_snapshot_id",
            "receipt_fact_version",
            name="uq_purchase_receipt_facts_supplier_snapshot_version",
        ),
        sa.UniqueConstraint(
            "material_requisition_item_id",
            "purchase_purpose_source_snapshot_id",
            "receipt_fact_version",
            name="uq_purchase_receipt_facts_requisition_snapshot_version",
        ),
        sa.CheckConstraint(
            "((supplier_requisition_order_item_id IS NOT NULL "
            "AND material_requisition_item_id IS NULL) OR "
            "(supplier_requisition_order_item_id IS NULL "
            "AND material_requisition_item_id IS NOT NULL))",
            name="ck_purchase_receipt_facts_exactly_one_source",
        ),
        sa.CheckConstraint(
            "receipt_fact_version >= 1 AND expected_source_version >= 1 "
            "AND purpose_snapshot_version >= 1",
            name="ck_purchase_receipt_facts_versions",
        ),
        sa.CheckConstraint(
            "unit_price > 0", name="ck_purchase_receipt_facts_unit_price"
        ),
        sa.CheckConstraint(
            "price_unit IN ('per_sheet','per_square_meter')",
            name="ck_purchase_receipt_facts_price_unit",
        ),
        sa.CheckConstraint(
            "tax_rate >= 0 AND tax_rate <= 1",
            name="ck_purchase_receipt_facts_tax_rate",
        ),
        sa.CheckConstraint(
            "((actual_material_code_snapshot = expected_material_code_snapshot "
            "AND NOT material_change_confirmed AND material_variance_approval_id IS NULL) OR "
            "(actual_material_code_snapshot <> expected_material_code_snapshot "
            "AND material_change_confirmed AND material_variance_approval_id IS NOT NULL))",
            name="ck_purchase_receipt_facts_material_change_confirmation",
        ),
        sa.CheckConstraint(
            "length(trim(source_key)) > 0 "
            "AND length(trim(actual_material_code_snapshot)) > 0 "
            "AND length(trim(expected_material_code_snapshot)) > 0 "
            "AND length(trim(currency)) = 3 "
            "AND length(trim(idempotency_key)) > 0 "
            "AND length(receipt_plan_fingerprint) = 64 "
            "AND actual_material_version >= 1 "
            "AND length(actual_material_fingerprint) = 64 "
            "AND length(request_hash) = 64",
            name="ck_purchase_receipt_facts_frozen_text",
        ),
    )
    op.create_index(
        "ix_purchase_receipt_facts_supplier_item",
        PRICE_TABLE,
        [
            "supplier_requisition_order_item_id",
            "purchase_purpose_source_snapshot_id",
            "receipt_fact_version",
        ],
    )
    op.create_index(
        "ix_purchase_receipt_facts_requisition_item",
        PRICE_TABLE,
        [
            "material_requisition_item_id",
            "purchase_purpose_source_snapshot_id",
            "receipt_fact_version",
        ],
    )
    op.create_index(
        "ix_purchase_receipt_facts_purpose_snapshot",
        PRICE_TABLE,
        ["purchase_purpose_source_snapshot_id"],
    )
    _create_immutability_guards(PRICE_TABLE)


def _create_allocation_table() -> None:
    op.create_table(
        ALLOCATION_TABLE,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("incoming_receipt_item_id", sa.Integer(), nullable=False),
        sa.Column("purpose_contract_status_snapshot", sa.String(length=20), nullable=False),
        sa.Column("purchase_purpose_source_snapshot_id", sa.Integer(), nullable=True),
        sa.Column("purchase_receipt_fact_id", sa.Integer(), nullable=True),
        sa.Column("supplier_requisition_order_item_id", sa.Integer(), nullable=True),
        sa.Column("material_requisition_item_id", sa.Integer(), nullable=True),
        sa.Column("source_kind", sa.String(length=30), nullable=False),
        sa.Column("source_key", sa.String(length=160), nullable=False),
        sa.Column("source_order_item_id", sa.Integer(), nullable=True),
        sa.Column("source_requisition_item_id", sa.Integer(), nullable=True),
        sa.Column("source_bom_requisition_source_id", sa.Integer(), nullable=True),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("customer_name_snapshot", sa.String(length=200), nullable=False),
        sa.Column("component_type", sa.String(length=20), nullable=False),
        sa.Column("order_purpose_plan_sheet_qty_snapshot", sa.Integer(), nullable=True),
        sa.Column("reserve_purpose_plan_sheet_qty_snapshot", sa.Integer(), nullable=True),
        sa.Column("receipt_total_sheet_qty", sa.Integer(), nullable=False),
        sa.Column("receipt_order_purpose_sheet_qty", sa.Integer(), nullable=False),
        sa.Column("receipt_reserve_purpose_sheet_qty", sa.Integer(), nullable=False),
        sa.Column("cumulative_total_sheet_qty_before", sa.Integer(), nullable=False),
        sa.Column("cumulative_total_sheet_qty_after", sa.Integer(), nullable=False),
        sa.Column(
            "cumulative_order_purpose_sheet_qty_before", sa.Integer(), nullable=False
        ),
        sa.Column(
            "cumulative_order_purpose_sheet_qty_after", sa.Integer(), nullable=False
        ),
        sa.Column(
            "cumulative_reserve_purpose_sheet_qty_before", sa.Integer(), nullable=False
        ),
        sa.Column(
            "cumulative_reserve_purpose_sheet_qty_after", sa.Integer(), nullable=False
        ),
        sa.Column("finished_output_qty_before", sa.Integer(), nullable=False),
        sa.Column("finished_output_qty_after", sa.Integer(), nullable=False),
        sa.Column("finished_output_qty_delta", sa.Integer(), nullable=False),
        sa.Column("sheet_cost", sa.Numeric(20, 6), nullable=True),
        sa.Column("order_purpose_cost", sa.Numeric(20, 6), nullable=True),
        sa.Column("reserve_purpose_cost", sa.Numeric(20, 6), nullable=True),
        sa.Column("total_cost", sa.Numeric(20, 6), nullable=True),
        sa.Column("capitalized_cost", sa.Numeric(20, 6), nullable=True),
        sa.Column("production_completion_id", sa.Integer(), nullable=True),
        sa.Column("finished_inventory_lot_id", sa.Integer(), nullable=True),
        sa.Column("semi_finished_inventory_lot_id", sa.Integer(), nullable=True),
        sa.Column("initial_semi_inventory_movement_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=20), server_default="posted", nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("created_by", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.func.current_timestamp(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["incoming_receipt_item_id"],
            ["incoming_receipt_items.id"],
            name="fk_receipt_purpose_allocations_receipt_item",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["purchase_purpose_source_snapshot_id"],
            ["purchase_purpose_source_snapshots.id"],
            name="fk_receipt_purpose_allocations_purpose_snapshot",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["purchase_receipt_fact_id"],
            ["purchase_receipt_facts.id"],
            name="fk_receipt_purpose_allocations_receipt_fact",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["supplier_requisition_order_item_id"],
            ["supplier_requisition_order_items.id"],
            name="fk_receipt_purpose_allocations_supplier_item",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["material_requisition_item_id"],
            ["material_requisition_items.id"],
            name="fk_receipt_purpose_allocations_requisition_item",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_order_item_id"],
            ["sales_order_items.id"],
            name="fk_receipt_purpose_allocations_order_item_source",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_requisition_item_id"],
            ["material_requisition_items.id"],
            name="fk_receipt_purpose_allocations_requisition_item_source",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_bom_requisition_source_id"],
            ["requisition_item_bom_sources.id"],
            name="fk_receipt_purpose_allocations_bom_source",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["customer_id"],
            ["customers.id"],
            name="fk_receipt_purpose_allocations_customer",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["production_completion_id"],
            ["production_completions.id"],
            name="fk_receipt_purpose_allocations_completion",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["finished_inventory_lot_id"],
            ["inventory_lots.id"],
            name="fk_receipt_purpose_allocations_finished_lot",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["semi_finished_inventory_lot_id"],
            ["inventory_lots.id"],
            name="fk_receipt_purpose_allocations_semi_lot",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["initial_semi_inventory_movement_id"],
            ["inventory_movements.id"],
            name="fk_receipt_purpose_allocations_semi_movement",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name="fk_receipt_purpose_allocations_created_by_users",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "incoming_receipt_item_id",
            name="uq_receipt_purpose_allocations_receipt_item",
        ),
        sa.UniqueConstraint(
            "production_completion_id",
            name="uq_receipt_purpose_allocations_completion",
        ),
        sa.UniqueConstraint(
            "finished_inventory_lot_id",
            name="uq_receipt_purpose_allocations_finished_lot",
        ),
        sa.UniqueConstraint(
            "semi_finished_inventory_lot_id",
            name="uq_receipt_purpose_allocations_semi_lot",
        ),
        sa.UniqueConstraint(
            "initial_semi_inventory_movement_id",
            name="uq_receipt_purpose_allocations_semi_movement",
        ),
        sa.CheckConstraint(
            "purpose_contract_status_snapshot IN ('legacy_unset','frozen')",
            name="ck_receipt_purpose_allocations_contract_status",
        ),
        sa.CheckConstraint(
            "((purpose_contract_status_snapshot = 'frozen' "
            "AND purchase_purpose_source_snapshot_id IS NOT NULL "
            "AND purchase_receipt_fact_id IS NOT NULL "
            "AND order_purpose_plan_sheet_qty_snapshot IS NOT NULL "
            "AND reserve_purpose_plan_sheet_qty_snapshot IS NOT NULL) OR "
            "(purpose_contract_status_snapshot = 'legacy_unset' "
            "AND purchase_purpose_source_snapshot_id IS NULL "
            "AND purchase_receipt_fact_id IS NULL "
            "AND order_purpose_plan_sheet_qty_snapshot IS NULL "
            "AND reserve_purpose_plan_sheet_qty_snapshot IS NULL))",
            name="ck_receipt_purpose_allocations_contract_links",
        ),
        sa.CheckConstraint(
            "((supplier_requisition_order_item_id IS NOT NULL "
            "AND material_requisition_item_id IS NULL) OR "
            "(supplier_requisition_order_item_id IS NULL "
            "AND material_requisition_item_id IS NOT NULL))",
            name="ck_receipt_purpose_allocations_exactly_one_source",
        ),
        sa.CheckConstraint(
            "source_kind IN ('order_item','requisition_item','bom_component',"
            "'direct_supplier_item')",
            name="ck_receipt_purpose_allocations_source_kind",
        ),
        sa.CheckConstraint(
            "((source_kind = 'order_item' AND source_order_item_id IS NOT NULL "
            "AND source_requisition_item_id IS NULL "
            "AND source_bom_requisition_source_id IS NULL) OR "
            "(source_kind = 'requisition_item' AND source_order_item_id IS NOT NULL "
            "AND source_requisition_item_id IS NOT NULL "
            "AND source_bom_requisition_source_id IS NULL) OR "
            "(source_kind = 'bom_component' AND source_order_item_id IS NULL "
            "AND source_requisition_item_id IS NULL "
            "AND source_bom_requisition_source_id IS NOT NULL) OR "
            "(source_kind = 'direct_supplier_item' "
            "AND supplier_requisition_order_item_id IS NOT NULL "
            "AND source_order_item_id IS NULL "
            "AND source_requisition_item_id IS NULL "
            "AND source_bom_requisition_source_id IS NULL))",
            name="ck_receipt_purpose_allocations_source_identity",
        ),
        sa.CheckConstraint(
            "component_type IN ('whole','cover','base')",
            name="ck_receipt_purpose_allocations_component_type",
        ),
        sa.CheckConstraint(
            "receipt_total_sheet_qty > 0 "
            "AND receipt_order_purpose_sheet_qty >= 0 "
            "AND receipt_reserve_purpose_sheet_qty >= 0 "
            "AND receipt_order_purpose_sheet_qty + receipt_reserve_purpose_sheet_qty "
            "= receipt_total_sheet_qty",
            name="ck_receipt_purpose_allocations_receipt_balance",
        ),
        sa.CheckConstraint(
            "cumulative_total_sheet_qty_before >= 0 "
            "AND cumulative_order_purpose_sheet_qty_before >= 0 "
            "AND cumulative_reserve_purpose_sheet_qty_before >= 0 "
            "AND cumulative_order_purpose_sheet_qty_before + "
            "cumulative_reserve_purpose_sheet_qty_before = "
            "cumulative_total_sheet_qty_before "
            "AND cumulative_total_sheet_qty_after = "
            "cumulative_total_sheet_qty_before + receipt_total_sheet_qty "
            "AND cumulative_order_purpose_sheet_qty_after = "
            "cumulative_order_purpose_sheet_qty_before + "
            "receipt_order_purpose_sheet_qty "
            "AND cumulative_reserve_purpose_sheet_qty_after = "
            "cumulative_reserve_purpose_sheet_qty_before + "
            "receipt_reserve_purpose_sheet_qty "
            "AND cumulative_order_purpose_sheet_qty_after + "
            "cumulative_reserve_purpose_sheet_qty_after = "
            "cumulative_total_sheet_qty_after",
            name="ck_receipt_purpose_allocations_cumulative_balance",
        ),
        sa.CheckConstraint(
            "purpose_contract_status_snapshot <> 'frozen' OR "
            "(order_purpose_plan_sheet_qty_snapshot >= 0 "
            "AND reserve_purpose_plan_sheet_qty_snapshot >= 0 "
            "AND ((reserve_purpose_plan_sheet_qty_snapshot > 0 "
            "AND cumulative_order_purpose_sheet_qty_after "
            "<= order_purpose_plan_sheet_qty_snapshot) OR "
            "(reserve_purpose_plan_sheet_qty_snapshot = 0 "
            "AND receipt_reserve_purpose_sheet_qty = 0 "
            "AND cumulative_reserve_purpose_sheet_qty_before = 0 "
            "AND cumulative_reserve_purpose_sheet_qty_after = 0)))",
            name="ck_receipt_purpose_allocations_frozen_purpose_policy",
        ),
        sa.CheckConstraint(
            "finished_output_qty_before >= 0 "
            "AND finished_output_qty_after >= finished_output_qty_before "
            "AND finished_output_qty_delta = "
            "finished_output_qty_after - finished_output_qty_before",
            name="ck_receipt_purpose_allocations_finished_balance",
        ),
        sa.CheckConstraint(
            "((finished_output_qty_delta = 0 "
            "AND production_completion_id IS NULL "
            "AND finished_inventory_lot_id IS NULL) OR "
            "(finished_output_qty_delta > 0 "
            "AND production_completion_id IS NOT NULL "
            "AND finished_inventory_lot_id IS NOT NULL))",
            name="ck_receipt_purpose_allocations_finished_links",
        ),
        sa.CheckConstraint(
            "((receipt_reserve_purpose_sheet_qty = 0 "
            "AND semi_finished_inventory_lot_id IS NULL "
            "AND initial_semi_inventory_movement_id IS NULL) OR "
            "(receipt_reserve_purpose_sheet_qty > 0 "
            "AND semi_finished_inventory_lot_id IS NOT NULL "
            "AND initial_semi_inventory_movement_id IS NOT NULL))",
            name="ck_receipt_purpose_allocations_semi_links",
        ),
        sa.CheckConstraint(
            "((purpose_contract_status_snapshot = 'frozen' "
            "AND sheet_cost IS NOT NULL AND sheet_cost >= 0 "
            "AND order_purpose_cost IS NOT NULL AND order_purpose_cost >= 0 "
            "AND reserve_purpose_cost IS NOT NULL AND reserve_purpose_cost >= 0 "
            "AND total_cost IS NOT NULL AND total_cost >= 0 "
            "AND capitalized_cost IS NOT NULL AND capitalized_cost >= 0 "
            "AND order_purpose_cost + reserve_purpose_cost = total_cost "
            "AND (finished_output_qty_delta > 0 OR capitalized_cost = 0)) OR "
            "(purpose_contract_status_snapshot = 'legacy_unset' "
            "AND sheet_cost IS NULL AND order_purpose_cost IS NULL "
            "AND reserve_purpose_cost IS NULL AND total_cost IS NULL "
            "AND capitalized_cost IS NULL))",
            name="ck_receipt_purpose_allocations_cost_balance",
        ),
        sa.CheckConstraint(
            "status = 'posted' AND version >= 1 "
            "AND length(trim(source_key)) > 0 "
            "AND length(trim(customer_name_snapshot)) > 0 "
            "AND length(request_hash) = 64",
            name="ck_receipt_purpose_allocations_frozen_fact",
        ),
    )
    op.create_index(
        "ix_receipt_purpose_allocations_formal_source",
        ALLOCATION_TABLE,
        ["supplier_requisition_order_item_id", "material_requisition_item_id"],
    )
    op.create_index(
        "ix_receipt_purpose_allocations_customer_source",
        ALLOCATION_TABLE,
        ["customer_id", "source_key"],
    )
    _create_immutability_guards(ALLOCATION_TABLE)


def _create_reversal_table() -> None:
    op.create_table(
        REVERSAL_TABLE,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("incoming_receipt_purpose_allocation_id", sa.Integer(), nullable=False),
        sa.Column("incoming_receipt_item_id", sa.Integer(), nullable=False),
        sa.Column("reversed_production_completion_id", sa.Integer(), nullable=True),
        sa.Column("reversed_finished_inventory_lot_id", sa.Integer(), nullable=True),
        sa.Column("reversed_semi_finished_inventory_lot_id", sa.Integer(), nullable=True),
        sa.Column("compensation_inventory_movement_id", sa.Integer(), nullable=True),
        sa.Column("cumulative_total_sheet_qty_before", sa.Integer(), nullable=False),
        sa.Column("cumulative_total_sheet_qty_after", sa.Integer(), nullable=False),
        sa.Column(
            "cumulative_order_purpose_sheet_qty_before", sa.Integer(), nullable=False
        ),
        sa.Column(
            "cumulative_order_purpose_sheet_qty_after", sa.Integer(), nullable=False
        ),
        sa.Column(
            "cumulative_reserve_purpose_sheet_qty_before", sa.Integer(), nullable=False
        ),
        sa.Column(
            "cumulative_reserve_purpose_sheet_qty_after", sa.Integer(), nullable=False
        ),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("reversed_by", sa.Integer(), nullable=False),
        sa.Column(
            "reversed_at",
            sa.DateTime(),
            server_default=sa.func.current_timestamp(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["incoming_receipt_purpose_allocation_id"],
            ["incoming_receipt_purpose_allocations.id"],
            name="fk_receipt_purpose_reversals_allocation",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["incoming_receipt_item_id"],
            ["incoming_receipt_items.id"],
            name="fk_receipt_purpose_reversals_receipt_item",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["reversed_production_completion_id"],
            ["production_completions.id"],
            name="fk_receipt_purpose_reversals_completion",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["reversed_finished_inventory_lot_id"],
            ["inventory_lots.id"],
            name="fk_receipt_purpose_reversals_finished_lot",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["reversed_semi_finished_inventory_lot_id"],
            ["inventory_lots.id"],
            name="fk_receipt_purpose_reversals_semi_lot",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["compensation_inventory_movement_id"],
            ["inventory_movements.id"],
            name="fk_receipt_purpose_reversals_compensation_movement",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["reversed_by"],
            ["users.id"],
            name="fk_receipt_purpose_reversals_reversed_by_users",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "incoming_receipt_purpose_allocation_id",
            name="uq_receipt_purpose_reversals_allocation",
        ),
        sa.UniqueConstraint(
            "incoming_receipt_item_id",
            name="uq_receipt_purpose_reversals_receipt_item",
        ),
        sa.UniqueConstraint(
            "compensation_inventory_movement_id",
            name="uq_receipt_purpose_reversals_compensation_movement",
        ),
        sa.CheckConstraint(
            "cumulative_total_sheet_qty_before > "
            "cumulative_total_sheet_qty_after "
            "AND cumulative_order_purpose_sheet_qty_before >= "
            "cumulative_order_purpose_sheet_qty_after "
            "AND cumulative_reserve_purpose_sheet_qty_before >= "
            "cumulative_reserve_purpose_sheet_qty_after "
            "AND cumulative_total_sheet_qty_after >= 0 "
            "AND cumulative_order_purpose_sheet_qty_after >= 0 "
            "AND cumulative_reserve_purpose_sheet_qty_after >= 0 "
            "AND cumulative_order_purpose_sheet_qty_before + "
            "cumulative_reserve_purpose_sheet_qty_before = "
            "cumulative_total_sheet_qty_before "
            "AND cumulative_order_purpose_sheet_qty_after + "
            "cumulative_reserve_purpose_sheet_qty_after = "
            "cumulative_total_sheet_qty_after",
            name="ck_receipt_purpose_reversals_cumulative_balance",
        ),
        sa.CheckConstraint(
            "length(request_hash) = 64",
            name="ck_receipt_purpose_reversals_request_hash",
        ),
    )
    op.create_index(
        "ix_receipt_purpose_reversals_receipt_item",
        REVERSAL_TABLE,
        ["incoming_receipt_item_id"],
    )
    _create_immutability_guards(REVERSAL_TABLE)


def _create_reversal_fact_table() -> None:
    op.create_table(
        REVERSAL_FACT_TABLE,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("target_kind", sa.String(length=30), nullable=False),
        sa.Column("target_id", sa.Integer(), nullable=False),
        sa.Column("incoming_receipt_item_id", sa.Integer(), nullable=True),
        sa.Column("incoming_receipt_purpose_reversal_id", sa.Integer(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=120), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("response_json", sa.Text(), nullable=False),
        sa.Column("reversed_by", sa.Integer(), nullable=False),
        sa.Column("reversed_at", sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False),
        sa.ForeignKeyConstraint(["incoming_receipt_item_id"], ["incoming_receipt_items.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["incoming_receipt_purpose_reversal_id"], [f"{REVERSAL_TABLE}.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["reversed_by"], ["users.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("idempotency_key", name="uq_incoming_reversal_facts_idem"),
        sa.UniqueConstraint("target_kind", "target_id", name="uq_incoming_reversal_facts_target"),
        sa.UniqueConstraint("incoming_receipt_item_id", name="uq_incoming_reversal_facts_receipt_item"),
        sa.UniqueConstraint("incoming_receipt_purpose_reversal_id", name="uq_incoming_reversal_facts_purpose_reversal"),
        sa.CheckConstraint("target_kind IN ('receipt_item','order_item','requisition_item') AND target_id > 0 AND length(trim(idempotency_key)) > 0 AND length(request_hash) = 64 AND length(trim(response_json)) > 0", name="ck_incoming_reversal_facts_frozen"),
        sa.CheckConstraint("((target_kind = 'receipt_item' AND incoming_receipt_item_id = target_id) OR (target_kind IN ('order_item','requisition_item') AND incoming_receipt_item_id IS NULL AND incoming_receipt_purpose_reversal_id IS NULL))", name="ck_incoming_reversal_facts_target_links"),
    )
    _create_immutability_guards(REVERSAL_FACT_TABLE)


def _create_batch_fact_table() -> None:
    op.create_table(
        BATCH_FACT_TABLE,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("idempotency_key", sa.String(length=120), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("scope_hash", sa.String(length=64), nullable=False),
        sa.Column("response_json", sa.Text(), nullable=False),
        sa.Column("received_by", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False),
        sa.ForeignKeyConstraint(["received_by"], ["users.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("idempotency_key", name="uq_incoming_receipt_batch_facts_idem"),
        sa.CheckConstraint("length(trim(idempotency_key)) > 0 AND length(request_hash) = 64 AND length(scope_hash) = 64 AND length(trim(response_json)) > 0", name="ck_incoming_receipt_batch_facts_frozen"),
    )
    _create_immutability_guards(BATCH_FACT_TABLE)


def upgrade() -> None:
    connection = op.get_bind()

    supplier_item_triggers = _drop_sqlite_triggers_referencing(
        connection, "supplier_requisition_order_items"
    )
    requisition_item_triggers = _drop_sqlite_triggers_referencing(
        connection, "material_requisition_items"
    )
    with op.batch_alter_table("supplier_requisition_order_items") as batch:
        batch.add_column(
            sa.Column(
                "purpose_contract_status",
                sa.String(length=20),
                server_default="legacy_unset",
                nullable=False,
            )
        )
    with op.batch_alter_table("material_requisition_items") as batch:
        batch.add_column(
            sa.Column(
                "purpose_contract_status",
                sa.String(length=20),
                server_default="legacy_unset",
                nullable=False,
            )
        )
        batch.add_column(
            sa.Column("version", sa.Integer(), server_default="1", nullable=False)
        )

    connection.execute(
        sa.text(
            "UPDATE supplier_requisition_order_items "
            "SET purpose_contract_status='frozen' "
            "WHERE EXISTS (SELECT 1 FROM purchase_purpose_source_snapshots p "
            "WHERE p.supplier_requisition_order_item_id="
            "supplier_requisition_order_items.id)"
        )
    )
    connection.execute(
        sa.text(
            "UPDATE material_requisition_items "
            "SET purpose_contract_status='frozen' "
            "WHERE EXISTS (SELECT 1 FROM purchase_purpose_source_snapshots p "
            "WHERE p.material_requisition_item_id=material_requisition_items.id)"
        )
    )

    with op.batch_alter_table("supplier_requisition_order_items") as batch:
        batch.create_check_constraint(
            "ck_supplier_requisition_order_items_purpose_contract_status",
            "purpose_contract_status IN ('legacy_unset','frozen')",
        )
    with op.batch_alter_table("material_requisition_items") as batch:
        batch.create_check_constraint(
            "ck_material_requisition_items_purpose_contract_status",
            "purpose_contract_status IN ('legacy_unset','frozen')",
        )
        batch.create_check_constraint(
            "ck_material_requisition_items_version", "version >= 1"
        )
    _restore_sqlite_triggers(connection, requisition_item_triggers)
    _restore_sqlite_triggers(connection, supplier_item_triggers)

    production_task_triggers = _drop_sqlite_triggers_referencing(
        connection, "production_tasks"
    )
    with op.batch_alter_table("production_tasks") as batch:
        batch.add_column(
            sa.Column(
                "task_role",
                sa.String(length=30),
                server_default="order_main",
                nullable=False,
            )
        )
    connection.execute(
        sa.text(
            "UPDATE production_tasks SET task_role='component_internal' "
            "WHERE sales_order_item_bom_component_id IS NOT NULL"
        )
    )
    with op.batch_alter_table("production_tasks") as batch:
        batch.drop_constraint("fk_production_tasks_bom_component", type_="foreignkey")
        batch.create_foreign_key(
            "fk_production_tasks_bom_component",
            "sales_order_item_bom_components",
            ["sales_order_item_bom_component_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.create_check_constraint(
            "ck_production_tasks_task_role",
            "task_role IN ('order_main','component_internal')",
        )
        batch.create_check_constraint(
            "ck_production_tasks_role_component",
            "((task_role = 'order_main' "
            "AND sales_order_item_bom_component_id IS NULL) OR "
            "(task_role = 'component_internal' "
            "AND sales_order_item_bom_component_id IS NOT NULL))",
        )
    _restore_sqlite_triggers(connection, production_task_triggers)

    production_completion_triggers = _drop_sqlite_triggers_referencing(
        connection, "production_completions"
    )
    with op.batch_alter_table("production_completions") as batch:
        batch.add_column(
            sa.Column(
                "origin",
                sa.String(length=20),
                server_default="manual",
                nullable=False,
            )
        )
        batch.create_check_constraint(
            "ck_production_completions_origin",
            "origin IN ('manual','receipt_auto')",
        )
    _restore_sqlite_triggers(connection, production_completion_triggers)

    lot_triggers = _drop_sqlite_triggers_referencing(connection, "inventory_lots")
    with op.batch_alter_table("inventory_lots") as batch:
        batch.drop_constraint("ck_inventory_lots_source_type", type_="check")
        batch.create_check_constraint(
            "ck_inventory_lots_source_type",
            "source_type IN ('manual','production_completion','production_surplus',"
            "'purchase_surplus','purchase_reserve','stocktake','transfer',"
            "'replenishment','delivery_return')",
        )
    _restore_sqlite_triggers(connection, lot_triggers)

    _create_material_variance_tables()
    _create_price_table()
    _create_allocation_table()
    _create_reversal_table()
    _create_reversal_fact_table()
    _create_batch_fact_table()


def _assert_safe_downgrade() -> None:
    connection = op.get_bind()
    counts = [
        int(
            connection.execute(sa.text(f"SELECT COUNT(*) FROM {PRICE_TABLE}")).scalar()
            or 0
        ),
        int(
            connection.execute(
                sa.text(f"SELECT COUNT(*) FROM {ALLOCATION_TABLE}")
            ).scalar()
            or 0
        ),
        int(
            connection.execute(sa.text(f"SELECT COUNT(*) FROM {REVERSAL_TABLE}")).scalar()
            or 0
        ),
        int(connection.execute(sa.text(f"SELECT COUNT(*) FROM {VARIANCE_TABLE}")).scalar() or 0),
        int(connection.execute(sa.text(f"SELECT COUNT(*) FROM {VARIANCE_APPROVAL_TABLE}")).scalar() or 0),
        int(connection.execute(sa.text(f"SELECT COUNT(*) FROM {REVERSAL_FACT_TABLE}")).scalar() or 0),
        int(connection.execute(sa.text(f"SELECT COUNT(*) FROM {BATCH_FACT_TABLE}")).scalar() or 0),
        int(
            connection.execute(
                sa.text(
                    "SELECT COUNT(*) FROM production_completions "
                    "WHERE origin='receipt_auto'"
                )
            ).scalar()
            or 0
        ),
        int(
            connection.execute(
                sa.text(
                    "SELECT COUNT(*) FROM inventory_lots "
                    "WHERE source_type='purchase_reserve'"
                )
            ).scalar()
            or 0
        ),
    ]
    if any(counts):
        raise RuntimeError(DOWNGRADE_BLOCKED_MESSAGE)


def downgrade() -> None:
    _assert_safe_downgrade()
    connection = op.get_bind()

    _drop_immutability_guards(BATCH_FACT_TABLE)
    op.drop_table(BATCH_FACT_TABLE)

    _drop_immutability_guards(REVERSAL_FACT_TABLE)
    op.drop_table(REVERSAL_FACT_TABLE)

    _drop_immutability_guards(REVERSAL_TABLE)
    op.drop_index(
        "ix_receipt_purpose_reversals_receipt_item",
        table_name=REVERSAL_TABLE,
    )
    op.drop_table(REVERSAL_TABLE)

    _drop_immutability_guards(ALLOCATION_TABLE)
    op.drop_index(
        "ix_receipt_purpose_allocations_customer_source",
        table_name=ALLOCATION_TABLE,
    )
    op.drop_index(
        "ix_receipt_purpose_allocations_formal_source",
        table_name=ALLOCATION_TABLE,
    )
    op.drop_table(ALLOCATION_TABLE)

    _drop_immutability_guards(PRICE_TABLE)
    op.drop_index(
        "ix_purchase_receipt_facts_purpose_snapshot", table_name=PRICE_TABLE
    )
    op.drop_index(
        "ix_purchase_receipt_facts_requisition_item", table_name=PRICE_TABLE
    )
    op.drop_index("ix_purchase_receipt_facts_supplier_item", table_name=PRICE_TABLE)
    op.drop_table(PRICE_TABLE)

    _drop_immutability_guards(VARIANCE_APPROVAL_TABLE)
    op.drop_table(VARIANCE_APPROVAL_TABLE)
    _drop_immutability_guards(VARIANCE_TABLE)
    op.drop_index("ix_purchase_material_variances_source", table_name=VARIANCE_TABLE)
    op.drop_table(VARIANCE_TABLE)

    lot_triggers = _drop_sqlite_triggers_referencing(connection, "inventory_lots")
    with op.batch_alter_table("inventory_lots") as batch:
        batch.drop_constraint("ck_inventory_lots_source_type", type_="check")
        batch.create_check_constraint(
            "ck_inventory_lots_source_type",
            "source_type IN ('manual','production_completion','production_surplus',"
            "'purchase_surplus','stocktake','transfer','replenishment','delivery_return')",
        )
    _restore_sqlite_triggers(connection, lot_triggers)

    production_completion_triggers = _drop_sqlite_triggers_referencing(
        connection, "production_completions"
    )
    with op.batch_alter_table("production_completions") as batch:
        batch.drop_constraint("ck_production_completions_origin", type_="check")
        batch.drop_column("origin")
    _restore_sqlite_triggers(connection, production_completion_triggers)

    production_task_triggers = _drop_sqlite_triggers_referencing(
        connection, "production_tasks"
    )
    with op.batch_alter_table("production_tasks") as batch:
        batch.drop_constraint("ck_production_tasks_role_component", type_="check")
        batch.drop_constraint("ck_production_tasks_task_role", type_="check")
        batch.drop_constraint("fk_production_tasks_bom_component", type_="foreignkey")
        batch.create_foreign_key(
            "fk_production_tasks_bom_component",
            "sales_order_item_bom_components",
            ["sales_order_item_bom_component_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch.drop_column("task_role")
    _restore_sqlite_triggers(connection, production_task_triggers)

    supplier_item_triggers = _drop_sqlite_triggers_referencing(
        connection, "supplier_requisition_order_items"
    )
    requisition_item_triggers = _drop_sqlite_triggers_referencing(
        connection, "material_requisition_items"
    )
    with op.batch_alter_table("material_requisition_items") as batch:
        batch.drop_constraint("ck_material_requisition_items_version", type_="check")
        batch.drop_constraint(
            "ck_material_requisition_items_purpose_contract_status", type_="check"
        )
        batch.drop_column("version")
        batch.drop_column("purpose_contract_status")
    with op.batch_alter_table("supplier_requisition_order_items") as batch:
        batch.drop_constraint(
            "ck_supplier_requisition_order_items_purpose_contract_status",
            type_="check",
        )
        batch.drop_column("purpose_contract_status")
    _restore_sqlite_triggers(connection, requisition_item_triggers)
    _restore_sqlite_triggers(connection, supplier_item_triggers)
