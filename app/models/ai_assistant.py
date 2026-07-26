from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base


class AiAnalysisRun(Base):
    """One immutable-input AI interpretation attempt over an ERP snapshot."""

    __tablename__ = "ai_analysis_runs"
    __table_args__ = (
        CheckConstraint(
            "analysis_type IN ('inventory_insight')",
            name="ck_ai_analysis_runs_type",
        ),
        CheckConstraint(
            "status IN ('running', 'success', 'failed', 'degraded')",
            name="ck_ai_analysis_runs_status",
        ),
        CheckConstraint(
            "length(input_hash) = 64",
            name="ck_ai_analysis_runs_input_hash",
        ),
        CheckConstraint(
            "latency_ms IS NULL OR latency_ms >= 0",
            name="ck_ai_analysis_runs_latency",
        ),
        CheckConstraint(
            "input_tokens >= 0 AND output_tokens >= 0",
            name="ck_ai_analysis_runs_tokens",
        ),
        CheckConstraint(
            "provider_cost_estimate >= 0",
            name="ck_ai_analysis_runs_cost",
        ),
        UniqueConstraint(
            "public_id",
            name="uq_ai_analysis_runs_public_id",
        ),
        UniqueConstraint(
            "requested_by",
            "idempotency_key",
            name="uq_ai_analysis_runs_user_idempotency",
        ),
        Index(
            "ix_ai_analysis_runs_requested_created",
            "requested_by",
            "created_at",
        ),
        Index(
            "ix_ai_analysis_runs_status_created",
            "status",
            "created_at",
        ),
        Index(
            "ix_ai_analysis_runs_input_hash",
            "input_hash",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(String(36), nullable=False)
    analysis_type: Mapped[str] = mapped_column(String(30), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    requested_by: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    as_of_date: Mapped[date] = mapped_column(Date, nullable=False)
    scope_json: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(120), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(80), nullable=False)
    provider_code: Mapped[str] = mapped_column(String(50), nullable=False)
    model_code: Mapped[str] = mapped_column(String(100), nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    sanitized_snapshot_json: Mapped[str] = mapped_column(Text, nullable=False)
    output_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    input_tokens: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default="0",
        nullable=False,
    )
    output_tokens: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default="0",
        nullable=False,
    )
    provider_cost_estimate: Mapped[Decimal] = mapped_column(
        Numeric(18, 6),
        default=Decimal("0"),
        server_default="0",
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.current_timestamp(),
        nullable=False,
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
    )


class AiAnalysisFeedback(Base):
    """One user's latest explicit rating for one analysis run."""

    __tablename__ = "ai_analysis_feedback"
    __table_args__ = (
        CheckConstraint(
            "rating IN ('helpful', 'inaccurate')",
            name="ck_ai_analysis_feedback_rating",
        ),
        CheckConstraint(
            "reason_code IS NULL OR reason_code IN ("
            "'useful_actionable', 'accurate', 'wrong_data', "
            "'missing_context', 'hard_to_understand', 'other'"
            ")",
            name="ck_ai_analysis_feedback_reason",
        ),
        CheckConstraint(
            "remark IS NULL OR length(remark) <= 500",
            name="ck_ai_analysis_feedback_remark",
        ),
        UniqueConstraint(
            "run_id",
            "user_id",
            name="uq_ai_analysis_feedback_run_user",
        ),
        Index(
            "ix_ai_analysis_feedback_created",
            "created_at",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("ai_analysis_runs.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    rating: Mapped[str] = mapped_column(String(20), nullable=False)
    reason_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    remark: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.current_timestamp(),
        nullable=False,
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        onupdate=func.current_timestamp(),
        nullable=True,
    )


class AiUsageLedger(Base):
    """Provider usage fact for one run; ERP business tables are never charged."""

    __tablename__ = "ai_usage_ledger"
    __table_args__ = (
        CheckConstraint(
            "feature_code IN ('ai.inventory.insight')",
            name="ck_ai_usage_ledger_feature",
        ),
        CheckConstraint(
            "request_count >= 0 AND request_count <= 1",
            name="ck_ai_usage_ledger_request_count",
        ),
        CheckConstraint(
            "input_tokens >= 0 AND output_tokens >= 0",
            name="ck_ai_usage_ledger_tokens",
        ),
        CheckConstraint(
            "billable_units >= 0 AND provider_cost_estimate >= 0",
            name="ck_ai_usage_ledger_cost",
        ),
        UniqueConstraint(
            "run_id",
            name="uq_ai_usage_ledger_run",
        ),
        Index(
            "ix_ai_usage_ledger_feature_created",
            "feature_code",
            "created_at",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("ai_analysis_runs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    feature_code: Mapped[str] = mapped_column(String(50), nullable=False)
    provider_code: Mapped[str] = mapped_column(String(50), nullable=False)
    model_code: Mapped[str] = mapped_column(String(100), nullable=False)
    request_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default="0",
        nullable=False,
    )
    input_tokens: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default="0",
        nullable=False,
    )
    output_tokens: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default="0",
        nullable=False,
    )
    billable_units: Mapped[Decimal] = mapped_column(
        Numeric(18, 6),
        default=Decimal("0"),
        server_default="0",
        nullable=False,
    )
    provider_cost_estimate: Mapped[Decimal] = mapped_column(
        Numeric(18, 6),
        default=Decimal("0"),
        server_default="0",
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.current_timestamp(),
        nullable=False,
    )
