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
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models import Base


class SupplierMonthlyStatement(Base):
    """One supplier, one closed 21st-to-20th settlement period."""

    __tablename__ = "supplier_monthly_statements"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft','difference','confirmed_pending_invoice',"
            "'invoiced_pending_payment','partial_payment','paid','voided')",
            name="ck_supplier_monthly_statements_status",
        ),
        CheckConstraint(
            "tax_basis IN ('tax_inclusive','tax_exclusive')",
            name="ck_supplier_monthly_statements_tax_basis",
        ),
        CheckConstraint(
            "period_start <= period_end",
            name="ck_supplier_monthly_statements_period",
        ),
        CheckConstraint(
            "erp_amount >= 0 AND adjusted_amount >= 0 "
            "AND invoice_allocated_amount >= 0 AND paid_amount >= 0",
            name="ck_supplier_monthly_statements_amounts",
        ),
        CheckConstraint(
            "supplier_statement_amount IS NULL OR supplier_statement_amount >= 0",
            name="ck_supplier_monthly_statements_supplier_amount",
        ),
        CheckConstraint(
            "confirmed_amount IS NULL OR confirmed_amount > 0",
            name="ck_supplier_monthly_statements_confirmed_amount",
        ),
        CheckConstraint("version >= 1", name="ck_supplier_monthly_statements_version"),
        CheckConstraint(
            "active_guard IS NULL OR active_guard = 1",
            name="ck_supplier_monthly_statements_active_guard",
        ),
        UniqueConstraint(
            "statement_number", name="uq_supplier_monthly_statements_number"
        ),
        UniqueConstraint(
            "supplier_id",
            "settlement_month",
            "currency",
            "tax_basis",
            "active_guard",
            name="uq_supplier_monthly_statements_active_period",
        ),
        Index(
            "ix_supplier_monthly_statements_month_status",
            "settlement_month",
            "status",
        ),
        Index(
            "ix_supplier_monthly_statements_supplier",
            "supplier_id",
            "period_end",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    statement_number: Mapped[str] = mapped_column(String(60), nullable=False)
    supplier_id: Mapped[int] = mapped_column(
        ForeignKey("supplier_master_records.id", ondelete="RESTRICT"), nullable=False
    )
    supplier_name_snapshot: Mapped[str] = mapped_column(String(200), nullable=False)
    settlement_month: Mapped[str] = mapped_column(String(7), nullable=False)
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    tax_basis: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(
        String(40), nullable=False, default="draft", server_default="draft"
    )
    active_guard: Mapped[int | None] = mapped_column(
        Integer, nullable=True, default=1, server_default="1"
    )
    erp_amount: Mapped[Decimal] = mapped_column(
        Numeric(18, 2), nullable=False, default=0, server_default="0"
    )
    adjustment_amount: Mapped[Decimal] = mapped_column(
        Numeric(18, 2), nullable=False, default=0, server_default="0"
    )
    adjusted_amount: Mapped[Decimal] = mapped_column(
        Numeric(18, 2), nullable=False, default=0, server_default="0"
    )
    supplier_statement_number: Mapped[str | None] = mapped_column(
        String(120), nullable=True
    )
    supplier_statement_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    supplier_statement_amount: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 2), nullable=True
    )
    confirmed_amount: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 2), nullable=True
    )
    invoice_allocated_amount: Mapped[Decimal] = mapped_column(
        Numeric(18, 2), nullable=False, default=0, server_default="0"
    )
    paid_amount: Mapped[Decimal] = mapped_column(
        Numeric(18, 2), nullable=False, default=0, server_default="0"
    )
    finance_payable_id: Mapped[int | None] = mapped_column(
        ForeignKey("finance_payables.id", ondelete="SET NULL"), nullable=True
    )
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    generated_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    generated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )
    reviewed_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    confirmed_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    reopened_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    reopened_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    voided_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    voided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, onupdate=func.current_timestamp()
    )

    lines: Mapped[list["SupplierMonthlyStatementLine"]] = relationship(
        back_populates="statement", order_by="SupplierMonthlyStatementLine.id"
    )
    adjustments: Mapped[list["SupplierMonthlyAdjustment"]] = relationship(
        back_populates="statement", order_by="SupplierMonthlyAdjustment.id"
    )
    invoices: Mapped[list["SupplierMonthlyInvoice"]] = relationship(
        back_populates="statement", order_by="SupplierMonthlyInvoice.id"
    )
    payments: Mapped[list["SupplierMonthlyPayment"]] = relationship(
        back_populates="statement", order_by="SupplierMonthlyPayment.id"
    )


