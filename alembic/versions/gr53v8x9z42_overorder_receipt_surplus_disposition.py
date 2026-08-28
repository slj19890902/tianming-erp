"""add explicit over-receipt disposition and reserve-to-finished audit

Revision ID: gr53v8x9z42
Revises: gq52v8x9z41
Create Date: 2026-08-28
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "gr53v8x9z42"
down_revision = "gq52v8x9z41"
branch_labels = None
depends_on = None


ALLOCATION_TABLE = "incoming_receipt_purpose_allocations"
CONVERSION_TABLE = "production_completion_reserve_conversions"
CONVERSION_REVERSAL_TABLE = "production_completion_reserve_conversion_reversals"


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
    if dialect == "sqlite":
        op.execute(
            f"""
            CREATE TRIGGER trg_{table_name}_immutable_update
            BEFORE UPDATE ON {table_name}
            FOR EACH ROW
            BEGIN
                SELECT RAISE(ABORT, '{table_name} rows are immutable');
            END
            """
        )
        op.execute(
            f"""
            CREATE TRIGGER trg_{table_name}_immutable_delete
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
            CREATE OR REPLACE FUNCTION {table_name}_immutable()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                RAISE EXCEPTION '{table_name} rows are immutable';
            END;
            $$
            """
        )
        op.execute(
            f"""
            CREATE TRIGGER trg_{table_name}_immutable
            BEFORE UPDATE OR DELETE ON {table_name}
            FOR EACH ROW EXECUTE FUNCTION {table_name}_immutable()
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


def _new_frozen_policy() -> str:
    return (
        "purpose_contract_status_snapshot <> 'frozen' OR "
        "(order_purpose_plan_sheet_qty_snapshot >= 0 "
        "AND reserve_purpose_plan_sheet_qty_snapshot >= 0 "
        "AND ((surplus_disposition = 'finished' "
        "AND receipt_reserve_purpose_sheet_qty = 0) OR "
        "(surplus_disposition = 'semi_finished_reserve' "
        "AND receipt_reserve_purpose_sheet_qty > 0 "
        "AND cumulative_order_purpose_sheet_qty_after "
        "<= order_purpose_plan_sheet_qty_snapshot) OR "
        "(surplus_disposition = 'not_applicable' "
        "AND receipt_reserve_purpose_sheet_qty = 0 "
        "AND cumulative_order_purpose_sheet_qty_after "
        "<= order_purpose_plan_sheet_qty_snapshot)))"
    )


def _old_frozen_policy() -> str:
    return (
        "purpose_contract_status_snapshot <> 'frozen' OR "
        "(order_purpose_plan_sheet_qty_snapshot >= 0 "
        "AND reserve_purpose_plan_sheet_qty_snapshot >= 0 "
        "AND ((reserve_purpose_plan_sheet_qty_snapshot > 0 "
        "AND cumulative_order_purpose_sheet_qty_after "
        "<= order_purpose_plan_sheet_qty_snapshot) OR "
        "(reserve_purpose_plan_sheet_qty_snapshot = 0 "
        "AND receipt_reserve_purpose_sheet_qty = 0 "
        "AND cumulative_reserve_purpose_sheet_qty_before = 0 "
        "AND cumulative_reserve_purpose_sheet_qty_after = 0)))"
    )


