from __future__ import annotations

from datetime import datetime
from decimal import Decimal

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
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models import Base


class CompositePhysicalPurchaseGroup(Base):
    """One frozen physical purchase line shared by several composite sources."""

    __tablename__ = "composite_physical_purchase_groups"
    __table_args__ = (
        CheckConstraint(
            "sheet_type_snapshot IN ('raw_board','net_sheet','creased_sheet')",
            name="ck_cppg_sheet_type",
        ),
        CheckConstraint(
            "material_version_snapshot >= 1 AND layer_count_snapshot > 0 "
            "AND report_length_mm > 0 AND report_width_mm > 0 "
            "AND yield_per_sheet > 0",
            name="ck_cppg_physical_numbers",
        ),
        CheckConstraint(
            "length(trim(group_key)) > 0 "
            "AND length(trim(material_code_snapshot)) > 0 "
            "AND length(trim(flute_type_snapshot)) > 0 "
            "AND length(trim(cutting_mode_snapshot)) > 0 "
            "AND length(physical_snapshot_hash) = 64 "
            "AND length(trim(idempotency_key)) > 0 "
            "AND length(request_hash) = 64",
            name="ck_cppg_frozen_text",
        ),
        CheckConstraint(
            "(crease_left_mm IS NULL OR crease_left_mm >= 0) "
            "AND (crease_middle_mm IS NULL OR crease_middle_mm >= 0) "
            "AND (crease_right_mm IS NULL OR crease_right_mm >= 0)",
            name="ck_cppg_crease_dimensions",
        ),
        CheckConstraint(
            "((is_die_cut_snapshot IS TRUE "
            "AND mold_max_yield_per_sheet_snapshot IS NOT NULL "
            "AND mold_max_yield_per_sheet_snapshot > 0 "
            "AND yield_per_sheet <= mold_max_yield_per_sheet_snapshot) OR "
            "(is_die_cut_snapshot IS FALSE "
            "AND (mold_max_yield_per_sheet_snapshot IS NULL "
            "OR mold_max_yield_per_sheet_snapshot > 0)))",
            name="ck_cppg_die_cut_yield",
        ),
        CheckConstraint(
            "source_count > 0 AND total_required_piece_quantity > 0 "
            "AND total_inventory_reserved_piece_quantity >= 0 "
            "AND total_inventory_reserved_piece_quantity "
            "<= total_required_piece_quantity "
            "AND net_required_piece_quantity = total_required_piece_quantity "
            "- total_inventory_reserved_piece_quantity",
            name="ck_cppg_piece_balance",
        ),
        CheckConstraint(
            "spare_sheet_quantity >= 0 "
            "AND order_purpose_sheet_quantity >= spare_sheet_quantity "
            "AND ((net_required_piece_quantity = 0 "
            "AND order_purpose_sheet_quantity = spare_sheet_quantity) OR "
            "(net_required_piece_quantity > 0 "
            "AND order_purpose_sheet_quantity > spare_sheet_quantity "
            "AND (order_purpose_sheet_quantity - spare_sheet_quantity) "
            "* yield_per_sheet >= net_required_piece_quantity "
            "AND (order_purpose_sheet_quantity - spare_sheet_quantity - 1) "
            "* yield_per_sheet < net_required_piece_quantity))",
            name="ck_cppg_single_rounding",
        ),
        CheckConstraint(
            "reserve_sheet_quantity >= 0 "
            "AND purchase_sheet_quantity = order_purpose_sheet_quantity "
            "+ reserve_sheet_quantity",
            name="ck_cppg_purchase_balance",
        ),
        CheckConstraint(
            "cutting_remainder_piece_quantity = "
            "(order_purpose_sheet_quantity - spare_sheet_quantity) "
            "* yield_per_sheet - net_required_piece_quantity "
            "AND cutting_remainder_piece_quantity >= 0 "
            "AND cutting_remainder_piece_quantity < yield_per_sheet",
            name="ck_cppg_cutting_remainder",
        ),
        CheckConstraint(
            "status IN ('active','partially_received','received','reversed','voided')",
            name="ck_cppg_status",
        ),
        CheckConstraint("version >= 1", name="ck_cppg_version"),
        UniqueConstraint("group_key", name="uq_cppg_group_key"),
        UniqueConstraint("idempotency_key", name="uq_cppg_idempotency"),
        UniqueConstraint(
            "supplier_requisition_order_item_id",
            name="uq_cppg_supplier_item",
        ),
        UniqueConstraint(
            "requisition_id",
            "physical_snapshot_hash",
            name="uq_cppg_requisition_physical",
        ),
        Index("ix_cppg_requisition_status", "requisition_id", "status"),
        Index(
            "ix_cppg_customer_component_status",
            "customer_id",
            "component_product_id",
            "status",
        ),
        Index("ix_cppg_physical_hash", "physical_snapshot_hash"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    group_key: Mapped[str] = mapped_column(String(160), nullable=False)
    requisition_id: Mapped[int] = mapped_column(
        ForeignKey("material_requisitions.id", ondelete="RESTRICT"), nullable=False
    )
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False
    )
    component_product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"), nullable=False
    )
    supplier_requisition_order_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("supplier_requisition_order_items.id", ondelete="SET NULL"),
        nullable=True,
    )
    material_id: Mapped[int] = mapped_column(
        ForeignKey("materials.id", ondelete="RESTRICT"), nullable=False
    )
    material_version_snapshot: Mapped[int] = mapped_column(Integer, nullable=False)
    material_code_snapshot: Mapped[str] = mapped_column(String(200), nullable=False)
    layer_count_snapshot: Mapped[int] = mapped_column(Integer, nullable=False)
    flute_type_snapshot: Mapped[str] = mapped_column(String(50), nullable=False)
    report_length_mm: Mapped[int] = mapped_column(Integer, nullable=False)
    report_width_mm: Mapped[int] = mapped_column(Integer, nullable=False)
    crease_type_snapshot: Mapped[str | None] = mapped_column(String(20), nullable=True)
    crease_left_mm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    crease_middle_mm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    crease_right_mm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sheet_type_snapshot: Mapped[str] = mapped_column(String(30), nullable=False)
    cutting_mode_snapshot: Mapped[str] = mapped_column(String(30), nullable=False)
    yield_per_sheet: Mapped[int] = mapped_column(Integer, nullable=False)
    is_die_cut_snapshot: Mapped[bool] = mapped_column(Boolean, nullable=False)
    mold_tool_id: Mapped[int | None] = mapped_column(
        ForeignKey("mold_tools.id", ondelete="RESTRICT"), nullable=True
    )
    mold_max_yield_per_sheet_snapshot: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    source_count: Mapped[int] = mapped_column(Integer, nullable=False)
    total_required_piece_quantity: Mapped[int] = mapped_column(
        Integer, nullable=False
    )
    total_inventory_reserved_piece_quantity: Mapped[int] = mapped_column(
        Integer, nullable=False
    )
    net_required_piece_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    spare_sheet_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    order_purpose_sheet_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    reserve_sheet_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    purchase_sheet_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    cutting_remainder_piece_quantity: Mapped[int] = mapped_column(
        Integer, nullable=False
    )
    physical_snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(30), default="active", server_default="active", nullable=False
    )
    version: Mapped[int] = mapped_column(
        Integer, default=1, server_default="1", nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )
    updated_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    sources: Mapped[list["CompositePhysicalPurchaseGroupSource"]] = relationship(
        back_populates="group",
        order_by="CompositePhysicalPurchaseGroupSource.source_sequence",
    )
    incoming_receipt_items: Mapped[list["IncomingReceiptItem"]] = relationship(
        "IncomingReceiptItem",
        back_populates="composite_physical_purchase_group",
        order_by="IncomingReceiptItem.id",
    )
    receipts: Mapped[list["CompositePhysicalGroupReceipt"]] = relationship(
        back_populates="group",
        order_by="CompositePhysicalGroupReceipt.receipt_sequence",
    )


