"""normalize the audited legacy pallet item into a traceable finished lot

Revision ID: fi44v8x9z33
Revises: fh43v8x9z32
Create Date: 2026-08-27
"""

from alembic import op

from app.services.legacy_pallet_item_lot_normalization import (
    downgrade_legacy_pallet_item_lot,
    upgrade_legacy_pallet_item_lot,
)


revision = "fi44v8x9z33"
down_revision = "fh43v8x9z32"
branch_labels = None
depends_on = None


def upgrade() -> None:
    upgrade_legacy_pallet_item_lot(op.get_bind())


def downgrade() -> None:
    downgrade_legacy_pallet_item_lot(op.get_bind())
