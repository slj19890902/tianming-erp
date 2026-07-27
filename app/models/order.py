from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
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
    from app.models.product import Product


class OrderDailySequence(Base):
    __tablename__ = "order_daily_sequences"

    sequence_date: Mapped[date] = mapped_column(Date, primary_key=True)
    last_value: Mapped[int] = mapped_column(Integer, nullable=False)


class Order(Base):
    __tablename__ = "sales_orders"
    __table_args__ = (
        CheckConstraint(
            "status IN ("
            "'pending_confirmation', 'pending_production', 'production', "
            "'pending_delivery', 'partially_delivered', 'pending_reconciliation', "
            "'pending_invoice', 'pending_payment', 'delivered', 'completed', "
            "'archived', 'closed', 'dead', 'cancelled'"
            ")",
            name="ck_sales_orders_status",
        ),
        CheckConstraint(
            "payment_status IN ('unpaid', 'paid')",
            name="ck_sales_orders_payment_status",
        ),
        UniqueConstraint("order_number", name="uq_sales_orders_order_number"),
        Index("ix_sales_orders_customer_id", "customer_id"),
        Index("ix_sales_orders_customer_po", "customer_po"),
        Index("ix_sales_orders_customer_po_group", "customer_id", "customer_po"),
        Index("ix_sales_orders_order_date", "order_date"),
        Index("ix_sales_orders_status", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    order_number: Mapped[str] = mapped_column(String(40), nullable=False)
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id", ondelete="RESTRICT"),
        nullable=False,
    )
    customer_po: Mapped[str | None] = mapped_column(String(150), nullable=True)
    order_date: Mapped[date] = mapped_column(Date, nullable=False)
    delivery_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(
        String(30),
        default="pending_production",
        nullable=False,
    )
    payment_status: Mapped[str] = mapped_column(
        String(20),
        default="unpaid",
        nullable=False,
    )
    total_amount: Mapped[Decimal] = mapped_column(
        Numeric(14, 2),
        default=Decimal("0"),
        nullable=False,
    )
    remark: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.current_timestamp(),
        nullable=False,
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        onupdate=func.current_timestamp(),
        nullable=True,
    )

    items: Mapped[list["OrderItem"]] = relationship(
        back_populates="order",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="OrderItem.id",
    )