class CompositePhysicalPurchaseGroupSource(Base):
    """Frozen, stably ordered contribution to one physical purchase group."""

    __tablename__ = "composite_physical_purchase_group_sources"
    __table_args__ = (
        CheckConstraint(
            "demand_basis IN ('order_sets','order_specific_pieces')",
            name="ck_cppgs_demand_basis",
        ),
        CheckConstraint(
            "component_type_snapshot IN ('whole','cover','base')",
            name="ck_cppgs_component_type",
        ),
        CheckConstraint(
            "parent_set_quantity > 0 AND quantity_per_set > 0 "
            "AND required_piece_quantity > 0 "
            "AND ((demand_basis = 'order_sets' "
            "AND required_piece_quantity = parent_set_quantity * quantity_per_set) "
            "OR demand_basis = 'order_specific_pieces')",
            name="ck_cppgs_required_formula",
        ),
        CheckConstraint(
            "inventory_reserved_piece_quantity >= 0 "
            "AND inventory_reserved_piece_quantity <= required_piece_quantity "
            "AND net_required_piece_quantity = required_piece_quantity "
            "- inventory_reserved_piece_quantity",
            name="ck_cppgs_piece_balance",
        ),
        CheckConstraint(
            "source_count_snapshot > 0 "
            "AND source_sequence >= 1 "
            "AND source_sequence <= source_count_snapshot "
            "AND ((is_last_source IS TRUE "
            "AND source_sequence = source_count_snapshot) OR "
            "(is_last_source IS FALSE "
            "AND source_sequence < source_count_snapshot))",
            name="ck_cppgs_sequence",
        ),
        CheckConstraint(
            "spare_sheet_quantity >= 0 "
            "AND allocated_order_purpose_sheet_quantity >= spare_sheet_quantity "
            "AND cumulative_allocated_sheet_quantity_before >= 0 "
            "AND cumulative_allocated_sheet_quantity_after = "
            "cumulative_allocated_sheet_quantity_before "
            "+ allocated_order_purpose_sheet_quantity "
            "AND cumulative_allocated_sheet_quantity_after "
            "<= group_order_purpose_sheet_quantity_snapshot "
            "AND group_order_purpose_sheet_quantity_snapshot >= 0",
            name="ck_cppgs_sheet_balance",
        ),
        CheckConstraint(
            "(source_sequence <> 1 "
            "OR cumulative_allocated_sheet_quantity_before = 0) "
            "AND (is_last_source IS FALSE "
            "OR cumulative_allocated_sheet_quantity_after "
            "= group_order_purpose_sheet_quantity_snapshot)",
            name="ck_cppgs_sheet_boundaries",
        ),
        CheckConstraint(
            "length(trim(source_key)) > 0 AND length(source_fingerprint) = 64",
            name="ck_cppgs_frozen_text",
        ),
        UniqueConstraint(
            "composite_physical_purchase_group_id",
            "source_key",
            name="uq_cppgs_group_source_key",
        ),
        UniqueConstraint(
            "requisition_item_bom_source_id", name="uq_cppgs_bom_source"
        ),
        UniqueConstraint(
            "purchase_purpose_source_snapshot_id",
            name="uq_cppgs_purpose_snapshot",
        ),
        UniqueConstraint(
            "composite_physical_purchase_group_id",
            "source_sequence",
            name="uq_cppgs_group_sequence",
        ),
        Index("ix_cppgs_group", "composite_physical_purchase_group_id"),
        Index("ix_cppgs_requisition_item", "requisition_item_id"),
        Index(
            "ix_cppgs_order_bom",
            "order_item_id",
            "sales_order_item_bom_component_id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    composite_physical_purchase_group_id: Mapped[int] = mapped_column(
        ForeignKey(
            "composite_physical_purchase_groups.id", ondelete="RESTRICT"
        ),
        nullable=False,
    )
    source_key: Mapped[str] = mapped_column(String(160), nullable=False)
    requisition_item_id: Mapped[int] = mapped_column(
        ForeignKey("material_requisition_items.id", ondelete="RESTRICT"),
        nullable=False,
    )
    requisition_item_bom_source_id: Mapped[int] = mapped_column(
        ForeignKey("requisition_item_bom_sources.id", ondelete="RESTRICT"),
        nullable=False,
    )
    purchase_purpose_source_snapshot_id: Mapped[int | None] = mapped_column(
        ForeignKey("purchase_purpose_source_snapshots.id", ondelete="RESTRICT"),
        nullable=True,
    )
    order_item_id: Mapped[int] = mapped_column(
        ForeignKey("sales_order_items.id", ondelete="RESTRICT"), nullable=False
    )
    sales_order_item_bom_component_id: Mapped[int] = mapped_column(
        ForeignKey("sales_order_item_bom_components.id", ondelete="RESTRICT"),
        nullable=False,
    )
    source_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    source_count_snapshot: Mapped[int] = mapped_column(Integer, nullable=False)
    is_last_source: Mapped[bool] = mapped_column(Boolean, nullable=False)
    demand_basis: Mapped[str] = mapped_column(String(30), nullable=False)
    component_type_snapshot: Mapped[str] = mapped_column(
        String(20), nullable=False
    )
    parent_set_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    quantity_per_set: Mapped[int] = mapped_column(Integer, nullable=False)
    required_piece_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    inventory_reserved_piece_quantity: Mapped[int] = mapped_column(
        Integer, nullable=False
    )
    net_required_piece_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    spare_sheet_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    allocated_order_purpose_sheet_quantity: Mapped[int] = mapped_column(
        Integer, nullable=False
    )
    cumulative_allocated_sheet_quantity_before: Mapped[int] = mapped_column(
        Integer, nullable=False
    )
    cumulative_allocated_sheet_quantity_after: Mapped[int] = mapped_column(
        Integer, nullable=False
    )
    group_order_purpose_sheet_quantity_snapshot: Mapped[int] = mapped_column(
        Integer, nullable=False
    )
    source_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )

    group: Mapped[CompositePhysicalPurchaseGroup] = relationship(
        back_populates="sources"
    )
    receipt_allocations: Mapped[
        list["CompositePhysicalGroupReceiptSourceAllocation"]
    ] = relationship(
        back_populates="group_source",
        order_by="CompositePhysicalGroupReceiptSourceAllocation.id",
    )


