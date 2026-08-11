"""add virtual composite parent and delivery visibility snapshots

Revision ID: ee13v8x9z02
Revises: ed12v8x9z01
Create Date: 2026-08-11
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "ee13v8x9z02"
down_revision = "ed12v8x9z01"
branch_labels = None
depends_on = None

SNAPSHOT_TABLE = "sales_order_item_bom_components"
SNAPSHOT_UPDATE_TRIGGER = "trg_sales_order_item_bom_components_immutable_update"
SNAPSHOT_SOURCE_TRIGGER = "trg_sales_order_item_bom_components_source_unlink_only"
SNAPSHOT_FUNCTION = "n034_immutable_sales_order_item_bom_component"


def _drop_snapshot_guard() -> None:
    connection = op.get_bind()
    if connection.dialect.name == "sqlite":
        op.execute(f"DROP TRIGGER IF EXISTS {SNAPSHOT_SOURCE_TRIGGER}")
        op.execute(f"DROP TRIGGER IF EXISTS {SNAPSHOT_UPDATE_TRIGGER}")
    elif connection.dialect.name == "postgresql":
        op.execute(
            f"DROP TRIGGER IF EXISTS {SNAPSHOT_UPDATE_TRIGGER} ON {SNAPSHOT_TABLE}"
        )
        op.execute(f"DROP FUNCTION IF EXISTS {SNAPSHOT_FUNCTION}()")


def _create_snapshot_guard() -> None:
    connection = op.get_bind()
    if connection.dialect.name == "sqlite":
        fact_columns = [
            column["name"]
            for column in sa.inspect(connection).get_columns(SNAPSHOT_TABLE)
            if column["name"] != "product_bom_component_id"
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
    elif connection.dialect.name == "postgresql":
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
        op.execute(
            f"""
            CREATE TRIGGER {SNAPSHOT_UPDATE_TRIGGER}
            BEFORE UPDATE ON {SNAPSHOT_TABLE}
            FOR EACH ROW EXECUTE FUNCTION {SNAPSHOT_FUNCTION}()
            """
        )


def upgrade() -> None:
    with op.batch_alter_table("products") as batch:
        batch.add_column(
            sa.Column(
                "is_virtual_composite_parent",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
    with op.batch_alter_table("sales_order_items") as batch:
        batch.add_column(
            sa.Column(
                "is_virtual_composite_parent_snapshot",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
    with op.batch_alter_table("product_bom_components") as batch:
        batch.add_column(
            sa.Column(
                "show_on_delivery",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            )
        )
    _drop_snapshot_guard()
    with op.batch_alter_table(SNAPSHOT_TABLE) as batch:
        batch.add_column(
            sa.Column(
                "show_on_delivery",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            )
        )
    _create_snapshot_guard()


def downgrade() -> None:
    connection = op.get_bind()
    facts = int(
        connection.execute(
            sa.text(
                "SELECT "
                "(SELECT COUNT(*) FROM products WHERE is_virtual_composite_parent = true) + "
                "(SELECT COUNT(*) FROM sales_order_items WHERE is_virtual_composite_parent_snapshot = true) + "
                "(SELECT COUNT(*) FROM product_bom_components WHERE show_on_delivery = false) + "
                "(SELECT COUNT(*) FROM sales_order_item_bom_components WHERE show_on_delivery = false)"
            )
        ).scalar_one()
    )
    if facts:
        raise RuntimeError(
            "P1-43A 已存在虚拟套装或送货显示业务事实，拒绝破坏性降级"
        )

    _drop_snapshot_guard()
    with op.batch_alter_table(SNAPSHOT_TABLE) as batch:
        batch.drop_column("show_on_delivery")
    _create_snapshot_guard()
    with op.batch_alter_table("product_bom_components") as batch:
        batch.drop_column("show_on_delivery")
    with op.batch_alter_table("sales_order_items") as batch:
        batch.drop_column("is_virtual_composite_parent_snapshot")
    with op.batch_alter_table("products") as batch:
        batch.drop_column("is_virtual_composite_parent")
