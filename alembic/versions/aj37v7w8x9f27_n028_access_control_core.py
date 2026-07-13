"""Add N028 role permission overrides and customer scopes.

Revision ID: aj37v7w8x9f27
Revises: ai36v7w8x9e26
Create Date: 2026-07-13
"""

from alembic import op
import sqlalchemy as sa


revision = "aj37v7w8x9f27"
down_revision = "ai36v7w8x9e26"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # SQLite requires table recreation to replace the existing role CHECK.
    with op.batch_alter_table("users", recreate="always") as batch_op:
        batch_op.drop_constraint("ck_users_role_valid", type_="check")
        batch_op.add_column(
            sa.Column(
                "customer_access_mode",
                sa.String(length=20),
                server_default="all",
                nullable=False,
            )
        )
        batch_op.create_check_constraint(
            "ck_users_role_valid",
            "role IN ('admin', 'boss', 'finance', 'sales', 'workshop', "
            "'delivery_picker')",
        )
        batch_op.create_check_constraint(
            "ck_users_customer_access_mode_valid",
            "customer_access_mode IN ('all', 'selected')",
        )

    op.create_table(
        "user_permission_overrides",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("permission_code", sa.String(length=100), nullable=False),
        sa.Column("is_allowed", sa.Boolean(), nullable=False),
        sa.Column("granted_by", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.func.current_timestamp(),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["granted_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id",
            "permission_code",
            name="uq_user_permission_overrides_user_permission",
        ),
    )
    op.create_index(
        "ix_user_permission_overrides_user_id",
        "user_permission_overrides",
        ["user_id"],
        unique=False,
    )

    op.create_table(
        "user_customer_scopes",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("assigned_by", sa.Integer(), nullable=True),
        sa.Column(
            "assigned_at",
            sa.DateTime(),
            server_default=sa.func.current_timestamp(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["assigned_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id",
            "customer_id",
            name="uq_user_customer_scopes_user_customer",
        ),
    )
    op.create_index(
        "ix_user_customer_scopes_user_id",
        "user_customer_scopes",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        "ix_user_customer_scopes_customer_id",
        "user_customer_scopes",
        ["customer_id"],
        unique=False,
    )


def downgrade() -> None:
    boss_count = op.get_bind().execute(
        sa.text("SELECT COUNT(*) FROM users WHERE role = 'boss'")
    ).scalar_one()
    if boss_count:
        raise RuntimeError(
            "Cannot downgrade N028 while boss users exist; reassign those users first."
        )
    op.drop_index("ix_user_customer_scopes_customer_id", table_name="user_customer_scopes")
    op.drop_index("ix_user_customer_scopes_user_id", table_name="user_customer_scopes")
    op.drop_table("user_customer_scopes")
    op.drop_index(
        "ix_user_permission_overrides_user_id",
        table_name="user_permission_overrides",
    )
    op.drop_table("user_permission_overrides")

    with op.batch_alter_table("users", recreate="always") as batch_op:
        batch_op.drop_constraint("ck_users_customer_access_mode_valid", type_="check")
        batch_op.drop_constraint("ck_users_role_valid", type_="check")
        batch_op.drop_column("customer_access_mode")
        batch_op.create_check_constraint(
            "ck_users_role_valid",
            "role IN ('admin', 'finance', 'sales', 'workshop', 'delivery_picker')",
        )
