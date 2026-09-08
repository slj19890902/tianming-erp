"""Storage intent and packaging, never a second inventory quantity ledger."""
from sqlalchemy import CheckConstraint, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from app.models import Base


class ShelfProfile(Base):
    __tablename__ = "warehouse_shelf_profiles"
    __table_args__ = (CheckConstraint("units_per_bundle IS NULL OR units_per_bundle > 0", name="ck_shelf_profile_bundle"), CheckConstraint("version > 0", name="ck_shelf_profile_version"))
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="RESTRICT"), primary_key=True)
    units_per_bundle: Mapped[int | None] = mapped_column(Integer)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class ShelfBinding(Base):
    __tablename__ = "warehouse_shelf_bindings"
    __table_args__ = (UniqueConstraint("product_id", "priority", name="uq_shelf_product_priority"), CheckConstraint("priority >= 0", name="ck_shelf_binding_priority"), CheckConstraint("capacity IS NULL OR capacity > 0", name="ck_shelf_binding_capacity"))
    location_id: Mapped[int] = mapped_column(ForeignKey("warehouse_locations.id", ondelete="RESTRICT"), primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("warehouse_shelf_profiles.product_id", ondelete="RESTRICT"), nullable=False, index=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False)
    capacity: Mapped[int | None] = mapped_column(Integer)


class ShelfLotState(Base):
    __tablename__ = "warehouse_shelf_lot_states"
    __table_args__ = (CheckConstraint("units_per_bundle IS NULL OR units_per_bundle > 0", name="ck_shelf_lot_bundle"),)
    lot_id: Mapped[int] = mapped_column(ForeignKey("inventory_lots.id", ondelete="RESTRICT"), primary_key=True)
    units_per_bundle: Mapped[int | None] = mapped_column(Integer)
    target_location_id: Mapped[int | None] = mapped_column(ForeignKey("warehouse_locations.id", ondelete="RESTRICT"), index=True)


class ShelfMutation(Base):
    __tablename__ = "warehouse_shelf_mutations"
    idempotency_key: Mapped[str] = mapped_column(String(100), primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    result_json: Mapped[str] = mapped_column(Text, nullable=False)
