from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, DateTime, Integer, Numeric, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base


class SalesOrderItemEstimatedCostSnapshot(Base):
    """Immutable internal estimate; it is never an actual accounting cost."""

    __tablename__ = "sales_order_item_estimated_cost_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "order_item_reference_snapshot",
            "snapshot_version",
            name="uq_order_item_estimated_cost_snapshot_version",
        ),
        UniqueConstraint(
            "order_item_reference_snapshot",
            "source_fingerprint",
            name="uq_order_item_estimated_cost_snapshot_fingerprint",
        ),
        CheckConstraint("snapshot_version > 0", name="ck_order_item_estimated_cost_version"),
        CheckConstraint("order_quantity_snapshot > 0", name="ck_order_item_estimated_cost_quantity"),
        CheckConstraint(
            "calculation_status IN ('calculated','partial','missing')",
            name="ck_order_item_estimated_cost_status",
        ),
        CheckConstraint("scope_code = 'estimated_total'", name="ck_order_item_estimated_cost_scope"),
        CheckConstraint("loss_rate IN (0.03,0.05)", name="ck_order_item_estimated_cost_loss_rate"),
        CheckConstraint(
            "one_time_fee_total >= 0 AND known_estimated_subtotal >= 0",
            name="ck_order_item_estimated_cost_nonnegative",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    sales_order_item_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    order_item_reference_snapshot: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    snapshot_version: Mapped[int] = mapped_column(Integer, nullable=False)
    source_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    material_cost_snapshot_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    material_cost_snapshot_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    calculation_status: Mapped[str] = mapped_column(String(20), nullable=False)
    scope_code: Mapped[str] = mapped_column(String(30), nullable=False, default="estimated_total")
    rule_version: Mapped[str] = mapped_column(String(50), nullable=False)
    precision_version: Mapped[str] = mapped_column(String(50), nullable=False)
    order_quantity_snapshot: Mapped[int] = mapped_column(Integer, nullable=False)
    loss_rate: Mapped[Decimal] = mapped_column(Numeric(8, 6), nullable=False)
    processing_category: Mapped[str | None] = mapped_column(String(30), nullable=True)
    printing_color_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    processing_batch_cost: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    processing_unit_cost: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 6), nullable=True
    )
    extra_color_unit_cost: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    loss_material_total_cost: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    processing_total_cost: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 2), nullable=True
    )
    one_time_fee_total: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    known_estimated_subtotal: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    estimated_unit_total_cost: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    estimated_order_total_cost: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    tax_rate_reference: Mapped[Decimal] = mapped_column(Numeric(8, 6), nullable=False)
    tax_basis_code: Mapped[str] = mapped_column(String(50), nullable=False)
    breakdown_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    missing_items_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    calculated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )
    created_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )
