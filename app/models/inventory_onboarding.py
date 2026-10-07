from __future__ import annotations
from app.models.dimension_type import SheetDimensionColumn

from datetime import date, datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models import Base

if TYPE_CHECKING:
    from app.models.customer import Customer
    from app.models.material import Material
    from app.models.product import Product
    from app.models.user import User
    from app.models.warehouse_inventory import (
        InventoryLot,
        InventoryPallet,
        WarehouseLocation,
    )


class InventoryOnboardingBatch(Base):
    """Private-file draft for first-time inventory onboarding.

    B1 deliberately stops at an immutable submitted snapshot.  Formal stock
    creation belongs to a later, separately audited B2 application ledger.
    """

    __tablename__ = "inventory_onboarding_batches"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft','submitted')",
            name="ck_inventory_onboarding_batches_status",
        ),
        CheckConstraint(
            "version >= 1",
            name="ck_inventory_onboarding_batches_version",
        ),
        CheckConstraint(
            "source_size > 0",
            name="ck_inventory_onboarding_batches_source_size",
        ),
        CheckConstraint(
            "length(source_file_sha256) = 64",
            name="ck_inventory_onboarding_batches_source_sha256",
        ),
        CheckConstraint(
            "source_format IN ('csv','xlsx')",
            name="ck_inventory_onboarding_batches_source_format",
        ),
        CheckConstraint(
            "(source_format = 'xlsx' AND source_encoding IS NULL) OR "
            "(source_format = 'csv' AND "
            "source_encoding IN ('utf-8','utf-8-sig','gb18030'))",
            name="ck_inventory_onboarding_batches_source_encoding",
        ),
        CheckConstraint(
            "(dry_run_fingerprint IS NULL AND dry_run_summary_json IS NULL "
            "AND dry_run_by IS NULL AND dry_run_at IS NULL) OR "
            "(dry_run_fingerprint IS NOT NULL "
            "AND length(dry_run_fingerprint) = 64 "
            "AND dry_run_summary_json IS NOT NULL "
            "AND dry_run_by IS NOT NULL AND dry_run_at IS NOT NULL)",
            name="ck_inventory_onboarding_batches_dry_run_state",
        ),
        CheckConstraint(
            "(status = 'draft' AND submit_idempotency_key IS NULL "
            "AND submitted_by IS NULL AND submitted_at IS NULL) OR "
            "(status = 'submitted' AND submit_idempotency_key IS NOT NULL "
            "AND submitted_by IS NOT NULL AND submitted_at IS NOT NULL "
            "AND dry_run_fingerprint IS NOT NULL)",
            name="ck_inventory_onboarding_batches_submit_state",
        ),
        UniqueConstraint(
            "batch_number",
            name="uq_inventory_onboarding_batches_number",
        ),
        UniqueConstraint(
            "source_file_reference",
            name="uq_inventory_onboarding_batches_source_reference",
        ),
        UniqueConstraint(
            "source_file_sha256",
            name="uq_inventory_onboarding_batches_source_sha256",
        ),
        UniqueConstraint(
            "submit_idempotency_key",
            name="uq_inventory_onboarding_batches_submit_idempotency",
        ),
        Index(
            "ix_inventory_onboarding_batches_status_created",
            "status",
            "created_at",
        ),
        Index(
            "ix_inventory_onboarding_batches_area_status",
            "resolved_area_code",
            "status",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    batch_number: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), default="draft", server_default="draft", nullable=False
    )
    version: Mapped[int] = mapped_column(
        Integer, default=1, server_default="1", nullable=False
    )

    source_file_reference: Mapped[str] = mapped_column(Text, nullable=False)
    source_file_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    source_content_type: Mapped[str] = mapped_column(String(150), nullable=False)
    source_size: Mapped[int] = mapped_column(Integer, nullable=False)
    source_format: Mapped[str] = mapped_column(String(10), nullable=False)
    source_encoding: Mapped[str | None] = mapped_column(String(20), nullable=True)

    resolved_floor: Mapped[int | None] = mapped_column(Integer, nullable=True)
    resolved_area_code: Mapped[str | None] = mapped_column(String(30), nullable=True)

    dry_run_fingerprint: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    dry_run_summary_json: Mapped[dict[str, object] | None] = mapped_column(
        JSON(none_as_null=True), nullable=True
    )
    dry_run_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    dry_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    submit_idempotency_key: Mapped[str | None] = mapped_column(
        String(120), nullable=True
    )
    submitted_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    created_by: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )
    updated_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime, onupdate=func.current_timestamp(), nullable=True
    )

    lines: Mapped[list["InventoryOnboardingLine"]] = relationship(
        back_populates="batch",
        cascade="all, delete-orphan",
        order_by="InventoryOnboardingLine.source_sheet_name, "
        "InventoryOnboardingLine.source_row_number",
    )


