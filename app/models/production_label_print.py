from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base


class ProductionLabelPlanRefresh(Base):
    """Append-only evidence for an operator-approved task label-plan refresh."""

    __tablename__ = "production_label_plan_refreshes"
    __table_args__ = (
        UniqueConstraint(
            "idempotency_key",
            name="uq_production_label_plan_refreshes_idempotency",
        ),
        CheckConstraint(
            "length(request_hash) = 64",
            name="ck_production_label_plan_refreshes_request_hash",
        ),
        CheckConstraint(
            "expected_task_version = before_task_version "
            "AND before_task_version >= 1 "
            "AND after_task_version = before_task_version + 1",
            name="ck_production_label_plan_refreshes_task_versions",
        ),
        CheckConstraint(
            "expected_product_version = before_product_version "
            "AND before_product_version >= 1 "
            "AND after_product_version = before_product_version",
            name="ck_production_label_plan_refreshes_product_versions",
        ),
        Index(
            "ix_production_label_plan_refreshes_task_created",
            "task_id",
            "created_at",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(
        ForeignKey("production_tasks.id", ondelete="RESTRICT"), nullable=False
    )
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"), nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(120), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    operator_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    expected_task_version: Mapped[int] = mapped_column(Integer, nullable=False)
    expected_product_version: Mapped[int] = mapped_column(Integer, nullable=False)
    before_task_version: Mapped[int] = mapped_column(Integer, nullable=False)
    after_task_version: Mapped[int] = mapped_column(Integer, nullable=False)
    before_product_version: Mapped[int] = mapped_column(Integer, nullable=False)
    after_product_version: Mapped[int] = mapped_column(Integer, nullable=False)
    before_snapshot_json: Mapped[str] = mapped_column(Text, nullable=False)
    after_snapshot_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )


class ProductionPackagingLabelPrintJob(Base):
    """Frozen printable package; only explicit confirmation makes it printed."""

    __tablename__ = "production_packaging_label_print_jobs"
    __table_args__ = (
        UniqueConstraint(
            "idempotency_key",
            name="uq_production_packaging_label_print_jobs_idempotency",
        ),
        UniqueConstraint(
            "printed_confirmation_key",
            name="uq_production_packaging_label_print_jobs_confirmation",
        ),
        CheckConstraint(
            "length(request_hash) = 64 AND length(plan_fingerprint) = 64 "
            "AND length(payload_hash) = 64",
            name="ck_production_packaging_label_print_jobs_hashes",
        ),
        CheckConstraint(
            "template_version IN "
            "('legacy_65x45_v1','current_40x30_v1','current_40x30_v2')",
            name="ck_production_packaging_label_print_jobs_template",
        ),
        CheckConstraint(
            "status IN ('prepared','printed','cancelled')",
            name="ck_production_packaging_label_print_jobs_status",
        ),
        CheckConstraint(
            "((supplier_order_id IS NOT NULL AND material_requisition_id IS NULL) OR "
            "(supplier_order_id IS NULL AND material_requisition_id IS NOT NULL))",
            name="ck_production_packaging_label_print_jobs_source",
        ),
        CheckConstraint(
            "((status = 'prepared' AND printed_confirmation_key IS NULL "
            "AND printed_at IS NULL AND cancelled_at IS NULL) OR "
            "(status = 'printed' AND printed_confirmation_key IS NOT NULL "
            "AND printed_at IS NOT NULL AND cancelled_at IS NULL) OR "
            "(status = 'cancelled' AND printed_confirmation_key IS NULL "
            "AND printed_at IS NULL AND cancelled_at IS NOT NULL))",
            name="ck_production_packaging_label_print_jobs_lifecycle",
        ),
        Index(
            "ix_production_packaging_label_print_jobs_supplier_created",
            "supplier_order_id",
            "created_at",
        ),
        Index(
            "ix_production_packaging_label_print_jobs_requisition_created",
            "material_requisition_id",
            "created_at",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    supplier_order_id: Mapped[int | None] = mapped_column(
        ForeignKey("supplier_requisition_orders.id", ondelete="RESTRICT"),
        nullable=True,
    )
    material_requisition_id: Mapped[int | None] = mapped_column(
        ForeignKey("material_requisitions.id", ondelete="RESTRICT"),
        nullable=True,
    )
    idempotency_key: Mapped[str] = mapped_column(String(120), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    operator_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    template_version: Mapped[str] = mapped_column(String(30), nullable=False)
    plan_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), default="prepared", server_default="prepared", nullable=False
    )
    printed_confirmation_key: Mapped[str | None] = mapped_column(
        String(120), nullable=True
    )
    printed_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    printed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    cancelled_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )


class ProductionPackagingLabelPrintJobTask(Base):
    """Task-level immutable evidence carried by one frozen print job."""

    __tablename__ = "production_packaging_label_print_job_tasks"
    __table_args__ = (
        UniqueConstraint(
            "print_job_id",
            "production_task_id",
            name="uq_production_packaging_label_print_job_tasks_job_task",
        ),
        CheckConstraint(
            "production_task_version >= 1",
            name="ck_production_packaging_label_print_job_tasks_task_version",
        ),
        CheckConstraint(
            "product_version IS NULL OR product_version >= 1",
            name="ck_production_packaging_label_print_job_tasks_product_version",
        ),
        CheckConstraint(
            "template_version IN "
            "('legacy_65x45_v1','current_40x30_v1','current_40x30_v2')",
            name="ck_production_packaging_label_print_job_tasks_template",
        ),
        Index(
            "ix_production_packaging_label_print_job_tasks_task",
            "production_task_id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    print_job_id: Mapped[int] = mapped_column(
        ForeignKey(
            "production_packaging_label_print_jobs.id",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    production_task_id: Mapped[int] = mapped_column(
        ForeignKey("production_tasks.id", ondelete="RESTRICT"), nullable=False
    )
    production_task_version: Mapped[int] = mapped_column(Integer, nullable=False)
    product_id: Mapped[int | None] = mapped_column(
        ForeignKey("products.id", ondelete="SET NULL"), nullable=True
    )
    product_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    template_version: Mapped[str] = mapped_column(String(30), nullable=False)
    snapshot_json: Mapped[str] = mapped_column(Text, nullable=False)


class ProductionPackagingLabelLayoutRevision(Base):
    """Append-only draft or released 40x30 packaging-label layout."""

    __tablename__ = "production_packaging_label_layout_revisions"
    __table_args__ = (
        UniqueConstraint(
            "stream",
            "version",
            name="uq_production_packaging_label_layout_stream_version",
        ),
        UniqueConstraint(
            "operation_key",
            name="uq_production_packaging_label_layout_operation_key",
        ),
        CheckConstraint(
            "stream IN ('draft','release')",
            name="ck_production_packaging_label_layout_stream",
        ),
        CheckConstraint(
            "version >= 1 AND base_release_version >= 0",
            name="ck_production_packaging_label_layout_versions",
        ),
        CheckConstraint(
            "length(payload_hash) = 64 AND "
            "(request_hash IS NULL OR length(request_hash) = 64)",
            name="ck_production_packaging_label_layout_hashes",
        ),
        CheckConstraint(
            "source_release_version IS NULL OR source_release_version >= 1",
            name="ck_production_packaging_label_layout_source_version",
        ),
        Index(
            "ix_production_packaging_label_layout_stream_version",
            "stream",
            "version",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    stream: Mapped[str] = mapped_column(String(10), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    catalog_version: Mapped[str] = mapped_column(String(40), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    base_release_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    operation_kind: Mapped[str] = mapped_column(String(30), nullable=False)
    operation_key: Mapped[str | None] = mapped_column(String(120), nullable=True)
    request_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_release_version: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )
