"""assign delivery pick tasks to one picker

Revision ID: dd86v8x9z75
Revises: dc85v8x9z74
Create Date: 2026-08-02
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "dd86v8x9z75"
down_revision: str | None = "dc85v8x9z74"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("delivery_pick_tasks") as batch_op:
        batch_op.add_column(sa.Column("assigned_to", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_delivery_pick_tasks_assigned_to_users",
            "users",
            ["assigned_to"],
            ["id"],
            ondelete="SET NULL",
        )
    op.create_index(
        "ix_delivery_pick_tasks_assigned_to_status",
        "delivery_pick_tasks",
        ["assigned_to", "status"],
        unique=False,
    )


def downgrade() -> None:
    connection = op.get_bind()
    assigned = connection.execute(
        sa.text("SELECT 1 FROM delivery_pick_tasks WHERE assigned_to IS NOT NULL LIMIT 1")
    ).first()
    if assigned is not None:
        raise RuntimeError(
            "存在已分配送货员的拿货任务，禁止破坏性降级；请保留当前数据库并恢复迁移前完整备份。"
        )
    op.drop_index(
        "ix_delivery_pick_tasks_assigned_to_status",
        table_name="delivery_pick_tasks",
    )
    with op.batch_alter_table("delivery_pick_tasks") as batch_op:
        batch_op.drop_constraint(
            "fk_delivery_pick_tasks_assigned_to_users",
            type_="foreignkey",
        )
        batch_op.drop_column("assigned_to")
