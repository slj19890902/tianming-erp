"""Add the P5 user interface mode preference.

Revision ID: az53v8x9z43
Revises: ay52v8x9z42
"""

from alembic import op
import sqlalchemy as sa


revision = "az53v8x9z43"
down_revision = "ay52v8x9z42"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("users", recreate="always") as batch_op:
        batch_op.add_column(
            sa.Column(
                "ui_mode",
                sa.String(length=20),
                server_default="standard",
                nullable=False,
            )
        )
        batch_op.create_check_constraint(
            "ck_users_ui_mode_valid",
            "ui_mode IN ('standard', 'large')",
        )

    op.execute(
        sa.text(
            "UPDATE users SET ui_mode = CASE "
            "WHEN role = 'boss' THEN 'large' ELSE 'standard' END"
        )
    )


def downgrade() -> None:
    with op.batch_alter_table("users", recreate="always") as batch_op:
        batch_op.drop_constraint("ck_users_ui_mode_valid", type_="check")
        batch_op.drop_column("ui_mode")
