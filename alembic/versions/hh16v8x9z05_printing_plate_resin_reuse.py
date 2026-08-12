"""add immutable printing plate resin reuse history

Revision ID: hh16v8x9z05
Revises: gg15v8x9z04
Create Date: 2026-08-12
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "hh16v8x9z05"
down_revision = "gg15v8x9z04"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "printing_plate_resin_reuses",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("printing_plate_id", sa.Integer(), nullable=False),
        sa.Column("plate_code_snapshot", sa.String(30), nullable=False),
        sa.Column("from_customer_id", sa.Integer(), nullable=False),
        sa.Column("from_customer_name_snapshot", sa.String(200), nullable=False),
        sa.Column("from_plate_name_snapshot", sa.String(200), nullable=False),
        sa.Column("from_color_name_snapshot", sa.String(100), nullable=False),
        sa.Column("to_customer_id", sa.Integer(), nullable=False),
        sa.Column("to_customer_name_snapshot", sa.String(200), nullable=False),
        sa.Column("to_plate_name_snapshot", sa.String(200), nullable=False),
        sa.Column("to_color_name_snapshot", sa.String(100), nullable=False),
        sa.Column("rack_location_snapshot", sa.String(100), nullable=False),
        sa.Column("actor_id", sa.Integer(), nullable=True),
        sa.Column("actor_username_snapshot", sa.String(100), nullable=False),
        sa.Column(
            "reused_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.String(120), nullable=False),
        sa.Column("expected_version", sa.Integer(), nullable=False),
        sa.Column("resulting_version", sa.Integer(), nullable=False),
        sa.Column(
            "old_resin_removed", sa.Boolean(), server_default=sa.true(), nullable=False
        ),
        sa.Column(
            "new_resin_mounted", sa.Boolean(), server_default=sa.true(), nullable=False
        ),
        sa.CheckConstraint(
            "expected_version >= 1",
            name="ck_printing_plate_resin_reuses_expected_version",
        ),
        sa.CheckConstraint(
            "resulting_version = expected_version + 1",
            name="ck_printing_plate_resin_reuses_resulting_version",
        ),
        sa.CheckConstraint(
            "old_resin_removed = true AND new_resin_mounted = true",
            name="ck_printing_plate_resin_reuses_physical_confirmations",
        ),
        sa.ForeignKeyConstraint(
            ["printing_plate_id"], ["printing_plates.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["from_customer_id"], ["customers.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["to_customer_id"], ["customers.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_printing_plate_resin_reuses_idempotency",
        ),
    )
    op.create_index(
        "ix_printing_plate_resin_reuses_plate_time",
        "printing_plate_resin_reuses",
        ["printing_plate_id", "reused_at"],
    )


def downgrade() -> None:
    connection = op.get_bind()
    facts = int(
        connection.execute(
            sa.text("SELECT COUNT(*) FROM printing_plate_resin_reuses")
        ).scalar_one()
        or 0
    )
    if facts:
        raise RuntimeError(
            "P1-44C 已存在挂板换版复用事实，拒绝破坏性降级；"
            "请恢复升级前完整备份。"
        )
    op.drop_index(
        "ix_printing_plate_resin_reuses_plate_time",
        table_name="printing_plate_resin_reuses",
    )
    op.drop_table("printing_plate_resin_reuses")
