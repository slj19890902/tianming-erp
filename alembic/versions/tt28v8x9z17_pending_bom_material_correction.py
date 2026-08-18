"""allow guarded pending BOM component material correction

Revision ID: tt28v8x9z17
Revises: ss27v8x9z16
Create Date: 2026-08-18
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "tt28v8x9z17"
down_revision = "ss27v8x9z16"
branch_labels = None
depends_on = None


SNAPSHOT_TABLE = "sales_order_item_bom_components"
SNAPSHOT_UPDATE_TRIGGER = "trg_sales_order_item_bom_components_immutable_update"
SNAPSHOT_SOURCE_TRIGGER = (
    "trg_sales_order_item_bom_components_source_unlink_only"
)
MATERIAL_GUARD_TRIGGER = (
    "trg_sales_order_item_bom_components_material_correction_guard"
)
SNAPSHOT_FUNCTION = "n034_immutable_sales_order_item_bom_component"
MUTABLE_MATERIAL_COLUMNS = (
    "snapshot_component_material",
    "snapshot_component_material_id",
    "snapshot_component_supplier_name",
    "snapshot_component_layer_count",
    "snapshot_component_flute_type",
    "component_product_version",
)
INACTIVE_REQUISITION_STATUSES = (
    "cancelled",
    "canceled",
    "voided",
    "withdrawn",
    "invalid",
    "已取消",
    "已作废",
    "已撤回",
)


def _drop_snapshot_guards() -> None:
    connection = op.get_bind()
    if connection.dialect.name == "sqlite":
        op.execute(f"DROP TRIGGER IF EXISTS {MATERIAL_GUARD_TRIGGER}")
        op.execute(f"DROP TRIGGER IF EXISTS {SNAPSHOT_SOURCE_TRIGGER}")
        op.execute(f"DROP TRIGGER IF EXISTS {SNAPSHOT_UPDATE_TRIGGER}")
    elif connection.dialect.name == "postgresql":
        op.execute(
            f"DROP TRIGGER IF EXISTS {SNAPSHOT_UPDATE_TRIGGER} "
            f"ON {SNAPSHOT_TABLE}"
        )
        op.execute(f"DROP FUNCTION IF EXISTS {SNAPSHOT_FUNCTION}()")


def _sqlite_active_fact_predicate() -> str:
    inactive = ", ".join(f"'{value}'" for value in INACTIVE_REQUISITION_STATUSES)
    return f"""
        EXISTS (
            SELECT 1
            FROM requisition_item_bom_sources AS source
            JOIN material_requisition_items AS requisition_item
              ON requisition_item.id = source.requisition_item_id
            WHERE source.sales_order_item_bom_component_id = OLD.id
              AND lower(coalesce(requisition_item.status, '')) NOT IN ({inactive})
        )
        OR EXISTS (
            SELECT 1
            FROM requisition_item_bom_sources AS source
            JOIN incoming_receipt_items AS receipt_item
              ON receipt_item.requisition_item_id = source.requisition_item_id
            WHERE source.sales_order_item_bom_component_id = OLD.id
              AND lower(coalesce(receipt_item.status, '')) = 'posted'
        )
        OR EXISTS (
            SELECT 1
            FROM inventory_reservations AS reservation
            WHERE reservation.sales_order_item_bom_component_id = OLD.id
              AND lower(coalesce(reservation.status, '')) <> 'cancelled'
              AND coalesce(reservation.reserved_stock_quantity, 0)
                  > coalesce(reservation.consumed_stock_quantity, 0)
                    + coalesce(reservation.released_stock_quantity, 0)
        )
        OR EXISTS (
            SELECT 1
            FROM production_tasks AS task
            JOIN production_completions AS completion
              ON completion.task_id = task.id
            WHERE task.sales_order_item_bom_component_id = OLD.id
        )
    """


def _create_sqlite_guard(*, allow_pending_material_correction: bool) -> None:
    connection = op.get_bind()
    excluded = {"product_bom_component_id"}
    if allow_pending_material_correction:
        excluded.update(MUTABLE_MATERIAL_COLUMNS)
    fact_columns = [
        column["name"]
        for column in sa.inspect(connection).get_columns(SNAPSHOT_TABLE)
        if column["name"] not in excluded
    ]
    update_columns = ", ".join(f'"{column}"' for column in fact_columns)
    op.execute(
        f"""
        CREATE TRIGGER {SNAPSHOT_UPDATE_TRIGGER}
        BEFORE UPDATE OF {update_columns} ON {SNAPSHOT_TABLE}
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
        BEFORE UPDATE OF product_bom_component_id ON {SNAPSHOT_TABLE}
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
    if allow_pending_material_correction:
        mutable_columns = ", ".join(
            f'"{column}"' for column in MUTABLE_MATERIAL_COLUMNS
        )
        op.execute(
            f"""
            CREATE TRIGGER {MATERIAL_GUARD_TRIGGER}
            BEFORE UPDATE OF {mutable_columns} ON {SNAPSHOT_TABLE}
            FOR EACH ROW
            WHEN NOT (
                NEW.product_bom_component_id IS OLD.product_bom_component_id
            ) OR ({_sqlite_active_fact_predicate()})
            BEGIN
                SELECT RAISE(
                    ABORT,
                    'BOM component material can only change before operational facts'
                );
            END
            """
        )