def upgrade() -> None:
    connection = op.get_bind()
    allocation_triggers = _drop_sqlite_triggers_referencing(
        connection, ALLOCATION_TABLE
    )
    with op.batch_alter_table(ALLOCATION_TABLE) as batch:
        batch.add_column(
            sa.Column("surplus_disposition", sa.String(30), nullable=True)
        )
    connection.execute(
        sa.text(
            f"UPDATE {ALLOCATION_TABLE} "
            "SET surplus_disposition = CASE "
            "WHEN receipt_reserve_purpose_sheet_qty > 0 "
            "THEN 'semi_finished_reserve' "
            "WHEN cumulative_order_purpose_sheet_qty_after > "
            "order_purpose_plan_sheet_qty_snapshot "
            "THEN 'finished' ELSE 'not_applicable' END"
        )
    )
    with op.batch_alter_table(ALLOCATION_TABLE, recreate="always") as batch:
        batch.drop_constraint(
            "ck_receipt_purpose_allocations_frozen_purpose_policy",
            type_="check",
        )
        batch.alter_column(
            "surplus_disposition",
            existing_type=sa.String(30),
            nullable=False,
            server_default="not_applicable",
        )
        batch.create_check_constraint(
            "ck_receipt_purpose_allocations_surplus_disposition",
            "surplus_disposition IN "
            "('not_applicable','finished','semi_finished_reserve')",
        )
        batch.create_check_constraint(
            "ck_receipt_purpose_allocations_frozen_purpose_policy",
            _new_frozen_policy(),
        )
    _restore_sqlite_triggers(connection, allocation_triggers)

    op.create_table(
        CONVERSION_TABLE,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("production_completion_id", sa.Integer(), nullable=False),
        sa.Column("receipt_purpose_allocation_id", sa.Integer(), nullable=False),
        sa.Column("semi_finished_inventory_lot_id", sa.Integer(), nullable=False),
        sa.Column("finished_inventory_lot_id", sa.Integer(), nullable=False),
        sa.Column("semi_consume_movement_id", sa.Integer(), nullable=False),
        sa.Column("finished_adjust_movement_id", sa.Integer(), nullable=False),
        sa.Column("converted_sheet_quantity", sa.Integer(), nullable=False),
        sa.Column("finished_quantity_delta", sa.Integer(), nullable=False),
        sa.Column("supported_finished_quantity_before", sa.Integer(), nullable=False),
        sa.Column("supported_finished_quantity_after", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(120), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.func.current_timestamp(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["production_completion_id"],
            ["production_completions.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["receipt_purpose_allocation_id"],
            [f"{ALLOCATION_TABLE}.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["semi_finished_inventory_lot_id"],
            ["inventory_lots.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["finished_inventory_lot_id"],
            ["inventory_lots.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["semi_consume_movement_id"],
            ["inventory_movements.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["finished_adjust_movement_id"],
            ["inventory_movements.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], ondelete="SET NULL"
        ),
        sa.UniqueConstraint(
            "semi_consume_movement_id",
            name="uq_completion_reserve_conversions_consume_movement",
        ),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_completion_reserve_conversions_idempotency",
        ),
        sa.CheckConstraint(
            "converted_sheet_quantity > 0 AND finished_quantity_delta >= 0",
            name="ck_completion_reserve_conversions_quantities",
        ),
        sa.CheckConstraint(
            "supported_finished_quantity_before >= 0 "
            "AND supported_finished_quantity_after >= "
            "supported_finished_quantity_before "
            "AND finished_quantity_delta = supported_finished_quantity_after - "
            "supported_finished_quantity_before",
            name="ck_completion_reserve_conversions_capacity",
        ),
        sa.CheckConstraint(
            "length(trim(idempotency_key)) > 0 AND length(request_hash) = 64",
            name="ck_completion_reserve_conversions_frozen",
        ),
    )
    op.create_index(
        "ix_completion_reserve_conversions_completion",
        CONVERSION_TABLE,
        ["production_completion_id", "id"],
    )
    op.create_index(
        "ix_completion_reserve_conversions_allocation",
        CONVERSION_TABLE,
        ["receipt_purpose_allocation_id", "id"],
    )
    _create_immutability_guards(CONVERSION_TABLE)

    op.create_table(
        CONVERSION_REVERSAL_TABLE,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "production_completion_reserve_conversion_id",
            sa.Integer(),
            nullable=False,
        ),
        sa.Column("semi_reverse_movement_id", sa.Integer(), nullable=False),
        sa.Column("finished_reversal_movement_id", sa.Integer(), nullable=False),
        sa.Column("restored_sheet_quantity", sa.Integer(), nullable=False),
        sa.Column("reversed_finished_quantity_delta", sa.Integer(), nullable=False),
        sa.Column("supported_finished_quantity_before", sa.Integer(), nullable=False),
        sa.Column("supported_finished_quantity_after", sa.Integer(), nullable=False),
        sa.Column("reason_type", sa.String(30), nullable=False),
        sa.Column("idempotency_key", sa.String(120), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.func.current_timestamp(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["production_completion_reserve_conversion_id"],
            [f"{CONVERSION_TABLE}.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["semi_reverse_movement_id"],
            ["inventory_movements.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["finished_reversal_movement_id"],
            ["inventory_movements.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], ondelete="SET NULL"
        ),
        sa.UniqueConstraint(
            "semi_reverse_movement_id",
            name="uq_completion_reserve_conversion_reversals_semi_movement",
        ),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_completion_reserve_conversion_reversals_idempotency",
        ),
        sa.CheckConstraint(
            "restored_sheet_quantity > 0 "
            "AND reversed_finished_quantity_delta >= 0",
            name="ck_completion_reserve_conversion_reversals_quantities",
        ),
        sa.CheckConstraint(
            "supported_finished_quantity_before >= "
            "supported_finished_quantity_after "
            "AND reversed_finished_quantity_delta = "
            "supported_finished_quantity_before - "
            "supported_finished_quantity_after",
            name="ck_completion_reserve_conversion_reversals_capacity",
        ),
        sa.CheckConstraint(
            "reason_type IN ('actual_adjustment','completion_reversal')",
            name="ck_completion_reserve_conversion_reversals_reason",
        ),
        sa.CheckConstraint(
            "length(trim(idempotency_key)) > 0 AND length(request_hash) = 64",
            name="ck_completion_reserve_conversion_reversals_frozen",
        ),
    )
    op.create_index(
        "ix_completion_reserve_conversion_reversals_conversion",
        CONVERSION_REVERSAL_TABLE,
        ["production_completion_reserve_conversion_id", "id"],
    )
    _create_immutability_guards(CONVERSION_REVERSAL_TABLE)


def downgrade() -> None:
    connection = op.get_bind()
    conversion_reversal_count = int(
        connection.execute(
            sa.text(f"SELECT COUNT(*) FROM {CONVERSION_REVERSAL_TABLE}")
        ).scalar_one()
    )
    conversion_count = int(
        connection.execute(
            sa.text(f"SELECT COUNT(*) FROM {CONVERSION_TABLE}")
        ).scalar_one()
    )
    incompatible_finished_choice_count = int(
        connection.execute(
            sa.text(
                f"SELECT COUNT(*) FROM {ALLOCATION_TABLE} "
                "WHERE surplus_disposition = 'finished' "
                "AND (reserve_purpose_plan_sheet_qty_snapshot > 0 "
                "OR cumulative_reserve_purpose_sheet_qty_before <> 0 "
                "OR cumulative_reserve_purpose_sheet_qty_after <> 0)"
            )
        ).scalar_one()
    )
    if conversion_count or conversion_reversal_count or incompatible_finished_choice_count:
        raise RuntimeError(
            "P0-32 已存在超收做成品或备库转成品事实，拒绝破坏性降级；"
            "请恢复升级前完整备份"
        )

    _drop_immutability_guards(CONVERSION_REVERSAL_TABLE)
    op.drop_index(
        "ix_completion_reserve_conversion_reversals_conversion",
        table_name=CONVERSION_REVERSAL_TABLE,
    )
    op.drop_table(CONVERSION_REVERSAL_TABLE)

    _drop_immutability_guards(CONVERSION_TABLE)
    op.drop_index(
        "ix_completion_reserve_conversions_allocation",
        table_name=CONVERSION_TABLE,
    )
    op.drop_index(
        "ix_completion_reserve_conversions_completion",
        table_name=CONVERSION_TABLE,
    )
    op.drop_table(CONVERSION_TABLE)

    allocation_triggers = _drop_sqlite_triggers_referencing(
        connection, ALLOCATION_TABLE
    )
    with op.batch_alter_table(ALLOCATION_TABLE, recreate="always") as batch:
        batch.drop_constraint(
            "ck_receipt_purpose_allocations_frozen_purpose_policy",
            type_="check",
        )
        batch.drop_constraint(
            "ck_receipt_purpose_allocations_surplus_disposition",
            type_="check",
        )
        batch.drop_column("surplus_disposition")
        batch.create_check_constraint(
            "ck_receipt_purpose_allocations_frozen_purpose_policy",
            _old_frozen_policy(),
        )
    _restore_sqlite_triggers(connection, allocation_triggers)
