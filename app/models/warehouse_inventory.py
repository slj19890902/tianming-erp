from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
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
    from app.models.customer import Customer
    from app.models.product import Product


class WarehouseLocation(Base):
    __tablename__ = "warehouse_locations"
    __table_args__ = (
        CheckConstraint(
            "warehouse_type IN ('finished','semi_finished','shared')",
            name="ck_warehouse_locations_type",
        ),
        UniqueConstraint("location_code", name="uq_warehouse_locations_code"),
        Index("ix_warehouse_locations_type_active", "warehouse_type", "is_active"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    location_code: Mapped[str] = mapped_column(String(50), nullable=False)
    location_name: Mapped[str] = mapped_column(String(100), nullable=False)
    warehouse_type: Mapped[str] = mapped_column(String(30), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    remarks: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime, onupdate=func.current_timestamp(), nullable=True
    )


class InventoryLot(Base):
    __tablename__ = "inventory_lots"
    __table_args__ = (
        CheckConstraint(
            "inventory_type IN ('finished','semi_finished')",
            name="ck_inventory_lots_type",
        ),
        CheckConstraint("unit IN ('boxes','sheets')", name="ck_inventory_lots_unit"),
        CheckConstraint(
            "status IN ('active','frozen','closed')",
            name="ck_inventory_lots_status",
        ),
        CheckConstraint(
            "source_type IN ('manual','production_surplus','purchase_surplus','stocktake','transfer')",
            name="ck_inventory_lots_source_type",
        ),
        CheckConstraint("quantity_available >= 0", name="ck_inventory_lots_available"),
        CheckConstraint("quantity_reserved >= 0", name="ck_inventory_lots_reserved"),
        CheckConstraint("quantity_consumed >= 0", name="ck_inventory_lots_consumed"),
        CheckConstraint("quantity_damaged >= 0", name="ck_inventory_lots_damaged"),
        CheckConstraint("quantity_scrapped >= 0", name="ck_inventory_lots_scrapped"),
        UniqueConstraint("lot_number", name="uq_inventory_lots_number"),
        Index("ix_inventory_lots_type_status", "inventory_type", "status"),
        Index("ix_inventory_lots_location_status", "warehouse_location_id", "status"),
        Index("ix_inventory_lots_stock_date", "stock_date"),
        Index("ix_inventory_lots_last_movement", "last_movement_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    lot_number: Mapped[str] = mapped_column(String(50), nullable=False)
    inventory_type: Mapped[str] = mapped_column(String(30), nullable=False)
    warehouse_location_id: Mapped[int] = mapped_column(
        ForeignKey("warehouse_locations.id", ondelete="RESTRICT"), nullable=False
    )
    quantity_available: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    quantity_reserved: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    quantity_consumed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    quantity_damaged: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    quantity_scrapped: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    unit: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)
    source_type: Mapped[str] = mapped_column(String(30), nullable=False)
    source_ref_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    source_ref_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stock_date: Mapped[date] = mapped_column(Date, nullable=False)
    last_movement_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    remarks: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime, onupdate=func.current_timestamp(), nullable=True
    )

    location: Mapped["WarehouseLocation"] = relationship()
    finished_detail: Mapped["FinishedGoodsInventoryDetail | None"] = relationship(
        back_populates="lot", cascade="all, delete-orphan", uselist=False
    )
    semi_finished_detail: Mapped["SemiFinishedInventoryDetail | None"] = relationship(
        back_populates="lot", cascade="all, delete-orphan", uselist=False
    )
    movements: Mapped[list["InventoryMovement"]] = relationship(
        back_populates="lot", order_by="InventoryMovement.id"
    )


class FinishedGoodsInventoryDetail(Base):
    __tablename__ = "finished_goods_inventory_details"
    __table_args__ = (
        CheckConstraint(
            "is_general = 1 OR owner_customer_id IS NOT NULL",
            name="ck_finished_inventory_owner",
        ),
        Index("ix_finished_inventory_owner_product", "owner_customer_id", "product_id"),
        Index("ix_finished_inventory_product", "product_id"),
        Index("ix_finished_inventory_general", "is_general"),
        Index("ix_finished_inventory_code", "inventory_code_snapshot"),
    )

    inventory_lot_id: Mapped[int] = mapped_column(
        ForeignKey("inventory_lots.id", ondelete="CASCADE"), primary_key=True
    )
    owner_customer_id: Mapped[int | None] = mapped_column(
        ForeignKey("customers.id", ondelete="SET NULL"), nullable=True
    )
    owner_customer_name_snapshot: Mapped[str | None] = mapped_column(
        String(200), nullable=True
    )
    is_general: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"), nullable=False
    )
    inventory_code_snapshot: Mapped[str] = mapped_column(String(150), nullable=False)
    product_name_snapshot: Mapped[str] = mapped_column(String(250), nullable=False)
    box_type_snapshot: Mapped[str | None] = mapped_column(String(150), nullable=True)
    length_mm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    width_mm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height_mm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    material_code_snapshot: Mapped[str | None] = mapped_column(String(100), nullable=True)
    flute_type_snapshot: Mapped[str | None] = mapped_column(String(20), nullable=True)

    lot: Mapped["InventoryLot"] = relationship(back_populates="finished_detail")
    customer: Mapped["Customer | None"] = relationship()
    product: Mapped["Product"] = relationship()


class SemiFinishedInventoryDetail(Base):
    __tablename__ = "semi_finished_inventory_details"
    __table_args__ = (
        CheckConstraint("layer_count IN (3,5)", name="ck_semi_inventory_layer"),
        CheckConstraint(
            "(layer_count=3 AND flute_type IN ('A','B','E')) OR "
            "(layer_count=5 AND flute_type IN ('AB','BE'))",
            name="ck_semi_inventory_flute",
        ),
        CheckConstraint("board_length_mm > 0", name="ck_semi_inventory_length"),
        CheckConstraint("board_width_mm > 0", name="ck_semi_inventory_width"),
        CheckConstraint(
            "sheet_type IN ('raw_board','net_sheet','creased_sheet')",
            name="ck_semi_inventory_sheet_type",
        ),
        Index(
            "ix_semi_inventory_flute_size",
            "flute_type",
            "board_length_mm",
            "board_width_mm",
        ),
        Index("ix_semi_inventory_sheet_flute", "sheet_type", "flute_type"),
        Index("ix_semi_inventory_owner", "owner_customer_id"),
        Index("ix_semi_inventory_material", "material_code_snapshot"),
    )

    inventory_lot_id: Mapped[int] = mapped_column(
        ForeignKey("inventory_lots.id", ondelete="CASCADE"), primary_key=True
    )
    supplier_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    owner_customer_id: Mapped[int | None] = mapped_column(
        ForeignKey("customers.id", ondelete="SET NULL"), nullable=True
    )
    owner_customer_name_snapshot: Mapped[str | None] = mapped_column(
        String(200), nullable=True
    )
    material_code_snapshot: Mapped[str] = mapped_column(String(100), nullable=False)
    layer_count: Mapped[int] = mapped_column(Integer, nullable=False)
    flute_type: Mapped[str] = mapped_column(String(20), nullable=False)
    board_length_mm: Mapped[int] = mapped_column(Integer, nullable=False)
    board_width_mm: Mapped[int] = mapped_column(Integer, nullable=False)
    sheet_type: Mapped[str] = mapped_column(String(30), nullable=False)
    crease_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    crease_left_mm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    crease_middle_mm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    crease_right_mm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cutting_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    lot: Mapped["InventoryLot"] = relationship(back_populates="semi_finished_detail")
    customer: Mapped["Customer | None"] = relationship()


class InventoryReservation(Base):
    __tablename__ = "inventory_reservations"
    __table_args__ = (
        CheckConstraint(
            "reservation_type IN ('finished_order','semi_requisition')",
            name="ck_inventory_reservations_type",
        ),
        CheckConstraint(
            "status IN ('active','released','consumed','cancelled')",
            name="ck_inventory_reservations_status",
        ),
        CheckConstraint(
            "reserved_stock_quantity > 0",
            name="ck_inventory_reservations_quantity",
        ),
        UniqueConstraint("reservation_number", name="uq_inventory_reservations_number"),
        UniqueConstraint("idempotency_key", name="uq_inventory_reservations_idempotency"),
        Index("ix_inventory_reservations_lot_status", "inventory_lot_id", "status"),
        Index(
            "ix_inventory_reservations_order_item",
            "order_item_id",
            "reservation_type",
            "status",
        ),
        Index("ix_inventory_reservations_requisition", "requisition_item_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    reservation_number: Mapped[str] = mapped_column(String(50), nullable=False)
    inventory_lot_id: Mapped[int] = mapped_column(
        ForeignKey("inventory_lots.id", ondelete="RESTRICT"), nullable=False
    )
    reservation_type: Mapped[str] = mapped_column(String(30), nullable=False)
    order_id: Mapped[int | None] = mapped_column(
        ForeignKey("sales_orders.id", ondelete="SET NULL"), nullable=True
    )
    order_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("sales_order_items.id", ondelete="SET NULL"), nullable=True
    )
    requisition_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("material_requisition_items.id", ondelete="SET NULL"), nullable=True
    )
    reserved_stock_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    credited_requirement_quantity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    yield_factor: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)
    warning_codes: Mapped[str | None] = mapped_column(Text, nullable=True)
    warning_acknowledged_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    reserved_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    reserved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    released_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    released_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    consumed_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    release_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime, onupdate=func.current_timestamp(), nullable=True
    )


