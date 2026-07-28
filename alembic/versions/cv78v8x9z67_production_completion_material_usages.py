"""record per-reservation production completion material usage

Revision ID: cv78v8x9z67
Revises: cu77v8x9z66
Create Date: 2026-07-28
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "cv78v8x9z67"
down_revision: Union[str, Sequence[str], None] = "cu77v8x9z66"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE_NAME = "production_completion_material_usages"
UPDATE_TRIGGER = "trg_production_completion_material_usages_update_guard"
DELETE_TRIGGER = "trg_production_completion_material_usages_delete_guard"
POSTGRES_GUARD_FUNCTION = "p111c_guard_production_completion_material_usage"
IMMUTABLE_COLUMNS = (
    "id",
    "completion_id",
    "task_id",
    "order_item_id",
    "reservation_id",
    "inventory_lot_id",
    "expected_lot_version",
    "result_lot_version",
    "assigned_stock_quantity",
    "actual_consumed_stock_quantity",
    "returned_intact_stock_quantity",
    "damaged_stock_quantity",
    "offcut_stock_quantity",
    "remaining_reserved_stock_quantity",
    "credited_requirement_quantity",
    "actual_credited_requirement_quantity",
    "yield_factor",
    "variance_reason_code",
    "variance_reason_text",
    "return_confirmed",
    "return_confirmed_by",
    "return_confirmed_at",
    "consume_movement_id",
    "release_movement_id",
    "operator_id",
    "created_at",
)


def _create_immutable_guards(connection: sa.Connection) -> None:
    if connection.dialect.name == "sqlite":
        immutable_comparison = " AND ".join(
            f"NEW.{column} IS OLD.{column}" for column in IMMUTABLE_COLUMNS
        )
        connection.execute(
            sa.text(
                f"""
                CREATE TRIGGER {UPDATE_TRIGGER}
                BEFORE UPDATE ON {TABLE_NAME}
                WHEN NOT (
                    OLD.status = 'posted'
                    AND NEW.status = 'reversed'
                    AND NEW.reversed_at IS NOT NULL
                    AND NEW.reversed_by IS NOT NULL
                    AND NEW.reversal_reason IS NOT NULL
                    AND length(trim(NEW.reversal_reason)) > 0
                    AND (
                        (
                            OLD.return_status = 'released'
                            AND NEW.return_status = 'reversed'
                        )
                        OR (
                            OLD.return_status = 'not_applicable'
                            AND NEW.return_status = 'not_applicable'
                        )
                    )
                    AND {immutable_comparison}
                )
                BEGIN
                    SELECT RAISE(
                        ABORT,
                        'production completion material usage is immutable'
                    );
                END
                """
            )
        )
        connection.execute(
            sa.text(
                f"""
                CREATE TRIGGER {DELETE_TRIGGER}
                BEFORE DELETE ON {TABLE_NAME}
                BEGIN
                    SELECT RAISE(
                        ABORT,
                        'production completion material usage cannot be deleted'
                    );
                END
                """
            )
        )
    elif connection.dialect.name == "postgresql":
        immutable_comparison = " AND ".join(
            f"NEW.{column} IS NOT DISTINCT FROM OLD.{column}"
            for column in IMMUTABLE_COLUMNS
        )
        connection.execute(
            sa.text(
                f"""
                CREATE FUNCTION {POSTGRES_GUARD_FUNCTION}() RETURNS trigger AS $$
                BEGIN
                    IF TG_OP = 'DELETE' THEN
                        RAISE EXCEPTION
                            'production completion material usage cannot be deleted';
                    END IF;
                    IF NOT (
                        OLD.status = 'posted'
                        AND NEW.status = 'reversed'
                        AND NEW.reversed_at IS NOT NULL
                        AND NEW.reversed_by IS NOT NULL
                        AND NEW.reversal_reason IS NOT NULL
                        AND NULLIF(BTRIM(NEW.reversal_reason), '') IS NOT NULL
                        AND (
                            (
                                OLD.return_status = 'released'
                                AND NEW.return_status = 'reversed'
                            )
                            OR (
                                OLD.return_status = 'not_applicable'
                                AND NEW.return_status = 'not_applicable'
                            )
                        )
                        AND {immutable_comparison}
                    ) THEN
                        RAISE EXCEPTION
                            'production completion material usage is immutable';
                    END IF;
                    RETURN NEW;
                END;
                $$ LANGUAGE plpgsql;

                CREATE TRIGGER {UPDATE_TRIGGER}
                BEFORE UPDATE ON {TABLE_NAME}
                FOR EACH ROW EXECUTE FUNCTION {POSTGRES_GUARD_FUNCTION}();

                CREATE TRIGGER {DELETE_TRIGGER}
                BEFORE DELETE ON {TABLE_NAME}
                FOR EACH ROW EXECUTE FUNCTION {POSTGRES_GUARD_FUNCTION}();
                """
            )
        )


def _drop_immutable_guards(connection: sa.Connection) -> None:
    if connection.dialect.name == "sqlite":
        connection.execute(sa.text(f"DROP TRIGGER IF EXISTS {DELETE_TRIGGER}"))
        connection.execute(sa.text(f"DROP TRIGGER IF EXISTS {UPDATE_TRIGGER}"))
    elif connection.dialect.name == "postgresql":
        connection.execute(
            sa.text(f"DROP TRIGGER IF EXISTS {DELETE_TRIGGER} ON {TABLE_NAME}")
        )
        connection.execute(
            sa.text(f"DROP TRIGGER IF EXISTS {UPDATE_TRIGGER} ON {TABLE_NAME}")
        )
        connection.execute(
            sa.text(f"DROP FUNCTION IF EXISTS {POSTGRES_GUARD_FUNCTION}()")
        )


def upgrade() -> None:
    op.create_table(
        "production_completion_material_usages",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("completion_id", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.Integer(), nullable=False),
        sa.Column("order_item_id", sa.Integer(), nullable=False),
        sa.Column("reservation_id", sa.Integer(), nullable=False),
        sa.Column("inventory_lot_id", sa.Integer(), nullable=False),
        sa.Column("expected_lot_version", sa.Integer(), nullable=False),
        sa.Column("result_lot_version", sa.Integer(), nullable=False),
        sa.Column("assigned_stock_quantity", sa.Integer(), nullable=False),
        sa.Column("actual_consumed_stock_quantity", sa.Integer(), nullable=False),
        sa.Column(
            "returned_intact_stock_quantity",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "damaged_stock_quantity",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "offcut_stock_quantity",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "remaining_reserved_stock_quantity",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column("credited_requirement_quantity", sa.Integer(), nullable=False),
        sa.Column(
            "actual_credited_requirement_quantity",
            sa.Integer(),
            nullable=False,
        ),
        sa.Column("yield_factor", sa.Integer(), nullable=False),
        sa.Column("variance_reason_code", sa.String(length=50), nullable=True),
        sa.Column("variance_reason_text", sa.Text(), nullable=True),
        sa.Column(
            "return_confirmed",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
        sa.Column("return_confirmed_by", sa.Integer(), nullable=True),
        sa.Column("return_confirmed_at", sa.DateTime(), nullable=True),
        sa.Column(
            "return_status",
            sa.String(length=30),
            server_default="not_applicable",
            nullable=False,
        ),
        sa.Column("consume_movement_id", sa.Integer(), nullable=True),
        sa.Column("release_movement_id", sa.Integer(), nullable=True),
        sa.Column(
            "status",
            sa.String(length=20),
            server_default="posted",
            nullable=False,
        ),
        sa.Column("operator_id", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.func.current_timestamp(),
            nullable=False,
        ),
        sa.Column("reversed_at", sa.DateTime(), nullable=True),
        sa.Column("reversed_by", sa.Integer(), nullable=True),
        sa.Column("reversal_reason", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "assigned_stock_quantity > 0 "
            "AND actual_consumed_stock_quantity >= 0 "
            "AND returned_intact_stock_quantity >= 0 "
            "AND damaged_stock_quantity >= 0 "
            "AND offcut_stock_quantity >= 0 "
            "AND remaining_reserved_stock_quantity >= 0",
            name="ck_production_completion_material_usages_nonnegative",
        ),
        sa.CheckConstraint(
            "assigned_stock_quantity = actual_consumed_stock_quantity "
            "+ returned_intact_stock_quantity + damaged_stock_quantity "
            "+ offcut_stock_quantity + remaining_reserved_stock_quantity",
            name="ck_production_completion_material_usages_conservation",
        ),
        sa.CheckConstraint(
            "remaining_reserved_stock_quantity = 0",
            name="ck_production_completion_material_usages_no_remaining",
        ),
        sa.CheckConstraint(
            "expected_lot_version >= 1 "
            "AND result_lot_version > expected_lot_version "
            "AND credited_requirement_quantity >= 0 "
            "AND credited_requirement_quantity "
            "<= assigned_stock_quantity * yield_factor "
            "AND actual_credited_requirement_quantity >= 0 "
            "AND actual_credited_requirement_quantity <= credited_requirement_quantity "
            "AND actual_credited_requirement_quantity "
            "<= actual_consumed_stock_quantity * yield_factor "
            "AND yield_factor >= 1",
            name="ck_production_completion_material_usages_credits",
        ),
        sa.CheckConstraint(
            "actual_consumed_stock_quantity = assigned_stock_quantity "
            "OR variance_reason_code "
            "IN ('intact_return','damaged','offcut','mixed')",
            name="ck_production_completion_material_usages_variance_reason",
        ),
        sa.CheckConstraint(
            "((actual_consumed_stock_quantity + damaged_stock_quantity "
            "+ offcut_stock_quantity = 0 AND consume_movement_id IS NULL) "
            "OR (actual_consumed_stock_quantity + damaged_stock_quantity "
            "+ offcut_stock_quantity > 0 AND consume_movement_id IS NOT NULL)) "
            "AND ((returned_intact_stock_quantity = 0 AND release_movement_id IS NULL) "
            "OR (returned_intact_stock_quantity > 0 AND release_movement_id IS NOT NULL))",
            name="ck_production_completion_material_usages_movements",
        ),
        sa.CheckConstraint(
            "status IN ('posted','reversed')",
            name="ck_production_completion_material_usages_status",
        ),
        sa.CheckConstraint(
            "return_status IN ('not_applicable','released','reversed')",
            name="ck_production_completion_material_usages_return_status",
        ),
        sa.CheckConstraint(
            "(returned_intact_stock_quantity = 0 "
            "AND return_confirmed IS FALSE "
            "AND return_status = 'not_applicable' "
            "AND return_confirmed_by IS NULL "
            "AND return_confirmed_at IS NULL) "
            "OR (returned_intact_stock_quantity > 0 "
            "AND return_confirmed IS TRUE "
            "AND return_status IN ('released','reversed') "
            "AND return_confirmed_by IS NOT NULL "
            "AND return_confirmed_at IS NOT NULL)",
            name="ck_production_completion_material_usages_return_confirmation",
        ),
        sa.CheckConstraint(
            "(status = 'posted' AND reversed_at IS NULL "
            "AND reversed_by IS NULL AND reversal_reason IS NULL) "
            "OR (status = 'reversed' AND reversed_at IS NOT NULL "
            "AND reversal_reason IS NOT NULL)",
            name="ck_production_completion_material_usages_reversal",
        ),
        sa.ForeignKeyConstraint(
            ["completion_id"],
            ["production_completions.id"],
            name="fk_production_completion_material_usages_completion",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["production_tasks.id"],
            name="fk_production_completion_material_usages_task",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["order_item_id"],
            ["sales_order_items.id"],
            name="fk_production_completion_material_usages_order_item",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["reservation_id"],
            ["inventory_reservations.id"],
            name="fk_production_completion_material_usages_reservation",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["inventory_lot_id"],
            ["inventory_lots.id"],
            name="fk_production_completion_material_usages_lot",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["consume_movement_id"],
            ["inventory_movements.id"],
            name="fk_production_completion_material_usages_consume_movement",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["release_movement_id"],
            ["inventory_movements.id"],
            name="fk_production_completion_material_usages_release_movement",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["operator_id"],
            ["users.id"],
            name="fk_production_completion_material_usages_operator",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["return_confirmed_by"],
            ["users.id"],
            name="fk_production_completion_material_usages_return_confirmed_by",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["reversed_by"],
            ["users.id"],
            name="fk_production_completion_material_usages_reversed_by",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "completion_id",
            "reservation_id",
            name="uq_production_completion_material_usages_completion_reservation",
        ),
        sa.UniqueConstraint(
            "consume_movement_id",
            name="uq_production_completion_material_usages_consume_movement",
        ),
        sa.UniqueConstraint(
            "release_movement_id",
            name="uq_production_completion_material_usages_release_movement",
        ),
    )
    op.create_index(
        "ix_production_completion_material_usages_task",
        "production_completion_material_usages",
        ["task_id", "status"],
    )
    op.create_index(
        "ix_production_completion_material_usages_reservation",
        "production_completion_material_usages",
        ["reservation_id", "status"],
    )
    op.create_index(
        "ix_production_completion_material_usages_lot",
        "production_completion_material_usages",
        ["inventory_lot_id", "status"],
    )
    _create_immutable_guards(op.get_bind())


def downgrade() -> None:
    connection = op.get_bind()
    existing_fact = connection.execute(
        sa.text(
            "SELECT 1 FROM production_completion_material_usages LIMIT 1"
        )
    ).first()
    if existing_fact is not None:
        raise RuntimeError(
            "存在生产完工逐批用料事实，禁止破坏性降级；请恢复迁移前数据库备份"
        )
    _drop_immutable_guards(connection)
    op.drop_index(
        "ix_production_completion_material_usages_lot",
        table_name="production_completion_material_usages",
    )
    op.drop_index(
        "ix_production_completion_material_usages_reservation",
        table_name="production_completion_material_usages",
    )
    op.drop_index(
        "ix_production_completion_material_usages_task",
        table_name="production_completion_material_usages",
    )
    op.drop_table("production_completion_material_usages")
