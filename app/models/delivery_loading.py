from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, Numeric, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base


class DeliveryVehicle(Base):
    __tablename__ = "delivery_vehicles"
    __table_args__ = (
        UniqueConstraint("plate_number", name="uq_delivery_vehicles_plate_number"),
        CheckConstraint("cargo_length_mm > 0 AND cargo_width_mm > 0 AND cargo_height_mm > 0", name="ck_delivery_vehicles_positive_dimensions"),
        CheckConstraint("safety_load_factor > 0 AND safety_load_factor <= 1", name="ck_delivery_vehicles_safety_factor"),
        CheckConstraint("yellow_threshold_pct > 0 AND yellow_threshold_pct <= red_threshold_pct", name="ck_delivery_vehicles_thresholds"),
        Index("ix_delivery_vehicles_active_plate", "is_active", "plate_number"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    vehicle_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    plate_number: Mapped[str] = mapped_column(String(50), nullable=False)
    cargo_length_mm: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    cargo_width_mm: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    cargo_height_mm: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    safety_load_factor: Mapped[Decimal] = mapped_column(Numeric(6, 4), nullable=False, default=Decimal("1.0"), server_default="1.0")
    yellow_threshold_pct: Mapped[Decimal] = mapped_column(Numeric(6, 2), nullable=False, default=Decimal("80"), server_default="80")
    red_threshold_pct: Mapped[Decimal] = mapped_column(Numeric(6, 2), nullable=False, default=Decimal("100"), server_default="100")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="1")
    remarks: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.current_timestamp())
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, onupdate=func.current_timestamp())


class ProductLoadingProfile(Base):
    __tablename__ = "product_loading_profiles"
    __table_args__ = (
        UniqueConstraint("product_id", name="uq_product_loading_profiles_product_id"),
        CheckConstraint("mode IN ('theoretical_box', 'manual_unit', 'package')", name="ck_product_loading_profiles_mode"),
        CheckConstraint("manual_unit_m3 IS NULL OR manual_unit_m3 > 0", name="ck_product_loading_profiles_manual_volume"),
        CheckConstraint("package_piece_count IS NULL OR package_piece_count > 0", name="ck_product_loading_profiles_package_piece_count"),
        CheckConstraint("(package_length_mm IS NULL OR package_length_mm > 0) AND (package_width_mm IS NULL OR package_width_mm > 0) AND (package_height_mm IS NULL OR package_height_mm > 0)", name="ck_product_loading_profiles_package_dimensions"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="RESTRICT"), nullable=False)
    mode: Mapped[str] = mapped_column(String(30), nullable=False)
    manual_unit_m3: Mapped[Decimal | None] = mapped_column(Numeric(14, 8), nullable=True)
    package_piece_count: Mapped[int | None] = mapped_column(nullable=True)
    package_length_mm: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    package_width_mm: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    package_height_mm: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    source_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    confirmed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.current_timestamp(), onupdate=func.current_timestamp())
