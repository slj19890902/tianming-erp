"""N081-B0 inventory source, ownership, and stock-date semantics.

Revision ID: cj66v8x9z55
Revises: cp72v8x9z61
Create Date: 2026-07-23
"""

from __future__ import annotations

from collections.abc import Callable

from alembic import op
import sqlalchemy as sa


revision = "cj66v8x9z55"
down_revision = "cp72v8x9z61"
branch_labels = None
depends_on = None


_B0_TRIGGER_NAMES = frozenset(
    {
        "trg_inventory_lots_unknown_date_source_insert",
        "trg_inventory_lots_unknown_date_source_update",
    }
)


def _sqlite_inventory_lot_related_triggers(
    connection: sa.Connection,
) -> list[tuple[str, str]]:
    return [
        (str(row.name), str(row.sql))
        for row in connection.execute(
            sa.text(
                "SELECT name, sql FROM sqlite_master "
                "WHERE type = 'trigger' "
                "AND sql IS NOT NULL "
                "AND INSTR(LOWER(sql), 'inventory_lots') > 0 "
                "ORDER BY name"
            )
        )
    ]


def _restore_missing_sqlite_triggers(
    connection: sa.Connection,
    triggers: list[tuple[str, str]],
    *,
    best_effort: bool,
) -> None:
    existing_triggers = {
        str(row.name)
        for row in connection.execute(
            sa.text("SELECT name FROM sqlite_master WHERE type = 'trigger'")
        )
    }
    for trigger_name, trigger_sql in triggers:
        if trigger_name in existing_triggers:
            continue
        try:
            connection.exec_driver_sql(trigger_sql)
            existing_triggers.add(trigger_name)
        except Exception:
            if not best_effort:
                raise


def _run_sqlite_inventory_lot_rebuild(
    connection: sa.Connection,
    rebuild: Callable[[], None],
) -> None:
    related_triggers = _sqlite_inventory_lot_related_triggers(connection)
    preserved_triggers = [
        (trigger_name, trigger_sql)
        for trigger_name, trigger_sql in related_triggers
        if trigger_name not in _B0_TRIGGER_NAMES
    ]
    trigger_names_to_drop = {
        trigger_name for trigger_name, _ in related_triggers
    } | set(_B0_TRIGGER_NAMES)
    for trigger_name in sorted(trigger_names_to_drop):
        quoted_name = '"' + trigger_name.replace('"', '""') + '"'
        connection.exec_driver_sql(f"DROP TRIGGER IF EXISTS {quoted_name}")

    try:
        rebuild()
    except Exception:
        # Preserve the original rebuild error.  Restoration is best effort
        # because a failure after SQLite drops the old table can also prevent
        # recreating triggers attached to that table.
        try:
            _restore_missing_sqlite_triggers(
                connection,
                related_triggers,
                best_effort=True,
            )
        except Exception:
            pass
        raise

    _restore_missing_sqlite_triggers(
        connection,
        preserved_triggers,
        best_effort=False,
    )


def upgrade() -> None:
    # SQLite can add these columns in place.  Do not batch-rebuild the formal
    # inventory table merely to add B0 metadata.
    op.add_column(
        "inventory_lots",
        sa.Column(
            "stock_date_accuracy",
            sa.String(length=20),
            sa.CheckConstraint(
                "stock_date_accuracy IN ('exact','estimated','unknown')",
                name="ck_inventory_lots_stock_date_accuracy",
            ),
            nullable=False,
            server_default=sa.text("'exact'"),
        ),
    )
    op.add_column(
        "inventory_lots",
        sa.Column("stock_date_original_text", sa.Text(), nullable=True),
    )

    # A legacy date value has no recorded provenance, so migration must not
    # silently promote it to an exact fact.
    op.execute(
        sa.text(
            "UPDATE inventory_lots "
            "SET stock_date_accuracy = 'unknown' "
        )
    )
    if op.get_bind().dialect.name == "sqlite":
        # Existing legacy rows intentionally remain unknown + NULL.  Any new
        # deliberate unknown fact must carry source text or the explicit
        # service marker, so downgrade can distinguish it from the backfill.
        op.execute(
            """
            CREATE TRIGGER trg_inventory_lots_unknown_date_source_insert
            BEFORE INSERT ON inventory_lots
            FOR EACH ROW
            WHEN NEW.stock_date_accuracy = 'unknown'
             AND TRIM(COALESCE(NEW.stock_date_original_text, '')) = ''
            BEGIN
                SELECT RAISE(
                    ABORT,
                    'new unknown stock date requires original text'
                );
            END
            """
        )
        op.execute(
            """
            CREATE TRIGGER trg_inventory_lots_unknown_date_source_update
            BEFORE UPDATE OF stock_date, stock_date_accuracy,
                stock_date_original_text
            ON inventory_lots
            FOR EACH ROW
            WHEN NEW.stock_date_accuracy = 'unknown'
             AND TRIM(COALESCE(NEW.stock_date_original_text, '')) = ''
            BEGIN
                SELECT RAISE(
                    ABORT,
                    'new unknown stock date requires original text'
                );
            END
            """
        )


def downgrade() -> None:
    connection = op.get_bind()
    fact_count = int(
        connection.execute(
            sa.text(
                "SELECT COUNT(*) FROM inventory_lots "
                "WHERE stock_date_accuracy != 'unknown' "
                "OR stock_date_original_text IS NOT NULL"
            )
        ).scalar_one()
    )
    if fact_count:
        raise RuntimeError(
            "N081-B0 stock-date facts exist; restore the verified pre-upgrade "
            "backup instead of dropping date accuracy or original text"
        )

    if connection.dialect.name == "sqlite":
        # A later migration can batch-rebuild ``inventory_lots`` while these
        # columns still exist.  SQLite then stores the named accuracy CHECK as
        # a table constraint instead of an inline column constraint.  Native
        # ``DROP COLUMN`` leaves that CHECK referring to the removed column and
        # aborts a deep head -> cp72 downgrade after the first column was
        # already dropped.  Recreate the table in one Alembic batch step and
        # remove the named CHECK together with both B0 columns.  Alembic does
        # not recreate SQLite triggers when it rebuilds a table.  Temporarily
        # drop and then restore both triggers on this table and triggers on
        # other tables that reference it; otherwise SQLite rejects the rename
        # while the external placement guard points at a missing table.
        def rebuild_inventory_lots() -> None:
            with op.batch_alter_table(
                "inventory_lots",
                recreate="always",
            ) as batch_op:
                batch_op.drop_constraint(
                    "ck_inventory_lots_stock_date_accuracy",
                    type_="check",
                )
                batch_op.drop_column("stock_date_original_text")
                batch_op.drop_column("stock_date_accuracy")

        _run_sqlite_inventory_lot_rebuild(
            connection,
            rebuild_inventory_lots,
        )
        return

    op.drop_constraint(
        "ck_inventory_lots_stock_date_accuracy",
        "inventory_lots",
        type_="check",
    )
    op.drop_column("inventory_lots", "stock_date_original_text")
    op.drop_column("inventory_lots", "stock_date_accuracy")
