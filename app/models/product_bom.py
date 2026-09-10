from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models import Base

if TYPE_CHECKING:
    from app.models.mold_tool import MoldTool
    from app.models.order import OrderItem
    from app.models.product import Product
    from app.models.requisition import RequisitionItem


class ProductBomComponent(Base):
    __tablename__ = "product_bom_components"
    __table_args__ = (
        CheckConstraint(
            "parent_product_id <> component_product_id",
            name="ck_product_bom_components_distinct_products",
        ),
        CheckConstraint(
            "quantity_per_set > 0 AND quantity_per_set = round(quantity_per_set, 0)",
            name="ck_product_bom_components_quantity_per_set",
        ),
        CheckConstraint(
            "display_order >= 0",
            name="ck_product_bom_components_display_order",
        ),
        CheckConstraint(
            "length(trim(internal_component_code)) > 0",
            name="ck_product_bom_components_internal_component_code",
        ),
        CheckConstraint(
            "mold_max_yield_per_sheet IS NULL OR mold_max_yield_per_sheet > 0",
            name="ck_product_bom_components_mold_yield",
        ),
        CheckConstraint(
            "spare_sheet_quantity >= 0",
            name="ck_product_bom_components_spare_sheets",
        ),
        CheckConstraint(
            "display_mode IN ('internal_only', 'show_on_delivery', 'show_on_all_docs')",
            name="ck_product_bom_components_display_mode",
        ),
        CheckConstraint(
            "is_die_cut IS TRUE OR (mold_tool_id IS NULL "
            "AND mold_max_yield_per_sheet IS NULL)",
            name="ck_product_bom_components_non_die_cut_mold_fields",
        ),
        CheckConstraint(
            "is_die_cut IS FALSE OR mold_tool_id IS NOT NULL",
            name="ck_product_bom_components_die_cut_mold_required",
        ),
        UniqueConstraint(
            "parent_product_id",
            "component_product_id",
            name="uq_product_bom_components_parent_component",
        ),
        UniqueConstraint(
            "parent_product_id",
            "display_order",
            name="uq_product_bom_components_parent_display_order",
        ),
        Index(
            "ix_product_bom_components_parent_display_order",
            "parent_product_id",
            "display_order",
        ),
        Index(
            "ix_product_bom_components_component_product_id",
            "component_product_id",
        ),
        Index("ix_product_bom_components_mold_tool_id", "mold_tool_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    parent_product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"),
        nullable=False,
    )
    component_product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"),
        nullable=False,
    )
    quantity_per_set: Mapped[Decimal] = mapped_column(Numeric(14, 4), nullable=False)
    display_order: Mapped[int] = mapped_column(Integer, nullable=False)
    internal_component_code: Mapped[str] = mapped_column(String(150), nullable=False)
    is_die_cut: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    die_cut_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    mold_tool_id: Mapped[int | None] = mapped_column(
        ForeignKey("mold_tools.id", ondelete="RESTRICT"),
        nullable=True,
    )
    mold_max_yield_per_sheet: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )
    spare_sheet_quantity: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
    )
    display_mode: Mapped[str] = mapped_column(
        String(30),
        default="internal_only",
        nullable=False,
    )
    show_on_delivery: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default="1",
        nullable=False,
    )
    is_required: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    remark: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.current_timestamp(),
        nullable=False,
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        onupdate=func.current_timestamp(),
        nullable=True,
    )

    parent_product: Mapped["Product"] = relationship(
        foreign_keys=[parent_product_id],
        back_populates="bom_components",
    )
    component_product: Mapped["Product"] = relationship(
        foreign_keys=[component_product_id],
    )
    mold_tool: Mapped["MoldTool | None"] = relationship()


