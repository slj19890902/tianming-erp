from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, Integer, Numeric, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models import Base

if TYPE_CHECKING:
    from app.models.product import Product


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    customer_number: Mapped[int | None] = mapped_column(
        Integer,
        unique=True,
        index=True,
        nullable=True,
    )
    customer_code: Mapped[str | None] = mapped_column(
        String(50),
        unique=True,
        index=True,
        nullable=True,
    )
    name: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    payment_term_days: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    credit_limit: Mapped[Decimal] = mapped_column(
        Numeric(14, 2),
        default=0,
        nullable=False,
    )
    delivery_method: Mapped[str] = mapped_column(
        String(20),
        default="配送",
        nullable=False,
    )

    contact_person: Mapped[str | None] = mapped_column(String(100), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(100), nullable=True)
    address: Mapped[str | None] = mapped_column(Text, nullable=True)
    billing_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    credit_terms: Mapped[str | None] = mapped_column(String(100), nullable=True)
    default_tax_rate: Mapped[Decimal] = mapped_column(
        Numeric(6, 4),
        default=Decimal("0.13"),
        nullable=False,
    )
    invoice_title: Mapped[str | None] = mapped_column(String(200), nullable=True)
    tax_no: Mapped[str | None] = mapped_column(String(100), nullable=True)
    bank_account: Mapped[str | None] = mapped_column(String(200), nullable=True)
    remark: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="active", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
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

    products: Mapped[list["Product"]] = relationship(
        back_populates="customer",
        passive_deletes=True,
    )
