from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base


class ProcessingCostSettings(Base):
    """The single versioned standard-time configuration row."""

    __tablename__ = "processing_cost_settings"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_processing_cost_settings_singleton"),
        CheckConstraint("version >= 1", name="ck_processing_cost_settings_version"),
        CheckConstraint(
            "default_printer IN ('new','old')",
            name="ck_processing_cost_settings_default_printer",
        ),
        CheckConstraint(
            "working_hours_per_day > 0 AND working_hours_per_day <= 24",
            name="ck_processing_cost_settings_working_hours",
        ),
        CheckConstraint(
            "working_days_per_month > 0 AND working_days_per_month <= 31",
            name="ck_processing_cost_settings_working_days",
        ),
        CheckConstraint(
            "new_printer_normal_sheets_per_minute > 0 "
            "AND new_printer_max_sheets_per_minute >= new_printer_normal_sheets_per_minute",
            name="ck_processing_cost_settings_new_printer_speed",
        ),
        CheckConstraint(
            "old_printer_normal_sheets_per_minute > 0 "
            "AND old_printer_max_sheets_per_minute >= old_printer_normal_sheets_per_minute",
            name="ck_processing_cost_settings_old_printer_speed",
        ),
        CheckConstraint(
            "printing_setup_minutes >= 0 AND printing_setup_minutes <= 1440",
            name="ck_processing_cost_settings_printing_setup",
        ),
        CheckConstraint(
            "printing_crew_size > 0 AND printing_crew_size <= 100",
            name="ck_processing_cost_settings_printing_crew",
        ),
        CheckConstraint(
            "colors_per_pass > 0 AND colors_per_pass <= 10",
            name="ck_processing_cost_settings_colors_per_pass",
        ),
        CheckConstraint(
            "die_setup_minutes >= 0 AND die_setup_minutes <= 1440",
            name="ck_processing_cost_settings_die_setup",
        ),
        CheckConstraint(
            "small_die_normal_pieces_per_minute > 0 "
            "AND small_die_max_pieces_per_minute >= small_die_normal_pieces_per_minute",
            name="ck_processing_cost_settings_small_die_speed",
        ),
        CheckConstraint(
            "small_die_crew_size > 0 AND small_die_crew_size <= 100 "
            "AND small_complex_die_crew_size > 0 AND small_complex_die_crew_size <= 100 "
            "AND large_die_crew_size > 0 AND large_die_crew_size <= 100 "
            "AND oversize_die_crew_size > 0 AND oversize_die_crew_size <= 100",
            name="ck_processing_cost_settings_die_crews",
        ),
        CheckConstraint(
            "large_die_pieces_per_minute > 0 "
            "AND oversize_die_seconds_per_piece > 0",
            name="ck_processing_cost_settings_large_die_speeds",
        ),
        CheckConstraint(
            "joining_normal_pieces_per_second > 0 "
            "AND joining_max_pieces_per_second >= joining_normal_pieces_per_second",
            name="ck_processing_cost_settings_joining_speed",
        ),
        CheckConstraint(
            "double_splice_seconds_per_piece > 0",
            name="ck_processing_cost_settings_double_splice_speed",
        ),
        CheckConstraint(
            "joining_crew_size > 0 AND joining_crew_size <= 100",
            name="ck_processing_cost_settings_joining_crew",
        ),
        CheckConstraint(
            "average_worker_monthly_salary IS NULL "
            "OR average_worker_monthly_salary > 0",
            name="ck_processing_cost_settings_salary",
        ),
        CheckConstraint(
            "average_worker_monthly_social_cost >= 0",
            name="ck_processing_cost_settings_social_cost",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    working_hours_per_day: Mapped[Decimal] = mapped_column(
        Numeric(8, 4), default=Decimal("8"), server_default="8", nullable=False
    )
    working_days_per_month: Mapped[Decimal] = mapped_column(
        Numeric(8, 4), default=Decimal("26"), server_default="26", nullable=False
    )
    default_printer: Mapped[str] = mapped_column(
        String(10), default="new", server_default="new", nullable=False
    )
    new_printer_normal_sheets_per_minute: Mapped[Decimal] = mapped_column(
        Numeric(12, 4), default=Decimal("90"), server_default="90", nullable=False
    )
    new_printer_max_sheets_per_minute: Mapped[Decimal] = mapped_column(
        Numeric(12, 4), default=Decimal("120"), server_default="120", nullable=False
    )
    old_printer_normal_sheets_per_minute: Mapped[Decimal] = mapped_column(
        Numeric(12, 4), default=Decimal("30"), server_default="30", nullable=False
    )
    old_printer_max_sheets_per_minute: Mapped[Decimal] = mapped_column(
        Numeric(12, 4), default=Decimal("60"), server_default="60", nullable=False
    )
    printing_setup_minutes: Mapped[Decimal] = mapped_column(
        Numeric(12, 4), default=Decimal("30"), server_default="30", nullable=False
    )
    printing_crew_size: Mapped[int] = mapped_column(
        Integer, default=2, server_default="2", nullable=False
    )
    colors_per_pass: Mapped[int] = mapped_column(
        Integer, default=2, server_default="2", nullable=False
    )
    die_setup_minutes: Mapped[Decimal] = mapped_column(
        Numeric(12, 4), default=Decimal("30"), server_default="30", nullable=False
    )
    small_die_normal_pieces_per_minute: Mapped[Decimal] = mapped_column(
        Numeric(12, 4), default=Decimal("60"), server_default="60", nullable=False
    )
    small_die_max_pieces_per_minute: Mapped[Decimal] = mapped_column(
        Numeric(12, 4), default=Decimal("120"), server_default="120", nullable=False
    )
    small_die_crew_size: Mapped[int] = mapped_column(
        Integer, default=2, server_default="2", nullable=False
    )
    small_complex_die_crew_size: Mapped[int] = mapped_column(
        Integer, default=3, server_default="3", nullable=False
    )
    large_die_pieces_per_minute: Mapped[Decimal] = mapped_column(
        Numeric(12, 4), default=Decimal("50"), server_default="50", nullable=False
    )
    large_die_crew_size: Mapped[int] = mapped_column(
        Integer, default=2, server_default="2", nullable=False
    )
    oversize_die_seconds_per_piece: Mapped[Decimal] = mapped_column(
        Numeric(12, 4), default=Decimal("120"), server_default="120", nullable=False
    )
    oversize_die_crew_size: Mapped[int] = mapped_column(
        Integer, default=2, server_default="2", nullable=False
    )
    joining_normal_pieces_per_second: Mapped[Decimal] = mapped_column(
        Numeric(12, 4), default=Decimal("1"), server_default="1", nullable=False
    )
    joining_max_pieces_per_second: Mapped[Decimal] = mapped_column(
        Numeric(12, 4), default=Decimal("3"), server_default="3", nullable=False
    )
    double_splice_seconds_per_piece: Mapped[Decimal] = mapped_column(
        Numeric(12, 4), default=Decimal("30"), server_default="30", nullable=False
    )
    joining_crew_size: Mapped[int] = mapped_column(
        Integer, default=2, server_default="2", nullable=False
    )
    average_worker_monthly_salary: Mapped[Decimal | None] = mapped_column(
        Numeric(14, 2), nullable=True
    )
    average_worker_monthly_social_cost: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), default=Decimal("0"), server_default="0", nullable=False
    )
    version: Mapped[int] = mapped_column(
        Integer, default=1, server_default="1", nullable=False
    )
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    updated_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime, onupdate=func.current_timestamp(), nullable=True
    )


