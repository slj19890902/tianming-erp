from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
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
    from app.models.product import Product


class Material(Base):
    __tablename__ = "materials"
    __table_args__ = (
        CheckConstraint("version >= 1", name="ck_materials_version"),
        UniqueConstraint(
            "supplier_name",
            "code",
            name="uq_materials_supplier_code",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(100), index=True)
    paper_composition: Mapped[str | None] = mapped_column(String(200), nullable=True)
    layer_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    flute_type: Mapped[str | None] = mapped_column(
        String(50),
        nullable=True,
    )
    basis_weight_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    quote_price: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 4),
        nullable=True,
    )
    rule_base_price: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 4), nullable=True
    )
    price_source: Mapped[str | None] = mapped_column(String(250), nullable=True)
    price_unit: Mapped[str | None] = mapped_column(String(50), nullable=True)
    supplier_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    quote_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    remarks: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    version: Mapped[int] = mapped_column(
        Integer,
        default=1,
        server_default="1",
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.current_timestamp(),
        nullable=False,
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        onupdate=func.current_timestamp(),
        nullable=True,
    )

    products: Mapped[list["Product"]] = relationship(
        back_populates="material",
        passive_deletes=True,
    )