class CompositePhysicalGroupReceipt(Base):
    """One posted partial receipt and its order/reserve output and cost split."""

    __tablename__ = "composite_physical_group_receipts"
    __table_args__ = (
        CheckConstraint(
            "receipt_sequence >= 1 AND group_version_snapshot >= 1 "
            "AND version >= 1",
            name="ck_cpgr_versions_sequence",
        ),
        CheckConstraint(
            "received_sheet_quantity > 0 "
            "AND order_purpose_received_sheet_quantity >= 0 "
            "AND reserve_received_sheet_quantity >= 0 "
            "AND received_sheet_quantity = order_purpose_received_sheet_quantity "
            "+ reserve_received_sheet_quantity",
            name="ck_cpgr_receipt_balance",
        ),
        CheckConstraint(
            "cumulative_received_sheet_quantity_before >= 0 "
            "AND cumulative_order_purpose_sheet_quantity_before >= 0 "
            "AND cumulative_reserve_sheet_quantity_before >= 0 "
            "AND cumulative_received_sheet_quantity_before = "
            "cumulative_order_purpose_sheet_quantity_before "
            "+ cumulative_reserve_sheet_quantity_before "
            "AND cumulative_order_purpose_sheet_quantity_after = "
            "cumulative_order_purpose_sheet_quantity_before "
            "+ order_purpose_received_sheet_quantity "
            "AND cumulative_reserve_sheet_quantity_after = "
            "cumulative_reserve_sheet_quantity_before "
            "+ reserve_received_sheet_quantity "
            "AND cumulative_received_sheet_quantity_after = "
            "cumulative_received_sheet_quantity_before + received_sheet_quantity "
            "AND cumulative_received_sheet_quantity_after = "
            "cumulative_order_purpose_sheet_quantity_after "
            "+ cumulative_reserve_sheet_quantity_after",
            name="ck_cpgr_cumulative_sheets",
        ),
        CheckConstraint(
            "yield_per_sheet_snapshot > 0 "
            "AND component_output_piece_quantity = "
            "order_purpose_received_sheet_quantity * yield_per_sheet_snapshot "
            "AND cumulative_component_output_piece_quantity_before >= 0 "
            "AND cumulative_component_output_piece_quantity_after = "
            "cumulative_component_output_piece_quantity_before "
            "+ component_output_piece_quantity",
            name="ck_cpgr_component_output",
        ),
        CheckConstraint(
            "((order_purpose_received_sheet_quantity > 0 "
            "AND component_output_piece_quantity > 0 "
            "AND component_inventory_lot_id IS NOT NULL "
            "AND component_unit_material_cost IS NOT NULL "
            "AND component_unit_material_cost > 0) OR "
            "(order_purpose_received_sheet_quantity = 0 "
            "AND component_output_piece_quantity = 0 "
            "AND component_inventory_lot_id IS NULL "
            "AND component_unit_material_cost IS NULL))",
            name="ck_cpgr_component_lot",
        ),
        CheckConstraint(
            "((reserve_received_sheet_quantity > 0 "
            "AND reserve_inventory_lot_id IS NOT NULL) OR "
            "(reserve_received_sheet_quantity = 0 "
            "AND reserve_inventory_lot_id IS NULL)) "
            "AND (component_inventory_lot_id IS NULL "
            "OR reserve_inventory_lot_id IS NULL "
            "OR component_inventory_lot_id <> reserve_inventory_lot_id)",
            name="ck_cpgr_reserve_lot",
        ),
        CheckConstraint(
            "actual_unit_price_per_sheet > 0 "
            "AND order_purpose_material_cost >= 0 "
            "AND reserve_material_cost >= 0 "
            "AND order_purpose_material_cost = actual_unit_price_per_sheet "
            "* order_purpose_received_sheet_quantity "
            "AND reserve_material_cost = actual_unit_price_per_sheet "
            "* reserve_received_sheet_quantity "
            "AND total_material_cost = order_purpose_material_cost "
            "+ reserve_material_cost "
            "AND total_material_cost = actual_unit_price_per_sheet "
            "* received_sheet_quantity "
            "AND ((order_purpose_received_sheet_quantity = 0 "
            "AND order_purpose_material_cost = 0) OR "
            "(order_purpose_received_sheet_quantity > 0 "
            "AND order_purpose_material_cost > 0)) "
            "AND ((reserve_received_sheet_quantity = 0 "
            "AND reserve_material_cost = 0) OR "
            "(reserve_received_sheet_quantity > 0 "
            "AND reserve_material_cost > 0))",
            name="ck_cpgr_cost_balance",
        ),
        CheckConstraint(
            "status IN ('posted','reversed')",
            name="ck_cpgr_status",
        ),
        CheckConstraint(
            "length(trim(currency_snapshot)) = 3 "
            "AND length(trim(idempotency_key)) > 0 "
            "AND length(request_hash) = 64",
            name="ck_cpgr_frozen_text",
        ),
        UniqueConstraint(
            "incoming_receipt_item_id", name="uq_cpgr_incoming_receipt_item"
        ),
        UniqueConstraint(
            "component_inventory_lot_id", name="uq_cpgr_component_lot"
        ),
        UniqueConstraint("reserve_inventory_lot_id", name="uq_cpgr_reserve_lot"),
        UniqueConstraint("idempotency_key", name="uq_cpgr_idempotency"),
        UniqueConstraint(
            "composite_physical_purchase_group_id",
            "receipt_sequence",
            name="uq_cpgr_group_sequence",
        ),
        Index(
            "ix_cpgr_group_status",
            "composite_physical_purchase_group_id",
            "status",
        ),
        Index("ix_cpgr_purchase_receipt_fact", "purchase_receipt_fact_id"),
        Index("ix_cpgr_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    composite_physical_purchase_group_id: Mapped[int] = mapped_column(
        ForeignKey(
            "composite_physical_purchase_groups.id", ondelete="RESTRICT"
        ),
        nullable=False,
    )
    incoming_receipt_item_id: Mapped[int] = mapped_column(
        ForeignKey("incoming_receipt_items.id", ondelete="RESTRICT"), nullable=False
    )
    receipt_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    purchase_receipt_fact_id: Mapped[int] = mapped_column(
        ForeignKey("purchase_receipt_facts.id", ondelete="RESTRICT"),
        nullable=False,
    )
    component_inventory_lot_id: Mapped[int | None] = mapped_column(
        ForeignKey("inventory_lots.id", ondelete="RESTRICT"), nullable=True
    )
    reserve_inventory_lot_id: Mapped[int | None] = mapped_column(
        ForeignKey("inventory_lots.id", ondelete="RESTRICT"), nullable=True
    )
    group_version_snapshot: Mapped[int] = mapped_column(Integer, nullable=False)
    received_sheet_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    order_purpose_received_sheet_quantity: Mapped[int] = mapped_column(
        Integer, nullable=False
    )
    reserve_received_sheet_quantity: Mapped[int] = mapped_column(
        Integer, nullable=False
    )
    cumulative_received_sheet_quantity_before: Mapped[int] = mapped_column(
        Integer, nullable=False
    )
    cumulative_received_sheet_quantity_after: Mapped[int] = mapped_column(
        Integer, nullable=False
    )
    cumulative_order_purpose_sheet_quantity_before: Mapped[int] = mapped_column(
        Integer, nullable=False
    )
    cumulative_order_purpose_sheet_quantity_after: Mapped[int] = mapped_column(
        Integer, nullable=False
    )
    cumulative_reserve_sheet_quantity_before: Mapped[int] = mapped_column(
        Integer, nullable=False
    )
    cumulative_reserve_sheet_quantity_after: Mapped[int] = mapped_column(
        Integer, nullable=False
    )
    yield_per_sheet_snapshot: Mapped[int] = mapped_column(Integer, nullable=False)
    component_output_piece_quantity: Mapped[int] = mapped_column(
        Integer, nullable=False
    )
    cumulative_component_output_piece_quantity_before: Mapped[int] = mapped_column(
        Integer, nullable=False
    )
    cumulative_component_output_piece_quantity_after: Mapped[int] = mapped_column(
        Integer, nullable=False
    )
    actual_unit_price_per_sheet: Mapped[Decimal] = mapped_column(
        Numeric(20, 6), nullable=False
    )
    component_unit_material_cost: Mapped[Decimal | None] = mapped_column(
        Numeric(20, 6), nullable=True
    )
    order_purpose_material_cost: Mapped[Decimal] = mapped_column(
        Numeric(20, 6), nullable=False
    )
    reserve_material_cost: Mapped[Decimal] = mapped_column(
        Numeric(20, 6), nullable=False
    )
    total_material_cost: Mapped[Decimal] = mapped_column(
        Numeric(20, 6), nullable=False
    )
    currency_snapshot: Mapped[str] = mapped_column(String(3), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), default="posted", server_default="posted", nullable=False
    )
    version: Mapped[int] = mapped_column(
        Integer, default=1, server_default="1", nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )

    group: Mapped[CompositePhysicalPurchaseGroup] = relationship(
        back_populates="receipts"
    )
    incoming_receipt_item: Mapped["IncomingReceiptItem"] = relationship(
        "IncomingReceiptItem",
        back_populates="composite_physical_group_receipt",
    )
    source_allocations: Mapped[
        list["CompositePhysicalGroupReceiptSourceAllocation"]
    ] = relationship(
        back_populates="group_receipt",
        order_by="CompositePhysicalGroupReceiptSourceAllocation.allocation_sequence",
    )
    reversal: Mapped["CompositePhysicalGroupReceiptReversal | None"] = relationship(
        back_populates="group_receipt", uselist=False
    )


