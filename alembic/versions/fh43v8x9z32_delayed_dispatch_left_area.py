"""publish the third-floor left delayed-dispatch turnover area

Revision ID: fh43v8x9z32
Revises: fg42v8x9z31
Create Date: 2026-08-27
"""

from __future__ import annotations

from alembic import op

from app.services.warehouse_delayed_dispatch_map import (
    downgrade_delayed_dispatch_map,
    upgrade_delayed_dispatch_map,
)


revision = "fh43v8x9z32"
down_revision = "fg42v8x9z31"
branch_labels = None
depends_on = None


def upgrade() -> None:
    upgrade_delayed_dispatch_map(op.get_bind())


def downgrade() -> None:
    downgrade_delayed_dispatch_map(op.get_bind())
