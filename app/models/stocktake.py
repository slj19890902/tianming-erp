from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models import Base

if TYPE_CHECKING:
    from app.models.user import User
    from app.models.warehouse_inventory import (
        InventoryLot,
        InventoryMovement,
        WarehouseLocation,
    )


class StocktakeOrder(Base):
    """A draft mobile stocktake that becomes immutable when submitted."""

    __tablename__ = "stocktake_orders"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft','submitted','approved','rejected')",
            name="ck_stocktake_orders_status",
        ),
        CheckConstraint("version >= 1", name="ck_stocktake_orders_version"),
        CheckConstraint(
            "(status IN ('draft', 'submitted') AND reviewed_by IS NULL "
            "AND reviewed_at IS NULL AND review_note IS NULL) OR "
            "(status IN ('approved', 'rejected') AND reviewed_by IS NOT NULL "
            "AND reviewed_at IS NOT NULL)",
            name="ck_stocktake_orders_review_state",
        ),
        UniqueConstraint("order_number", name="uq_stocktake_orders_number"),
        UniqueConstraint("idempotency_key", name="uq_stocktake_orders_idempotency"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    order_number: Mapped[str] = mapped_column(String(50), nullable=False)
    location_id: Mapped[int] = mapped_column(
        ForeignKey("warehouse_locations.id", ondelete="RESTRICT"), nullable=False
    )
    # Snapshot of the map point the counter selected.  Historical and
    # deliberately unmapped locations remain NULL; a mapped empty location
    # must carry a positive version before approval may create live stock.
    location_layout_version: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    # Stable physical-address snapshots.  They prevent a phone submission or
    # later approval from applying after a rack is rebound or a published map
    # has changed while the operator still has an older page open.
    location_address_version: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    location_position_status: Mapped[str | None] = mapped_column(
        String(30), nullable=True
    )
    published_map_revision: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    status: Mapped[str] = mapped_column(
        String(20), default="draft", server_default="draft", nullable=False
    )
    version: Mapped[int] = mapped_column(
        Integer, default=1, server_default="1", nullable=False
    )
    submitted_by: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )
    reviewed_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    review_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(120), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
        nullable=False,
    )

    location: Mapped["WarehouseLocation"] = relationship()
    submitter: Mapped["User"] = relationship(foreign_keys=[submitted_by])
    reviewer: Mapped["User | None"] = relationship(foreign_keys=[reviewed_by])
    items: Mapped[list["StocktakeItem"]] = relationship(
        back_populates="order",
        cascade="all, delete-orphan",
        order_by="StocktakeItem.id",
    )
    reviews: Mapped[list["StocktakeReview"]] = relationship(
        back_populates="order",
        cascade="all, delete-orphan",
        order_by="StocktakeReview.sequence",
    )


class StocktakeItem(Base):
    """Immutable-at-submission inventory and master-data snapshot for one lot."""

    __tablename__ = "stocktake_items"
    __table_args__ = (
        CheckConstraint(
            "lot_version_snapshot >= 1", name="ck_stocktake_items_lot_version"
        ),
        CheckConstraint(
            "available_quantity_snapshot >= 0 AND reserved_quantity_snapshot >= 0 "
            "AND on_hand_quantity_snapshot >= 0 AND counted_quantity >= 0",
            name="ck_stocktake_items_quantities_nonnegative",
        ),
        CheckConstraint(
            "on_hand_quantity_snapshot = available_quantity_snapshot + reserved_quantity_snapshot",
            name="ck_stocktake_items_on_hand_snapshot",
        ),
        CheckConstraint(
            "difference_quantity = counted_quantity - on_hand_quantity_snapshot",
            name="ck_stocktake_items_difference",
        ),
        UniqueConstraint("order_id", "inventory_lot_id", name="uq_stocktake_items_order_lot"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(
        ForeignKey("stocktake_orders.id", ondelete="CASCADE"), nullable=False
    )
    inventory_lot_id: Mapped[int] = mapped_column(
        ForeignKey("inventory_lots.id", ondelete="RESTRICT"), nullable=False
    )
    lot_version_snapshot: Mapped[int] = mapped_column(Integer, nullable=False)
    available_quantity_snapshot: Mapped[int] = mapped_column(Integer, nullable=False)
    reserved_quantity_snapshot: Mapped[int] = mapped_column(Integer, nullable=False)
    on_hand_quantity_snapshot: Mapped[int] = mapped_column(Integer, nullable=False)
    counted_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    # Signed discrepancy: negative is a shortage and positive is an overage.
    difference_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    lot_number_snapshot: Mapped[str] = mapped_column(String(50), nullable=False)
    customer_name_snapshot: Mapped[str | None] = mapped_column(String(200), nullable=True)
    product_name_snapshot: Mapped[str | None] = mapped_column(String(250), nullable=True)
    specification_snapshot: Mapped[str | None] = mapped_column(String(150), nullable=True)
    inventory_code_snapshot: Mapped[str | None] = mapped_column(String(150), nullable=True)
    location_code_snapshot: Mapped[str] = mapped_column(String(50), nullable=False)
    unit_snapshot: Mapped[str] = mapped_column(String(20), nullable=False)
    adjustment_movement_id: Mapped[int | None] = mapped_column(
        ForeignKey("inventory_movements.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )

    order: Mapped["StocktakeOrder"] = relationship(back_populates="items")
    inventory_lot: Mapped["InventoryLot"] = relationship()
    adjustment_movement: Mapped["InventoryMovement | None"] = relationship()


class StocktakeReview(Base):
    """Append-only review ledger; database triggers reject updates and deletes."""

    __tablename__ = "stocktake_reviews"
    __table_args__ = (
        CheckConstraint("sequence >= 1", name="ck_stocktake_reviews_sequence"),
        CheckConstraint(
            "action IN ('approve','reject')", name="ck_stocktake_reviews_action"
        ),
        CheckConstraint(
            "(action = 'approve' AND from_status = 'submitted' AND to_status = 'approved') OR "
            "(action = 'reject' AND from_status = 'submitted' AND to_status = 'rejected')",
            name="ck_stocktake_reviews_transition",
        ),
        UniqueConstraint("order_id", "sequence", name="uq_stocktake_reviews_order_sequence"),
        UniqueConstraint("idempotency_key", name="uq_stocktake_reviews_idempotency"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(
        ForeignKey("stocktake_orders.id", ondelete="CASCADE"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    action: Mapped[str] = mapped_column(String(20), nullable=False)
    from_status: Mapped[str] = mapped_column(String(20), nullable=False)
    to_status: Mapped[str] = mapped_column(String(20), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(120), nullable=False)
    details_json: Mapped[dict[str, object] | None] = mapped_column(JSON, nullable=True)
    reviewed_by: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    reviewed_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )

    order: Mapped["StocktakeOrder"] = relationship(back_populates="reviews")
    reviewer: Mapped["User"] = relationship(foreign_keys=[reviewed_by])
