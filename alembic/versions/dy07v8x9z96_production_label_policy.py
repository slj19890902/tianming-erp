"""add product production-label policy and frozen task snapshots

Revision ID: dy07v8x9z96
Revises: dx06v8x9z95
Create Date: 2026-08-10
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "dy07v8x9z96"
down_revision: Union[str, Sequence[str], None] = "dx06v8x9z95"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("products", recreate="always") as batch:
        batch.add_column(
            sa.Column(
                "production_label_enabled",
                sa.Boolean(),
                server_default=sa.false(),
                nullable=False,
            )
        )
        batch.add_column(
            sa.Column(
                "production_label_units_per_label",
                sa.Integer(),
                nullable=True,
            )
        )
        batch.create_check_constraint(
            "ck_products_production_label_policy",
            "((production_label_enabled = false "
            "AND production_label_units_per_label IS NULL) OR "
            "(production_label_enabled = true "
            "AND production_label_units_per_label > 0))",
        )

    with op.batch_alter_table("production_tasks", recreate="always") as batch:
        batch.add_column(
            sa.Column(
                "production_label_enabled_snapshot",
                sa.Boolean(),
                server_default=sa.false(),
                nullable=False,
            )
        )
        batch.add_column(
            sa.Column(
                "production_label_units_per_label_snapshot",
                sa.Integer(),
                nullable=True,
            )
        )
        batch.add_column(
            sa.Column(
                "production_label_total_quantity_snapshot",
                sa.Integer(),
                server_default="0",
                nullable=False,
            )
        )
        batch.add_column(
            sa.Column(
                "production_label_count_snapshot",
                sa.Integer(),
                server_default="0",
                nullable=False,
            )
        )
        batch.create_check_constraint(
            "ck_production_tasks_production_label_snapshot",
            "((production_label_enabled_snapshot = false "
            "AND production_label_units_per_label_snapshot IS NULL "
            "AND production_label_total_quantity_snapshot = 0 "
            "AND production_label_count_snapshot = 0) OR "
            "(production_label_enabled_snapshot = true "
            "AND production_label_units_per_label_snapshot > 0 "
            "AND production_label_total_quantity_snapshot > 0 "
            "AND production_label_count_snapshot > 0))",
        )


def downgrade() -> None:
    connection = op.get_bind()
    configured_products = connection.execute(
        sa.text(
            "SELECT COUNT(*) FROM products WHERE "
            "production_label_enabled IS TRUE OR "
            "production_label_units_per_label IS NOT NULL"
        )
    ).scalar_one()
    configured_tasks = connection.execute(
        sa.text(
            "SELECT COUNT(*) FROM production_tasks WHERE "
            "production_label_enabled_snapshot IS TRUE OR "
            "production_label_units_per_label_snapshot IS NOT NULL OR "
            "production_label_total_quantity_snapshot <> 0 OR "
            "production_label_count_snapshot <> 0"
        )
    ).scalar_one()
    if int(configured_products or 0) or int(configured_tasks or 0):
        raise RuntimeError(
            "已有常用箱包装标签策略或生产任务标签快照，禁止破坏性降级；"
            "请恢复 dy07v8x9z96 升级前完整备份。"
        )

    with op.batch_alter_table("production_tasks", recreate="always") as batch:
        batch.drop_constraint(
            "ck_production_tasks_production_label_snapshot",
            type_="check",
        )
        for column_name in (
            "production_label_count_snapshot",
            "production_label_total_quantity_snapshot",
            "production_label_units_per_label_snapshot",
            "production_label_enabled_snapshot",
        ):
            batch.drop_column(column_name)

    with op.batch_alter_table("products", recreate="always") as batch:
        batch.drop_constraint("ck_products_production_label_policy", type_="check")
        batch.drop_column("production_label_units_per_label")
        batch.drop_column("production_label_enabled")
