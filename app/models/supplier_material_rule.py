from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base


class SupplierMaterialBasePrice(Base):
    __tablename__ = "supplier_material_base_prices"
    __table_args__ = (
        UniqueConstraint(
            "supplier_name", "material_code", "effective_date",
            name="uq_supplier_material_base_price",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    supplier_name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    material_code: Mapped[str] = mapped_column(String(20), nullable=False)
    layer_count: Mapped[int] = mapped_column(Integer, nullable=False)
    base_price: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    effective_date: Mapped[date] = mapped_column(Date, nullable=False)
    source: Mapped[str] = mapped_column(String(250), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )


class SupplierMaterialSubstitutionRule(Base):
    __tablename__ = "supplier_material_substitution_rules"
    __table_args__ = (
        UniqueConstraint(
            "supplier_name", "rule_type", "from_code", "to_code", "effective_date",
            name="uq_supplier_material_substitution_rule",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    supplier_name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    rule_type: Mapped[str] = mapped_column(String(40), nullable=False)
    from_code: Mapped[str] = mapped_column(String(1), nullable=False)
    to_code: Mapped[str] = mapped_column(String(1), nullable=False)
    price_delta: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    effective_date: Mapped[date] = mapped_column(Date, nullable=False)
    source: Mapped[str] = mapped_column(String(250), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )


class SupplierMaterialRuleConfig(Base):
    __tablename__ = "supplier_material_rule_configs"
    __table_args__ = (
        UniqueConstraint(
            "supplier_name", "rule_key",
            name="uq_supplier_material_rule_config",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    supplier_name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    rule_key: Mapped[str] = mapped_column(String(80), nullable=False)
    rule_value: Mapped[str | None] = mapped_column(String(200), nullable=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="active")
    description: Mapped[str] = mapped_column(Text, nullable=False)
    participates_in_pricing: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, onupdate=func.current_timestamp()
    )
