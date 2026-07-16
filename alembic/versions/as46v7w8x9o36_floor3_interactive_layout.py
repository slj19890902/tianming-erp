"""Add interactive floor-three layouts and immutable movement metadata.

Revision ID: as46v7w8x9o36
Revises: ar45v7w8x9n35
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP
from math import ceil, sqrt
from typing import Any

from alembic import op
import sqlalchemy as sa


revision = "as46v7w8x9o36"
down_revision = "ar45v7w8x9n35"
branch_labels = None
depends_on = None


DOWNGRADE_BLOCKED_MESSAGE = (
    "floor3 interactive layout downgrade blocked: manual layouts, layout versions "
    "greater than 1, or non-empty movement idempotency keys exist"
)

_QUANTUM = Decimal("0.0001")
_ZERO = Decimal("0")
_HUNDRED = Decimal("100")


def _q(value: Decimal) -> Decimal:
    return value.quantize(_QUANTUM, rounding=ROUND_HALF_UP)


def _boundary(start: Decimal, end: Decimal, count: int, index: int) -> Decimal:
    if index <= 0:
        return _q(start)
    if index >= count:
        return _q(end)
    return _q(start + (end - start) * Decimal(index) / Decimal(count))


def _cell(
    column: int,
    row: int,
    columns: int,
    rows: int,
    left: Decimal = _ZERO,
    top: Decimal = _ZERO,
    right: Decimal = _HUNDRED,
    bottom: Decimal = _HUNDRED,
) -> dict[str, Decimal]:
    cell_left = _boundary(left, right, columns, column)
    cell_right = _boundary(left, right, columns, column + 1)
    cell_top = _boundary(top, bottom, rows, row)
    cell_bottom = _boundary(top, bottom, rows, row + 1)
    return {
        "left_pct": cell_left,
        "top_pct": cell_top,
        "width_pct": cell_right - cell_left,
        "height_pct": cell_bottom - cell_top,
    }


def _side_sort_key(side_code: Any) -> tuple[int, str]:
    order = {"L": 0, "M": 1, "R": 2, "P": 3, "U": 4, "D": 5}
    value = str(side_code or "")
    return order.get(value, 99), value


def _row_key(row: dict[str, Any]) -> tuple[Any, ...]:
    return (_side_sort_key(row.get("side_code")), int(row["sort_order"] or 0), int(row["id"]))


def _unrotate_display_cell(cell: dict[str, Decimal]) -> dict[str, Decimal]:
    """Persist a desired clockwise-rotated screen cell in source-map coordinates."""
    return {
        "left_pct": _q(_HUNDRED - cell["top_pct"] - cell["height_pct"]),
        "top_pct": _q(cell["left_pct"]),
        "width_pct": _q(cell["height_pct"]),
        "height_pct": _q(cell["width_pct"]),
    }


def _layout_rows(connection: sa.Connection) -> list[dict[str, Any]]:
    source_rows = [
        dict(row)
        for row in connection.execute(
            sa.text(
                """
                SELECT id, area_code, storage_type, level_no, side_code,
                       sort_order, location_code
                FROM warehouse_locations
                WHERE source_version = 'V11'
                ORDER BY area_code, storage_type, level_no, side_code,
                         sort_order, id
                """
            )
        ).mappings()
    ]
    if len(source_rows) != 396:
        raise RuntimeError(
            "floor3 interactive layout seed requires exactly 396 V11 locations; "
            f"found {len(source_rows)}"
        )

    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in source_rows:
        grouped[(str(row["area_code"]), str(row["storage_type"]))].append(row)

    by_area: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for area_code, storage_type in grouped:
        by_area[area_code].append((area_code, storage_type))

    result: list[dict[str, Any]] = []
    for area_code in sorted(by_area):
        if area_code == "D1":
            ground_rows = sorted(grouped[("D1", "ground")], key=_row_key)
            rack_rows = sorted(grouped[("D1", "rack")], key=_row_key)
            left_rows = [row for row in ground_rows if row.get("side_code") == "L"]
            right_rows = [row for row in ground_rows if row.get("side_code") == "R"]
            structured_groups = (
                (rack_rows, Decimal("4"), Decimal("34"), 6),
                (left_rows, Decimal("41"), Decimal("66"), 5),
                (right_rows, Decimal("70"), Decimal("95"), 5),
            )
            for rows, top, bottom, z_index in structured_groups:
                for index, row in enumerate(rows):
                    display_cell = _cell(
                        index,
                        0,
                        len(rows),
                        1,
                        top=top,
                        bottom=bottom,
                    )
                    result.append(
                        {
                            "location_id": row["id"],
                            **_unrotate_display_cell(display_cell),
                            "z_index": z_index,
                            "version": 1,
                            "source_type": "seeded",
                        }
                    )
            continue
        if area_code == "E4":
            ground_rows = sorted(grouped[("E4", "ground")], key=_row_key)
            rack_rows = sorted(grouped[("E4", "rack")], key=_row_key)
            left_rows = [row for row in ground_rows if row["location_code"].startswith("E4-L")]
            right_rows = [row for row in ground_rows if row["location_code"].startswith("E4-R")]
            structured_groups = (
                (rack_rows, Decimal("2"), Decimal("98"), Decimal("2"), Decimal("29"), 6),
                (left_rows, Decimal("2"), Decimal("98"), Decimal("34"), Decimal("65"), 5),
                (right_rows, Decimal("2"), Decimal("98"), Decimal("67"), Decimal("98"), 5),
            )
            for rows, top, bottom, left, right, z_index in structured_groups:
                for index, row in enumerate(rows):
                    display_cell = _cell(
                        0,
                        index,
                        1,
                        len(rows),
                        left=left,
                        right=right,
                        top=top,
                        bottom=bottom,
                    )
                    result.append(
                        {
                            "location_id": row["id"],
                            **_unrotate_display_cell(display_cell),
                            "z_index": z_index,
                            "version": 1,
                            "source_type": "seeded",
                        }
                    )
            continue
        if area_code == "DE1":
            rows = sorted(grouped[("DE1", "ground")], key=_row_key)
            for index, row in enumerate(rows):
                display_cell = _cell(
                    0,
                    index,
                    1,
                    len(rows),
                    left=Decimal("12"),
                    right=Decimal("88"),
                    top=Decimal("3"),
                    bottom=Decimal("98"),
                )
                result.append(
                    {
                        "location_id": row["id"],
                        **_unrotate_display_cell(display_cell),
                        "z_index": 5,
                        "version": 1,
                        "source_type": "seeded",
                    }
                )
            continue
        groups = sorted(by_area[area_code], key=lambda item: item[1])
        group_count = len(groups)
        for group_index, group_key in enumerate(groups):
            rows = sorted(grouped[group_key], key=_row_key)
            band_top = _boundary(_ZERO, _HUNDRED, group_count, group_index)
            band_bottom = _boundary(_ZERO, _HUNDRED, group_count, group_index + 1)
            storage_type = group_key[1]

            cells: list[dict[str, Decimal]] = []
            if storage_type == "temporary_aisle":
                cells = [
                    _cell(
                        0,
                        index,
                        1,
                        len(rows),
                        top=band_top,
                        bottom=band_bottom,
                    )
                    for index in range(len(rows))
                ]
            elif storage_type == "rack" and any(
                row["level_no"] is not None for row in rows
            ):
                levels = sorted({int(row["level_no"]) for row in rows})
                rows_by_level: dict[int, list[dict[str, Any]]] = defaultdict(list)
                for row in rows:
                    rows_by_level[int(row["level_no"])].append(row)
                max_positions = max(len(level_rows) for level_rows in rows_by_level.values())
                level_ordered_rows: list[dict[str, Any]] = []
                for level_index, level in enumerate(levels):
                    level_rows = sorted(rows_by_level[level], key=_row_key)
                    level_ordered_rows.extend(level_rows)
                    cells.extend(
                        _cell(
                            position,
                            level_index,
                            max_positions,
                            len(levels),
                            top=band_top,
                            bottom=band_bottom,
                        )
                        for position, _row in enumerate(level_rows)
                    )
                # Cells are emitted level by level, so pair them with rows in
                # that same order instead of the side-first general ordering.
                rows = level_ordered_rows
            else:
                sides = sorted(
                    {row["side_code"] for row in rows if row["side_code"] is not None},
                    key=_side_sort_key,
                )
                if sides:
                    rows_by_side: dict[Any, list[dict[str, Any]]] = defaultdict(list)
                    for row in rows:
                        rows_by_side[row["side_code"]].append(row)
                    max_positions = max(len(side_rows) for side_rows in rows_by_side.values())
                    for column, side in enumerate(sides):
                        side_rows = sorted(rows_by_side[side], key=_row_key)
                        cells.extend(
                            _cell(
                                column,
                                position,
                                len(sides),
                                max_positions,
                                top=band_top,
                                bottom=band_bottom,
                            )
                            for position, _row in enumerate(side_rows)
                        )
                else:
                    columns = max(1, ceil(sqrt(len(rows))))
                    grid_rows = ceil(len(rows) / columns)
                    cells = [
                        _cell(
                            index % columns,
                            index // columns,
                            columns,
                            grid_rows,
                            top=band_top,
                            bottom=band_bottom,
                        )
                        for index in range(len(rows))
                    ]

            if len(cells) != len(rows):
                raise RuntimeError(
                    f"layout generator lost rows for {area_code}/{storage_type}"
                )
            result.extend(
                {
                    "location_id": row["id"],
                    **cell,
                    "z_index": group_index,
                    "version": 1,
                    "source_type": "seeded",
                }
                for row, cell in zip(rows, cells)
            )

    if len(result) != 396 or len({row["location_id"] for row in result}) != 396:
        raise RuntimeError("floor3 interactive layout seed must contain 396 unique layouts")
    return result


def _create_movement_immutability_guards() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        op.execute(
            """
            CREATE TRIGGER trg_inventory_location_movements_immutable_update
            BEFORE UPDATE ON inventory_location_movements
            FOR EACH ROW
            BEGIN
                SELECT RAISE(ABORT, 'inventory_location_movements rows are immutable');
            END
            """
        )
        op.execute(
            """
            CREATE TRIGGER trg_inventory_location_movements_immutable_delete
            BEFORE DELETE ON inventory_location_movements
            FOR EACH ROW
            BEGIN
                SELECT RAISE(ABORT, 'inventory_location_movements rows are immutable');
            END
            """
        )
    elif dialect == "postgresql":
        op.execute(
            """
            CREATE OR REPLACE FUNCTION floor3_immutable_inventory_location_movement()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                RAISE EXCEPTION 'inventory_location_movements rows are immutable';
            END;
            $$
            """
        )
        op.execute(
            """
            CREATE TRIGGER trg_inventory_location_movements_immutable
            BEFORE UPDATE OR DELETE ON inventory_location_movements
            FOR EACH ROW EXECUTE FUNCTION floor3_immutable_inventory_location_movement()
            """
        )


def _drop_movement_immutability_guards() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS trg_inventory_location_movements_immutable_delete")
        op.execute("DROP TRIGGER IF EXISTS trg_inventory_location_movements_immutable_update")
    elif dialect == "postgresql":
        op.execute(
            "DROP TRIGGER IF EXISTS trg_inventory_location_movements_immutable "
            "ON inventory_location_movements"
        )
        op.execute(
            "DROP FUNCTION IF EXISTS floor3_immutable_inventory_location_movement()"
        )


def _assert_safe_downgrade(connection: sa.Connection) -> None:
    layout_usage = connection.execute(
        sa.text(
            """
            SELECT COUNT(*)
            FROM floor3_location_layouts
            WHERE source_type = 'manual' OR version > 1
            """
        )
    ).scalar_one()
    movement_usage = connection.execute(
        sa.text(
            """
            SELECT COUNT(*)
            FROM inventory_location_movements
            WHERE idempotency_key IS NOT NULL
            """
        )
    ).scalar_one()
    if int(layout_usage or 0) or int(movement_usage or 0):
        raise RuntimeError(DOWNGRADE_BLOCKED_MESSAGE)


def upgrade() -> None:
    op.create_table(
        "floor3_location_layouts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("location_id", sa.Integer(), nullable=False),
        sa.Column("left_pct", sa.Numeric(7, 4), nullable=False),
        sa.Column("top_pct", sa.Numeric(7, 4), nullable=False),
        sa.Column("width_pct", sa.Numeric(7, 4), nullable=False),
        sa.Column("height_pct", sa.Numeric(7, 4), nullable=False),
        sa.Column("z_index", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("source_type", sa.String(20), nullable=False, server_default="manual"),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("updated_by", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["location_id"], ["warehouse_locations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("location_id", name="uq_floor3_location_layouts_location"),
        sa.CheckConstraint(
            "left_pct >= 0 AND left_pct <= 100",
            name="ck_floor3_location_layouts_left_pct",
        ),
        sa.CheckConstraint(
            "top_pct >= 0 AND top_pct <= 100",
            name="ck_floor3_location_layouts_top_pct",
        ),
        sa.CheckConstraint(
            "width_pct > 0 AND width_pct <= 100",
            name="ck_floor3_location_layouts_width_pct",
        ),
        sa.CheckConstraint(
            "height_pct > 0 AND height_pct <= 100",
            name="ck_floor3_location_layouts_height_pct",
        ),
        sa.CheckConstraint(
            "left_pct + width_pct <= 100",
            name="ck_floor3_location_layouts_right_pct",
        ),
        sa.CheckConstraint(
            "top_pct + height_pct <= 100",
            name="ck_floor3_location_layouts_bottom_pct",
        ),
        sa.CheckConstraint(
            "version > 0", name="ck_floor3_location_layouts_version"
        ),
        sa.CheckConstraint(
            "source_type IN ('seeded','manual')",
            name="ck_floor3_location_layouts_source_type",
        ),
    )

    connection = op.get_bind()
    layouts = sa.table(
        "floor3_location_layouts",
        sa.column("location_id", sa.Integer()),
        sa.column("left_pct", sa.Numeric(7, 4)),
        sa.column("top_pct", sa.Numeric(7, 4)),
        sa.column("width_pct", sa.Numeric(7, 4)),
        sa.column("height_pct", sa.Numeric(7, 4)),
        sa.column("z_index", sa.Integer()),
        sa.column("version", sa.Integer()),
        sa.column("source_type", sa.String(20)),
    )
    connection.execute(sa.insert(layouts), _layout_rows(connection))

    op.add_column(
        "inventory_location_movements",
        sa.Column("idempotency_key", sa.String(120), nullable=True),
    )
    op.add_column(
        "inventory_location_movements",
        sa.Column("confirmed_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "inventory_location_movements",
        sa.Column("pallet_version_before", sa.Integer(), nullable=True),
    )
    op.add_column(
        "inventory_location_movements",
        sa.Column("pallet_version_after", sa.Integer(), nullable=True),
    )
    if connection.dialect.name == "sqlite":
        op.create_index(
            "uq_inventory_location_movements_idempotency_key",
            "inventory_location_movements",
            ["idempotency_key"],
            unique=True,
            sqlite_where=sa.text("idempotency_key IS NOT NULL"),
        )
    elif connection.dialect.name == "postgresql":
        op.create_index(
            "uq_inventory_location_movements_idempotency_key",
            "inventory_location_movements",
            ["idempotency_key"],
            unique=True,
            postgresql_where=sa.text("idempotency_key IS NOT NULL"),
        )
    else:
        op.create_index(
            "uq_inventory_location_movements_idempotency_key",
            "inventory_location_movements",
            ["idempotency_key"],
            unique=True,
        )
    _create_movement_immutability_guards()


def downgrade() -> None:
    connection = op.get_bind()
    _assert_safe_downgrade(connection)
    _drop_movement_immutability_guards()
    op.drop_index(
        "uq_inventory_location_movements_idempotency_key",
        table_name="inventory_location_movements",
    )
    for column_name in (
        "pallet_version_after",
        "pallet_version_before",
        "confirmed_at",
        "idempotency_key",
    ):
        op.drop_column("inventory_location_movements", column_name)
    op.drop_table("floor3_location_layouts")
