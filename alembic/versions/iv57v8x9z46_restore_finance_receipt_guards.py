"""restore SQLite finance receipt resolution-action guards

Revision ID: iv57v8x9z46
Revises: hu56v8x9z45
Create Date: 2026-08-31
"""

from __future__ import annotations

from alembic import op


revision = "iv57v8x9z46"
down_revision = "hu56v8x9z45"
branch_labels = None
depends_on = None


_INSERT_TRIGGER = "trg_finance_receipt_resolution_action_insert"
_UPDATE_TRIGGER = "trg_finance_receipt_resolution_action_update"


def _drop_guards() -> None:
    op.execute(f"DROP TRIGGER IF EXISTS {_UPDATE_TRIGGER}")
    op.execute(f"DROP TRIGGER IF EXISTS {_INSERT_TRIGGER}")


def _create_guards() -> None:
    op.execute(
        f"""
        CREATE TRIGGER {_INSERT_TRIGGER}
        BEFORE INSERT ON finance_return_receipt_items
        FOR EACH ROW
        WHEN NEW.resolution_action IS NOT NULL
         AND NEW.resolution_action NOT IN
             ('continue_delivery','accept_short','accept_over')
        BEGIN
            SELECT RAISE(ABORT, 'invalid finance receipt resolution_action');
        END
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER {_UPDATE_TRIGGER}
        BEFORE UPDATE OF resolution_action ON finance_return_receipt_items
        FOR EACH ROW
        WHEN NEW.resolution_action IS NOT NULL
         AND NEW.resolution_action NOT IN
             ('continue_delivery','accept_short','accept_over')
        BEGIN
            SELECT RAISE(ABORT, 'invalid finance receipt resolution_action');
        END
        """
    )


def upgrade() -> None:
    if op.get_bind().dialect.name != "sqlite":
        return
    # ht55 uses SQLite batch table recreation to add reconciliation fields.
    # SQLite drops table-bound triggers during that recreation, so restore the
    # original N005 database guard at a new linear revision for both fresh and
    # already-upgraded databases.
    _drop_guards()
    _create_guards()


def downgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        _drop_guards()
