"""freeze component cutting mode in order BOM snapshots

Revision ID: co71v8x9z60
Revises: ci65v8x9z54
Create Date: 2026-07-24
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "co71v8x9z60"
down_revision: Union[str, Sequence[str], None] = "ci65v8x9z54"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "sales_order_item_bom_components"
SOURCE_TABLE = "requisition_item_bom_sources"
UPDATE_TRIGGER = "trg_sales_order_item_bom_components_immutable_update"
SOURCE_TRIGGER = "trg_sales_order_item_bom_components_source_unlink_only"
IMMUTABLE_FUNCTION = "n034_immutable_sales_order_item_bom_component"
SOURCE_RULE_TRIGGER = "trg_requisition_item_bom_sources_rule_immutable"
SOURCE_RULE_FUNCTION = "n039_immutable_requisition_item_bom_source_rule"
CUTTING_MODES = ("一开一", "一开二", "一开三", "一开四", "一开五")


def _drop_snapshot_guard() -> None:
    connection = op.get_bind()
    if connection.dialect.name == "sqlite":
        op.execute(f"DROP TRIGGER IF EXISTS {SOURCE_TRIGGER}")
        op.execute(f"DROP TRIGGER IF EXISTS {UPDATE_TRIGGER}")
    elif connection.dialect.name == "postgresql":
        op.execute(f"DROP TRIGGER IF EXISTS {UPDATE_TRIGGER} ON {TABLE}")
        op.execute(f"DROP FUNCTION IF EXISTS {IMMUTABLE_FUNCTION}()")


def _create_snapshot_guard() -> None:
    connection = op.get_bind()
    if connection.dialect.name == "sqlite":
        fact_columns = [
            column["name"]
            for column in sa.inspect(connection).get_columns(TABLE)
            if column["name"] != "product_bom_component_id"
        ]
        update_columns = ", ".join(f'"{column}"' for column in fact_columns)
        op.execute(
            f"""
            CREATE TRIGGER {UPDATE_TRIGGER}
            BEFORE UPDATE OF {update_columns} ON {TABLE}
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
            CREATE TRIGGER {SOURCE_TRIGGER}
            BEFORE UPDATE OF product_bom_component_id ON {TABLE}
            FOR EACH ROW
            WHEN NOT (
                OLD.product_bom_component_id IS NOT NULL
                AND NEW.product_bom_component_id IS NULL
            )
            BEGIN
                SELECT RAISE(ABORT, 'snapshot source may only be unlinked');
            END
            """
        )
    elif connection.dialect.name == "postgresql":
        op.execute(
            f"""
            CREATE OR REPLACE FUNCTION {IMMUTABLE_FUNCTION}()
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
            CREATE TRIGGER {UPDATE_TRIGGER}
            BEFORE UPDATE ON {TABLE}
            FOR EACH ROW EXECUTE FUNCTION {IMMUTABLE_FUNCTION}()
            """
        )


def _drop_source_rule_guard() -> None:
    connection = op.get_bind()
    if connection.dialect.name == "sqlite":
        op.execute(f"DROP TRIGGER IF EXISTS {SOURCE_RULE_TRIGGER}")
    elif connection.dialect.name == "postgresql":
        op.execute(
            f"DROP TRIGGER IF EXISTS {SOURCE_RULE_TRIGGER} ON {SOURCE_TABLE}"
        )
        op.execute(f"DROP FUNCTION IF EXISTS {SOURCE_RULE_FUNCTION}()")


def _create_source_rule_guard(*, include_demand_basis: bool = True) -> None:
    connection = op.get_bind()
    if connection.dialect.name == "sqlite":
        protected_columns = (
            "calculation_rule_version, demand_basis"
            if include_demand_basis
            else "calculation_rule_version"
        )
        op.execute(
            f"""
            CREATE TRIGGER {SOURCE_RULE_TRIGGER}
            BEFORE UPDATE OF {protected_columns} ON {SOURCE_TABLE}
            FOR EACH ROW
            BEGIN
                SELECT RAISE(
                    ABORT,
                    'requisition_item_bom_sources calculation basis is immutable'
                );
            END
            """
        )
    elif connection.dialect.name == "postgresql":
        demand_basis_guard = (
            "\n                   OR NEW.demand_basis IS DISTINCT FROM OLD.demand_basis"
            if include_demand_basis
            else ""
        )
        op.execute(
            f"""
            CREATE OR REPLACE FUNCTION {SOURCE_RULE_FUNCTION}()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF NEW.calculation_rule_version IS DISTINCT FROM OLD.calculation_rule_version
                   {demand_basis_guard}
                THEN
                    RAISE EXCEPTION
                        'requisition_item_bom_sources calculation basis is immutable';
                END IF;
                RETURN NEW;
            END;
            $$
            """
        )
        op.execute(
            f"""
            CREATE TRIGGER {SOURCE_RULE_TRIGGER}
            BEFORE UPDATE ON {SOURCE_TABLE}
            FOR EACH ROW EXECUTE FUNCTION {SOURCE_RULE_FUNCTION}()
            """
        )


def upgrade() -> None:
    _drop_snapshot_guard()
    with op.batch_alter_table(TABLE) as batch:
        batch.add_column(
            sa.Column(
                "snapshot_component_default_cutting_mode",
                sa.String(length=20),
                server_default="一开一",
                nullable=False,
            )
        )
        batch.create_check_constraint(
            "ck_sales_order_item_bom_components_default_cutting_mode",
            "snapshot_component_default_cutting_mode IN "
            "('一开一','一开二','一开三','一开四','一开五')",
        )
    with op.batch_alter_table(TABLE) as batch:
        batch.alter_column(
            "snapshot_component_default_cutting_mode",
            existing_type=sa.String(length=20),
            server_default=None,
        )
    _create_snapshot_guard()

    _drop_source_rule_guard()
    with op.batch_alter_table(SOURCE_TABLE) as batch:
        batch.drop_constraint(
            "ck_requisition_item_bom_sources_required_piece_formula",
            type_="check",
        )
        batch.add_column(
            sa.Column(
                "demand_basis",
                sa.String(length=30),
                server_default="order_sets",
                nullable=False,
            )
        )
        batch.create_check_constraint(
            "ck_requisition_item_bom_sources_demand_basis",
            "demand_basis IN ('order_sets','order_specific_pieces')",
        )
        batch.create_check_constraint(
            "ck_requisition_item_bom_sources_required_piece_formula",
            "((demand_basis = 'order_sets' "
            "AND required_piece_quantity = order_set_quantity * quantity_per_set) "
            "OR demand_basis = 'order_specific_pieces')",
        )
    with op.batch_alter_table(SOURCE_TABLE) as batch:
        batch.alter_column(
            "demand_basis",
            existing_type=sa.String(length=30),
            server_default=None,
        )
    _create_source_rule_guard()


def downgrade() -> None:
    connection = op.get_bind()
    new_fact_count = int(
        connection.execute(
            sa.text(
                f"SELECT (SELECT COUNT(*) FROM {TABLE} "
                "WHERE snapshot_schema_version >= 3 "
                "OR snapshot_component_default_cutting_mode <> '一开一') "
                f"+ (SELECT COUNT(*) FROM {SOURCE_TABLE} "
                "WHERE demand_basis <> 'order_sets')"
            )
        ).scalar_one()
    )
    if new_fact_count:
        raise RuntimeError(
            "已有订单组件开料方式快照，禁止破坏性降级；请恢复升级前完整备份。"
        )
    _drop_source_rule_guard()
    with op.batch_alter_table(SOURCE_TABLE) as batch:
        batch.drop_constraint(
            "ck_requisition_item_bom_sources_required_piece_formula",
            type_="check",
        )
        batch.drop_constraint(
            "ck_requisition_item_bom_sources_demand_basis",
            type_="check",
        )
        batch.drop_column("demand_basis")
        batch.create_check_constraint(
            "ck_requisition_item_bom_sources_required_piece_formula",
            "required_piece_quantity = order_set_quantity * quantity_per_set",
        )
    _create_source_rule_guard(include_demand_basis=False)
    _drop_snapshot_guard()
    with op.batch_alter_table(TABLE) as batch:
        batch.drop_constraint(
            "ck_sales_order_item_bom_components_default_cutting_mode",
            type_="check",
        )
        batch.drop_column("snapshot_component_default_cutting_mode")
    _create_snapshot_guard()
