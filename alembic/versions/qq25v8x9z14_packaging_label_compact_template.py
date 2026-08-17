"""add compact packaging-label v2 and versioned visual layouts

Revision ID: qq25v8x9z14
Revises: pp24v8x9z13
Create Date: 2026-08-17
"""

from __future__ import annotations

from alembic import op
import json
import sqlalchemy as sa


revision = "qq25v8x9z14"
down_revision = "pp24v8x9z13"
branch_labels = None
depends_on = None

COMPACT_TEMPLATE = "current_40x30_v2"


def _replace_task_constraints(*, include_compact: bool) -> None:
    versions = (
        "('legacy_65x45_v1','current_40x30_v1','current_40x30_v2')"
        if include_compact
        else "('legacy_65x45_v1','current_40x30_v1')"
    )
    current_versions = (
        "('current_40x30_v1','current_40x30_v2')"
        if include_compact
        else "('current_40x30_v1')"
    )
    with op.batch_alter_table("production_tasks", recreate="always") as batch_op:
        batch_op.drop_constraint(
            "ck_production_tasks_label_template_version",
            type_="check",
        )
        batch_op.drop_constraint(
            "ck_production_tasks_label_product_version",
            type_="check",
        )
        batch_op.create_check_constraint(
            "ck_production_tasks_label_template_version",
            f"production_label_template_version_snapshot IN {versions}",
        )
        batch_op.create_check_constraint(
            "ck_production_tasks_label_product_version",
            "((production_label_template_version_snapshot = 'legacy_65x45_v1' "
            "AND (production_label_product_version_snapshot IS NULL OR "
            "production_label_product_version_snapshot >= 1)) OR "
            f"(production_label_template_version_snapshot IN {current_versions} "
            "AND production_label_product_version_snapshot IS NOT NULL "
            "AND production_label_product_version_snapshot >= 1))",
        )


def _replace_print_constraint(
    table: str,
    constraint: str,
    *,
    include_compact: bool,
) -> None:
    versions = (
        "('legacy_65x45_v1','current_40x30_v1','current_40x30_v2')"
        if include_compact
        else "('legacy_65x45_v1','current_40x30_v1')"
    )
    with op.batch_alter_table(table, recreate="always") as batch_op:
        batch_op.drop_constraint(constraint, type_="check")
        batch_op.create_check_constraint(
            constraint,
            f"template_version IN {versions}",
        )


def upgrade() -> None:
    with op.batch_alter_table("customers") as batch_op:
        batch_op.add_column(
            sa.Column("chinese_short_name", sa.String(length=30), nullable=True)
        )
    _replace_task_constraints(include_compact=True)
    _replace_print_constraint(
        "production_packaging_label_print_jobs",
        "ck_production_packaging_label_print_jobs_template",
        include_compact=True,
    )
    _replace_print_constraint(
        "production_packaging_label_print_job_tasks",
        "ck_production_packaging_label_print_job_tasks_template",
        include_compact=True,
    )
    op.create_table(
        "production_packaging_label_layout_revisions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("stream", sa.String(length=10), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("catalog_version", sa.String(length=40), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("base_release_version", sa.Integer(), nullable=False),
        sa.Column("operation_kind", sa.String(length=30), nullable=False),
        sa.Column("operation_key", sa.String(length=120), nullable=True),
        sa.Column("request_hash", sa.String(length=64), nullable=True),
        sa.Column("source_release_version", sa.Integer(), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "stream IN ('draft','release')",
            name="ck_production_packaging_label_layout_stream",
        ),
        sa.CheckConstraint(
            "version >= 1 AND base_release_version >= 0",
            name="ck_production_packaging_label_layout_versions",
        ),
        sa.CheckConstraint(
            "length(payload_hash) = 64 AND "
            "(request_hash IS NULL OR length(request_hash) = 64)",
            name="ck_production_packaging_label_layout_hashes",
        ),
        sa.CheckConstraint(
            "source_release_version IS NULL OR source_release_version >= 1",
            name="ck_production_packaging_label_layout_source_version",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "operation_key",
            name="uq_production_packaging_label_layout_operation_key",
        ),
        sa.UniqueConstraint(
            "stream",
            "version",
            name="uq_production_packaging_label_layout_stream_version",
        ),
    )
    op.create_index(
        "ix_production_packaging_label_layout_stream_version",
        "production_packaging_label_layout_revisions",
        ["stream", "version"],
        unique=False,
    )


def downgrade() -> None:
    bind = op.get_bind()
    customer_short_name_count = bind.execute(
        sa.text(
            "SELECT COUNT(*) FROM customers "
            "WHERE chinese_short_name IS NOT NULL "
            "AND trim(chinese_short_name) <> ''"
        )
    ).scalar_one()
    if int(customer_short_name_count or 0) > 0:
        raise RuntimeError(
            "P1-66A 客户中文简称事实已存在，拒绝降级抹除人工维护值"
        )
    customer_history_rows = bind.execute(
        sa.text(
            "SELECT snapshot_json FROM master_data_object_versions "
            "WHERE object_type = 'customer' "
            "AND snapshot_json LIKE '%\"chinese_short_name\"%'"
        )
    ).scalars()
    for raw_snapshot in customer_history_rows:
        try:
            snapshot = json.loads(str(raw_snapshot or ""))
        except (TypeError, ValueError) as error:
            raise RuntimeError(
                "P1-66A 客户版本快照损坏，拒绝降级"
            ) from error
        if str(snapshot.get("chinese_short_name") or "").strip():
            raise RuntimeError(
                "P1-66A 客户中文简称历史事实已存在，拒绝降级抹除人工维护值"
            )
    layout_count = bind.execute(
        sa.text(
            "SELECT COUNT(*) FROM production_packaging_label_layout_revisions"
        )
    ).scalar_one()
    if int(layout_count or 0) > 0:
        raise RuntimeError(
            "P1-66B 包装标签布局版本事实已存在，拒绝降级抹除布局历史"
        )
    protected_tables = (
        ("production_tasks", "production_label_template_version_snapshot"),
        ("production_packaging_label_print_jobs", "template_version"),
        ("production_packaging_label_print_job_tasks", "template_version"),
    )
    for table, column in protected_tables:
        count = bind.execute(
            sa.text(
                f"SELECT COUNT(*) FROM {table} WHERE {column} = :template_version"
            ),
            {"template_version": COMPACT_TEMPLATE},
        ).scalar_one()
        if int(count or 0) > 0:
            raise RuntimeError(
                "P1-66A 精简包装标签事实已存在，拒绝降级抹除模板版本"
            )

    _replace_print_constraint(
        "production_packaging_label_print_job_tasks",
        "ck_production_packaging_label_print_job_tasks_template",
        include_compact=False,
    )
    _replace_print_constraint(
        "production_packaging_label_print_jobs",
        "ck_production_packaging_label_print_jobs_template",
        include_compact=False,
    )
    _replace_task_constraints(include_compact=False)
    op.drop_index(
        "ix_production_packaging_label_layout_stream_version",
        table_name="production_packaging_label_layout_revisions",
    )
    op.drop_table("production_packaging_label_layout_revisions")
    with op.batch_alter_table("customers") as batch_op:
        batch_op.drop_column("chinese_short_name")
