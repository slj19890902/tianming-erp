"""Safely baseline the SQLAlchemy users table.

Revision ID: d0607640f8ea
Revises: 
Create Date: 2026-06-13

Existing databases already contain a legacy users table. This revision leaves
that table and every historical business table untouched. The explicit legacy
role conversion will run only after the old login path has been replaced.

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd0607640f8ea'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create users only for a fresh database; never replace existing tables."""
    bind = op.get_bind()
    if "users" in sa.inspect(bind).get_table_names():
        return

    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("username", sa.String(length=50), nullable=False),
        sa.Column("password_hash", sa.String(length=300), nullable=False),
        sa.Column("role", sa.String(length=30), nullable=False),
        sa.Column("real_name", sa.String(length=100), nullable=False),
        sa.Column("display_name", sa.String(length=100), nullable=True),
        sa.Column(
            "is_active",
            sa.Boolean(),
            server_default=sa.text("1"),
            nullable=False,
        ),
        sa.Column(
            "must_change_password",
            sa.Boolean(),
            server_default=sa.text("1"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "role IN ('admin', 'finance', 'sales', 'workshop')",
            name="ck_users_role_valid",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_users_role", "users", ["role"], unique=False)
    op.create_index("ix_users_username", "users", ["username"], unique=True)


def downgrade() -> None:
    """The initial baseline is deliberately non-destructive."""
    return