class InventoryMovement(Base):
    __tablename__ = "inventory_movements"
    __table_args__ = (
        CheckConstraint(
            "movement_type IN ('manual_in','adjust','freeze','unfreeze','damage','scrap',"
            "'transfer_to_general','reserve','release_reserve','consume')",
            name="ck_inventory_movements_type",
        ),
        CheckConstraint("quantity >= 0", name="ck_inventory_movements_quantity"),
        UniqueConstraint("movement_number", name="uq_inventory_movements_number"),
        UniqueConstraint("idempotency_key", name="uq_inventory_movements_idempotency"),
        Index("ix_inventory_movements_lot_created", "inventory_lot_id", "created_at"),
        Index("ix_inventory_movements_reservation", "reservation_id"),
        Index("ix_inventory_movements_order_item", "related_order_item_id"),
        Index("ix_inventory_movements_supplier_order", "related_supplier_order_id"),
        Index("ix_inventory_movements_delivery", "related_delivery_id"),
        Index("ix_inventory_movements_type_created", "movement_type", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    movement_number: Mapped[str] = mapped_column(String(50), nullable=False)
    inventory_lot_id: Mapped[int] = mapped_column(
        ForeignKey("inventory_lots.id", ondelete="RESTRICT"), nullable=False
    )
    movement_type: Mapped[str] = mapped_column(String(30), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit: Mapped[str] = mapped_column(String(20), nullable=False)
    before_available: Mapped[int] = mapped_column(Integer, nullable=False)
    after_available: Mapped[int] = mapped_column(Integer, nullable=False)
    before_reserved: Mapped[int] = mapped_column(Integer, nullable=False)
    after_reserved: Mapped[int] = mapped_column(Integer, nullable=False)
    before_consumed: Mapped[int] = mapped_column(Integer, nullable=False)
    after_consumed: Mapped[int] = mapped_column(Integer, nullable=False)
    before_damaged: Mapped[int] = mapped_column(Integer, nullable=False)
    after_damaged: Mapped[int] = mapped_column(Integer, nullable=False)
    before_scrapped: Mapped[int] = mapped_column(Integer, nullable=False)
    after_scrapped: Mapped[int] = mapped_column(Integer, nullable=False)
    reservation_id: Mapped[int | None] = mapped_column(
        ForeignKey("inventory_reservations.id", ondelete="SET NULL"), nullable=True
    )
    related_order_id: Mapped[int | None] = mapped_column(
        ForeignKey("sales_orders.id", ondelete="SET NULL"), nullable=True
    )
    related_order_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("sales_order_items.id", ondelete="SET NULL"), nullable=True
    )
    related_requisition_id: Mapped[int | None] = mapped_column(
        ForeignKey("material_requisitions.id", ondelete="SET NULL"), nullable=True
    )
    related_supplier_order_id: Mapped[int | None] = mapped_column(
        ForeignKey("supplier_requisition_orders.id", ondelete="SET NULL"), nullable=True
    )
    related_delivery_id: Mapped[int | None] = mapped_column(
        ForeignKey("sales_deliveries.id", ondelete="SET NULL"), nullable=True
    )
    reversal_of_movement_id: Mapped[int | None] = mapped_column(
        ForeignKey("inventory_movements.id", ondelete="SET NULL"), nullable=True
    )
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    remarks: Mapped[str | None] = mapped_column(Text, nullable=True)
    operator_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    idempotency_key: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )

    lot: Mapped["InventoryLot"] = relationship(back_populates="movements")
