"""allow guarded delivery-cancel ground occupancy restoration

Revision ID: jm71v8x9z60
Revises: jl70v8x9z59
Create Date: 2026-09-02
"""

from __future__ import annotations

from alembic import op


revision = "jm71v8x9z60"
down_revision = "jl70v8x9z59"
branch_labels = None
depends_on = None


OCCUPANCY_TRIGGER = "trg_ground_occupancies_transition_guard"
SLOT_TRIGGER = "trg_ground_occupancy_slots_transition_guard"


def _occupancy_guard_sql(*, allow_restore: bool) -> str:
    restore = """
                  OR (OLD.status = 'released' AND NEW.status = 'active'
                      AND NEW.version = OLD.version + 1
                      AND NEW.released_by IS NULL AND NEW.released_at IS NULL)
    """ if allow_restore else ""
    return f"""
        CREATE TRIGGER {OCCUPANCY_TRIGGER}
        BEFORE UPDATE ON warehouse_ground_occupancies
        WHEN NEW.pallet_id <> OLD.pallet_id
          OR NEW.primary_location_id <> OLD.primary_location_id
          OR NEW.customer_id <> OLD.customer_id
          OR NEW.product_id <> OLD.product_id
          OR NEW.footprint_kind <> OLD.footprint_kind
          OR NEW.capacity_quantity <> OLD.capacity_quantity
          OR NOT (
              (OLD.status = 'active' AND NEW.status = 'released'
               AND NEW.version = OLD.version + 1
               AND NEW.released_by IS NOT NULL AND NEW.released_at IS NOT NULL)
              {restore}
          )
        BEGIN
          SELECT RAISE(ABORT, 'invalid ground occupancy transition');
        END
    """


def _slot_guard_sql(*, allow_restore: bool) -> str:
    restore = """
                  OR (OLD.status = 'released' AND NEW.status = 'active'
                      AND NEW.released_at IS NULL)
    """ if allow_restore else ""
    return f"""
        CREATE TRIGGER {SLOT_TRIGGER}
        BEFORE UPDATE ON warehouse_ground_occupancy_slots
        WHEN NEW.occupancy_id <> OLD.occupancy_id
          OR NEW.location_id <> OLD.location_id
          OR NEW.slot_sequence <> OLD.slot_sequence
          OR NOT (
              (OLD.status = 'active' AND NEW.status = 'released'
               AND NEW.released_at IS NOT NULL)
              {restore}
          )
        BEGIN
          SELECT RAISE(ABORT, 'invalid ground occupancy slot transition');
        END
    """


def _replace_transition_guards(*, allow_restore: bool) -> None:
    op.execute(f"DROP TRIGGER IF EXISTS {OCCUPANCY_TRIGGER}")
    op.execute(f"DROP TRIGGER IF EXISTS {SLOT_TRIGGER}")
    op.execute(_occupancy_guard_sql(allow_restore=allow_restore))
    op.execute(_slot_guard_sql(allow_restore=allow_restore))


def upgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        _replace_transition_guards(allow_restore=True)


def downgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        _replace_transition_guards(allow_restore=False)
