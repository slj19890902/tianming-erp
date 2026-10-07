"""v0.19.2-B: 供应商报料单模型"""
from __future__ import annotations
from app.models.dimension_type import SheetDimensionColumn

from datetime import date, datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Date,
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
    pass


class SupplierRequisitionOrder(Base):
    __tablename__ = "supplier_requisition_orders"
    __table_args__ = (
        CheckConstraint(
            "((request_hash IS NULL AND request_actor_id IS NULL) OR "
            "(request_key IS NOT NULL AND length(trim(request_key)) > 0 "
            "AND length(request_hash) = 64 AND request_actor_id IS NOT NULL))",
            name="ck_supplier_requisition_orders_request_fact",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_number: Mapped[str] = mapped_column(String(40), nullable=False, unique=True)
    request_key: Mapped[str | None] = mapped_column(
        String(64), nullable=True, unique=True
    )
    request_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    request_actor_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey(
            "users.id",
            ondelete="RESTRICT",
            name="fk_supplier_requisition_orders_request_actor_id_users",
        ),
        nullable=True,
    )
    supplier_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    material_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("materials.id"), nullable=True)
    layer_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    flute_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    report_length_mm: Mapped[int | float | None] = mapped_column(SheetDimensionColumn, nullable=True)
    report_width_mm: Mapped[int | float | None] = mapped_column(SheetDimensionColumn, nullable=True)
    crease_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    crease_left_mm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    crease_middle_mm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    crease_right_mm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cutting_mode: Mapped[str | None] = mapped_column(String(30), nullable=True)
    pieces_per_box: Mapped[int | None] = mapped_column(Integer, nullable=True)
    required_piece_qty: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    stock_deduction_qty: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    requisition_qty: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    remark: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="confirmed")
    created_by: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, server_default=func.now())
    voided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    items: Mapped[list["SupplierRequisitionOrderItem"]] = relationship(
        "SupplierRequisitionOrderItem",
        back_populates="supplier_order",
        cascade="all, delete-orphan",
    )


class SupplierRequisitionOrderItem(Base):
    __tablename__ = "supplier_requisition_order_items"
    __table_args__ = (
        Index(
            "ix_supplier_requisition_order_items_product_created",
            "product_id",
            "supplier_order_id",
        ),
        Index(
            "ix_supplier_requisition_order_items_material_id",
            "material_id",
        ),
        Index(
            "ix_supplier_requisition_order_items_order_status",
            "supplier_order_id",
            "status",
            "id",
        ),
        UniqueConstraint(
            "void_idempotency_key",
            name="uq_supplier_requisition_order_items_void_idempotency",
        ),
        CheckConstraint(
            "status IN ('active','voided')",
            name="ck_supplier_requisition_order_items_status",
        ),
        CheckConstraint(
            "version >= 1",
            name="ck_supplier_requisition_order_items_version",
        ),
        CheckConstraint(
            "purpose_contract_status IN ('legacy_unset','frozen')",
            name="ck_supplier_requisition_order_items_purpose_contract_status",
        ),
        CheckConstraint(
            "((status = 'active' AND voided_at IS NULL AND voided_by IS NULL "
            "AND void_idempotency_key IS NULL AND void_request_hash IS NULL) OR "
            "(status = 'voided' AND voided_at IS NOT NULL "
            "AND void_idempotency_key IS NOT NULL "
            "AND length(void_request_hash) = 64))",
            name="ck_supplier_requisition_order_items_void_fact",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    supplier_order_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("supplier_requisition_orders.id", ondelete="CASCADE"), nullable=False
    )
    order_item_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("sales_order_items.id", ondelete="SET NULL"), nullable=True
    )
    source_key: Mapped[str | None] = mapped_column(String(120), nullable=True)
    product_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("products.id", ondelete="SET NULL"), nullable=True
    )
    material_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("materials.id", ondelete="SET NULL"), nullable=True
    )
    material_code_snapshot: Mapped[str | None] = mapped_column(
        String(100), nullable=True
    )
    supplier_name_snapshot: Mapped[str | None] = mapped_column(
        String(200), nullable=True
    )
    layer_count_snapshot: Mapped[int | None] = mapped_column(Integer, nullable=True)
    flute_type_snapshot: Mapped[str | None] = mapped_column(String(50), nullable=True)
    order_number: Mapped[str | None] = mapped_column(String(40), nullable=True)
    product_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    product_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    report_length_mm: Mapped[int | float | None] = mapped_column(SheetDimensionColumn, nullable=True)
    report_width_mm: Mapped[int | float | None] = mapped_column(SheetDimensionColumn, nullable=True)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    stock_deduction_qty: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    requisition_qty: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cutting_mode: Mapped[str | None] = mapped_column(String(30), nullable=True)
    pieces_per_box: Mapped[int | None] = mapped_column(Integer, nullable=True)
    required_piece_qty: Mapped[int | None] = mapped_column(Integer, nullable=True)
    customer_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    delivery_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), default="active", server_default="active", nullable=False
    )
    version: Mapped[int] = mapped_column(
        Integer, default=1, server_default="1", nullable=False
    )
    purpose_contract_status: Mapped[str] = mapped_column(
        String(20),
        default="legacy_unset",
        server_default="legacy_unset",
        nullable=False,
    )
    voided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    voided_by: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey(
            "users.id",
            ondelete="SET NULL",
            name="fk_supplier_requisition_order_items_voided_by_users",
        ),
        nullable=True,
    )
    void_idempotency_key: Mapped[str | None] = mapped_column(
        String(120), nullable=True
    )
    void_request_hash: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )

    supplier_order: Mapped["SupplierRequisitionOrder"] = relationship(
        "SupplierRequisitionOrder", back_populates="items"
    )
    purpose_source_snapshots: Mapped[list["PurchasePurposeSourceSnapshot"]] = (
        relationship(
            "PurchasePurposeSourceSnapshot",
            back_populates="supplier_requisition_order_item",
            passive_deletes=True,
        )
    )


