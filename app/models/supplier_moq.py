from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base


class SupplierMinimumOrderRule(Base):
    __tablename__ = "supplier_minimum_order_rules"
    __table_args__ = (
        CheckConstraint(
            "scope_type IN ('supplier','customer','material','flute')",
            name="ck_supplier_minimum_order_rules_scope_type",
        ),
        CheckConstraint(
            "unit IN ('sheets','square_meters','meters','amount','group_total')",
            name="ck_supplier_minimum_order_rules_unit",
        ),
        CheckConstraint(
            "status IN ('active','inactive')",
            name="ck_supplier_minimum_order_rules_status",
        ),
        CheckConstraint(
            "source = 'manual_confirmation'",
            name="ck_supplier_minimum_order_rules_source",
        ),
        CheckConstraint(
            "minimum_quantity > 0",
            name="ck_supplier_minimum_order_rules_minimum_quantity",
        ),
        CheckConstraint(
            "(merge_allowed AND merge_window_days BETWEEN 1 AND 365) OR "
            "((NOT merge_allowed) AND merge_window_days IS NULL)",
            name="ck_supplier_minimum_order_rules_merge_window",
        ),
        CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from",
            name="ck_supplier_minimum_order_rules_effective_dates",
        ),
        CheckConstraint(
            "version >= 1",
            name="ck_supplier_minimum_order_rules_version",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    supplier_id: Mapped[int] = mapped_column(
        ForeignKey("supplier_master_records.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    supplier_name_snapshot: Mapped[str] = mapped_column(String(200), nullable=False)
    scope_type: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    scope_value: Mapped[str | None] = mapped_column(String(200), nullable=True)
    scope_label_snapshot: Mapped[str] = mapped_column(String(200), nullable=False)
    minimum_quantity: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    unit: Mapped[str] = mapped_column(String(30), nullable=False)
    merge_allowed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    merge_window_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="active", server_default="active", index=True
    )
    source: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        default="manual_confirmation",
        server_default="manual_confirmation",
    )
    evidence_reference: Mapped[str] = mapped_column(Text, nullable=False)
    confirmed_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    confirmed_by_username_snapshot: Mapped[str] = mapped_column(
        String(100), nullable=False
    )
    confirmed_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )
    created_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    updated_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, onupdate=func.current_timestamp()
    )
