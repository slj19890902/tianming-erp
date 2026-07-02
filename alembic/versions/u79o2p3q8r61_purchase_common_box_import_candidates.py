"""Restore the missing common-box migration chain marker.

Revision ID: u79o2p3q8r61
Revises: t68n0r1s7u50

The production schema already contains the common-box import tables. This
marker restores the revision graph that v84p1q2r3s72 depends on.
"""

revision = "u79o2p3q8r61"
down_revision = "t68n0r1s7u50"
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