class SalesOrderItemBomComponent(Base):
    """Immutable component requisition inputs captured with one parent order item."""

    __tablename__ = "sales_order_item_bom_components"
    __table_args__ = (
        CheckConstraint(
            "quantity_per_set > 0 AND quantity_per_set = round(quantity_per_set, 0)",
            name="ck_sales_order_item_bom_components_quantity_per_set",
        ),
        CheckConstraint(
            "order_set_quantity > 0",
            name="ck_sales_order_item_bom_components_order_set_quantity",
        ),
        CheckConstraint(
            "required_piece_quantity > 0",
            name="ck_sales_order_item_bom_components_required_piece_quantity",
        ),
        CheckConstraint(
            "required_piece_quantity = order_set_quantity * quantity_per_set",
            name="ck_sales_order_item_bom_components_required_piece_formula",
        ),
        CheckConstraint(
            "display_order >= 0",
            name="ck_sales_order_item_bom_components_display_order",
        ),
        CheckConstraint(
            "length(trim(internal_component_code)) > 0",
            name="ck_sales_order_item_bom_components_internal_component_code",
        ),
        CheckConstraint(
            "mold_max_yield_per_sheet IS NULL OR mold_max_yield_per_sheet > 0",
            name="ck_sales_order_item_bom_components_mold_yield",
        ),
        CheckConstraint(
            "spare_sheet_quantity >= 0",
            name="ck_sales_order_item_bom_components_spare_sheets",
        ),
        CheckConstraint(
            "display_mode IN ('internal_only', 'show_on_delivery', 'show_on_all_docs')",
            name="ck_sales_order_item_bom_components_display_mode",
        ),
        CheckConstraint(
            "is_die_cut IS TRUE OR (snapshot_mold_tool_id IS NULL "
            "AND mold_max_yield_per_sheet IS NULL)",
            name="ck_sales_order_item_bom_components_non_die_cut_mold_fields",
        ),
        CheckConstraint(
            "is_die_cut IS FALSE OR snapshot_mold_tool_id IS NOT NULL",
            name="ck_sales_order_item_bom_components_die_cut_mold_required",
        ),
        CheckConstraint(
            "(snapshot_component_default_cutting_mode IN "
            "('一开一','一开二','一开三','一开四','一开五','一开六') "
            "OR (substr(snapshot_component_default_cutting_mode, 1, 2) = '一开' "
            "AND length(snapshot_component_default_cutting_mode) BETWEEN 3 AND 20 "
            "AND CAST(substr(snapshot_component_default_cutting_mode, 3) AS BIGINT) > 0 "
            "AND substr(snapshot_component_default_cutting_mode, 3) = "
            "CAST(CAST(substr(snapshot_component_default_cutting_mode, 3) AS BIGINT) AS VARCHAR)))",
            name="ck_sales_order_item_bom_components_default_cutting_mode",
        ),
        UniqueConstraint(
            "sales_order_item_id",
            "display_order",
            name="uq_sales_order_item_bom_components_item_display_order",
        ),
        UniqueConstraint(
            "sales_order_item_id",
            "product_bom_component_id",
            name="uq_sales_order_item_bom_components_item_source",
        ),
        Index(
            "ix_sales_order_item_bom_components_item_display_order",
            "sales_order_item_id",
            "display_order",
        ),
        Index(
            "uq_bom_snapshot_order_identity",
            "id", "sales_order_item_id", unique=True,
        ),
        Index(
            "uq_bom_snapshot_order_product_identity",
            "id", "sales_order_item_id", "component_product_id", unique=True,
        ),
        Index(
            "ix_sales_order_item_bom_components_component_product_id",
            "component_product_id",
        ),
        Index(
            "ix_sales_order_item_bom_components_source_component_id",
            "product_bom_component_id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    sales_order_item_id: Mapped[int] = mapped_column(
        ForeignKey("sales_order_items.id", ondelete="CASCADE"),
        nullable=False,
    )
    product_bom_component_id: Mapped[int | None] = mapped_column(
        ForeignKey("product_bom_components.id", ondelete="SET NULL"),
        nullable=True,
    )
    component_product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"),
        nullable=False,
    )
    parent_product_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    component_product_version: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )
    snapshot_schema_version: Mapped[int] = mapped_column(
        Integer,
        default=3,
        nullable=False,
    )
    order_set_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    quantity_per_set: Mapped[Decimal] = mapped_column(Numeric(14, 4), nullable=False)
    required_piece_quantity: Mapped[Decimal] = mapped_column(
        Numeric(14, 4),
        nullable=False,
    )
    display_order: Mapped[int] = mapped_column(Integer, nullable=False)
    internal_component_code: Mapped[str] = mapped_column(String(150), nullable=False)
    is_die_cut: Mapped[bool] = mapped_column(Boolean, nullable=False)
    snapshot_die_cut_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    snapshot_mold_tool_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    snapshot_mold_tool_code: Mapped[str | None] = mapped_column(String(120), nullable=True)
    snapshot_mold_tool_name: Mapped[str | None] = mapped_column(String(250), nullable=True)
    mold_max_yield_per_sheet: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )
    spare_sheet_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    display_mode: Mapped[str] = mapped_column(String(30), nullable=False)
    show_on_delivery: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default="1",
        nullable=False,
    )
    is_required: Mapped[bool] = mapped_column(Boolean, nullable=False)
    remark: Mapped[str | None] = mapped_column(Text, nullable=True)
    snapshot_component_product_code: Mapped[str] = mapped_column(
        String(150),
        nullable=False,
    )
    snapshot_component_product_name: Mapped[str] = mapped_column(
        String(250),
        nullable=False,
    )
    snapshot_component_spec: Mapped[str | None] = mapped_column(String(150), nullable=True)
    snapshot_component_material: Mapped[str | None] = mapped_column(
        String(250),
        nullable=True,
    )
    snapshot_component_material_id: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )
    snapshot_component_supplier_name: Mapped[str | None] = mapped_column(
        String(200),
        nullable=True,
    )
    snapshot_component_layer_count: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )
    snapshot_component_flute_type: Mapped[str | None] = mapped_column(
        String(20),
        nullable=True,
    )
    snapshot_component_box_category: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
    )
    snapshot_component_box_style: Mapped[str | None] = mapped_column(
        String(150),
        nullable=True,
    )
    snapshot_component_default_cutting_mode: Mapped[str] = mapped_column(
        String(20),
        default="一开一",
        nullable=False,
    )
    snapshot_component_production_process: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )
    snapshot_component_production_notes: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )
    snapshot_component_report_length_mm: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )
    snapshot_component_report_width_mm: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )
    snapshot_component_crease_type: Mapped[str | None] = mapped_column(
        String(20),
        nullable=True,
    )
    snapshot_component_crease_left_mm: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )
    snapshot_component_crease_middle_mm: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )
    snapshot_component_crease_right_mm: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )
    snapshot_component_report_notes: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )
    snapshot_component_base_report_length_mm: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )
    snapshot_component_base_report_width_mm: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )
    snapshot_component_base_crease_type: Mapped[str | None] = mapped_column(
        String(20),
        nullable=True,
    )
    snapshot_component_base_crease_left_mm: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )
    snapshot_component_base_crease_middle_mm: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )
    snapshot_component_base_crease_right_mm: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )
    snapshot_component_base_report_notes: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )
    snapshot_component_splice_mode: Mapped[str | None] = mapped_column(
        String(20),
        nullable=True,
    )
    snapshot_component_pieces_per_box: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )
    snapshot_component_flap_mm: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.current_timestamp(),
        nullable=False,
    )

    sales_order_item: Mapped["OrderItem"] = relationship()
    product_bom_component: Mapped["ProductBomComponent | None"] = relationship()
    component_product: Mapped["Product"] = relationship()


