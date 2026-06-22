from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base


class HistoricalRequisitionMap(Base):
    __tablename__ = "historical_requisition_maps"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    product_id: Mapped[int | None] = mapped_column(
        ForeignKey("products.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    search_key: Mapped[str] = mapped_column(String(500), nullable=False, index=True)
    normalized_search_key: Mapped[str] = mapped_column(
        String(500),
        nullable=False,
        unique=True,
    )
    cardboard_length: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    cardboard_width: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    score_lines: Mapped[str | None] = mapped_column(String(250), nullable=True)
    material_code: Mapped[str] = mapped_column(String(100), nullable=False)
    quantity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    record_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    source_workbook: Mapped[str] = mapped_column(String(260), nullable=False)
    source_sheet: Mapped[str] = mapped_column(String(150), nullable=False)
    source_row: Mapped[int] = mapped_column(Integer, nullable=False)
    raw_data: Mapped[str | None] = mapped_column(Text, nullable=True)
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