class SupplierMonthlyStatementLine(Base):
    """Frozen receipt and price facts used by one supplier statement draft."""

    __tablename__ = "supplier_monthly_statement_lines"
    __table_args__ = (
        CheckConstraint(
            "source_type IN ('paperboard','external_packaging')",
            name="ck_supplier_monthly_statement_lines_source_type",
        ),
        CheckConstraint(
            "((source_type = 'paperboard' AND incoming_receipt_item_id IS NOT NULL "
            "AND external_receipt_item_id IS NULL) OR "
            "(source_type = 'external_packaging' AND incoming_receipt_item_id IS NULL "
            "AND external_receipt_item_id IS NOT NULL))",
            name="ck_supplier_monthly_statement_lines_source_link",
        ),
        CheckConstraint(
            "received_quantity > 0 AND frozen_unit_price > 0 "
            "AND erp_amount >= 0 AND tax_amount >= 0",
            name="ck_supplier_monthly_statement_lines_amounts",
        ),
        CheckConstraint(
            "tax_basis IN ('tax_inclusive','tax_exclusive')",
            name="ck_supplier_monthly_statement_lines_tax_basis",
        ),
        CheckConstraint(
            "active_guard IS NULL OR active_guard = 1",
            name="ck_supplier_monthly_statement_lines_active_guard",
        ),
        UniqueConstraint(
            "statement_id",
            "source_key",
            name="uq_supplier_monthly_statement_lines_statement_source",
        ),
        UniqueConstraint(
            "source_key",
            "active_guard",
            name="uq_supplier_monthly_statement_lines_active_source",
        ),
        Index(
            "ix_supplier_monthly_statement_lines_statement",
            "statement_id",
            "active_guard",
        ),
        Index(
            "ix_supplier_monthly_statement_lines_receipt_date", "receipt_date"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    statement_id: Mapped[int] = mapped_column(
        ForeignKey("supplier_monthly_statements.id", ondelete="RESTRICT"),
        nullable=False,
    )
    source_type: Mapped[str] = mapped_column(String(30), nullable=False)
    source_key: Mapped[str] = mapped_column(String(100), nullable=False)
    incoming_receipt_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("incoming_receipt_items.id", ondelete="RESTRICT"), nullable=True
    )
    external_receipt_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("external_packaging_receipt_items.id", ondelete="RESTRICT"),
        nullable=True,
    )
    purchase_document_number: Mapped[str] = mapped_column(String(80), nullable=False)
    receipt_number: Mapped[str] = mapped_column(String(80), nullable=False)
    receipt_date: Mapped[date] = mapped_column(Date, nullable=False)
    category_label: Mapped[str] = mapped_column(String(80), nullable=False)
    specification_snapshot: Mapped[str | None] = mapped_column(
        String(500), nullable=True
    )
    material_or_product_snapshot: Mapped[str] = mapped_column(
        String(250), nullable=False
    )
    received_quantity: Mapped[Decimal] = mapped_column(
        Numeric(18, 6), nullable=False
    )
    quantity_unit: Mapped[str] = mapped_column(String(20), nullable=False)
    frozen_unit_price: Mapped[Decimal] = mapped_column(
        Numeric(20, 6), nullable=False
    )
    price_unit: Mapped[str] = mapped_column(String(30), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    tax_basis: Mapped[str] = mapped_column(String(20), nullable=False)
    tax_rate: Mapped[Decimal] = mapped_column(Numeric(8, 6), nullable=False)
    erp_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    tax_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    source_link: Mapped[str] = mapped_column(String(255), nullable=False)
    active_guard: Mapped[int | None] = mapped_column(
        Integer, nullable=True, default=1, server_default="1"
    )
    released_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )

    statement: Mapped[SupplierMonthlyStatement] = relationship(back_populates="lines")