class CompositePhysicalGroupReceiptSourceAllocation(Base):
    """Positive component reservation allocated to one frozen group source."""

    __tablename__ = "composite_physical_group_receipt_source_allocations"
    __table_args__ = (
        CheckConstraint(
            "allocation_sequence >= 1 "
            "AND source_net_required_piece_quantity_snapshot > 0 "
            "AND allocated_reserved_component_piece_quantity > 0",
            name="ck_cpgrsa_positive_quantities",
        ),
        CheckConstraint(
            "source_cumulative_reserved_piece_quantity_before >= 0 "
            "AND source_cumulative_reserved_piece_quantity_after = "
            "source_cumulative_reserved_piece_quantity_before "
            "+ allocated_reserved_component_piece_quantity "
            "AND source_cumulative_reserved_piece_quantity_after "
            "<= source_net_required_piece_quantity_snapshot",
            name="ck_cpgrsa_cumulative_balance",
        ),
        CheckConstraint(
            "status IN ('active','reversed') AND version >= 1",
            name="ck_cpgrsa_status_version",
        ),
        CheckConstraint(
            "((status = 'active' AND reversed_by IS NULL AND reversed_at IS NULL) "
            "OR (status = 'reversed' "
            "AND reversed_by IS NOT NULL AND reversed_at IS NOT NULL))",
            name="ck_cpgrsa_reversal_audit",
        ),
        CheckConstraint(
            "length(trim(idempotency_key)) > 0 AND length(request_hash) = 64",
            name="ck_cpgrsa_frozen_text",
        ),
        UniqueConstraint("idempotency_key", name="uq_cpgrsa_idempotency"),
        UniqueConstraint(
            "inventory_reservation_id", name="uq_cpgrsa_inventory_reservation"
        ),
        UniqueConstraint(
            "composite_physical_group_receipt_id",
            "composite_physical_purchase_group_source_id",
            name="uq_cpgrsa_receipt_source",
        ),
        UniqueConstraint(
            "composite_physical_group_receipt_id",
            "allocation_sequence",
            name="uq_cpgrsa_receipt_sequence",
        ),
        Index(
            "ix_cpgrsa_group_source",
            "composite_physical_purchase_group_source_id",
        ),
        Index(
            "ix_cpgrsa_order_bom",
            "order_item_id",
            "sales_order_item_bom_component_id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    composite_physical_group_receipt_id: Mapped[int] = mapped_column(
        ForeignKey("composite_physical_group_receipts.id", ondelete="RESTRICT"),
        nullable=False,
    )
    composite_physical_purchase_group_source_id: Mapped[int] = mapped_column(
        ForeignKey(
            "composite_physical_purchase_group_sources.id", ondelete="RESTRICT"
        ),
        nullable=False,
    )
    allocation_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    order_id: Mapped[int] = mapped_column(
        ForeignKey("sales_orders.id", ondelete="RESTRICT"), nullable=False
    )
    order_item_id: Mapped[int] = mapped_column(
        ForeignKey("sales_order_items.id", ondelete="RESTRICT"), nullable=False
    )
    sales_order_item_bom_component_id: Mapped[int] = mapped_column(
        ForeignKey("sales_order_item_bom_components.id", ondelete="RESTRICT"),
        nullable=False,
    )
    inventory_reservation_id: Mapped[int] = mapped_column(
        ForeignKey("inventory_reservations.id", ondelete="RESTRICT"), nullable=False
    )
    source_net_required_piece_quantity_snapshot: Mapped[int] = mapped_column(
        Integer, nullable=False
    )
    allocated_reserved_component_piece_quantity: Mapped[int] = mapped_column(
        Integer, nullable=False
    )
    source_cumulative_reserved_piece_quantity_before: Mapped[int] = mapped_column(
        Integer, nullable=False
    )
    source_cumulative_reserved_piece_quantity_after: Mapped[int] = mapped_column(
        Integer, nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(20), default="active", server_default="active", nullable=False
    )
    version: Mapped[int] = mapped_column(
        Integer, default=1, server_default="1", nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )
    reversed_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    reversed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    group_receipt: Mapped[CompositePhysicalGroupReceipt] = relationship(
        back_populates="source_allocations"
    )
    group_source: Mapped[CompositePhysicalPurchaseGroupSource] = relationship(
        back_populates="receipt_allocations"
    )


class CompositePhysicalGroupReceiptReversal(Base):
    """Append-only audit fact for reversing one group receipt exactly once."""

    __tablename__ = "composite_physical_group_receipt_reversals"
    __table_args__ = (
        CheckConstraint(
            "receipt_version_before >= 1 "
            "AND receipt_version_after = receipt_version_before + 1",
            name="ck_cpgrr_version_transition",
        ),
        CheckConstraint(
            "reversed_sheet_quantity > 0 "
            "AND reversed_component_output_piece_quantity >= 0 "
            "AND reversed_reserved_component_piece_quantity >= 0 "
            "AND reversed_reserved_component_piece_quantity "
            "<= reversed_component_output_piece_quantity",
            name="ck_cpgrr_quantities",
        ),
        CheckConstraint(
            "length(trim(reason)) > 0 "
            "AND length(trim(idempotency_key)) > 0 "
            "AND length(request_hash) = 64",
            name="ck_cpgrr_frozen_text",
        ),
        UniqueConstraint(
            "composite_physical_group_receipt_id", name="uq_cpgrr_receipt"
        ),
        UniqueConstraint("idempotency_key", name="uq_cpgrr_idempotency"),
        Index("ix_cpgrr_reversed_at", "reversed_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    composite_physical_group_receipt_id: Mapped[int] = mapped_column(
        ForeignKey("composite_physical_group_receipts.id", ondelete="RESTRICT"),
        nullable=False,
    )
    receipt_version_before: Mapped[int] = mapped_column(Integer, nullable=False)
    receipt_version_after: Mapped[int] = mapped_column(Integer, nullable=False)
    reversed_sheet_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    reversed_component_output_piece_quantity: Mapped[int] = mapped_column(
        Integer, nullable=False
    )
    reversed_reserved_component_piece_quantity: Mapped[int] = mapped_column(
        Integer, nullable=False
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    reversed_by: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    reversed_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )

    group_receipt: Mapped[CompositePhysicalGroupReceipt] = relationship(
        back_populates="reversal"
    )
