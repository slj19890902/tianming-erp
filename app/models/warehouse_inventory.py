from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    false,
    func,
    text,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models import Base

if TYPE_CHECKING:
    from app.models.customer import Customer
    from app.models.material import Material
    from app.models.product import Product


class WarehouseLocation(Base):
    __tablename__ = "warehouse_locations"
    __table_args__ = (
        CheckConstraint(
            "warehouse_type IN ('finished','semi_finished','shared')",
            name="ck_warehouse_locations_type",
        ),
        CheckConstraint(
            "storage_type IS NULL OR storage_type IN ('ground','rack','temporary_aisle')",
            name="ck_warehouse_locations_storage_type",
        ),
        CheckConstraint(
            "placement_status IS NULL OR placement_status IN ('unplaced','placed')",
            name="ck_warehouse_locations_placement_status",
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
    warehouse_floor: Mapped[int | None] = mapped_column(Integer, nullable=True)
    area_code: Mapped[str | None] = mapped_column(String(30), nullable=True)
    storage_type: Mapped[str | None] = mapped_column(String(30), nullable=True)
    level_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    side_code: Mapped[str | None] = mapped_column(String(10), nullable=True)
    sort_order: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    is_temporary: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False
    )
    source_version: Mapped[str | None] = mapped_column(String(30), nullable=True)
    # Alembic owns the formal non-null/default gate. Keeping ORM-only test
    # schemas nullable preserves legacy fixtures that predate spatial placement.
    placement_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime, onupdate=func.current_timestamp(), nullable=True
    )
    current_pallet: Mapped["InventoryPallet | None"] = relationship(
        primaryjoin=lambda: (WarehouseLocation.id == InventoryPallet.location_id)
        & InventoryPallet.is_current.is_(True),
        viewonly=True,
        uselist=False,
    )
    floor3_layout: Mapped["Floor3LocationLayout | None"] = relationship(
        back_populates="location",
        cascade="all, delete-orphan",
        uselist=False,
    )


