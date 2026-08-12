from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models import Base

if TYPE_CHECKING:
    from app.models.product import Product
    from app.models.user import User


class MoldTool(Base):
    """Physical die/mold master and its fixed warehouse rack location."""

    __tablename__ = "mold_tools"
    __table_args__ = (
        UniqueConstraint("mold_code", name="uq_mold_tools_code"),
        Index("ix_mold_tools_active_location", "is_active", "rack_location"),
        Index("ix_mold_tools_archive_status", "archive_status", "rack_location"),
        CheckConstraint(
            "archive_status IN ('active', 'archived')",
            name="ck_mold_tools_archive_status",
        ),
        CheckConstraint(
            "((archive_status = 'active' AND archived_at IS NULL "
            "AND archived_by IS NULL AND archive_reason IS NULL "
            "AND pre_archive_location IS NULL) OR "
            "(archive_status = 'archived' AND is_active = false "
            "AND archived_at IS NOT NULL AND archived_by IS NOT NULL "
            "AND archive_reason IS NOT NULL AND pre_archive_location IS NOT NULL))",
            name="ck_mold_tools_archive_state",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    mold_code: Mapped[str] = mapped_column(String(100), nullable=False)
    mold_name: Mapped[str] = mapped_column(String(200), nullable=False)
    rack_location: Mapped[str] = mapped_column(String(250), nullable=False)
    location_version: Mapped[int] = mapped_column(
        Integer,
        default=1,
        server_default="1",
        nullable=False,
    )
    last_location_confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
    )
    last_location_confirmed_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    remarks: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    archive_status: Mapped[str] = mapped_column(
        String(20), default="active", server_default="active", nullable=False
    )
    archived_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    archived_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    archive_reason: Mapped[str | None] = mapped_column(String(50), nullable=True)
    pre_archive_location: Mapped[str | None] = mapped_column(String(250), nullable=True)
    restored_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    restored_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    updated_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime, onupdate=func.current_timestamp(), nullable=True
    )

    products: Mapped[list["Product"]] = relationship(
        back_populates="mold_tool",
        passive_deletes=True,
    )
    location_movements: Mapped[list["MoldLocationMovement"]] = relationship(
        back_populates="mold_tool",
        passive_deletes=True,
        order_by="MoldLocationMovement.id",
    )
    last_location_confirmer: Mapped["User | None"] = relationship(
        foreign_keys=[last_location_confirmed_by],
    )
    archiver: Mapped["User | None"] = relationship(foreign_keys=[archived_by])
    restorer: Mapped["User | None"] = relationship(foreign_keys=[restored_by])


class MoldLocationMovement(Base):
    """Immutable audit ledger for confirmed mold-location changes."""

    __tablename__ = "mold_location_movements"
    __table_args__ = (
        UniqueConstraint(
            "idempotency_key",
            name="uq_mold_location_movements_idempotency_key",
        ),
        CheckConstraint(
            "expected_version >= 1",
            name="ck_mold_location_movements_expected_version",
        ),
        CheckConstraint(
            "resulting_version = expected_version + 1",
            name="ck_mold_location_movements_resulting_version",
        ),
        CheckConstraint(
            "from_location <> to_location",
            name="ck_mold_location_movements_actual_change",
        ),
        Index(
            "ix_mold_location_movements_mold_time",
            "mold_tool_id",
            "moved_at",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    mold_tool_id: Mapped[int] = mapped_column(
        ForeignKey("mold_tools.id", ondelete="RESTRICT"),
        nullable=False,
    )
    mold_code_snapshot: Mapped[str] = mapped_column(String(100), nullable=False)
    from_location: Mapped[str] = mapped_column(String(250), nullable=False)
    to_location: Mapped[str] = mapped_column(String(250), nullable=False)
    actor_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    moved_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.current_timestamp(),
        nullable=False,
    )
    idempotency_key: Mapped[str] = mapped_column(String(120), nullable=False)
    expected_version: Mapped[int] = mapped_column(Integer, nullable=False)
    resulting_version: Mapped[int] = mapped_column(Integer, nullable=False)
    source: Mapped[str] = mapped_column(
        String(30),
        default="manual_input",
        server_default="manual_input",
        nullable=False,
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    mold_tool: Mapped["MoldTool"] = relationship(back_populates="location_movements")
    actor: Mapped["User | None"] = relationship(foreign_keys=[actor_id])
