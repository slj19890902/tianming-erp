"""enforce one formal location per published rack cell

Revision ID: ix59v8x9z48
Revises: iw58v8x9z47
Create Date: 2026-09-01

This migration performs no backfill. Existing unbound locations remain
unbound; unsafe or ambiguous rack bindings stop the upgrade for manual review.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "ix59v8x9z48"
down_revision = "iw58v8x9z47"
branch_labels = None
depends_on = None


INDEX_NAME = "uq_warehouse_locations_map_rack_cell"
IDENTITY_GUARD_TRIGGER = "trg_stocktake_orders_location_identity_guard"
IDENTITY_GUARD_FUNCTION = "stocktake_orders_location_identity_guard"
INVALID_BINDING_MESSAGE = (
    "P1-133 rack-cell identity upgrade blocked: invalid existing rack binding"
)
DUPLICATE_BINDING_MESSAGE = (
    "P1-133 rack-cell identity upgrade blocked: duplicate existing rack-cell key"
)
PENDING_STOCKTAKE_MESSAGE = (
    "P1-133 rack-cell identity upgrade blocked: submitted stocktake must be reviewed first"
)


def _valid_cell_number(value: object) -> bool:
    return type(value) is int and 1 <= value <= 99


def _render_rows(rows: list[dict[str, object]]) -> str:
    return "; ".join(
        "id={id},code={location_code!r},kind={address_kind!r},"
        "rack={map_rack_id!r},level={level_no!r},slot={slot_no!r}".format(**row)
        for row in rows[:10]
    )


def _assert_existing_rack_bindings_are_safe() -> None:
    connection = op.get_bind()
    rows = list(
        connection.execute(
            sa.text(
                "SELECT id, location_code, address_kind, map_rack_id, level_no, slot_no "
                "FROM warehouse_locations WHERE map_rack_id IS NOT NULL ORDER BY id"
            )
        ).mappings()
    )

    invalid: list[dict[str, object]] = []
    cells: dict[tuple[str, int, int], dict[str, object]] = {}
    duplicates: list[dict[str, object]] = []
    for raw_row in rows:
        row = dict(raw_row)
        rack_id = row["map_rack_id"]
        level_no = row["level_no"]
        slot_no = row["slot_no"]
        if (
            not isinstance(rack_id, str)
            or not rack_id.strip()
            or rack_id != rack_id.strip()
            or row["address_kind"] != "rack_slot"
            or not _valid_cell_number(level_no)
            or not _valid_cell_number(slot_no)
        ):
            invalid.append(row)
            continue

        key = (rack_id, level_no, slot_no)
        first = cells.setdefault(key, row)
        if first is not row:
            if first not in duplicates:
                duplicates.append(first)
            duplicates.append(row)

    if invalid:
        raise RuntimeError(f"{INVALID_BINDING_MESSAGE}: {_render_rows(invalid)}")
    if duplicates:
        raise RuntimeError(f"{DUPLICATE_BINDING_MESSAGE}: {_render_rows(duplicates)}")


def _assert_no_pending_stocktakes() -> None:
    rows = list(
        op.get_bind()
        .execute(
            sa.text(
                "SELECT id, order_number FROM stocktake_orders "
                "WHERE status = 'submitted' ORDER BY id LIMIT 10"
            )
        )
        .mappings()
    )
    if rows:
        rendered = "; ".join(
            f"id={row['id']},number={row['order_number']!r}" for row in rows
        )
        raise RuntimeError(f"{PENDING_STOCKTAKE_MESSAGE}: {rendered}")


def _create_stocktake_identity_guard() -> None:
    connection = op.get_bind()
    if connection.dialect.name == "sqlite":
        connection.execute(
            sa.text(
                f"""
                CREATE TRIGGER {IDENTITY_GUARD_TRIGGER}
                BEFORE UPDATE ON stocktake_orders
                WHEN OLD.status <> 'draft' AND NOT (
                    NEW.location_address_version IS OLD.location_address_version
                    AND NEW.location_position_status IS OLD.location_position_status
                    AND NEW.published_map_revision IS OLD.published_map_revision
                )
                BEGIN
                    SELECT RAISE(
                        ABORT,
                        'submitted stocktake location identity is immutable'
                    );
                END
                """
            )
        )
    elif connection.dialect.name == "postgresql":
        connection.execute(
            sa.text(
                f"""
                CREATE FUNCTION {IDENTITY_GUARD_FUNCTION}() RETURNS trigger AS $$
                BEGIN
                    IF OLD.status <> 'draft' AND NOT (
                        NEW.location_address_version IS NOT DISTINCT FROM
                            OLD.location_address_version
                        AND NEW.location_position_status IS NOT DISTINCT FROM
                            OLD.location_position_status
                        AND NEW.published_map_revision IS NOT DISTINCT FROM
                            OLD.published_map_revision
                    ) THEN
                        RAISE EXCEPTION
                            'submitted stocktake location identity is immutable';
                    END IF;
                    RETURN NEW;
                END;
                $$ LANGUAGE plpgsql;

                CREATE TRIGGER {IDENTITY_GUARD_TRIGGER}
                BEFORE UPDATE ON stocktake_orders
                FOR EACH ROW EXECUTE FUNCTION {IDENTITY_GUARD_FUNCTION}();
                """
            )
        )


def _drop_stocktake_identity_guard() -> None:
    connection = op.get_bind()
    if connection.dialect.name == "sqlite":
        connection.execute(
            sa.text(f"DROP TRIGGER IF EXISTS {IDENTITY_GUARD_TRIGGER}")
        )
    elif connection.dialect.name == "postgresql":
        connection.execute(
            sa.text(
                f"DROP TRIGGER IF EXISTS {IDENTITY_GUARD_TRIGGER} "
                "ON stocktake_orders"
            )
        )
        connection.execute(
            sa.text(f"DROP FUNCTION IF EXISTS {IDENTITY_GUARD_FUNCTION}()")
        )


def _drop_sqlite_triggers_depending_on_stocktake_orders() -> list[str]:
    connection = op.get_bind()
    if connection.dialect.name != "sqlite":
        return []
    rows = list(
        connection.execute(
            sa.text(
                "SELECT name, sql FROM sqlite_master "
                "WHERE type = 'trigger' AND sql IS NOT NULL "
                "AND (tbl_name = 'stocktake_orders' "
                "OR instr(lower(sql), 'stocktake_orders') > 0) "
                "ORDER BY name"
            )
        ).mappings()
    )
    trigger_sql: list[str] = []
    for row in rows:
        name = str(row["name"])
        sql = str(row["sql"])
        quoted_name = connection.dialect.identifier_preparer.quote(name)
        connection.execute(sa.text(f"DROP TRIGGER IF EXISTS {quoted_name}"))
        trigger_sql.append(sql)
    return trigger_sql


def _restore_sqlite_triggers(trigger_sql: list[str]) -> None:
    connection = op.get_bind()
    for statement in trigger_sql:
        connection.execute(sa.text(statement))


def upgrade() -> None:
    _assert_existing_rack_bindings_are_safe()
    _assert_no_pending_stocktakes()
    op.add_column(
        "stocktake_orders",
        sa.Column("location_address_version", sa.Integer(), nullable=True),
    )
    op.add_column(
        "stocktake_orders",
        sa.Column("location_position_status", sa.String(length=30), nullable=True),
    )
    op.add_column(
        "stocktake_orders",
        sa.Column("published_map_revision", sa.String(length=64), nullable=True),
    )
    op.create_index(
        INDEX_NAME,
        "warehouse_locations",
        ["map_rack_id", "level_no", "slot_no"],
        unique=True,
        sqlite_where=sa.text("map_rack_id IS NOT NULL"),
        postgresql_where=sa.text("map_rack_id IS NOT NULL"),
    )
    _create_stocktake_identity_guard()


def downgrade() -> None:
    _drop_stocktake_identity_guard()
    op.drop_index(INDEX_NAME, table_name="warehouse_locations")
    dependent_triggers = _drop_sqlite_triggers_depending_on_stocktake_orders()
    try:
        with op.batch_alter_table("stocktake_orders") as batch_op:
            batch_op.drop_column("published_map_revision")
            batch_op.drop_column("location_position_status")
            batch_op.drop_column("location_address_version")
    finally:
        _restore_sqlite_triggers(dependent_triggers)
