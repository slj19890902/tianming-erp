from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
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
    from app.models.customer import Customer


class InvoiceSellerEntity(Base):
    __tablename__ = "invoice_seller_entities"
    __table_args__ = (
        CheckConstraint("version >= 1", name="ck_invoice_sellers_version"),
        CheckConstraint(
            "confirmation_status IN ('pending','confirmed')",
            name="ck_invoice_sellers_confirmation_status",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    seller_code: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    seller_name: Mapped[str] = mapped_column(String(200), nullable=False)
    tax_no: Mapped[str | None] = mapped_column(String(100), nullable=True)
    address: Mapped[str | None] = mapped_column(Text, nullable=True)
    phone: Mapped[str | None] = mapped_column(String(100), nullable=True)
    bank_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    bank_account: Mapped[str | None] = mapped_column(String(200), nullable=True)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    effective_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    confirmation_status: Mapped[str] = mapped_column(
        String(20), default="pending", server_default="pending", nullable=False
    )
    version: Mapped[int] = mapped_column(
        Integer, default=1, server_default="1", nullable=False
    )
    confirmed_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime, onupdate=func.current_timestamp(), nullable=True
    )


class FinanceSettlementEntity(Base):
    """Independent buyer/settlement master for consolidated customer billing."""

    __tablename__ = "finance_settlement_entities"
    __table_args__ = (
        CheckConstraint("version >= 1", name="ck_finance_settlement_entities_version"),
        CheckConstraint(
            "confirmation_status IN ('pending','confirmed')",
            name="ck_finance_settlement_entities_confirmation_status",
        ),
        CheckConstraint(
            "statement_cycle_start_day BETWEEN 1 AND 28",
            name="ck_finance_settlement_entities_cycle_day",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    entity_code: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    entity_name: Mapped[str] = mapped_column(String(200), nullable=False)
    short_name: Mapped[str | None] = mapped_column(String(50), nullable=True)
    tax_no: Mapped[str | None] = mapped_column(String(100), nullable=True)
    invoice_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    invoice_phone: Mapped[str | None] = mapped_column(String(100), nullable=True)
    bank_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    bank_account: Mapped[str | None] = mapped_column(String(200), nullable=True)
    default_seller_id: Mapped[int | None] = mapped_column(
        ForeignKey("invoice_seller_entities.id", ondelete="RESTRICT"), nullable=True
    )
    statement_cycle_start_day: Mapped[int] = mapped_column(
        Integer, default=20, server_default="20", nullable=False
    )
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1", nullable=False)
    confirmation_status: Mapped[str] = mapped_column(
        String(20), default="pending", server_default="pending", nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1", nullable=False)
    confirmed_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime, onupdate=func.current_timestamp(), nullable=True
    )


class CustomerInvoiceProfile(Base):
    __tablename__ = "customer_invoice_profiles"
    __table_args__ = (
        UniqueConstraint("customer_id", name="uq_customer_invoice_profiles_customer"),
        CheckConstraint("version >= 1", name="ck_customer_invoice_profiles_version"),
        CheckConstraint(
            "confirmation_status IN ('pending','confirmed')",
            name="ck_customer_invoice_profiles_confirmation_status",
        ),
        CheckConstraint(
            "invoice_type = 'digital_vat_special'",
            name="ck_customer_invoice_profiles_invoice_type",
        ),
        CheckConstraint(
            "price_tax_mode IN ('tax_inclusive','tax_exclusive')",
            name="ck_customer_invoice_profiles_price_tax_mode",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False
    )
    invoice_title: Mapped[str | None] = mapped_column(String(200), nullable=True)
    tax_no: Mapped[str | None] = mapped_column(String(100), nullable=True)
    invoice_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    invoice_phone: Mapped[str | None] = mapped_column(String(100), nullable=True)
    bank_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    bank_account: Mapped[str | None] = mapped_column(String(200), nullable=True)
    default_seller_id: Mapped[int | None] = mapped_column(
        ForeignKey("invoice_seller_entities.id", ondelete="RESTRICT"), nullable=True
    )
    settlement_entity_id: Mapped[int | None] = mapped_column(
        ForeignKey("finance_settlement_entities.id", ondelete="RESTRICT"), nullable=True
    )
    invoice_type: Mapped[str] = mapped_column(
        String(40), default="digital_vat_special", nullable=False
    )
    price_tax_mode: Mapped[str] = mapped_column(
        String(30), default="tax_inclusive", nullable=False
    )
    default_tax_rate: Mapped[Decimal | None] = mapped_column(
        Numeric(6, 4), nullable=True
    )
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    confirmation_status: Mapped[str] = mapped_column(
        String(20), default="pending", server_default="pending", nullable=False
    )
    version: Mapped[int] = mapped_column(
        Integer, default=1, server_default="1", nullable=False
    )
    confirmed_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime, onupdate=func.current_timestamp(), nullable=True
    )

    customer: Mapped["Customer"] = relationship(back_populates="invoice_profile")


class CustomerInvoiceItemRule(Base):
    __tablename__ = "customer_invoice_item_rules"
    __table_args__ = (
        UniqueConstraint(
            "customer_id", "product_id", "version",
            name="uq_customer_invoice_item_rules_version",
        ),
        CheckConstraint("version >= 1", name="ck_customer_invoice_item_rules_version"),
        CheckConstraint(
            "confirmation_status IN ('pending','confirmed')",
            name="ck_customer_invoice_item_rules_confirmation_status",
        ),
        CheckConstraint(
            "spec_source IN ('product_snapshot','product_code_snapshot','blank')",
            name="ck_customer_invoice_item_rules_spec_source",
        ),
        CheckConstraint(
            "quantity_source = 'statement_received_quantity'",
            name="ck_customer_invoice_item_rules_quantity_source",
        ),
        CheckConstraint(
            "amount_source = 'statement_receivable_amount'",
            name="ck_customer_invoice_item_rules_amount_source",
        ),
        Index("ix_customer_invoice_item_rules_customer", "customer_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False
    )
    product_id: Mapped[int | None] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"), nullable=True
    )
    project_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    tax_classification_code: Mapped[str | None] = mapped_column(
        String(80), nullable=True
    )
    unit: Mapped[str | None] = mapped_column(String(40), nullable=True)
    tax_rate: Mapped[Decimal | None] = mapped_column(Numeric(6, 4), nullable=True)
    spec_source: Mapped[str] = mapped_column(
        String(40), default="product_snapshot", nullable=False
    )
    quantity_source: Mapped[str] = mapped_column(
        String(50), default="statement_received_quantity", nullable=False
    )
    amount_source: Mapped[str] = mapped_column(
        String(50), default="statement_receivable_amount", nullable=False
    )
    fill_unit_price: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    effective_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    confirmation_status: Mapped[str] = mapped_column(
        String(20), default="pending", server_default="pending", nullable=False
    )
    version: Mapped[int] = mapped_column(
        Integer, default=1, server_default="1", nullable=False
    )
    confirmed_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )


class FinanceInvoiceTask(Base):
    __tablename__ = "finance_invoice_tasks"
    __table_args__ = (
        UniqueConstraint("task_number", name="uq_finance_invoice_tasks_number"),
        UniqueConstraint("idempotency_key", name="uq_finance_invoice_tasks_idempotency"),
        CheckConstraint(
            "status IN ('draft','ready','exported','issued','failed','voided')",
            name="ck_finance_invoice_tasks_status",
        ),
        CheckConstraint("version >= 1", name="ck_finance_invoice_tasks_version"),
        Index("ix_finance_invoice_tasks_statement", "statement_id"),
        Index("ix_finance_invoice_tasks_customer_status", "customer_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    task_number: Mapped[str] = mapped_column(String(60), nullable=False)
    statement_id: Mapped[int] = mapped_column(
        ForeignKey("finance_statements.id", ondelete="RESTRICT"), nullable=False
    )
    statement_version: Mapped[int] = mapped_column(Integer, nullable=False)
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False
    )
    seller_entity_id: Mapped[int] = mapped_column(
        ForeignKey("invoice_seller_entities.id", ondelete="RESTRICT"), nullable=False
    )
    buyer_snapshot_json: Mapped[str] = mapped_column(Text, nullable=False)
    seller_snapshot_json: Mapped[str] = mapped_column(Text, nullable=False)
    invoice_type: Mapped[str] = mapped_column(String(40), nullable=False)
    price_tax_mode: Mapped[str] = mapped_column(String(30), nullable=False)
    net_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    tax_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    total_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), default="draft", server_default="draft", nullable=False
    )
    rule_version: Mapped[int] = mapped_column(Integer, nullable=False)
    source_snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    export_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_export_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_exported_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    confirmed_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    voided_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    voided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    version: Mapped[int] = mapped_column(
        Integer, default=1, server_default="1", nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime, onupdate=func.current_timestamp(), nullable=True
    )


class FinanceInvoiceTaskItem(Base):
    __tablename__ = "finance_invoice_task_items"
    __table_args__ = (
        UniqueConstraint("task_id", "sequence_no", name="uq_invoice_task_items_sequence"),
        UniqueConstraint(
            "task_id", "statement_item_id", name="uq_invoice_task_items_statement_item"
        ),
        CheckConstraint(
            "source_type IN ('delivery','customer_charge')",
            name="ck_finance_invoice_task_items_source_type",
        ),
        CheckConstraint(
            "((source_type = 'delivery' AND customer_charge_id IS NULL) OR "
            "(source_type = 'customer_charge' AND customer_charge_id IS NOT NULL))",
            name="ck_finance_invoice_task_items_charge_source",
        ),
        Index("ix_finance_invoice_task_items_task", "task_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(
        ForeignKey("finance_invoice_tasks.id", ondelete="CASCADE"), nullable=False
    )
    sequence_no: Mapped[int] = mapped_column(Integer, nullable=False)
    statement_item_id: Mapped[int] = mapped_column(
        ForeignKey("finance_statement_items.id", ondelete="RESTRICT"), nullable=False
    )
    source_type: Mapped[str] = mapped_column(
        String(30), default="delivery", server_default="delivery", nullable=False
    )
    customer_charge_id: Mapped[int | None] = mapped_column(
        ForeignKey("finance_customer_charges.id", ondelete="RESTRICT"), nullable=True
    )
    product_code_snapshot: Mapped[str | None] = mapped_column(String(100), nullable=True)
    product_name_snapshot: Mapped[str | None] = mapped_column(String(200), nullable=True)
    project_name: Mapped[str] = mapped_column(String(200), nullable=False)
    tax_classification_code: Mapped[str] = mapped_column(String(80), nullable=False)
    specification: Mapped[str | None] = mapped_column(String(200), nullable=True)
    unit: Mapped[str] = mapped_column(String(40), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(14, 4), nullable=False)
    unit_price: Mapped[Decimal | None] = mapped_column(Numeric(14, 6), nullable=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    tax_rate: Mapped[Decimal] = mapped_column(Numeric(6, 4), nullable=False)
    tax_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    rule_version: Mapped[int] = mapped_column(Integer, nullable=False)


class CustomerInvoiceSellerChange(Base):
    __tablename__ = "customer_invoice_seller_changes"
    __table_args__ = (
        CheckConstraint(
            "change_type IN ('temporary','permanent')",
            name="ck_customer_invoice_seller_changes_type",
        ),
        Index("ix_customer_invoice_seller_changes_customer", "customer_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False
    )
    old_seller_id: Mapped[int | None] = mapped_column(
        ForeignKey("invoice_seller_entities.id", ondelete="RESTRICT"), nullable=True
    )
    new_seller_id: Mapped[int] = mapped_column(
        ForeignKey("invoice_seller_entities.id", ondelete="RESTRICT"), nullable=False
    )
    change_type: Mapped[str] = mapped_column(String(20), nullable=False)
    statement_id: Mapped[int | None] = mapped_column(
        ForeignKey("finance_statements.id", ondelete="RESTRICT"), nullable=True
    )
    invoice_task_id: Mapped[int | None] = mapped_column(
        ForeignKey("finance_invoice_tasks.id", ondelete="RESTRICT"), nullable=True
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )


class FinanceInvoiceAttachment(Base):
    __tablename__ = "finance_invoice_attachments"
    __table_args__ = (
        CheckConstraint(
            "attachment_type IN ('original','organized')",
            name="ck_finance_invoice_attachments_type",
        ),
        UniqueConstraint(
            "task_id", "attachment_type", "content_hash",
            name="uq_finance_invoice_attachments_hash",
        ),
        Index("ix_finance_invoice_attachments_task", "task_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(
        ForeignKey("finance_invoice_tasks.id", ondelete="RESTRICT"), nullable=False
    )
    attachment_type: Mapped[str] = mapped_column(String(20), nullable=False)
    original_name: Mapped[str] = mapped_column(String(255), nullable=False)
    stored_name: Mapped[str] = mapped_column(String(255), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    file_size: Mapped[int] = mapped_column(Integer, nullable=False)
    source_attachment_id: Mapped[int | None] = mapped_column(
        ForeignKey("finance_invoice_attachments.id", ondelete="RESTRICT"), nullable=True
    )
    uploaded_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )
