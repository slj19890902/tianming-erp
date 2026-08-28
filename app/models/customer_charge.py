from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base


class CustomerCharge(Base):
    """A customer receivable source that is not a delivered inventory item."""

    __tablename__ = "finance_customer_charges"
    __table_args__ = (
        CheckConstraint(
            "charge_type IN "
            "('mold','printing_plate','sample','setup','freight','other')",
            name="ck_finance_customer_charges_type",
        ),
        CheckConstraint(
            "status IN ('draft','confirmed','cancelled')",
            name="ck_finance_customer_charges_status",
        ),
        CheckConstraint("quantity > 0", name="ck_finance_customer_charges_quantity"),
        CheckConstraint("unit_price >= 0", name="ck_finance_customer_charges_unit_price"),
        CheckConstraint("amount > 0", name="ck_finance_customer_charges_amount"),
        CheckConstraint("version >= 1", name="ck_finance_customer_charges_version"),
        CheckConstraint(
            "price_tax_mode IS NULL OR "
            "price_tax_mode IN ('tax_inclusive','tax_exclusive')",
            name="ck_finance_customer_charges_price_tax_mode",
        ),
        CheckConstraint(
            "tax_rate IS NULL OR (tax_rate >= 0 AND tax_rate <= 1)",
            name="ck_finance_customer_charges_tax_rate",
        ),
        CheckConstraint(
            "((status = 'draft' AND reconciliation_month IS NULL "
            "AND confirmed_by IS NULL AND confirmed_at IS NULL) OR "
            "(status = 'confirmed' AND reconciliation_month IS NOT NULL "
            "AND confirmed_by IS NOT NULL AND confirmed_at IS NOT NULL) OR "
            "(status = 'cancelled'))",
            name="ck_finance_customer_charges_confirmation_state",
        ),
        Index(
            "ix_finance_customer_charges_customer_month_status",
            "customer_id",
            "reconciliation_month",
            "status",
        ),
        Index("ix_finance_customer_charges_order", "order_id"),
        Index("ix_finance_customer_charges_mold", "mold_tool_id"),
        Index("ix_finance_customer_charges_plate", "printing_plate_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False
    )
    order_id: Mapped[int] = mapped_column(
        ForeignKey("sales_orders.id", ondelete="RESTRICT"), nullable=False
    )
    order_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("sales_order_items.id", ondelete="RESTRICT"), nullable=True
    )
    mold_tool_id: Mapped[int | None] = mapped_column(
        ForeignKey("mold_tools.id", ondelete="RESTRICT"), nullable=True
    )
    printing_plate_id: Mapped[int | None] = mapped_column(
        ForeignKey("printing_plates.id", ondelete="RESTRICT"), nullable=True
    )
    charge_type: Mapped[str] = mapped_column(String(30), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(14, 4), nullable=False)
    unit: Mapped[str] = mapped_column(String(40), nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(14, 4), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    price_tax_mode: Mapped[str | None] = mapped_column(String(30), nullable=True)
    tax_rate: Mapped[Decimal | None] = mapped_column(Numeric(6, 4), nullable=True)
    tax_project_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    tax_classification_code: Mapped[str | None] = mapped_column(
        String(80), nullable=True
    )
    reconciliation_month: Mapped[str | None] = mapped_column(String(7), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), default="draft", server_default="draft", nullable=False
    )
    version: Mapped[int] = mapped_column(
        Integer, default=1, server_default="1", nullable=False
    )
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    updated_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    confirmed_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    cancelled_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime, onupdate=func.current_timestamp(), nullable=True
    )
