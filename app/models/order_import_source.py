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
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models import Base


class OrderImportSource(Base):
    """One trusted external source occurrence mapped to one sales order."""

    __tablename__ = "order_import_sources"
    __table_args__ = (
        CheckConstraint(
            "source_kind IN ('pdf_upload', 'email_attachment', 'excel_upload')",
            name="ck_order_import_sources_kind",
        ),
        CheckConstraint(
            "length(source_hash) = 64 AND length(payload_hash) = 64",
            name="ck_order_import_sources_hashes",
        ),
        UniqueConstraint(
            "source_kind",
            "source_key",
            name="uq_order_import_sources_kind_key",
        ),
        UniqueConstraint("order_id", name="uq_order_import_sources_order_id"),
        UniqueConstraint(
            "email_attachment_id",
            name="uq_order_import_sources_email_attachment_id",
        ),
        Index(
            "ix_order_import_sources_customer_po",
            "customer_id",
            "customer_po_snapshot",
        ),
        Index("ix_order_import_sources_hash", "source_hash"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_kind: Mapped[str] = mapped_column(String(30), nullable=False)
    source_key: Mapped[str] = mapped_column(String(120), nullable=False)
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_name_snapshot: Mapped[str] = mapped_column(String(255), nullable=False)
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False
    )
    customer_po_snapshot: Mapped[str | None] = mapped_column(String(150), nullable=True)
    order_id: Mapped[int | None] = mapped_column(
        ForeignKey("sales_orders.id", ondelete="SET NULL"), nullable=True
    )
    email_attachment_id: Mapped[int | None] = mapped_column(
        ForeignKey("email_intake_attachments.id", ondelete="SET NULL"), nullable=True
    )
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    confirmation_summary_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )

    lines: Mapped[list["OrderImportSourceLine"]] = relationship(
        back_populates="source",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="OrderImportSourceLine.source_position",
    )


class OrderImportSourceLine(Base):
    """Immutable line trace from the signed preview to the created order item."""

    __tablename__ = "order_import_source_lines"
    __table_args__ = (
        CheckConstraint(
            "source_position >= 1",
            name="ck_order_import_source_lines_position",
        ),
        UniqueConstraint(
            "source_id",
            "source_position",
            name="uq_order_import_source_lines_position",
        ),
        UniqueConstraint(
            "order_item_id",
            name="uq_order_import_source_lines_order_item_id",
        ),
        Index(
            "ix_order_import_source_lines_source_line",
            "source_id",
            "source_line_label",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_id: Mapped[int] = mapped_column(
        ForeignKey("order_import_sources.id", ondelete="CASCADE"), nullable=False
    )
    order_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("sales_order_items.id", ondelete="SET NULL"), nullable=True
    )
    source_position: Mapped[int] = mapped_column(Integer, nullable=False)
    source_page: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_sheet: Mapped[str | None] = mapped_column(String(120), nullable=True)
    source_row: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_line_label: Mapped[str | None] = mapped_column(String(80), nullable=True)
    raw_line_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    recognized_line_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    submitted_line_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    confirmation_summary_json: Mapped[str] = mapped_column(Text, nullable=False)

    source: Mapped[OrderImportSource] = relationship(back_populates="lines")
