from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base


class WarehouseCapacityForecastPlan(Base):
    """Operator-confirmed pallet-slot impact used only for capacity forecasting.

    It is deliberately separate from inventory facts.  Saving or cancelling a
    row never creates, moves, reserves, receives or dispatches inventory.
    """

    __tablename__ = "warehouse_capacity_forecast_plans"
    __table_args__ = (
        CheckConstraint(
            "source_type IN ('supplier_requisition','production_task','delivery')",
            name="ck_capacity_forecast_plans_source_type",
        ),
        CheckConstraint(
            "effect IN ('inflow','outflow','no_storage')",
            name="ck_capacity_forecast_plans_effect",
        ),
        CheckConstraint(
            "status IN ('active','cancelled')",
            name="ck_capacity_forecast_plans_status",
        ),
        CheckConstraint("source_id > 0", name="ck_capacity_forecast_plans_source_id"),
        CheckConstraint("version >= 1", name="ck_capacity_forecast_plans_version"),
        CheckConstraint(
            "(effect = 'no_storage' AND floor_id IS NULL AND pallet_slots = 0 "
            "AND scope_key = 'none') OR "
            "(effect IN ('inflow','outflow') AND floor_id IS NOT NULL "
            "AND pallet_slots > 0 AND scope_key <> 'none')",
            name="ck_capacity_forecast_plans_effect_consistency",
        ),
        CheckConstraint(
            "(status = 'active' AND cancelled_by IS NULL AND cancelled_at IS NULL) OR "
            "(status = 'cancelled' AND cancelled_at IS NOT NULL)",
            name="ck_capacity_forecast_plans_cancel_consistency",
        ),
        UniqueConstraint(
            "source_type",
            "source_id",
            "scope_key",
            name="uq_capacity_forecast_plans_source_scope",
        ),
        Index(
            "ix_capacity_forecast_plans_date_status",
            "planned_date",
            "status",
        ),
        Index(
            "ix_capacity_forecast_plans_floor_date",
            "floor_id",
            "planned_date",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    source_type: Mapped[str] = mapped_column(String(30), nullable=False)
    source_id: Mapped[int] = mapped_column(Integer, nullable=False)
    source_number_snapshot: Mapped[str] = mapped_column(String(100), nullable=False)
    source_label_snapshot: Mapped[str] = mapped_column(String(250), nullable=False)
    effect: Mapped[str] = mapped_column(String(20), nullable=False)
    floor_id: Mapped[int | None] = mapped_column(
        ForeignKey("warehouse_floors.id", ondelete="RESTRICT"), nullable=True
    )
    scope_key: Mapped[str] = mapped_column(String(50), nullable=False)
    planned_date: Mapped[date] = mapped_column(Date, nullable=False)
    pallet_slots: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), default="active", server_default="active", nullable=False
    )
    version: Mapped[int] = mapped_column(
        Integer, default=1, server_default="1", nullable=False
    )
    last_operation_key: Mapped[str] = mapped_column(String(64), nullable=False)
    last_request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    updated_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    cancelled_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