class ProductProcessingProfile(Base):
    """Sparse per-product exceptions; ordinary products use automatic rules."""

    __tablename__ = "product_processing_profiles"
    __table_args__ = (
        UniqueConstraint("product_id", name="uq_product_processing_profiles_product"),
        CheckConstraint(
            "printer_mode IN ('auto','new','old','none')",
            name="ck_product_processing_profiles_printer_mode",
        ),
        CheckConstraint(
            "die_cut_mode IN "
            "('auto','none','small_normal','small_complex','large','oversize')",
            name="ck_product_processing_profiles_die_cut_mode",
        ),
        CheckConstraint(
            "assembly_worker_days_per_1000 IS NULL "
            "OR assembly_worker_days_per_1000 > 0",
            name="ck_product_processing_profiles_assembly_days",
        ),
        CheckConstraint("version >= 1", name="ck_product_processing_profiles_version"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True
    )
    printer_mode: Mapped[str] = mapped_column(
        String(10), default="auto", server_default="auto", nullable=False
    )
    die_cut_mode: Mapped[str] = mapped_column(
        String(20), default="auto", server_default="auto", nullable=False
    )
    assembly_worker_days_per_1000: Mapped[Decimal | None] = mapped_column(
        Numeric(14, 6), nullable=True
    )
    version: Mapped[int] = mapped_column(
        Integer, default=1, server_default="1", nullable=False
    )
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    updated_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime, onupdate=func.current_timestamp(), nullable=True
    )
