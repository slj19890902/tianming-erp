from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models import Base


class IncomingReceipt(Base):
    """One immutable receiving operation, possibly containing several lines."""

    __tablename__ = "incoming_receipts"
    __table_args__ = (
        CheckConstraint(
            "status IN ('posted','reversed')",
            name="ck_incoming_receipts_status",
        ),
        UniqueConstraint("receipt_number", name="uq_incoming_receipts_number"),
        UniqueConstraint("idempotency_key", name="uq_incoming_receipts_idempotency"),
        Index("ix_incoming_receipts_received_at", "received_at"),
        Index("ix_incoming_receipts_status", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    receipt_number: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="posted")
    received_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    received_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    idempotency_key: Mapped[str] = mapped_column(String(100), nullable=False)
    remarks: Mapped[str | None] = mapped_column(Text, nullable=True)
    reversed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    reversed_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    reversal_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )

    items: Mapped[list["IncomingReceiptItem"]] = relationship(
        back_populates="receipt",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="IncomingReceiptItem.id",
    )


class IncomingReceiptItem(Base):
    """The quantity fact and human variance decision for one received line."""

    __tablename__ = "incoming_receipt_items"
    __table_args__ = (
        CheckConstraint("planned_quantity > 0", name="ck_incoming_receipt_items_planned"),
        CheckConstraint("received_quantity > 0", name="ck_incoming_receipt_items_received"),
        CheckConstraint(
            "cumulative_received_quantity > 0",
            name="ck_incoming_receipt_items_cumulative",
        ),
        CheckConstraint(
            "variance_type IN ('matched','short','over')",
            name="ck_incoming_receipt_items_variance_type",
        ),
        CheckConstraint(
            "resolution_status IN ('not_required','pending','resolved')",
            name="ck_incoming_receipt_items_resolution_status",
        ),
        CheckConstraint(
            "resolution_action IS NULL OR resolution_action IN "
            "('await_supplier','accept_short','all_to_production',"
            "'transfer_to_semi_inventory')",
            name="ck_incoming_receipt_items_resolution_action",
        ),
        CheckConstraint(
            "status IN ('posted','reversed')",
            name="ck_incoming_receipt_items_status",
        ),
        Index(
            "ix_incoming_receipt_items_order_item",
            "order_item_id",
            "status",
        ),
        Index(
            "ix_incoming_receipt_items_requisition_item",
            "requisition_item_id",
            "status",
        ),
        Index("ix_incoming_receipt_items_receipt", "receipt_id"),
        Index("ix_incoming_receipt_items_surplus_lot", "surplus_inventory_lot_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    receipt_id: Mapped[int] = mapped_column(
        ForeignKey("incoming_receipts.id", ondelete="CASCADE"), nullable=False
    )
    order_id: Mapped[int] = mapped_column(
        ForeignKey("sales_orders.id", ondelete="RESTRICT"), nullable=False
    )
    order_item_id: Mapped[int] = mapped_column(
        ForeignKey("sales_order_items.id", ondelete="RESTRICT"), nullable=False
    )
    requisition_id: Mapped[int | None] = mapped_column(
        ForeignKey("material_requisitions.id", ondelete="SET NULL"), nullable=True
    )
    requisition_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("material_requisition_items.id", ondelete="SET NULL"), nullable=True
    )
    supplier_order_id: Mapped[int | None] = mapped_column(
        ForeignKey("supplier_requisition_orders.id", ondelete="SET NULL"), nullable=True
    )
    supplier_order_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("supplier_requisition_order_items.id", ondelete="SET NULL"),
        nullable=True,
    )
    planned_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    received_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    cumulative_received_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    variance_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    variance_type: Mapped[str] = mapped_column(String(20), nullable=False)
    resolution_status: Mapped[str] = mapped_column(String(20), nullable=False)
    resolution_action: Mapped[str | None] = mapped_column(String(40), nullable=True)
    resolution_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    surplus_inventory_lot_id: Mapped[int | None] = mapped_column(
        ForeignKey("inventory_lots.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="posted")
    reversal_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    reversed_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    reversed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )

    receipt: Mapped[IncomingReceipt] = relationship(back_populates="items")
