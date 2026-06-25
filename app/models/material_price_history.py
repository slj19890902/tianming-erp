from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, ForeignKey, Integer, Numeric, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base


class MaterialPriceAdjustmentBatch(Base):
    """供应商统一调价批次：一次「确认应用调价」对应一条批次记录。"""

    __tablename__ = "material_price_adjustment_batches"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    supplier_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    adjust_percent: Mapped[Decimal | None] = mapped_column(Numeric(8, 4), nullable=True)
    effective_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    affected_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    remark: Mapped[str | None] = mapped_column(Text, nullable=True)
    operator: Mapped[str | None] = mapped_column(String(100), nullable=True)
    backup_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    report_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )


class MaterialPriceHistory(Base):
    """每个被调价材质的历史价格记录（一次调价 → 每条材质一行）。"""

    __tablename__ = "material_price_history"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    material_id: Mapped[int] = mapped_column(
        ForeignKey("materials.id", ondelete="CASCADE"), index=True, nullable=False
    )
    supplier_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    material_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    old_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
    new_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
    adjust_percent: Mapped[Decimal | None] = mapped_column(Numeric(8, 4), nullable=True)
    effective_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    adjust_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    operator: Mapped[str | None] = mapped_column(String(100), nullable=True)
    batch_id: Mapped[int | None] = mapped_column(
        ForeignKey("material_price_adjustment_batches.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )
