from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

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
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base


class FinanceCostCenter(Base):
    """Small factory cost centre used by the management-cost subledger."""

    __tablename__ = "finance_cost_centers"
    __table_args__ = (
        CheckConstraint(
            "center_type IN "
            "('production','warehouse_delivery','sales','administration','finance','unallocated')",
            name="ck_finance_cost_centers_type",
        ),
        CheckConstraint(
            "length(trim(code)) BETWEEN 2 AND 30",
            name="ck_finance_cost_centers_code_not_blank",
        ),
        CheckConstraint(
            "length(trim(name)) BETWEEN 1 AND 100",
            name="ck_finance_cost_centers_name_not_blank",
        ),
        CheckConstraint("version >= 1", name="ck_finance_cost_centers_version"),
        UniqueConstraint("code", name="uq_finance_cost_centers_code"),
        Index("ix_finance_cost_centers_active", "is_active", "center_type"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(30), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    center_type: Mapped[str] = mapped_column(String(30), nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="1", nullable=False
    )
    version: Mapped[int] = mapped_column(
        Integer, default=1, server_default="1", nullable=False
    )
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime, onupdate=func.current_timestamp(), nullable=True
    )


class FinanceCostPoolEntry(Base):
    """Auditable cost/expense fact awaiting a later month-close allocation."""

    __tablename__ = "finance_cost_pool_entries"
    __table_args__ = (
        CheckConstraint(
            "cost_month GLOB '[0-9][0-9][0-9][0-9]-[0-1][0-9]' "
            "AND CAST(substr(cost_month, 6, 2) AS INTEGER) BETWEEN 1 AND 12",
            name="ck_finance_cost_pool_entries_month_shape",
        ),
        CheckConstraint(
            "cost_category IN ("
            "'outsourcing','inbound_freight',"
            "'production_wages','factory_utilities','factory_rent','maintenance',"
            "'delivery_freight','driver_wages','sales_expense','administrative_wages',"
            "'administrative_expense','finance_expense','finance_wages','tax_fee','other')",
            name="ck_finance_cost_pool_entries_category",
        ),
        CheckConstraint(
            "accounting_class IN ('manufacturing','selling','administrative','finance','excluded')",
            name="ck_finance_cost_pool_entries_class",
        ),
        CheckConstraint(
            "allocation_basis IN ("
            "'unallocated','direct','completion_area','production_quantity',"
            "'machine_hours','delivery_quantity','manual')",
            name="ck_finance_cost_pool_entries_allocation_basis",
        ),
        CheckConstraint(
            "source_type IN ('manual','excel_import','finance_payable')",
            name="ck_finance_cost_pool_entries_source_type",
        ),
        CheckConstraint(
            "status IN ('draft','confirmed','voided')",
            name="ck_finance_cost_pool_entries_status",
        ),
        CheckConstraint(
            "(status = 'draft' AND confirmed_at IS NULL AND voided_at IS NULL) OR "
            "(status = 'confirmed' AND confirmed_at IS NOT NULL AND voided_at IS NULL) OR "
            "(status = 'voided' AND voided_at IS NOT NULL)",
            name="ck_finance_cost_pool_entries_status_timestamps",
        ),
        CheckConstraint(
            "length(trim(description)) BETWEEN 1 AND 300",
            name="ck_finance_cost_pool_entries_description_not_blank",
        ),
        CheckConstraint(
            "length(trim(source_reference)) BETWEEN 1 AND 200",
            name="ck_finance_cost_pool_entries_source_reference_not_blank",
        ),
        CheckConstraint(
            "source_fingerprint IS NULL OR length(trim(source_fingerprint)) > 0",
            name="ck_finance_cost_pool_entries_source_fingerprint_not_blank",
        ),
        CheckConstraint(
            "amount > 0 AND amount <= 999999999999.99",
            name="ck_finance_cost_pool_entries_amount",
        ),
        CheckConstraint(
            "tax_amount >= 0 AND tax_amount <= amount "
            "AND tax_amount <= 999999999999.99",
            name="ck_finance_cost_pool_entries_tax_amount",
        ),
        CheckConstraint("version >= 1", name="ck_finance_cost_pool_entries_version"),
        UniqueConstraint(
            "source_fingerprint",
            name="uq_finance_cost_pool_entries_source_fingerprint",
        ),
        Index(
            "ix_finance_cost_pool_entries_month_status",
            "cost_month",
            "status",
        ),
        Index(
            "ix_finance_cost_pool_entries_center_month",
            "cost_center_id",
            "cost_month",
        ),
        Index(
            "ix_finance_cost_pool_entries_category_month",
            "cost_category",
            "cost_month",
        ),
        Index(
            "ix_finance_cost_pool_entries_source",
            "source_type",
            "source_reference",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    cost_month: Mapped[str] = mapped_column(String(7), nullable=False)
    document_date: Mapped[date] = mapped_column(Date, nullable=False)
    cost_center_id: Mapped[int] = mapped_column(
        ForeignKey("finance_cost_centers.id", ondelete="RESTRICT"), nullable=False
    )
    cost_center_code_snapshot: Mapped[str] = mapped_column(String(30), nullable=False)
    cost_center_name_snapshot: Mapped[str] = mapped_column(String(100), nullable=False)
    cost_center_type_snapshot: Mapped[str] = mapped_column(String(30), nullable=False)
    cost_category: Mapped[str] = mapped_column(String(40), nullable=False)
    accounting_class: Mapped[str] = mapped_column(String(30), nullable=False)
    allocation_basis: Mapped[str] = mapped_column(
        String(30), default="unallocated", server_default="unallocated", nullable=False
    )
    description: Mapped[str] = mapped_column(String(300), nullable=False)
    counterparty_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    document_number: Mapped[str | None] = mapped_column(String(100), nullable=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    tax_amount: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), default=Decimal("0.00"), server_default="0.00", nullable=False
    )
    source_type: Mapped[str] = mapped_column(
        String(30), default="manual", server_default="manual", nullable=False
    )
    source_reference: Mapped[str] = mapped_column(String(200), nullable=False)
    source_fingerprint: Mapped[str | None] = mapped_column(String(96), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), default="draft", server_default="draft", nullable=False
    )
    version: Mapped[int] = mapped_column(
        Integer, default=1, server_default="1", nullable=False
    )
    confirmed_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
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
