"""P1-81 immutable purchase-price and receipt-purpose facts."""

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


class PurchaseReceiptMaterialVariance(Base):
    """Immutable request explaining an actual-material deviation."""

    __tablename__ = "purchase_receipt_material_variances"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_purchase_material_variances_idem"),
        CheckConstraint(
            "((supplier_requisition_order_item_id IS NOT NULL AND "
            "material_requisition_item_id IS NULL) OR "
            "(supplier_requisition_order_item_id IS NULL AND "
            "material_requisition_item_id IS NOT NULL))",
            name="ck_purchase_material_variances_one_source",
        ),
        CheckConstraint(
            "actual_material_code_snapshot <> expected_material_code_snapshot "
            "AND expected_source_version >= 1 AND purpose_snapshot_version >= 1",
            name="ck_purchase_material_variances_changed_material",
        ),
        CheckConstraint(
            "length(trim(source_key)) > 0 AND length(trim(reason)) > 0 "
            "AND length(receipt_plan_fingerprint) = 64 "
            "AND length(actual_material_fingerprint) = 64 "
            "AND length(request_hash) = 64",
            name="ck_purchase_material_variances_frozen_text",
        ),
        Index(
            "ix_purchase_material_variances_source",
            "supplier_requisition_order_item_id",
            "material_requisition_item_id",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    supplier_requisition_order_item_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("supplier_requisition_order_items.id", ondelete="RESTRICT"),
        nullable=True,
    )
    material_requisition_item_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("material_requisition_items.id", ondelete="RESTRICT"),
        nullable=True,
    )
    purchase_purpose_source_snapshot_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("purchase_purpose_source_snapshots.id", ondelete="RESTRICT"),
        nullable=False,
    )
    source_key: Mapped[str] = mapped_column(String(160), nullable=False)
    expected_source_version: Mapped[int] = mapped_column(Integer, nullable=False)
    purpose_snapshot_version: Mapped[int] = mapped_column(Integer, nullable=False)
    receipt_plan_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    expected_material_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("materials.id", ondelete="RESTRICT"), nullable=True
    )
    expected_material_code_snapshot: Mapped[str] = mapped_column(String(200), nullable=False)
    actual_material_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("materials.id", ondelete="RESTRICT"), nullable=False
    )
    actual_material_code_snapshot: Mapped[str] = mapped_column(String(200), nullable=False)
    actual_material_version: Mapped[int] = mapped_column(Integer, nullable=False)
    actual_material_layer_count_snapshot: Mapped[int | None] = mapped_column(Integer, nullable=True)
    actual_material_flute_type_snapshot: Mapped[str | None] = mapped_column(String(50), nullable=True)
    actual_material_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(120), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    requested_by: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    requested_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )


