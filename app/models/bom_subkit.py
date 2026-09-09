"""Sub-kit definitions and immutable conversion lineage; quantities live in InventoryLot."""
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base


class ProductSubkit(Base):
    __tablename__ = "product_subkits"
    __table_args__ = (
        CheckConstraint("parent_product_id <> kit_product_id", name="ck_subkit_distinct"),
        CheckConstraint("kits_per_parent > 0 AND version > 0", name="ck_subkit_positive"),
    )
    parent_product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="RESTRICT"), primary_key=True)
    kit_product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="RESTRICT"), unique=True)
    kits_per_parent: Mapped[int] = mapped_column(Integer, nullable=False)
    recipe_json: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class OrderSubkit(Base):
    __tablename__ = "order_subkits"
    __table_args__ = (CheckConstraint("kits_per_parent > 0 AND definition_version > 0", name="ck_order_subkit_positive"),)
    order_item_id: Mapped[int] = mapped_column(ForeignKey("sales_order_items.id", ondelete="RESTRICT"), primary_key=True)
    kit_product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="RESTRICT"), nullable=False)
    kits_per_parent: Mapped[int] = mapped_column(Integer, nullable=False)
    definition_version: Mapped[int] = mapped_column(Integer, nullable=False)
    recipe_json: Mapped[str] = mapped_column(Text, nullable=False)
    kit_name_snapshot: Mapped[str] = mapped_column(String(250), nullable=False)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.current_timestamp())


class SubkitConversion(Base):
    __tablename__ = "subkit_conversions"
    __table_args__ = (
        CheckConstraint("quantity >= 0 AND total_cost >= 0", name="ck_subkit_conversion_positive"),
        CheckConstraint("status IN ('posted','reversed')", name="ck_subkit_conversion_status"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    order_item_id: Mapped[int] = mapped_column(ForeignKey("order_subkits.order_item_id", ondelete="RESTRICT"), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    output_lot_id: Mapped[int | None] = mapped_column(ForeignKey("inventory_lots.id", ondelete="RESTRICT"))
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    total_cost: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    cost_detail_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="posted")
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.current_timestamp())


class SubkitConversionInput(Base):
    __tablename__ = "subkit_conversion_inputs"
    __table_args__ = (
        UniqueConstraint("conversion_id", "lot_id", name="uq_subkit_conversion_input"),
        CheckConstraint("quantity > 0 AND total_cost >= 0", name="ck_subkit_input_positive"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    conversion_id: Mapped[int] = mapped_column(ForeignKey("subkit_conversions.id", ondelete="RESTRICT"), index=True)
    lot_id: Mapped[int] = mapped_column(ForeignKey("inventory_lots.id", ondelete="RESTRICT"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="RESTRICT"))
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    total_cost: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    consume_movement_id: Mapped[int] = mapped_column(ForeignKey("inventory_movements.id", ondelete="RESTRICT"))
    reservations_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")


class SubkitReceiptOutput(Base):
    __tablename__ = "subkit_receipt_outputs"
    __table_args__ = (CheckConstraint("quantity > 0 AND total_cost >= 0", name="ck_subkit_receipt_positive"),)
    allocation_id: Mapped[int] = mapped_column(ForeignKey("incoming_receipt_purpose_allocations.id", ondelete="RESTRICT"), primary_key=True)
    order_item_id: Mapped[int] = mapped_column(ForeignKey("order_subkits.order_item_id", ondelete="RESTRICT"), index=True)
    lot_id: Mapped[int] = mapped_column(ForeignKey("inventory_lots.id", ondelete="RESTRICT"), unique=True)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    total_cost: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    reversed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class SubkitDeliveryAllocation(Base):
    __tablename__ = "subkit_delivery_allocations"
    __table_args__ = (
        UniqueConstraint("delivery_item_id", "operation_key", "lot_id", name="uq_subkit_delivery_lot_operation"),
        CheckConstraint("quantity > 0 AND total_cost >= 0", name="ck_subkit_delivery_positive"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    delivery_item_id: Mapped[int] = mapped_column(ForeignKey("sales_delivery_items.id", ondelete="RESTRICT"), index=True)
    lot_id: Mapped[int] = mapped_column(ForeignKey("inventory_lots.id", ondelete="RESTRICT"))
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    total_cost: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    operation_key: Mapped[str] = mapped_column(String(120), nullable=False)
    cost_detail_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    consume_movement_id: Mapped[int] = mapped_column(ForeignKey("inventory_movements.id", ondelete="RESTRICT"))
    reversed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
