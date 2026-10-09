"""Explicit interchangeability facts; quantities remain on InventoryLot only."""
from datetime import datetime
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column
from app.models import Base


class SharedFinishedGroup(Base):
    __tablename__ = "shared_finished_groups"
    id: Mapped[int] = mapped_column(primary_key=True)
    operation_key: Mapped[str] = mapped_column(String(100), unique=True)
    request_json: Mapped[str] = mapped_column(Text)
    evidence: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.current_timestamp())


class SharedFinishedMember(Base):
    __tablename__ = "shared_finished_members"
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="RESTRICT"), primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("shared_finished_groups.id", ondelete="RESTRICT"), index=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id", ondelete="RESTRICT"), index=True)
    identity_json: Mapped[str] = mapped_column(Text)
    product_basis_json: Mapped[str] = mapped_column(Text)


class SharedFinishedLot(Base):
    __tablename__ = "shared_finished_lots"
    lot_id: Mapped[int] = mapped_column(ForeignKey("inventory_lots.id", ondelete="RESTRICT"), primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("shared_finished_groups.id", ondelete="RESTRICT"), index=True)
    identity_json: Mapped[str] = mapped_column(Text)
    source_lot_id: Mapped[int | None] = mapped_column(ForeignKey("inventory_lots.id", ondelete="RESTRICT"))


class SharedFinishedReservation(Base):
    __tablename__ = "shared_finished_reservations"
    reservation_id: Mapped[int] = mapped_column(ForeignKey("inventory_reservations.id", ondelete="RESTRICT"), primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("shared_finished_groups.id", ondelete="RESTRICT"))
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="RESTRICT"))
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id", ondelete="RESTRICT"))
    lot_identity_json: Mapped[str] = mapped_column(Text)
    product_basis_json: Mapped[str] = mapped_column(Text)


class SharedFinishedPolicy(Base):
    __tablename__ = "shared_finished_policies"
    group_id: Mapped[int] = mapped_column(ForeignKey("shared_finished_groups.id", ondelete="RESTRICT"), primary_key=True)
    auto_enroll: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class SharedFinishedMutation(Base):
    __tablename__ = "shared_finished_mutations"
    id: Mapped[int] = mapped_column(primary_key=True)
    operation_key: Mapped[str] = mapped_column(String(100), unique=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("shared_finished_groups.id", ondelete="RESTRICT"), index=True)
    request_json: Mapped[str] = mapped_column(Text)
    result_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.current_timestamp())


class SharedFinishedOrderBasis(Base):
    __tablename__ = "shared_finished_order_bases"
    order_item_id: Mapped[int] = mapped_column(ForeignKey("sales_order_items.id", ondelete="CASCADE"), primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("shared_finished_groups.id", ondelete="RESTRICT"))
    group_version: Mapped[int] = mapped_column(Integer)
    member_identity_json: Mapped[str] = mapped_column(Text)
    product_basis_json: Mapped[str] = mapped_column(Text)
    order_identity_json: Mapped[str] = mapped_column(Text)


# Covers ordinary orders, PDF/email and other real creation paths equally.
from sqlalchemy import event
from app.models.order import OrderItem

def _capture_shared_order(mapper, connection, item):
    from app.services.shared_finished_receipts import capture_order
    capture_order(connection, item)

event.listen(OrderItem, "after_insert", _capture_shared_order)
