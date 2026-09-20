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


class DeliveryPrintTemplateRevision(Base):
    """Append-only draft or published delivery print layout."""

    __tablename__ = "delivery_print_template_revisions"
    __table_args__ = (
        UniqueConstraint(
            "profile_key",
            "stream",
            "version",
            name="uq_delivery_print_template_profile_stream_version",
        ),
        UniqueConstraint(
            "operation_key",
            name="uq_delivery_print_template_operation_key",
        ),
        CheckConstraint(
            "stream IN ('draft','release')",
            name="ck_delivery_print_template_stream",
        ),
        CheckConstraint(
            "operation_kind IN ('save_draft','publish','rollback')",
            name="ck_delivery_print_template_operation_kind",
        ),
        CheckConstraint(
            "version >= 1 AND base_release_version >= 0",
            name="ck_delivery_print_template_versions",
        ),
        CheckConstraint(
            "length(payload_hash) = 64 AND length(request_hash) = 64",
            name="ck_delivery_print_template_hashes",
        ),
        Index(
            "ix_delivery_print_template_profile_stream_version",
            "profile_key",
            "stream",
            "version",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    profile_key: Mapped[str] = mapped_column(String(80), nullable=False)
    customer_id: Mapped[int | None] = mapped_column(
        ForeignKey("customers.id", ondelete="RESTRICT"), nullable=True
    )
    stream: Mapped[str] = mapped_column(String(10), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    catalog_version: Mapped[str] = mapped_column(String(40), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    base_release_version: Mapped[int] = mapped_column(Integer, nullable=False)
    operation_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    operation_key: Mapped[str] = mapped_column(String(120), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_release_version: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )
