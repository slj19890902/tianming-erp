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


class FinanceRecurringRule(Base):
    """One small, editable rule that creates a monthly cost draft."""

    __tablename__ = "finance_recurring_rules"
    __table_args__ = (
        CheckConstraint(
            "rule_type IN ('employee_wage','annual_rent','fixed_monthly')",
            name="ck_finance_recurring_rules_type",
        ),
        CheckConstraint(
            "cost_category IN ('production_wages','factory_rent',"
            "'administrative_expense','finance_expense')",
            name="ck_finance_recurring_rules_category",
        ),
        CheckConstraint(
            "start_month GLOB '[0-9][0-9][0-9][0-9]-[0-1][0-9]' "
            "AND CAST(substr(start_month, 6, 2) AS INTEGER) BETWEEN 1 AND 12",
            name="ck_finance_recurring_rules_start_month",
        ),
        CheckConstraint(
            "end_month IS NULL OR ("
            "end_month GLOB '[0-9][0-9][0-9][0-9]-[0-1][0-9]' "
            "AND CAST(substr(end_month, 6, 2) AS INTEGER) BETWEEN 1 AND 12 "
            "AND end_month >= start_month)",
            name="ck_finance_recurring_rules_end_month",
        ),
        CheckConstraint(
            "base_amount >= 0 AND allowance_amount >= 0 "
            "AND employer_social_amount >= 0 AND deduction_amount >= 0 "
            "AND annual_amount >= 0 AND monthly_amount >= 0",
            name="ck_finance_recurring_rules_non_negative",
        ),
        CheckConstraint(
            "(rule_type = 'employee_wage' "
            "AND base_amount + allowance_amount + employer_social_amount "
            "- deduction_amount > 0) OR "
            "(rule_type = 'annual_rent' AND annual_amount > 0) OR "
            "(rule_type = 'fixed_monthly' AND monthly_amount > 0)",
            name="ck_finance_recurring_rules_amount",
        ),
        CheckConstraint("due_day BETWEEN 1 AND 28", name="ck_finance_recurring_rules_due_day"),
        CheckConstraint("version >= 1", name="ck_finance_recurring_rules_version"),
        Index("ix_finance_recurring_rules_active_period", "is_active", "start_month", "end_month"),
        Index("ix_finance_recurring_rules_type", "rule_type", "is_active"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    rule_type: Mapped[str] = mapped_column(String(30), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    role_name: Mapped[str | None] = mapped_column(String(80), nullable=True)
    cost_center_id: Mapped[int] = mapped_column(
        ForeignKey("finance_cost_centers.id", ondelete="RESTRICT"), nullable=False
    )
    cost_category: Mapped[str] = mapped_column(String(40), nullable=False)
    start_month: Mapped[str] = mapped_column(String(7), nullable=False)
    end_month: Mapped[str | None] = mapped_column(String(7), nullable=True)
    base_amount: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, default=0, server_default="0"
    )
    allowance_amount: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, default=0, server_default="0"
    )
    employer_social_amount: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, default=0, server_default="0"
    )
    deduction_amount: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, default=0, server_default="0"
    )
    annual_amount: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, default=0, server_default="0"
    )
    monthly_amount: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, default=0, server_default="0"
    )
    due_day: Mapped[int] = mapped_column(Integer, nullable=False, default=20, server_default="20")
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="1")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )
    updated_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class FinanceUtilityReading(Base):
    """Monthly water/electricity meter and invoice facts."""

    __tablename__ = "finance_utility_readings"
    __table_args__ = (
        CheckConstraint(
            "utility_type IN ('water','electricity')",
            name="ck_finance_utility_readings_type",
        ),
        CheckConstraint(
            "cost_month GLOB '[0-9][0-9][0-9][0-9]-[0-1][0-9]' "
            "AND CAST(substr(cost_month, 6, 2) AS INTEGER) BETWEEN 1 AND 12",
            name="ck_finance_utility_readings_month",
        ),
        CheckConstraint(
            "previous_reading >= 0 AND current_reading >= previous_reading "
            "AND abs(usage_quantity - (current_reading - previous_reading)) < 0.0005",
            name="ck_finance_utility_readings_meter",
        ),
        CheckConstraint(
            "unit_price >= 0 AND calculated_amount >= 0 "
            "AND (invoice_amount IS NULL OR invoice_amount >= 0) "
            "AND paid_amount >= 0",
            name="ck_finance_utility_readings_amounts",
        ),
        CheckConstraint("version >= 1", name="ck_finance_utility_readings_version"),
        UniqueConstraint("cost_month", "utility_type", name="uq_finance_utility_readings_month_type"),
        UniqueConstraint("cost_pool_entry_id", name="uq_finance_utility_readings_cost_entry"),
        Index("ix_finance_utility_readings_month", "cost_month", "utility_type"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    cost_month: Mapped[str] = mapped_column(String(7), nullable=False)
    utility_type: Mapped[str] = mapped_column(String(20), nullable=False)
    cost_center_id: Mapped[int] = mapped_column(
        ForeignKey("finance_cost_centers.id", ondelete="RESTRICT"), nullable=False
    )
    previous_reading: Mapped[Decimal] = mapped_column(Numeric(18, 3), nullable=False)
    current_reading: Mapped[Decimal] = mapped_column(Numeric(18, 3), nullable=False)
    usage_quantity: Mapped[Decimal] = mapped_column(Numeric(18, 3), nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(14, 4), nullable=False)
    calculated_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    invoice_number: Mapped[str | None] = mapped_column(String(120), nullable=True)
    invoice_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    invoice_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    paid_amount: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, default=0, server_default="0"
    )
    payment_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    cost_pool_entry_id: Mapped[int] = mapped_column(
        ForeignKey("finance_cost_pool_entries.id", ondelete="RESTRICT"), nullable=False
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )
    updated_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class FinanceAcceptanceNote(Base):
    """Customer acceptance held or fully endorsed to one supplier statement."""

    __tablename__ = "finance_acceptance_notes"
    __table_args__ = (
        CheckConstraint(
            "status IN ('held','endorsed','matured','returned','voided')",
            name="ck_finance_acceptance_notes_status",
        ),
        CheckConstraint("amount > 0", name="ck_finance_acceptance_notes_amount"),
        CheckConstraint(
            "maturity_date >= received_date",
            name="ck_finance_acceptance_notes_dates",
        ),
        CheckConstraint("version >= 1", name="ck_finance_acceptance_notes_version"),
        UniqueConstraint("bill_number", name="uq_finance_acceptance_notes_bill_number"),
        UniqueConstraint("supplier_payment_id", name="uq_finance_acceptance_notes_supplier_payment"),
        Index("ix_finance_acceptance_notes_status_maturity", "status", "maturity_date"),
        Index("ix_finance_acceptance_notes_customer", "customer_id", "received_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    bill_number: Mapped[str] = mapped_column(String(120), nullable=False)
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False
    )
    customer_name_snapshot: Mapped[str] = mapped_column(String(200), nullable=False)
    customer_statement_id: Mapped[int | None] = mapped_column(
        ForeignKey("finance_statements.id", ondelete="SET NULL"), nullable=True
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    received_date: Mapped[date] = mapped_column(Date, nullable=False)
    maturity_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="held", server_default="held")
    supplier_id: Mapped[int | None] = mapped_column(
        ForeignKey("supplier_master_records.id", ondelete="RESTRICT"), nullable=True
    )
    supplier_name_snapshot: Mapped[str | None] = mapped_column(String(200), nullable=True)
    supplier_statement_id: Mapped[int | None] = mapped_column(
        ForeignKey("supplier_monthly_statements.id", ondelete="RESTRICT"), nullable=True
    )
    supplier_payment_id: Mapped[int | None] = mapped_column(
        ForeignKey("supplier_monthly_payments.id", ondelete="RESTRICT"), nullable=True
    )
    endorsed_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )
    updated_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