def _create_postgresql_guard(*, allow_pending_material_correction: bool) -> None:
    if not allow_pending_material_correction:
        op.execute(
            f"""
            CREATE OR REPLACE FUNCTION {SNAPSHOT_FUNCTION}()
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
    else:
        excluded = ["product_bom_component_id", *MUTABLE_MATERIAL_COLUMNS]
        excluded_sql = ", ".join(f"'{column}'" for column in excluded)
        inactive = ", ".join(
            f"'{value}'" for value in INACTIVE_REQUISITION_STATUSES
        )
        op.execute(
            f"""
            CREATE OR REPLACE FUNCTION {SNAPSHOT_FUNCTION}()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF OLD.product_bom_component_id IS NOT NULL
                   AND NEW.product_bom_component_id IS NULL
                   AND to_jsonb(NEW) - 'product_bom_component_id'
                       = to_jsonb(OLD) - 'product_bom_component_id'
                THEN
                    RETURN NEW;
                END IF;

                IF NEW.product_bom_component_id IS NOT DISTINCT FROM
                       OLD.product_bom_component_id
                   AND to_jsonb(NEW) - ARRAY[{excluded_sql}]
                       = to_jsonb(OLD) - ARRAY[{excluded_sql}]
                THEN
                    IF EXISTS (
                        SELECT 1
                        FROM requisition_item_bom_sources AS source
                        JOIN material_requisition_items AS requisition_item
                          ON requisition_item.id = source.requisition_item_id
                        WHERE source.sales_order_item_bom_component_id = OLD.id
                          AND lower(coalesce(requisition_item.status, ''))
                              NOT IN ({inactive})
                    ) OR EXISTS (
                        SELECT 1
                        FROM requisition_item_bom_sources AS source
                        JOIN incoming_receipt_items AS receipt_item
                          ON receipt_item.requisition_item_id = source.requisition_item_id
                        WHERE source.sales_order_item_bom_component_id = OLD.id
                          AND lower(coalesce(receipt_item.status, '')) = 'posted'
                    ) OR EXISTS (
                        SELECT 1
                        FROM inventory_reservations AS reservation
                        WHERE reservation.sales_order_item_bom_component_id = OLD.id
                          AND lower(coalesce(reservation.status, '')) <> 'cancelled'
                          AND coalesce(reservation.reserved_stock_quantity, 0)
                              > coalesce(reservation.consumed_stock_quantity, 0)
                                + coalesce(reservation.released_stock_quantity, 0)
                    ) OR EXISTS (
                        SELECT 1
                        FROM production_tasks AS task
                        JOIN production_completions AS completion
                          ON completion.task_id = task.id
                        WHERE task.sales_order_item_bom_component_id = OLD.id
                    ) THEN
                        RAISE EXCEPTION
                            'BOM component material can only change before operational facts';
                    END IF;
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
        BEFORE UPDATE ON {SNAPSHOT_TABLE}
        FOR EACH ROW EXECUTE FUNCTION {SNAPSHOT_FUNCTION}()
        """
    )


def _create_snapshot_guard(*, allow_pending_material_correction: bool) -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        _create_sqlite_guard(
            allow_pending_material_correction=allow_pending_material_correction
        )
    elif dialect == "postgresql":
        _create_postgresql_guard(
            allow_pending_material_correction=allow_pending_material_correction
        )


def upgrade() -> None:
    _drop_snapshot_guards()
    _create_snapshot_guard(allow_pending_material_correction=True)


def downgrade() -> None:
    _drop_snapshot_guards()
    _create_snapshot_guard(allow_pending_material_correction=False)