class PurchaseReceiptMaterialVarianceApproval(Base):
    """Independent immutable confirmation for one material variance."""

    __tablename__ = "purchase_receipt_material_variance_approvals"
    __table_args__ = (
        UniqueConstraint("material_variance_id", name="uq_purchase_material_variance_approval"),
        UniqueConstraint("idempotency_key", name="uq_purchase_material_variance_approval_idem"),
        CheckConstraint(
            "length(trim(idempotency_key)) > 0 AND length(request_hash) = 64",
            name="ck_purchase_material_variance_approval_frozen",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    material_variance_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("purchase_receipt_material_variances.id", ondelete="RESTRICT"),
        nullable=False,
    )
    idempotency_key: Mapped[str] = mapped_column(String(120), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    confirmed_by: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    confirmed_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )


class PurchaseReceiptFact(Base):
    """One immutable, versioned actual-material and final-price fact."""

    __tablename__ = "purchase_receipt_facts"
    __table_args__ = (
        UniqueConstraint(
            "idempotency_key",
            name="uq_purchase_receipt_facts_idempotency_key",
        ),
        UniqueConstraint(
            "supplier_requisition_order_item_id",
            "purchase_purpose_source_snapshot_id",
            "receipt_fact_version",
            name="uq_purchase_receipt_facts_supplier_snapshot_version",
        ),
        UniqueConstraint(
            "material_requisition_item_id",
            "purchase_purpose_source_snapshot_id",
            "receipt_fact_version",
            name="uq_purchase_receipt_facts_requisition_snapshot_version",
        ),
        CheckConstraint(
            "((supplier_requisition_order_item_id IS NOT NULL "
            "AND material_requisition_item_id IS NULL) OR "
            "(supplier_requisition_order_item_id IS NULL "
            "AND material_requisition_item_id IS NOT NULL))",
            name="ck_purchase_receipt_facts_exactly_one_source",
        ),
        CheckConstraint(
            "receipt_fact_version >= 1 AND expected_source_version >= 1 "
            "AND purpose_snapshot_version >= 1",
            name="ck_purchase_receipt_facts_versions",
        ),
        CheckConstraint(
            "unit_price > 0",
            name="ck_purchase_receipt_facts_unit_price",
        ),
        CheckConstraint(
            "price_unit IN ('per_sheet','per_square_meter')",
            name="ck_purchase_receipt_facts_price_unit",
        ),
        CheckConstraint(
            "tax_rate >= 0 AND tax_rate <= 1",
            name="ck_purchase_receipt_facts_tax_rate",
        ),
        CheckConstraint(
            "((actual_material_code_snapshot = expected_material_code_snapshot "
            "AND NOT material_change_confirmed "
            "AND material_variance_approval_id IS NULL) OR "
            "(actual_material_code_snapshot <> expected_material_code_snapshot "
            "AND material_change_confirmed "
            "AND material_variance_approval_id IS NOT NULL))",
            name="ck_purchase_receipt_facts_material_change_confirmation",
        ),
        CheckConstraint(
            "length(trim(source_key)) > 0 "
            "AND length(trim(actual_material_code_snapshot)) > 0 "
            "AND length(trim(expected_material_code_snapshot)) > 0 "
            "AND length(trim(currency)) = 3 "
            "AND length(trim(idempotency_key)) > 0 "
            "AND length(receipt_plan_fingerprint) = 64 "
            "AND actual_material_version >= 1 "
            "AND length(actual_material_fingerprint) = 64 "
            "AND length(request_hash) = 64",
            name="ck_purchase_receipt_facts_frozen_text",
        ),
        Index(
            "ix_purchase_receipt_facts_supplier_item",
            "supplier_requisition_order_item_id",
            "purchase_purpose_source_snapshot_id",
            "receipt_fact_version",
        ),
        Index(
            "ix_purchase_receipt_facts_requisition_item",
            "material_requisition_item_id",
            "purchase_purpose_source_snapshot_id",
            "receipt_fact_version",
        ),
        Index(
            "ix_purchase_receipt_facts_purpose_snapshot",
            "purchase_purpose_source_snapshot_id",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    supplier_requisition_order_item_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey(
            "supplier_requisition_order_items.id",
            ondelete="RESTRICT",
            name="fk_purchase_receipt_facts_supplier_item",
        ),
        nullable=True,
    )
    material_requisition_item_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey(
            "material_requisition_items.id",
            ondelete="RESTRICT",
            name="fk_purchase_receipt_facts_requisition_item",
        ),
        nullable=True,
    )
    purchase_purpose_source_snapshot_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey(
            "purchase_purpose_source_snapshots.id",
            ondelete="RESTRICT",
            name="fk_purchase_receipt_facts_purpose_snapshot",
        ),
        nullable=False,
    )
    source_key: Mapped[str] = mapped_column(String(160), nullable=False)
    receipt_fact_version: Mapped[int] = mapped_column(Integer, nullable=False)
    expected_source_version: Mapped[int] = mapped_column(Integer, nullable=False)
    purpose_snapshot_version: Mapped[int] = mapped_column(Integer, nullable=False)
    receipt_plan_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    actual_material_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey(
            "materials.id",
            ondelete="RESTRICT",
            name="fk_purchase_receipt_facts_actual_material",
        ),
        nullable=False,
    )
    actual_material_code_snapshot: Mapped[str] = mapped_column(
        String(200), nullable=False
    )
    actual_material_version: Mapped[int] = mapped_column(Integer, nullable=False)
    actual_material_layer_count_snapshot: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    actual_material_flute_type_snapshot: Mapped[str | None] = mapped_column(
        String(50), nullable=True
    )
    actual_material_is_active_snapshot: Mapped[bool] = mapped_column(
        Boolean, nullable=False
    )
    actual_material_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    expected_material_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey(
            "materials.id",
            ondelete="RESTRICT",
            name="fk_purchase_receipt_facts_expected_material",
        ),
        nullable=True,
    )
    expected_material_code_snapshot: Mapped[str] = mapped_column(
        String(200), nullable=False
    )
    material_change_confirmed: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0", nullable=False
    )
    material_variance_approval_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey(
            "purchase_receipt_material_variance_approvals.id",
            ondelete="RESTRICT",
            name="fk_purchase_receipt_facts_material_variance_approval",
        ),
        nullable=True,
    )
    unit_price: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    price_unit: Mapped[str] = mapped_column(String(30), nullable=False)
    tax_included: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="1", nullable=False
    )
    tax_rate: Mapped[Decimal] = mapped_column(Numeric(8, 6), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(120), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by: Mapped[int] = mapped_column(
        Integer,
        ForeignKey(
            "users.id",
            ondelete="RESTRICT",
            name="fk_purchase_receipt_facts_created_by_users",
        ),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )


class IncomingReceiptPurposeAllocation(Base):
    """Immutable distribution and downstream ledger for one receipt item."""

    __tablename__ = "incoming_receipt_purpose_allocations"
    __table_args__ = (
        UniqueConstraint(
            "incoming_receipt_item_id",
            name="uq_receipt_purpose_allocations_receipt_item",
        ),
        UniqueConstraint(
            "production_completion_id",
            name="uq_receipt_purpose_allocations_completion",
        ),
        UniqueConstraint(
            "finished_inventory_lot_id",
            name="uq_receipt_purpose_allocations_finished_lot",
        ),
        UniqueConstraint(
            "semi_finished_inventory_lot_id",
            name="uq_receipt_purpose_allocations_semi_lot",
        ),
        UniqueConstraint(
            "initial_semi_inventory_movement_id",
            name="uq_receipt_purpose_allocations_semi_movement",
        ),
        CheckConstraint(
            "purpose_contract_status_snapshot IN ('legacy_unset','frozen')",
            name="ck_receipt_purpose_allocations_contract_status",
        ),
        CheckConstraint(
            "((purpose_contract_status_snapshot = 'frozen' "
            "AND purchase_purpose_source_snapshot_id IS NOT NULL "
            "AND purchase_receipt_fact_id IS NOT NULL "
            "AND order_purpose_plan_sheet_qty_snapshot IS NOT NULL "
            "AND reserve_purpose_plan_sheet_qty_snapshot IS NOT NULL) OR "
            "(purpose_contract_status_snapshot = 'legacy_unset' "
            "AND purchase_purpose_source_snapshot_id IS NULL "
            "AND purchase_receipt_fact_id IS NULL "
            "AND order_purpose_plan_sheet_qty_snapshot IS NULL "
            "AND reserve_purpose_plan_sheet_qty_snapshot IS NULL))",
            name="ck_receipt_purpose_allocations_contract_links",
        ),
        CheckConstraint(
            "((supplier_requisition_order_item_id IS NOT NULL "
            "AND material_requisition_item_id IS NULL) OR "
            "(supplier_requisition_order_item_id IS NULL "
            "AND material_requisition_item_id IS NOT NULL))",
            name="ck_receipt_purpose_allocations_exactly_one_source",
        ),
        CheckConstraint(
            "source_kind IN ('order_item','requisition_item','bom_component',"
            "'direct_supplier_item')",
            name="ck_receipt_purpose_allocations_source_kind",
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
            name="ck_receipt_purpose_allocations_source_identity",
        ),
        CheckConstraint(
            "component_type IN ('whole','cover','base')",
            name="ck_receipt_purpose_allocations_component_type",
        ),
        CheckConstraint(
            "receipt_total_sheet_qty > 0 "
            "AND receipt_order_purpose_sheet_qty >= 0 "
            "AND receipt_reserve_purpose_sheet_qty >= 0 "
            "AND receipt_order_purpose_sheet_qty + receipt_reserve_purpose_sheet_qty "
            "= receipt_total_sheet_qty",
            name="ck_receipt_purpose_allocations_receipt_balance",
        ),
        CheckConstraint(
            "cumulative_total_sheet_qty_before >= 0 "
            "AND cumulative_order_purpose_sheet_qty_before >= 0 "
            "AND cumulative_reserve_purpose_sheet_qty_before >= 0 "
            "AND cumulative_order_purpose_sheet_qty_before + "
            "cumulative_reserve_purpose_sheet_qty_before = "
            "cumulative_total_sheet_qty_before "
            "AND cumulative_total_sheet_qty_after = "
            "cumulative_total_sheet_qty_before + receipt_total_sheet_qty "
            "AND cumulative_order_purpose_sheet_qty_after = "
            "cumulative_order_purpose_sheet_qty_before + "
            "receipt_order_purpose_sheet_qty "
            "AND cumulative_reserve_purpose_sheet_qty_after = "
            "cumulative_reserve_purpose_sheet_qty_before + "
            "receipt_reserve_purpose_sheet_qty "
            "AND cumulative_order_purpose_sheet_qty_after + "
            "cumulative_reserve_purpose_sheet_qty_after = "
            "cumulative_total_sheet_qty_after",
            name="ck_receipt_purpose_allocations_cumulative_balance",
        ),
        CheckConstraint(
            "purpose_contract_status_snapshot <> 'frozen' OR "
            "(order_purpose_plan_sheet_qty_snapshot >= 0 "
            "AND reserve_purpose_plan_sheet_qty_snapshot >= 0 "
            "AND ((reserve_purpose_plan_sheet_qty_snapshot > 0 "
            "AND cumulative_order_purpose_sheet_qty_after "
            "<= order_purpose_plan_sheet_qty_snapshot) OR "
            "(reserve_purpose_plan_sheet_qty_snapshot = 0 "
            "AND receipt_reserve_purpose_sheet_qty = 0 "
            "AND cumulative_reserve_purpose_sheet_qty_before = 0 "
            "AND cumulative_reserve_purpose_sheet_qty_after = 0)))",
            name="ck_receipt_purpose_allocations_frozen_purpose_policy",
        ),
        CheckConstraint(
            "finished_output_qty_before >= 0 "
            "AND finished_output_qty_after >= finished_output_qty_before "
            "AND finished_output_qty_delta = "
            "finished_output_qty_after - finished_output_qty_before",
            name="ck_receipt_purpose_allocations_finished_balance",
        ),
        CheckConstraint(
            "((finished_output_qty_delta = 0 "
            "AND production_completion_id IS NULL "
            "AND finished_inventory_lot_id IS NULL) OR "
            "(finished_output_qty_delta > 0 "
            "AND production_completion_id IS NOT NULL "
            "AND finished_inventory_lot_id IS NOT NULL))",
            name="ck_receipt_purpose_allocations_finished_links",
        ),
        CheckConstraint(
            "((receipt_reserve_purpose_sheet_qty = 0 "
            "AND semi_finished_inventory_lot_id IS NULL "
            "AND initial_semi_inventory_movement_id IS NULL) OR "
            "(receipt_reserve_purpose_sheet_qty > 0 "
            "AND semi_finished_inventory_lot_id IS NOT NULL "
            "AND initial_semi_inventory_movement_id IS NOT NULL))",
            name="ck_receipt_purpose_allocations_semi_links",
        ),
        CheckConstraint(
            "((purpose_contract_status_snapshot = 'frozen' "
            "AND sheet_cost IS NOT NULL AND sheet_cost >= 0 "
            "AND order_purpose_cost IS NOT NULL AND order_purpose_cost >= 0 "
            "AND reserve_purpose_cost IS NOT NULL AND reserve_purpose_cost >= 0 "
            "AND total_cost IS NOT NULL AND total_cost >= 0 "
            "AND capitalized_cost IS NOT NULL AND capitalized_cost >= 0 "
            "AND order_purpose_cost + reserve_purpose_cost = total_cost "
            "AND (finished_output_qty_delta > 0 OR capitalized_cost = 0)) OR "
            "(purpose_contract_status_snapshot = 'legacy_unset' "
            "AND sheet_cost IS NULL AND order_purpose_cost IS NULL "
            "AND reserve_purpose_cost IS NULL AND total_cost IS NULL "
            "AND capitalized_cost IS NULL))",
            name="ck_receipt_purpose_allocations_cost_balance",
        ),
        CheckConstraint(
            "status = 'posted' AND version >= 1 "
            "AND length(trim(source_key)) > 0 "
            "AND length(trim(customer_name_snapshot)) > 0 "
            "AND length(request_hash) = 64",
            name="ck_receipt_purpose_allocations_frozen_fact",
        ),
        Index(
            "ix_receipt_purpose_allocations_formal_source",
            "supplier_requisition_order_item_id",
            "material_requisition_item_id",
        ),
        Index(
            "ix_receipt_purpose_allocations_customer_source",
            "customer_id",
            "source_key",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    incoming_receipt_item_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey(
            "incoming_receipt_items.id",
            ondelete="RESTRICT",
            name="fk_receipt_purpose_allocations_receipt_item",
        ),
        nullable=False,
    )
    purpose_contract_status_snapshot: Mapped[str] = mapped_column(
        String(20), nullable=False
    )
    purchase_purpose_source_snapshot_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey(
            "purchase_purpose_source_snapshots.id",
            ondelete="RESTRICT",
            name="fk_receipt_purpose_allocations_purpose_snapshot",
        ),
        nullable=True,
    )
    purchase_receipt_fact_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey(
            "purchase_receipt_facts.id",
            ondelete="RESTRICT",
            name="fk_receipt_purpose_allocations_receipt_fact",
        ),
        nullable=True,
    )
    supplier_requisition_order_item_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey(
            "supplier_requisition_order_items.id",
            ondelete="RESTRICT",
            name="fk_receipt_purpose_allocations_supplier_item",
        ),
        nullable=True,
    )
    material_requisition_item_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey(
            "material_requisition_items.id",
            ondelete="RESTRICT",
            name="fk_receipt_purpose_allocations_requisition_item",
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
            name="fk_receipt_purpose_allocations_order_item_source",
        ),
        nullable=True,
    )
    source_requisition_item_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey(
            "material_requisition_items.id",
            ondelete="RESTRICT",
            name="fk_receipt_purpose_allocations_requisition_item_source",
        ),
        nullable=True,
    )
    source_bom_requisition_source_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey(
            "requisition_item_bom_sources.id",
            ondelete="RESTRICT",
            name="fk_receipt_purpose_allocations_bom_source",
        ),
        nullable=True,
    )
    customer_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey(
            "customers.id",
            ondelete="RESTRICT",
            name="fk_receipt_purpose_allocations_customer",
        ),
        nullable=False,
    )
    customer_name_snapshot: Mapped[str] = mapped_column(String(200), nullable=False)
    component_type: Mapped[str] = mapped_column(String(20), nullable=False)
    order_purpose_plan_sheet_qty_snapshot: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    reserve_purpose_plan_sheet_qty_snapshot: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    receipt_total_sheet_qty: Mapped[int] = mapped_column(Integer, nullable=False)
    receipt_order_purpose_sheet_qty: Mapped[int] = mapped_column(
        Integer, nullable=False
    )
    receipt_reserve_purpose_sheet_qty: Mapped[int] = mapped_column(
        Integer, nullable=False
    )
    cumulative_total_sheet_qty_before: Mapped[int] = mapped_column(
        Integer, nullable=False
    )
    cumulative_total_sheet_qty_after: Mapped[int] = mapped_column(
        Integer, nullable=False
    )
    cumulative_order_purpose_sheet_qty_before: Mapped[int] = mapped_column(
        Integer, nullable=False
    )
    cumulative_order_purpose_sheet_qty_after: Mapped[int] = mapped_column(
        Integer, nullable=False
    )
    cumulative_reserve_purpose_sheet_qty_before: Mapped[int] = mapped_column(
        Integer, nullable=False
    )
    cumulative_reserve_purpose_sheet_qty_after: Mapped[int] = mapped_column(
        Integer, nullable=False
    )
    finished_output_qty_before: Mapped[int] = mapped_column(Integer, nullable=False)
    finished_output_qty_after: Mapped[int] = mapped_column(Integer, nullable=False)
    finished_output_qty_delta: Mapped[int] = mapped_column(Integer, nullable=False)
    sheet_cost: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)
    order_purpose_cost: Mapped[Decimal | None] = mapped_column(
        Numeric(20, 6), nullable=True
    )
    reserve_purpose_cost: Mapped[Decimal | None] = mapped_column(
        Numeric(20, 6), nullable=True
    )
    total_cost: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)
    capitalized_cost: Mapped[Decimal | None] = mapped_column(
        Numeric(20, 6), nullable=True
    )
    production_completion_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey(
            "production_completions.id",
            ondelete="RESTRICT",
            name="fk_receipt_purpose_allocations_completion",
        ),
        nullable=True,
    )
    finished_inventory_lot_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey(
            "inventory_lots.id",
            ondelete="RESTRICT",
            name="fk_receipt_purpose_allocations_finished_lot",
        ),
        nullable=True,
    )
    semi_finished_inventory_lot_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey(
            "inventory_lots.id",
            ondelete="RESTRICT",
            name="fk_receipt_purpose_allocations_semi_lot",
        ),
        nullable=True,
    )
    initial_semi_inventory_movement_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey(
            "inventory_movements.id",
            ondelete="RESTRICT",
            name="fk_receipt_purpose_allocations_semi_movement",
        ),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(
        String(20), default="posted", server_default="posted", nullable=False
    )
    version: Mapped[int] = mapped_column(
        Integer, default=1, server_default="1", nullable=False
    )
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by: Mapped[int] = mapped_column(
        Integer,
        ForeignKey(
            "users.id",
            ondelete="RESTRICT",
            name="fk_receipt_purpose_allocations_created_by_users",
        ),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )

    incoming_receipt_item: Mapped["IncomingReceiptItem"] = relationship(
        "IncomingReceiptItem", back_populates="purpose_allocation"
    )


