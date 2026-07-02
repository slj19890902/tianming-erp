from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models import Base


class QuotationDailySequence(Base):
    __tablename__ = "quotation_daily_sequences"

    sequence_date: Mapped[date] = mapped_column(Date, primary_key=True)
    last_value: Mapped[int] = mapped_column(Integer, nullable=False)


class QuotationOrder(Base):
    __tablename__ = "quotation_orders"
    __table_args__ = (
        UniqueConstraint("quotation_no", name="uq_quotation_orders_no"),
        CheckConstraint(
            "status IN ('draft','quoted','accepted','converted','voided')",
            name="ck_quotation_orders_status",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    quotation_no: Mapped[str] = mapped_column(String(40), nullable=False)
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    customer_name: Mapped[str] = mapped_column(String(250), nullable=False)
    quotation_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft")
    total_amount: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, default=Decimal("0")
    )
    remarks: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, onupdate=func.current_timestamp()
    )

    items: Mapped[list["QuotationItem"]] = relationship(
        back_populates="quotation",
        cascade="all, delete-orphan",
        order_by="QuotationItem.id",
    )


class QuotationItem(Base):
    __tablename__ = "quotation_items"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    quotation_id: Mapped[int] = mapped_column(
        ForeignKey("quotation_orders.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    product_name: Mapped[str] = mapped_column(String(250), nullable=False)
    temporary_code: Mapped[str | None] = mapped_column(String(150), nullable=True)
    box_type: Mapped[str] = mapped_column(String(150), nullable=False)
    length_mm: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    width_mm: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    height_mm: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    material_id: Mapped[int | None] = mapped_column(
        ForeignKey("materials.id", ondelete="SET NULL"), nullable=True
    )
    material_supplier: Mapped[str | None] = mapped_column(String(200), nullable=True)
    material_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    flute_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    quantity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    estimated_unit_cost: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 4), nullable=True
    )
    margin_rate: Mapped[Decimal] = mapped_column(
        Numeric(7, 4), nullable=False, default=Decimal("20")
    )
    suggested_unit_price: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 4), nullable=True
    )
    final_unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    remarks: Mapped[str | None] = mapped_column(Text, nullable=True)
    converted_product_id: Mapped[int | None] = mapped_column(
        ForeignKey("products.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, onupdate=func.current_timestamp()
    )

    quotation: Mapped["QuotationOrder"] = relationship(back_populates="items")
