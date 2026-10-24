"""Explicit material work owned by an independent replenishment demand."""
from datetime import datetime
from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, Text, String, func
from sqlalchemy.orm import Mapped, mapped_column
from app.models import Base


class ReplenishmentProductionJob(Base):
    __tablename__ = 'stock_replenishment_production_jobs'
    __table_args__ = (
        CheckConstraint("status IN ('pending','completed','cancelled')", name='ck_replenishment_production_status'),
        CheckConstraint('input_quantity > 0 AND expected_output > 0 AND actual_output >= 0 AND version > 0', name='ck_replenishment_production_quantity'),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    stock_replenishment_item_id: Mapped[int] = mapped_column(ForeignKey('stock_replenishment_order_items.id', ondelete='RESTRICT'), index=True)
    reservation_id: Mapped[int] = mapped_column(ForeignKey('inventory_reservations.id', ondelete='RESTRICT'), unique=True)
    product_id: Mapped[int] = mapped_column(ForeignKey('products.id', ondelete='RESTRICT'))
    snapshot_json: Mapped[str] = mapped_column(Text)
    input_quantity: Mapped[int] = mapped_column(Integer)
    expected_output: Mapped[int] = mapped_column(Integer)
    actual_output: Mapped[int] = mapped_column(Integer, default=0)
    output_lot_id: Mapped[int | None] = mapped_column(ForeignKey('inventory_lots.id', ondelete='RESTRICT'))
    status: Mapped[str] = mapped_column(String(20), default='pending')
    version: Mapped[int] = mapped_column(Integer, default=1)
    actor_id: Mapped[int] = mapped_column(ForeignKey('users.id', ondelete='RESTRICT'))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.current_timestamp())
