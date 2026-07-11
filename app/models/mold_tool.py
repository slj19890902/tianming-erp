from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models import Base

if TYPE_CHECKING:
    from app.models.product import Product


class MoldTool(Base):
    """Physical die/mold master and its fixed warehouse rack location."""

    __tablename__ = "mold_tools"
    __table_args__ = (
        UniqueConstraint("mold_code", name="uq_mold_tools_code"),
        Index("ix_mold_tools_active_location", "is_active", "rack_location"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    mold_code: Mapped[str] = mapped_column(String(100), nullable=False)
    mold_name: Mapped[str] = mapped_column(String(200), nullable=False)
    rack_location: Mapped[str] = mapped_column(String(250), nullable=False)
    remarks: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
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