class OrderItem(Base):
    __tablename__ = "sales_order_items"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_sales_order_items_quantity"),
        CheckConstraint("unit_price >= 0", name="ck_sales_order_items_unit_price"),
        CheckConstraint("subtotal >= 0", name="ck_sales_order_items_subtotal"),
        CheckConstraint(
            "material_status IN ('pending', 'received')",
            name="ck_sales_order_items_material_status",
        ),
        CheckConstraint(
            "combination_mode_snapshot IS NULL OR "
            "combination_mode_snapshot IN ('parent_priced_set', 'component_priced')",
            name="ck_sales_order_items_combination_mode_snapshot",
        ),
        CheckConstraint(
            "combination_role IN ('standalone', 'set_parent', 'priced_component')",
            name="ck_sales_order_items_combination_role",
        ),
        CheckConstraint(
            "combination_role <> 'priced_component' OR "
            "(combination_mode_snapshot = 'component_priced' "
            "AND combination_group_key IS NOT NULL "
            "AND length(trim(combination_group_key)) > 0 "
            "AND combination_parent_product_id IS NOT NULL "
            "AND combination_parent_name_snapshot IS NOT NULL "
            "AND length(trim(combination_parent_name_snapshot)) > 0 "
            "AND combination_set_quantity_snapshot IS NOT NULL "
            "AND combination_set_quantity_snapshot > 0 "
            "AND combination_quantity_per_set_snapshot IS NOT NULL "
            "AND combination_quantity_per_set_snapshot > 0)",
            name="ck_sales_order_items_priced_component_source",
        ),
        CheckConstraint(
            "combination_role <> 'standalone' OR "
            "(combination_mode_snapshot IS NULL "
            "AND combination_group_key IS NULL "
            "AND combination_parent_product_id IS NULL "
            "AND combination_parent_name_snapshot IS NULL "
            "AND combination_set_quantity_snapshot IS NULL "
            "AND combination_quantity_per_set_snapshot IS NULL)",
            name="ck_sales_order_items_standalone_without_combination_source",
        ),
        CheckConstraint(
            "combination_role <> 'set_parent' OR "
            "(combination_mode_snapshot = 'parent_priced_set' "
            "AND combination_group_key IS NULL "
            "AND combination_parent_product_id IS NULL "
            "AND combination_parent_name_snapshot IS NULL "
            "AND combination_set_quantity_snapshot IS NULL "
            "AND combination_quantity_per_set_snapshot IS NULL)",
            name="ck_sales_order_items_set_parent_source",
        ),
        Index("ix_sales_order_items_order_id", "order_id"),
        Index("ix_sales_order_items_product_id", "product_id"),
        Index("ux_sales_order_items_item_order_number", "item_order_number", unique=True),
        Index("ix_sales_order_items_material_status", "material_status"),
        Index("ix_sales_order_items_snapshot_product_code", "snapshot_product_code"),
        Index("ix_sales_order_items_snapshot_product_name", "snapshot_product_name"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(
        ForeignKey("sales_orders.id", ondelete="CASCADE"),
        nullable=False,
    )
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"),
        nullable=False,
    )
    item_order_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    item_sequence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    delivered_quantity: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
    )
    is_force_closed: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
    )
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    subtotal: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    material_status: Mapped[str] = mapped_column(
        String(20),
        default="pending",
        nullable=False,
    )
    material_received_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
    )
    material_received_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    snapshot_product_name: Mapped[str] = mapped_column(String(250), nullable=False)
    snapshot_product_code: Mapped[str | None] = mapped_column(
        String(150),
        nullable=True,
    )
    snapshot_spec: Mapped[str | None] = mapped_column(String(150), nullable=True)
    snapshot_material: Mapped[str | None] = mapped_column(String(250), nullable=True)
    snapshot_original_material_code: Mapped[str | None] = mapped_column(
        String(250), nullable=True
    )
    snapshot_customer_model: Mapped[str | None] = mapped_column(Text, nullable=True)
    snapshot_production_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    inventory_deducted_qty: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
    )
    requisition_qty: Mapped[int | None] = mapped_column(Integer, nullable=True)
    requisition_status: Mapped[str] = mapped_column(
        String(30),
        default="未报料",
        nullable=False,
        index=True,
    )
    special_process: Mapped[str] = mapped_column(
        String(30),
        default="无",
        nullable=False,
    )
    requisition_spec: Mapped[str | None] = mapped_column(
        String(150),
        nullable=True,
    )
    cardboard_len: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 2),
        nullable=True,
    )
    cardboard_width: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 2),
        nullable=True,
    )
    requisition_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    supplier_delivery_time: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
    )
    supplier_order_number: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
    )
    requisition_remark: Mapped[str | None] = mapped_column(Text, nullable=True)
    # v0.19.2-B Hotfix: 常用箱层数/楞型/材质快照 + 订单级图纸
    layer_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    flute_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    material_id: Mapped[int | None] = mapped_column(
        ForeignKey("materials.id", ondelete="SET NULL"), nullable=True
    )
    snapshot_supplier_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    snapshot_weight: Mapped[str | None] = mapped_column(String(200), nullable=True)
    drawing_file: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # v0.19.2-B: 报料快照（常用箱数据在下单时固化，历史订单保留 NULL）
    snapshot_report_length_mm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    snapshot_report_width_mm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    snapshot_crease_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    snapshot_crease_left_mm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    snapshot_crease_middle_mm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    snapshot_crease_right_mm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    snapshot_report_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    snapshot_base_report_length_mm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    snapshot_base_report_width_mm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    snapshot_base_crease_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    snapshot_base_crease_left_mm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    snapshot_base_crease_middle_mm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    snapshot_base_crease_right_mm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    snapshot_base_report_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    snapshot_splice_mode: Mapped[str | None] = mapped_column(String(20), nullable=True)
    snapshot_pieces_per_box: Mapped[int | None] = mapped_column(Integer, nullable=True)
    snapshot_flap_mm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    combination_mode_snapshot: Mapped[str | None] = mapped_column(
        String(30), nullable=True
    )
    combination_role: Mapped[str] = mapped_column(
        String(30), default="standalone", server_default="standalone", nullable=False
    )
    combination_group_key: Mapped[str | None] = mapped_column(String(80), nullable=True)
    combination_parent_product_id: Mapped[int | None] = mapped_column(
        ForeignKey("products.id", ondelete="SET NULL"), nullable=True
    )
    combination_parent_name_snapshot: Mapped[str | None] = mapped_column(
        String(250), nullable=True
    )
    combination_set_quantity_snapshot: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    combination_quantity_per_set_snapshot: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.current_timestamp(),
        nullable=False,
    )

    order: Mapped["Order"] = relationship(back_populates="items")
    product: Mapped["Product"] = relationship(foreign_keys=[product_id])


class OrderItemNumberSequence(Base):
    __tablename__ = "order_item_number_sequences"

    order_id: Mapped[int] = mapped_column(
        ForeignKey("sales_orders.id", ondelete="CASCADE"),
        primary_key=True,
    )
    last_item_sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
        nullable=False,
    )