class RequisitionItemBomSource(Base):
    __tablename__ = "requisition_item_bom_sources"
    __table_args__ = (
        CheckConstraint(
            "order_set_quantity > 0",
            name="ck_requisition_item_bom_sources_order_set_quantity",
        ),
        CheckConstraint(
            "quantity_per_set > 0 AND quantity_per_set = round(quantity_per_set, 0)",
            name="ck_requisition_item_bom_sources_quantity_per_set",
        ),
        CheckConstraint(
            "required_piece_quantity > 0",
            name="ck_requisition_item_bom_sources_required_piece_quantity",
        ),
        CheckConstraint(
            "((demand_basis = 'order_sets' "
            "AND required_piece_quantity = order_set_quantity * quantity_per_set) "
            "OR demand_basis = 'order_specific_pieces')",
            name="ck_requisition_item_bom_sources_required_piece_formula",
        ),
        CheckConstraint(
            "demand_basis IN ('order_sets','order_specific_pieces')",
            name="ck_requisition_item_bom_sources_demand_basis",
        ),
        CheckConstraint(
            "mold_max_yield_per_sheet IS NULL OR mold_max_yield_per_sheet > 0",
            name="ck_requisition_item_bom_sources_mold_yield",
        ),
        CheckConstraint(
            "actual_yield_per_sheet IS NULL OR actual_yield_per_sheet > 0",
            name="ck_requisition_item_bom_sources_actual_yield",
        ),
        CheckConstraint(
            "actual_yield_per_sheet IS NULL OR mold_max_yield_per_sheet IS NOT NULL",
            name="ck_requisition_item_bom_sources_actual_yield_has_maximum",
        ),
        CheckConstraint(
            "actual_yield_per_sheet IS NULL OR "
            "actual_yield_per_sheet <= mold_max_yield_per_sheet",
            name="ck_requisition_item_bom_sources_actual_yield_within_maximum",
        ),
        CheckConstraint(
            "spare_sheet_quantity >= 0",
            name="ck_requisition_item_bom_sources_spare_sheets",
        ),
        CheckConstraint(
            "calculated_purchase_quantity >= 0",
            name="ck_requisition_item_bom_sources_calculated_purchase_quantity",
        ),
        CheckConstraint(
            "calculated_purchase_quantity >= spare_sheet_quantity",
            name="ck_requisition_item_bom_sources_purchase_covers_spares",
        ),
        CheckConstraint(
            "direction_note IS NULL OR length(trim(direction_note)) > 0",
            name="ck_requisition_item_bom_sources_direction_note",
        ),
        CheckConstraint(
            "component_type IN ('whole','cover','base')",
            name="ck_requisition_item_bom_sources_component_type",
        ),
        CheckConstraint(
            "active_guard IS NULL OR active_guard = 1",
            name="ck_requisition_item_bom_sources_active_guard",
        ),
        UniqueConstraint(
            "requisition_item_id",
            "sales_order_item_bom_component_id",
            "component_type",
            name="uq_requisition_item_bom_sources_item_snapshot",
        ),
        Index(
            "ix_requisition_item_bom_sources_requisition_item_id",
            "requisition_item_id",
        ),
        Index(
            "ix_requisition_item_bom_sources_snapshot_id",
            "sales_order_item_bom_component_id",
        ),
        Index(
            "ix_requisition_item_bom_sources_snapshot_component",
            "sales_order_item_bom_component_id",
            "component_type",
        ),
        Index(
            "uq_requisition_item_bom_sources_active_physical_source",
            "sales_order_item_bom_component_id",
            "component_type",
            unique=True,
            sqlite_where=text("active_guard = 1"),
            postgresql_where=text("active_guard = 1"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    requisition_item_id: Mapped[int] = mapped_column(
        ForeignKey("material_requisition_items.id", ondelete="CASCADE"),
        nullable=False,
    )
    sales_order_item_bom_component_id: Mapped[int] = mapped_column(
        ForeignKey("sales_order_item_bom_components.id", ondelete="RESTRICT"),
        nullable=False,
    )
    component_type: Mapped[str] = mapped_column(
        String(20),
        default="whole",
        nullable=False,
    )
    active_guard: Mapped[int | None] = mapped_column(
        Integer,
        default=1,
        server_default="1",
        nullable=True,
    )
    order_set_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    quantity_per_set: Mapped[Decimal] = mapped_column(Numeric(14, 4), nullable=False)
    required_piece_quantity: Mapped[Decimal] = mapped_column(
        Numeric(14, 4),
        nullable=False,
    )
    demand_basis: Mapped[str] = mapped_column(
        String(30),
        default="order_sets",
        nullable=False,
    )
    mold_max_yield_per_sheet: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )
    actual_yield_per_sheet: Mapped[Decimal | None] = mapped_column(
        Numeric(14, 4),
        nullable=True,
    )
    spare_sheet_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    calculated_purchase_quantity: Mapped[Decimal] = mapped_column(
        Numeric(14, 4),
        nullable=False,
    )
    direction_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    calculation_rule_version: Mapped[str] = mapped_column(
        String(80),
        default="n039-v1",
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.current_timestamp(),
        nullable=False,
    )

    requisition_item: Mapped["RequisitionItem"] = relationship()
    sales_order_item_bom_component: Mapped["SalesOrderItemBomComponent"] = (
        relationship()
    )


class SalesOrderItemBomDemandAdjustment(Base):
    """Append-only quantity adjustment for one immutable BOM component snapshot."""

    __tablename__ = "sales_order_item_bom_demand_adjustments"
    __table_args__ = (
        CheckConstraint(
            "length(trim(event_type)) > 0",
            name="ck_sales_order_item_bom_demand_adjustments_event_type",
        ),
        CheckConstraint(
            "delta_order_set_quantity <> 0 OR delta_required_piece_quantity <> 0",
            name="ck_sales_order_item_bom_demand_adjustments_not_noop",
        ),
        CheckConstraint(
            "length(trim(reason)) > 0",
            name="ck_sales_order_item_bom_demand_adjustments_reason",
        ),
        UniqueConstraint(
            "idempotency_key",
            name="uq_sales_order_item_bom_demand_adjustments_idempotency",
        ),
        Index(
            "ix_sales_order_item_bom_demand_adjustments_snapshot_created",
            "sales_order_item_bom_component_id",
            "created_at",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    sales_order_item_bom_component_id: Mapped[int] = mapped_column(
        ForeignKey("sales_order_item_bom_components.id", ondelete="CASCADE"),
        nullable=False,
    )
    event_type: Mapped[str] = mapped_column(String(60), nullable=False)
    delta_order_set_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    delta_required_piece_quantity: Mapped[Decimal] = mapped_column(
        Numeric(14, 4),
        nullable=False,
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    actor_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    idempotency_key: Mapped[str] = mapped_column(String(120), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.current_timestamp(),
        nullable=False,
    )

    sales_order_item_bom_component: Mapped["SalesOrderItemBomComponent"] = (
        relationship()
    )


class BomComponentDirectDeliveryAllocation(Base):
    """Tracks direct-kit completion consumption and cancellation reversal per delivery."""

    __tablename__ = "bom_component_direct_delivery_allocations"
    __table_args__ = (
        CheckConstraint(
            "consumed_quantity > 0",
            name="ck_bom_component_direct_delivery_allocations_consumed_quantity",
        ),
        CheckConstraint(
            "reversed_quantity >= 0 AND reversed_quantity <= consumed_quantity",
            name="ck_bom_component_direct_delivery_allocations_reversed_quantity",
        ),
        CheckConstraint(
            "status IN ('active', 'partial', 'reversed')",
            name="ck_bom_component_direct_delivery_allocations_status",
        ),
        CheckConstraint(
            "((status = 'active' AND reversed_quantity = 0) OR "
            "(status = 'partial' AND reversed_quantity > 0 "
            "AND reversed_quantity < consumed_quantity) OR "
            "(status = 'reversed' AND reversed_quantity = consumed_quantity))",
            name="ck_bom_component_direct_delivery_allocations_status_quantity",
        ),
        UniqueConstraint(
            "delivery_item_id",
            "production_completion_id",
            name="uq_bom_component_direct_delivery_allocations_delivery_completion",
        ),
        Index(
            "ix_bom_component_direct_delivery_allocations_snapshot_status",
            "sales_order_item_bom_component_id",
            "status",
        ),
        Index(
            "ix_bom_component_direct_delivery_allocations_completion_status",
            "production_completion_id",
            "status",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    delivery_item_id: Mapped[int] = mapped_column(
        ForeignKey("sales_delivery_items.id", ondelete="RESTRICT"),
        nullable=False,
    )
    production_completion_id: Mapped[int] = mapped_column(
        ForeignKey("production_completions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    sales_order_item_bom_component_id: Mapped[int] = mapped_column(
        ForeignKey("sales_order_item_bom_components.id", ondelete="RESTRICT"),
        nullable=False,
    )
    consumed_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    reversed_quantity: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(20),
        default="active",
        nullable=False,
    )
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    reversed_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.current_timestamp(),
        nullable=False,
    )
    reversed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    sales_order_item_bom_component: Mapped["SalesOrderItemBomComponent"] = (
        relationship()
    )
