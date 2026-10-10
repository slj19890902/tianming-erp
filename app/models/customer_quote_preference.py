from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models import Base


class CustomerQuotePreference(Base):
    """A customer's tax-inclusive square-metre quote preference.

    This is a master-data preference only.  It never rewrites a product,
    order, quotation, or their historical price snapshots.
    """

    __tablename__ = "customer_quote_preferences"
    __table_args__ = (
        UniqueConstraint(
            "customer_id",
            "box_type",
            "crease_type",
            "material_id",
            "flute_type",
            name="uq_customer_quote_preferences_identity",
        ),
        CheckConstraint("tax_included_square_price > 0", name="ck_cqp_square_price"),
        CheckConstraint("version >= 1", name="ck_cqp_version"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    box_type: Mapped[str] = mapped_column(String(150), nullable=False)
    crease_type: Mapped[str] = mapped_column(
        String(20), nullable=False, default="压线", server_default="压线"
    )
    material_id: Mapped[int] = mapped_column(
        ForeignKey("materials.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    flute_type: Mapped[str] = mapped_column(String(20), nullable=False)
    tax_included_square_price: Mapped[Decimal] = mapped_column(
        Numeric(12, 4), nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, onupdate=func.current_timestamp()
    )

    customer = relationship("Customer", back_populates="quote_preferences")
    material = relationship("Material")
