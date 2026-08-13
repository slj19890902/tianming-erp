"""add immutable ordered printing-colors snapshot to production tasks

Revision ID: kk19v8x9z08
Revises: jj18v8x9z07
Create Date: 2026-08-13
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "kk19v8x9z08"
down_revision = "jj18v8x9z07"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Nullable with no default is intentional: historical tasks did not freeze
    # ordered direct-print colours and must remain visibly unknown.
    op.add_column(
        "production_tasks",
        sa.Column("printing_colors_snapshot", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    connection = op.get_bind()
    snapshot_count = int(
        connection.execute(
            sa.text(
                "SELECT COUNT(*) FROM production_tasks "
                "WHERE printing_colors_snapshot IS NOT NULL"
            )
        ).scalar_one()
        or 0
    )
    if snapshot_count:
        raise RuntimeError(
            "P1-51C 已存在生产任务印刷颜色快照事实，拒绝破坏性降级；"
            "请恢复升级前完整备份。"
        )

    with op.batch_alter_table("production_tasks", recreate="always") as batch:
        batch.drop_column("printing_colors_snapshot")
