"""freeze mold label paper template version

Revision ID: ss27v8x9z16
Revises: rr26v8x9z15
Create Date: 2026-08-17
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "ss27v8x9z16"
down_revision = "rr26v8x9z15"
branch_labels = None
depends_on = None


JOBS = "mold_label_print_jobs"
LEGACY_TEMPLATE = "mold_40x30_v1"
CURRENT_TEMPLATE = "mold_80x40_v1"


def upgrade() -> None:
    op.add_column(
        JOBS,
        sa.Column(
            "template_version",
            sa.String(30),
            sa.CheckConstraint(
                "template_version IN ('mold_40x30_v1','mold_80x40_v1')",
                name="ck_mold_label_print_jobs_template_version",
            ),
            nullable=False,
            server_default=LEGACY_TEMPLATE,
        ),
    )


def downgrade() -> None:
    current_count = int(
        op.get_bind()
        .execute(
            sa.text(
                f"SELECT COUNT(*) FROM {JOBS} "
                "WHERE template_version <> :legacy_template"
            ),
            {"legacy_template": LEGACY_TEMPLATE},
        )
        .scalar_one()
        or 0
    )
    if current_count:
        raise RuntimeError(
            "P1-62 已存在 40×80 模具标签打印事实，拒绝删除模板版本；"
            "请恢复升级前完整备份。"
        )
    op.drop_column(JOBS, "template_version")