class InventoryOnboardingLine(Base):
    """One preserved source row plus its editable, resolved B1 interpretation."""

    __tablename__ = "inventory_onboarding_lines"
    __table_args__ = (
        CheckConstraint(
            "version >= 1",
            name="ck_inventory_onboarding_lines_version",
        ),
        CheckConstraint(
            "source_row_number > 0",
            name="ck_inventory_onboarding_lines_source_row",
        ),
        CheckConstraint(
            "length(source_row_hash) = 64",
            name="ck_inventory_onboarding_lines_source_hash",
        ),
        CheckConstraint(
            "inventory_type IS NULL OR "
            "inventory_type IN ('finished','semi_finished')",
            name="ck_inventory_onboarding_lines_inventory_type",
        ),
        CheckConstraint(
            "ownership_type IS NULL OR "
            "ownership_type IN ('customer_specific','general')",
            name="ck_inventory_onboarding_lines_ownership",
        ),
        CheckConstraint(
            "quantity IS NULL OR quantity >= 0",
            name="ck_inventory_onboarding_lines_quantity",
        ),
        CheckConstraint(
            "unit IS NULL OR unit IN ('boxes','sheets')",
            name="ck_inventory_onboarding_lines_unit",
        ),
        CheckConstraint(
            "inventory_type IS NULL OR unit IS NULL OR "
            "(inventory_type = 'finished' AND unit = 'boxes') OR "
            "(inventory_type = 'semi_finished' AND unit = 'sheets')",
            name="ck_inventory_onboarding_lines_type_unit",
        ),
        CheckConstraint(
            "source_type = 'stocktake'",
            name="ck_inventory_onboarding_lines_source_type",
        ),
        CheckConstraint(
            "stock_date_accuracy IS NULL OR "
            "stock_date_accuracy IN ('exact','estimated','unknown')",
            name="ck_inventory_onboarding_lines_date_accuracy",
        ),
        CheckConstraint(
            "layer_count IS NULL OR layer_count IN (3,5,7)",
            name="ck_inventory_onboarding_lines_layer_count",
        ),
        CheckConstraint(
            "board_length_mm IS NULL OR board_length_mm > 0",
            name="ck_inventory_onboarding_lines_board_length",
        ),
        CheckConstraint(
            "board_width_mm IS NULL OR board_width_mm > 0",
            name="ck_inventory_onboarding_lines_board_width",
        ),
        CheckConstraint(
            "sheet_type IS NULL OR "
            "sheet_type IN ('raw_board','net_sheet','creased_sheet')",
            name="ck_inventory_onboarding_lines_sheet_type",
        ),
        CheckConstraint(
            "component_type IS NULL OR "
            "component_type IN ('whole','cover','base')",
            name="ck_inventory_onboarding_lines_component",
        ),
        CheckConstraint(
            "pieces_per_box IS NULL OR pieces_per_box > 0",
            name="ck_inventory_onboarding_lines_pieces_per_box",
        ),
        CheckConstraint(
            "stock_yield_per_sheet IS NULL OR stock_yield_per_sheet > 0",
            name="ck_inventory_onboarding_lines_stock_yield",
        ),
        CheckConstraint(
            "action_decision IN ("
            "'pending','create_new','route_n035','route_semi_adjust',"
            "'route_snapshot_conversion','exclude')",
            name="ck_inventory_onboarding_lines_action",
        ),
        CheckConstraint(
            "match_status IN ('pending','ready','blocked','routed','excluded')",
            name="ck_inventory_onboarding_lines_match_status",
        ),
        CheckConstraint(
            "(action_decision NOT IN ('route_n035','route_semi_adjust') "
            "OR existing_lot_id IS NOT NULL) AND "
            "(action_decision != 'route_snapshot_conversion' "
            "OR existing_pallet_id IS NOT NULL)",
            name="ck_inventory_onboarding_lines_route_target",
        ),
        UniqueConstraint(
            "batch_id",
            "source_sheet_name",
            "source_row_number",
            name="uq_inventory_onboarding_lines_source_row",
        ),
        Index(
            "ix_inventory_onboarding_lines_batch_match",
            "batch_id",
            "match_status",
        ),
        Index(
            "ix_inventory_onboarding_lines_batch_action",
            "batch_id",
            "action_decision",
        ),
        Index(
            "ix_inventory_onboarding_lines_location",
            "location_id",
        ),
        Index(
            "ix_inventory_onboarding_lines_customer_product",
            "customer_id",
            "product_id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    batch_id: Mapped[int] = mapped_column(
        ForeignKey("inventory_onboarding_batches.id", ondelete="CASCADE"),
        nullable=False,
    )

    # These source facts are immutable even while the interpretation is draft.
    source_sheet_name: Mapped[str] = mapped_column(String(100), nullable=False)
    source_row_number: Mapped[int] = mapped_column(Integer, nullable=False)
    source_row_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    raw_row_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    original_values_json: Mapped[dict[str, object]] = mapped_column(
        JSON, nullable=False
    )

    stocktake_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    stocktaker_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    inventory_type: Mapped[str | None] = mapped_column(String(30), nullable=True)
    ownership_type: Mapped[str | None] = mapped_column(String(30), nullable=True)

    location_id: Mapped[int | None] = mapped_column(
        ForeignKey("warehouse_locations.id", ondelete="RESTRICT"), nullable=True
    )
    location_code_snapshot: Mapped[str | None] = mapped_column(
        String(50), nullable=True
    )
    floor_snapshot: Mapped[int | None] = mapped_column(Integer, nullable=True)
    warehouse_name_snapshot: Mapped[str | None] = mapped_column(
        String(100), nullable=True
    )
    area_code_snapshot: Mapped[str | None] = mapped_column(
        String(30), nullable=True
    )
    pallet_code: Mapped[str | None] = mapped_column(String(100), nullable=True)

    customer_id: Mapped[int | None] = mapped_column(
        ForeignKey("customers.id", ondelete="RESTRICT"), nullable=True
    )
    customer_code_snapshot: Mapped[str | None] = mapped_column(
        String(100), nullable=True
    )
    customer_name_snapshot: Mapped[str | None] = mapped_column(
        String(200), nullable=True
    )
    product_id: Mapped[int | None] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"), nullable=True
    )
    inventory_code_snapshot: Mapped[str | None] = mapped_column(
        String(150), nullable=True
    )
    product_name_snapshot: Mapped[str | None] = mapped_column(
        String(250), nullable=True
    )
    material_id: Mapped[int | None] = mapped_column(
        ForeignKey("materials.id", ondelete="RESTRICT"), nullable=True
    )
    material_code_snapshot: Mapped[str | None] = mapped_column(
        String(100), nullable=True
    )

    quantity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    unit: Mapped[str | None] = mapped_column(String(20), nullable=True)
    source_type: Mapped[str] = mapped_column(
        String(30), default="stocktake", server_default="stocktake", nullable=False
    )
    stock_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    stock_date_accuracy: Mapped[str | None] = mapped_column(
        String(20), nullable=True
    )
    stock_date_original_text: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )

    supplier_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    layer_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    flute_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    board_length_mm: Mapped[int | float | None] = mapped_column(SheetDimensionColumn, nullable=True)
    board_width_mm: Mapped[int | float | None] = mapped_column(SheetDimensionColumn, nullable=True)
    sheet_type: Mapped[str | None] = mapped_column(String(30), nullable=True)
    component_type: Mapped[str | None] = mapped_column(
        String(20), nullable=True
    )
    pieces_per_box: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stock_yield_per_sheet: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    crease_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    crease_left_mm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    crease_middle_mm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    crease_right_mm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cutting_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    remarks: Mapped[str | None] = mapped_column(Text, nullable=True)

    action_decision: Mapped[str] = mapped_column(
        String(40), default="pending", server_default="pending", nullable=False
    )
    existing_lot_id: Mapped[int | None] = mapped_column(
        ForeignKey("inventory_lots.id", ondelete="RESTRICT"), nullable=True
    )
    existing_pallet_id: Mapped[int | None] = mapped_column(
        ForeignKey("inventory_pallets.id", ondelete="RESTRICT"), nullable=True
    )
    match_status: Mapped[str] = mapped_column(
        String(20), default="pending", server_default="pending", nullable=False
    )
    error_codes_json: Mapped[list[str]] = mapped_column(
        JSON, default=list, server_default=text("'[]'"), nullable=False
    )
    warning_codes_json: Mapped[list[str]] = mapped_column(
        JSON, default=list, server_default=text("'[]'"), nullable=False
    )
    match_evidence_json: Mapped[dict[str, object]] = mapped_column(
        JSON, default=dict, server_default=text("'{}'"), nullable=False
    )
    version: Mapped[int] = mapped_column(
        Integer, default=1, server_default="1", nullable=False
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

    batch: Mapped["InventoryOnboardingBatch"] = relationship(
        back_populates="lines"
    )
    location: Mapped["WarehouseLocation | None"] = relationship()
    customer: Mapped["Customer | None"] = relationship()
    product: Mapped["Product | None"] = relationship()
    material: Mapped["Material | None"] = relationship()
    existing_lot: Mapped["InventoryLot | None"] = relationship()
    existing_pallet: Mapped["InventoryPallet | None"] = relationship()
