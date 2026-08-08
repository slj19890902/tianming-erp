from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
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


class SalesOrderItemMaterialCostSnapshot(Base):
    """Immutable material-only cost fact for one order-item version."""

    __tablename__ = "sales_order_item_material_cost_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "order_item_reference_snapshot",
            "snapshot_version",
            name="uq_order_item_material_cost_snapshot_version",
        ),
        UniqueConstraint(
            "order_item_reference_snapshot",
            "source_fingerprint",
            name="uq_order_item_material_cost_snapshot_fingerprint",
        ),
        CheckConstraint(
            "snapshot_version > 0",
            name="ck_order_item_material_cost_snapshot_version",
        ),
        CheckConstraint(
            "order_quantity_snapshot > 0",
            name="ck_order_item_material_cost_snapshot_quantity",
        ),
        CheckConstraint(
            "calculation_status IN ('calculated','partial','missing')",
            name="ck_order_item_material_cost_snapshot_status",
        ),
        CheckConstraint(
            "scope_code = 'material_only'",
            name="ck_order_item_material_cost_snapshot_scope",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # Logical reference only: a draft order may still be deleted under the
    # existing double-confirmation rule, while this immutable audit fact stays.
    sales_order_item_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    order_item_reference_snapshot: Mapped[str] = mapped_column(
        String(100), nullable=False, index=True
    )
    snapshot_version: Mapped[int] = mapped_column(Integer, nullable=False)
    source_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    calculation_status: Mapped[str] = mapped_column(String(20), nullable=False)
    scope_code: Mapped[str] = mapped_column(
        String(30), nullable=False, default="material_only"
    )
    formula_version: Mapped[str] = mapped_column(String(50), nullable=False)
    precision_version: Mapped[str] = mapped_column(String(50), nullable=False)
    order_quantity_snapshot: Mapped[int] = mapped_column(Integer, nullable=False)
    estimated_material_unit_cost: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 6), nullable=True
    )
    estimated_material_total_cost: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 2), nullable=True
    )
    known_material_subtotal: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 2), nullable=True
    )
    components_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    missing_items_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    calculated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )
    created_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )
