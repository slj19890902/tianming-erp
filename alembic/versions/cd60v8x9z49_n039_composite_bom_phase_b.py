"""add composite BOM phase B demand, component workflow, and direct-kit facts

Revision ID: cd60v8x9z49
Revises: cc59v8x9z48
Create Date: 2026-07-19
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "cd60v8x9z49"
down_revision: Union[str, Sequence[str], None] = "cc59v8x9z48"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


PRODUCT_BOM_TABLE = "product_bom_components"
ORDER_BOM_SNAPSHOT_TABLE = "sales_order_item_bom_components"
REQUISITION_SOURCE_TABLE = "requisition_item_bom_sources"
ADJUSTMENT_TABLE = "sales_order_item_bom_demand_adjustments"
DIRECT_DELIVERY_ALLOCATION_TABLE = "bom_component_direct_delivery_allocations"

SNAPSHOT_UPDATE_TRIGGER = "trg_sales_order_item_bom_components_immutable_update"
SNAPSHOT_SOURCE_TRIGGER = "trg_sales_order_item_bom_components_source_unlink_only"
SNAPSHOT_IMMUTABLE_FUNCTION = "n034_immutable_sales_order_item_bom_component"
SOURCE_RULE_TRIGGER = "trg_requisition_item_bom_sources_rule_immutable"
SOURCE_RULE_FUNCTION = "n039_immutable_requisition_item_bom_source_rule"
ADJUSTMENT_UPDATE_TRIGGER = "trg_sales_order_item_bom_demand_adjustments_immutable_update"
ADJUSTMENT_IMMUTABLE_FUNCTION = "n039_immutable_bom_demand_adjustment"
DOWNGRADE_BLOCKED_MESSAGE = (
    "N039 复合 BOM Phase B 事实已产生，禁止破坏性降级；"
    "请停止服务并恢复 cc59 升级前的完整数据库备份。"
)


def _create_snapshot_immutability_guard() -> None:
    """Restore the N034 snapshot guard after SQLite table recreation.

    Inspecting the columns deliberately makes every later factual snapshot column,
    including Phase B version fields, immutable without maintaining an allow-list.
    """

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
    connection = op.get_bind()
    if connection.dialect.name == "sqlite":
        op.execute(f"DROP TRIGGER IF EXISTS {SNAPSHOT_SOURCE_TRIGGER}")
        op.execute(f"DROP TRIGGER IF EXISTS {SNAPSHOT_UPDATE_TRIGGER}")
    elif connection.dialect.name == "postgresql":
        op.execute(
            f"DROP TRIGGER IF EXISTS {SNAPSHOT_UPDATE_TRIGGER} "
            f"ON {ORDER_BOM_SNAPSHOT_TABLE}"
        )
        op.execute(f"DROP FUNCTION IF EXISTS {SNAPSHOT_IMMUTABLE_FUNCTION}()")


def _create_source_rule_immutability_guard() -> None:
    connection = op.get_bind()
    if connection.dialect.name == "sqlite":
        op.execute(
            f"""
            CREATE TRIGGER {SOURCE_RULE_TRIGGER}
            BEFORE UPDATE OF calculation_rule_version ON {REQUISITION_SOURCE_TABLE}
            FOR EACH ROW
            BEGIN
                SELECT RAISE(
                    ABORT,
                    'requisition_item_bom_sources calculation rule is immutable'
                );
            END
            """
        )
    elif connection.dialect.name == "postgresql":
        op.execute(
            f"""
            CREATE FUNCTION {SOURCE_RULE_FUNCTION}()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF NEW.calculation_rule_version IS DISTINCT FROM OLD.calculation_rule_version THEN
                    RAISE EXCEPTION 'requisition_item_bom_sources calculation rule is immutable';
                END IF;
                RETURN NEW;
            END;
            $$
            """
        )
        op.execute(
            f"""
            CREATE TRIGGER {SOURCE_RULE_TRIGGER}
            BEFORE UPDATE ON {REQUISITION_SOURCE_TABLE}
            FOR EACH ROW EXECUTE FUNCTION {SOURCE_RULE_FUNCTION}()
            """
        )


def _drop_source_rule_immutability_guard() -> None:
    connection = op.get_bind()
    if connection.dialect.name == "sqlite":
        op.execute(f"DROP TRIGGER IF EXISTS {SOURCE_RULE_TRIGGER}")
    elif connection.dialect.name == "postgresql":
        op.execute(f"DROP TRIGGER IF EXISTS {SOURCE_RULE_TRIGGER} ON {REQUISITION_SOURCE_TABLE}")
        op.execute(f"DROP FUNCTION IF EXISTS {SOURCE_RULE_FUNCTION}()")


def _create_adjustment_immutability_guard() -> None:
    connection = op.get_bind()
    if connection.dialect.name == "sqlite":
        op.execute(
            f"""
            CREATE TRIGGER {ADJUSTMENT_UPDATE_TRIGGER}
            BEFORE UPDATE ON {ADJUSTMENT_TABLE}
            FOR EACH ROW
            BEGIN
                SELECT RAISE(
                    ABORT,
                    'sales_order_item_bom_demand_adjustments are immutable'
                );
            END
            """
        )
    elif connection.dialect.name == "postgresql":
        op.execute(
            f"""
            CREATE FUNCTION {ADJUSTMENT_IMMUTABLE_FUNCTION}()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                RAISE EXCEPTION 'sales_order_item_bom_demand_adjustments are immutable';
            END;
            $$
            """
        )
        op.execute(
            f"""
            CREATE TRIGGER {ADJUSTMENT_UPDATE_TRIGGER}
            BEFORE UPDATE ON {ADJUSTMENT_TABLE}
            FOR EACH ROW EXECUTE FUNCTION {ADJUSTMENT_IMMUTABLE_FUNCTION}()
            """
        )


def _drop_adjustment_immutability_guard() -> None:
    connection = op.get_bind()
    if connection.dialect.name == "sqlite":
        op.execute(f"DROP TRIGGER IF EXISTS {ADJUSTMENT_UPDATE_TRIGGER}")
    elif connection.dialect.name == "postgresql":
        op.execute(f"DROP TRIGGER IF EXISTS {ADJUSTMENT_UPDATE_TRIGGER} ON {ADJUSTMENT_TABLE}")
        op.execute(f"DROP FUNCTION IF EXISTS {ADJUSTMENT_IMMUTABLE_FUNCTION}()")


def _assert_integral_quantity_per_set(connection: sa.Connection) -> None:
    for table_name in (
        PRODUCT_BOM_TABLE,
        ORDER_BOM_SNAPSHOT_TABLE,
        REQUISITION_SOURCE_TABLE,
    ):
        invalid_count = connection.execute(
            sa.text(
                f"SELECT COUNT(*) FROM {table_name} "
                "WHERE quantity_per_set <= 0 "
                "OR quantity_per_set <> round(quantity_per_set, 0)"
            )
        ).scalar_one()
        if int(invalid_count or 0) > 0:
            raise RuntimeError(
                f"N039 requires positive integral quantity_per_set; {table_name} contains incompatible facts"
            )


def _assert_safe_downgrade(connection: sa.Connection) -> None:
    fact_count = connection.execute(
        sa.text(
            f"""
            SELECT
                (SELECT COUNT(*) FROM {ADJUSTMENT_TABLE})
              + (SELECT COUNT(*) FROM {DIRECT_DELIVERY_ALLOCATION_TABLE})
              + (SELECT COUNT(*) FROM production_tasks
                   WHERE sales_order_item_bom_component_id IS NOT NULL)
               + (SELECT COUNT(*) FROM inventory_reservations
                    WHERE sales_order_item_bom_component_id IS NOT NULL)
              + (SELECT COUNT(*) FROM order_item_semi_requirements
                   WHERE sales_order_item_bom_component_id IS NOT NULL)
               + (SELECT COUNT(*) FROM production_completions completion
                   JOIN production_tasks task ON task.id = completion.task_id
                   WHERE task.sales_order_item_bom_component_id IS NOT NULL)
              + (SELECT COUNT(*) FROM {REQUISITION_SOURCE_TABLE}
                   WHERE calculation_rule_version <> 'legacy-n034')
              + (SELECT COUNT(*) FROM {ORDER_BOM_SNAPSHOT_TABLE}
                   WHERE parent_product_version IS NOT NULL
                      OR component_product_version IS NOT NULL
                      OR snapshot_schema_version <> 1)
            """
        )
    ).scalar_one()
    if int(fact_count or 0) > 0:
        raise RuntimeError(DOWNGRADE_BLOCKED_MESSAGE)


def upgrade() -> None:
    connection = op.get_bind()
    _assert_integral_quantity_per_set(connection)

    with op.batch_alter_table(PRODUCT_BOM_TABLE) as batch_op:
        batch_op.drop_constraint(
            "ck_product_bom_components_quantity_per_set", type_="check"
        )
        batch_op.create_check_constraint(
            "ck_product_bom_components_quantity_per_set",
            "quantity_per_set > 0 AND quantity_per_set = round(quantity_per_set, 0)",
        )

    _drop_snapshot_immutability_guard()
    with op.batch_alter_table(ORDER_BOM_SNAPSHOT_TABLE) as batch_op:
        batch_op.drop_constraint(
            "ck_sales_order_item_bom_components_quantity_per_set", type_="check"
        )
        batch_op.add_column(sa.Column("parent_product_version", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("component_product_version", sa.Integer(), nullable=True))
        batch_op.add_column(
            sa.Column(
                "snapshot_schema_version",
                sa.Integer(),
                nullable=False,
                server_default="1",
            )
        )
        batch_op.create_check_constraint(
            "ck_sales_order_item_bom_components_quantity_per_set",
            "quantity_per_set > 0 AND quantity_per_set = round(quantity_per_set, 0)",
        )
        batch_op.create_check_constraint(
            "ck_sales_order_item_bom_components_snapshot_schema_version",
            "snapshot_schema_version >= 1",
        )
    with op.batch_alter_table(ORDER_BOM_SNAPSHOT_TABLE) as batch_op:
        batch_op.alter_column(
            "snapshot_schema_version",
            existing_type=sa.Integer(),
            server_default=None,
        )
    _create_snapshot_immutability_guard()

    with op.batch_alter_table(REQUISITION_SOURCE_TABLE) as batch_op:
        batch_op.drop_constraint(
            "ck_requisition_item_bom_sources_quantity_per_set", type_="check"
        )
        batch_op.add_column(
            sa.Column(
                "calculation_rule_version",
                sa.String(length=80),
                nullable=False,
                server_default="legacy-n034",
            )
        )
        batch_op.create_check_constraint(
            "ck_requisition_item_bom_sources_quantity_per_set",
            "quantity_per_set > 0 AND quantity_per_set = round(quantity_per_set, 0)",
        )
    with op.batch_alter_table(REQUISITION_SOURCE_TABLE) as batch_op:
        batch_op.alter_column(
            "calculation_rule_version",
            existing_type=sa.String(length=80),
            server_default=None,
        )
    _create_source_rule_immutability_guard()

    op.create_table(
        ADJUSTMENT_TABLE,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("sales_order_item_bom_component_id", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=60), nullable=False),
        sa.Column("delta_order_set_quantity", sa.Integer(), nullable=False),
        sa.Column("delta_required_piece_quantity", sa.Numeric(14, 4), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.Integer(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=120), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.ForeignKeyConstraint(
            ["sales_order_item_bom_component_id"],
            [f"{ORDER_BOM_SNAPSHOT_TABLE}.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"], ondelete="SET NULL"),
        sa.CheckConstraint(
            "length(trim(event_type)) > 0",
            name="ck_sales_order_item_bom_demand_adjustments_event_type",
        ),
        sa.CheckConstraint(
            "delta_order_set_quantity <> 0 OR delta_required_piece_quantity <> 0",
            name="ck_sales_order_item_bom_demand_adjustments_not_noop",
        ),
        sa.CheckConstraint(
            "length(trim(reason)) > 0",
            name="ck_sales_order_item_bom_demand_adjustments_reason",
        ),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_sales_order_item_bom_demand_adjustments_idempotency",
        ),
    )
    op.create_index(
        "ix_sales_order_item_bom_demand_adjustments_snapshot_created",
        ADJUSTMENT_TABLE,
        ["sales_order_item_bom_component_id", "created_at"],
    )
    _create_adjustment_immutability_guard()

    with op.batch_alter_table("inventory_reservations") as batch_op:
        batch_op.add_column(
            sa.Column("sales_order_item_bom_component_id", sa.Integer(), nullable=True)
        )
        batch_op.create_foreign_key(
            "fk_inventory_reservations_bom_component",
            ORDER_BOM_SNAPSHOT_TABLE,
            ["sales_order_item_bom_component_id"],
            ["id"],
            ondelete="SET NULL",
        )
    op.create_index(
        "ix_inventory_reservations_bom_component",
        "inventory_reservations",
        ["sales_order_item_bom_component_id"],
    )

    with op.batch_alter_table("order_item_semi_requirements") as batch_op:
        batch_op.add_column(
            sa.Column("sales_order_item_bom_component_id", sa.Integer(), nullable=True)
        )
        batch_op.create_foreign_key(
            "fk_order_item_semi_requirements_bom_component",
            ORDER_BOM_SNAPSHOT_TABLE,
            ["sales_order_item_bom_component_id"],
            ["id"],
            ondelete="SET NULL",
        )
    op.create_index(
        "ix_order_item_semi_requirements_bom_component",
        "order_item_semi_requirements",
        ["sales_order_item_bom_component_id"],
    )

    with op.batch_alter_table("production_tasks") as batch_op:
        batch_op.drop_constraint("uq_production_tasks_order_item", type_="unique")
        batch_op.add_column(
            sa.Column("sales_order_item_bom_component_id", sa.Integer(), nullable=True)
        )
        batch_op.create_foreign_key(
            "fk_production_tasks_bom_component",
            ORDER_BOM_SNAPSHOT_TABLE,
            ["sales_order_item_bom_component_id"],
            ["id"],
            ondelete="SET NULL",
        )
    op.create_index(
        "uq_production_tasks_regular_order_item",
        "production_tasks",
        ["order_item_id"],
        unique=True,
        sqlite_where=sa.text("sales_order_item_bom_component_id IS NULL"),
        postgresql_where=sa.text("sales_order_item_bom_component_id IS NULL"),
    )
    op.create_index(
        "uq_production_tasks_bom_component",
        "production_tasks",
        ["sales_order_item_bom_component_id"],
        unique=True,
        sqlite_where=sa.text("sales_order_item_bom_component_id IS NOT NULL"),
        postgresql_where=sa.text("sales_order_item_bom_component_id IS NOT NULL"),
    )

    duplicate_task_completion_count = connection.execute(
        sa.text(
            "SELECT COUNT(*) FROM ("
            "SELECT task_id FROM production_completions "
            "GROUP BY task_id HAVING COUNT(*) > 1"
            ") AS duplicate_task_completions"
        )
    ).scalar_one()
    if int(duplicate_task_completion_count or 0) > 0:
        raise RuntimeError(
            "N039 cannot replace production completion order-item uniqueness: "
            "legacy duplicate task completions exist"
        )
    with op.batch_alter_table("production_completions") as batch_op:
        batch_op.drop_constraint(
            "uq_production_completions_order_item", type_="unique"
        )
    op.create_index(
        "uq_production_completions_task",
        "production_completions",
        ["task_id"],
        unique=True,
    )

    op.create_table(
        DIRECT_DELIVERY_ALLOCATION_TABLE,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("delivery_item_id", sa.Integer(), nullable=False),
        sa.Column("production_completion_id", sa.Integer(), nullable=False),
        sa.Column("sales_order_item_bom_component_id", sa.Integer(), nullable=False),
        sa.Column("consumed_quantity", sa.Integer(), nullable=False),
        sa.Column(
            "reversed_quantity", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "status", sa.String(length=20), nullable=False, server_default="active"
        ),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("reversed_by", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.Column("reversed_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["delivery_item_id"], ["sales_delivery_items.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["production_completion_id"],
            ["production_completions.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["sales_order_item_bom_component_id"],
            [f"{ORDER_BOM_SNAPSHOT_TABLE}.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["reversed_by"], ["users.id"], ondelete="SET NULL"),
        sa.CheckConstraint(
            "consumed_quantity > 0",
            name="ck_bom_component_direct_delivery_allocations_consumed_quantity",
        ),
        sa.CheckConstraint(
            "reversed_quantity >= 0 AND reversed_quantity <= consumed_quantity",
            name="ck_bom_component_direct_delivery_allocations_reversed_quantity",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'partial', 'reversed')",
            name="ck_bom_component_direct_delivery_allocations_status",
        ),
        sa.CheckConstraint(
            "((status = 'active' AND reversed_quantity = 0) OR "
            "(status = 'partial' AND reversed_quantity > 0 "
            "AND reversed_quantity < consumed_quantity) OR "
            "(status = 'reversed' AND reversed_quantity = consumed_quantity))",
            name="ck_bom_component_direct_delivery_allocations_status_quantity",
        ),
        sa.UniqueConstraint(
            "delivery_item_id",
            "production_completion_id",
            name="uq_bom_component_direct_delivery_allocations_delivery_completion",
        ),
    )
    op.create_index(
        "ix_bom_component_direct_delivery_allocations_snapshot_status",
        DIRECT_DELIVERY_ALLOCATION_TABLE,
        ["sales_order_item_bom_component_id", "status"],
    )
    op.create_index(
        "ix_bom_component_direct_delivery_allocations_completion_status",
        DIRECT_DELIVERY_ALLOCATION_TABLE,
        ["production_completion_id", "status"],
    )


def downgrade() -> None:
    connection = op.get_bind()
    _assert_safe_downgrade(connection)

    op.drop_index(
        "ix_bom_component_direct_delivery_allocations_completion_status",
        table_name=DIRECT_DELIVERY_ALLOCATION_TABLE,
    )
    op.drop_index(
        "ix_bom_component_direct_delivery_allocations_snapshot_status",
        table_name=DIRECT_DELIVERY_ALLOCATION_TABLE,
    )
    op.drop_table(DIRECT_DELIVERY_ALLOCATION_TABLE)

    op.drop_index("uq_production_completions_task", table_name="production_completions")
    with op.batch_alter_table("production_completions") as batch_op:
        batch_op.create_unique_constraint(
            "uq_production_completions_order_item", ["order_item_id"]
        )

    op.drop_index("uq_production_tasks_bom_component", table_name="production_tasks")
    op.drop_index(
        "uq_production_tasks_regular_order_item", table_name="production_tasks"
    )
    with op.batch_alter_table("production_tasks") as batch_op:
        batch_op.drop_constraint("fk_production_tasks_bom_component", type_="foreignkey")
        batch_op.drop_column("sales_order_item_bom_component_id")
        batch_op.create_unique_constraint(
            "uq_production_tasks_order_item", ["order_item_id"]
        )

    op.drop_index(
        "ix_order_item_semi_requirements_bom_component",
        table_name="order_item_semi_requirements",
    )
    with op.batch_alter_table("order_item_semi_requirements") as batch_op:
        batch_op.drop_constraint(
            "fk_order_item_semi_requirements_bom_component", type_="foreignkey"
        )
        batch_op.drop_column("sales_order_item_bom_component_id")

    op.drop_index(
        "ix_inventory_reservations_bom_component",
        table_name="inventory_reservations",
    )
    with op.batch_alter_table("inventory_reservations") as batch_op:
        batch_op.drop_constraint(
            "fk_inventory_reservations_bom_component", type_="foreignkey"
        )
        batch_op.drop_column("sales_order_item_bom_component_id")

    _drop_adjustment_immutability_guard()
    op.drop_index(
        "ix_sales_order_item_bom_demand_adjustments_snapshot_created",
        table_name=ADJUSTMENT_TABLE,
    )
    op.drop_table(ADJUSTMENT_TABLE)

    _drop_source_rule_immutability_guard()
    with op.batch_alter_table(REQUISITION_SOURCE_TABLE) as batch_op:
        batch_op.drop_constraint(
            "ck_requisition_item_bom_sources_quantity_per_set", type_="check"
        )
        batch_op.drop_column("calculation_rule_version")
        batch_op.create_check_constraint(
            "ck_requisition_item_bom_sources_quantity_per_set", "quantity_per_set > 0"
        )

    _drop_snapshot_immutability_guard()
    with op.batch_alter_table(ORDER_BOM_SNAPSHOT_TABLE) as batch_op:
        batch_op.drop_constraint(
            "ck_sales_order_item_bom_components_snapshot_schema_version", type_="check"
        )
        batch_op.drop_constraint(
            "ck_sales_order_item_bom_components_quantity_per_set", type_="check"
        )
        batch_op.drop_column("snapshot_schema_version")
        batch_op.drop_column("component_product_version")
        batch_op.drop_column("parent_product_version")
        batch_op.create_check_constraint(
            "ck_sales_order_item_bom_components_quantity_per_set", "quantity_per_set > 0"
        )
    _create_snapshot_immutability_guard()

    with op.batch_alter_table(PRODUCT_BOM_TABLE) as batch_op:
        batch_op.drop_constraint(
            "ck_product_bom_components_quantity_per_set", type_="check"
        )
        batch_op.create_check_constraint(
            "ck_product_bom_components_quantity_per_set", "quantity_per_set > 0"
        )