class Floor3LocationLayout(Base):
    """Interactive-map placement for one physical floor-three pallet location."""

    __tablename__ = "floor3_location_layouts"
    __table_args__ = (
        CheckConstraint(
            "left_pct >= 0 AND left_pct <= 100",
            name="ck_floor3_location_layouts_left_pct",
        ),
        CheckConstraint(
            "top_pct >= 0 AND top_pct <= 100",
            name="ck_floor3_location_layouts_top_pct",
        ),
        CheckConstraint(
            "width_pct > 0 AND width_pct <= 100",
            name="ck_floor3_location_layouts_width_pct",
        ),
        CheckConstraint(
            "height_pct > 0 AND height_pct <= 100",
            name="ck_floor3_location_layouts_height_pct",
        ),
        CheckConstraint(
            "left_pct + width_pct <= 100",
            name="ck_floor3_location_layouts_right_pct",
        ),
        CheckConstraint(
            "top_pct + height_pct <= 100",
            name="ck_floor3_location_layouts_bottom_pct",
        ),
        CheckConstraint("version > 0", name="ck_floor3_location_layouts_version"),
        CheckConstraint(
            "source_type IN ('seeded','manual')",
            name="ck_floor3_location_layouts_source_type",
        ),
        UniqueConstraint("location_id", name="uq_floor3_location_layouts_location"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    location_id: Mapped[int] = mapped_column(
        ForeignKey("warehouse_locations.id", ondelete="CASCADE"), nullable=False
    )
    left_pct: Mapped[Decimal] = mapped_column(Numeric(7, 4), nullable=False)
    top_pct: Mapped[Decimal] = mapped_column(Numeric(7, 4), nullable=False)
    width_pct: Mapped[Decimal] = mapped_column(Numeric(7, 4), nullable=False)
    height_pct: Mapped[Decimal] = mapped_column(Numeric(7, 4), nullable=False)
    z_index: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    version: Mapped[int] = mapped_column(
        Integer, default=1, server_default="1", nullable=False
    )
    source_type: Mapped[str] = mapped_column(
        String(20), default="manual", server_default="manual", nullable=False
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

    location: Mapped["WarehouseLocation"] = relationship(back_populates="floor3_layout")


class InventoryPallet(Base):
    __tablename__ = "inventory_pallets"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active','closed')", name="ck_inventory_pallets_status"
        ),
        CheckConstraint(
            "is_current = 0 OR location_id IS NOT NULL",
            name="ck_inventory_pallets_current_location",
        ),
        CheckConstraint("version > 0", name="ck_inventory_pallets_version"),
        UniqueConstraint("pallet_code", name="uq_inventory_pallets_code"),
        Index(
            "uq_inventory_pallets_current_location",
            "location_id",
            unique=True,
            sqlite_where=text("is_current = 1"),
            postgresql_where=text("is_current = true"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    pallet_code: Mapped[str] = mapped_column(String(100), nullable=False)
    location_id: Mapped[int | None] = mapped_column(
        ForeignKey("warehouse_locations.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(
        String(20), default="active", server_default="active", nullable=False
    )
    is_current: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=true(), nullable=False
    )
    needs_relocation: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False
    )
    version: Mapped[int] = mapped_column(
        Integer, default=1, server_default="1", nullable=False
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
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    location: Mapped["WarehouseLocation | None"] = relationship()
    items: Mapped[list["InventoryPalletItem"]] = relationship(
        back_populates="pallet", cascade="all, delete-orphan", order_by="InventoryPalletItem.id"
    )
    movements: Mapped[list["InventoryLocationMovement"]] = relationship(
        back_populates="pallet", cascade="all, delete-orphan", order_by="InventoryLocationMovement.id"
    )


class InventoryPalletItem(Base):
    __tablename__ = "inventory_pallet_items"
    __table_args__ = (
        CheckConstraint(
            "item_type IN ('finished','semi_finished','raw_material')",
            name="ck_inventory_pallet_items_type",
        ),
        CheckConstraint("quantity > 0", name="ck_inventory_pallet_items_quantity"),
        CheckConstraint(
            "match_status IN ('matched','pending')",
            name="ck_inventory_pallet_items_match_status",
        ),
        UniqueConstraint(
            "inventory_lot_id", name="uq_inventory_pallet_items_inventory_lot"
        ),
        Index("ix_inventory_pallet_items_pallet_id", "pallet_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    pallet_id: Mapped[int] = mapped_column(
        ForeignKey("inventory_pallets.id", ondelete="CASCADE"), nullable=False
    )
    inventory_lot_id: Mapped[int | None] = mapped_column(
        ForeignKey("inventory_lots.id", ondelete="CASCADE"), nullable=True
    )
    customer_id: Mapped[int | None] = mapped_column(
        ForeignKey("customers.id", ondelete="SET NULL"), nullable=True
    )
    product_id: Mapped[int | None] = mapped_column(
        ForeignKey("products.id", ondelete="SET NULL"), nullable=True
    )
    inventory_code: Mapped[str | None] = mapped_column(String(150), nullable=True)
    order_no: Mapped[str | None] = mapped_column(String(100), nullable=True)
    customer_name_snapshot: Mapped[str | None] = mapped_column(String(200), nullable=True)
    product_name: Mapped[str | None] = mapped_column(String(250), nullable=True)
    item_type: Mapped[str] = mapped_column(String(20), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(14, 3), nullable=False)
    unit: Mapped[str] = mapped_column(String(30), nullable=False)
    match_status: Mapped[str] = mapped_column(
        String(20), default="pending", server_default="pending", nullable=False
    )
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

    pallet: Mapped["InventoryPallet"] = relationship(back_populates="items")
    inventory_lot: Mapped["InventoryLot | None"] = relationship(
        back_populates="pallet_item"
    )
    customer: Mapped["Customer | None"] = relationship()
    product: Mapped["Product | None"] = relationship()


class InventoryLocationMovement(Base):
    __tablename__ = "inventory_location_movements"
    __table_args__ = (
        CheckConstraint(
            "movement_type IN ('create','add_item','move','clear')",
            name="ck_inventory_location_movements_type",
        ),
        Index(
            "uq_inventory_location_movements_idempotency_key",
            "idempotency_key",
            unique=True,
            sqlite_where=text("idempotency_key IS NOT NULL"),
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
        Index("ix_inventory_location_movements_pallet_moved", "pallet_id", "moved_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    pallet_id: Mapped[int] = mapped_column(
        ForeignKey("inventory_pallets.id", ondelete="CASCADE"), nullable=False
    )
    from_location_id: Mapped[int | None] = mapped_column(
        ForeignKey("warehouse_locations.id", ondelete="SET NULL"), nullable=True
    )
    to_location_id: Mapped[int | None] = mapped_column(
        ForeignKey("warehouse_locations.id", ondelete="SET NULL"), nullable=True
    )
    movement_type: Mapped[str] = mapped_column(String(20), nullable=False)
    operator_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    moved_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )
    idempotency_key: Mapped[str | None] = mapped_column(String(120), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    pallet_version_before: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pallet_version_after: Mapped[int | None] = mapped_column(Integer, nullable=True)
    remarks: Mapped[str | None] = mapped_column(Text, nullable=True)

    pallet: Mapped["InventoryPallet"] = relationship(back_populates="movements")


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
            "source_type IN ('manual','production_surplus','purchase_surplus','stocktake','transfer','replenishment')",
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
    estimated_unit_cost_snapshot: Mapped[Decimal | None] = mapped_column(
        Numeric(14, 4), nullable=True
    )
    estimated_square_price_snapshot: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 4), nullable=True
    )
    estimated_cost_area_m2_snapshot: Mapped[Decimal | None] = mapped_column(
        Numeric(14, 6), nullable=True
    )
    cost_snapshot_source: Mapped[str | None] = mapped_column(
        String(80), nullable=True
    )
    cost_snapshot_detail_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    cost_snapshot_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
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
    pallet_item: Mapped["InventoryPalletItem | None"] = relationship(
        back_populates="inventory_lot", uselist=False
    )
    allowed_products: Mapped[list["SemiFinishedLotAllowedProduct"]] = relationship(
        back_populates="lot", cascade="all, delete-orphan", order_by="SemiFinishedLotAllowedProduct.id"
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
        CheckConstraint("layer_count IN (3,5,7)", name="ck_semi_inventory_layer"),
        CheckConstraint(
            "(layer_count=3 AND flute_type IN ('A','B','E')) OR "
            "(layer_count=5 AND flute_type IN ('AB','BE')) OR "
            "(layer_count=7 AND flute_type IN ('AAA','ABC'))",
            name="ck_semi_inventory_flute",
        ),
        CheckConstraint("board_length_mm > 0", name="ck_semi_inventory_length"),
        CheckConstraint("board_width_mm > 0", name="ck_semi_inventory_width"),
        CheckConstraint(
            "sheet_type IN ('raw_board','net_sheet','creased_sheet')",
            name="ck_semi_inventory_sheet_type",
        ),
        CheckConstraint(
            "component_type IN ('whole','cover','base')",
            name="ck_semi_inventory_component",
        ),
        CheckConstraint(
            "pieces_per_box > 0",
            name="ck_semi_inventory_pieces_per_box",
        ),
        CheckConstraint(
            "stock_yield_per_sheet > 0",
            name="ck_semi_inventory_stock_yield",
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
        Index("ix_semi_inventory_material_id", "material_id"),
        Index(
            "ix_semi_inventory_shared_signature",
            "owner_customer_id",
            "board_length_mm",
            "board_width_mm",
            "normalized_material_code",
            "flute_type",
            "component_type",
            "pieces_per_box",
            "stock_yield_per_sheet",
        ),
    )

    inventory_lot_id: Mapped[int] = mapped_column(
        ForeignKey("inventory_lots.id", ondelete="CASCADE"), primary_key=True
    )
    supplier_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    owner_customer_id: Mapped[int | None] = mapped_column(
        ForeignKey("customers.id", ondelete="SET NULL"), nullable=True
    )
    material_id: Mapped[int | None] = mapped_column(
        ForeignKey("materials.id", ondelete="SET NULL"), nullable=True
    )
    owner_customer_name_snapshot: Mapped[str | None] = mapped_column(
        String(200), nullable=True
    )
    material_code_snapshot: Mapped[str] = mapped_column(String(100), nullable=False)
    normalized_material_code: Mapped[str] = mapped_column(String(100), nullable=False)
    layer_count: Mapped[int] = mapped_column(Integer, nullable=False)
    flute_type: Mapped[str] = mapped_column(String(20), nullable=False)
    board_length_mm: Mapped[int] = mapped_column(Integer, nullable=False)
    board_width_mm: Mapped[int] = mapped_column(Integer, nullable=False)
    component_type: Mapped[str] = mapped_column(
        String(20), default="whole", nullable=False
    )
    pieces_per_box: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    stock_yield_per_sheet: Mapped[int] = mapped_column(
        Integer, default=1, nullable=False
    )
    sheet_type: Mapped[str] = mapped_column(String(30), nullable=False)
    crease_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    crease_left_mm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    crease_middle_mm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    crease_right_mm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cutting_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    lot: Mapped["InventoryLot"] = relationship(back_populates="semi_finished_detail")
    customer: Mapped["Customer | None"] = relationship()
    material: Mapped["Material | None"] = relationship()


class OrderItemSemiRequirement(Base):
    __tablename__ = "order_item_semi_requirements"
    __table_args__ = (
        CheckConstraint(
            "component_type IN ('whole','cover','base')",
            name="ck_order_item_semi_requirements_component",
        ),
        CheckConstraint(
            "board_length_mm > 0 AND board_width_mm > 0",
            name="ck_order_item_semi_requirements_dimensions",
        ),
        CheckConstraint(
            "pieces_per_box > 0 AND stock_yield_per_sheet > 0",
            name="ck_order_item_semi_requirements_conversion",
        ),
        CheckConstraint(
            "required_piece_quantity > 0",
            name="ck_order_item_semi_requirements_quantity",
        ),
        UniqueConstraint(
            "order_item_id",
            "component_type",
            name="uq_order_item_semi_requirements_item_component",
        ),
        Index(
            "ix_order_item_semi_requirements_signature",
            "customer_id",
            "board_length_mm",
            "board_width_mm",
            "normalized_material_code",
            "flute_type",
            "component_type",
        ),
        Index(
            "ix_order_item_semi_requirements_bom_component",
            "sales_order_item_bom_component_id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    order_item_id: Mapped[int] = mapped_column(
        ForeignKey("sales_order_items.id", ondelete="CASCADE"), nullable=False
    )
    sales_order_item_bom_component_id: Mapped[int | None] = mapped_column(
        ForeignKey("sales_order_item_bom_components.id", ondelete="SET NULL"),
        nullable=True,
    )
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False
    )
    component_type: Mapped[str] = mapped_column(String(20), nullable=False)
    board_length_mm: Mapped[int] = mapped_column(Integer, nullable=False)
    board_width_mm: Mapped[int] = mapped_column(Integer, nullable=False)
    material_code_snapshot: Mapped[str] = mapped_column(String(100), nullable=False)
    normalized_material_code: Mapped[str] = mapped_column(String(100), nullable=False)
    flute_type: Mapped[str] = mapped_column(String(20), nullable=False)
    pieces_per_box: Mapped[int] = mapped_column(Integer, nullable=False)
    stock_yield_per_sheet: Mapped[int] = mapped_column(Integer, nullable=False)
    required_piece_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
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


class SemiFinishedMatchRule(Base):
    __tablename__ = "semi_finished_match_rules"
    __table_args__ = (
        CheckConstraint(
            "component_type IN ('whole','cover','base')",
            name="ck_semi_finished_match_rules_component",
        ),
        CheckConstraint(
            "board_length_mm > 0 AND board_width_mm > 0",
            name="ck_semi_finished_match_rules_dimensions",
        ),
        CheckConstraint(
            "pieces_per_box > 0 AND stock_yield_per_sheet > 0",
            name="ck_semi_finished_match_rules_conversion",
        ),
        UniqueConstraint(
            "customer_id",
            "board_length_mm",
            "board_width_mm",
            "normalized_material_code",
            "flute_type",
            "component_type",
            "pieces_per_box",
            "stock_yield_per_sheet",
            name="uq_semi_finished_match_rules_signature",
        ),
        Index(
            "ix_semi_finished_match_rules_active_component",
            "active",
            "component_type",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False
    )
    board_length_mm: Mapped[int] = mapped_column(Integer, nullable=False)
    board_width_mm: Mapped[int] = mapped_column(Integer, nullable=False)
    normalized_material_code: Mapped[str] = mapped_column(String(100), nullable=False)
    flute_type: Mapped[str] = mapped_column(String(20), nullable=False)
    component_type: Mapped[str] = mapped_column(String(20), nullable=False)
    pieces_per_box: Mapped[int] = mapped_column(Integer, nullable=False)
    stock_yield_per_sheet: Mapped[int] = mapped_column(Integer, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
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


class SemiFinishedMatchRuleProduct(Base):
    """Shared recommendation memory; this is not a per-lot authorization."""

    __tablename__ = "semi_finished_match_rule_products"
    __table_args__ = (
        UniqueConstraint(
            "rule_id",
            "product_id",
            name="uq_semi_finished_match_rule_products_rule_product",
        ),
        Index(
            "ix_semi_finished_match_rule_products_product",
            "product_id",
            "rule_id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    rule_id: Mapped[int] = mapped_column(
        ForeignKey("semi_finished_match_rules.id", ondelete="CASCADE"),
        nullable=False,
    )
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"), nullable=False
    )
    confirmed_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    confirmed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class SemiFinishedLotAllowedProduct(Base):
    """Hard product binding for one semi-finished inventory lot."""

    __tablename__ = "semi_finished_lot_allowed_products"
    __table_args__ = (
        UniqueConstraint(
            "inventory_lot_id",
            "product_id",
            name="uq_semi_finished_lot_allowed_products_lot_product",
        ),
        Index(
            "ix_semi_finished_lot_allowed_products_product_lot",
            "product_id",
            "inventory_lot_id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    inventory_lot_id: Mapped[int] = mapped_column(
        ForeignKey("inventory_lots.id", ondelete="CASCADE"), nullable=False
    )
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"), nullable=False
    )
    confirmed_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    confirmed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    lot: Mapped["InventoryLot"] = relationship(back_populates="allowed_products")
    product: Mapped["Product"] = relationship()


class InventoryReservation(Base):
    __tablename__ = "inventory_reservations"
    __table_args__ = (
        CheckConstraint(
            "reservation_type IN ('finished_order','semi_requisition','semi_order')",
            name="ck_inventory_reservations_type",
        ),
        CheckConstraint(
            "status IN ('active','partial','released','consumed','cancelled')",
            name="ck_inventory_reservations_status",
        ),
        CheckConstraint(
            "reserved_stock_quantity > 0",
            name="ck_inventory_reservations_quantity",
        ),
        CheckConstraint(
            "consumed_stock_quantity >= 0 AND released_stock_quantity >= 0 "
            "AND consumed_stock_quantity + released_stock_quantity "
            "<= reserved_stock_quantity",
            name="ck_inventory_reservations_cumulative_quantities",
        ),
        CheckConstraint(
            "consumed_requirement_quantity >= 0 "
            "AND released_requirement_quantity >= 0 "
            "AND (credited_requirement_quantity IS NULL OR "
            "consumed_requirement_quantity + released_requirement_quantity "
            "<= credited_requirement_quantity)",
            name="ck_inventory_reservations_cumulative_requirement_quantities",
        ),
        UniqueConstraint("reservation_number", name="uq_inventory_reservations_number"),
        UniqueConstraint("idempotency_key", name="uq_inventory_reservations_idempotency"),
        UniqueConstraint(
            "reservation_group_key",
            "inventory_lot_id",
            name="uq_inventory_reservations_group_lot",
        ),
        Index("ix_inventory_reservations_lot_status", "inventory_lot_id", "status"),
        Index(
            "ix_inventory_reservations_order_item",
            "order_item_id",
            "reservation_type",
            "status",
        ),
        Index("ix_inventory_reservations_requisition", "requisition_item_id"),
        Index(
            "ix_inventory_reservations_semi_requirement",
            "semi_requirement_id",
            "status",
        ),
        Index("ix_inventory_reservations_match_rule", "match_rule_id"),
        Index("ix_inventory_reservations_group", "reservation_group_key"),
        Index(
            "ix_inventory_reservations_bom_component",
            "sales_order_item_bom_component_id",
        ),
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
    sales_order_item_bom_component_id: Mapped[int | None] = mapped_column(
        ForeignKey("sales_order_item_bom_components.id", ondelete="SET NULL"),
        nullable=True,
    )
    requisition_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("material_requisition_items.id", ondelete="SET NULL"), nullable=True
    )
    semi_requirement_id: Mapped[int | None] = mapped_column(
        ForeignKey("order_item_semi_requirements.id", ondelete="SET NULL"),
        nullable=True,
    )
    match_rule_id: Mapped[int | None] = mapped_column(
        ForeignKey("semi_finished_match_rules.id", ondelete="SET NULL"),
        nullable=True,
    )
    reserved_stock_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    credited_requirement_quantity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    yield_factor: Mapped[int | None] = mapped_column(Integer, nullable=True)
    consumed_stock_quantity: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )
    released_stock_quantity: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )
    consumed_requirement_quantity: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )
    released_requirement_quantity: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )
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
    reservation_group_key: Mapped[str | None] = mapped_column(String(100), nullable=True)
    reservation_group_requested_quantity: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
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
            "'transfer_to_general','reserve','release_reserve','consume','reverse_consume')",
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


class DeliveryInventoryAllocation(Base):
    __tablename__ = "delivery_inventory_allocations"
    __table_args__ = (
        CheckConstraint(
            "consumed_stock_quantity > 0 AND credited_requirement_quantity > 0",
            name="ck_delivery_inventory_allocations_quantities",
        ),
        CheckConstraint(
            "reversed_stock_quantity >= 0 "
            "AND reversed_stock_quantity <= consumed_stock_quantity "
            "AND reversed_requirement_quantity >= 0 "
            "AND reversed_requirement_quantity <= credited_requirement_quantity",
            name="ck_delivery_inventory_allocations_reversed",
        ),
        CheckConstraint(
            "status IN ('active','partial','reversed')",
            name="ck_delivery_inventory_allocations_status",
        ),
        UniqueConstraint(
            "consume_movement_id",
            name="uq_delivery_inventory_allocations_consume_movement",
        ),
        Index(
            "ix_delivery_inventory_allocations_delivery_item",
            "delivery_item_id",
            "status",
        ),
        Index(
            "ix_delivery_inventory_allocations_reservation",
            "reservation_id",
            "status",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    delivery_item_id: Mapped[int] = mapped_column(
        ForeignKey("sales_delivery_items.id", ondelete="RESTRICT"), nullable=False
    )
    reservation_id: Mapped[int] = mapped_column(
        ForeignKey("inventory_reservations.id", ondelete="RESTRICT"), nullable=False
    )
    consume_movement_id: Mapped[int] = mapped_column(
        ForeignKey("inventory_movements.id", ondelete="RESTRICT"), nullable=False
    )
    consumed_stock_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    credited_requirement_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    reversed_stock_quantity: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )
    reversed_requirement_quantity: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    reversed_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )
    reversed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
