"""Append-only multi-receipt delivery evidence, not another inventory ledger."""
from datetime import datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, Numeric, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column
from app.models import Base


class FinanceDeliveryGraphCostFact(Base):
    __tablename__ = "finance_delivery_graph_cost_facts"
    __table_args__ = (
        CheckConstraint("(delivery_inventory_allocation_id IS NOT NULL AND unordered_allocation_id IS NULL) OR (delivery_inventory_allocation_id IS NULL AND unordered_allocation_id IS NOT NULL)", name="ck_graph_cost_source"),
        CheckConstraint("snapshot_version > 0 AND consumed_quantity > 0 AND source_offset >= 0 AND source_quantity >= source_offset + consumed_quantity AND total_cost >= 0", name="ck_graph_cost_quantity"),
        CheckConstraint("length(currency) = 3 AND length(source_fingerprint) = 64", name="ck_graph_cost_text"),
        UniqueConstraint("delivery_inventory_allocation_id", "snapshot_version", name="uq_graph_cost_inventory_version"),
        UniqueConstraint("unordered_allocation_id", "snapshot_version", name="uq_graph_cost_unordered_version"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    delivery_item_id: Mapped[int] = mapped_column(ForeignKey("sales_delivery_items.id", ondelete="RESTRICT"), index=True)
    delivery_inventory_allocation_id: Mapped[int | None] = mapped_column(ForeignKey("delivery_inventory_allocations.id", ondelete="RESTRICT"))
    unordered_allocation_id: Mapped[int | None] = mapped_column(ForeignKey("unordered_finished_delivery_allocations.id", ondelete="RESTRICT"))
    inventory_lot_id: Mapped[int] = mapped_column(ForeignKey("inventory_lots.id", ondelete="RESTRICT"))
    consume_movement_id: Mapped[int] = mapped_column(ForeignKey("inventory_movements.id", ondelete="RESTRICT"))
    snapshot_version: Mapped[int] = mapped_column(Integer)
    source_quantity: Mapped[int] = mapped_column(Integer)
    source_offset: Mapped[int] = mapped_column(Integer)
    consumed_quantity: Mapped[int] = mapped_column(Integer)
    total_cost: Mapped[Decimal] = mapped_column(Numeric(20, 6))
    currency: Mapped[str] = mapped_column(String(3))
    source_fingerprint: Mapped[str] = mapped_column(String(64))
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.current_timestamp())


class FinanceDeliveryGraphCostPortion(Base):
    __tablename__ = "finance_delivery_graph_cost_portions"
    __table_args__ = (
        UniqueConstraint("fact_id", "ordinal", name="uq_graph_cost_portion"),
        CheckConstraint("ordinal >= 0 AND full_output_cost >= 0 AND charged_cost >= 0 AND tax_rate >= 0 AND tax_rate <= 1", name="ck_graph_cost_portion_amount"),
        CheckConstraint("(purchase_receipt_fact_id IS NOT NULL AND purpose_allocation_id IS NOT NULL AND external_receipt_item_id IS NULL) OR (purchase_receipt_fact_id IS NULL AND purpose_allocation_id IS NULL AND external_receipt_item_id IS NOT NULL)", name="ck_graph_cost_portion_source"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    fact_id: Mapped[int] = mapped_column(ForeignKey("finance_delivery_graph_cost_facts.id", ondelete="RESTRICT"), index=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    purchase_receipt_fact_id: Mapped[int | None] = mapped_column(ForeignKey("purchase_receipt_facts.id", ondelete="RESTRICT"))
    purpose_allocation_id: Mapped[int | None] = mapped_column(ForeignKey("incoming_receipt_purpose_allocations.id", ondelete="RESTRICT"))
    external_receipt_item_id: Mapped[int | None] = mapped_column(ForeignKey("external_packaging_receipt_items.id", ondelete="RESTRICT"))
    full_output_cost: Mapped[Decimal] = mapped_column(Numeric(20, 6))
    charged_cost: Mapped[Decimal] = mapped_column(Numeric(20, 6))
    tax_included: Mapped[bool]
    tax_rate: Mapped[Decimal] = mapped_column(Numeric(8, 6))
