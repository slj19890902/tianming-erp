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


class ProductionTaskProfileRefresh(Base):
    """Append-only evidence for one explicitly confirmed P1-92 refresh."""

    __tablename__ = "production_task_profile_refreshes"
    __table_args__ = (
        UniqueConstraint(
            "idempotency_key",
            name="uq_production_task_profile_refreshes_idempotency",
        ),
        CheckConstraint(
            "length(request_hash) = 64 AND length(preview_fingerprint) = 64",
            name="ck_production_task_profile_refreshes_hashes",
        ),
        CheckConstraint(
            "expected_task_version = before_task_version "
            "AND before_task_version >= 1 "
            "AND after_task_version = before_task_version + 1",
            name="ck_production_task_profile_refreshes_task_versions",
        ),
        CheckConstraint(
            "expected_source_version = source_version_snapshot "
            "AND source_version_snapshot >= 1",
            name="ck_production_task_profile_refreshes_source_version",
        ),
        Index(
            "ix_production_task_profile_refreshes_task_created",
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
    operator_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    idempotency_key: Mapped[str] = mapped_column(String(120), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    preview_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    expected_task_version: Mapped[int] = mapped_column(Integer, nullable=False)
    expected_source_version: Mapped[int] = mapped_column(Integer, nullable=False)
    before_task_version: Mapped[int] = mapped_column(Integer, nullable=False)
    after_task_version: Mapped[int] = mapped_column(Integer, nullable=False)
    source_version_snapshot: Mapped[int] = mapped_column(Integer, nullable=False)
    before_snapshot_json: Mapped[str] = mapped_column(Text, nullable=False)
    after_snapshot_json: Mapped[str] = mapped_column(Text, nullable=False)
    changes_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )
