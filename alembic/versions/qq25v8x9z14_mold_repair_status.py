"""add independent mold repair status and immutable events

Revision ID: qq25v8x9z14
Revises: pp24v8x9z13
Create Date: 2026-08-17
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "qq25v8x9z14"
down_revision = "pp24v8x9z13"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("mold_tools") as batch:
        batch.add_column(sa.Column("repair_status", sa.String(length=20), server_default="normal", nullable=False))
        batch.add_column(sa.Column("repair_version", sa.Integer(), server_default="1", nullable=False))
        batch.create_check_constraint("ck_mold_tools_repair_status", "repair_status IN ('normal', 'needs_repair')")
        batch.create_check_constraint("ck_mold_tools_repair_version", "repair_version >= 1")
    op.create_table(
        "mold_repair_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("mold_tool_id", sa.Integer(), nullable=False),
        sa.Column("mold_code_snapshot", sa.String(length=100), nullable=False),
        sa.Column("before_status", sa.String(length=20), nullable=False),
        sa.Column("after_status", sa.String(length=20), nullable=False),
        sa.Column("actor_id", sa.Integer(), nullable=True),
        sa.Column("actor_username_snapshot", sa.String(length=100), nullable=False),
        sa.Column("occurred_at", sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=120), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("expected_version", sa.Integer(), nullable=False),
        sa.Column("resulting_version", sa.Integer(), nullable=False),
        sa.CheckConstraint("before_status IN ('normal', 'needs_repair')", name="ck_mold_repair_events_before_status"),
        sa.CheckConstraint("after_status IN ('normal', 'needs_repair')", name="ck_mold_repair_events_after_status"),
        sa.CheckConstraint("before_status <> after_status", name="ck_mold_repair_events_actual_change"),
        sa.CheckConstraint("expected_version >= 1", name="ck_mold_repair_events_expected_version"),
        sa.CheckConstraint("resulting_version = expected_version + 1", name="ck_mold_repair_events_resulting_version"),
        sa.CheckConstraint("length(request_hash) = 64", name="ck_mold_repair_events_request_hash"),
        sa.ForeignKeyConstraint(["mold_tool_id"], ["mold_tools.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key", name="uq_mold_repair_events_idempotency_key"),
    )
    op.create_index("ix_mold_repair_events_mold_time", "mold_repair_events", ["mold_tool_id", "occurred_at", "id"])
    if op.get_bind().dialect.name == "sqlite":
        op.execute(
            sa.text(
                "CREATE TRIGGER trg_mold_repair_events_immutable_update "
                "BEFORE UPDATE ON mold_repair_events BEGIN "
                "SELECT RAISE(ABORT, 'mold_repair_events rows are immutable'); END"
            )
        )
        op.execute(
            sa.text(
                "CREATE TRIGGER trg_mold_repair_events_immutable_delete "
                "BEFORE DELETE ON mold_repair_events BEGIN "
                "SELECT RAISE(ABORT, 'mold_repair_events rows are immutable'); END"
            )
        )


def downgrade() -> None:
    connection = op.get_bind()
    event_count = int(connection.execute(sa.text("SELECT COUNT(*) FROM mold_repair_events")).scalar_one())
    changed_count = int(connection.execute(sa.text("SELECT COUNT(*) FROM mold_tools WHERE repair_status <> 'normal' OR repair_version <> 1")).scalar_one())
    if event_count or changed_count:
        raise RuntimeError("存在模具维修状态或维修流水，禁止降级以免丢失正式事实")
    if connection.dialect.name == "sqlite":
        op.execute(sa.text("DROP TRIGGER IF EXISTS trg_mold_repair_events_immutable_update"))
        op.execute(sa.text("DROP TRIGGER IF EXISTS trg_mold_repair_events_immutable_delete"))
    op.drop_index("ix_mold_repair_events_mold_time", table_name="mold_repair_events")
    op.drop_table("mold_repair_events")
    with op.batch_alter_table("mold_tools") as batch:
        batch.drop_constraint("ck_mold_tools_repair_version", type_="check")
        batch.drop_constraint("ck_mold_tools_repair_status", type_="check")
        batch.drop_column("repair_version")
        batch.drop_column("repair_status")
