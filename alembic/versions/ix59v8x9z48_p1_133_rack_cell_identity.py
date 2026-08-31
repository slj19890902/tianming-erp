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


def downgrade() -> None:
    op.drop_index(INDEX_NAME, table_name="warehouse_locations")
    with op.batch_alter_table("stocktake_orders") as batch_op:
        batch_op.drop_column("published_map_revision")
        batch_op.drop_column("location_position_status")
        batch_op.drop_column("location_address_version")
