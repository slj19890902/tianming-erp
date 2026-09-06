"""Allow the single explicit recount-pending identity without claiming placement.

Revision ID: rp06v8x9z65
Revises: kq75v8x9z64
Create Date: 2026-09-06

No inventory rows are changed. Ordinary unplaced locations remain prohibited.
"""
from alembic import op
import sqlalchemy as sa

revision = "rp06v8x9z65"
down_revision = "kq75v8x9z64"
branch_labels = None
depends_on = None


def _pending(alias):
    return f"""{alias}.location_code = 'RECOUNT-PENDING'
        AND {alias}.source_version = 'RECOUNT_PENDING'
        AND {alias}.is_active = 1 AND {alias}.is_temporary = 1
        AND {alias}.placement_status = 'unplaced'
        AND {alias}.warehouse_type = 'shared'
        AND {alias}.warehouse_floor IS NULL AND {alias}.storage_type IS NULL
        AND {alias}.area_code IS NULL AND {alias}.address_kind = 'legacy'
        AND {alias}.address_area_id IS NULL AND {alias}.map_rack_id IS NULL
        AND {alias}.rack_code IS NULL AND {alias}.level_no IS NULL
        AND {alias}.ground_row_no IS NULL AND {alias}.slot_no IS NULL"""


def _replace_guards(allow_pending):
    for table, column, active, update_columns, label in (
        ("inventory_lots", "warehouse_location_id",
         "NEW.status IN ('active','frozen') AND "
         "(NEW.quantity_available + NEW.quantity_reserved + NEW.quantity_damaged) > 0",
         "warehouse_location_id, status, quantity_available, quantity_reserved, quantity_damaged",
         "inventory lot"),
        ("inventory_pallets", "location_id", "NEW.is_current = 1",
         "location_id, is_current", "current pallet"),
    ):
        allowed = "location.placement_status = 'placed'"
        if allow_pending:
            allowed = f"({allowed} OR ({_pending('location')}))"
        for suffix, event in (("insert", "INSERT"), ("update", f"UPDATE OF {update_columns}")):
            name = f"trg_{table}_require_placed_location_{suffix}"
            op.execute(f"DROP TRIGGER IF EXISTS {name}")
            op.execute(f"""CREATE TRIGGER {name}
                BEFORE {event} ON {table} FOR EACH ROW
                WHEN {active} AND NOT EXISTS (
                    SELECT 1 FROM warehouse_locations location
                    WHERE location.id = NEW.{column} AND location.is_active = 1 AND {allowed}
                ) BEGIN
                    SELECT RAISE(ABORT, '{label} requires an active placed location');
                END""")


def _require_sqlite():
    if op.get_bind().dialect.name != "sqlite":
        raise RuntimeError("Recount pending guards have only been validated for SQLite")


def upgrade():
    _require_sqlite()
    # Refuse collisions before touching any guard; do not repurpose existing data.
    if op.get_bind().execute(sa.text(f"""SELECT 1 FROM warehouse_locations location
            WHERE (location_code = 'RECOUNT-PENDING' OR source_version = 'RECOUNT_PENDING')
            AND COALESCE(({_pending('location')}), 0) <> 1 LIMIT 1""")).first():
        raise RuntimeError("Reserved recount identity already exists; review before upgrade")
    _replace_guards(True)
    for suffix, event, old_guard in (
        ("insert", "INSERT", ""),
        ("update", "UPDATE", "OLD.location_code = 'RECOUNT-PENDING' OR OLD.source_version = 'RECOUNT_PENDING' OR "),
    ):
        op.execute(f"""CREATE TRIGGER trg_recount_pending_identity_{suffix}
            BEFORE {event} ON warehouse_locations FOR EACH ROW
            WHEN ({old_guard}NEW.location_code = 'RECOUNT-PENDING' OR NEW.source_version = 'RECOUNT_PENDING')
                AND COALESCE(({_pending('NEW')}), 0) <> 1
            BEGIN SELECT RAISE(ABORT, 'recount pending identity cannot represent a physical location'); END""")


def downgrade():
    _require_sqlite()
    pending = "SELECT id FROM warehouse_locations WHERE location_code = 'RECOUNT-PENDING' OR source_version = 'RECOUNT_PENDING'"
    if op.get_bind().execute(sa.text(f"""SELECT 1 FROM inventory_lots
            WHERE warehouse_location_id IN ({pending}) AND status IN ('active','frozen')
                AND quantity_available + quantity_reserved + quantity_damaged > 0
            UNION ALL SELECT 1 FROM inventory_pallets
            WHERE location_id IN ({pending}) AND is_current = 1 LIMIT 1""")).first():
        raise RuntimeError("Place or restore pending inventory before downgrade; no data was changed")
    for suffix in ("insert", "update"):
        op.execute(f"DROP TRIGGER trg_recount_pending_identity_{suffix}")
    _replace_guards(False)
