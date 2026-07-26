"""AI-001 inventory interpretation run, feedback and usage ledgers.

Revision ID: cq74v8x9z63
Revises: cn70v8x9z59
Create Date: 2026-07-26
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "cq74v8x9z63"
down_revision = "cn70v8x9z59"
branch_labels = None
depends_on = None


RUN_TABLE = "ai_analysis_runs"
FEEDBACK_TABLE = "ai_analysis_feedback"
USAGE_TABLE = "ai_usage_ledger"
DOWNGRADE_BLOCKED_MESSAGE = (
    "AI analysis facts already exist; refusing to remove audit, feedback or usage ledgers"
)


def _assert_safe_downgrade() -> None:
    connection = op.get_bind()
    fact_count = connection.execute(
        sa.text(
            f"""
            SELECT
                (SELECT COUNT(*) FROM {RUN_TABLE})
              + (SELECT COUNT(*) FROM {FEEDBACK_TABLE})
              + (SELECT COUNT(*) FROM {USAGE_TABLE})
            """
        )
    ).scalar_one()
    if int(fact_count or 0) > 0:
        raise RuntimeError(DOWNGRADE_BLOCKED_MESSAGE)


def upgrade() -> None:
    op.create_table(
        RUN_TABLE,
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("public_id", sa.String(length=36), nullable=False),
        sa.Column("analysis_type", sa.String(length=30), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("requested_by", sa.Integer(), nullable=False),
        sa.Column("as_of_date", sa.Date(), nullable=False),
        sa.Column("scope_json", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=120), nullable=False),
        sa.Column("prompt_version", sa.String(length=80), nullable=False),
        sa.Column("provider_code", sa.String(length=50), nullable=False),
        sa.Column("model_code", sa.String(length=100), nullable=False),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column("sanitized_snapshot_json", sa.Text(), nullable=False),
        sa.Column("output_json", sa.Text(), nullable=True),
        sa.Column("error_code", sa.String(length=80), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column(
            "input_tokens",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "output_tokens",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "provider_cost_estimate",
            sa.Numeric(precision=18, scale=6),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "analysis_type IN ('inventory_insight')",
            name="ck_ai_analysis_runs_type",
        ),
        sa.CheckConstraint(
            "status IN ('running', 'success', 'failed', 'degraded')",
            name="ck_ai_analysis_runs_status",
        ),
        sa.CheckConstraint(
            "length(input_hash) = 64",
            name="ck_ai_analysis_runs_input_hash",
        ),
        sa.CheckConstraint(
            "latency_ms IS NULL OR latency_ms >= 0",
            name="ck_ai_analysis_runs_latency",
        ),
        sa.CheckConstraint(
            "input_tokens >= 0 AND output_tokens >= 0",
            name="ck_ai_analysis_runs_tokens",
        ),
        sa.CheckConstraint(
            "provider_cost_estimate >= 0",
            name="ck_ai_analysis_runs_cost",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by"],
            ["users.id"],
            name="fk_ai_analysis_runs_requested_by",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "public_id",
            name="uq_ai_analysis_runs_public_id",
        ),
        sa.UniqueConstraint(
            "requested_by",
            "idempotency_key",
            name="uq_ai_analysis_runs_user_idempotency",
        ),
    )
    op.create_index(
        "ix_ai_analysis_runs_requested_created",
        RUN_TABLE,
        ["requested_by", "created_at"],
    )
    op.create_index(
        "ix_ai_analysis_runs_status_created",
        RUN_TABLE,
        ["status", "created_at"],
    )
    op.create_index(
        "ix_ai_analysis_runs_input_hash",
        RUN_TABLE,
        ["input_hash"],
    )

    op.create_table(
        FEEDBACK_TABLE,
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("rating", sa.String(length=20), nullable=False),
        sa.Column("reason_code", sa.String(length=40), nullable=True),
        sa.Column("remark", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "rating IN ('helpful', 'inaccurate')",
            name="ck_ai_analysis_feedback_rating",
        ),
        sa.CheckConstraint(
            "reason_code IS NULL OR reason_code IN ("
            "'useful_actionable', 'accurate', 'wrong_data', "
            "'missing_context', 'hard_to_understand', 'other'"
            ")",
            name="ck_ai_analysis_feedback_reason",
        ),
        sa.CheckConstraint(
            "remark IS NULL OR length(remark) <= 500",
            name="ck_ai_analysis_feedback_remark",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            [f"{RUN_TABLE}.id"],
            name="fk_ai_analysis_feedback_run_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_ai_analysis_feedback_user_id",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "run_id",
            "user_id",
            name="uq_ai_analysis_feedback_run_user",
        ),
    )
    op.create_index(
        "ix_ai_analysis_feedback_run_id",
        FEEDBACK_TABLE,
        ["run_id"],
    )
    op.create_index(
        "ix_ai_analysis_feedback_user_id",
        FEEDBACK_TABLE,
        ["user_id"],
    )
    op.create_index(
        "ix_ai_analysis_feedback_created",
        FEEDBACK_TABLE,
        ["created_at"],
    )

    op.create_table(
        USAGE_TABLE,
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("feature_code", sa.String(length=50), nullable=False),
        sa.Column("provider_code", sa.String(length=50), nullable=False),
        sa.Column("model_code", sa.String(length=100), nullable=False),
        sa.Column(
            "request_count",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "input_tokens",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "output_tokens",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "billable_units",
            sa.Numeric(precision=18, scale=6),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "provider_cost_estimate",
            sa.Numeric(precision=18, scale=6),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "feature_code IN ('ai.inventory.insight')",
            name="ck_ai_usage_ledger_feature",
        ),
        sa.CheckConstraint(
            "request_count >= 0 AND request_count <= 1",
            name="ck_ai_usage_ledger_request_count",
        ),
        sa.CheckConstraint(
            "input_tokens >= 0 AND output_tokens >= 0",
            name="ck_ai_usage_ledger_tokens",
        ),
        sa.CheckConstraint(
            "billable_units >= 0 AND provider_cost_estimate >= 0",
            name="ck_ai_usage_ledger_cost",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            [f"{RUN_TABLE}.id"],
            name="fk_ai_usage_ledger_run_id",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "run_id",
            name="uq_ai_usage_ledger_run",
        ),
    )
    op.create_index(
        "ix_ai_usage_ledger_feature_created",
        USAGE_TABLE,
        ["feature_code", "created_at"],
    )


def downgrade() -> None:
    _assert_safe_downgrade()
    op.drop_index(
        "ix_ai_usage_ledger_feature_created",
        table_name=USAGE_TABLE,
    )
    op.drop_table(USAGE_TABLE)
    op.drop_index(
        "ix_ai_analysis_feedback_created",
        table_name=FEEDBACK_TABLE,
    )
    op.drop_index(
        "ix_ai_analysis_feedback_user_id",
        table_name=FEEDBACK_TABLE,
    )
    op.drop_index(
        "ix_ai_analysis_feedback_run_id",
        table_name=FEEDBACK_TABLE,
    )
    op.drop_table(FEEDBACK_TABLE)
    op.drop_index(
        "ix_ai_analysis_runs_input_hash",
        table_name=RUN_TABLE,
    )
    op.drop_index(
        "ix_ai_analysis_runs_status_created",
        table_name=RUN_TABLE,
    )
    op.drop_index(
        "ix_ai_analysis_runs_requested_created",
        table_name=RUN_TABLE,
    )
    op.drop_table(RUN_TABLE)
