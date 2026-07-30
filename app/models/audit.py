from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models import Base

if TYPE_CHECKING:
    from app.models.user import User


class OperationLog(Base):
    __tablename__ = "operation_logs"
    __table_args__ = (
        Index(
            "ix_operation_logs_category_created_id",
            "event_category",
            "created_at",
            "id",
        ),
        Index(
            "ix_operation_logs_operator_created_id",
            "actor_user_id_snapshot",
            "created_at",
            "id",
        ),
        Index(
            "ix_operation_logs_module_action_created_id",
            "module_code",
            "action_code",
            "created_at",
            "id",
        ),
        Index(
            "ix_operation_logs_object_ref_created_id",
            "object_ref",
            "created_at",
            "id",
        ),
        Index(
            "ix_operation_logs_customer_created_id",
            "customer_id_snapshot",
            "created_at",
            "id",
        ),
        Index(
            "ix_operation_logs_result_source_created_id",
            "result",
            "source",
            "created_at",
            "id",
        ),
        Index("ix_operation_logs_request_id", "request_id"),
        Index("ix_operation_logs_batch_id", "batch_id"),
        Index("ix_operation_logs_schema_version", "schema_version"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    action: Mapped[str] = mapped_column(String(30), index=True)
    resource: Mapped[str] = mapped_column(String(100), index=True)
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.current_timestamp(),
        nullable=False,
        index=True,
    )

    # Retained while legacy sqlite3 APIs are being strangled out.
    username: Mapped[str | None] = mapped_column(String(50), nullable=True)
    role: Mapped[str | None] = mapped_column(String(30), nullable=True)
    entity_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    entity_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    extra_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Q1-02 structured audit envelope.  Every column remains nullable.  The
    # migration marks old rows only as schema 0 / legacy source and result;
    # it does not invent a category, module, action, operator or customer.
    event_category: Mapped[str | None] = mapped_column(
        String(30),
        nullable=True,
    )
    result: Mapped[str | None] = mapped_column(String(20), nullable=True)
    source: Mapped[str | None] = mapped_column(String(30), nullable=True)
    module_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    action_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    actor_user_id_snapshot: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )
    operator_name_snapshot: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
    )
    object_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    customer_id_snapshot: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )
    customer_name_snapshot: Mapped[str | None] = mapped_column(
        String(200),
        nullable=True,
    )
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    batch_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    schema_version: Mapped[int | None] = mapped_column(Integer, nullable=True)

    user: Mapped["User | None"] = relationship(back_populates="operation_logs")
