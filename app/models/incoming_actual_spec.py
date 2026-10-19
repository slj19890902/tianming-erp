"""Append-only actual sheet contract owned by a single receiving transaction."""
from datetime import datetime
from sqlalchemy import ForeignKey, Text, DateTime, func
from sqlalchemy.orm import Mapped, mapped_column
from app.models import Base


class IncomingActualSpec(Base):
    __tablename__ = 'incoming_receipt_actual_specs'
    receipt_item_id: Mapped[int] = mapped_column(ForeignKey('incoming_receipt_items.id', ondelete='RESTRICT'), primary_key=True)
    material_id: Mapped[int] = mapped_column(ForeignKey('materials.id', ondelete='RESTRICT'), nullable=False)
    actor_id: Mapped[int] = mapped_column(ForeignKey('users.id', ondelete='RESTRICT'), nullable=False)
    request_json: Mapped[str] = mapped_column(Text, nullable=False)
    actual_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.current_timestamp(), nullable=False)