class SupplierMonthlyAdjustment(Base):
    __tablename__ = "supplier_monthly_adjustments"
    __table_args__ = (
        CheckConstraint(
            "difference_type IN ('price','quantity','tax_rounding','other')",
            name="ck_supplier_monthly_adjustments_type",
        ),
        CheckConstraint("amount <> 0", name="ck_supplier_monthly_adjustments_amount"),
        Index("ix_supplier_monthly_adjustments_statement", "statement_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    statement_id: Mapped[int] = mapped_column(
        ForeignKey("supplier_monthly_statements.id", ondelete="RESTRICT"),
        nullable=False,
    )
    statement_line_id: Mapped[int | None] = mapped_column(
        ForeignKey("supplier_monthly_statement_lines.id", ondelete="RESTRICT"),
        nullable=True,
    )
    difference_type: Mapped[str] = mapped_column(String(30), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )

    statement: Mapped[SupplierMonthlyStatement] = relationship(
        back_populates="adjustments"
    )


class SupplierMonthlyInvoice(Base):
    """Invoice amount allocated manually to one supplier settlement period."""

    __tablename__ = "supplier_monthly_invoices"
    __table_args__ = (
        CheckConstraint(
            "invoice_total_amount > 0 AND allocated_amount > 0 "
            "AND tax_amount >= 0 AND tax_amount <= allocated_amount",
            name="ck_supplier_monthly_invoices_amounts",
        ),
        UniqueConstraint(
            "statement_id",
            "invoice_number",
            name="uq_supplier_monthly_invoices_statement_number",
        ),
        Index("ix_supplier_monthly_invoices_statement", "statement_id"),
        Index(
            "ix_supplier_monthly_invoices_supplier_number",
            "supplier_id",
            "invoice_number",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    statement_id: Mapped[int] = mapped_column(
        ForeignKey("supplier_monthly_statements.id", ondelete="RESTRICT"),
        nullable=False,
    )
    supplier_id: Mapped[int] = mapped_column(
        ForeignKey("supplier_master_records.id", ondelete="RESTRICT"), nullable=False
    )
    invoice_number: Mapped[str] = mapped_column(String(120), nullable=False)
    invoice_date: Mapped[date] = mapped_column(Date, nullable=False)
    received_date: Mapped[date] = mapped_column(Date, nullable=False)
    invoice_total_amount: Mapped[Decimal] = mapped_column(
        Numeric(18, 2), nullable=False
    )
    allocated_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    tax_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    attachment_original_name: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    attachment_stored_name: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    attachment_content_hash: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    attachment_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    attached_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    attached_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )

    statement: Mapped[SupplierMonthlyStatement] = relationship(back_populates="invoices")


class SupplierMonthlyPayment(Base):
    __tablename__ = "supplier_monthly_payments"
    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_supplier_monthly_payments_amount"),
        CheckConstraint(
            "payment_method IN ('bank','acceptance')",
            name="ck_supplier_monthly_payments_method",
        ),
        CheckConstraint(
            "(payment_method = 'bank' AND acceptance_note_id IS NULL) OR "
            "(payment_method = 'acceptance' AND acceptance_note_id IS NOT NULL)",
            name="ck_supplier_monthly_payments_acceptance_link",
        ),
        UniqueConstraint(
            "acceptance_note_id", name="uq_supplier_monthly_payments_acceptance"
        ),
        Index("ix_supplier_monthly_payments_statement", "statement_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    statement_id: Mapped[int] = mapped_column(
        ForeignKey("supplier_monthly_statements.id", ondelete="RESTRICT"),
        nullable=False,
    )
    payment_date: Mapped[date] = mapped_column(Date, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    payment_method: Mapped[str] = mapped_column(
        String(20), nullable=False, default="bank", server_default="bank"
    )
    acceptance_note_id: Mapped[int | None] = mapped_column(
        ForeignKey("finance_acceptance_notes.id", ondelete="RESTRICT"), nullable=True
    )
    reference: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )

    statement: Mapped[SupplierMonthlyStatement] = relationship(back_populates="payments")
