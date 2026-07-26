from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models import Base

if TYPE_CHECKING:
    from app.models.order import Order


class ContractDailySequence(Base):
    __tablename__ = "contract_daily_sequences"

    sequence_date: Mapped[date] = mapped_column(Date, primary_key=True)
    last_value: Mapped[int] = mapped_column(Integer, nullable=False)


class CustomerContract(Base):
    __tablename__ = "customer_contracts"
    __table_args__ = (
        UniqueConstraint("contract_no", name="uq_customer_contracts_contract_no"),
        UniqueConstraint(
            "converted_order_id",
            name="uq_customer_contracts_converted_order_id",
        ),
        UniqueConstraint(
            "conversion_idempotency_key",
            name="uq_customer_contracts_conversion_idempotency_key",
        ),
        CheckConstraint(
            "status IN ('draft', 'confirmed', 'converted')",
            name="ck_customer_contracts_status",
        ),
        CheckConstraint("version >= 1", name="ck_customer_contracts_version"),
        CheckConstraint(
            "total_amount >= 0", name="ck_customer_contracts_total_amount"
        ),
        Index("ix_customer_contracts_customer_id", "customer_id"),
        Index("ix_customer_contracts_status", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    contract_no: Mapped[str] = mapped_column(String(40), nullable=False)
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False
    )
    customer_name: Mapped[str] = mapped_column(String(250), nullable=False)
    customer_contact: Mapped[str | None] = mapped_column(String(100), nullable=True)
    customer_phone: Mapped[str | None] = mapped_column(String(100), nullable=True)
    customer_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    invoice_title: Mapped[str | None] = mapped_column(String(200), nullable=True)
    tax_no: Mapped[str | None] = mapped_column(String(100), nullable=True)
    bank_account: Mapped[str | None] = mapped_column(String(200), nullable=True)
    payment_terms: Mapped[str | None] = mapped_column(String(200), nullable=True)
    contract_date: Mapped[date] = mapped_column(Date, nullable=False)
    customer_po: Mapped[str | None] = mapped_column(String(150), nullable=True)
    delivery_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    remarks: Mapped[str | None] = mapped_column(Text, nullable=True)
    total_amount: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, default=Decimal("0")
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, onupdate=func.current_timestamp()
    )
    confirmed_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    converted_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    converted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    converted_order_id: Mapped[int | None] = mapped_column(
        ForeignKey("sales_orders.id", ondelete="RESTRICT"), nullable=True
    )
    conversion_idempotency_key: Mapped[str | None] = mapped_column(
        String(120), nullable=True
    )

    items: Mapped[list["CustomerContractItem"]] = relationship(
        back_populates="contract", cascade="all, delete-orphan", order_by="CustomerContractItem.line_no"
    )
    converted_order: Mapped["Order | None"] = relationship(
        foreign_keys=[converted_order_id], uselist=False
    )


class CustomerContractItem(Base):
    __tablename__ = "customer_contract_items"
    __table_args__ = (
        UniqueConstraint(
            "contract_id", "line_no", name="uq_customer_contract_items_line"
        ),
        CheckConstraint("line_no >= 1", name="ck_customer_contract_items_line_no"),
        CheckConstraint("quantity > 0", name="ck_customer_contract_items_quantity"),
        CheckConstraint("unit_price >= 0", name="ck_customer_contract_items_unit_price"),
        CheckConstraint("subtotal >= 0", name="ck_customer_contract_items_subtotal"),
        Index("ix_customer_contract_items_product_id", "product_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    contract_id: Mapped[int] = mapped_column(
        ForeignKey("customer_contracts.id", ondelete="CASCADE"), nullable=False
    )
    line_no: Mapped[int] = mapped_column(Integer, nullable=False)
    product_id: Mapped[int | None] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"), nullable=True
    )
    product_code: Mapped[str | None] = mapped_column(String(150), nullable=True)
    product_name: Mapped[str] = mapped_column(String(250), nullable=False)
    specification: Mapped[str | None] = mapped_column(String(250), nullable=True)
    box_style: Mapped[str | None] = mapped_column(String(150), nullable=True)
    length_mm: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    width_mm: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    height_mm: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    material_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    flute_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    subtotal: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    remarks: Mapped[str | None] = mapped_column(Text, nullable=True)

    contract: Mapped["CustomerContract"] = relationship(back_populates="items")
