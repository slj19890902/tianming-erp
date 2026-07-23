"""add N081-A space placement readiness gate

Revision ID: ch64v8x9z53
Revises: cg63v8x9z52
Create Date: 2026-07-23
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "ch64v8x9z53"
down_revision: Union[str, Sequence[str], None] = "cg63v8x9z52"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


C1_EXTENSION_CODES = ("C1-R12", "C1-R13")
DOWNGRADE_BLOCKED_MESSAGE = (
    "N081-A 补齐的 C1-R12/C1-R13 已承载库存、栈板或人工布局，禁止破坏性降级；"
    "请恢复升级前完整备份。"
)


def _create_sqlite_guards() -> None:
    op.execute(
        """
        CREATE TRIGGER trg_warehouse_locations_placement_status_insert
        BEFORE INSERT ON warehouse_locations
        FOR EACH ROW
        WHEN NEW.placement_status IS NULL
          OR NEW.placement_status NOT IN ('unplaced','placed')
        BEGIN
            SELECT RAISE(ABORT, 'invalid warehouse_locations.placement_status');
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_warehouse_locations_placement_status_update
        BEFORE UPDATE OF placement_status ON warehouse_locations
        FOR EACH ROW
        WHEN NEW.placement_status IS NULL
          OR NEW.placement_status NOT IN ('unplaced','placed')
        BEGIN
            SELECT RAISE(ABORT, 'invalid warehouse_locations.placement_status');
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_warehouse_locations_placed_fields_insert
        BEFORE INSERT ON warehouse_locations
        FOR EACH ROW
        WHEN NEW.placement_status = 'placed'
         AND (
              NEW.warehouse_floor IS NULL
              OR TRIM(COALESCE(NEW.area_code, '')) = ''
              OR TRIM(COALESCE(NEW.storage_type, '')) = ''
         )
        BEGIN
            SELECT RAISE(ABORT, 'placed warehouse location requires floor area and storage type');
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_warehouse_locations_placed_fields_update
        BEFORE UPDATE OF placement_status, warehouse_floor, area_code, storage_type
        ON warehouse_locations
        FOR EACH ROW
        WHEN NEW.placement_status = 'placed'
         AND (
              NEW.warehouse_floor IS NULL
              OR TRIM(COALESCE(NEW.area_code, '')) = ''
              OR TRIM(COALESCE(NEW.storage_type, '')) = ''
         )
        BEGIN
            SELECT RAISE(ABORT, 'placed warehouse location requires floor area and storage type');
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_inventory_lots_require_placed_location_insert
        BEFORE INSERT ON inventory_lots
        FOR EACH ROW
        WHEN NEW.status IN ('active','frozen')
         AND NOT EXISTS (
            SELECT 1 FROM warehouse_locations location
            WHERE location.id = NEW.warehouse_location_id
              AND location.is_active = 1
              AND location.placement_status = 'placed'
        )
        BEGIN
            SELECT RAISE(ABORT, 'inventory lot requires an active placed location');
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_inventory_lots_require_placed_location_update
        BEFORE UPDATE OF warehouse_location_id, status ON inventory_lots
        FOR EACH ROW
        WHEN NEW.status IN ('active','frozen')
         AND NOT EXISTS (
            SELECT 1 FROM warehouse_locations location
            WHERE location.id = NEW.warehouse_location_id
              AND location.is_active = 1
              AND location.placement_status = 'placed'
         )
        BEGIN
            SELECT RAISE(ABORT, 'inventory lot requires an active placed location');
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_inventory_pallets_require_placed_location_insert
        BEFORE INSERT ON inventory_pallets
        FOR EACH ROW
        WHEN NEW.is_current = 1
         AND NOT EXISTS (
            SELECT 1 FROM warehouse_locations location
            WHERE location.id = NEW.location_id
              AND location.is_active = 1
              AND location.placement_status = 'placed'
         )
        BEGIN
            SELECT RAISE(ABORT, 'current pallet requires an active placed location');
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_inventory_pallets_require_placed_location_update
        BEFORE UPDATE OF location_id, is_current ON inventory_pallets
        FOR EACH ROW
        WHEN NEW.is_current = 1
         AND NOT EXISTS (
            SELECT 1 FROM warehouse_locations location
            WHERE location.id = NEW.location_id
              AND location.is_active = 1
              AND location.placement_status = 'placed'
         )
        BEGIN
            SELECT RAISE(ABORT, 'current pallet requires an active placed location');
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_warehouse_locations_unplace_reference_guard
        BEFORE UPDATE OF placement_status, is_active ON warehouse_locations
        FOR EACH ROW
        WHEN (NEW.placement_status <> 'placed' OR NEW.is_active <> 1)
         AND (
            EXISTS (
                SELECT 1 FROM inventory_lots lot
                WHERE lot.warehouse_location_id = NEW.id
                  AND lot.status IN ('active','frozen')
            )
            OR EXISTS (
                SELECT 1 FROM inventory_pallets pallet
                WHERE pallet.location_id = NEW.id
                  AND pallet.is_current = 1
            )
         )
        BEGIN
            SELECT RAISE(ABORT, 'referenced warehouse location cannot be unplaced or disabled');
        END
        """
    )


def _drop_sqlite_guards() -> None:
    for trigger in (
        "trg_warehouse_locations_unplace_reference_guard",
        "trg_inventory_pallets_require_placed_location_update",
        "trg_inventory_pallets_require_placed_location_insert",
        "trg_inventory_lots_require_placed_location_update",
        "trg_inventory_lots_require_placed_location_insert",
        "trg_warehouse_locations_placed_fields_update",
        "trg_warehouse_locations_placed_fields_insert",
        "trg_warehouse_locations_placement_status_update",
        "trg_warehouse_locations_placement_status_insert",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {trigger}")


def _backfill_c1_extension(connection: sa.Connection) -> None:
    rows = connection.execute(
        sa.text(
            """
            SELECT id, location_code, warehouse_type, warehouse_floor, area_code,
                   storage_type, source_version
            FROM warehouse_locations
            WHERE location_code IN ('C1-R12','C1-R13')
            ORDER BY location_code
            """
        )
    ).mappings().all()
    if not rows:
        return
    for row in rows:
        if row["warehouse_type"] not in {"finished", "shared"}:
            raise RuntimeError(
                f"{row['location_code']} 不是成品或共用库位，不能自动纳入 C1"
            )
        for field, actual, expected in (
            ("warehouse_floor", row["warehouse_floor"], 3),
            ("area_code", row["area_code"], "C1"),
            ("storage_type", row["storage_type"], "ground"),
        ):
            if actual not in (None, "", expected):
                raise RuntimeError(
                    f"{row['location_code']} 的 {field}={actual!r} 与 C1 物理位冲突"
                )

    connection.execute(
        sa.text(
            """
            UPDATE warehouse_locations
            SET warehouse_floor = 3,
                area_code = 'C1',
                storage_type = 'ground',
                is_temporary = 0,
                source_version = 'V11',
                placement_status = 'placed',
                sort_order = COALESCE(
                    (SELECT sort_order FROM warehouse_locations anchor
                     WHERE anchor.location_code = 'C1-R11'),
                    sort_order
                ) + CASE location_code WHEN 'C1-R12' THEN 1 ELSE 2 END
            WHERE location_code IN ('C1-R12','C1-R13')
            """
        )
    )

    right_rows = connection.execute(
        sa.text(
            """
            SELECT id, location_code
            FROM warehouse_locations
            WHERE warehouse_floor = 3
              AND area_code = 'C1'
              AND storage_type = 'ground'
              AND location_code LIKE 'C1-R%'
            ORDER BY CAST(SUBSTR(location_code, 5) AS INTEGER)
            """
        )
    ).mappings().all()
    numbers = [int(str(row["location_code"])[4:]) for row in right_rows]
    if numbers != list(range(1, len(numbers) + 1)):
        raise RuntimeError("C1 右侧货位编码不连续，拒绝自动重排平面图")
    if len(right_rows) <= 11:
        return

    manual_count = int(
        connection.execute(
            sa.text(
                """
                SELECT COUNT(*)
                FROM floor3_location_layouts layout
                JOIN warehouse_locations location ON location.id=layout.location_id
                WHERE location.location_code LIKE 'C1-R%'
                  AND (layout.source_type <> 'seeded' OR layout.version <> 1)
                """
            )
        ).scalar_one()
    )
    if manual_count:
        raise RuntimeError("C1 右侧平面图已有人工调整，拒绝自动覆盖")

    height = 100 / len(right_rows)
    for index, row in enumerate(right_rows):
        values = {
            "location_id": int(row["id"]),
            "left_pct": 50,
            "top_pct": round(index * height, 4),
            "width_pct": 50,
            "height_pct": round(100 - index * height, 4)
            if index == len(right_rows) - 1
            else round(height, 4),
        }
        updated = connection.execute(
            sa.text(
                """
                UPDATE floor3_location_layouts
                SET left_pct=:left_pct, top_pct=:top_pct,
                    width_pct=:width_pct, height_pct=:height_pct,
                    updated_at=CURRENT_TIMESTAMP
                WHERE location_id=:location_id
                """
            ),
            values,
        )
        if updated.rowcount:
            continue
        connection.execute(
            sa.text(
                """
                INSERT INTO floor3_location_layouts (
                    location_id, left_pct, top_pct, width_pct, height_pct,
                    z_index, version, source_type
                ) VALUES (
                    :location_id, :left_pct, :top_pct, :width_pct, :height_pct,
                    0, 1, 'seeded'
                )
                """
            ),
            values,
        )


def _assert_no_unplaced_references(connection: sa.Connection) -> None:
    bad_lots = int(
        connection.execute(
            sa.text(
                """
                SELECT COUNT(*)
                FROM inventory_lots lot
                JOIN warehouse_locations location
                  ON location.id=lot.warehouse_location_id
                WHERE lot.status IN ('active','frozen')
                  AND (
                    location.is_active <> 1
                    OR location.placement_status <> 'placed'
                  )
                """
            )
        ).scalar_one()
    )
    bad_pallets = int(
        connection.execute(
            sa.text(
                """
                SELECT COUNT(*)
                FROM inventory_pallets pallet
                JOIN warehouse_locations location ON location.id=pallet.location_id
                WHERE pallet.is_current=1
                  AND (
                    location.is_active <> 1
                    OR location.placement_status <> 'placed'
                  )
                """
            )
        ).scalar_one()
    )
    if bad_lots or bad_pallets:
        raise RuntimeError(
            "存在库存或当前栈板位于未放置库位，N081-A 迁移已停止"
        )


def upgrade() -> None:
    connection = op.get_bind()
    op.add_column(
        "warehouse_locations",
        sa.Column(
            "placement_status",
            sa.String(length=20),
            nullable=False,
            server_default="unplaced",
        ),
    )
    op.create_index(
        "ix_warehouse_locations_placement_active",
        "warehouse_locations",
        ["placement_status", "is_active"],
    )
    connection.execute(
        sa.text(
            """
            UPDATE warehouse_locations
            SET placement_status='placed'
            WHERE warehouse_floor IS NOT NULL
              AND TRIM(COALESCE(area_code, '')) <> ''
              AND TRIM(COALESCE(storage_type, '')) <> ''
              AND (
                warehouse_floor <> 3
                OR EXISTS (
                    SELECT 1 FROM floor3_location_layouts layout
                    WHERE layout.location_id=warehouse_locations.id
                )
              )
            """
        )
    )
    _backfill_c1_extension(connection)
    _assert_no_unplaced_references(connection)
    if connection.dialect.name == "sqlite":
        _create_sqlite_guards()
    else:
        op.create_check_constraint(
            "ck_warehouse_locations_placement_status",
            "warehouse_locations",
            "placement_status IN ('unplaced','placed')",
        )


def _assert_safe_downgrade(connection: sa.Connection) -> None:
    referenced = int(
        connection.execute(
            sa.text(
                """
                SELECT COUNT(*)
                FROM warehouse_locations location
                WHERE location.location_code IN ('C1-R12','C1-R13')
                  AND (
                    EXISTS (
                        SELECT 1 FROM inventory_lots lot
                        WHERE lot.warehouse_location_id=location.id
                    )
                    OR EXISTS (
                        SELECT 1 FROM inventory_pallets pallet
                        WHERE pallet.location_id=location.id
                    )
                  )
                """
            )
        ).scalar_one()
    )
    modified_layouts = int(
        connection.execute(
            sa.text(
                """
                SELECT COUNT(*)
                FROM floor3_location_layouts layout
                JOIN warehouse_locations location ON location.id=layout.location_id
                WHERE location.location_code IN ('C1-R12','C1-R13')
                  AND (layout.source_type <> 'seeded' OR layout.version <> 1)
                """
            )
        ).scalar_one()
    )
    if referenced or modified_layouts:
        raise RuntimeError(DOWNGRADE_BLOCKED_MESSAGE)


def downgrade() -> None:
    connection = op.get_bind()
    _assert_safe_downgrade(connection)
    if connection.dialect.name == "sqlite":
        _drop_sqlite_guards()
    else:
        op.drop_constraint(
            "ck_warehouse_locations_placement_status",
            "warehouse_locations",
            type_="check",
        )

    connection.execute(
        sa.text(
            """
            DELETE FROM floor3_location_layouts
            WHERE location_id IN (
                SELECT id FROM warehouse_locations
                WHERE location_code IN ('C1-R12','C1-R13')
            )
            """
        )
    )
    connection.execute(
        sa.text(
            """
            UPDATE warehouse_locations
            SET warehouse_floor=NULL, area_code=NULL, storage_type=NULL,
                is_temporary=0, source_version=NULL, sort_order=0
            WHERE location_code IN ('C1-R12','C1-R13')
            """
        )
    )
    original_height = 100 / 11
    for number in range(1, 12):
        connection.execute(
            sa.text(
                """
                UPDATE floor3_location_layouts
                SET left_pct=50, top_pct=:top_pct, width_pct=50,
                    height_pct=:height_pct, updated_at=CURRENT_TIMESTAMP
                WHERE location_id=(
                    SELECT id FROM warehouse_locations
                    WHERE location_code=:location_code
                )
                """
            ),
            {
                "location_code": f"C1-R{number:02d}",
                "top_pct": round((number - 1) * original_height, 4),
                "height_pct": round(
                    100 - (number - 1) * original_height, 4
                )
                if number == 11
                else round(original_height, 4),
            },
        )
    op.drop_index(
        "ix_warehouse_locations_placement_active",
        table_name="warehouse_locations",
    )
    with op.batch_alter_table("warehouse_locations") as batch:
        batch.drop_column("placement_status")
