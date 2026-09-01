from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models import Base

if TYPE_CHECKING:
    from app.models.customer import Customer
    from app.models.warehouse_inventory import WarehouseArea


class CustomerFinishedStoragePreference(Base):
    """Ordered formal warehouse areas preferred for one customer's finished goods."""

    __tablename__ = "customer_finished_storage_area_preferences"
    __table_args__ = (
        CheckConstraint(
            "priority > 0",
            name="ck_customer_finished_storage_preferences_priority",
        ),
        UniqueConstraint(
            "customer_id",
            "warehouse_area_id",
            name="uq_customer_finished_storage_preferences_area",
        ),
        UniqueConstraint(
            "customer_id",
            "priority",
            name="uq_customer_finished_storage_preferences_priority",
        ),
        Index(
            "ix_customer_finished_storage_preferences_area",
            "warehouse_area_id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"),
        nullable=False,
    )
    warehouse_area_id: Mapped[int] = mapped_column(
        ForeignKey("warehouse_areas.id", ondelete="RESTRICT"),
        nullable=False,
    )
    priority: Mapped[int] = mapped_column(Integer, nullable=False)
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.current_timestamp(),
        nullable=False,
    )

    customer: Mapped["Customer"] = relationship(
        back_populates="finished_storage_preferences"
    )
    warehouse_area: Mapped["WarehouseArea"] = relationship()
