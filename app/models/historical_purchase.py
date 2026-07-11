from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    Date,
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


class HistoricalPurchaseEntry(Base):
    """One source row from the manually maintained purchase-history workbook."""

    __tablename__ = "historical_purchase_entries"
    __table_args__ = (
        UniqueConstraint(
            "source_workbook",
            "source_sheet",
            "source_row",
            name="uq_historical_purchase_entries_source_row",
        ),
        Index(
            "ix_historical_purchase_entries_product_date",
            "product_id",
            "record_date",
        ),
        Index(
            "ix_historical_purchase_entries_customer_date",
            "customer_id",
            "record_date",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    source_workbook: Mapped[str] = mapped_column(String(260), nullable=False)
    source_sheet: Mapped[str] = mapped_column(String(150), nullable=False)
    source_row: Mapped[int] = mapped_column(Integer, nullable=False)
    source_file_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_fingerprint: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True
    )

    supplier_name: Mapped[str | None] = mapped_column(String(150), nullable=True)
    record_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    product_reference: Mapped[str] = mapped_column(String(500), nullable=False)
    search_text: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_search_text: Mapped[str] = mapped_column(
        String(1000), nullable=False, index=True
    )
    material_code: Mapped[str] = mapped_column(String(100), nullable=False)
    historical_quantity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    report_length_mm: Mapped[int] = mapped_column(Integer, nullable=False)
    report_width_mm: Mapped[int] = mapped_column(Integer, nullable=False)
    crease_text: Mapped[str | None] = mapped_column(String(250), nullable=True)
    crease_type: Mapped[str | None] = mapped_column(String(30), nullable=True)
    crease_left_mm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    crease_middle_mm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    crease_right_mm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    product_id: Mapped[int | None] = mapped_column(
        ForeignKey("products.id", ondelete="SET NULL"), nullable=True, index=True
    )
    customer_id: Mapped[int | None] = mapped_column(
        ForeignKey("customers.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime, onupdate=func.current_timestamp(), nullable=True
    )
