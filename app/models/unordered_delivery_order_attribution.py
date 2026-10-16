"""Audited fulfillment of an order by an already dispatched unordered line.

The physical allocation and the delivery's frozen sale contract stay on the
original delivery item.  This separate link gives the order a truthful
fulfillment source without manufacturing a second shipment.
"""

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base


class UnorderedDeliveryOrderAttribution(Base):
    __tablename__ = "unordered_delivery_order_attributions"
    __table_args__ = (
        UniqueConstraint("delivery_item_id", name="uq_unordered_delivery_order_attribution_line"),
        UniqueConstraint("operation_key", name="uq_unordered_delivery_order_attribution_operation"),
        CheckConstraint("quantity > 0", name="ck_unordered_delivery_order_attribution_quantity"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    delivery_item_id: Mapped[int] = mapped_column(
        ForeignKey("sales_delivery_items.id", ondelete="RESTRICT"), nullable=False
    )
    order_item_id: Mapped[int] = mapped_column(
        ForeignKey("sales_order_items.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    actor_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    operation_key: Mapped[str] = mapped_column(String(120), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )
