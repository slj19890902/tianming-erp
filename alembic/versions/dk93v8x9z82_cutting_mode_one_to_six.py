"""extend product and BOM cutting modes through one-to-six

Revision ID: dk93v8x9z82
Revises: dj92v8x9z81
Create Date: 2026-08-05
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "dk93v8x9z82"
down_revision: Union[str, Sequence[str], None] = "dj92v8x9z81"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

PRODUCT_TABLE = "products"
BOM_SNAPSHOT_TABLE = "sales_order_item_bom_components"
SNAPSHOT_UPDATE_TRIGGER = "trg_sales_order_item_bom_components_immutable_update"
SNAPSHOT_SOURCE_TRIGGER = "trg_sales_order_item_bom_components_source_unlink_only"
SNAPSHOT_FUNCTION = "n034_immutable_sales_order_item_bom_component"

OLD_MODES = "('一开一','一开二','一开三','一开四','一开五')"
NEW_MODES = "('一开一','一开二','一开三','一开四','一开五','一开六')"


def _drop_snapshot_guard() -> None:
    connection = op.get_bind()
    if connection.dialect.name == "sqlite":
        op.execute(f"DROP TRIGGER IF EXISTS {SNAPSHOT_SOURCE_TRIGGER}")
        op.execute(f"DROP TRIGGER IF EXISTS {SNAPSHOT_UPDATE_TRIGGER}")
    elif connection.dialect.name == "postgresql":
        op.execute(
            f"DROP TRIGGER IF EXISTS {SNAPSHOT_UPDATE_TRIGGER} "
            f"ON {BOM_SNAPSHOT_TABLE}"
        )
        op.execute(f"DROP FUNCTION IF EXISTS {SNAPSHOT_FUNCTION}()")


def _create_snapshot_guard() -> None:
    connection = op.get_bind()
    if connection.dialect.name == "sqlite":
        fact_columns = [
            column["name"]
            for column in sa.inspect(connection).get_columns(BOM_SNAPSHOT_TABLE)
            if column["name"] != "product_bom_component_id"
        ]
        update_columns = ", ".join(f'"{column}"' for column in fact_columns)
        op.execute(
            f"""
            CREATE TRIGGER {SNAPSHOT_UPDATE_TRIGGER}
            BEFORE UPDATE OF {update_columns} ON {BOM_SNAPSHOT_TABLE}
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
            BEFORE UPDATE OF product_bom_component_id ON {BOM_SNAPSHOT_TABLE}
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
            BEFORE UPDATE ON {BOM_SNAPSHOT_TABLE}
            FOR EACH ROW EXECUTE FUNCTION {SNAPSHOT_FUNCTION}()
            """
        )


def _replace_constraints(*, modes: str) -> None:
    with op.batch_alter_table(PRODUCT_TABLE) as batch:
        batch.drop_constraint("ck_products_default_cutting_mode", type_="check")
        batch.create_check_constraint(
            "ck_products_default_cutting_mode",
            f"default_cutting_mode IN {modes}",
        )

    _drop_snapshot_guard()
    with op.batch_alter_table(BOM_SNAPSHOT_TABLE) as batch:
        batch.drop_constraint(
            "ck_sales_order_item_bom_components_default_cutting_mode",
            type_="check",
        )
        batch.create_check_constraint(
            "ck_sales_order_item_bom_components_default_cutting_mode",
            f"snapshot_component_default_cutting_mode IN {modes}",
        )
    _create_snapshot_guard()


def upgrade() -> None:
    _replace_constraints(modes=NEW_MODES)


def downgrade() -> None:
    connection = op.get_bind()
    one_to_six_count = int(
        connection.execute(
            sa.text(
                f"SELECT (SELECT COUNT(*) FROM {PRODUCT_TABLE} "
                "WHERE default_cutting_mode = '一开六') + "
                f"(SELECT COUNT(*) FROM {BOM_SNAPSHOT_TABLE} "
                "WHERE snapshot_component_default_cutting_mode = '一开六')"
            )
        ).scalar_one()
    )
    if one_to_six_count:
        raise RuntimeError(
            "已有一开六产品或订单快照，禁止破坏性降级；请恢复升级前完整备份。"
        )
    _replace_constraints(modes=OLD_MODES)
