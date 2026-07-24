from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models import Base

if TYPE_CHECKING:
    from app.models.inventory_onboarding import InventoryOnboardingBatch
    from app.models.user import User


class InventoryOnboardingPosting(Base):
    """Immutable receipt for one formally posted onboarding batch."""

    __tablename__ = "inventory_onboarding_postings"
    __table_args__ = (
        CheckConstraint(
            "onboarding_batch_version >= 1",
            name="ck_inventory_onboarding_postings_batch_version",
        ),
        CheckConstraint(
            "length(onboarding_batch_fingerprint) = 64 "
            "AND length(posting_fingerprint) = 64",
            name="ck_inventory_onboarding_postings_fingerprints",
        ),
        CheckConstraint(
            "length(trim(area_code_snapshot)) > 0",
            name="ck_inventory_onboarding_postings_area",
        ),
        CheckConstraint(
            "line_count > 0 AND line_count <= 100",
            name="ck_inventory_onboarding_postings_line_count",
        ),
        CheckConstraint(
            "finished_line_count >= 0 "
            "AND semi_finished_line_count >= 0 "
            "AND finished_line_count + semi_finished_line_count = line_count",
            name="ck_inventory_onboarding_postings_type_counts",
        ),
        UniqueConstraint(
            "posting_number",
            name="uq_inventory_onboarding_postings_number",
        ),
        UniqueConstraint(
            "onboarding_batch_id",
            name="uq_inventory_onboarding_postings_batch",
        ),
        UniqueConstraint(
            "idempotency_key",
            name="uq_inventory_onboarding_postings_idempotency",
        ),
        Index(
            "ix_inventory_onboarding_postings_posted_at",
            "posted_at",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    posting_number: Mapped[str] = mapped_column(String(50), nullable=False)
    onboarding_batch_id: Mapped[int] = mapped_column(
        ForeignKey("inventory_onboarding_batches.id", ondelete="RESTRICT"),
        nullable=False,
    )
    onboarding_batch_version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )
    onboarding_batch_fingerprint: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )
    posting_fingerprint: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )
    idempotency_key: Mapped[str] = mapped_column(String(100), nullable=False)
    floor_snapshot: Mapped[str | None] = mapped_column(
        String(20),
        nullable=True,
    )
    area_code_snapshot: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
    )
    line_count: Mapped[int] = mapped_column(Integer, nullable=False)
    finished_line_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )
    semi_finished_line_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )
    evidence_json: Mapped[dict[str, object]] = mapped_column(
        JSON,
        nullable=False,
    )
    posted_by: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    posted_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.current_timestamp(),
        nullable=False,
    )

    onboarding_batch: Mapped["InventoryOnboardingBatch"] = relationship()
    operator: Mapped["User"] = relationship()
