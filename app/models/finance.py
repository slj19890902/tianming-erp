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
    from app.models.delivery import Delivery, DeliveryItem


class StatementMonthlySequence(Base):
    __tablename__ = "statement_monthly_sequences"

    statement_month: Mapped[str] = mapped_column(String(7), primary_key=True)
    last_value: Mapped[int] = mapped_column(Integer, nullable=False)


class ReturnReceipt(Base):
    __tablename__ = "finance_return_receipts"
    __table_args__ = (
        CheckConstraint(
            "status IN ('confirmed', 'cancelled')",
            name="ck_finance_return_receipts_status",
        ),
        UniqueConstraint(
            "delivery_id",
            name="uq_finance_return_receipts_delivery",
        ),
        Index(
            "ix_finance_return_receipts_received_date",
            "actual_received_date",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    delivery_id: Mapped[int] = mapped_column(
        ForeignKey("sales_deliveries.id", ondelete="RESTRICT"),
        nullable=False,
    )
    actual_received_date: Mapped[date] = mapped_column(Date, nullable=False)
    signed_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    status: Mapped[str] = mapped_column(
        String(20),
        default="confirmed",
        nullable=False,
    )
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.current_timestamp(),
        nullable=False,
    )

    delivery: Mapped["Delivery"] = relationship()
    items: Mapped[list["ReturnReceiptItem"]] = relationship(
        back_populates="return_receipt",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="ReturnReceiptItem.id",
    )


class ReturnReceiptItem(Base):
    __tablename__ = "finance_return_receipt_items"
    __table_args__ = (
        CheckConstraint(
            "actual_received_quantity >= 0",
            name="ck_finance_return_receipt_items_quantity",
        ),
        CheckConstraint(
            "resolution_action IS NULL OR resolution_action IN "
            "('continue_delivery','accept_short','accept_over')",
            name="ck_finance_return_receipt_items_resolution_action",
        ),
        UniqueConstraint(
            "delivery_item_id",
            name="uq_finance_return_receipt_items_delivery_item",
        ),
        Index(
            "ix_finance_return_receipt_items_receipt_id",
            "return_receipt_id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    return_receipt_id: Mapped[int] = mapped_column(
        ForeignKey("finance_return_receipts.id", ondelete="CASCADE"),
        nullable=False,
    )
    delivery_item_id: Mapped[int] = mapped_column(
        ForeignKey("sales_delivery_items.id", ondelete="RESTRICT"),
        nullable=False,
    )
    actual_received_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    resolution_action: Mapped[str | None] = mapped_column(String(30), nullable=True)
    difference_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.current_timestamp(),
        nullable=False,
    )

    return_receipt: Mapped["ReturnReceipt"] = relationship(back_populates="items")
    delivery_item: Mapped["DeliveryItem"] = relationship()


class Statement(Base):
    __tablename__ = "finance_statements"
    __table_args__ = (
        CheckConstraint(
            "status IN ('unsettled', 'settled')",
            name="ck_finance_statements_status",
        ),
        UniqueConstraint(
            "statement_number",
            name="uq_finance_statements_number",
        ),
        Index(
            "ix_finance_statements_customer_month",
            "customer_id",
            "statement_month",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    statement_number: Mapped[str] = mapped_column(String(40), nullable=False)
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id", ondelete="RESTRICT"),
        nullable=False,
    )
    statement_month: Mapped[str] = mapped_column(String(7), nullable=False)
    total_receivable: Mapped[Decimal] = mapped_column(
        Numeric(14, 2),
        default=Decimal("0"),
        nullable=False,
    )
    total_gross_profit: Mapped[Decimal] = mapped_column(
        Numeric(14, 2),
        default=Decimal("0"),
        nullable=False,
    )
    invoiced_amount: Mapped[Decimal] = mapped_column(
        Numeric(14, 2),
        default=Decimal("0"),
        nullable=False,
    )
    settled_amount: Mapped[Decimal] = mapped_column(
        Numeric(14, 2),
        default=Decimal("0"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(20),
        default="unsettled",
        nullable=False,
    )
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.current_timestamp(),
        nullable=False,
    )

    items: Mapped[list["StatementItem"]] = relationship(
        back_populates="statement",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="StatementItem.id",
    )
    invoices: Mapped[list["Invoice"]] = relationship(
        back_populates="statement",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="Invoice.id",
    )
    settlements: Mapped[list["SettlementRecord"]] = relationship(
        back_populates="statement",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="SettlementRecord.id",
    )


class StatementItem(Base):
    __tablename__ = "finance_statement_items"
    __table_args__ = (
        UniqueConstraint(
            "return_receipt_item_id",
            name="uq_finance_statement_items_receipt_item",
        ),
        Index("ix_finance_statement_items_statement_id", "statement_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    statement_id: Mapped[int] = mapped_column(
        ForeignKey("finance_statements.id", ondelete="CASCADE"),
        nullable=False,
    )
    return_receipt_item_id: Mapped[int] = mapped_column(
        ForeignKey("finance_return_receipt_items.id", ondelete="RESTRICT"),
        nullable=False,
    )
    actual_received_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price_snapshot: Mapped[Decimal] = mapped_column(
        Numeric(12, 4),
        nullable=False,
    )
    unit_cost_snapshot: Mapped[Decimal] = mapped_column(
        Numeric(12, 4),
        nullable=False,
    )
    receivable_amount: Mapped[Decimal] = mapped_column(
        Numeric(14, 2),
        nullable=False,
    )
    gross_profit_amount: Mapped[Decimal] = mapped_column(
        Numeric(14, 2),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.current_timestamp(),
        nullable=False,
    )

    statement: Mapped["Statement"] = relationship(back_populates="items")
    return_receipt_item: Mapped["ReturnReceiptItem"] = relationship()


class Invoice(Base):
    __tablename__ = "finance_invoices"
    __table_args__ = (
        CheckConstraint(
            "invoice_amount > 0",
            name="ck_finance_invoices_amount_positive",
        ),
        UniqueConstraint("invoice_number", name="uq_finance_invoices_number"),
        Index("ix_finance_invoices_statement_id", "statement_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    statement_id: Mapped[int] = mapped_column(
        ForeignKey("finance_statements.id", ondelete="RESTRICT"),
        nullable=False,
    )
    invoice_number: Mapped[str] = mapped_column(String(80), nullable=False)
    invoice_date: Mapped[date] = mapped_column(Date, nullable=False)
    invoice_amount: Mapped[Decimal] = mapped_column(
        Numeric(14, 2),
        nullable=False,
    )
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.current_timestamp(),
        nullable=False,
    )

    statement: Mapped["Statement"] = relationship(back_populates="invoices")


class SettlementRecord(Base):
    __tablename__ = "finance_settlement_records"
    __table_args__ = (
        CheckConstraint(
            "settled_amount > 0",
            name="ck_finance_settlement_records_amount_positive",
        ),
        Index("ix_finance_settlement_records_statement_id", "statement_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    statement_id: Mapped[int] = mapped_column(
        ForeignKey("finance_statements.id", ondelete="RESTRICT"),
        nullable=False,
    )
    settled_amount: Mapped[Decimal] = mapped_column(
        Numeric(14, 2),
        nullable=False,
    )
    settlement_date: Mapped[date] = mapped_column(Date, nullable=False)
    account: Mapped[str] = mapped_column(String(120), nullable=False)
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.current_timestamp(),
        nullable=False,
    )

    statement: Mapped["Statement"] = relationship(back_populates="settlements")
