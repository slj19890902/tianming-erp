from datetime import datetime
from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column
from app.models import Base

class BusinessApproval(Base):
    __tablename__ = "business_approvals"
    __table_args__ = (
        UniqueConstraint("applicant_id", "idempotency_key", name="uq_business_approval_request"),
        CheckConstraint("status IN ('pending','applied','rejected','withdrawn')", name="ck_business_approval_status"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    applicant_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    action: Mapped[str] = mapped_column(String(50))
    target_id: Mapped[int | None] = mapped_column(Integer)
    payload_json: Mapped[str] = mapped_column(Text)
    before_json: Mapped[str] = mapped_column(Text, default="{}")
    basis_hash: Mapped[str] = mapped_column(String(64))
    request_hash: Mapped[str] = mapped_column(String(64))
    idempotency_key: Mapped[str] = mapped_column(String(160))
    note: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    reviewer_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    decision_note: Mapped[str | None] = mapped_column(Text)
    result_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.current_timestamp())
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime)
