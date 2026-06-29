from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
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
    from app.models.order import OrderItem


class RequisitionDailySequence(Base):
    __tablename__ = "requisition_daily_sequences"

    sequence_date: Mapped[date] = mapped_column(Date, primary_key=True)
    last_value: Mapped[int] = mapped_column(Integer, nullable=False)


class Requisition(Base):
    __tablename__ = "material_requisitions"
    __table_args__ = (
        UniqueConstraint(
            "requisition_number",
            name="uq_material_requisitions_number",
        ),
        Index("ix_material_requisitions_date", "requisition_date"),
        Index("ix_material_requisitions_status", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    requisition_number: Mapped[str] = mapped_column(String(40), nullable=False)
    requisition_date: Mapped[date] = mapped_column(Date, nullable=False)
    supplier_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    status: Mapped[str] = mapped_column(
        String(30),
        default="已报料",
        nullable=False,
    )
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.current_timestamp(),
        nullable=False,
    )

    items: Mapped[list["RequisitionItem"]] = relationship(
        back_populates="requisition",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="RequisitionItem.id",
    )


class RequisitionItem(Base):
    __tablename__ = "material_requisition_items"
    __table_args__ = (
        Index(
            "ix_material_requisition_items_requisition_id",
            "requisition_id",
        ),
        Index(
            "ix_material_requisition_items_order_item_id",
            "order_item_id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    requisition_id: Mapped[int] = mapped_column(
        ForeignKey("material_requisitions.id", ondelete="CASCADE"),
        nullable=False,
    )
    order_item_id: Mapped[int] = mapped_column(
        ForeignKey("sales_order_items.id", ondelete="RESTRICT"),
        nullable=False,
    )
    inventory_deducted_qty: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
    )
    requisition_qty: Mapped[int] = mapped_column(Integer, nullable=False)
    cardboard_len: Mapped[Decimal] = mapped_column(
        Numeric(12, 2),
        nullable=False,
    )
    cardboard_width: Mapped[Decimal] = mapped_column(
        Numeric(12, 2),
        nullable=False,
    )
    pieces_per_box: Mapped[int | None] = mapped_column(Integer, nullable=True)
    required_piece_qty: Mapped[int | None] = mapped_column(Integer, nullable=True)
    special_process: Mapped[str] = mapped_column(
        String(30),
        default="无",
        nullable=False,
    )
    material_snapshot: Mapped[str | None] = mapped_column(
        String(250),
        nullable=True,
    )
    product_code_snapshot: Mapped[str | None] = mapped_column(
        String(150),
        nullable=True,
    )
    product_name_snapshot: Mapped[str] = mapped_column(
        String(250),
        nullable=False,
    )
    specification_snapshot: Mapped[str | None] = mapped_column(
        String(150),
        nullable=True,
    )
    remark: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(30),
        default="有效",
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.current_timestamp(),
        nullable=False,
    )

    requisition: Mapped["Requisition"] = relationship(back_populates="items")
    order_item: Mapped["OrderItem"] = relationship()