class IncomingReceiptPurposeReversal(Base):
    """Immutable compensation fact; the source allocation itself stays posted."""

    __tablename__ = "incoming_receipt_purpose_reversals"
    __table_args__ = (
        UniqueConstraint(
            "incoming_receipt_purpose_allocation_id",
            name="uq_receipt_purpose_reversals_allocation",
        ),
        UniqueConstraint(
            "incoming_receipt_item_id",
            name="uq_receipt_purpose_reversals_receipt_item",
        ),
        UniqueConstraint(
            "compensation_inventory_movement_id",
            name="uq_receipt_purpose_reversals_compensation_movement",
        ),
        CheckConstraint(
            "cumulative_total_sheet_qty_before > "
            "cumulative_total_sheet_qty_after "
            "AND cumulative_order_purpose_sheet_qty_before >= "
            "cumulative_order_purpose_sheet_qty_after "
            "AND cumulative_reserve_purpose_sheet_qty_before >= "
            "cumulative_reserve_purpose_sheet_qty_after "
            "AND cumulative_total_sheet_qty_after >= 0 "
            "AND cumulative_order_purpose_sheet_qty_after >= 0 "
            "AND cumulative_reserve_purpose_sheet_qty_after >= 0 "
            "AND cumulative_order_purpose_sheet_qty_before + "
            "cumulative_reserve_purpose_sheet_qty_before = "
            "cumulative_total_sheet_qty_before "
            "AND cumulative_order_purpose_sheet_qty_after + "
            "cumulative_reserve_purpose_sheet_qty_after = "
            "cumulative_total_sheet_qty_after",
            name="ck_receipt_purpose_reversals_cumulative_balance",
        ),
        CheckConstraint(
            "length(request_hash) = 64",
            name="ck_receipt_purpose_reversals_request_hash",
        ),
        Index(
            "ix_receipt_purpose_reversals_receipt_item",
            "incoming_receipt_item_id",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    incoming_receipt_purpose_allocation_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey(
            "incoming_receipt_purpose_allocations.id",
            ondelete="RESTRICT",
            name="fk_receipt_purpose_reversals_allocation",
        ),
        nullable=False,
    )
    incoming_receipt_item_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey(
            "incoming_receipt_items.id",
            ondelete="RESTRICT",
            name="fk_receipt_purpose_reversals_receipt_item",
        ),
        nullable=False,
    )
    reversed_production_completion_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey(
            "production_completions.id",
            ondelete="RESTRICT",
            name="fk_receipt_purpose_reversals_completion",
        ),
        nullable=True,
    )
    reversed_finished_inventory_lot_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey(
            "inventory_lots.id",
            ondelete="RESTRICT",
            name="fk_receipt_purpose_reversals_finished_lot",
        ),
        nullable=True,
    )
    reversed_semi_finished_inventory_lot_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey(
            "inventory_lots.id",
            ondelete="RESTRICT",
            name="fk_receipt_purpose_reversals_semi_lot",
        ),
        nullable=True,
    )
    compensation_inventory_movement_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey(
            "inventory_movements.id",
            ondelete="RESTRICT",
            name="fk_receipt_purpose_reversals_compensation_movement",
        ),
        nullable=True,
    )
    cumulative_total_sheet_qty_before: Mapped[int] = mapped_column(
        Integer, nullable=False
    )
    cumulative_total_sheet_qty_after: Mapped[int] = mapped_column(
        Integer, nullable=False
    )
    cumulative_order_purpose_sheet_qty_before: Mapped[int] = mapped_column(
        Integer, nullable=False
    )
    cumulative_order_purpose_sheet_qty_after: Mapped[int] = mapped_column(
        Integer, nullable=False
    )
    cumulative_reserve_purpose_sheet_qty_before: Mapped[int] = mapped_column(
        Integer, nullable=False
    )
    cumulative_reserve_purpose_sheet_qty_after: Mapped[int] = mapped_column(
        Integer, nullable=False
    )
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    reversed_by: Mapped[int] = mapped_column(
        Integer,
        ForeignKey(
            "users.id",
            ondelete="RESTRICT",
            name="fk_receipt_purpose_reversals_reversed_by_users",
        ),
        nullable=False,
    )
    reversed_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )


