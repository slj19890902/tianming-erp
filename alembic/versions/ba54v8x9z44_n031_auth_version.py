"""Add auth-version session revocation support.

Revision ID: ba54v8x9z44
Revises: az53v8x9z43
"""

from alembic import op
import sqlalchemy as sa
import os


revision = "ba54v8x9z44"
down_revision = "az53v8x9z43"
branch_labels = None
depends_on = None


# Dropping this column destroys the per-user session-revocation state.  This
# acknowledgement is deliberately operational rather than a secret: the
# migration never reads, validates, logs, or otherwise handles a session key.
DATA_LOSS_CONFIRMATION_ENV = "N031_AUTH_VERSION_DOWNGRADE_CONFIRM"
DATA_LOSS_CONFIRMATION_VALUE = "DOWNTIME_COMPLETE_AND_SESSION_SECRET_ROTATED"


def upgrade() -> None:
    # server_default backfills all existing rows and keeps the NOT NULL change
    # compatible with SQLite as well as deployed databases.
    with op.batch_alter_table("users", recreate="always") as batch_op:
        batch_op.add_column(
            sa.Column(
                "auth_version",
                sa.Integer(),
                nullable=False,
                server_default="1",
            )
        )


def downgrade() -> None:
    # On a populated database this would discard the revocation state and let
    # pre-N031 application code resume accepting those signed sessions.  That
    # rollback is unsafe, so fail closed.  An empty users table has no valid
    # sessions and remains reversibly downgradeable for disposable databases.
    user_count = op.get_bind().execute(sa.text("SELECT COUNT(*) FROM users")).scalar_one()
    confirmation = os.environ.get(DATA_LOSS_CONFIRMATION_ENV)
    if user_count and confirmation != DATA_LOSS_CONFIRMATION_VALUE:
        raise RuntimeError(
            "Refusing to drop users.auth_version from a populated database: "
            "it would discard session-revocation state. Only after the "
            "application has been stopped and its session-signing secret has "
            "been rotated, set "
            f"{DATA_LOSS_CONFIRMATION_ENV}={DATA_LOSS_CONFIRMATION_VALUE} "
            "for this one downgrade command."
        )
    with op.batch_alter_table("users", recreate="always") as batch_op:
        batch_op.drop_column("auth_version")
