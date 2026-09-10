"""Advisory material uses, independent of allocation and allowed-product bindings."""
from datetime import datetime
from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column
from app.models import Base


class MaterialCandidateSelection(Base):
    __tablename__ = "material_candidate_selections"
    __table_args__ = (UniqueConstraint("lot_id", "lot_version", name="uq_material_candidate_version"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    lot_id: Mapped[int] = mapped_column(ForeignKey("inventory_lots.id", ondelete="RESTRICT"), index=True)
    lot_version: Mapped[int] = mapped_column(Integer)
    idempotency_key: Mapped[str] = mapped_column(String(100), unique=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    candidates_json: Mapped[str] = mapped_column(Text)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.current_timestamp())
