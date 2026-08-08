from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models import Base

if TYPE_CHECKING:
    from app.models.customer import Customer
    from app.models.user import User


class PrintingPlate(Base):
    """One physical printing plate for one printing colour."""

    __tablename__ = "printing_plates"
    __table_args__ = (
        UniqueConstraint("plate_code", name="uq_printing_plates_code"),
        CheckConstraint(
            "status IN ('active','inactive','damaged')",
            name="ck_printing_plates_status",
        ),
        CheckConstraint("version >= 1", name="ck_printing_plates_version"),
        CheckConstraint(
            "location_version >= 1",
            name="ck_printing_plates_location_version",
        ),
        Index("ix_printing_plates_customer_status", "customer_id", "status"),
        Index(
            "uq_printing_plates_occupied_location",
            "rack_location",
            unique=True,
            sqlite_where=text("status IN ('active','damaged')"),
            postgresql_where=text("status IN ('active','damaged')"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    plate_code: Mapped[str] = mapped_column(String(30), nullable=False)
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False
    )
    plate_name: Mapped[str] = mapped_column(String(200), nullable=False)
    color_name: Mapped[str] = mapped_column(String(100), nullable=False)
    rack_location: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), default="active", server_default="active", nullable=False
    )
    version: Mapped[int] = mapped_column(
        Integer, default=1, server_default="1", nullable=False
    )
    location_version: Mapped[int] = mapped_column(
        Integer, default=1, server_default="1", nullable=False
    )
    last_location_confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    last_location_confirmed_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    remarks: Mapped[str | None] = mapped_column(Text, nullable=True)
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

    customer: Mapped["Customer"] = relationship()
    last_location_confirmer: Mapped["User | None"] = relationship(
        foreign_keys=[last_location_confirmed_by]
    )
    location_movements: Mapped[list["PrintingPlateLocationMovement"]] = relationship(
        back_populates="printing_plate",
        passive_deletes=True,
        order_by="PrintingPlateLocationMovement.id",
    )


class PrintingPlateLocationMovement(Base):
    """Immutable, idempotent audit ledger for printing-plate moves."""

    __tablename__ = "printing_plate_location_movements"
    __table_args__ = (
        UniqueConstraint(
            "idempotency_key",
            name="uq_printing_plate_location_movements_idempotency",
        ),
        CheckConstraint(
            "expected_version >= 1",
            name="ck_printing_plate_movements_expected_version",
        ),
        CheckConstraint(
            "resulting_version = expected_version + 1",
            name="ck_printing_plate_movements_resulting_version",
        ),
        CheckConstraint(
            "from_location <> to_location",
            name="ck_printing_plate_movements_actual_change",
        ),
        Index(
            "ix_printing_plate_movements_plate_time",
            "printing_plate_id",
            "moved_at",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    printing_plate_id: Mapped[int] = mapped_column(
        ForeignKey("printing_plates.id", ondelete="RESTRICT"), nullable=False
    )
    plate_code_snapshot: Mapped[str] = mapped_column(String(30), nullable=False)
    from_location: Mapped[str] = mapped_column(String(100), nullable=False)
    to_location: Mapped[str] = mapped_column(String(100), nullable=False)
    actor_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    moved_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(120), nullable=False)
    expected_version: Mapped[int] = mapped_column(Integer, nullable=False)
    resulting_version: Mapped[int] = mapped_column(Integer, nullable=False)
    source: Mapped[str] = mapped_column(
        String(30), default="manual_input", server_default="manual_input", nullable=False
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    printing_plate: Mapped["PrintingPlate"] = relationship(
        back_populates="location_movements"
    )
    actor: Mapped["User | None"] = relationship(foreign_keys=[actor_id])