class IncomingReceiptReversalFact(Base):
    """Immutable idempotency envelope retaining every formal reversal response."""

    __tablename__ = "incoming_receipt_reversal_facts"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_incoming_reversal_facts_idem"),
        UniqueConstraint("target_kind", "target_id", name="uq_incoming_reversal_facts_target"),
        UniqueConstraint("incoming_receipt_item_id", name="uq_incoming_reversal_facts_receipt_item"),
        UniqueConstraint("incoming_receipt_purpose_reversal_id", name="uq_incoming_reversal_facts_purpose_reversal"),
        CheckConstraint(
            "target_kind IN ('receipt_item','order_item','requisition_item') "
            "AND target_id > 0 AND length(trim(idempotency_key)) > 0 "
            "AND length(request_hash) = 64 AND length(trim(response_json)) > 0",
            name="ck_incoming_reversal_facts_frozen",
        ),
        CheckConstraint(
            "((target_kind = 'receipt_item' AND incoming_receipt_item_id = target_id) OR "
            "(target_kind IN ('order_item','requisition_item') "
            "AND incoming_receipt_item_id IS NULL "
            "AND incoming_receipt_purpose_reversal_id IS NULL))",
            name="ck_incoming_reversal_facts_target_links",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    target_kind: Mapped[str] = mapped_column(String(30), nullable=False)
    target_id: Mapped[int] = mapped_column(Integer, nullable=False)
    incoming_receipt_item_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("incoming_receipt_items.id", ondelete="RESTRICT"),
        nullable=True,
    )
    incoming_receipt_purpose_reversal_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("incoming_receipt_purpose_reversals.id", ondelete="RESTRICT"),
        nullable=True,
    )
    idempotency_key: Mapped[str] = mapped_column(String(120), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    response_json: Mapped[str] = mapped_column(Text, nullable=False)
    reversed_by: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    reversed_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )


class IncomingReceiptBatchFact(Base):
    """Immutable outer idempotency result for one formal batch receive."""

    __tablename__ = "incoming_receipt_batch_facts"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_incoming_receipt_batch_facts_idem"),
        CheckConstraint(
            "length(trim(idempotency_key)) > 0 AND length(request_hash) = 64 "
            "AND length(scope_hash) = 64 AND length(trim(response_json)) > 0",
            name="ck_incoming_receipt_batch_facts_frozen",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    idempotency_key: Mapped[str] = mapped_column(String(120), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    scope_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    response_json: Mapped[str] = mapped_column(Text, nullable=False)
    received_by: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )
