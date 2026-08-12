"""add mold archive and restore lifecycle

Revision ID: gg15v8x9z04
Revises: ff14v8x9z03
Create Date: 2026-08-12
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "gg15v8x9z04"
down_revision = "ff14v8x9z03"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("mold_tools", recreate="always") as batch:
        batch.add_column(
            sa.Column(
                "archive_status",
                sa.String(20),
                nullable=False,
                server_default="active",
            )
        )
        batch.add_column(sa.Column("archived_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("archived_by", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("archive_reason", sa.String(50), nullable=True))
        batch.add_column(
            sa.Column("pre_archive_location", sa.String(250), nullable=True)
        )
        batch.add_column(sa.Column("restored_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("restored_by", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_mold_tools_archived_by_users",
            "users",
            ["archived_by"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.create_foreign_key(
            "fk_mold_tools_restored_by_users",
            "users",
            ["restored_by"],
            ["id"],
            ondelete="SET NULL",
        )
        batch.create_check_constraint(
            "ck_mold_tools_archive_status",
            "archive_status IN ('active', 'archived')",
        )
        batch.create_check_constraint(
            "ck_mold_tools_archive_state",
            "((archive_status = 'active' AND archived_at IS NULL "
            "AND archived_by IS NULL AND archive_reason IS NULL "
            "AND pre_archive_location IS NULL) OR "
            "(archive_status = 'archived' AND is_active = false "
            "AND archived_at IS NOT NULL AND archived_by IS NOT NULL "
            "AND archive_reason IS NOT NULL AND pre_archive_location IS NOT NULL))",
        )
        batch.create_index(
            "ix_mold_tools_archive_status",
            ["archive_status", "rack_location"],
        )


def downgrade() -> None:
    connection = op.get_bind()
    archived = int(
        connection.execute(
            sa.text(
                "SELECT COUNT(*) FROM mold_tools WHERE archive_status <> 'active' "
                "OR archived_at IS NOT NULL OR archived_by IS NOT NULL "
                "OR archive_reason IS NOT NULL OR pre_archive_location IS NOT NULL "
                "OR restored_at IS NOT NULL OR restored_by IS NOT NULL"
            )
        ).scalar_one()
        or 0
    )
    movements = int(
        connection.execute(
            sa.text(
                "SELECT COUNT(*) FROM mold_location_movements "
                "WHERE source IN ('archive','restore')"
            )
        ).scalar_one()
        or 0
    )
    if archived or movements:
        raise RuntimeError(
            "P1-44B 已存在模具封存、恢复或位置移动事实，拒绝破坏性降级；"
            "请恢复升级前完整备份。"
        )
    with op.batch_alter_table("mold_tools", recreate="always") as batch:
        batch.drop_index("ix_mold_tools_archive_status")
        batch.drop_constraint("ck_mold_tools_archive_state", type_="check")
        batch.drop_constraint("ck_mold_tools_archive_status", type_="check")
        batch.drop_constraint("fk_mold_tools_restored_by_users", type_="foreignkey")
        batch.drop_constraint("fk_mold_tools_archived_by_users", type_="foreignkey")
        for column_name in (
            "restored_by",
            "restored_at",
            "pre_archive_location",
            "archive_reason",
            "archived_by",
            "archived_at",
            "archive_status",
        ):
            batch.drop_column(column_name)
