"""allow positive-integer cutting yields without a fixed option list

Revision ID: ed12v8x9z01
Revises: ec11v8x9z00
Create Date: 2026-08-11
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "ed12v8x9z01"
down_revision = "ec11v8x9z00"
branch_labels = None
depends_on = None

PRODUCT_TABLE = "products"
BOM_SNAPSHOT_TABLE = "sales_order_item_bom_components"
SNAPSHOT_UPDATE_TRIGGER = "trg_sales_order_item_bom_components_immutable_update"
SNAPSHOT_SOURCE_TRIGGER = "trg_sales_order_item_bom_components_source_unlink_only"
SNAPSHOT_FUNCTION = "n034_immutable_sales_order_item_bom_component"
LEGACY_MODES = "('一开一','一开二','一开三','一开四','一开五','一开六')"


def _positive_integer_check(column: str) -> str:
    return (
        f"({column} IN {LEGACY_MODES} OR ("
        f"substr({column}, 1, 2) = '一开' AND "
        f"length({column}) BETWEEN 3 AND 20 AND "
        f"CAST(substr({column}, 3) AS BIGINT) > 0 AND "
        f"substr({column}, 3) = "
        f"CAST(CAST(substr({column}, 3) AS BIGINT) AS VARCHAR)))"
    )


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


def _replace_constraints(*, product_check: str, snapshot_check: str) -> None:
    with op.batch_alter_table(PRODUCT_TABLE) as batch:
        batch.drop_constraint("ck_products_default_cutting_mode", type_="check")
        batch.create_check_constraint(
            "ck_products_default_cutting_mode",
            product_check,
        )

    _drop_snapshot_guard()
    with op.batch_alter_table(BOM_SNAPSHOT_TABLE) as batch:
        batch.drop_constraint(
            "ck_sales_order_item_bom_components_default_cutting_mode",
            type_="check",
        )
        batch.create_check_constraint(
            "ck_sales_order_item_bom_components_default_cutting_mode",
            snapshot_check,
        )
    _create_snapshot_guard()


def upgrade() -> None:
    _replace_constraints(
        product_check=_positive_integer_check("default_cutting_mode"),
        snapshot_check=_positive_integer_check(
            "snapshot_component_default_cutting_mode"
        ),
    )


def downgrade() -> None:
    connection = op.get_bind()
    newer_fact_count = int(
        connection.execute(
            sa.text(
                f"SELECT (SELECT COUNT(*) FROM {PRODUCT_TABLE} "
                f"WHERE default_cutting_mode NOT IN {LEGACY_MODES}) + "
                f"(SELECT COUNT(*) FROM {BOM_SNAPSHOT_TABLE} "
                f"WHERE snapshot_component_default_cutting_mode NOT IN {LEGACY_MODES})"
            )
        ).scalar_one()
    )
    if newer_fact_count:
        raise RuntimeError(
            "已有一开七及以上产品或订单快照，禁止破坏性降级；请恢复升级前完整备份。"
        )
    _replace_constraints(
        product_check=f"default_cutting_mode IN {LEGACY_MODES}",
        snapshot_check=(
            f"snapshot_component_default_cutting_mode IN {LEGACY_MODES}"
        ),
    )
