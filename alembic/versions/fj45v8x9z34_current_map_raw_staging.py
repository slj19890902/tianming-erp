"""normalize the current-map raw-material receiving rack

Revision ID: fj45v8x9z34
Revises: fi44v8x9z33
Create Date: 2026-08-27
"""

from alembic import op

from app.services.current_map_raw_staging import (
    downgrade_current_map_raw_staging,
    upgrade_current_map_raw_staging,
)


revision = "fj45v8x9z34"
down_revision = "fi44v8x9z33"
branch_labels = None
depends_on = None


def upgrade() -> None:
    upgrade_current_map_raw_staging(op.get_bind())


def downgrade() -> None:
    downgrade_current_map_raw_staging(op.get_bind())
