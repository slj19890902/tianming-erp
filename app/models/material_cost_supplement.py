"""Approved retrospective references, never purchase or payable facts."""
from datetime import datetime
from decimal import Decimal
from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column
from app.models import Base


class FinanceMaterialCostSupplement(Base):
    __tablename__ = "finance_material_cost_supplements"
    __table_args__ = (
        UniqueConstraint("target_fingerprint", name="uq_material_supplement_target"),
        CheckConstraint("quantity_limit > 0 AND unit_cost > 0", name="ck_material_supplement_positive"),
        CheckConstraint("currency = 'CNY'", name="ck_material_supplement_currency"),
        CheckConstraint("length(target_fingerprint) = 64 AND length(evidence_fingerprint) = 64", name="ck_material_supplement_fingerprint"),
        CheckConstraint("length(reason) > 0", name="ck_material_supplement_reason"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    delivery_item_id: Mapped[int] = mapped_column(ForeignKey("sales_delivery_items.id", ondelete="RESTRICT"), index=True)
    inventory_lot_id: Mapped[int | None] = mapped_column(ForeignKey("inventory_lots.id", ondelete="RESTRICT"), nullable=True)
    month: Mapped[str] = mapped_column(String(7), index=True)
    source_kind: Mapped[str] = mapped_column(String(40))
    source_id: Mapped[int] = mapped_column(Integer)
    target_fingerprint: Mapped[str] = mapped_column(String(64))
    target_json: Mapped[str] = mapped_column(Text)
    quantity_limit: Mapped[int] = mapped_column(Integer)
    unit_cost: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    currency: Mapped[str] = mapped_column(String(3), default="CNY")
    reference_kind: Mapped[str] = mapped_column(String(50))
    evidence_json: Mapped[str] = mapped_column(Text)
    evidence_fingerprint: Mapped[str] = mapped_column(String(64))
    algorithm_version: Mapped[str] = mapped_column(String(40))
    batch_id: Mapped[str] = mapped_column(String(100), index=True)
    reason: Mapped[str] = mapped_column(Text)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.current_timestamp())
