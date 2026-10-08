from __future__ import annotations
from app.models.dimension_type import SheetDimensionColumn

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    JSON,
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
    from app.models.customer import Customer
    from app.models.material import Material
    from app.models.product import Product
    from app.models.user import User
    from app.models.warehouse_inventory import InventoryLot, WarehouseLocation


class InventoryStockPolicy(Base):
    __tablename__ = "inventory_stock_policies"
    __table_args__ = (
        CheckConstraint(
            "target_inventory_type IN ('finished','semi_finished')",
            name="ck_inventory_stock_policies_target_type",
        ),
        CheckConstraint(
            "warning_quantity >= 0 AND target_quantity > 0 "
            "AND target_quantity >= warning_quantity",
            name="ck_inventory_stock_policies_quantities",
        ),
        CheckConstraint(
            "component_type IN ('whole','cover','base')",
            name="ck_inventory_stock_policies_component",
        ),
        CheckConstraint(
            "pieces_per_box > 0 AND stock_yield_per_sheet > 0",
            name="ck_inventory_stock_policies_conversion",
        ),
        Index(
            "ix_inventory_stock_policies_active_type",
            "active",
            "target_inventory_type",
        ),
        Index(
            "ix_inventory_stock_policies_product",
            "product_id",
            "active",
        ),
        Index(
            "ix_inventory_stock_policies_semi_signature",
            "customer_id",
            "report_length_mm",
            "report_width_mm",
            "normalized_material_code",
            "flute_type",
            "component_type",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    policy_name: Mapped[str] = mapped_column(String(200), nullable=False)
    target_inventory_type: Mapped[str] = mapped_column(String(30), nullable=False)
    product_id: Mapped[int | None] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"), nullable=True
    )
    customer_id: Mapped[int | None] = mapped_column(
        ForeignKey("customers.id", ondelete="RESTRICT"), nullable=True
    )
    material_code_snapshot: Mapped[str | None] = mapped_column(String(100), nullable=True)
    normalized_material_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    layer_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    flute_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    report_length_mm: Mapped[int | float | None] = mapped_column(SheetDimensionColumn, nullable=True)
    report_width_mm: Mapped[int | float | None] = mapped_column(SheetDimensionColumn, nullable=True)
    sheet_type: Mapped[str] = mapped_column(
        String(30), default="raw_board", nullable=False
    )
    component_type: Mapped[str] = mapped_column(
        String(20), default="whole", nullable=False
    )
    pieces_per_box: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    stock_yield_per_sheet: Mapped[int] = mapped_column(
        Integer, default=1, nullable=False
    )
    warning_quantity: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    target_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    default_location_id: Mapped[int | None] = mapped_column(
        ForeignKey("warehouse_locations.id", ondelete="SET NULL"), nullable=True
    )
    supplier_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    remark: Mapped[str | None] = mapped_column(Text, nullable=True)
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

    product: Mapped["Product | None"] = relationship()
    customer: Mapped["Customer | None"] = relationship()
    default_location: Mapped["WarehouseLocation | None"] = relationship()


class StockReplenishmentOrder(Base):
    __tablename__ = "stock_replenishment_orders"
    __table_args__ = (
        UniqueConstraint("order_number", name="uq_stock_replenishment_orders_number"),
        CheckConstraint(
            "source_type IN ('stock_warning','customer_request','manual_history')",
            name="ck_stock_replenishment_orders_source",
        ),
        CheckConstraint(
            "status IN ('draft','confirmed','partially_stocked','stocked','voided')",
            name="ck_stock_replenishment_orders_status",
        ),
        Index("ix_stock_replenishment_orders_status", "status", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    order_number: Mapped[str] = mapped_column(String(40), nullable=False)
    request_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    supplier_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    customer_id: Mapped[int | None] = mapped_column(
        ForeignKey("customers.id", ondelete="SET NULL"), nullable=True
    )
    source_type: Mapped[str] = mapped_column(String(30), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="draft", nullable=False)
    remark: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    confirmed_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    stocked_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    stocked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    voided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    customer: Mapped["Customer | None"] = relationship()
    items: Mapped[list["StockReplenishmentOrderItem"]] = relationship(
        back_populates="order",
        cascade="all, delete-orphan",
        order_by="StockReplenishmentOrderItem.id",
    )


class StockReplenishmentOrderItem(Base):
    sheet_cutting_snapshot: Mapped[dict | None] = mapped_column(JSON(none_as_null=True), nullable=True)
    __tablename__ = "stock_replenishment_order_items"
    __table_args__ = (
        CheckConstraint(
            "target_inventory_type IN ('finished','semi_finished')",
            name="ck_stock_replenishment_items_target_type",
        ),
        CheckConstraint(
            "procurement_route_snapshot IS NULL OR "
            "procurement_route_snapshot IN ('paperboard','external_packaging')",
            name="ck_stock_replenishment_items_procurement_route",
        ),
        CheckConstraint(
            "quantity > 0 AND stocked_quantity >= 0 "
            "AND stocked_quantity <= quantity",
            name="ck_stock_replenishment_items_quantities",
        ),
        CheckConstraint(
            "component_type IN ('whole','cover','base')",
            name="ck_stock_replenishment_items_component",
        ),
        CheckConstraint(
            "pieces_per_box > 0 AND stock_yield_per_sheet > 0",
            name="ck_stock_replenishment_items_conversion",
        ),
        Index("ix_stock_replenishment_items_order", "replenishment_order_id"),
        Index("ix_stock_replenishment_items_policy", "stock_policy_id"),
        Index("ix_stock_replenishment_items_lot", "inventory_lot_id"),
        Index("ix_stock_replenishment_items_material", "material_id"),
        Index(
            "ix_stock_replenishment_items_reference_product",
            "reference_product_id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    replenishment_order_id: Mapped[int] = mapped_column(
        ForeignKey("stock_replenishment_orders.id", ondelete="CASCADE"),
        nullable=False,
    )
    stock_policy_id: Mapped[int | None] = mapped_column(
        ForeignKey("inventory_stock_policies.id", ondelete="SET NULL"), nullable=True
    )
    target_inventory_type: Mapped[str] = mapped_column(String(30), nullable=False)
    procurement_route_snapshot: Mapped[str | None] = mapped_column(
        String(30), nullable=True
    )
    product_id: Mapped[int | None] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"), nullable=True
    )
    reference_product_id: Mapped[int | None] = mapped_column(
        ForeignKey("products.id", ondelete="SET NULL"), nullable=True
    )
    customer_id: Mapped[int | None] = mapped_column(
        ForeignKey("customers.id", ondelete="SET NULL"), nullable=True
    )
    material_id: Mapped[int | None] = mapped_column(
        ForeignKey("materials.id", ondelete="SET NULL"), nullable=True
    )
    product_code_snapshot: Mapped[str | None] = mapped_column(String(150), nullable=True)
    product_name_snapshot: Mapped[str] = mapped_column(String(250), nullable=False)
    internal_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    material_code_snapshot: Mapped[str | None] = mapped_column(String(100), nullable=True)
    normalized_material_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    layer_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    flute_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    report_length_mm: Mapped[int | float | None] = mapped_column(SheetDimensionColumn, nullable=True)
    report_width_mm: Mapped[int | float | None] = mapped_column(SheetDimensionColumn, nullable=True)
    crease_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    crease_left_mm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    crease_middle_mm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    crease_right_mm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sheet_type: Mapped[str] = mapped_column(
        String(30), default="raw_board", nullable=False
    )
    component_type: Mapped[str] = mapped_column(
        String(20), default="whole", nullable=False
    )
    pieces_per_box: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    stock_yield_per_sheet: Mapped[int] = mapped_column(
        Integer, default=1, nullable=False
    )
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    # NULL preserves the original customer-plan quantity on legacy external orders.
    quantity_contract_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    stocked_quantity: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    location_id: Mapped[int | None] = mapped_column(
        ForeignKey("warehouse_locations.id", ondelete="SET NULL"), nullable=True
    )
    inventory_lot_id: Mapped[int | None] = mapped_column(
        ForeignKey("inventory_lots.id", ondelete="SET NULL"), nullable=True
    )
    historical_workbook: Mapped[str | None] = mapped_column(String(260), nullable=True)
    historical_sheet: Mapped[str | None] = mapped_column(String(150), nullable=True)
    historical_row: Mapped[int | None] = mapped_column(Integer, nullable=True)
    historical_search_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    remark: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )
    stocked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    order: Mapped["StockReplenishmentOrder"] = relationship(back_populates="items")
    stock_policy: Mapped["InventoryStockPolicy | None"] = relationship()
    product: Mapped["Product | None"] = relationship(foreign_keys=[product_id])
    reference_product: Mapped["Product | None"] = relationship(
        foreign_keys=[reference_product_id]
    )
    customer: Mapped["Customer | None"] = relationship()
    material: Mapped["Material | None"] = relationship()
    location: Mapped["WarehouseLocation | None"] = relationship(
        foreign_keys=[location_id]
    )
    inventory_lot: Mapped["InventoryLot | None"] = relationship(
        foreign_keys=[inventory_lot_id]
    )
