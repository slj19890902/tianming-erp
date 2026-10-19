"""Frozen print jobs for actual finished inventory; independent of purchase tasks."""
from datetime import datetime
from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column
from app.models import Base


class FinishedProductLabelPrintJob(Base):
    __tablename__ = 'finished_product_label_print_jobs'
    __table_args__ = (
        CheckConstraint("status IN ('prepared','printed')", name='ck_finished_label_status'),
        CheckConstraint("length(request_hash)=64 AND length(plan_fingerprint)=64 AND length(payload_hash)=64", name='ck_finished_label_hashes'),
        CheckConstraint("(status='prepared' AND printed_at IS NULL AND printed_confirmation_key IS NULL AND printed_by_user_id IS NULL) OR (status='printed' AND printed_at IS NOT NULL AND printed_confirmation_key IS NOT NULL AND printed_by_user_id IS NOT NULL)", name='ck_finished_label_lifecycle'),
        Index('ix_finished_label_created', 'created_at'),
    )
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    inventory_lot_id: Mapped[int] = mapped_column(ForeignKey('inventory_lots.id', ondelete='RESTRICT'), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    plan_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    operator_id: Mapped[int] = mapped_column(ForeignKey('users.id', ondelete='RESTRICT'), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default='prepared', server_default='prepared', nullable=False)
    printed_confirmation_key: Mapped[str | None] = mapped_column(String(120), unique=True, nullable=True)
    printed_by_user_id: Mapped[int | None] = mapped_column(ForeignKey('users.id', ondelete='RESTRICT'), nullable=True)
    printed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.current_timestamp(), nullable=False)
