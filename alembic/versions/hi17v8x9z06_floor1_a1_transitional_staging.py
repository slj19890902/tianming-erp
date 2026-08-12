"""allow actual replenishment receipts into transitional floor-one A1 staging

Revision ID: hi17v8x9z06
Revises: hh16v8x9z05
Create Date: 2026-08-12
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "hi17v8x9z06"
down_revision = "hh16v8x9z05"
branch_labels = None
depends_on = None


INSERT_TRIGGER = "trg_inventory_lots_require_placed_location_insert"
UPDATE_TRIGGER = "trg_inventory_lots_require_placed_location_update"
DOWNGRADE_BLOCKED_MESSAGE = (
    "一楼 A1 过渡原料暂存标记 1FA 已承载有效库存，禁止恢复不识别该落点的旧规则；"
    "请先完成库存迁位或恢复升级前完整备份。"
)


def _drop_sqlite_lot_guards() -> None:
    op.execute(f"DROP TRIGGER IF EXISTS {UPDATE_TRIGGER}")
    op.execute(f"DROP TRIGGER IF EXISTS {INSERT_TRIGGER}")


def _accepted_location_sql(*, location_reference: str) -> str:
    return f"""
        SELECT 1
        FROM warehouse_locations location
        WHERE location.id = {location_reference}
          AND location.is_active = 1
          AND (
            location.placement_status = 'placed'
            OR (
              location.placement_status = 'unplaced'
              AND UPPER(TRIM(location.location_code)) = '1FA'
              AND location.warehouse_type IN ('semi_finished', 'shared')
              AND location.warehouse_floor = 1
              AND UPPER(TRIM(COALESCE(location.area_code, ''))) = 'A1'
              AND location.storage_type IN ('ground', 'temporary_aisle')
              AND COALESCE(location.source_version, '') <> 'V11'
              AND NEW.inventory_type = 'semi_finished'
              AND NEW.source_type = 'replenishment'
              AND NEW.source_ref_type = 'stock_replenishment_receipt'
              AND NEW.source_ref_id IS NOT NULL
              AND EXISTS (
                SELECT 1
                FROM incoming_receipt_items receipt_item
                WHERE receipt_item.id = NEW.source_ref_id
                  AND receipt_item.status = 'posted'
                  AND receipt_item.stock_replenishment_item_id IS NOT NULL
              )
              AND EXISTS (
                SELECT 1
                FROM warehouse_floors floor
                JOIN warehouse_areas area ON area.floor_id = floor.id
                WHERE floor.floor_number = 1
                  AND floor.construction_status = 'enabled'
                  AND UPPER(TRIM(area.area_code)) = 'A1'
                  AND area.construction_status IN ('ledger_building', 'enabled')
              )
            )
          )
    """


def _create_sqlite_lot_guards(*, allow_transitional_staging: bool) -> None:
    if allow_transitional_staging:
        accepted_insert = _accepted_location_sql(
            location_reference="NEW.warehouse_location_id"
        )
        accepted_update = _accepted_location_sql(
            location_reference="NEW.warehouse_location_id"
        )
    else:
        accepted_insert = """
            SELECT 1 FROM warehouse_locations location
            WHERE location.id = NEW.warehouse_location_id
              AND location.is_active = 1
              AND location.placement_status = 'placed'
        """
        accepted_update = accepted_insert

    op.execute(
        f"""
        CREATE TRIGGER {INSERT_TRIGGER}
        BEFORE INSERT ON inventory_lots
        FOR EACH ROW
        WHEN NEW.status IN ('active','frozen')
         AND NOT EXISTS ({accepted_insert})
        BEGIN
            SELECT RAISE(ABORT, 'inventory lot requires an active placed location');
        END
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER {UPDATE_TRIGGER}
        BEFORE UPDATE OF warehouse_location_id, status ON inventory_lots
        FOR EACH ROW
        WHEN NEW.status IN ('active','frozen')
         AND NOT EXISTS ({accepted_update})
        BEGIN
            SELECT RAISE(ABORT, 'inventory lot requires an active placed location');
        END
        """
    )


def _transitional_active_lot_count(connection: sa.Connection) -> int:
    return int(
        connection.execute(
            sa.text(
                """
                SELECT COUNT(*)
                FROM inventory_lots lot
                JOIN warehouse_locations location
                  ON location.id = lot.warehouse_location_id
                WHERE lot.status IN ('active', 'frozen')
                  AND location.placement_status = 'unplaced'
                  AND UPPER(TRIM(location.location_code)) = '1FA'
                """
            )
        ).scalar_one()
        or 0
    )


def upgrade() -> None:
    connection = op.get_bind()
    if connection.dialect.name != "sqlite":
        return
    _drop_sqlite_lot_guards()
    _create_sqlite_lot_guards(allow_transitional_staging=True)


def downgrade() -> None:
    connection = op.get_bind()
    if connection.dialect.name != "sqlite":
        return
    if _transitional_active_lot_count(connection):
        raise RuntimeError(DOWNGRADE_BLOCKED_MESSAGE)
    _drop_sqlite_lot_guards()
    _create_sqlite_lot_guards(allow_transitional_staging=False)
