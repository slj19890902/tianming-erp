"""Homepage attention state, separate from the underlying business facts."""
from datetime import datetime, date
from sqlalchemy import CheckConstraint, Date, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from app.models import Base


class HomeTaskPreference(Base):
    __tablename__ = "home_task_preferences"
    __table_args__ = (
        UniqueConstraint("scope_key", "task_key", name="uq_home_task_scope"),
        CheckConstraint("version > 0", name="ck_home_task_version"),
        CheckConstraint("state IN ('active','snoozed','waiting_customer','stock_review','cancel_review','condition_wait','verify','hidden')", name="ck_home_task_state"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    scope_key: Mapped[str] = mapped_column(String(40))
    task_key: Mapped[str] = mapped_column(String(180))
    customer_ids_json: Mapped[str] = mapped_column(Text)
    source_hash: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(30))
    reason: Mapped[str] = mapped_column(String(40))
    remind_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    actor_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    version: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(DateTime)


class HomeTaskMutation(Base):
    __tablename__ = "home_task_mutations"
    __table_args__ = (UniqueConstraint("actor_id", "idempotency_key", name="uq_home_mutation_key"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    actor_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    preference_id: Mapped[int] = mapped_column(ForeignKey("home_task_preferences.id", ondelete="RESTRICT"))
    idempotency_key: Mapped[str] = mapped_column(String(120))
    request_hash: Mapped[str] = mapped_column(String(64))
    customer_ids_json: Mapped[str] = mapped_column(Text)
    response_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime)
