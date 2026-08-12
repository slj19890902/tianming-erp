"""freeze 40x30 production label plans and durable print jobs

Revision ID: jj18v8x9z07
Revises: ii17v8x9z06
Create Date: 2026-08-13
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "jj18v8x9z07"
down_revision = "ii17v8x9z06"
branch_labels = None
depends_on = None


LEGACY_TEMPLATE_VERSION = "legacy_65x45_v1"
CURRENT_TEMPLATE_VERSION = "current_40x30_v1"


def upgrade() -> None:
    # The legacy value is historical metadata only.  It deliberately does not
    # assert that an old task was physically printed.
    op.add_column(
        "production_tasks",
        sa.Column(
            "production_label_template_version_snapshot",
            sa.String(length=30),
            nullable=False,
            server_default=LEGACY_TEMPLATE_VERSION,
        ),
    )
    op.add_column(
        "production_tasks",
        sa.Column(
            "production_label_product_version_snapshot",
            sa.Integer(),
            nullable=True,
        ),
    )
    with op.batch_alter_table("production_tasks", recreate="always") as batch:
        batch.alter_column(
            "production_label_template_version_snapshot",
            existing_type=sa.String(length=30),
            nullable=False,
            # Only the two authoritative task-creation services may opt into
            # the current template together with a frozen product version.
            # Legacy/direct writers stay visibly legacy instead of producing
            # a false current snapshot with no product-version provenance.
            server_default=LEGACY_TEMPLATE_VERSION,
        )
        batch.create_check_constraint(
            "ck_production_tasks_label_template_version",
            "production_label_template_version_snapshot IN "
            "('legacy_65x45_v1','current_40x30_v1')",
        )
        batch.create_check_constraint(
            "ck_production_tasks_label_product_version",
            "((production_label_template_version_snapshot = 'legacy_65x45_v1' "
            "AND (production_label_product_version_snapshot IS NULL OR "
            "production_label_product_version_snapshot >= 1)) OR "
            "(production_label_template_version_snapshot = 'current_40x30_v1' "
            "AND production_label_product_version_snapshot IS NOT NULL "
            "AND production_label_product_version_snapshot >= 1))",
        )

    op.create_table(
        "production_label_plan_refreshes",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("task_id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=120), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("operator_id", sa.Integer(), nullable=True),
        sa.Column("expected_task_version", sa.Integer(), nullable=False),
        sa.Column("expected_product_version", sa.Integer(), nullable=False),
        sa.Column("before_task_version", sa.Integer(), nullable=False),
        sa.Column("after_task_version", sa.Integer(), nullable=False),
        sa.Column("before_product_version", sa.Integer(), nullable=False),
        sa.Column("after_product_version", sa.Integer(), nullable=False),
        sa.Column("before_snapshot_json", sa.Text(), nullable=False),
        sa.Column("after_snapshot_json", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.func.current_timestamp(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(request_hash) = 64",
            name="ck_production_label_plan_refreshes_request_hash",
        ),
        sa.CheckConstraint(
            "expected_task_version = before_task_version "
            "AND before_task_version >= 1 "
            "AND after_task_version = before_task_version + 1",
            name="ck_production_label_plan_refreshes_task_versions",
        ),
        sa.CheckConstraint(
            "expected_product_version = before_product_version "
            "AND before_product_version >= 1 "
            "AND after_product_version = before_product_version",
            name="ck_production_label_plan_refreshes_product_versions",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["production_tasks.id"],
            name="fk_production_label_plan_refreshes_task",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.id"],
            name="fk_production_label_plan_refreshes_product",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["operator_id"],
            ["users.id"],
            name="fk_production_label_plan_refreshes_operator",
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_production_label_plan_refreshes_idempotency",
        ),
    )
    op.create_index(
        "ix_production_label_plan_refreshes_task_created",
        "production_label_plan_refreshes",
        ["task_id", "created_at"],
    )

    op.create_table(
        "production_packaging_label_print_jobs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("supplier_order_id", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=120), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("operator_id", sa.Integer(), nullable=True),
        sa.Column("template_version", sa.String(length=30), nullable=False),
        sa.Column("plan_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default="prepared",
        ),
        sa.Column("printed_confirmation_key", sa.String(length=120), nullable=True),
        sa.Column("printed_by_user_id", sa.Integer(), nullable=True),
        sa.Column("printed_at", sa.DateTime(), nullable=True),
        sa.Column("cancelled_by_user_id", sa.Integer(), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.func.current_timestamp(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(request_hash) = 64 AND length(plan_fingerprint) = 64 "
            "AND length(payload_hash) = 64",
            name="ck_production_packaging_label_print_jobs_hashes",
        ),
        sa.CheckConstraint(
            "template_version IN ('legacy_65x45_v1','current_40x30_v1')",
            name="ck_production_packaging_label_print_jobs_template",
        ),
        sa.CheckConstraint(
            "status IN ('prepared','printed','cancelled')",
            name="ck_production_packaging_label_print_jobs_status",
        ),
        sa.CheckConstraint(
            "((status = 'prepared' AND printed_confirmation_key IS NULL "
            "AND printed_at IS NULL AND cancelled_at IS NULL) OR "
            "(status = 'printed' AND printed_confirmation_key IS NOT NULL "
            "AND printed_at IS NOT NULL AND cancelled_at IS NULL) OR "
            "(status = 'cancelled' AND printed_confirmation_key IS NULL "
            "AND printed_at IS NULL AND cancelled_at IS NOT NULL))",
            name="ck_production_packaging_label_print_jobs_lifecycle",
        ),
        sa.ForeignKeyConstraint(
            ["supplier_order_id"],
            ["supplier_requisition_orders.id"],
            name="fk_production_packaging_label_print_jobs_supplier_order",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["operator_id"],
            ["users.id"],
            name="fk_production_packaging_label_print_jobs_operator",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["printed_by_user_id"],
            ["users.id"],
            name="fk_production_packaging_label_print_jobs_printed_by",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["cancelled_by_user_id"],
            ["users.id"],
            name="fk_production_packaging_label_print_jobs_cancelled_by",
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_production_packaging_label_print_jobs_idempotency",
        ),
        sa.UniqueConstraint(
            "printed_confirmation_key",
            name="uq_production_packaging_label_print_jobs_confirmation",
        ),
    )
    op.create_index(
        "ix_production_packaging_label_print_jobs_supplier_created",
        "production_packaging_label_print_jobs",
        ["supplier_order_id", "created_at"],
    )

    op.create_table(
        "production_packaging_label_print_job_tasks",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("print_job_id", sa.Integer(), nullable=False),
        sa.Column("production_task_id", sa.Integer(), nullable=False),
        sa.Column("production_task_version", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=True),
        sa.Column("product_version", sa.Integer(), nullable=True),
        sa.Column("template_version", sa.String(length=30), nullable=False),
        sa.Column("snapshot_json", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "production_task_version >= 1",
            name="ck_production_packaging_label_print_job_tasks_task_version",
        ),
        sa.CheckConstraint(
            "product_version IS NULL OR product_version >= 1",
            name="ck_production_packaging_label_print_job_tasks_product_version",
        ),
        sa.CheckConstraint(
            "template_version IN ('legacy_65x45_v1','current_40x30_v1')",
            name="ck_production_packaging_label_print_job_tasks_template",
        ),
        sa.ForeignKeyConstraint(
            ["print_job_id"],
            ["production_packaging_label_print_jobs.id"],
            name="fk_production_packaging_label_print_job_tasks_job",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["production_task_id"],
            ["production_tasks.id"],
            name="fk_production_packaging_label_print_job_tasks_task",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.id"],
            name="fk_production_packaging_label_print_job_tasks_product",
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint(
            "print_job_id",
            "production_task_id",
            name="uq_production_packaging_label_print_job_tasks_job_task",
        ),
    )
    op.create_index(
        "ix_production_packaging_label_print_job_tasks_task",
        "production_packaging_label_print_job_tasks",
        ["production_task_id"],
    )


def downgrade() -> None:
    connection = op.get_bind()
    refresh_count = int(
        connection.execute(
            sa.text("SELECT COUNT(*) FROM production_label_plan_refreshes")
        ).scalar_one()
        or 0
    )
    job_count = int(
        connection.execute(
            sa.text("SELECT COUNT(*) FROM production_packaging_label_print_jobs")
        ).scalar_one()
        or 0
    )
    task_fact_count = int(
        connection.execute(
            sa.text(
                "SELECT COUNT(*) FROM production_tasks "
                "WHERE production_label_template_version_snapshot = "
                ":current_template OR "
                "production_label_product_version_snapshot IS NOT NULL"
            ),
            {"current_template": CURRENT_TEMPLATE_VERSION},
        ).scalar_one()
        or 0
    )
    if refresh_count or job_count or task_fact_count:
        raise RuntimeError(
            "P1-50C 已存在新任务标签快照、标签计划刷新或打印作业事实，"
            "拒绝破坏性降级；"
            "请恢复升级前完整备份。"
        )

    op.drop_index(
        "ix_production_packaging_label_print_job_tasks_task",
        table_name="production_packaging_label_print_job_tasks",
    )
    op.drop_table("production_packaging_label_print_job_tasks")
    op.drop_index(
        "ix_production_packaging_label_print_jobs_supplier_created",
        table_name="production_packaging_label_print_jobs",
    )
    op.drop_table("production_packaging_label_print_jobs")
    op.drop_index(
        "ix_production_label_plan_refreshes_task_created",
        table_name="production_label_plan_refreshes",
    )
    op.drop_table("production_label_plan_refreshes")

    with op.batch_alter_table("production_tasks", recreate="always") as batch:
        batch.drop_constraint(
            "ck_production_tasks_label_product_version", type_="check"
        )
        batch.drop_constraint(
            "ck_production_tasks_label_template_version", type_="check"
        )
        batch.drop_column("production_label_product_version_snapshot")
        batch.drop_column("production_label_template_version_snapshot")
