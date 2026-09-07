from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
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
            "document_revision >= 1",
            name="ck_supplier_monthly_statements_document_revision",
        ),
        CheckConstraint(
            "settlement_day_snapshot BETWEEN 1 AND 31",
            name="ck_supplier_monthly_statements_settlement_day",
        ),
        CheckConstraint(
            "generation_origin IN ('legacy','automatic','manual','regenerate')",
            name="ck_supplier_monthly_statements_generation_origin",
        ),
        CheckConstraint(
            "((generation_origin = 'regenerate' AND supersedes_statement_id IS NOT NULL "
            "AND document_revision > 1) OR "
            "(generation_origin <> 'regenerate' AND supersedes_statement_id IS NULL "
            "AND document_revision = 1)) "
            "AND (source_hash IS NULL OR length(source_hash) = 64)",
            name="ck_supplier_monthly_statements_revision_source",
        ),
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
            "uq_supplier_monthly_statements_business_revision",
            "supplier_id",
            "settlement_month",
            "currency",
            "tax_basis",
            "document_revision",
            unique=True,
            sqlite_where=text("generation_origin <> 'legacy'"),
            postgresql_where=text("generation_origin <> 'legacy'"),
        ),
        Index(
            "uq_supplier_monthly_statements_superseded_once",
            "supersedes_statement_id",
            unique=True,
            sqlite_where=text("supersedes_statement_id IS NOT NULL"),
            postgresql_where=text("supersedes_statement_id IS NOT NULL"),
        ),
        Index(
            "uq_supplier_monthly_statements_id_supplier",
            "id",
            "supplier_id",
            unique=True,
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
    document_revision: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    settlement_day_snapshot: Mapped[int] = mapped_column(
        Integer, nullable=False, default=20, server_default="20"
    )
    generation_origin: Mapped[str] = mapped_column(
        String(20), nullable=False, default="manual", server_default="legacy"
    )
    source_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    supersedes_statement_id: Mapped[int | None] = mapped_column(
        ForeignKey("supplier_monthly_statements.id", ondelete="RESTRICT"),
        nullable=True,
    )
    supersede_reason: Mapped[str | None] = mapped_column(String(120), nullable=True)
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


class SupplierReceiptSettlementPriceFact(Base):
    """Immutable supplier price snapshot for one posted receipt item."""

    __tablename__ = "supplier_receipt_settlement_price_facts"
    __table_args__ = (
        CheckConstraint(
            "fact_origin IN ('receipt_frozen','historical_master_adoption',"
            "'historical_document_confirmation')",
            name="ck_supplier_receipt_price_facts_origin",
        ),
        CheckConstraint(
            "source_kind IN "
            "('supplier_order_item','requisition_item','stock_replenishment_item')",
            name="ck_supplier_receipt_price_facts_source",
        ),
        CheckConstraint(
            "match_strategy IN "
            "('purchase_receipt_fact','stable_material_id',"
            "'supplier_unique_material_code','owner_authorized_finance_test_classification')",
            name="ck_supplier_receipt_price_facts_match",
        ),
        CheckConstraint(
            "finance_only_test_classification IN (0,1) AND "
            "(finance_only_test_classification = 0 OR "
            "(fact_origin = 'historical_master_adoption' AND "
            "match_strategy = 'owner_authorized_finance_test_classification'))",
            name="ck_supplier_receipt_price_facts_finance_only_test",
        ),
        CheckConstraint(
            "unit_price > 0 AND source_material_version >= 1 "
            "AND price_unit IN ('per_sheet','per_square_meter')",
            name="ck_supplier_receipt_price_facts_price",
        ),
        CheckConstraint(
            "received_quantity_snapshot > 0 AND report_length_mm > 0 "
            "AND report_width_mm > 0 AND quantity_unit = '张'",
            name="ck_supplier_receipt_price_facts_receipt",
        ),
        CheckConstraint(
            "currency = 'CNY' AND tax_included AND tax_rate = 0.13 "
            "AND shipping_fee_mode = 'included'",
            name="ck_supplier_receipt_price_facts_tax",
        ),
        CheckConstraint(
            "((fact_origin = 'receipt_frozen' AND adoption_reason IS NULL "
            "AND adoption_evidence_reference IS NULL) OR "
            "(fact_origin = 'historical_master_adoption' AND "
            "adoption_reason IS NOT NULL AND "
            "adoption_reason = '2026-09-02 老板确认采用当前主数据' "
            "AND adoption_evidence_reference IS NOT NULL "
            "AND length(trim(adoption_evidence_reference)) > 0) OR "
            "(fact_origin = 'historical_document_confirmation' AND "
            "adoption_reason IS NOT NULL AND length(trim(adoption_reason)) > 0 "
            "AND adoption_evidence_reference IS NOT NULL "
            "AND length(trim(adoption_evidence_reference)) > 0))",
            name="ck_supplier_receipt_price_facts_adoption",
        ),
        CheckConstraint(
            "length(trim(supplier_name_snapshot)) > 0 "
            "AND length(trim(material_code_snapshot)) > 0 "
            "AND length(trim(purchase_document_number_snapshot)) > 0 "
            "AND length(trim(receipt_number_snapshot)) > 0 "
            "AND length(trim(currency)) = 3 "
            "AND length(source_hash) = 64",
            name="ck_supplier_receipt_price_facts_text",
        ),
        UniqueConstraint(
            "incoming_receipt_item_id",
            name="uq_supplier_receipt_price_facts_item",
        ),
        Index(
            "ix_supplier_receipt_price_facts_supplier",
            "supplier_id",
            "created_at",
        ),
        Index(
            "ix_supplier_receipt_price_facts_material",
            "material_id",
            "created_at",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    incoming_receipt_item_id: Mapped[int] = mapped_column(
        ForeignKey(
            "incoming_receipt_items.id",
            ondelete="RESTRICT",
            name="fk_supplier_receipt_price_facts_item",
        ),
        nullable=False,
    )
    supplier_id: Mapped[int] = mapped_column(
        ForeignKey(
            "supplier_master_records.id",
            ondelete="RESTRICT",
            name="fk_supplier_receipt_price_facts_supplier",
        ),
        nullable=False,
    )
    supplier_name_snapshot: Mapped[str] = mapped_column(String(200), nullable=False)
    material_id: Mapped[int] = mapped_column(
        ForeignKey(
            "materials.id",
            ondelete="RESTRICT",
            name="fk_supplier_receipt_price_facts_material",
        ),
        nullable=False,
    )
    material_code_snapshot: Mapped[str] = mapped_column(String(200), nullable=False)
    source_material_version: Mapped[int] = mapped_column(Integer, nullable=False)
    source_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    purchase_document_number_snapshot: Mapped[str] = mapped_column(
        String(80), nullable=False
    )
    receipt_number_snapshot: Mapped[str] = mapped_column(String(80), nullable=False)
    receipt_date_snapshot: Mapped[date] = mapped_column(Date, nullable=False)
    received_quantity_snapshot: Mapped[Decimal] = mapped_column(
        Numeric(18, 6), nullable=False
    )
    quantity_unit: Mapped[str] = mapped_column(String(20), nullable=False)
    report_length_mm: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)
    report_width_mm: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    price_unit: Mapped[str] = mapped_column(String(30), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    tax_included: Mapped[bool] = mapped_column(Boolean, nullable=False)
    tax_rate: Mapped[Decimal] = mapped_column(Numeric(8, 6), nullable=False)
    shipping_fee_mode: Mapped[str] = mapped_column(String(20), nullable=False)
    fact_origin: Mapped[str] = mapped_column(String(40), nullable=False)
    match_strategy: Mapped[str] = mapped_column(String(50), nullable=False)
    finance_only_test_classification: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    adoption_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    adoption_evidence_reference: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    created_by: Mapped[int] = mapped_column(
        ForeignKey(
            "users.id",
            ondelete="RESTRICT",
            name="fk_supplier_receipt_price_facts_creator",
        ),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )

    statement_lines: Mapped[list["SupplierMonthlyStatementLine"]] = relationship(
        back_populates="supplier_receipt_price_fact"
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
        CheckConstraint(
            "supplier_receipt_price_fact_id IS NULL OR "
            "(source_type = 'paperboard' AND incoming_receipt_item_id IS NOT NULL)",
            name="ck_supplier_monthly_statement_lines_price_fact",
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
        Index(
            "ix_supplier_monthly_statement_lines_price_fact",
            "supplier_receipt_price_fact_id",
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
    supplier_receipt_price_fact_id: Mapped[int | None] = mapped_column(
        ForeignKey(
            "supplier_receipt_settlement_price_facts.id",
            ondelete="RESTRICT",
            name="fk_supplier_statement_lines_receipt_price_fact",
        ),
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
    supplier_receipt_price_fact: Mapped[
        SupplierReceiptSettlementPriceFact | None
    ] = relationship(back_populates="statement_lines")


class SupplierMonthlyAdjustment(Base):
    __tablename__ = "supplier_monthly_adjustments"
    __table_args__ = (
        CheckConstraint(
            "difference_type IN ('price','quantity','tax_rounding','other')",
            name="ck_supplier_monthly_adjustments_type",
        ),
        CheckConstraint("amount <> 0", name="ck_supplier_monthly_adjustments_amount"),
        CheckConstraint(
            "(is_post_confirmation IS FALSE "
            "AND statement_version_before IS NULL AND amount_before IS NULL "
            "AND amount_after IS NULL) OR "
            "(statement_version_before >= 1 AND amount_before IS NOT NULL "
            "AND amount_after IS NOT NULL "
            "AND abs(amount_after - (amount_before + amount)) < 0.005)",
            name="ck_supplier_monthly_adjustments_audit",
        ),
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
    statement_version_before: Mapped[int | None] = mapped_column(Integer, nullable=True)
    amount_before: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    amount_after: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    is_post_confirmation: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
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


class SupplierPaymentBatch(Base):
    """One operator action that settles a statement with credit, acceptance and bank."""

    __tablename__ = "supplier_payment_batches"
    __table_args__ = (
        CheckConstraint(
            "credit_applied_amount >= 0 AND acceptance_face_amount >= 0 "
            "AND acceptance_applied_amount >= 0 AND bank_amount >= 0 "
            "AND credit_created_amount >= 0 AND settled_amount > 0",
            name="ck_supplier_payment_batches_amounts",
        ),
        CheckConstraint(
            "acceptance_applied_amount <= acceptance_face_amount",
            name="ck_supplier_payment_batches_acceptance_amount",
        ),
        CheckConstraint(
            "(acceptance_note_id IS NULL AND acceptance_face_amount = 0 "
            "AND acceptance_applied_amount = 0 AND credit_created_amount = 0) OR "
            "(acceptance_note_id IS NOT NULL AND acceptance_face_amount > 0 "
            "AND acceptance_applied_amount > 0)",
            name="ck_supplier_payment_batches_acceptance_link",
        ),
        CheckConstraint(
            "abs(settled_amount - (credit_applied_amount + "
            "acceptance_applied_amount + bank_amount)) < 0.005 "
            "AND abs(credit_created_amount - (acceptance_face_amount - "
            "acceptance_applied_amount)) < 0.005 "
            "AND (credit_created_amount = 0 OR bank_amount = 0)",
            name="ck_supplier_payment_batches_balance",
        ),
        UniqueConstraint(
            "acceptance_note_id", name="uq_supplier_payment_batches_acceptance"
        ),
        UniqueConstraint(
            "id", "statement_id", name="uq_supplier_payment_batches_id_statement"
        ),
        UniqueConstraint(
            "id",
            "acceptance_note_id",
            "statement_id",
            "acceptance_applied_amount",
            name="uq_supplier_payment_batches_acceptance_payment",
        ),
        UniqueConstraint(
            "id",
            "supplier_id",
            "statement_id",
            "acceptance_note_id",
            "credit_created_amount",
            name="uq_supplier_payment_batches_credit_source",
        ),
        ForeignKeyConstraint(
            ["statement_id", "supplier_id"],
            ["supplier_monthly_statements.id", "supplier_monthly_statements.supplier_id"],
            name="fk_supplier_payment_batches_statement_supplier",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["acceptance_note_id", "supplier_id", "statement_id", "acceptance_face_amount"],
            [
                "finance_acceptance_notes.id",
                "finance_acceptance_notes.supplier_id",
                "finance_acceptance_notes.supplier_statement_id",
                "finance_acceptance_notes.amount",
            ],
            name="fk_supplier_payment_batches_acceptance_supplier",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        Index("ix_supplier_payment_batches_statement", "statement_id", "payment_date"),
        Index("ix_supplier_payment_batches_supplier", "supplier_id", "payment_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    statement_id: Mapped[int] = mapped_column(
        ForeignKey("supplier_monthly_statements.id", ondelete="RESTRICT"), nullable=False
    )
    supplier_id: Mapped[int] = mapped_column(
        ForeignKey("supplier_master_records.id", ondelete="RESTRICT"), nullable=False
    )
    acceptance_note_id: Mapped[int | None] = mapped_column(
        ForeignKey("finance_acceptance_notes.id", ondelete="RESTRICT"), nullable=True
    )
    payment_date: Mapped[date] = mapped_column(Date, nullable=False)
    credit_applied_amount: Mapped[Decimal] = mapped_column(
        Numeric(18, 2), nullable=False, default=0, server_default="0"
    )
    acceptance_face_amount: Mapped[Decimal] = mapped_column(
        Numeric(18, 2), nullable=False, default=0, server_default="0"
    )
    acceptance_applied_amount: Mapped[Decimal] = mapped_column(
        Numeric(18, 2), nullable=False, default=0, server_default="0"
    )
    bank_amount: Mapped[Decimal] = mapped_column(
        Numeric(18, 2), nullable=False, default=0, server_default="0"
    )
    credit_created_amount: Mapped[Decimal] = mapped_column(
        Numeric(18, 2), nullable=False, default=0, server_default="0"
    )
    settled_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    bank_reference: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )


class SupplierCreditLot(Base):
    """Auditable supplier-specific credit created by an over-endorsed acceptance."""

    __tablename__ = "supplier_credit_lots"
    __table_args__ = (
        CheckConstraint(
            "source_type IN ('acceptance_overpayment','statement_adjustment')",
            name="ck_supplier_credit_lots_source_type",
        ),
        CheckConstraint(
            "original_amount > 0 AND available_amount >= 0 "
            "AND available_amount <= original_amount",
            name="ck_supplier_credit_lots_amounts",
        ),
        CheckConstraint(
            "status IN ('available','partial','exhausted','voided')",
            name="ck_supplier_credit_lots_status",
        ),
        CheckConstraint(
            "(source_type = 'acceptance_overpayment' "
            "AND source_acceptance_note_id IS NOT NULL "
            "AND source_payment_batch_id IS NOT NULL) OR "
            "(source_type = 'statement_adjustment' "
            "AND source_acceptance_note_id IS NULL "
            "AND source_payment_batch_id IS NULL)",
            name="ck_supplier_credit_lots_source_link",
        ),
        CheckConstraint(
            "(status = 'available' AND available_amount = original_amount) OR "
            "(status = 'partial' AND available_amount > 0 "
            "AND available_amount < original_amount) OR "
            "(status = 'exhausted' AND available_amount = 0) OR status = 'voided'",
            name="ck_supplier_credit_lots_balance_status",
        ),
        CheckConstraint("version >= 1", name="ck_supplier_credit_lots_version"),
        UniqueConstraint(
            "source_acceptance_note_id",
            name="uq_supplier_credit_lots_source_acceptance",
        ),
        ForeignKeyConstraint(
            ["source_statement_id", "supplier_id"],
            ["supplier_monthly_statements.id", "supplier_monthly_statements.supplier_id"],
            name="fk_supplier_credit_lots_statement_supplier",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            [
                "source_payment_batch_id",
                "supplier_id",
                "source_statement_id",
                "source_acceptance_note_id",
                "original_amount",
            ],
            [
                "supplier_payment_batches.id",
                "supplier_payment_batches.supplier_id",
                "supplier_payment_batches.statement_id",
                "supplier_payment_batches.acceptance_note_id",
                "supplier_payment_batches.credit_created_amount",
            ],
            name="fk_supplier_credit_lots_acceptance_source",
            ondelete="RESTRICT",
        ),
        Index("ix_supplier_credit_lots_supplier", "supplier_id", "status", "id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    supplier_id: Mapped[int] = mapped_column(
        ForeignKey("supplier_master_records.id", ondelete="RESTRICT"), nullable=False
    )
    supplier_name_snapshot: Mapped[str] = mapped_column(String(200), nullable=False)
    source_type: Mapped[str] = mapped_column(String(40), nullable=False)
    source_acceptance_note_id: Mapped[int | None] = mapped_column(
        ForeignKey("finance_acceptance_notes.id", ondelete="RESTRICT"), nullable=True
    )
    source_statement_id: Mapped[int] = mapped_column(
        ForeignKey("supplier_monthly_statements.id", ondelete="RESTRICT"), nullable=False
    )
    source_payment_batch_id: Mapped[int | None] = mapped_column(
        ForeignKey("supplier_payment_batches.id", ondelete="RESTRICT"), nullable=True
    )
    original_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    available_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="available", server_default="available"
    )
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )


class SupplierMonthlyPayment(Base):
    __tablename__ = "supplier_monthly_payments"
    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_supplier_monthly_payments_amount"),
        CheckConstraint(
            "payment_method IN ('bank','acceptance','credit')",
            name="ck_supplier_monthly_payments_method",
        ),
        CheckConstraint(
            "(payment_method = 'bank' AND acceptance_note_id IS NULL "
            "AND supplier_credit_id IS NULL) OR "
            "(payment_method = 'acceptance' AND acceptance_note_id IS NOT NULL "
            "AND supplier_credit_id IS NULL) OR "
            "(payment_method = 'credit' AND acceptance_note_id IS NULL "
            "AND supplier_credit_id IS NOT NULL AND payment_batch_id IS NOT NULL)",
            name="ck_supplier_monthly_payments_acceptance_link",
        ),
        UniqueConstraint(
            "acceptance_note_id", name="uq_supplier_monthly_payments_acceptance"
        ),
        ForeignKeyConstraint(
            ["payment_batch_id", "statement_id"],
            ["supplier_payment_batches.id", "supplier_payment_batches.statement_id"],
            name="fk_supplier_monthly_payments_batch_statement",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["payment_batch_id", "acceptance_note_id", "statement_id", "amount"],
            [
                "supplier_payment_batches.id",
                "supplier_payment_batches.acceptance_note_id",
                "supplier_payment_batches.statement_id",
                "supplier_payment_batches.acceptance_applied_amount",
            ],
            name="fk_supplier_monthly_payments_acceptance_batch",
            ondelete="RESTRICT",
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
    payment_batch_id: Mapped[int | None] = mapped_column(
        ForeignKey("supplier_payment_batches.id", ondelete="RESTRICT"), nullable=True
    )
    supplier_credit_id: Mapped[int | None] = mapped_column(
        ForeignKey("supplier_credit_lots.id", ondelete="RESTRICT"), nullable=True
    )
    reference: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )

    statement: Mapped[SupplierMonthlyStatement] = relationship(back_populates="payments")
