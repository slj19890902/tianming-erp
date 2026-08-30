from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

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
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base


class FinancePayable(Base):
    """Small, auditable AP/expense register; it is not a general ledger."""

    __tablename__ = "finance_payables"
    __table_args__ = (
        CheckConstraint(
            "category IN ('material','outsourcing','freight','utilities','rent','wages','maintenance','tax_fee','other')",
            name="ck_finance_payables_category",
        ),
        CheckConstraint(
            "status IN ('draft','confirmed','paid','voided')",
            name="ck_finance_payables_status",
        ),
        CheckConstraint("amount > 0", name="ck_finance_payables_amount_positive"),
        CheckConstraint("version >= 1", name="ck_finance_payables_version"),
        UniqueConstraint("idempotency_key", name="uq_finance_payables_idempotency"),
        Index("ix_finance_payables_document_date", "document_date"),
        Index("ix_finance_payables_due_status", "due_date", "status"),
        Index("ix_finance_payables_supplier", "supplier_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    supplier_id: Mapped[int | None] = mapped_column(
        ForeignKey("supplier_master_records.id", ondelete="RESTRICT"), nullable=True
    )
    counterparty_name: Mapped[str] = mapped_column(String(200), nullable=False)
    category: Mapped[str] = mapped_column(String(30), nullable=False)
    document_number: Mapped[str | None] = mapped_column(String(100), nullable=True)
    document_date: Mapped[date] = mapped_column(Date, nullable=False)
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), default="draft", server_default="draft", nullable=False
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(120), nullable=False)
    version: Mapped[int] = mapped_column(
        Integer, default=1, server_default="1", nullable=False
    )
    confirmed_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    paid_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    paid_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    voided_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    voided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime, onupdate=func.current_timestamp(), nullable=True
    )
