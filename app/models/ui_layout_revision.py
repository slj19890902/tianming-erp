from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base


class UiLayoutRevision(Base):
    """Append-only role and display-mode UI layout revision."""

    __tablename__ = "ui_layout_revisions"
    __table_args__ = (
        UniqueConstraint("role_code", "display_mode", "stream", "version", name="uq_ui_layout_revision_stream_version"),
        UniqueConstraint("operation_key", name="uq_ui_layout_revision_operation_key"),
        CheckConstraint("role_code IN ('admin','finance','sales','workshop','boss')", name="ck_ui_layout_revision_role"),
        CheckConstraint("display_mode IN ('standard','large','mobile')", name="ck_ui_layout_revision_display_mode"),
        CheckConstraint("stream IN ('draft','release')", name="ck_ui_layout_revision_stream"),
        CheckConstraint("version > 0", name="ck_ui_layout_revision_version"),
        CheckConstraint("base_release_version >= 0", name="ck_ui_layout_revision_base_release_version"),
        Index("ix_ui_layout_revisions_profile_stream_version", "role_code", "display_mode", "stream", "version"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    role_code: Mapped[str] = mapped_column(String(20), nullable=False)
    display_mode: Mapped[str] = mapped_column(String(20), nullable=False)
    stream: Mapped[str] = mapped_column(String(10), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    catalog_version: Mapped[str] = mapped_column(String(40), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    base_release_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    operation_kind: Mapped[str] = mapped_column(String(30), nullable=False)
    operation_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    request_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_release_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )
