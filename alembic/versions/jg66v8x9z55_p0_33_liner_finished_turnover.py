"""activate existing F34/F12 liner turnover anchors

Revision ID: jg66v8x9z55
Revises: jf65v8x9z54
Create Date: 2026-09-01
"""

from alembic import op

from app.services.liner_finished_turnover import (
    downgrade_liner_finished_turnover,
    upgrade_liner_finished_turnover,
)


revision = "jg66v8x9z55"
down_revision = "jf65v8x9z54"
branch_labels = None
depends_on = None


def upgrade() -> None:
    upgrade_liner_finished_turnover(op.get_bind())


def downgrade() -> None:
    downgrade_liner_finished_turnover(op.get_bind())
