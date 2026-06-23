from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    Date,
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

if TYPE_CHECKING:
    from app.models.order import OrderItem


class DeliveryDailySequence(Base):
    __tablename__ = "delivery_daily_sequences"

    sequence_date: Mapped[date] = mapped_column(Date, primary_key=True)
    last_value: Mapped[int] = mapped_column(Integer, nullable=False)


class Delivery(Base):
    __tablename__ = "sales_deliveries"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'dispatched')",
            name="ck_sales_deliveries_status",
        ),
        UniqueConstraint(
            "delivery_number",
            name="uq_sales_deliveries_delivery_number",
        ),
        Index("ix_sales_deliveries_customer_id", "customer_id"),
        Index("ix_sales_deliveries_delivery_date", "delivery_date"),
        Index("ix_sales_deliveries_status", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    delivery_number: Mapped[str] = mapped_column(String(40), nullable=False)
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id", ondelete="RESTRICT"),
        nullable=False,
    )
    delivery_date: Mapped[date] = mapped_column(Date, nullable=False)
    vehicle_number: Mapped[str | None] = mapped_column(String(50), nullable=True)
    status: Mapped[str] = mapped_column(
        String(20),
        default="pending",
        nullable=False,
    )
    total_quantity: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    dispatched_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    printed_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    printed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.current_timestamp(),
        nullable=False,
    )

    items: Mapped[list["DeliveryItem"]] = relationship(
        back_populates="delivery",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="DeliveryItem.id",
    )


class DeliveryItem(Base):
    __tablename__ = "sales_delivery_items"
    __table_args__ = (
        CheckConstraint(
            "delivered_quantity > 0",
            name="ck_sales_delivery_items_quantity",
        ),
        UniqueConstraint(
            "delivery_id",
            "order_item_id",
            name="uq_sales_delivery_items_order_item",
        ),
        Index("ix_sales_delivery_items_delivery_id", "delivery_id"),
        Index("ix_sales_delivery_items_order_item_id", "order_item_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    delivery_id: Mapped[int] = mapped_column(
        ForeignKey("sales_deliveries.id", ondelete="CASCADE"),
        nullable=False,
    )
    order_item_id: Mapped[int] = mapped_column(
        ForeignKey("sales_order_items.id", ondelete="RESTRICT"),
        nullable=False,
    )
    delivered_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    remarks: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.current_timestamp(),
        nullable=False,
    )

    delivery: Mapped["Delivery"] = relationship(back_populates="items")
    order_item: Mapped["OrderItem"] = relationship()
