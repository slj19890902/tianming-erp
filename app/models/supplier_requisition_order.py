"""v0.19.2-B: 供应商报料单模型"""
from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    DateTime,
    Date,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models import Base

if TYPE_CHECKING:
    pass


class SupplierRequisitionOrder(Base):
    __tablename__ = "supplier_requisition_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_number: Mapped[str] = mapped_column(String(40), nullable=False, unique=True)
    request_key: Mapped[str | None] = mapped_column(
        String(64), nullable=True, unique=True
    )
    supplier_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    material_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("materials.id"), nullable=True)
    layer_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    flute_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    report_length_mm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    report_width_mm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    crease_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    crease_left_mm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    crease_middle_mm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    crease_right_mm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cutting_mode: Mapped[str | None] = mapped_column(String(30), nullable=True)
    pieces_per_box: Mapped[int | None] = mapped_column(Integer, nullable=True)
    required_piece_qty: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    stock_deduction_qty: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    requisition_qty: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    remark: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="confirmed")
    created_by: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, server_default=func.now())
    voided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    items: Mapped[list["SupplierRequisitionOrderItem"]] = relationship(
        "SupplierRequisitionOrderItem",
        back_populates="supplier_order",
        cascade="all, delete-orphan",
    )


class SupplierRequisitionOrderItem(Base):
    __tablename__ = "supplier_requisition_order_items"
    __table_args__ = (
        Index(
            "ix_supplier_requisition_order_items_product_created",
            "product_id",
            "supplier_order_id",
        ),
        Index(
            "ix_supplier_requisition_order_items_material_id",
            "material_id",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    supplier_order_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("supplier_requisition_orders.id", ondelete="CASCADE"), nullable=False
    )
    order_item_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("sales_order_items.id", ondelete="SET NULL"), nullable=True
    )
    source_key: Mapped[str | None] = mapped_column(String(120), nullable=True)
    product_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("products.id", ondelete="SET NULL"), nullable=True
    )
    material_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("materials.id", ondelete="SET NULL"), nullable=True
    )
    material_code_snapshot: Mapped[str | None] = mapped_column(
        String(100), nullable=True
    )
    supplier_name_snapshot: Mapped[str | None] = mapped_column(
        String(200), nullable=True
    )
    layer_count_snapshot: Mapped[int | None] = mapped_column(Integer, nullable=True)
    flute_type_snapshot: Mapped[str | None] = mapped_column(String(50), nullable=True)
    order_number: Mapped[str | None] = mapped_column(String(40), nullable=True)
    product_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    product_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    report_length_mm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    report_width_mm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    stock_deduction_qty: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    requisition_qty: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cutting_mode: Mapped[str | None] = mapped_column(String(30), nullable=True)
    pieces_per_box: Mapped[int | None] = mapped_column(Integer, nullable=True)
    required_piece_qty: Mapped[int | None] = mapped_column(Integer, nullable=True)
    customer_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    delivery_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    supplier_order: Mapped["SupplierRequisitionOrder"] = relationship(
        "SupplierRequisitionOrder", back_populates="items"
    )
