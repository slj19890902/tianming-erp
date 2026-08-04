from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models import Base

if TYPE_CHECKING:
    from app.models.customer import Customer
    from app.models.order import OrderItem
    from app.models.product import Product


class DeliveryDailySequence(Base):
    __tablename__ = "delivery_daily_sequences"

    sequence_date: Mapped[date] = mapped_column(Date, primary_key=True)
    last_value: Mapped[int] = mapped_column(Integer, nullable=False)


class Delivery(Base):
    __tablename__ = "sales_deliveries"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'dispatched', 'voided')",
            name="ck_sales_deliveries_status",
        ),
        CheckConstraint(
            "source_mode IN ('order', 'unordered_finished', 'mixed')",
            name="ck_sales_deliveries_source_mode",
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
    source_mode: Mapped[str] = mapped_column(
        String(30),
        default="order",
        server_default="order",
        nullable=False,
    )
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
    ever_dispatched_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
    )
    voided_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    voided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
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
        CheckConstraint(
            "source_type IN ('order', 'unordered_finished')",
            name="ck_sales_delivery_items_source_type",
        ),
        CheckConstraint(
            "(source_type = 'order' AND order_item_id IS NOT NULL AND product_id IS NULL "
            "AND unit_price_snapshot IS NULL) OR "
            "(source_type = 'unordered_finished' AND order_item_id IS NULL "
            "AND product_id IS NOT NULL AND product_code_snapshot IS NOT NULL "
            "AND product_name_snapshot IS NOT NULL AND unit_snapshot IS NOT NULL "
            "AND (unit_price_snapshot IS NULL OR unit_price_snapshot > 0) "
            "AND price_source IS NOT NULL)",
            name="ck_sales_delivery_items_source_reference",
        ),
        UniqueConstraint(
            "delivery_id",
            "order_item_id",
            name="uq_sales_delivery_items_order_item",
        ),
        Index("ix_sales_delivery_items_delivery_id", "delivery_id"),
        Index("ix_sales_delivery_items_order_item_id", "order_item_id"),
        Index("ix_sales_delivery_items_product_id", "product_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    delivery_id: Mapped[int] = mapped_column(
        ForeignKey("sales_deliveries.id", ondelete="CASCADE"),
        nullable=False,
    )
    source_type: Mapped[str] = mapped_column(
        String(30),
        default="order",
        server_default="order",
        nullable=False,
    )
    order_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("sales_order_items.id", ondelete="RESTRICT"),
        nullable=True,
    )
    product_id: Mapped[int | None] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"),
        nullable=True,
    )
    product_code_snapshot: Mapped[str | None] = mapped_column(String(150), nullable=True)
    product_name_snapshot: Mapped[str | None] = mapped_column(String(250), nullable=True)
    specification_snapshot: Mapped[str | None] = mapped_column(String(255), nullable=True)
    unit_snapshot: Mapped[str | None] = mapped_column(String(20), nullable=True)
    unit_price_snapshot: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 4), nullable=True
    )
    price_source: Mapped[str | None] = mapped_column(String(50), nullable=True)
    delivered_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    ordered_quantity_snapshot: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    order_remaining_snapshot: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    over_delivery_quantity: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    over_delivery_confirmed_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    over_delivery_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    remarks: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.current_timestamp(),
        nullable=False,
    )

    delivery: Mapped["Delivery"] = relationship(back_populates="items")
    order_item: Mapped["OrderItem | None"] = relationship()
    product: Mapped["Product | None"] = relationship()


class DeliveryPickTask(Base):
    """Authenticated driver picking snapshot for one pending delivery."""

    __tablename__ = "delivery_pick_tasks"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pushed', 'driver_confirmed', 'exception', 'applied', 'dispatched')",
            name="ck_delivery_pick_tasks_status",
        ),
        UniqueConstraint("delivery_id", name="uq_delivery_pick_tasks_delivery_id"),
        Index("ix_delivery_pick_tasks_customer_id", "customer_id"),
        Index("ix_delivery_pick_tasks_status", "status"),
        Index("ix_delivery_pick_tasks_assigned_to_status", "assigned_to", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    delivery_id: Mapped[int] = mapped_column(
        ForeignKey("sales_deliveries.id", ondelete="CASCADE"), nullable=False
    )
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(24), default="pushed", nullable=False)
    snapshot_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    assigned_to: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    submitted_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    applied_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime, onupdate=func.current_timestamp(), nullable=True
    )

    delivery: Mapped["Delivery"] = relationship()
    customer: Mapped["Customer"] = relationship()
    items: Mapped[list["DeliveryPickTaskItem"]] = relationship(
        back_populates="task",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="DeliveryPickTaskItem.id",
    )


class DeliveryPickTaskItem(Base):
    __tablename__ = "delivery_pick_task_items"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'picked', 'partial', 'no_stock')",
            name="ck_delivery_pick_task_items_status",
        ),
        CheckConstraint("original_quantity > 0", name="ck_delivery_pick_task_items_original"),
        CheckConstraint("picked_quantity >= 0", name="ck_delivery_pick_task_items_picked"),
        UniqueConstraint("task_id", "delivery_item_id", name="uq_delivery_pick_task_item_snapshot"),
        Index("ix_delivery_pick_task_items_task_id", "task_id"),
        Index("ix_delivery_pick_task_items_delivery_item_id", "delivery_item_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(
        ForeignKey("delivery_pick_tasks.id", ondelete="CASCADE"), nullable=False
    )
    delivery_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("sales_delivery_items.id", ondelete="SET NULL"), nullable=True
    )
    order_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("sales_order_items.id", ondelete="RESTRICT"), nullable=True
    )
    original_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    picked_quantity: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    product_code_snapshot: Mapped[str | None] = mapped_column(String(100), nullable=True)
    product_name_snapshot: Mapped[str | None] = mapped_column(String(255), nullable=True)
    specification_snapshot: Mapped[str | None] = mapped_column(String(255), nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime, onupdate=func.current_timestamp(), nullable=True
    )

    task: Mapped["DeliveryPickTask"] = relationship(back_populates="items")
    delivery_item: Mapped["DeliveryItem"] = relationship()
    order_item: Mapped["OrderItem"] = relationship()
