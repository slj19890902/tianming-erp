from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base


class FinanceDeliveryMaterialCostFact(Base):
    """Immutable direct-material cost frozen for one delivery source event.

    The row is deliberately separate from the mutable delivery-allocation
    progress fields.  A cancellation or return changes the active quantity on
    the source allocation; it never rewrites the original cost evidence.
    Re-dispatching a reusable source allocation creates the next snapshot
    version instead of mutating history.
    """

    __tablename__ = "finance_delivery_material_cost_facts"
    __table_args__ = (
        CheckConstraint(
            "source_kind IN ("
            "'inventory_allocation','unordered_inventory_allocation',"
            "'bom_direct_completion')",
            name="ck_finance_delivery_material_cost_facts_source_kind",
        ),
        CheckConstraint(
            "((source_kind = 'inventory_allocation' "
            "AND delivery_inventory_allocation_id IS NOT NULL "
            "AND unordered_finished_delivery_allocation_id IS NULL "
            "AND bom_component_direct_delivery_allocation_id IS NULL "
            "AND inventory_lot_id IS NOT NULL) OR "
            "(source_kind = 'unordered_inventory_allocation' "
            "AND delivery_inventory_allocation_id IS NULL "
            "AND unordered_finished_delivery_allocation_id IS NOT NULL "
            "AND bom_component_direct_delivery_allocation_id IS NULL "
            "AND inventory_lot_id IS NOT NULL) OR "
            "(source_kind = 'bom_direct_completion' "
            "AND delivery_inventory_allocation_id IS NULL "
            "AND unordered_finished_delivery_allocation_id IS NULL "
            "AND bom_component_direct_delivery_allocation_id IS NOT NULL "
            "AND inventory_lot_id IS NULL "
            "AND production_completion_id IS NOT NULL))",
            name="ck_finance_delivery_material_cost_facts_source_identity",
        ),
        CheckConstraint(
            "snapshot_version >= 1 AND consumed_quantity > 0",
            name="ck_finance_delivery_material_cost_facts_quantity_version",
        ),
        CheckConstraint(
            "unit_material_cost > 0 AND total_material_cost > 0",
            name="ck_finance_delivery_material_cost_facts_cost",
        ),
        CheckConstraint(
            "length(trim(quantity_unit_snapshot)) > 0 "
            "AND length(trim(currency_snapshot)) = 3 "
            "AND length(source_fingerprint) = 64",
            name="ck_finance_delivery_material_cost_facts_frozen_text",
        ),
        CheckConstraint(
            "tax_rate_snapshot >= 0 AND tax_rate_snapshot <= 1",
            name="ck_finance_delivery_material_cost_facts_tax_rate",
        ),
        CheckConstraint(
            "((incoming_receipt_purpose_allocation_id IS NOT NULL "
            "AND composite_physical_group_receipt_id IS NULL) OR "
            "(incoming_receipt_purpose_allocation_id IS NULL "
            "AND composite_physical_group_receipt_id IS NOT NULL))",
            name="ck_finance_delivery_material_cost_facts_receipt_source",
        ),
        UniqueConstraint(
            "delivery_inventory_allocation_id",
            "snapshot_version",
            name="uq_finance_delivery_material_cost_facts_inventory_version",
        ),
        UniqueConstraint(
            "unordered_finished_delivery_allocation_id",
            "snapshot_version",
            name="uq_finance_delivery_material_cost_facts_unordered_version",
        ),
        UniqueConstraint(
            "bom_component_direct_delivery_allocation_id",
            "snapshot_version",
            name="uq_finance_delivery_material_cost_facts_bom_version",
        ),
        UniqueConstraint(
            "source_fingerprint",
            name="uq_finance_delivery_material_cost_facts_fingerprint",
        ),
        Index(
            "ix_finance_delivery_material_cost_facts_delivery_item",
            "delivery_item_id",
            "source_kind",
        ),
        Index(
            "ix_finance_delivery_material_cost_facts_delivery",
            "delivery_id",
        ),
        Index(
            "ix_finance_delivery_material_cost_facts_receipt_fact",
            "purchase_receipt_fact_id",
        ),
        Index(
            "ix_finance_delivery_material_cost_facts_composite_receipt",
            "composite_physical_group_receipt_id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    source_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    snapshot_version: Mapped[int] = mapped_column(Integer, nullable=False)
    delivery_id: Mapped[int] = mapped_column(
        ForeignKey("sales_deliveries.id", ondelete="RESTRICT"), nullable=False
    )
    delivery_item_id: Mapped[int] = mapped_column(
        ForeignKey("sales_delivery_items.id", ondelete="RESTRICT"), nullable=False
    )
    delivery_inventory_allocation_id: Mapped[int | None] = mapped_column(
        ForeignKey("delivery_inventory_allocations.id", ondelete="RESTRICT"),
        nullable=True,
    )
    unordered_finished_delivery_allocation_id: Mapped[int | None] = mapped_column(
        ForeignKey(
            "unordered_finished_delivery_allocations.id", ondelete="RESTRICT"
        ),
        nullable=True,
    )
    bom_component_direct_delivery_allocation_id: Mapped[int | None] = mapped_column(
        ForeignKey(
            "bom_component_direct_delivery_allocations.id", ondelete="RESTRICT"
        ),
        nullable=True,
    )
    inventory_lot_id: Mapped[int | None] = mapped_column(
        ForeignKey("inventory_lots.id", ondelete="RESTRICT"), nullable=True
    )
    production_completion_id: Mapped[int | None] = mapped_column(
        ForeignKey("production_completions.id", ondelete="RESTRICT"), nullable=True
    )
    incoming_receipt_purpose_allocation_id: Mapped[int | None] = mapped_column(
        ForeignKey(
            "incoming_receipt_purpose_allocations.id", ondelete="RESTRICT"
        ),
        nullable=True,
    )
    composite_physical_group_receipt_id: Mapped[int | None] = mapped_column(
        ForeignKey(
            "composite_physical_group_receipts.id", ondelete="RESTRICT"
        ),
        nullable=True,
    )
    purchase_receipt_fact_id: Mapped[int] = mapped_column(
        ForeignKey("purchase_receipt_facts.id", ondelete="RESTRICT"),
        nullable=False,
    )
    consumed_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    quantity_unit_snapshot: Mapped[str] = mapped_column(String(20), nullable=False)
    unit_material_cost: Mapped[Decimal] = mapped_column(
        Numeric(20, 6), nullable=False
    )
    total_material_cost: Mapped[Decimal] = mapped_column(
        Numeric(20, 6), nullable=False
    )
    currency_snapshot: Mapped[str] = mapped_column(String(3), nullable=False)
    tax_included_snapshot: Mapped[bool] = mapped_column(Boolean, nullable=False)
    tax_rate_snapshot: Mapped[Decimal] = mapped_column(
        Numeric(8, 6), nullable=False
    )
    source_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )
