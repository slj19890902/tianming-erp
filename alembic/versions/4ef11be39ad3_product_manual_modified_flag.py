"""Add products.manual_modified / manual_modified_at for 常用箱编辑 status display.

Revision ID: 4ef11be39ad3
Revises: c20u7v8w9x19
"""

from alembic import op
import sqlalchemy as sa


revision = "4ef11be39ad3"
down_revision = "c20u7v8w9x19"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "products",
        sa.Column("manual_modified", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "products",
        sa.Column("manual_modified_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("products", "manual_modified_at")
    op.drop_column("products", "manual_modified")
