from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
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
    from app.models.product_bom import ProductBomComponent
    from app.models.user import User
    from app.models.warehouse_inventory import (
        InventoryLot,
        InventoryReservation,
        WarehouseLocation,
    )


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
    report_length_mm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    report_width_mm: Mapped[int | None] = mapped_column(Integer, nullable=True)
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
    component_plans: Mapped[list["StockReplenishmentBomComponentPlan"]] = relationship(
        back_populates="order",
        order_by="StockReplenishmentBomComponentPlan.display_order",
        passive_deletes=True,
    )


class StockReplenishmentOrderItem(Base):
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
    report_length_mm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    report_width_mm: Mapped[int | None] = mapped_column(Integer, nullable=True)
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
    bom_component_plan: Mapped["StockReplenishmentBomComponentPlan | None"] = relationship(
        back_populates="replenishment_item",
        uselist=False,
        passive_deletes=True,
    )


class StockReplenishmentBomComponentPlan(Base):
    """Frozen physical-component plan for one virtual composite parent demand.

    A plan row remains present even when inventory offsets the whole component
    demand.  In that case ``replenishment_item_id`` is deliberately NULL and no
    supplier purchase line is fabricated for the virtual parent or component.
    """

    __tablename__ = "stock_replenishment_bom_component_plans"
    __table_args__ = (
        CheckConstraint(
            "parent_product_version > 0 AND component_product_version > 0",
            name="ck_stock_replenishment_bom_plan_product_versions",
        ),
        CheckConstraint(
            "parent_product_id <> component_product_id",
            name="ck_stock_replenishment_bom_plan_distinct_products",
        ),
        CheckConstraint(
            "display_order >= 0",
            name="ck_stock_replenishment_bom_plan_display_order",
        ),
        CheckConstraint(
            "parent_set_quantity > 0 AND quantity_per_set > 0",
            name="ck_stock_replenishment_bom_plan_parent_quantities",
        ),
        CheckConstraint(
            "required_piece_quantity = parent_set_quantity * quantity_per_set",
            name="ck_stock_replenishment_bom_plan_required_formula",
        ),
        CheckConstraint(
            "incoming_covered_piece_quantity >= 0 "
            "AND reserved_piece_quantity >= 0 "
            "AND incoming_covered_piece_quantity + reserved_piece_quantity "
            "<= required_piece_quantity",
            name="ck_stock_replenishment_bom_plan_coverages",
        ),
        CheckConstraint(
            "net_required_piece_quantity = required_piece_quantity "
            "- incoming_covered_piece_quantity - reserved_piece_quantity",
            name="ck_stock_replenishment_bom_plan_net_formula",
        ),
        CheckConstraint(
            "pieces_per_box > 0 AND yield_per_sheet > 0 "
            "AND spare_sheet_quantity >= 0",
            name="ck_stock_replenishment_bom_plan_conversions",
        ),
        CheckConstraint(
            "length(trim(cutting_mode_snapshot)) > 0 "
            "AND (mold_max_yield_per_sheet_snapshot IS NULL "
            "OR mold_max_yield_per_sheet_snapshot > 0) "
            "AND (NOT is_die_cut_snapshot OR "
            "(mold_max_yield_per_sheet_snapshot IS NOT NULL "
            "AND yield_per_sheet <= mold_max_yield_per_sheet_snapshot))",
            name="ck_stock_replenishment_bom_plan_cutting_snapshot",
        ),
        CheckConstraint(
            "(net_required_piece_quantity = 0 "
            "AND purchase_sheet_quantity = 0) OR "
            "(net_required_piece_quantity > 0 "
            "AND purchase_sheet_quantity > spare_sheet_quantity "
            "AND (purchase_sheet_quantity - spare_sheet_quantity) "
            "* yield_per_sheet >= net_required_piece_quantity "
            "AND (purchase_sheet_quantity - spare_sheet_quantity - 1) "
            "* yield_per_sheet < net_required_piece_quantity)",
            name="ck_stock_replenishment_bom_plan_purchase_formula",
        ),
        CheckConstraint(
            "cutting_remainder_piece_quantity = "
            "CASE WHEN net_required_piece_quantity = 0 THEN 0 ELSE "
            "(purchase_sheet_quantity - spare_sheet_quantity) * yield_per_sheet END "
            "- net_required_piece_quantity",
            name="ck_stock_replenishment_bom_plan_remainder_formula",
        ),
        CheckConstraint(
            "(purchase_sheet_quantity = 0 AND replenishment_item_id IS NULL) OR "
            "(purchase_sheet_quantity > 0 AND replenishment_item_id IS NOT NULL)",
            name="ck_stock_replenishment_bom_plan_purchase_item",
        ),
        CheckConstraint(
            "component_type IN ('whole','cover','base')",
            name="ck_stock_replenishment_bom_plan_component_type",
        ),
        CheckConstraint(
            "sheet_type IN ('raw_board','net_sheet','creased_sheet')",
            name="ck_stock_replenishment_bom_plan_sheet_type",
        ),
        CheckConstraint(
            "procurement_route_snapshot IS NULL OR "
            "procurement_route_snapshot IN ('paperboard','external_packaging')",
            name="ck_stock_replenishment_bom_plan_procurement_route",
        ),
        CheckConstraint(
            "length(bom_fingerprint) = 64 AND length(request_fingerprint) = 64",
            name="ck_stock_replenishment_bom_plan_fingerprints",
        ),
        UniqueConstraint(
            "replenishment_order_id",
            "product_bom_component_id",
            name="uq_stock_replenishment_bom_plan_order_component",
        ),
        UniqueConstraint(
            "replenishment_item_id",
            name="uq_stock_replenishment_bom_plan_item",
        ),
        Index(
            "ix_stock_replenishment_bom_plan_order",
            "replenishment_order_id",
            "display_order",
        ),
        Index(
            "ix_stock_replenishment_bom_plan_policy",
            "stock_policy_id",
        ),
        Index(
            "ix_stock_replenishment_bom_plan_parent",
            "parent_product_id",
        ),
        Index(
            "ix_stock_replenishment_bom_plan_component",
            "component_product_id",
        ),
        Index(
            "ix_stock_replenishment_bom_plan_request",
            "request_fingerprint",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    replenishment_order_id: Mapped[int] = mapped_column(
        ForeignKey("stock_replenishment_orders.id", ondelete="RESTRICT"),
        nullable=False,
    )
    replenishment_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("stock_replenishment_order_items.id", ondelete="RESTRICT"),
        nullable=True,
    )
    stock_policy_id: Mapped[int | None] = mapped_column(
        ForeignKey("inventory_stock_policies.id", ondelete="RESTRICT"),
        nullable=True,
    )
    parent_product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"), nullable=False
    )
    parent_product_version: Mapped[int] = mapped_column(Integer, nullable=False)
    parent_product_code_snapshot: Mapped[str | None] = mapped_column(
        String(150), nullable=True
    )
    parent_product_name_snapshot: Mapped[str] = mapped_column(
        String(250), nullable=False
    )
    product_bom_component_id: Mapped[int] = mapped_column(
        ForeignKey("product_bom_components.id", ondelete="RESTRICT"),
        nullable=False,
    )
    component_product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"), nullable=False
    )
    component_product_version: Mapped[int] = mapped_column(Integer, nullable=False)
    component_product_code_snapshot: Mapped[str | None] = mapped_column(
        String(150), nullable=True
    )
    component_product_name_snapshot: Mapped[str] = mapped_column(
        String(250), nullable=False
    )
    display_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    parent_set_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    quantity_per_set: Mapped[int] = mapped_column(Integer, nullable=False)
    required_piece_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    incoming_covered_piece_quantity: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )
    reserved_piece_quantity: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )
    net_required_piece_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    pieces_per_box: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    yield_per_sheet: Mapped[int] = mapped_column(Integer, nullable=False)
    cutting_mode_snapshot: Mapped[str] = mapped_column(String(30), nullable=False)
    is_die_cut_snapshot: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0", nullable=False
    )
    mold_max_yield_per_sheet_snapshot: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    spare_sheet_quantity: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )
    purchase_sheet_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    cutting_remainder_piece_quantity: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )
    customer_id: Mapped[int | None] = mapped_column(
        ForeignKey("customers.id", ondelete="RESTRICT"), nullable=True
    )
    supplier_name_snapshot: Mapped[str | None] = mapped_column(
        String(200), nullable=True
    )
    procurement_route_snapshot: Mapped[str | None] = mapped_column(
        String(30), nullable=True
    )
    material_id: Mapped[int | None] = mapped_column(
        ForeignKey("materials.id", ondelete="RESTRICT"), nullable=True
    )
    material_code_snapshot: Mapped[str | None] = mapped_column(
        String(100), nullable=True
    )
    normalized_material_code: Mapped[str | None] = mapped_column(
        String(100), nullable=True
    )
    layer_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    flute_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    report_length_mm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    report_width_mm: Mapped[int | None] = mapped_column(Integer, nullable=True)
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
    internal_component_code_snapshot: Mapped[str] = mapped_column(
        String(150), nullable=False
    )
    bom_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )

    order: Mapped["StockReplenishmentOrder"] = relationship(
        back_populates="component_plans"
    )
    replenishment_item: Mapped["StockReplenishmentOrderItem | None"] = relationship(
        back_populates="bom_component_plan",
        foreign_keys=[replenishment_item_id],
    )
    stock_policy: Mapped["InventoryStockPolicy | None"] = relationship()
    parent_product: Mapped["Product"] = relationship(
        foreign_keys=[parent_product_id]
    )
    product_bom_component: Mapped["ProductBomComponent"] = relationship()
    component_product: Mapped["Product"] = relationship(
        foreign_keys=[component_product_id]
    )
    customer: Mapped["Customer | None"] = relationship()
    material: Mapped["Material | None"] = relationship()
    reservations: Mapped[list["InventoryReservation"]] = relationship(
        back_populates="stock_replenishment_bom_component_plan",
        passive_deletes=True,
    )