class PurchasePurposeSourceSnapshot(Base):
    """Immutable allocation of one purchase line to one physical source."""

    __tablename__ = "purchase_purpose_source_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "snapshot_key",
            name="uq_purchase_purpose_source_snapshots_snapshot_key",
        ),
        UniqueConstraint(
            "supplier_requisition_order_item_id",
            name="uq_purchase_purpose_source_snapshots_supplier_item",
        ),
        UniqueConstraint(
            "material_requisition_item_id",
            name="uq_purchase_purpose_source_snapshots_requisition_item",
        ),
        CheckConstraint(
            "((supplier_requisition_order_item_id IS NOT NULL "
            "AND material_requisition_item_id IS NULL) OR "
            "(supplier_requisition_order_item_id IS NULL "
            "AND material_requisition_item_id IS NOT NULL))",
            name="ck_purchase_purpose_source_snapshots_formal_item",
        ),
        CheckConstraint(
            "source_kind IN ('order_item','requisition_item','bom_component',"
            "'direct_supplier_item')",
            name="ck_purchase_purpose_source_snapshots_source_kind",
        ),
        CheckConstraint(
            "((source_kind = 'order_item' AND source_order_item_id IS NOT NULL "
            "AND source_requisition_item_id IS NULL "
            "AND source_bom_requisition_source_id IS NULL) OR "
            "(source_kind = 'requisition_item' AND source_order_item_id IS NOT NULL "
            "AND source_requisition_item_id IS NOT NULL "
            "AND source_bom_requisition_source_id IS NULL) OR "
            "(source_kind = 'bom_component' AND source_order_item_id IS NULL "
            "AND source_requisition_item_id IS NULL "
            "AND source_bom_requisition_source_id IS NOT NULL) OR "
            "(source_kind = 'direct_supplier_item' "
            "AND supplier_requisition_order_item_id IS NOT NULL "
            "AND source_order_item_id IS NULL "
            "AND source_requisition_item_id IS NULL "
            "AND source_bom_requisition_source_id IS NULL))",
            name="ck_purchase_purpose_source_snapshots_source_identity",
        ),
        CheckConstraint(
            "component_type IN ('whole','cover','base')",
            name="ck_purchase_purpose_source_snapshots_component_type",
        ),
        CheckConstraint(
            "source_finished_qty_snapshot >= 0 "
            "AND pieces_per_finished_snapshot > 0 "
            "AND source_required_piece_qty_snapshot >= 0 "
            "AND source_semi_reserved_piece_qty_snapshot >= 0 "
            "AND source_semi_reserved_piece_qty_snapshot "
            "<= source_required_piece_qty_snapshot "
            "AND source_effective_piece_qty_snapshot >= 0 "
            "AND source_effective_piece_qty_snapshot <= "
            "source_required_piece_qty_snapshot - "
            "source_semi_reserved_piece_qty_snapshot",
            name="ck_purchase_purpose_source_snapshots_source_quantities",
        ),
        CheckConstraint(
            "yield_per_sheet_snapshot > 0 "
            "AND group_effective_piece_qty_snapshot >= 0 "
            "AND group_effective_piece_qty_snapshot >= "
            "source_effective_piece_qty_snapshot "
            "AND group_authoritative_order_sheet_qty_snapshot >= 0 "
            "AND group_authoritative_order_sheet_qty_snapshot * "
            "yield_per_sheet_snapshot >= group_effective_piece_qty_snapshot",
            name="ck_purchase_purpose_source_snapshots_group_conversion",
        ),
        CheckConstraint(
            "purchase_sheet_qty >= 0 AND order_purpose_sheet_qty >= 0 "
            "AND reserve_purpose_sheet_qty >= 0 "
            "AND order_purpose_sheet_qty + reserve_purpose_sheet_qty = "
            "purchase_sheet_qty "
            "AND order_purpose_sheet_qty <= "
            "group_authoritative_order_sheet_qty_snapshot",
            name="ck_purchase_purpose_source_snapshots_purpose_balance",
        ),
        CheckConstraint(
            "snapshot_version >= 1",
            name="ck_purchase_purpose_source_snapshots_version",
        ),
        CheckConstraint(
            "length(trim(snapshot_key)) > 0 "
            "AND length(trim(allocation_group_key)) > 0 "
            "AND length(trim(source_key)) > 0 "
            "AND length(trim(customer_name_snapshot)) > 0 "
            "AND length(trim(calculation_rule_version)) > 0 "
            "AND length(preview_fingerprint) = 64 "
            "AND length(request_hash) = 64",
            name="ck_purchase_purpose_source_snapshots_frozen_text",
        ),
        Index(
            "ix_purchase_purpose_source_snapshots_supplier_item",
            "supplier_requisition_order_item_id",
        ),
        Index(
            "ix_purchase_purpose_source_snapshots_requisition_item",
            "material_requisition_item_id",
        ),
        Index(
            "ix_purchase_purpose_source_snapshots_customer_group",
            "customer_id",
            "allocation_group_key",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    snapshot_key: Mapped[str] = mapped_column(String(160), nullable=False)
    allocation_group_key: Mapped[str] = mapped_column(String(160), nullable=False)
    supplier_requisition_order_item_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey(
            "supplier_requisition_order_items.id",
            ondelete="RESTRICT",
            name="fk_purchase_purpose_snapshots_supplier_item",
        ),
        nullable=True,
    )
    material_requisition_item_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey(
            "material_requisition_items.id",
            ondelete="RESTRICT",
            name="fk_purchase_purpose_snapshots_requisition_item",
        ),
        nullable=True,
    )
    source_kind: Mapped[str] = mapped_column(String(30), nullable=False)
    source_key: Mapped[str] = mapped_column(String(160), nullable=False)
    source_order_item_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey(
            "sales_order_items.id",
            ondelete="RESTRICT",
            name="fk_purchase_purpose_snapshots_order_item_source",
        ),
        nullable=True,
    )
    source_requisition_item_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey(
            "material_requisition_items.id",
            ondelete="RESTRICT",
            name="fk_purchase_purpose_snapshots_requisition_item_source",
        ),
        nullable=True,
    )
    source_bom_requisition_source_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey(
            "requisition_item_bom_sources.id",
            ondelete="RESTRICT",
            name="fk_purchase_purpose_snapshots_bom_source",
        ),
        nullable=True,
    )
    customer_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey(
            "customers.id",
            ondelete="RESTRICT",
            name="fk_purchase_purpose_snapshots_customer",
        ),
        nullable=False,
    )
    customer_name_snapshot: Mapped[str] = mapped_column(String(200), nullable=False)
    component_type: Mapped[str] = mapped_column(
        String(20), default="whole", server_default="whole", nullable=False
    )
    source_finished_qty_snapshot: Mapped[int] = mapped_column(Integer, nullable=False)
    pieces_per_finished_snapshot: Mapped[int] = mapped_column(Integer, nullable=False)
    source_required_piece_qty_snapshot: Mapped[int] = mapped_column(Integer, nullable=False)
    source_semi_reserved_piece_qty_snapshot: Mapped[int] = mapped_column(
        Integer, nullable=False
    )
    source_effective_piece_qty_snapshot: Mapped[int] = mapped_column(
        Integer, nullable=False
    )
    yield_per_sheet_snapshot: Mapped[int] = mapped_column(Integer, nullable=False)
    group_effective_piece_qty_snapshot: Mapped[int] = mapped_column(
        Integer, nullable=False
    )
    group_authoritative_order_sheet_qty_snapshot: Mapped[int] = mapped_column(
        Integer, nullable=False
    )
    purchase_sheet_qty: Mapped[int] = mapped_column(Integer, nullable=False)
    order_purpose_sheet_qty: Mapped[int] = mapped_column(Integer, nullable=False)
    reserve_purpose_sheet_qty: Mapped[int] = mapped_column(Integer, nullable=False)
    calculation_rule_version: Mapped[str] = mapped_column(String(40), nullable=False)
    snapshot_version: Mapped[int] = mapped_column(
        Integer, default=1, server_default="1", nullable=False
    )
    preview_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by: Mapped[int] = mapped_column(
        Integer,
        ForeignKey(
            "users.id",
            ondelete="RESTRICT",
            name="fk_purchase_purpose_snapshots_created_by_users",
        ),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )

    supplier_requisition_order_item: Mapped[
        "SupplierRequisitionOrderItem | None"
    ] = relationship(
        "SupplierRequisitionOrderItem",
        back_populates="purpose_source_snapshots",
    )
