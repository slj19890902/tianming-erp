"""Explicit production plans over existing replenishment receipt lots."""
from datetime import datetime
from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column
from app.models import Base


class StockPreparationJob(Base):
    __tablename__ = "stock_preparation_jobs"
    __table_args__ = (
        CheckConstraint("status IN ('pending','completed','cancelled')", name="ck_stock_prep_status"),
        CheckConstraint("input_quantity > 0 AND expected_output > 0 AND actual_output >= 0", name="ck_stock_prep_qty"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    receipt_item_id: Mapped[int] = mapped_column(ForeignKey("incoming_receipt_items.id", ondelete="RESTRICT"), index=True)
    reservation_id: Mapped[int] = mapped_column(ForeignKey("inventory_reservations.id", ondelete="RESTRICT"), unique=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="RESTRICT"))
    product_snapshot: Mapped[str] = mapped_column(Text)
    input_quantity: Mapped[int] = mapped_column(Integer)
    expected_output: Mapped[int] = mapped_column(Integer)
    actual_output: Mapped[int] = mapped_column(Integer, default=0)
    output_lot_id: Mapped[int | None] = mapped_column(ForeignKey("inventory_lots.id", ondelete="RESTRICT"))
    status: Mapped[str] = mapped_column(String(20), default="pending")
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.current_timestamp())


class StockPreparationCommand(Base):
    __tablename__ = "stock_preparation_commands"
    operation_key: Mapped[str] = mapped_column(String(80), primary_key=True)
    receipt_item_id: Mapped[int] = mapped_column(ForeignKey("incoming_receipt_items.id", ondelete="RESTRICT"), index=True)
    request_json: Mapped[str] = mapped_column(Text)
    result_json: Mapped[str] = mapped_column(Text)
    actor_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.current_timestamp())
