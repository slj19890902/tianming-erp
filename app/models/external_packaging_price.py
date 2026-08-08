from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models import Base


class ExternalPackagingPriceVersion(Base):
    __tablename__ = "external_packaging_price_versions"
    __table_args__ = (
        UniqueConstraint(
            "external_product_id",
            "version_number",
            name="uq_external_packaging_price_product_version",
        ),
        UniqueConstraint(
            "external_product_id",
            "quote_fingerprint",
            name="uq_external_packaging_price_product_fingerprint",
        ),
        CheckConstraint("version_number >= 1", name="ck_external_packaging_price_version"),
        CheckConstraint("product_version >= 1", name="ck_external_packaging_price_product_version"),
        CheckConstraint("unit_price > 0", name="ck_external_packaging_price_unit_price"),
        CheckConstraint("tax_rate >= 0 AND tax_rate <= 1", name="ck_external_packaging_price_tax_rate"),
        CheckConstraint(
            "tax_amount_per_unit IS NULL OR tax_amount_per_unit >= 0",
            name="ck_external_packaging_price_tax_amount",
        ),
        CheckConstraint(
            "moq_quantity IS NULL OR moq_quantity > 0",
            name="ck_external_packaging_price_moq",
        ),
        CheckConstraint(
            "packaging_multiple IS NULL OR packaging_multiple > 0",
            name="ck_external_packaging_price_multiple",
        ),
        CheckConstraint(
            "shipping_fee IS NULL OR shipping_fee >= 0",
            name="ck_external_packaging_price_shipping",
        ),
        CheckConstraint(
            "sample_fee IS NULL OR sample_fee >= 0",
            name="ck_external_packaging_price_sample_fee",
        ),
        CheckConstraint(
            "plate_fee IS NULL OR plate_fee >= 0",
            name="ck_external_packaging_price_plate_fee",
        ),
        CheckConstraint(
            "die_fee IS NULL OR die_fee >= 0",
            name="ck_external_packaging_price_die_fee",
        ),
        CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from",
            name="ck_external_packaging_price_effective_range",
        ),
        CheckConstraint(
            "tax_mode IN ('tax_inclusive','tax_exclusive')",
            name="ck_external_packaging_price_tax_mode",
        ),
        CheckConstraint(
            "shipping_fee_mode IN ('not_provided','included','per_order','per_unit')",
            name="ck_external_packaging_price_shipping_mode",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    external_product_id: Mapped[int] = mapped_column(
        ForeignKey("external_packaging_products.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    product_version: Mapped[int] = mapped_column(Integer, nullable=False)
    specification_snapshot_json: Mapped[str] = mapped_column(Text, nullable=False)
    quote_unit: Mapped[str] = mapped_column(String(20), nullable=False)
    unit_conversion_basis: Mapped[str | None] = mapped_column(Text, nullable=True)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    tax_mode: Mapped[str] = mapped_column(String(20), nullable=False)
    tax_rate: Mapped[Decimal] = mapped_column(Numeric(8, 6), nullable=False)
    tax_amount_per_unit: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 6), nullable=True
    )
    unit_price: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    moq_quantity: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    moq_unit: Mapped[str | None] = mapped_column(String(20), nullable=True)
    packaging_multiple: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 4), nullable=True
    )
    tier_prices_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="[]", server_default="[]"
    )
    shipping_fee_mode: Mapped[str] = mapped_column(String(20), nullable=False)
    shipping_fee: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    sample_fee: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    plate_fee: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    die_fee: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    evidence_reference: Mapped[str] = mapped_column(String(500), nullable=False)
    remarks: Mapped[str | None] = mapped_column(Text, nullable=True)
    quote_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )

    product = relationship("ExternalPackagingProduct")

