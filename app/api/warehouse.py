from __future__ import annotations

import base64
from datetime import date, datetime, timedelta
from decimal import Decimal
import hashlib
from io import BytesIO
import json
import re
import sqlite3
from threading import Lock
from collections.abc import Mapping
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
import qrcode
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import String, cast, and_, case, func, inspect, or_, select, text, update
from sqlalchemy.exc import IntegrityError, OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session, selectinload, object_session
from app.services.warehouse_goods import goods_profile
from app.services.mobile_qr import (
    location_mobile_url,
    lot_mobile_url,
    mobile_absolute_url,
    mold_mobile_url,
    qr_data_url,
    rack_mobile_url,
)

from app.api.deps import (
    PermissionChecker,
    RoleChecker,
    customer_scope_ids,
    get_db,
    get_current_user,
    has_permission,
    has_unrestricted_customer_access,
    require_customer_access,
)
from app.core.time_contract import (
    beijing_date_bounds_utc_naive,
    beijing_naive_to_api,
    beijing_now_naive,
    beijing_today,
    utc_naive_to_api,
)
from app.api.master_data_common import audit_master_change, clean_code
from app.models.user import User
from app.models.audit import OperationLog
from app.models.customer import Customer
from app.models.customer_finished_storage_preference import (
    CustomerFinishedStoragePreference,
)
from app.models.delivery import Delivery, DeliveryPickTask
from app.models.mold_tool import (
    MoldLabelLayoutRevision,
    MoldLabelPrintJob,
    MoldLabelPrintJobItem,
    MoldLocationMovement,
    MoldMasterMutation,
    MoldRepairEvent,
    MoldScanEvent,
    MoldTool,
    MoldToolCustomer,
)
from app.models.printing_plate import (
    PrintingPlate,
    PrintingPlateLocationMovement,
    PrintingPlateResinReuse,
)
from app.models.product import Product
from app.models.receipt_putaway import ProductStoragePreference
from app.models.product_bom import SalesOrderItemBomComponent
from app.models.product_bom import RequisitionItemBomSource
from app.models.requisition import Requisition, RequisitionItem
from app.models.order import Order, OrderItem
from app.models.incoming_receipt import IncomingReceiptItem
from app.models.incoming_receipt import IncomingReceipt
from app.models.inventory_onboarding import (
    InventoryOnboardingBatch,
    InventoryOnboardingLine,
)
from app.models.master_data_object_version import MasterDataObjectVersion
from app.models.production import ProductionTask
from app.models.stock_replenishment import (
    InventoryStockPolicy,
    StockReplenishmentOrder,
    StockReplenishmentOrderItem,
)
from app.models.stocktake import StocktakeOrder
from app.models.supplier_requisition_order import (
    SupplierRequisitionOrder,
    SupplierRequisitionOrderItem,
)
from app.models.warehouse_capacity import WarehouseCapacityForecastPlan
from app.models.warehouse_inventory import (
    FinishedGoodsInventoryDetail,
    Floor3LocationLayout,
    InventoryLocationMovement,
    InventoryLot,
    InventoryLotTransfer,
    InventoryMovement,
    InventoryPallet,
    InventoryPalletItem,
    InventoryReservation,
    OrderedFinishedReceiptReturn,
    OrderItemSemiRequirement,
    SemiFinishedInventoryDetail,
    SemiFinishedLotAllowedProduct,
    WAREHOUSE_CAPACITY_REVIEW_STATUSES,
    WarehouseArea,
    WarehouseAreaStoragePolicy,
    WarehouseFloor,
    WarehouseGroundLayoutPlan,
    WarehouseGroundLayoutPlanRetirement,
    WarehouseGroundLayoutSlot,
    WarehouseGroundOccupancy,
    WarehouseGroundOccupancySlot,
    WarehouseGroundPlacementMutation,
    WarehouseLocation,
    WarehouseLocationAlias,
    WarehouseLocationDiscrepancy,
    WarehouseRackLevelLabelPrintJob,
    WarehouseUnmatchedInventoryObservation,
)
from app.services.floor3_locations import (
    Floor3LocationError,
    add_pallet_item,
    adjust_area_location_count,
    bind_finished_lot_to_floor3_pallet,
    clear_pallet,
    convert_snapshot_to_finished_lot,
    create_pallet,
    create_layout_slot,
    merge_pallet_remaining_goods,
    move_pallet,
    set_pallet_relocation,
    set_layout_slot_active,
    update_layout_area,
)
from app.services.warehouse_area_activation import (
    AREA_LOCATION_SOURCE_VERSION,
    WarehouseAreaActivationError,
    adjust_area_location_count as adjust_activated_area_location_count,
    area_location_management_payload,
    assert_area_not_archived,
    formal_area_location_rows,
    floor3_v11_map_binding_is_proven,
    formal_area,
    legacy_v11_name_only_change_is_safe,
    location_warehouse_type_for_inventory_types,
    policy_location_transition_blockers,
    policy_inventory_types,
    publish_floor_area_policies,
    resolve_area_location_management,
    resolve_location_management,
    set_area_location_active,
    unbound_area_location_transition_blockers,
    update_area_location_layout,
    warehouse_floor_for_code,
)
from app.services.factory_maps import FactoryMapNotFoundError, load_factory_map
from app.services.warehouse_twin_layout import (
    WarehouseTwinLayoutNotFoundError,
    load_warehouse_twin_floor,
)
from app.services.warehouse_pallet_standard import standard_pallet_contract
from app.services.warehouse_area_tombstones import (
    ArchivedWarehouseAreaTargetError,
    archived_area_identity_sets,
    archived_area_tombstones_for_floor,
    assert_warehouse_asset_location_not_archived,
    partition_archived_area_layout,
    warehouse_asset_location_floor,
)
from app.services.warehouse_floor1_candidate_planner import (
    Floor1CandidatePlanningError,
    _percent_round_trip_epsilon,
    build_floor1_formal_candidate_plan,
    confirmed_capacity_slots_for_zone,
    confirm_floor1_formal_candidate_plan,
    inspect_floor1_formal_candidate_state,
    overlay_formal_area_bindings,
    validate_capacity_layout_slots_for_zone,
)
from app.services.warehouse_twin_layout_editor import (
    WarehouseTwinLayoutEditConflictError,
    WarehouseTwinLayoutEditError,
    WarehouseTwinLayoutEditNotFoundError,
    WAREHOUSE_TWIN_LAYOUT_TRANSACTION_LOCK,
    apply_warehouse_twin_archived_area_tombstones,
    begin_warehouse_twin_no_go_removal_publish,
    begin_warehouse_twin_one_step_rack_publish,
    begin_warehouse_twin_one_step_publish,
    calibrate_floor4_freight_elevator,
    create_warehouse_twin_feature,
    create_warehouse_twin_rack,
    number_warehouse_twin_area_racks,
    delete_warehouse_twin_feature,
    delete_warehouse_twin_rack,
    delete_published_warehouse_twin_rack,
    discard_warehouse_twin_layout_draft,
    load_effective_warehouse_twin_floor_for_edit,
    load_published_warehouse_twin_floor_for_edit,
    load_warehouse_twin_layout_draft,
    publish_warehouse_twin_layout_draft,
    rebuild_stale_warehouse_twin_layout_draft,
    rebase_warehouse_twin_advanced_draft_after_one_step,
    rebase_warehouse_twin_advanced_draft_after_no_go_removal,
    rebase_warehouse_twin_advanced_rack_after_one_step,
    restore_warehouse_twin_layout_draft,
    restore_warehouse_twin_publish_state,
    snapshot_warehouse_twin_layout_draft,
    snapshot_warehouse_twin_publish_state,
    update_warehouse_twin_rack,
    update_warehouse_twin_feature_geometry,
    update_warehouse_twin_zone_geometry,
    update_warehouse_twin_zone_policy,
    validate_warehouse_twin_layout_draft,
)
from app.services.warehouse_twin_production import (
    WarehouseTwinProductionError,
    build_production_projection,
    delete_production_projection_mapping,
    list_production_projection_mappings,
    save_production_projection_mapping,
)
from app.services.asset_time_archive import (
    build_inventory_lot_detail_timeline,
    build_inventory_lot_time_archives,
    build_mold_detail_timeline,
    build_printing_plate_time_archives,
)
from app.services.production_workflow import (
    PENDING,
    list_production_task_dashboard_rows,
    list_production_tasks,
)
from app.services.order_status_policy import (
    order_item_forward_fulfillment_sql_conditions,
    persisted_order_status_label,
)
from app.services.semi_finished_inventory import (
    SemiFinishedCandidate,
    SemiFinishedLotVersion,
    active_semi_requirement_credited_quantity,
    browse_semi_finished_inventory,
    browse_semi_finished_inventory_for_product,
    confirm_semi_finished_match,
    consume_semi_finished_reservation,
    release_semi_finished_reservation,
    reserve_semi_finished_inventory,
    safe_physical_board_facts_match,
    reverse_semi_finished_consumption,
    save_order_item_semi_requirement,
    semi_finished_candidates_for_product,
    semi_finished_inventory_candidates,
)
from app.services.warehouse_inventory import (
    WarehouseInventoryError,
    active_finished_reserved_qty,
    component_effective_required_piece_qty,
    component_inventory_coverage,
    edit_semi_finished_lot_customer,
    edit_finished_lot,
    finished_inventory_candidates,
    finished_inventory_candidates_for_product,
    finished_inventory_candidates_for_bom_component,
    inventory_age_warning,
    manual_finished_in,
    manual_semi_finished_in,
    mutate_lot,
    normalize_material_code,
    release_finished_reservation,
    reserve_finished_inventory,
    reserve_finished_inventory_for_bom_component,
    replace_semi_finished_lot_allowed_products,
    semi_finished_lot_allowed_product_ids,
    transfer_staging_finished_lot,
    transfer_finished_lot_between_locations,
    void_semi_finished_lot,
)
from app.services.warehouse_ground_slots import (
    WarehouseGroundSlotError,
    active_ground_occupancy_for_location,
    active_ground_occupancy_for_pallet,
    build_ground_slot_preview,
    canonical_hash as ground_canonical_hash,
    ground_candidate_rows,
    ground_occupancy_payload,
    ground_preview_fingerprint,
    ground_slots_adjacent,
    effective_ground_slot_geometries,
    number_ground_physical_slots,
    occupancy_physical_quantity,
    published_ground_plan,
)
from app.services.warehouse_movement_batch import (
    BATCH_AUDIT_ACTION_CODE,
    WAREHOUSE_MOVEMENT_BATCH_LOCK,
    WarehouseMovementBatchError,
    WarehouseMovementBatchItem,
    execute_warehouse_movement_batch,
    load_movable_pallet,
    movement_batch_replay,
    movement_batch_request_hash,
)
from app.services.warehouse_stocktake_batch import (
    STOCKTAKE_BATCH_ACTION_CODE,
    WAREHOUSE_STOCKTAKE_BATCH_LOCK,
    WarehouseStocktakeBatchError,
    WarehouseStocktakeBatchItem,
    execute_warehouse_stocktake_batch,
    stocktake_batch_audit_details,
    stocktake_batch_replay,
    stocktake_batch_request_hash,
    stocktake_decrease_issues,
    stocktake_item_customer_id,
)
from app.services.warehouse_pallet_merge_batch import (
    PALLET_MERGE_BATCH_ACTION_CODE,
    PALLET_MERGE_BATCH_LOCK,
    PalletMergeBatchError,
    PalletMergeBatchSource,
    execute_pallet_merge_batch,
    pallet_merge_batch_replay,
    pallet_merge_batch_request_hash,
)
from app.services.inventory_insights import build_inventory_insights
from app.services.warehouse_twin_dashboard import (
    build_inventory_code_search_results,
    build_warehouse_twin_dashboard,
    inventory_search_matches,
    suppress_capacity_metrics_until_all_confirmed,
    warehouse_capacity_summary,
)
from app.services.warehouse_capacity_forecast import (
    ALLOWED_EFFECTS,
    build_warehouse_capacity_forecast,
    resolve_capacity_forecast_source,
    serialize_capacity_forecast_plan,
)
from app.services.audit_log import append_audit_event
from app.services.mold_repair import (
    MoldRepairError,
    change_mold_repair_status,
)
from app.services.mold_label_template import (
    MOLD_LABEL_TEMPLATE_40X30,
    MOLD_LABEL_TEMPLATE_80X40,
    mold_label_template_label,
)
from app.services.location_candidates import (
    claim_active_placed_location,
    claim_warehouse_floor_projection,
    has_space_ledger,
    list_operational_locations,
    load_warehouse_location_projection_contexts,
    operational_location_issue,
    operational_location_payload,
    pallet_has_physical_goods_condition,
    warehouse_location_projection,
)
from app.services.warehouse_floor_claim import (
    claim_warehouse_floor_projection_by_id,
)
from app.services.warehouse_location_address import (
    AddressChangeCommand,
    WarehouseLocationAddressError,
    build_address_change_preview,
    confirm_address_change,
    employee_area_name,
    employee_location_name,
    location_address_payload,
    location_alias_conflict,
    resolve_location_address,
)
from app.services.warehouse_rack_cells import (
    WarehouseRackCellSyncError,
    preview_legacy_rack_cell_bindings,
    sync_published_rack_cells,
)
from app.services.requisition_quantities import cutting_factor, normalize_cutting_mode
from app.services.product_specification import product_dimension_specification
from app.services.master_data_versioning import (
    apply_versioned_update,
    record_versioned_create,
)
from app.services.mold_identity import (
    MoldIdentityError,
    compose_mold_display_name,
    mold_customer_short_name,
    mold_label_display_number,
    next_available_mold_code,
    next_available_internal_mold_code,
    normalize_mold_chinese_short_name,
    normalize_mold_label_name,
)
from app.services.mold_label_content import (
    LABEL_OVERRIDE_FIELDS,
    MoldLabelContentError,
    apply_label_overrides,
    canonical_label_overrides,
    normalize_label_override_text,
    parse_label_overrides,
)
from app.services.mold_label_layout import (
    MoldLabelLayoutConflict,
    MoldLabelLayoutError,
    admin_state as mold_label_layout_admin_state,
    canonical_json as canonical_mold_label_layout_json,
    effective_layout as effective_mold_label_layout,
    layout_diff_summary as mold_label_layout_diff_summary,
    load_snapshot as load_mold_label_layout_snapshot,
    restore_default as restore_default_mold_label_layout,
    rollback_release as rollback_mold_label_layout,
    save_and_publish as save_and_publish_mold_label_layout,
)
from app.core.config import load_settings
from app.services.mold_location import (
    MOLD_ARCHIVE_AREA_CODE,
    MoldLocationError,
    MoldLocationMoveResult,
    MoldLocationPreview,
    confirm_mold_location_move,
    describe_mold_location,
    mold_rack_layout_relocation_warnings,
    mold_rack_structure,
    mold_location_feature_codes,
    one_floor_mold_location_options,
    plan_mold_rack_layout_relocations,
    preview_mold_location_move,
)
from app.services.mold_archive import (
    MoldArchiveResult,
    archive_mold_tool,
    mold_archive_candidate,
    restore_mold_tool,
)
from app.services.printing_plate_location import (
    PrintingPlateLocationError,
    PrintingPlateLocationPreview,
    PrintingPlateMoveResult,
    confirm_printing_plate_move,
    describe_printing_plate_location,
    normalize_printing_plate_location,
    preview_printing_plate_move,
)
from app.services.printing_plate_resin_reuse import (
    PrintingPlateResinReuseError,
    PrintingPlateResinReusePreview,
    PrintingPlateResinReuseResult,
    confirm_printing_plate_resin_reuse,
    preview_printing_plate_resin_reuse,
    printing_plate_binding_count,
)


router = APIRouter()
GROUND_STORAGE_TRANSACTION_LOCK = Lock()
_MOLD_CODE_WRITE_LOCK = Lock()
_MOLD_LABEL_LAYOUT_WRITE_LOCK = Lock()
# Configuration/master-data operations have no N028 permission equivalent and
# intentionally retain their legacy admin-only boundary.
admin_only = RoleChecker(["admin"])
can_read = PermissionChecker("warehouse.view")
can_operate = PermissionChecker("warehouse.execute")
can_archive = PermissionChecker("warehouse.archive")
can_read_orders = PermissionChecker("orders.view")
can_reserve = PermissionChecker("warehouse.reserve")
can_view_reservations = PermissionChecker("warehouse.view")
can_submit_stocktake = PermissionChecker("warehouse.stocktake.submit")
VALID_SOURCE_TYPES = {
    "manual",
    "production_surplus",
    "purchase_surplus",
    "stocktake",
    "transfer",
    "replenishment",
}
WAREHOUSE_CONSTRUCTION_STATUSES = {
    "not_started",
    "ledger_building",
    "ledger_complete",
    "layout_building",
    "layout_complete",
    "enabled",
}


def _can_locate_twin(
    current_user: User = Depends(get_current_user),
) -> User:
    if (
        has_permission(current_user, "warehouse.view")
        or has_permission(current_user, "deliveries.pick")
        or has_permission(current_user, "warehouse.stocktake.submit")
    ):
        return current_user
    raise HTTPException(status_code=403, detail="权限不足")


def _is_a3_bom_snapshot(snapshot: SalesOrderItemBomComponent) -> bool:
    box_style = (snapshot.snapshot_component_box_style or "").strip().upper()
    return bool(box_style) and ("天地盖" in box_style or "A3" in box_style)


def _bom_snapshot_component_type(
    snapshot: SalesOrderItemBomComponent,
    requested_component: str,
) -> str:
    component = (requested_component or "").strip().lower() or "whole"
    allowed = {"cover", "base"} if _is_a3_bom_snapshot(snapshot) else {"whole"}
    if component not in allowed:
        label = "盖片或底片" if _is_a3_bom_snapshot(snapshot) else "整片"
        raise WarehouseInventoryError(f"该组合组件库存只能选择{label}", 409)
    return component


def _bom_snapshot_physical_facts(
    snapshot: SalesOrderItemBomComponent,
    component_type: str,
) -> dict[str, int | str | None]:
    is_base = component_type == "base"
    return {
        "board_length_mm": (
            snapshot.snapshot_component_base_report_length_mm
            if is_base
            else snapshot.snapshot_component_report_length_mm
        ),
        "board_width_mm": (
            snapshot.snapshot_component_base_report_width_mm
            if is_base
            else snapshot.snapshot_component_report_width_mm
        ),
        "crease_type": (
            snapshot.snapshot_component_base_crease_type
            if is_base
            else snapshot.snapshot_component_crease_type
        ),
        "crease_left_mm": (
            snapshot.snapshot_component_base_crease_left_mm
            if is_base
            else snapshot.snapshot_component_crease_left_mm
        ),
        "crease_middle_mm": (
            snapshot.snapshot_component_base_crease_middle_mm
            if is_base
            else snapshot.snapshot_component_crease_middle_mm
        ),
        "crease_right_mm": (
            snapshot.snapshot_component_base_crease_right_mm
            if is_base
            else snapshot.snapshot_component_crease_right_mm
        ),
    }


def _bom_snapshot_physical_pieces_per_component(
    snapshot: SalesOrderItemBomComponent,
    component_type: str,
) -> int:
    if component_type in {"cover", "base"}:
        return 1
    frozen_value = int(snapshot.snapshot_component_pieces_per_box or 0)
    if frozen_value > 0:
        return frozen_value
    if (snapshot.snapshot_component_splice_mode or "").strip().lower() == "double":
        return 2
    return 1


class LocationPayload(BaseModel):
    location_code: str = Field(min_length=1, max_length=50)
    location_name: str = Field(min_length=1, max_length=100)
    warehouse_type: str
    warehouse_floor: int | None = Field(default=None, ge=1, le=99)
    area_code: str | None = Field(default=None, max_length=30)
    storage_type: str | None = Field(default=None, max_length=30)
    remarks: str | None = None

    @field_validator("location_code", "location_name")
    @classmethod
    def strip_required(cls, value: str) -> str:
        return value.strip()

    @field_validator("warehouse_type")
    @classmethod
    def valid_type(cls, value: str) -> str:
        if value not in {"finished", "semi_finished", "shared"}:
            raise ValueError("库位类型必须是成品、半成品或共用")
        return value

    @field_validator("area_code")
    @classmethod
    def normalize_area(cls, value: str | None) -> str | None:
        normalized = (value or "").strip().upper()
        return normalized or None

    @field_validator("storage_type")
    @classmethod
    def valid_storage_type(cls, value: str | None) -> str | None:
        normalized = (value or "").strip().lower()
        if not normalized:
            return None
        if normalized not in {"ground", "rack", "temporary_aisle"}:
            raise ValueError("存储方式必须是地面位、货架位或临时位")
        return normalized

    @model_validator(mode="after")
    def validate_floor_area(self):
        if self.warehouse_floor == 3:
            if not self.area_code:
                raise ValueError("三楼库位必须填写所属区域")
            if not self.location_code.upper().startswith(f"{self.area_code}-"):
                raise ValueError("三楼货位编码必须以区域编码加连字符开头")
        elif any((self.warehouse_floor, self.area_code, self.storage_type)) and not all(
            (self.warehouse_floor, self.area_code, self.storage_type)
        ):
            raise ValueError("普通库位要启用入库，必须同时填写楼层、区域和存储方式")
        return self


class WarehouseAddressChangePayload(BaseModel):
    model_config = {"extra": "forbid"}

    action_kind: Literal["area", "rack", "location"]
    area_id: int | None = Field(default=None, gt=0)
    location_id: int | None = Field(default=None, gt=0)
    current_rack_code: str | None = Field(default=None, max_length=1)
    new_zone_code: str | None = Field(default=None, max_length=1)
    new_subzone_no: int | None = Field(default=None, ge=1, le=99)
    new_address_kind: Literal["rack_slot", "ground_slot"] | None = None
    new_rack_code: str | None = Field(default=None, max_length=1)
    new_level_no: int | None = Field(default=None, ge=1, le=99)
    new_ground_row_no: int | None = Field(default=None, ge=1, le=99)
    new_slot_no: int | None = Field(default=None, ge=1, le=99)

    @field_validator("current_rack_code", "new_zone_code", "new_rack_code")
    @classmethod
    def normalize_address_letter(cls, value: str | None) -> str | None:
        normalized = (value or "").strip().upper()
        return normalized or None

    def command(self) -> AddressChangeCommand:
        return AddressChangeCommand(**self.model_dump())


class WarehouseAddressConfirmPayload(WarehouseAddressChangePayload):
    preview_fingerprint: str = Field(min_length=64, max_length=64)
    idempotency_key: str = Field(min_length=8, max_length=120)

    @field_validator("preview_fingerprint", "idempotency_key")
    @classmethod
    def strip_address_confirm_text(cls, value: str) -> str:
        return value.strip()

    def command(self) -> AddressChangeCommand:
        values = self.model_dump(
            exclude={"preview_fingerprint", "idempotency_key"}
        )
        return AddressChangeCommand(**values)


class WarehouseFloorPayload(BaseModel):
    floor_code: str = Field(min_length=1, max_length=30)
    floor_name: str = Field(min_length=1, max_length=100)
    floor_number: int = Field(ge=1, le=99)
    planning_reference_pallet_capacity: int = Field(default=0, ge=0)
    construction_status: str = "not_started"
    remarks: str | None = None

    @field_validator("floor_code")
    @classmethod
    def normalize_floor_code(cls, value: str) -> str:
        return value.strip().upper()

    @field_validator("floor_name")
    @classmethod
    def strip_floor_name(cls, value: str) -> str:
        return value.strip()

    @field_validator("construction_status")
    @classmethod
    def valid_construction_status(cls, value: str) -> str:
        if value not in WAREHOUSE_CONSTRUCTION_STATUSES:
            raise ValueError("建设状态不合法")
        return value

    @field_validator("remarks")
    @classmethod
    def strip_floor_remarks(cls, value: str | None) -> str | None:
        normalized = (value or "").strip()
        return normalized or None


class WarehouseAreaPayload(BaseModel):
    floor_id: int = Field(gt=0)
    area_code: str = Field(min_length=1, max_length=30)
    area_name: str = Field(min_length=1, max_length=100)
    planned_location_count: int = Field(default=0, ge=0)
    planned_pallet_capacity: int = Field(default=0, ge=0)
    capacity_review_status: str = "pending"
    capacity_eligible: bool = False
    confirmed_pallet_capacity: int | None = Field(default=None, gt=0)
    construction_status: str = "ledger_building"
    remarks: str | None = None

    @field_validator("area_code")
    @classmethod
    def normalize_area_code(cls, value: str) -> str:
        return value.strip().upper()

    @field_validator("area_name")
    @classmethod
    def strip_area_name(cls, value: str) -> str:
        return value.strip()

    @field_validator("construction_status")
    @classmethod
    def valid_area_construction_status(cls, value: str) -> str:
        if value not in WAREHOUSE_CONSTRUCTION_STATUSES:
            raise ValueError("建设状态不合法")
        return value

    @field_validator("capacity_review_status")
    @classmethod
    def valid_capacity_review_status(cls, value: str) -> str:
        if value not in WAREHOUSE_CAPACITY_REVIEW_STATUSES:
            raise ValueError("容量复核状态不合法")
        return value

    @field_validator("remarks")
    @classmethod
    def strip_area_remarks(cls, value: str | None) -> str | None:
        normalized = (value or "").strip()
        return normalized or None

    @model_validator(mode="after")
    def validate_capacity_review(self):
        if self.capacity_review_status == "pending":
            if self.capacity_eligible or self.confirmed_pallet_capacity is not None:
                raise ValueError("待复核区域不能提前计入安全容量")
        elif self.capacity_review_status == "confirmed":
            if not self.capacity_eligible or self.confirmed_pallet_capacity is None:
                raise ValueError("计入长期容量时必须填写现场确认栈板数")
        elif self.capacity_eligible or self.confirmed_pallet_capacity is not None:
            raise ValueError("不计入容量的区域不能填写现场确认栈板数")
        return self


class WarehouseCapacityForecastPlanPayload(BaseModel):
    source_type: Literal["supplier_requisition", "production_task", "delivery"]
    source_id: int = Field(gt=0)
    effect: Literal["inflow", "outflow", "no_storage"]
    floor_id: int | None = Field(default=None, gt=0)
    planned_date: date
    pallet_slots: int = Field(default=0, ge=0)
    expected_version: int | None = Field(default=None, ge=1)
    operation_key: str = Field(min_length=8, max_length=64)

    @model_validator(mode="after")
    def validate_effect(self):
        allowed = ALLOWED_EFFECTS[self.source_type]
        if self.effect not in allowed:
            raise ValueError("该单据不能使用所选的容量变化方式")
        if self.effect == "no_storage":
            if self.floor_id is not None or self.pallet_slots != 0:
                raise ValueError("直接使用或直接待送不填写楼层和栈板位")
        elif self.floor_id is None or self.pallet_slots <= 0:
            raise ValueError("预计入仓或出仓必须选择楼层并填写大于 0 的栈板位")
        return self


class WarehouseCapacityForecastCancelPayload(BaseModel):
    expected_version: int = Field(ge=1)
    operation_key: str = Field(min_length=8, max_length=64)


class Floor3PalletItemPayload(BaseModel):
    customer_id: int | None = Field(default=None, gt=0)
    product_id: int | None = Field(default=None, gt=0)
    inventory_code: str | None = Field(default=None, max_length=150)
    order_no: str | None = Field(default=None, max_length=100)
    product_name: str | None = Field(default=None, max_length=250)
    item_type: str = "finished"
    quantity: Decimal = Field(gt=0, max_digits=14, decimal_places=3)
    unit: str | None = Field(default=None, max_length=20)
    match_status: str = "matched"
    create_finished_inventory: bool = False
    stock_date: date | None = None
    idempotency_key: str | None = Field(default=None, max_length=120)
    remarks: str | None = Field(default=None, max_length=500)

    @field_validator(
        "inventory_code", "order_no", "product_name", "unit", "remarks"
    )
    @classmethod
    def strip_floor3_item_text(cls, value: str | None) -> str | None:
        text = (value or "").strip()
        return text or None

    @field_validator("item_type")
    @classmethod
    def valid_floor3_item_type(cls, value: str) -> str:
        if value not in {"finished", "semi_finished", "raw_material"}:
            raise ValueError("货物类型无效")
        return value

    @field_validator("match_status")
    @classmethod
    def valid_floor3_match_status(cls, value: str) -> str:
        if value not in {"matched", "pending"}:
            raise ValueError("产品匹配状态无效")
        return value

    @model_validator(mode="after")
    def validate_finished_inventory_request(self) -> "Floor3PalletItemPayload":
        if self.create_finished_inventory:
            if self.item_type != "finished" or self.match_status != "matched":
                raise ValueError("正式成品入库只能用于已匹配的成品行")
            if self.customer_id is None or self.product_id is None:
                raise ValueError("正式成品入库必须选择客户和产品")
            if self.quantity != self.quantity.to_integral_value():
                raise ValueError("正式成品入库数量必须是正整数")
            if self.stock_date is None:
                raise ValueError("正式成品入库必须填写入库日期")
            if not self.idempotency_key or not self.idempotency_key.strip():
                raise ValueError("正式成品入库必须提供幂等键")
        return self


class Floor3PalletPromoteFinishedPayload(BaseModel):
    expected_version: int = Field(gt=0)
    stock_date: date
    idempotency_key: str = Field(min_length=1, max_length=120)
    confirmed: Literal[True]

    @field_validator("idempotency_key")
    @classmethod
    def strip_promote_idempotency_key(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("幂等键不能为空")
        return value


class Floor3PalletCreatePayload(BaseModel):
    location_id: int = Field(gt=0)
    expected_layout_version: int | None = Field(default=None, gt=0)
    pallet_code: str | None = Field(default=None, max_length=80)
    remarks: str | None = Field(default=None, max_length=500)
    items: list[Floor3PalletItemPayload] = Field(min_length=1, max_length=50)

    @field_validator("pallet_code", "remarks")
    @classmethod
    def strip_floor3_pallet_text(cls, value: str | None) -> str | None:
        text = (value or "").strip()
        return text or None

    @model_validator(mode="after")
    def reject_mixed_finished_and_snapshot_rows(self) -> "Floor3PalletCreatePayload":
        has_finished = any(item.create_finished_inventory for item in self.items)
        has_snapshot = any(not item.create_finished_inventory for item in self.items)
        if has_finished and has_snapshot:
            raise ValueError("正式成品行与现场快照行不能混合，请分开保存")
        return self


class Floor3PalletAddItemPayload(BaseModel):
    expected_version: int = Field(gt=0)
    item: Floor3PalletItemPayload


class Floor3PalletMovePayload(BaseModel):
    expected_version: int = Field(gt=0)
    to_location_id: int = Field(gt=0)
    expected_target_layout_version: int | None = Field(default=None, gt=0)
    confirmed: bool
    idempotency_key: str = Field(min_length=1, max_length=120)
    remarks: str | None = Field(default=None, max_length=500)

    @field_validator("remarks")
    @classmethod
    def strip_floor3_move_remarks(cls, value: str | None) -> str | None:
        text = (value or "").strip()
        return text or None

    @field_validator("idempotency_key")
    @classmethod
    def strip_floor3_move_idempotency_key(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("幂等键不能为空")
        return text

    @field_validator("confirmed")
    @classmethod
    def require_floor3_move_confirmation(cls, value: bool) -> bool:
        if value is not True:
            raise ValueError("移位操作必须明确确认")
        return value


class Floor3PalletMergePayload(BaseModel):
    expected_version: int = Field(gt=0)
    target_pallet_id: int = Field(gt=0)
    expected_target_version: int = Field(gt=0)
    confirmed: Literal[True]
    idempotency_key: str = Field(min_length=1, max_length=70)

    @field_validator("idempotency_key")
    @classmethod
    def strip_floor3_merge_idempotency_key(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("幂等键不能为空")
        return text


class PalletMergeBatchSourcePayload(BaseModel):
    client_item_id: str = Field(min_length=1, max_length=80)
    pallet_id: int = Field(gt=0)
    expected_version: int = Field(gt=0)

    @field_validator("client_item_id")
    @classmethod
    def strip_pallet_merge_client_item_id(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("合并草稿来源标识不能为空")
        return text


class PalletMergeBatchPayload(BaseModel):
    idempotency_key: str = Field(min_length=1, max_length=64)
    confirmed: Literal[True]
    target_pallet_id: int = Field(gt=0)
    expected_target_version: int = Field(gt=0)
    sources: list[PalletMergeBatchSourcePayload] = Field(min_length=1, max_length=19)

    @field_validator("idempotency_key")
    @classmethod
    def strip_pallet_merge_batch_key(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("幂等键不能为空")
        return text

    @model_validator(mode="after")
    def reject_invalid_pallet_merge_selection(self) -> "PalletMergeBatchPayload":
        client_ids = [source.client_item_id for source in self.sources]
        if len(client_ids) != len(set(client_ids)):
            raise ValueError("合并草稿来源标识不能重复")
        return self


class TwinFinishedInboundPayload(BaseModel):
    """Admin-confirmed map entry into the existing finished-goods ledger."""

    location_id: int = Field(gt=0)
    expected_layout_version: int = Field(gt=0)
    pallet_code: str | None = Field(default=None, max_length=80)
    customer_id: int = Field(gt=0)
    product_id: int = Field(gt=0)
    quantity: int = Field(gt=0)
    stock_date: date
    idempotency_key: str = Field(min_length=1, max_length=120)
    confirmed: Literal[True]
    remarks: str | None = Field(default=None, max_length=500)

    @field_validator("pallet_code", "remarks")
    @classmethod
    def strip_twin_finished_inbound_text(cls, value: str | None) -> str | None:
        text = (value or "").strip()
        return text or None

    @field_validator("idempotency_key")
    @classmethod
    def strip_twin_finished_inbound_key(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("幂等键不能为空")
        return text


class TwinSemiFinishedInboundPayload(BaseModel):
    """Admin-confirmed semi-finished stock entry from one chosen map location."""

    location_id: int = Field(gt=0)
    expected_layout_version: int = Field(gt=0)
    customer_id: int = Field(gt=0)
    product_id: int = Field(gt=0)
    quantity: int = Field(gt=0)
    stock_date: date
    idempotency_key: str = Field(min_length=1, max_length=120)
    confirmed: Literal[True]
    remarks: str | None = Field(default=None, max_length=500)

    @field_validator("remarks")
    @classmethod
    def strip_twin_semi_finished_remarks(cls, value: str | None) -> str | None:
        text = (value or "").strip()
        return text or None

    @field_validator("idempotency_key")
    @classmethod
    def strip_twin_semi_finished_key(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("幂等键不能为空")
        return text


class TwinStagingPlacementPayload(BaseModel):
    """Admin-confirmed placement of an existing staging lot into one map location."""

    location_id: int = Field(gt=0)
    expected_layout_version: int = Field(gt=0)
    expected_address_version: int | None = Field(default=None, gt=0)
    expected_map_revision: str | None = Field(default=None, min_length=1, max_length=120)
    expected_version: int = Field(gt=0)
    quantity: int = Field(gt=0)
    idempotency_key: str = Field(min_length=1, max_length=120)
    confirmed: Literal[True]

    @field_validator("idempotency_key")
    @classmethod
    def strip_twin_staging_placement_key(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("幂等键不能为空")
        return text


class PendingRelocationResetPayload(BaseModel):
    expected_fingerprint: str = Field(min_length=64, max_length=64)
    idempotency_key: str = Field(min_length=1, max_length=100)
    confirmed: Literal[True]


class TwinTemporaryFinishedInboundPayload(BaseModel):
    """Explicit temporary product creation and first stock placement by an admin."""

    location_id: int = Field(gt=0)
    expected_layout_version: int = Field(gt=0)
    pallet_code: str | None = Field(default=None, max_length=80)
    customer_id: int = Field(gt=0)
    inventory_code: str = Field(min_length=1, max_length=150)
    product_name: str = Field(min_length=1, max_length=250)
    quantity: int = Field(gt=0)
    stock_date: date
    reason: str = Field(min_length=2, max_length=500)
    idempotency_key: str = Field(min_length=1, max_length=120)
    confirmed: Literal[True]

    @field_validator("pallet_code")
    @classmethod
    def strip_twin_temporary_pallet_code(cls, value: str | None) -> str | None:
        text = (value or "").strip()
        return text or None

    @field_validator("inventory_code", "product_name", "reason", "idempotency_key")
    @classmethod
    def strip_twin_temporary_inbound_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("不能为空")
        return text


class TwinPalletMovePayload(BaseModel):
    """Admin-confirmed physical pallet move initiated from the 2D map."""

    expected_version: int = Field(gt=0)
    to_location_id: int = Field(gt=0)
    expected_target_layout_version: int = Field(gt=0)
    idempotency_key: str = Field(min_length=1, max_length=120)
    confirmed: Literal[True]
    remarks: str | None = Field(default=None, max_length=500)

    @field_validator("idempotency_key")
    @classmethod
    def strip_twin_move_key(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("幂等键不能为空")
        return text

    @field_validator("remarks")
    @classmethod
    def strip_twin_move_remarks(cls, value: str | None) -> str | None:
        text = (value or "").strip()
        return text or None


class TwinMovementBatchItemPayload(BaseModel):
    client_item_id: str = Field(min_length=1, max_length=80)
    operation: Literal["pallet_move", "lot_transfer"]
    pallet_id: int | None = Field(default=None, gt=0)
    lot_id: int | None = Field(default=None, gt=0)
    expected_version: int = Field(gt=0)
    quantity: int | None = Field(default=None, gt=0)
    target_location_id: int = Field(gt=0)
    expected_target_layout_version: int = Field(gt=0)
    remarks: str | None = Field(default=None, max_length=500)

    @field_validator("client_item_id")
    @classmethod
    def strip_movement_batch_client_item_id(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("页面草稿标识不能为空")
        return text

    @field_validator("remarks")
    @classmethod
    def strip_movement_batch_remarks(cls, value: str | None) -> str | None:
        text = (value or "").strip()
        return text or None

    @model_validator(mode="after")
    def validate_operation_fields(self) -> "TwinMovementBatchItemPayload":
        if self.operation == "pallet_move":
            if self.pallet_id is None or self.lot_id is not None or self.quantity is not None:
                raise ValueError("整板移动只能填写 pallet_id，不能填写 lot_id 或 quantity")
        elif self.lot_id is None or self.pallet_id is not None or self.quantity is None:
            raise ValueError("部分移货必须填写 lot_id 和 quantity，不能填写 pallet_id")
        return self


class TwinMovementBatchPayload(BaseModel):
    idempotency_key: str = Field(min_length=1, max_length=64)
    confirmed: Literal[True]
    items: list[TwinMovementBatchItemPayload] = Field(min_length=1, max_length=50)

    @field_validator("idempotency_key")
    @classmethod
    def strip_movement_batch_key(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("幂等键不能为空")
        return text

    @model_validator(mode="after")
    def reject_duplicate_client_item_ids(self) -> "TwinMovementBatchPayload":
        ids = [str(item.client_item_id) for item in self.items]
        if len(ids) != len(set(ids)):
            raise ValueError("批次内 client_item_id 不能重复")
        return self


class TwinStocktakeBatchItemPayload(BaseModel):
    client_item_id: str = Field(min_length=1, max_length=80)
    operation: Literal["add", "decrease"]
    location_id: int = Field(gt=0)
    expected_layout_version: int = Field(gt=0)
    inventory_type: Literal["finished", "semi_finished", "raw_material"] | None = None
    unit: Literal["boxes", "sheets"] | None = None
    customer_id: int | None = Field(default=None, gt=0)
    product_id: int | None = Field(default=None, gt=0)
    quantity: int = Field(gt=0)
    stock_date: date | None = None
    lot_id: int | None = Field(default=None, gt=0)
    expected_version: int | None = Field(default=None, gt=0)
    source_kind: Literal["existing_stocktake", "partner_transfer"] | None = None
    stock_stage: Literal["complete", "body"] = "complete"

    @field_validator("client_item_id")
    @classmethod
    def strip_stocktake_batch_client_item_id(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("盘点草稿标识不能为空")
        return text

    @model_validator(mode="after")
    def validate_stocktake_operation_fields(self) -> "TwinStocktakeBatchItemPayload":
        if self.stock_stage == "body" and (self.operation != "add" or self.inventory_type != "finished"):
            raise ValueError("未组装本体只能作为成品类新增盘点，原料与半成品保持自身类型")
        if self.operation == "add":
            expected_unit = {
                "finished": "boxes",
                "semi_finished": "sheets",
                "raw_material": "sheets",
            }.get(self.inventory_type or "")
            if (
                expected_unit is None
                or self.unit != expected_unit
                or self.customer_id is None
                or self.product_id is None
                or self.stock_date is None
                or self.lot_id is not None
                or self.expected_version is not None
                or self.source_kind not in {None, "existing_stocktake", "partner_transfer"}
            ):
                raise ValueError(
                    "盘点新增必须填写货位、客户、产品、类型、匹配单位、数量和库存日期"
                )
        elif (
            self.lot_id is None
            or self.expected_version is None
            or self.inventory_type is not None
            or self.unit is not None
            or self.customer_id is not None
            or self.product_id is not None
            or self.stock_date is not None
            or self.source_kind is not None
        ):
            raise ValueError("盘点调减只允许填写货位、批次、版本和数量")
        return self


class TwinStocktakeBatchPayload(BaseModel):
    idempotency_key: str = Field(min_length=1, max_length=64)
    confirmed: Literal[True]
    initial_inventory_snapshot: str | None = Field(default=None, min_length=64, max_length=64)
    existing_inventory_acknowledged: bool = False
    items: list[TwinStocktakeBatchItemPayload] = Field(
        min_length=1, max_length=50
    )

    @field_validator("idempotency_key")
    @classmethod
    def strip_stocktake_batch_key(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("盘点批次幂等键不能为空")
        return text

    @model_validator(mode="after")
    def reject_duplicate_stocktake_client_item_ids(self) -> "TwinStocktakeBatchPayload":
        ids = [item.client_item_id for item in self.items]
        if len(ids) != len(set(ids)):
            raise ValueError("盘点批次内 client_item_id 不能重复")
        return self


class Floor3LayoutGeometryPayload(BaseModel):
    left_pct: Decimal = Field(ge=0, le=100, max_digits=7, decimal_places=4)
    top_pct: Decimal = Field(ge=0, le=100, max_digits=7, decimal_places=4)
    width_pct: Decimal = Field(gt=0, le=100, max_digits=7, decimal_places=4)
    height_pct: Decimal = Field(gt=0, le=100, max_digits=7, decimal_places=4)
    z_index: int = Field(default=0, ge=-1000, le=1000)

    @model_validator(mode="after")
    def valid_floor3_layout_bounds(self) -> "Floor3LayoutGeometryPayload":
        if self.left_pct + self.width_pct > Decimal("100"):
            raise ValueError(
                "布局不能超出地图右边界：left_pct + width_pct 不能超过 100"
            )
        if self.top_pct + self.height_pct > Decimal("100"):
            raise ValueError(
                "布局不能超出地图下边界：top_pct + height_pct 不能超过 100"
            )
        return self


class Floor3LayoutCreateSlotPayload(Floor3LayoutGeometryPayload):
    location_code: str = Field(min_length=1, max_length=50)
    location_name: str = Field(min_length=1, max_length=100)

    @field_validator("location_code", "location_name")
    @classmethod
    def strip_floor3_layout_slot_text(cls, value: str) -> str:
        return value.strip()


class Floor3LayoutAreaSlotPayload(Floor3LayoutGeometryPayload):
    location_id: int = Field(gt=0)
    expected_version: int = Field(gt=0)


class Floor3LayoutAreaPatchPayload(BaseModel):
    slots: list[Floor3LayoutAreaSlotPayload] = Field(min_length=1, max_length=500)
    expected_map_revision: str | None = Field(default=None, min_length=1, max_length=64)
    expected_policy_version: int | None = Field(default=None, ge=1)


class PublishedGroundLayoutPositionPatchPayload(BaseModel):
    """Move existing published pallet footprints without changing inventory."""

    model_config = {"extra": "forbid"}

    slots: list[Floor3LayoutAreaSlotPayload] = Field(min_length=1, max_length=500)
    expected_map_revision: str = Field(min_length=1, max_length=64)
    expected_policy_version: int = Field(gt=0)
    expected_plan_version: int = Field(gt=0)
    idempotency_key: str = Field(min_length=8, max_length=64)

    @field_validator("expected_map_revision", "idempotency_key")
    @classmethod
    def strip_published_position_text(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def unique_location_ids(self):
        location_ids = [slot.location_id for slot in self.slots]
        if len(location_ids) != len(set(location_ids)):
            raise ValueError("同一个货位不能在一次保存中重复提交")
        return self


class Floor3AreaLocationCountPayload(BaseModel):
    target_count: int = Field(ge=0, le=500)
    confirmed: Literal[True]
    expected_map_revision: str | None = Field(default=None, min_length=1, max_length=64)
    expected_policy_version: int | None = Field(default=None, ge=1)
    expected_ground_plan_version: int | None = Field(default=None, ge=1)
    expected_layout_versions: dict[int, int] | None = Field(
        default=None, max_length=500
    )

    @field_validator("expected_layout_versions")
    @classmethod
    def validate_expected_layout_versions(
        cls, value: dict[int, int] | None
    ) -> dict[int, int] | None:
        if value is not None and any(
            location_id <= 0 or version <= 0
            for location_id, version in value.items()
        ):
            raise ValueError("货位编号和布局版本必须为正整数")
        return value


class AreaLocationAutoArrangePayload(BaseModel):
    confirmed: Literal[True]
    adopt_historical_layouts: bool = False
    expected_map_revision: str | None = Field(default=None, min_length=1, max_length=64)
    expected_policy_version: int | None = Field(default=None, ge=1)
    expected_layout_versions: dict[int, int] = Field(min_length=1, max_length=500)

    @field_validator("expected_layout_versions")
    @classmethod
    def validate_expected_layout_versions(cls, value: dict[int, int]) -> dict[int, int]:
        if any(
            location_id <= 0 or version <= 0
            for location_id, version in value.items()
        ):
            raise ValueError("货位编号和布局版本必须为正整数")
        return value


class GroundLayoutDraftPayload(BaseModel):
    model_config = {"extra": "forbid"}

    target_slot_count: int = Field(ge=1, le=500)
    numbering_origin: Literal["south", "north", "west", "east"]
    row_direction: Literal["from_aisle_inward", "from_inside_outward"]
    slot_direction: Literal["left_to_right", "right_to_left"]
    row_start_no: int = Field(default=1, ge=1, le=99)
    slot_start_no: int = Field(default=1, ge=1, le=99)
    expected_policy_version: int = Field(gt=0)
    expected_map_revision: str = Field(min_length=1, max_length=64)
    expected_plan_version: int | None = Field(default=None, gt=0)


class GroundLayoutPublishPayload(BaseModel):
    model_config = {"extra": "forbid"}

    expected_plan_version: int = Field(gt=0)
    preview_fingerprint: str = Field(min_length=64, max_length=64)
    idempotency_key: str = Field(min_length=1, max_length=120)

    @field_validator("preview_fingerprint", "idempotency_key")
    @classmethod
    def strip_ground_publish_text(cls, value: str) -> str:
        return value.strip()


class GroundFinishedInboundPayload(BaseModel):
    model_config = {"extra": "forbid"}

    location_id: int = Field(gt=0)
    expected_layout_version: int = Field(gt=0)
    secondary_location_id: int | None = Field(default=None, gt=0)
    expected_secondary_layout_version: int | None = Field(default=None, gt=0)
    customer_id: int = Field(gt=0)
    product_id: int = Field(gt=0)
    quantity: int = Field(gt=0)
    capacity_quantity: int = Field(gt=0)
    stock_date: date
    idempotency_key: str = Field(min_length=1, max_length=120)
    remarks: str | None = Field(default=None, max_length=500)

    @field_validator("idempotency_key")
    @classmethod
    def strip_ground_inbound_key(cls, value: str) -> str:
        return value.strip()

    @field_validator("remarks")
    @classmethod
    def strip_ground_inbound_remarks(cls, value: str | None) -> str | None:
        return (value or "").strip() or None

    @model_validator(mode="after")
    def validate_ground_inbound_footprint(self) -> "GroundFinishedInboundPayload":
        if (self.secondary_location_id is None) != (
            self.expected_secondary_layout_version is None
        ):
            raise ValueError("大型货物的第二位置与布局版本必须同时提供")
        if self.secondary_location_id == self.location_id:
            raise ValueError("大型货物必须选择两个不同的相邻位置")
        if self.capacity_quantity < self.quantity:
            raise ValueError("位置容量不能小于本次入库数量")
        return self


class GroundFinishedTransferPayload(BaseModel):
    model_config = {"extra": "forbid"}

    location_id: int = Field(gt=0)
    expected_layout_version: int = Field(gt=0)
    secondary_location_id: int | None = Field(default=None, gt=0)
    expected_secondary_layout_version: int | None = Field(default=None, gt=0)
    expected_lot_version: int = Field(gt=0)
    quantity: int = Field(gt=0)
    capacity_quantity: int = Field(gt=0)
    idempotency_key: str = Field(min_length=1, max_length=120)

    @field_validator("idempotency_key")
    @classmethod
    def strip_ground_transfer_key(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def validate_ground_transfer_footprint(self) -> "GroundFinishedTransferPayload":
        if (self.secondary_location_id is None) != (
            self.expected_secondary_layout_version is None
        ):
            raise ValueError("大型货物的第二位置与布局版本必须同时提供")
        if self.secondary_location_id == self.location_id:
            raise ValueError("大型货物必须选择两个不同的相邻位置")
        if self.capacity_quantity < self.quantity:
            raise ValueError("位置容量不能小于本次转位数量")
        return self


class Floor3LayoutSlotStatePayload(BaseModel):
    expected_version: int = Field(gt=0)
    expected_map_revision: str | None = Field(default=None, min_length=1, max_length=64)
    expected_policy_version: int | None = Field(default=None, ge=1)
    retire_published_ground_slot: bool = False
    expected_ground_plan_version: int | None = Field(default=None, ge=1)


class Floor3PalletClearPayload(BaseModel):
    expected_version: int = Field(gt=0)
    remarks: str | None = Field(default=None, max_length=500)


class Floor3PalletRelocationPayload(BaseModel):
    expected_version: int = Field(gt=0)
    needs_relocation: bool
    placement_confirmed: bool = False
    remarks: str | None = Field(default=None, max_length=500)


class MoldCustomerAssociationPayload(BaseModel):
    customer_id: int = Field(gt=0)
    display_order: Literal[1, 2] | None = None


class MoldLabelOverridesPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_identity: str | None = Field(default=None, max_length=200)
    product_name: str | None = Field(default=None, max_length=250)
    report_specification: str | None = Field(default=None, max_length=200)
    cutting_mode: str | None = Field(default=None, max_length=100)
    remarks: str | None = Field(default=None, max_length=500)

    @field_validator(*LABEL_OVERRIDE_FIELDS)
    @classmethod
    def normalize_fields(cls, value: str | None, info) -> str | None:
        try:
            return normalize_label_override_text(value, field=info.field_name)
        except MoldLabelContentError as error:
            raise ValueError(str(error)) from error

    def as_dict(self) -> dict[str, str | None]:
        return {field: getattr(self, field) for field in LABEL_OVERRIDE_FIELDS}


class MoldToolPayload(BaseModel):
    # mold_code remains optional for compatibility with older API clients.
    # The warehouse UI sends customer_initials and lets the server allocate it.
    mold_code: str | None = Field(default=None, max_length=100)
    customer_initials: str | None = Field(default=None, max_length=20)
    mold_name: str | None = Field(default=None, max_length=200)
    label_name: str | None = Field(default=None, max_length=200)
    chinese_short_name: str | None = Field(default=None, max_length=100)
    label_overrides: MoldLabelOverridesPayload | None = None
    customers: list[MoldCustomerAssociationPayload] | None = Field(
        default=None,
        max_length=50,
    )
    expected_version: int | None = Field(default=None, gt=0)
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=120)
    rack_location: str = Field(min_length=1, max_length=250)
    remarks: str | None = None
    expected_location_version: int | None = Field(default=None, gt=0)
    location_idempotency_key: str | None = Field(
        default=None,
        min_length=8,
        max_length=120,
    )
    physical_move_confirmed: bool = False
    location_note: str | None = Field(default=None, max_length=500)

    @field_validator("rack_location")
    @classmethod
    def strip_mold_fields(cls, value: str) -> str:
        return value.strip()

    @field_validator(
        "mold_code",
        "customer_initials",
        "mold_name",
        "label_name",
        "chinese_short_name",
        "idempotency_key",
        "location_idempotency_key",
        "location_note",
    )
    @classmethod
    def strip_optional_mold_fields(cls, value: str | None) -> str | None:
        text = (value or "").strip()
        return text or None

    @model_validator(mode="after")
    def validate_identity_contract(self) -> "MoldToolPayload":
        uses_formal_identity = any(
            value is not None
            for value in (
                self.label_name,
                self.chinese_short_name,
                self.label_overrides,
                self.customers,
                self.expected_version,
                self.idempotency_key,
            )
        )
        if not uses_formal_identity:
            if not self.mold_name:
                raise ValueError("请填写模具名称")
            return self
        if not self.label_name:
            raise ValueError("请填写模具标签名称")
        if not self.customers:
            raise ValueError("请至少选择一个正式客户")
        customer_ids = [item.customer_id for item in self.customers]
        if len(customer_ids) != len(set(customer_ids)):
            raise ValueError("同一客户不能重复关联")
        primary_orders = sorted(
            item.display_order
            for item in self.customers
            if item.display_order is not None
        )
        if primary_orders not in ([1], [1, 2]):
            raise ValueError("主标签客户必须从第一位开始，且最多选择两个")
        if not self.idempotency_key:
            raise ValueError("模具资料保存凭证缺失，请刷新后重试")
        return self


class MoldProductBindingItem(BaseModel):
    product_id: int = Field(gt=0)
    expected_version: int = Field(gt=0)


class MoldProductBindingPayload(BaseModel):
    items: list[MoldProductBindingItem] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def reject_duplicate_products(self) -> "MoldProductBindingPayload":
        ids = [item.product_id for item in self.items]
        if len(ids) != len(set(ids)):
            raise ValueError("同一款常用箱不能重复选择")
        return self


class MoldLocationPreviewPayload(BaseModel):
    mold_code: str = Field(min_length=1, max_length=100)
    target_location: str = Field(min_length=1, max_length=250)

    @field_validator("mold_code", "target_location")
    @classmethod
    def strip_mold_location_preview_fields(cls, value: str) -> str:
        return value.strip()


class MoldLocationConfirmPayload(MoldLocationPreviewPayload):
    expected_version: int = Field(gt=0)
    idempotency_key: str = Field(min_length=8, max_length=120)
    source: Literal["manual_input", "scanner_paste", "url_parameter", "api"] = (
        "manual_input"
    )
    note: str | None = Field(default=None, max_length=500)

    @field_validator("idempotency_key")
    @classmethod
    def strip_mold_location_idempotency_key(cls, value: str) -> str:
        text = value.strip()
        if len(text) < 8:
            raise ValueError("幂等键去除首尾空白后至少需要 8 个字符")
        return text

    @field_validator("note")
    @classmethod
    def strip_mold_location_note(cls, value: str | None) -> str | None:
        text = (value or "").strip()
        return text or None


class MoldArchiveConfirmPayload(BaseModel):
    expected_version: int = Field(gt=0)
    idempotency_key: str = Field(min_length=8, max_length=120)
    reason: Literal["unbound", "all_products_inactive"]
    physical_move_confirmed: Literal[True]

    @field_validator("idempotency_key")
    @classmethod
    def strip_archive_idempotency_key(cls, value: str) -> str:
        text = value.strip()
        if len(text) < 8:
            raise ValueError("幂等键去除首尾空白后至少需要 8 个字符")
        return text


class MoldRestoreConfirmPayload(BaseModel):
    target_location: str = Field(min_length=1, max_length=250)
    expected_version: int = Field(gt=0)
    idempotency_key: str = Field(min_length=8, max_length=120)
    physical_move_confirmed: Literal[True]

    @field_validator("target_location", "idempotency_key")
    @classmethod
    def strip_restore_fields(cls, value: str) -> str:
        return value.strip()


class MoldRepairStatusPayload(BaseModel):
    target_status: Literal["normal", "needs_repair"]
    expected_version: int = Field(gt=0)
    idempotency_key: str = Field(min_length=8, max_length=120)
    confirmed: Literal[True]

    @field_validator("idempotency_key")
    @classmethod
    def strip_repair_idempotency_key(cls, value: str) -> str:
        return value.strip()


class MoldLabelPrintRegisterPayload(BaseModel):
    mold_ids: list[int] = Field(min_length=1, max_length=100)
    source: Literal["single", "batch"]
    template_version: Literal["mold_40x30_v1", "mold_80x40_v1"] = (
        MOLD_LABEL_TEMPLATE_40X30
    )
    idempotency_key: str = Field(min_length=8, max_length=120)

    @field_validator("idempotency_key")
    @classmethod
    def strip_mold_label_print_idempotency_key(cls, value: str) -> str:
        text = value.strip()
        if len(text) < 8:
            raise ValueError("幂等键去除首尾空白后至少需要 8 个字符")
        return text

    @model_validator(mode="after")
    def validate_mold_label_print_selection(self) -> "MoldLabelPrintRegisterPayload":
        if any(mold_id <= 0 for mold_id in self.mold_ids):
            raise ValueError("模具选择无效")
        if len(self.mold_ids) != len(set(self.mold_ids)):
            raise ValueError("同一模具不能重复选择")
        if self.source == "single" and len(self.mold_ids) != 1:
            raise ValueError("单个打印一次只能选择一件模具")
        return self


class MoldLabelLayoutPaperPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    width_mm: float
    height_mm: float


class MoldLabelLayoutElementPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=50)
    kind: Literal["text", "qr"]
    x_mm: float
    y_mm: float
    width_mm: float
    height_mm: float
    font_size_mm: float | None = None
    font_weight: int | None = None
    text_align: Literal["left", "center", "right"] | None = None
    visible: bool


class MoldLabelLayoutPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    catalog_version: str = Field(min_length=1, max_length=40)
    paper: MoldLabelLayoutPaperPayload
    elements: list[MoldLabelLayoutElementPayload]


class MoldLabelLayoutSaveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation_key: str = Field(min_length=8, max_length=120)
    expected_release_version: int = Field(ge=0)
    layout: MoldLabelLayoutPayload

    @field_validator("operation_key")
    @classmethod
    def strip_mold_layout_operation_key(cls, value: str) -> str:
        text = value.strip()
        if len(text) < 8:
            raise ValueError("操作编号去除首尾空白后至少需要8个字符")
        return text


class MoldLabelLayoutReleaseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation_key: str = Field(min_length=8, max_length=120)
    expected_release_version: int = Field(ge=0)

    @field_validator("operation_key")
    @classmethod
    def strip_mold_layout_release_operation_key(cls, value: str) -> str:
        text = value.strip()
        if len(text) < 8:
            raise ValueError("操作编号去除首尾空白后至少需要8个字符")
        return text


class MoldScanEventPayload(BaseModel):
    production_task_id: int = Field(gt=0)
    expected_mold_location_version: int = Field(gt=0)
    idempotency_key: str = Field(min_length=8, max_length=120)

    @field_validator("idempotency_key")
    @classmethod
    def strip_mold_scan_idempotency_key(cls, value: str) -> str:
        text = value.strip()
        if len(text) < 8:
            raise ValueError("幂等键去除首尾空白后至少需要 8 个字符")
        return text


class PrintingPlateCreatePayload(BaseModel):
    customer_id: int = Field(gt=0)
    plate_name: str = Field(min_length=1, max_length=200)
    color_name: str = Field(min_length=1, max_length=100)
    rack_location: str = Field(min_length=1, max_length=100)
    remarks: str | None = Field(default=None, max_length=1000)

    @field_validator("plate_name", "color_name", "rack_location")
    @classmethod
    def strip_required_plate_fields(cls, value: str) -> str:
        return value.strip()

    @field_validator("remarks")
    @classmethod
    def strip_optional_plate_fields(cls, value: str | None) -> str | None:
        return (value or "").strip() or None


class PrintingPlateUpdatePayload(BaseModel):
    expected_version: int = Field(gt=0)
    plate_name: str = Field(min_length=1, max_length=200)
    color_name: str = Field(min_length=1, max_length=100)
    rack_location: str = Field(min_length=1, max_length=100)
    remarks: str | None = Field(default=None, max_length=1000)

    @field_validator("plate_name", "color_name", "rack_location")
    @classmethod
    def strip_required_plate_update_fields(cls, value: str) -> str:
        return value.strip()

    @field_validator("remarks")
    @classmethod
    def strip_optional_plate_update_fields(cls, value: str | None) -> str | None:
        return (value or "").strip() or None


class PrintingPlateStatusPayload(BaseModel):
    expected_version: int = Field(gt=0)
    status: Literal["active", "inactive", "damaged"]


class PrintingPlateResinReusePreviewPayload(BaseModel):
    target_customer_id: int = Field(gt=0)
    target_plate_name: str = Field(min_length=1, max_length=200)
    target_color_name: str = Field(min_length=1, max_length=100)

    @field_validator("target_plate_name", "target_color_name")
    @classmethod
    def strip_printing_plate_resin_reuse_fields(cls, value: str) -> str:
        return value.strip()


class PrintingPlateResinReuseConfirmPayload(PrintingPlateResinReusePreviewPayload):
    expected_version: int = Field(gt=0)
    idempotency_key: str = Field(min_length=8, max_length=120)
    old_resin_removed: Literal[True]
    new_resin_mounted: Literal[True]

    @field_validator("idempotency_key")
    @classmethod
    def strip_printing_plate_resin_reuse_key(cls, value: str) -> str:
        return value.strip()


class PrintingPlateLocationPreviewPayload(BaseModel):
    plate_code: str = Field(min_length=1, max_length=30)
    target_location: str = Field(min_length=1, max_length=100)

    @field_validator("plate_code", "target_location")
    @classmethod
    def strip_printing_plate_location_fields(cls, value: str) -> str:
        return value.strip()


class PrintingPlateLocationConfirmPayload(PrintingPlateLocationPreviewPayload):
    expected_version: int = Field(gt=0)
    idempotency_key: str = Field(min_length=8, max_length=120)
    source: Literal["manual_input", "scanner_paste", "url_parameter", "api"] = (
        "manual_input"
    )
    note: str | None = Field(default=None, max_length=500)

    @field_validator("idempotency_key")
    @classmethod
    def strip_printing_plate_idempotency_key(cls, value: str) -> str:
        return value.strip()

    @field_validator("note")
    @classmethod
    def strip_printing_plate_note(cls, value: str | None) -> str | None:
        return (value or "").strip() or None


class FinishedManualInPayload(BaseModel):
    customer_id: int
    product_id: int
    location_id: int
    expected_layout_version: int | None = Field(default=None, gt=0)
    quantity: int = Field(gt=0)
    stock_date: date
    stock_date_accuracy: Literal["exact", "estimated", "unknown"] = "exact"
    stock_date_original_text: str | None = Field(default=None, max_length=100)
    source_type: str = "manual"
    stock_stage: Literal["complete", "body"] = "complete"
    remarks: str | None = None
    idempotency_key: str | None = Field(default=None, max_length=100)

    @field_validator("source_type")
    @classmethod
    def valid_source_type(cls, value: str) -> str:
        if value not in VALID_SOURCE_TYPES:
            raise ValueError("库存来源无效")
        return value


class FinishedLotEditPayload(BaseModel):
    expected_version: int = Field(gt=0)
    is_general: bool
    customer_id: int | None = Field(default=None, gt=0)
    product_id: int = Field(gt=0)
    quantity_available: int = Field(ge=0)
    location_id: int = Field(gt=0)
    expected_layout_version: int | None = Field(default=None, gt=0)
    stock_date: date
    confirm_stock_date_exact: bool = False
    idempotency_key: str = Field(min_length=1, max_length=100)

    @field_validator("idempotency_key")
    @classmethod
    def strip_finished_edit_idempotency_key(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("幂等键不能为空")
        return text

    @model_validator(mode="after")
    def customer_required_for_dedicated_inventory(self) -> "FinishedLotEditPayload":
        if not self.is_general and self.customer_id is None:
            raise ValueError("客户专用库存必须选择客户")
        return self


class FinishedLotLocationTransferPayload(BaseModel):
    expected_version: int = Field(gt=0)
    quantity: int = Field(gt=0)
    location_id: int = Field(gt=0)
    expected_target_layout_version: int | None = Field(default=None, gt=0)
    idempotency_key: str = Field(min_length=1, max_length=120)

    @field_validator("idempotency_key")
    @classmethod
    def strip_location_transfer_idempotency_key(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("请求标识不能为空")
        return text


class SemiFinishedManualInPayload(BaseModel):
    location_id: int
    expected_layout_version: int | None = Field(default=None, gt=0)
    quantity: int = Field(gt=0)
    stock_date: date
    stock_date_accuracy: Literal["exact", "estimated", "unknown"] = "exact"
    stock_date_original_text: str | None = Field(default=None, max_length=100)
    source_type: str = "manual"
    material_code: str = Field(min_length=1, max_length=100)
    layer_count: int
    flute_type: str
    board_length_mm: int = Field(gt=0)
    board_width_mm: int = Field(gt=0)
    sheet_type: str
    component_type: str = "whole"
    pieces_per_box: int = Field(default=1, gt=0)
    stock_yield_per_sheet: int = Field(default=1, gt=0)
    supplier_name: str | None = None
    customer_id: int | None = None
    crease_type: str | None = None
    crease_left_mm: int | None = Field(default=None, ge=0)
    crease_middle_mm: int | None = Field(default=None, ge=0)
    crease_right_mm: int | None = Field(default=None, ge=0)
    cutting_note: str | None = None
    remarks: str | None = None
    idempotency_key: str | None = Field(default=None, max_length=100)

    @field_validator("source_type")
    @classmethod
    def valid_source_type(cls, value: str) -> str:
        if value not in VALID_SOURCE_TYPES:
            raise ValueError("库存来源无效")
        return value


class SemiFinishedLotEditPayload(BaseModel):
    expected_version: int = Field(gt=0)
    customer_id: int | None = Field(default=None, gt=0)


class SemiFinishedLotVoidPayload(BaseModel):
    expected_version: int = Field(gt=0)
    reason: str | None = Field(default=None, max_length=500)


class VersionPayload(BaseModel):
    expected_version: int = Field(gt=0)
    reason: str | None = Field(default=None, max_length=500)
    idempotency_key: str | None = Field(default=None, max_length=100)


class QuantityOperationPayload(VersionPayload):
    quantity: int = Field(gt=0)


class AdjustPayload(VersionPayload):
    quantity_delta: int

    @field_validator("quantity_delta")
    @classmethod
    def nonzero(cls, value: int) -> int:
        if value == 0:
            raise ValueError("调整数量不能为0")
        return value


class TwinLotQuantityCorrectionPayload(BaseModel):
    expected_version: int = Field(gt=0)
    action: Literal["decrease", "remove"]
    quantity: int | None = Field(default=None, gt=0)
    reason: str = Field(min_length=2, max_length=500)
    idempotency_key: str = Field(min_length=1, max_length=100)
    confirmed: Literal[True]

    @field_validator("reason", "idempotency_key")
    @classmethod
    def strip_twin_correction_text(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def validate_twin_correction_quantity(self) -> "TwinLotQuantityCorrectionPayload":
        if self.action == "decrease" and self.quantity is None:
            raise ValueError("减少库存必须填写数量")
        if self.action == "remove" and self.quantity is not None:
            raise ValueError("移除货物不需要填写数量")
        return self



class FinishedReservationPayload(BaseModel):
    order_item_id: int
    inventory_lot_id: int
    quantity: int = Field(gt=0)
    expected_version: int = Field(gt=0)
    idempotency_key: str = Field(min_length=8, max_length=100)
    warning_acknowledged_codes: list[str] = Field(default_factory=list)


class BomComponentFinishedReservationPayload(FinishedReservationPayload):
    bom_snapshot_id: int = Field(gt=0)


class BomComponentAutoCoverPayload(BaseModel):
    order_item_id: int = Field(gt=0)
    idempotency_key: str = Field(min_length=8, max_length=60)
    component_type: Literal["whole", "cover", "base"] = "whole"


class ReleaseReservationPayload(BaseModel):
    release_reason: str | None = Field(default=None, max_length=500)
    idempotency_key: str = Field(min_length=8, max_length=100)


class SemiRequirementPayload(BaseModel):
    component_type: str
    board_length_mm: int = Field(gt=0)
    board_width_mm: int = Field(gt=0)
    material_code: str = Field(min_length=1, max_length=100)
    material_id: int | None = None
    flute_type: str = Field(min_length=1, max_length=20)
    pieces_per_box: int = Field(gt=0)
    stock_yield_per_sheet: int = Field(gt=0)
    required_piece_quantity: int | None = Field(default=None, gt=0)


class SemiProductCandidatePayload(BaseModel):
    customer_id: int
    board_length_mm: int = Field(gt=0)
    board_width_mm: int = Field(gt=0)
    material_code: str = Field(min_length=1, max_length=100)
    flute_type: str = Field(min_length=1, max_length=20)
    component_type: str
    pieces_per_box: int = Field(gt=0)
    stock_yield_per_sheet: int = Field(gt=0)
    layer_count: int | None = Field(default=None, gt=0)
    crease_type: str | None = Field(default=None, max_length=20)
    crease_left_mm: int | None = Field(default=None, ge=0)
    crease_middle_mm: int | None = Field(default=None, ge=0)
    crease_right_mm: int | None = Field(default=None, ge=0)
    # New orders and PDF imports may only adopt stock that is already a usable
    # semi-finished part.  Stock that requires a fresh cutting decision stays
    # visible to the requisition workflow, where its cutting plan is confirmed.
    stage: Literal["order", "requisition"] = "order"


class SemiMatchConfirmPayload(BaseModel):
    inventory_lot_id: int
    override: bool = False
    warning_acknowledged_codes: list[str] = Field(default_factory=list)


class SemiLotVersionPayload(BaseModel):
    lot_id: int
    expected_version: int = Field(gt=0)


class SemiReservationPayload(BaseModel):
    requested_requirement_quantity: int = Field(gt=0)
    lots: list[SemiLotVersionPayload] = Field(min_length=1)
    confirmed: bool
    override: bool = False
    warning_acknowledged_codes: list[str] = Field(default_factory=list)
    idempotency_key: str = Field(min_length=1, max_length=80)


class BomComponentSemiRequirementPayload(BaseModel):
    stock_yield_per_sheet: int = Field(default=1, gt=0)
    component_type: Literal["whole", "cover", "base"] = "whole"


class SemiReleasePayload(BaseModel):
    expected_version: int = Field(gt=0)
    stock_quantity: int | None = Field(default=None, gt=0)
    release_reason: str | None = Field(default=None, max_length=500)
    idempotency_key: str = Field(min_length=1, max_length=100)


class SemiProductAssignmentsPayload(BaseModel):
    expected_version: int = Field(gt=0)
    product_ids: list[int] = Field(default_factory=list, max_length=500)

    @field_validator("product_ids")
    @classmethod
    def valid_product_ids(cls, values: list[int]) -> list[int]:
        if any(int(value) <= 0 for value in values):
            raise ValueError("成品款号ID必须是正整数")
        return list(dict.fromkeys(int(value) for value in values))


class SemiConsumePayload(BaseModel):
    expected_version: int = Field(gt=0)
    stock_quantity: int = Field(gt=0)
    delivery_item_id: int | None = None
    idempotency_key: str = Field(min_length=1, max_length=100)


class SemiReverseConsumePayload(BaseModel):
    expected_version: int = Field(gt=0)
    stock_quantity: int = Field(gt=0)
    allocation_id: int | None = None
    idempotency_key: str = Field(min_length=1, max_length=100)


def _handle(error: WarehouseInventoryError) -> None:
    raise HTTPException(status_code=error.status_code, detail=str(error)) from error


def _handle_integrity(error: IntegrityError) -> None:
    raise HTTPException(
        status_code=409,
        detail="库存或学习规则已被其他请求修改，请刷新后重试",
    ) from error


def _visible_customer_ids(user: User, db: Session) -> set[int] | None:
    if has_unrestricted_customer_access(user, db):
        return None
    return customer_scope_ids(user, db)


def _require_order_item_customer_access(
    db: Session,
    order_item_id: int,
    user: User,
) -> None:
    customer_id = db.scalar(
        select(Order.customer_id)
        .join(OrderItem, OrderItem.order_id == Order.id)
        .where(OrderItem.id == order_item_id)
    )
    if customer_id is None:
        raise HTTPException(status_code=404, detail="订单明细不存在")
    require_customer_access(customer_id, user, db)


def _require_requirement_customer_access(
    db: Session,
    requirement_id: int,
    user: User,
) -> None:
    requirement = db.get(OrderItemSemiRequirement, requirement_id)
    if requirement is None:
        raise HTTPException(status_code=404, detail="半成品需求不存在")
    require_customer_access(requirement.customer_id, user, db)


def _require_reservation_customer_access(
    db: Session,
    reservation_id: int,
    user: User,
) -> None:
    reservation = db.get(InventoryReservation, reservation_id)
    if reservation is None:
        return
    _require_lot_customer_access(db, reservation.inventory_lot_id, user)
    customer_id = None
    if reservation.order_id is not None:
        customer_id = db.scalar(
            select(Order.customer_id).where(Order.id == reservation.order_id)
        )
    elif reservation.semi_requirement_id is not None:
        customer_id = db.scalar(
            select(OrderItemSemiRequirement.customer_id).where(
                OrderItemSemiRequirement.id == reservation.semi_requirement_id
            )
        )
    if customer_id is not None:
        require_customer_access(customer_id, user, db)


def _location_map_status(
    row: WarehouseLocation,
    projection_context: Mapping[str, object] | None = None,
) -> str:
    """Compatibility field derived only from the canonical published projection."""

    return str(
        warehouse_location_projection(
            row,
            **dict(projection_context or {}),
        )["map_status"]
    )


def _location_dict(
    row: WarehouseLocation | None,
    projection_context: Mapping[str, object] | None = None,
) -> dict:
    if row is None:
        return {
            "id": None,
            "location_code": None,
            "location_name": "尚未绑定正式位置",
            "location_master_name": None,
            "warehouse_type": None,
            "warehouse_floor": None,
            "area_code": None,
            "storage_type": None,
            "storage_layout": None,
            "can_receive_pallet": False,
            "level_no": None,
            "side_code": None,
            "sort_order": 0,
            "is_temporary": False,
            "source_version": None,
            "placement_status": "unplaced",
            "is_active": False,
            "position_status": "unlocated",
            "map_status": "ledger_only",
            "map_position": None,
            "map_feature_id": None,
            "published_map_revision": None,
            "map_issue": "库存批次尚未绑定正式位置",
            "layout_version": None,
            "remarks": None,
            "current_address_code": None,
            "current_address_name": "尚未绑定正式位置",
            "employee_location_name": "尚未绑定正式位置",
            "projection_source": "unlocated",
        }
    context = dict(projection_context or {})
    projection = warehouse_location_projection(row, **context)
    floor = context.get("floor")
    area = context.get("area")
    address_area = area if projection_context is not None else getattr(
        row, "address_area", None
    )
    address_payload = location_address_payload(
        row,
        area=address_area,
        floor=floor,
        position_status=str(projection["position_status"]),
        area_sequence=(int(context["area_sequence"]) if context.get("area_sequence") else None),
    )
    layout = context.get("layout")
    policy = context.get("policy")
    storage_layout = (
        policy.storage_layout
        if isinstance(policy, WarehouseAreaStoragePolicy)
        else None
    )
    return {
        "id": row.id,
        "location_code": row.location_code,
        "location_name": address_payload["employee_location_name"],
        "location_master_name": row.location_name,
        "warehouse_type": row.warehouse_type,
        "warehouse_floor": getattr(row, "warehouse_floor", None),
        "area_code": getattr(row, "area_code", None),
        "storage_type": getattr(row, "storage_type", None),
        "storage_layout": storage_layout,
        "can_receive_pallet": bool(
            row.address_kind != "functional"
            and storage_layout in {"pallet_ground", "mixed"}
            and isinstance(layout, Floor3LocationLayout)
            and layout.layout_kind == "physical_pallet"
        ),
        "level_no": getattr(row, "level_no", None),
        "side_code": getattr(row, "side_code", None),
        "sort_order": getattr(row, "sort_order", 0),
        "is_temporary": getattr(row, "is_temporary", False),
        "source_version": getattr(row, "source_version", None),
        "placement_status": getattr(row, "placement_status", None) or "unplaced",
        "is_active": row.is_active,
        **projection,
        "remarks": row.remarks,
        **address_payload,
        # The generic address payload cannot know the live map/layout tokens.
        # Keep the canonical projection values last so a response never turns
        # a valid concurrency token into ``None``.
        "layout_version": (
            int(layout.version)
            if isinstance(layout, Floor3LocationLayout)
            else None
        ),
        "published_map_revision": projection.get("published_map_revision"),
    }


def _location_dict_for_db(
    db: Session,
    row: WarehouseLocation | None,
) -> dict:
    """Serialize one public location response with the canonical map context."""

    if row is None:
        return _location_dict(None)
    context = load_warehouse_location_projection_contexts(db, [row]).get(
        int(row.id),
        {},
    )
    return _location_dict(row, context)


def _formal_inventory_location_condition():
    """Allow standard and twin-area locations plus valid third-floor V11 rows."""
    return or_(
        WarehouseLocation.source_version.is_(None),
        WarehouseLocation.source_version != "V11",
        and_(
            WarehouseLocation.source_version == "V11",
            WarehouseLocation.warehouse_floor == 3,
        ),
    )


def _reject_floor3_for_semi_finished_inventory(
    db: Session,
    location_id: int,
) -> None:
    location = db.get(WarehouseLocation, location_id)
    if (
        location is not None
        and location.source_version == "V11"
    ):
        raise HTTPException(
            status_code=409,
            detail="三楼货位目前只接入成品仓；半成品请使用半成品库位。",
        )


def _reject_v11_location_configuration(row: WarehouseLocation) -> None:
    if row.source_version in {"V11", AREA_LOCATION_SOURCE_VERSION}:
        raise HTTPException(
            status_code=409,
            detail="三楼平面图或仓库地图绑定的正式库位只能通过对应地图维护。",
        )


def _floor3_item_visible(
    row: InventoryPalletItem,
    visible_customer_ids: set[int] | None,
) -> bool:
    if visible_customer_ids is None:
        return True
    if row.customer_id is None or row.customer_id not in visible_customer_ids:
        return False

    linked_lot = row.inventory_lot
    if linked_lot is None:
        # Preserve the existing scoped behavior for legal snapshot rows.  A
        # dangling non-null lot id is not a legal snapshot and must fail closed.
        return row.inventory_lot_id is None
    if linked_lot.inventory_type != "finished":
        # Floor-three semi-finished history keeps its existing customer check.
        return True

    detail = linked_lot.finished_detail
    product = row.product
    return bool(
        row.item_type == "finished"
        and detail is not None
        and product is not None
        and row.customer_id == detail.owner_customer_id
        and row.product_id == detail.product_id
        and product.customer_id == detail.owner_customer_id
    )


def _floor3_item_scope_condition(visible_customer_ids: set[int]):
    """SQL equivalent of the scoped floor-three item visibility check."""

    valid_finished_lot_ids = (
        select(FinishedGoodsInventoryDetail.inventory_lot_id)
        .join(
            InventoryLot,
            InventoryLot.id == FinishedGoodsInventoryDetail.inventory_lot_id,
        )
        .join(Product, Product.id == FinishedGoodsInventoryDetail.product_id)
        .where(
            InventoryLot.inventory_type == "finished",
            FinishedGoodsInventoryDetail.owner_customer_id.in_(
                visible_customer_ids
            ),
            FinishedGoodsInventoryDetail.owner_customer_id
            == InventoryPalletItem.customer_id,
            FinishedGoodsInventoryDetail.product_id
            == InventoryPalletItem.product_id,
            Product.customer_id
            == FinishedGoodsInventoryDetail.owner_customer_id,
        )
    )
    non_finished_lot_ids = select(InventoryLot.id).where(
        InventoryLot.inventory_type != "finished"
    )
    return and_(
        InventoryPalletItem.customer_id.in_(visible_customer_ids),
        or_(
            InventoryPalletItem.inventory_lot_id.is_(None),
            InventoryPalletItem.inventory_lot_id.in_(non_finished_lot_ids),
            InventoryPalletItem.inventory_lot_id.in_(valid_finished_lot_ids),
        ),
    )


def _floor3_item_dict(
    row: InventoryPalletItem,
    customer_names: dict[int, str],
) -> dict:
    linked_lot = row.inventory_lot
    quantity = (
        int(linked_lot.quantity_available or 0)
        + int(linked_lot.quantity_reserved or 0)
        if linked_lot is not None
        else row.quantity
    )
    return {
        "id": row.id,
        "customer_id": row.customer_id,
        "customer_name": customer_names.get(row.customer_id) if row.customer_id else None,
        "product_id": row.product_id,
        "inventory_code": row.inventory_code,
        "order_no": row.order_no,
        "product_name": row.product_name,
        "item_type": row.item_type,
        "quantity": float(quantity) if quantity is not None else None,
        "inventory_lot_id": row.inventory_lot_id,
        "lot_number": linked_lot.lot_number if linked_lot is not None else None,
        "official_inventory": linked_lot is not None,
        "quantity_available": (
            linked_lot.quantity_available if linked_lot is not None else None
        ),
        "quantity_reserved": (
            linked_lot.quantity_reserved if linked_lot is not None else None
        ),
        "unit": row.unit,
        "match_status": row.match_status,
        "remarks": row.remarks,
        "created_at": utc_naive_to_api(row.created_at),
        "updated_at": utc_naive_to_api(row.updated_at) if row.updated_at else None,
    }


def _floor3_pallet_dict(
    row: InventoryPallet,
    *,
    visible_customer_ids: set[int] | None,
    customer_names: dict[int, str],
) -> dict:
    all_items = list(row.items)
    visible_items = [
        item
        for item in all_items
        if _floor3_item_visible(item, visible_customer_ids)
    ]
    if visible_customer_ids is not None and len(visible_items) != len(all_items):
        # A mixed or fully hidden pallet must not disclose pallet identifiers,
        # free-text remarks, item counts, or even its visible subset.  The
        # surrounding location payload already communicates neutral occupancy.
        return {"access_restricted": True}
    total_quantity = sum(
        (
            Decimal(
                int(item.inventory_lot.quantity_available or 0)
                + int(item.inventory_lot.quantity_reserved or 0)
            )
            if item.inventory_lot is not None
            else item.quantity
        )
        for item in visible_items
    )
    return {
        "id": row.id,
        "pallet_code": row.pallet_code,
        "location_id": row.location_id,
        "status": row.status,
        "is_current": row.is_current,
        "needs_relocation": row.needs_relocation,
        "remarks": row.remarks,
        "version": getattr(row, "version", None),
        "item_count": len(all_items),
        "visible_item_count": len(visible_items),
        "hidden_item_count": len(all_items) - len(visible_items),
        "total_quantity": float(total_quantity),
        "items": [
            _floor3_item_dict(item, customer_names) for item in visible_items
        ],
        "created_at": utc_naive_to_api(row.created_at),
        "updated_at": utc_naive_to_api(row.updated_at) if row.updated_at else None,
        "closed_at": (
            beijing_naive_to_api(row.closed_at) if row.closed_at else None
        ),
    }


def _floor3_layout_dict(row: Floor3LocationLayout | None) -> dict | None:
    if row is None:
        return None
    return {
        "location_id": row.location_id,
        "left_pct": float(row.left_pct),
        "top_pct": float(row.top_pct),
        "width_pct": float(row.width_pct),
        "height_pct": float(row.height_pct),
        "z_index": row.z_index,
        "version": row.version,
        "source_type": row.source_type,
        "layout_kind": row.layout_kind,
        "updated_at": (
            beijing_naive_to_api(row.updated_at) if row.updated_at else None
        ),
    }


def _floor3_location_dict(
    row: WarehouseLocation,
    *,
    pallet: InventoryPallet | None,
    visible_customer_ids: set[int] | None,
    customer_names: dict[int, str],
    projection_context: Mapping[str, object] | None = None,
) -> dict:
    payload = _location_dict(row, projection_context)
    payload["layout"] = _floor3_layout_dict(row.floor3_layout)
    payload["current_pallet"] = (
        _floor3_pallet_dict(
            pallet,
            visible_customer_ids=visible_customer_ids,
            customer_names=customer_names,
        )
        if pallet is not None
        else None
    )
    payload["occupancy_status"] = "occupied" if pallet is not None else "empty"
    return payload


def _floor3_customer_names(
    db: Session,
    pallets: list[InventoryPallet],
) -> dict[int, str]:
    ids = {
        item.customer_id
        for pallet in pallets
        for item in pallet.items
        if item.customer_id is not None
    }
    if not ids:
        return {}
    return dict(
        db.execute(
            select(Customer.id, Customer.name).where(Customer.id.in_(ids))
        ).all()
    )


def _require_floor3_pallet_customer_access(
    db: Session,
    pallet: InventoryPallet,
    user: User,
) -> None:
    visible_customer_ids = _visible_customer_ids(user, db)
    if visible_customer_ids is None:
        return
    inaccessible = [
        item
        for item in pallet.items
        if not _floor3_item_visible(item, visible_customer_ids)
    ]
    if inaccessible:
        raise HTTPException(
            status_code=403,
            detail="当前栈板包含无权访问或客户归属异常的内容",
        )


def _require_floor3_item_customer_access(
    db: Session,
    payload: Floor3PalletItemPayload,
    user: User,
) -> None:
    customer_id = payload.customer_id
    if payload.product_id is not None:
        product = db.get(Product, payload.product_id)
        if product is None:
            raise HTTPException(status_code=404, detail="产品不存在")
        customer_id = product.customer_id
        if payload.customer_id is not None and payload.customer_id != customer_id:
            raise HTTPException(status_code=409, detail="所选产品不属于当前客户")
    if customer_id is None and _visible_customer_ids(user, db) is not None:
        raise HTTPException(
            status_code=403,
            detail="当前账号只能登记已授权客户，待匹配内容也必须先选择客户",
        )
    if customer_id is not None:
        require_customer_access(customer_id, user, db)


def _floor3_log(
    db: Session,
    *,
    request: Request,
    user: User,
    action: str,
    pallet: InventoryPallet,
    description: str,
    details: dict,
) -> None:
    db.add(
        OperationLog(
            user_id=user.id,
            username=user.username,
            role=user.role,
            action=action,
            resource=f"warehouse/floor3/pallets/{pallet.id}",
            entity_type="inventory_pallet",
            entity_id=pallet.id,
            description=description,
            details=json.dumps(details, ensure_ascii=False, default=str),
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )
    )


def _floor3_layout_log(
    db: Session,
    *,
    request: Request,
    user: User,
    action: str,
    location: WarehouseLocation,
    description: str,
    details: dict,
) -> None:
    db.add(
        OperationLog(
            user_id=user.id,
            username=user.username,
            role=user.role,
            action=action,
            resource=f"warehouse/floor3/layout/slots/{location.id}",
            entity_type="floor3_location_layout",
            entity_id=location.floor3_layout.id if location.floor3_layout else None,
            description=description,
            details=json.dumps(details, ensure_ascii=False, default=str),
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )
    )


def _lot_query(*, require_formal_location: bool = True):
    query = select(InventoryLot)
    if require_formal_location:
        query = query.where(
            InventoryLot.warehouse_location_id.in_(
                select(WarehouseLocation.id).where(
                    _formal_inventory_location_condition()
                )
            )
        )
    return query.options(
        selectinload(InventoryLot.location).selectinload(
            WarehouseLocation.address_aliases
        ),
        selectinload(InventoryLot.location)
        .selectinload(WarehouseLocation.address_area)
        .selectinload(WarehouseArea.floor),
        selectinload(InventoryLot.finished_detail),
        selectinload(InventoryLot.semi_finished_detail),
        selectinload(InventoryLot.allowed_products).selectinload(
            SemiFinishedLotAllowedProduct.product
        ),
        selectinload(InventoryLot.pallet_item).selectinload(
            InventoryPalletItem.pallet
        ),
    )


def _visible_lot_condition(visible_customer_ids: set[int]):
    finished_lot_ids = (
        select(FinishedGoodsInventoryDetail.inventory_lot_id)
        .join(Product, Product.id == FinishedGoodsInventoryDetail.product_id)
        .outerjoin(
            InventoryPalletItem,
            InventoryPalletItem.inventory_lot_id
            == FinishedGoodsInventoryDetail.inventory_lot_id,
        )
        .where(
            FinishedGoodsInventoryDetail.owner_customer_id.in_(
                visible_customer_ids
            ),
            Product.customer_id
            == FinishedGoodsInventoryDetail.owner_customer_id,
            or_(
                InventoryPalletItem.id.is_(None),
                and_(
                    InventoryPalletItem.customer_id
                    == FinishedGoodsInventoryDetail.owner_customer_id,
                    InventoryPalletItem.product_id
                    == FinishedGoodsInventoryDetail.product_id,
                ),
            ),
        )
    )
    semi_finished_lot_ids = select(
        SemiFinishedInventoryDetail.inventory_lot_id
    ).where(SemiFinishedInventoryDetail.owner_customer_id.in_(visible_customer_ids))
    return or_(
        InventoryLot.id.in_(finished_lot_ids),
        InventoryLot.id.in_(semi_finished_lot_ids),
    )


def _require_lot_customer_access(
    db: Session,
    lot_id: int,
    user: User,
) -> InventoryLot:
    lot = db.scalar(
        _lot_query(require_formal_location=False).where(InventoryLot.id == lot_id)
    )
    if lot is None:
        # Fail closed: callers pass the raw id to mutation services, so a lot
        # outside the visible formal-ledger scope must not fall through.
        raise HTTPException(status_code=404, detail="库存批次不存在")
    customer_id = None
    if lot.finished_detail is not None:
        customer_id = lot.finished_detail.owner_customer_id
    elif lot.semi_finished_detail is not None:
        customer_id = lot.semi_finished_detail.owner_customer_id
    if has_unrestricted_customer_access(user, db):
        return lot
    if customer_id is None:
        raise HTTPException(status_code=403, detail="无客户访问权限")
    if lot.finished_detail is not None:
        product_customer_id = db.scalar(
            select(Product.customer_id).where(
                Product.id == lot.finished_detail.product_id
            )
        )
        if product_customer_id != customer_id:
            raise HTTPException(status_code=403, detail="成品库存客户归属异常")
        if lot.pallet_item is not None and (
            lot.pallet_item.customer_id != customer_id
            or lot.pallet_item.product_id != lot.finished_detail.product_id
        ):
            raise HTTPException(status_code=403, detail="三楼栈板客户归属异常")
    require_customer_access(customer_id, user, db)
    return lot


def _lot_dict(
    row: InventoryLot,
    time_archive: dict | None = None,
    *,
    location_projection_context: Mapping[str, object] | None = None,
) -> dict:
    from app.services.warehouse_display_units import lot_display_unit
    warning = inventory_age_warning(row)
    pallet_item = row.pallet_item
    pallet = pallet_item.pallet if pallet_item is not None else None
    detail: dict = {}
    if row.finished_detail:
        item = row.finished_detail
        detail = {
            "owner_customer_id": item.owner_customer_id,
            "owner_customer_name": item.owner_customer_name_snapshot,
            "is_general": item.is_general,
            "product_id": item.product_id,
            "inventory_code": item.inventory_code_snapshot,
            "product_name": item.product_name_snapshot,
            "box_type": item.box_type_snapshot,
            "length_mm": item.length_mm,
            "width_mm": item.width_mm,
            "height_mm": item.height_mm,
            "material_code": item.material_code_snapshot,
            "flute_type": item.flute_type_snapshot,
        }
    elif row.semi_finished_detail:
        item = row.semi_finished_detail
        allowed_products = [
            {
                "id": binding.product_id,
                "product_code": binding.product.product_code,
                "product_name": binding.product.product_name,
            }
            for binding in row.allowed_products
            if binding.product is not None
        ]
        detail = {
            "supplier_name": item.supplier_name,
            "owner_customer_id": item.owner_customer_id,
            "owner_customer_name": item.owner_customer_name_snapshot,
            "customer_generic_eligible": bool(item.customer_generic_eligible),
            "internal_name": item.internal_name,
            "material_id": item.material_id,
            "material_code": item.material_code_snapshot,
            "normalized_material_code": item.normalized_material_code,
            "layer_count": item.layer_count,
            "flute_type": item.flute_type,
            "board_length_mm": item.board_length_mm,
            "board_width_mm": item.board_width_mm,
            "component_type": item.component_type,
            "pieces_per_box": item.pieces_per_box,
            "stock_yield_per_sheet": item.stock_yield_per_sheet,
            "sheet_type": item.sheet_type,
            "crease_type": item.crease_type,
            "crease_left_mm": item.crease_left_mm,
            "crease_middle_mm": item.crease_middle_mm,
            "crease_right_mm": item.crease_right_mm,
            "cutting_note": item.cutting_note,
            "inventory_display_name": (
                item.internal_name
                or "客户通用纸板备料"
                if item.customer_generic_eligible
                else "客户专用纸板备料"
                if item.owner_customer_id is not None
                else "通用半成品片料"
            ),
            "allowed_products": allowed_products,
        }
    return {
        "id": row.id,
        "lot_number": row.lot_number,
        "inventory_type": row.inventory_type,
        "location": _location_dict(
            row.location,
            location_projection_context,
        ),
        "quantity_available": row.quantity_available,
        "quantity_reserved": row.quantity_reserved,
        "quantity_consumed": row.quantity_consumed,
        "quantity_damaged": row.quantity_damaged,
        "quantity_scrapped": row.quantity_scrapped,
        "unit": row.unit,
        "display_unit": lot_display_unit(row),
        "subkit_role": "component" if row.source_ref_type == "subkit_receipt" else "kit" if row.source_ref_type in ("subkit_conversion", "bom_assembly") else None,
        "status": row.status,
        "source_type": row.source_type,
        "stock_date": row.stock_date,
        "stock_date_accuracy": row.stock_date_accuracy,
        "stock_date_original_text": row.stock_date_original_text,
        "last_movement_at": utc_naive_to_api(row.last_movement_at),
        "time_archive": time_archive,
        "version": row.version,
        "remarks": row.remarks,
        "floor3_binding": (
            {
                "pallet_id": pallet.id,
                "pallet_code": pallet.pallet_code,
                "pallet_version": pallet.version,
                "location_id": pallet.location_id,
            }
            if pallet is not None and pallet.is_current
            else None
        ),
        "age_days": warning.days,
        "age_warning_level": warning.level,
        "age_warning_text": warning.text,
        "detail": detail,
    }


def _lot_dict_for_db(
    db: Session,
    row: InventoryLot,
    time_archive: dict | None = None,
) -> dict:
    """Serialize one public lot response with its canonical map context."""

    context = (
        load_warehouse_location_projection_contexts(db, [row.location]).get(
            int(row.warehouse_location_id),
            {},
        )
        if row.location is not None and row.warehouse_location_id is not None
        else None
    )
    return _lot_dict(
        row,
        time_archive=time_archive,
        location_projection_context=context,
    )


def _movement_dict(
    row: InventoryMovement,
    *,
    include_sensitive_details: bool = True,
) -> dict:
    return {
        "id": row.id,
        "movement_number": row.movement_number,
        "inventory_lot_id": row.inventory_lot_id,
        "lot_number": row.lot.lot_number if row.lot else None,
        "inventory_type": row.lot.inventory_type if row.lot else None,
        "movement_type": row.movement_type,
        "quantity": row.quantity,
        "unit": row.unit,
        "before_available": row.before_available,
        "after_available": row.after_available,
        "before_reserved": row.before_reserved,
        "after_reserved": row.after_reserved,
        "before_consumed": row.before_consumed,
        "after_consumed": row.after_consumed,
        "before_damaged": row.before_damaged,
        "after_damaged": row.after_damaged,
        "before_scrapped": row.before_scrapped,
        "after_scrapped": row.after_scrapped,
        "reason": row.reason if include_sensitive_details else None,
        "operator_id": row.operator_id,
        "created_at": utc_naive_to_api(row.created_at),
    }


def _reservation_dict(
    row: InventoryReservation,
    db: Session,
    *,
    include_sensitive_details: bool = True,
) -> dict:
    from app.services.delivery_quantities import requirement_amount, requirement_denominator
    denominator = requirement_denominator(row)
    requirement_fields = {}
    for field in ('credited_requirement_quantity', 'consumed_requirement_quantity', 'released_requirement_quantity'):
        raw = getattr(row, field)
        value = requirement_amount(row, field)
        requirement_fields[field] = None if raw is None else (int(value) if value.denominator == 1 else float(value))
        requirement_fields[field + '_numerator'] = raw
    item = db.get(OrderItem, row.order_item_id) if row.order_item_id else None
    order = db.get(Order, row.order_id) if row.order_id else None
    lot = db.get(InventoryLot, row.inventory_lot_id)
    customer = db.get(Customer, order.customer_id) if order else None
    warning_codes: list[str] = []
    if row.warning_codes:
        try:
            parsed = json.loads(row.warning_codes)
            warning_codes = parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            warning_codes = [row.warning_codes]
    return {
        "id": row.id,
        "reservation_number": row.reservation_number,
        "reservation_type": row.reservation_type,
        "inventory_lot_id": row.inventory_lot_id,
        "lot_number": lot.lot_number if lot else None,
        "order_id": row.order_id,
        "order_item_id": row.order_item_id,
        "order_number": order.order_number if order else None,
        "customer_po": order.customer_po if order else None,
        "customer_name": customer.name if customer else None,
        "product_name": item.snapshot_product_name if item else None,
        "reserved_stock_quantity": row.reserved_stock_quantity,
        **requirement_fields,
        "requirement_quantity_denominator": denominator,
        "consumed_stock_quantity": row.consumed_stock_quantity,
        "released_stock_quantity": row.released_stock_quantity,
        "remaining_reserved_stock_quantity": (
            row.reserved_stock_quantity
            - row.consumed_stock_quantity
            - row.released_stock_quantity
        ),
        "semi_requirement_id": row.semi_requirement_id,
        "match_rule_id": row.match_rule_id,
        "reservation_group_key": row.reservation_group_key,
        "status": row.status,
        "warning_codes": warning_codes,
        "reserved_at": utc_naive_to_api(row.reserved_at) if row.reserved_at else None,
        "released_at": utc_naive_to_api(row.released_at) if row.released_at else None,
        "release_reason": (
            row.release_reason if include_sensitive_details else None
        ),
    }


def _semi_requirement_dict(row: OrderItemSemiRequirement) -> dict:
    return {
        "id": row.id,
        "order_item_id": row.order_item_id,
        "customer_id": row.customer_id,
        "component_type": row.component_type,
        "board_length_mm": row.board_length_mm,
        "board_width_mm": row.board_width_mm,
        "material_code": row.material_code_snapshot,
        "normalized_material_code": row.normalized_material_code,
        "flute_type": row.flute_type,
        "pieces_per_box": row.pieces_per_box,
        "stock_yield_per_sheet": row.stock_yield_per_sheet,
        "required_piece_quantity": row.required_piece_quantity,
    }


def _semi_candidate_dict(
    row: SemiFinishedCandidate,
    projection_context: Mapping[str, object] | None = None,
) -> dict:
    lot = row.lot
    detail = lot.semi_finished_detail
    session = object_session(lot)
    profile = goods_profile(session, lot) if session is not None else None
    customer_bound = bool(profile.get("scope") == "customers" and profile.get("customer_ids")) if profile else bool(detail.owner_customer_id)
    handling_stage = (
        "requisition"
        if row.cut_plan
        else "order"
        if row.selectable
        else "review"
    )
    return {
        "lot_id": lot.id,
        "lot_number": lot.lot_number,
        "version": lot.version,
        "source": row.source,
        "match_rule_id": row.match_rule_id,
        "available_stock_quantity": row.available_stock_quantity,
        "deductible_requirement_quantity": row.deductible_requirement_quantity,
        "warehouse_location": _location_dict(lot.location, projection_context),
        "customer_id": detail.owner_customer_id,
        "customer_bound": customer_bound,
        "applicability_scope": (profile or {}).get("scope", "customers" if detail.owner_customer_id else "unconfirmed"),
        "applicable_customer_ids": (profile or {}).get("customer_ids", [detail.owner_customer_id] if detail.owner_customer_id else []),
        "customer_name": detail.owner_customer_name_snapshot,
        "customer_generic_eligible": bool(detail.customer_generic_eligible),
        "internal_name": detail.internal_name,
        "processing": (profile or {}).get("processing"),
        "board_length_mm": detail.board_length_mm,
        "board_width_mm": detail.board_width_mm,
        "material_code": detail.material_code_snapshot,
        "normalized_material_code": detail.normalized_material_code,
        "flute_type": detail.flute_type,
        "component_type": detail.component_type,
        "pieces_per_box": detail.pieces_per_box,
        "stock_yield_per_sheet": row.cut_plan["yield_factor"] if row.cut_plan else detail.stock_yield_per_sheet,
        "cut_plan": row.cut_plan,
        "handling_stage": handling_stage,
        "handling_reason": (
            "需要在报料中确认分切方案"
            if row.cut_plan
            else "当前订单可直接采用"
            if row.selectable
            else "库存用途或适用性待核对"
        ),
        "requires_requisition_cut_plan": bool(row.cut_plan),
        "selectable": row.selectable,
        "layer_count": detail.layer_count,
        "sheet_type": detail.sheet_type,
        "crease_type": detail.crease_type,
        "crease_left_mm": detail.crease_left_mm,
        "crease_middle_mm": detail.crease_middle_mm,
        "crease_right_mm": detail.crease_right_mm,
        "direct_deduction_eligible": row.direct_deduction_eligible,
        "automatic_recommendation": row.automatic_recommendation and customer_bound,
        "match_reason": row.match_reason,
        "match_score": row.match_score,
        "recommendation_tier": row.recommendation_tier,
        "dimension_distance": row.dimension_distance,
        "signature_differences": list(row.signature_differences),
        "warning_codes": list(row.warning_codes),
        "warning_messages": list(row.warning_messages),
    }


def _visible_finished_candidate_lots(
    rows: list[InventoryLot], user: User, db: Session
) -> list[InventoryLot]:
    visible_customer_ids = _visible_customer_ids(user, db)
    if visible_customer_ids is None:
        return rows
    lot_ids = [lot.id for lot in rows]
    if not lot_ids:
        return []
    visible_lot_ids = set(
        db.scalars(
            select(FinishedGoodsInventoryDetail.inventory_lot_id)
            .join(Product, Product.id == FinishedGoodsInventoryDetail.product_id)
            .where(
                FinishedGoodsInventoryDetail.inventory_lot_id.in_(lot_ids),
                FinishedGoodsInventoryDetail.owner_customer_id.in_(
                    visible_customer_ids
                ),
                Product.customer_id
                == FinishedGoodsInventoryDetail.owner_customer_id,
            )
        ).all()
    )
    return [lot for lot in rows if lot.id in visible_lot_ids]


def _visible_semi_candidates(
    rows: list[SemiFinishedCandidate], user: User, db: Session
) -> list[SemiFinishedCandidate]:
    visible_customer_ids = _visible_customer_ids(user, db)
    if visible_customer_ids is None:
        return rows
    return [
        row
        for row in rows
        if row.lot.semi_finished_detail is not None
        and row.lot.semi_finished_detail.owner_customer_id in visible_customer_ids
    ]


@router.get("/finished/candidates")
def finished_candidates(
    order_item_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(can_view_reservations),
) -> dict:
    _require_order_item_customer_access(db, order_item_id, user)
    try:
        from app.services.warehouse_inventory import finished_stock_customer_capacity
        item = db.get(OrderItem, order_item_id)
        if item is None:
            raise WarehouseInventoryError("订单明细不存在", 404)
        reserved = active_finished_reserved_qty(db, order_item_id)
        rows = finished_inventory_candidates(db, order_item_id)
        rows = _visible_finished_candidate_lots(rows, user, db)
        projection_contexts = load_warehouse_location_projection_contexts(
            db,
            [lot.location for lot in rows],
        )
        employee_names = {
            int(lot.location.id): employee_location_name(
                lot.location,
                area=projection_contexts.get(int(lot.location.id), {}).get("area"),
                floor=projection_contexts.get(int(lot.location.id), {}).get("floor"),
                area_sequence=projection_contexts.get(
                    int(lot.location.id), {}
                ).get("area_sequence"),
            )
            for lot in rows
        }
        return {
            "order_item_id": order_item_id,
            "order_quantity": item.quantity,
            "finished_reserved_quantity": reserved,
            "remaining_requirement": max(item.quantity - reserved, 0),
            "items": [
                {
                    "lot_id": lot.id,
                    "lot_number": lot.lot_number,
                    "version": lot.version,
                    "customer_name": lot.finished_detail.owner_customer_name_snapshot,
                    "is_general": lot.finished_detail.is_general,
                    "inventory_code": lot.finished_detail.inventory_code_snapshot,
                    "product_name": lot.finished_detail.product_name_snapshot,
                    "specification": " × ".join(
                        str(value)
                        for value in (
                            lot.finished_detail.length_mm,
                            lot.finished_detail.width_mm,
                            lot.finished_detail.height_mm,
                        )
                        if value is not None
                    ),
                    "material_display": " / ".join(
                        value
                        for value in (
                            lot.finished_detail.material_code_snapshot,
                            lot.finished_detail.flute_type_snapshot,
                        )
                        if value
                    ),
                    "warehouse_location": (
                        f"{lot.location.location_code} "
                        f"{employee_names[int(lot.location.id)]}"
                    ),
                    "quantity_available": lot.quantity_available,
                    "customer_quantity_available": finished_stock_customer_capacity(db, item, lot),
                    "stock_date": lot.stock_date,
                    "stock_date_accuracy": lot.stock_date_accuracy,
                    "last_movement_at": utc_naive_to_api(lot.last_movement_at),
                    "warning_codes": (
                        ["GENERAL_FINISHED_STOCK"]
                        if lot.finished_detail.is_general
                        else []
                    ),
                    "warning_messages": (
                        ["通用库存，请人工确认是否用于该客户订单。"]
                        if lot.finished_detail.is_general
                        else []
                    ),
                }
                for lot in rows
            ],
        }
    except WarehouseInventoryError as error:
        _handle(error)


@router.post("/finished/reservations")
def create_finished_reservation(
    payload: FinishedReservationPayload,
    db: Session = Depends(get_db),
    user: User = Depends(can_reserve),
) -> dict:
    _require_order_item_customer_access(db, payload.order_item_id, user)
    _require_lot_customer_access(db, payload.inventory_lot_id, user)
    try:
        row = reserve_finished_inventory(
            db,
            operator_id=user.id,
            **payload.model_dump(),
        )
        db.commit()
        return _reservation_dict(row, db)
    except WarehouseInventoryError as error:
        db.rollback()
        _handle(error)


@router.get("/finished/bom-components/{bom_snapshot_id}/candidates")
def finished_bom_component_candidates(
    bom_snapshot_id: int,
    order_item_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(can_view_reservations),
) -> dict:
    _require_order_item_customer_access(db, order_item_id, user)
    try:
        snapshot = db.get(SalesOrderItemBomComponent, bom_snapshot_id)
        if snapshot is None or snapshot.sales_order_item_id != order_item_id:
            raise WarehouseInventoryError("组件快照不属于当前订单明细", 409)
        rows = _visible_finished_candidate_lots(
            finished_inventory_candidates_for_bom_component(
                db, order_item_id=order_item_id, bom_snapshot_id=bom_snapshot_id
            ), user, db
        )
        return {
            "order_item_id": order_item_id,
            "bom_snapshot_id": bom_snapshot_id,
            "component_product_id": snapshot.component_product_id,
            "items": [
                {
                    "lot_id": lot.id, "lot_number": lot.lot_number,
                    "version": lot.version, "quantity_available": lot.quantity_available,
                    "is_general": lot.finished_detail.is_general,
                    "warning_codes": (["GENERAL_FINISHED_STOCK"] if lot.finished_detail.is_general else []),
                }
                for lot in rows
            ],
        }
    except WarehouseInventoryError as error:
        _handle(error)


@router.post("/finished/bom-components/reservations")
def create_bom_component_finished_reservation(
    payload: BomComponentFinishedReservationPayload,
    db: Session = Depends(get_db),
    user: User = Depends(can_reserve),
) -> dict:
    _require_order_item_customer_access(db, payload.order_item_id, user)
    _require_lot_customer_access(db, payload.inventory_lot_id, user)
    try:
        row = reserve_finished_inventory_for_bom_component(
            db, operator_id=user.id, **payload.model_dump()
        )
        db.commit()
        return _reservation_dict(row, db)
    except WarehouseInventoryError as error:
        db.rollback()
        _handle(error)


@router.post("/finished/bom-components/{bom_snapshot_id}/auto-cover")
def auto_cover_bom_component_inventory(
    bom_snapshot_id: int,
    payload: BomComponentAutoCoverPayload,
    db: Session = Depends(get_db),
    user: User = Depends(can_reserve),
) -> dict:
    """One-click safe coverage: dedicated finished first, then exact semi stock.

    General stock and signature overrides remain outside this shortcut because
    those facts require an explicit warning review. The button itself is the
    user's single confirmation for exact, customer-owned matches.
    """
    _require_order_item_customer_access(db, payload.order_item_id, user)
    snapshot = db.get(SalesOrderItemBomComponent, bom_snapshot_id)
    if (
        snapshot is None
        or snapshot.sales_order_item_id != payload.order_item_id
    ):
        raise HTTPException(status_code=404, detail="本订单组件快照不存在")
    try:
        item = db.get(OrderItem, payload.order_item_id)
        order = db.get(Order, item.order_id) if item is not None else None
        if item is None or order is None:
            raise WarehouseInventoryError("订单明细不存在", 404)
        component_type = _bom_snapshot_component_type(
            snapshot,
            payload.component_type,
        )
        physical_facts = _bom_snapshot_physical_facts(
            snapshot,
            component_type,
        )
        required = (
            component_effective_required_piece_qty(db, snapshot)
            * _bom_snapshot_physical_pieces_per_component(
                snapshot,
                component_type,
            )
        )

        # Prefer formal finished components. Only the current customer's
        # dedicated stock is eligible for this no-dialog shortcut.
        finished_added = 0
        physical_pieces_per_component = (
            _bom_snapshot_physical_pieces_per_component(
                snapshot,
                component_type,
            )
        )
        for lot in finished_inventory_candidates_for_bom_component(
            db,
            order_item_id=item.id,
            bom_snapshot_id=snapshot.id,
        ):
            detail = lot.finished_detail
            if (
                detail is None
                or detail.is_general
                or detail.owner_customer_id != order.customer_id
            ):
                continue
            coverage = component_inventory_coverage(
                db,
                snapshot.id,
                component_type=component_type,
            )
            remaining = max(required - coverage["total_piece_quantity"], 0)
            quantity = min(
                int(lot.quantity_available or 0),
                remaining // physical_pieces_per_component,
            )
            if quantity <= 0:
                break
            reserve_finished_inventory_for_bom_component(
                db,
                order_item_id=item.id,
                bom_snapshot_id=snapshot.id,
                inventory_lot_id=lot.id,
                quantity=quantity,
                expected_version=lot.version,
                operator_id=user.id,
                idempotency_key=f"{payload.idempotency_key}:f:{lot.id}",
                warning_acknowledged_codes=[],
            )
            finished_added += quantity

        # Then use only exact, customer-owned semi-finished matches. Existing
        # matching and CAS services still perform every authorization check.
        coverage = component_inventory_coverage(
            db,
            snapshot.id,
            component_type=component_type,
        )
        remaining = max(required - coverage["total_piece_quantity"], 0)
        semi_added = 0
        semi_signature_complete = (
            int(physical_facts["board_length_mm"] or 0) > 0
            and int(physical_facts["board_width_mm"] or 0) > 0
            and bool(str(snapshot.snapshot_component_material or "").strip())
            and bool(str(snapshot.snapshot_component_flute_type or "").strip())
        )
        if remaining > 0 and semi_signature_complete:
            requirement = db.scalar(
                select(OrderItemSemiRequirement).where(
                    OrderItemSemiRequirement.sales_order_item_bom_component_id
                    == snapshot.id,
                    OrderItemSemiRequirement.component_type == component_type,
                )
            )
            from app.services.bom_physical_quantities import resolve_bom_sheet_yield
            from app.services.semi_finished_inventory import semi_finished_candidates_for_bom_component
            try:
                yield_per_sheet = resolve_bom_sheet_yield(snapshot, strict=True).yield_per_sheet
            except ValueError as error:
                raise WarehouseInventoryError(str(error), 409) from error
            if requirement is None:
                preview_candidates = semi_finished_candidates_for_bom_component(
                    db, snapshot_id=snapshot.id, component_type=component_type)
            else:
                preview_candidates = semi_finished_inventory_candidates(
                    db, requirement.id
                )
            safe_candidates = [
                row
                for row in preview_candidates
                if (
                    row.lot.semi_finished_detail is not None
                    and row.lot.semi_finished_detail.owner_customer_id
                    == order.customer_id
                    and not row.signature_differences
                    and row.source in {"signature", "learned"}
                    and safe_physical_board_facts_match(
                        row.lot.semi_finished_detail,
                        supplier_name=snapshot.snapshot_component_supplier_name,
                        layer_count=snapshot.snapshot_component_layer_count,
                        crease_type=physical_facts["crease_type"],
                        crease_left_mm=physical_facts["crease_left_mm"],
                        crease_middle_mm=physical_facts["crease_middle_mm"],
                        crease_right_mm=physical_facts["crease_right_mm"],
                    )
                )
            ]
            # No match means no unexplained empty inventory "draft" is left
            # behind; the normal requisition remains the only user task.
            if requirement is None and safe_candidates:
                requirement = save_order_item_semi_requirement(
                    db,
                    order_item_id=item.id,
                    sales_order_item_bom_component_id=snapshot.id,
                    component_type=component_type,
                    board_length_mm=int(physical_facts["board_length_mm"]),
                    board_width_mm=int(physical_facts["board_width_mm"]),
                    material_code=str(snapshot.snapshot_component_material),
                    flute_type=str(snapshot.snapshot_component_flute_type),
                    pieces_per_box=physical_pieces_per_component,
                    stock_yield_per_sheet=yield_per_sheet,
                    required_piece_quantity=required,
                    operator_id=user.id,
                )
            if requirement is not None and safe_candidates:
                result = reserve_semi_finished_inventory(
                    db,
                    requirement_id=requirement.id,
                    requested_requirement_quantity=remaining,
                    lots=[
                        SemiFinishedLotVersion(
                            lot_id=row.lot.id,
                            expected_version=row.lot.version,
                        )
                        for row in safe_candidates
                    ],
                    operator_id=user.id,
                    idempotency_key=(
                        f"{payload.idempotency_key}:s:{component_type}"
                    ),
                    confirmed=True,
                    override=False,
                    warning_acknowledged_codes=[],
                )
                semi_added = result.allocated_requirement_quantity

        coverage = component_inventory_coverage(
            db,
            snapshot.id,
            component_type=component_type,
        )
        remaining = max(required - coverage["total_piece_quantity"], 0)
        db.commit()
        if finished_added or semi_added:
            message = (
                f"已自动使用成品 {finished_added} 件、半成品 {semi_added} 件；"
                f"仍需报料 {remaining} 件"
            )
        else:
            message = "没有找到可安全自动匹配的客户专用库存，待报料数量未变"
        return {
            "finished_reserved_piece_qty": coverage["finished_piece_quantity"],
            "semi_finished_reserved_piece_qty": coverage["semi_piece_quantity"],
            "inventory_covered_piece_qty": coverage["total_piece_quantity"],
            "remaining_required_piece_qty": remaining,
            "message": message,
        }
    except WarehouseInventoryError as error:
        db.rollback()
        _handle(error)
    except IntegrityError as error:
        db.rollback()
        _handle_integrity(error)


@router.get("/reservations")
def list_reservations(
    order_item_id: int | None = None,
    inventory_lot_id: int | None = None,
    status_filter: str | None = Query(default=None, alias="status"),
    db: Session = Depends(get_db),
    user: User = Depends(can_view_reservations),
) -> dict:
    query = select(InventoryReservation).where(
        InventoryReservation.reservation_type == "finished_order"
    )
    if order_item_id:
        _require_order_item_customer_access(db, order_item_id, user)
        query = query.where(InventoryReservation.order_item_id == order_item_id)
    if inventory_lot_id:
        _require_lot_customer_access(db, inventory_lot_id, user)
        query = query.where(
            InventoryReservation.inventory_lot_id == inventory_lot_id
        )
    if status_filter:
        query = query.where(InventoryReservation.status == status_filter)
    visible_customer_ids = _visible_customer_ids(user, db)
    if visible_customer_ids is not None:
        query = query.join(
            Order, Order.id == InventoryReservation.order_id
        ).join(
            InventoryLot,
            InventoryLot.id == InventoryReservation.inventory_lot_id,
        ).where(
            Order.customer_id.in_(visible_customer_ids),
            _visible_lot_condition(visible_customer_ids),
        )
    rows = db.scalars(
        query.order_by(InventoryReservation.id.desc()).limit(500)
    ).all()
    return {"items": [_reservation_dict(row, db) for row in rows]}


@router.post("/reservations/{reservation_id}/release")
def release_reservation(
    reservation_id: int,
    payload: ReleaseReservationPayload,
    db: Session = Depends(get_db),
    user: User = Depends(can_reserve),
) -> dict:
    _require_reservation_customer_access(db, reservation_id, user)
    try:
        row = release_finished_reservation(
            db,
            reservation_id=reservation_id,
            operator_id=user.id,
            release_reason=payload.release_reason,
            idempotency_key=payload.idempotency_key,
        )
        db.commit()
        return _reservation_dict(row, db)
    except WarehouseInventoryError as error:
        db.rollback()
        _handle(error)


@router.get("/finished/products/{product_id}/candidates")
def finished_product_candidates(
    product_id: int,
    customer_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(can_view_reservations),
) -> dict:
    require_customer_access(customer_id, user, db)
    try:
        rows = finished_inventory_candidates_for_product(
            db,
            customer_id=customer_id,
            product_id=product_id,
        )
        rows = _visible_finished_candidate_lots(rows, user, db)
        from app.services.delivery_quantities import product_basis, require_physical_stock, available_customer_quantity, QuantityContractError
        try:
            basis = product_basis(db.get(Product, product_id))
        except QuantityContractError as error:
            raise WarehouseInventoryError(str(error), 409) from error
        verified = []
        for lot in rows:
            try:
                require_physical_stock(lot, basis)
            except QuantityContractError:
                continue
            verified.append(lot)
        rows = verified
        projection_contexts = load_warehouse_location_projection_contexts(
            db,
            [lot.location for lot in rows if lot.location is not None],
        )
        return {
            "customer_id": customer_id,
            "product_id": product_id,
            "items": [
                {
                    "lot_id": lot.id,
                    "lot_number": lot.lot_number,
                    "version": lot.version,
                    "is_general": lot.finished_detail.is_general,
                    "quantity_available": lot.quantity_available,
                    "customer_quantity_available": available_customer_quantity(basis, lot.quantity_available),
                    "quantity_contract": basis,
                    "warehouse_location": _location_dict(
                        lot.location,
                        (
                            projection_contexts.get(int(lot.warehouse_location_id))
                            if lot.warehouse_location_id is not None
                            else None
                        ),
                    ),
                    "warning_codes": (
                        ["GENERAL_FINISHED_STOCK"]
                        if lot.finished_detail.is_general
                        else []
                    ),
                    "warning_messages": (
                        ["通用成品库存，必须人工确认后才能预占。"]
                        if lot.finished_detail.is_general
                        else []
                    ),
                }
                for lot in rows
            ],
        }
    except WarehouseInventoryError as error:
        _handle(error)


@router.put("/semi-finished/order-items/{order_item_id}/requirements")
def upsert_semi_requirement(
    order_item_id: int,
    payload: SemiRequirementPayload,
    db: Session = Depends(get_db),
    user: User = Depends(can_reserve),
) -> dict:
    _require_order_item_customer_access(db, order_item_id, user)
    try:
        row = save_order_item_semi_requirement(
            db,
            order_item_id=order_item_id,
            operator_id=user.id,
            **payload.model_dump(),
        )
        db.commit()
        return _semi_requirement_dict(row)
    except WarehouseInventoryError as error:
        db.rollback()
        _handle(error)
    except IntegrityError as error:
        db.rollback()
        _handle_integrity(error)


@router.put("/semi-finished/bom-components/{bom_snapshot_id}/requirements")
def upsert_bom_component_semi_requirement(
    bom_snapshot_id: int,
    payload: BomComponentSemiRequirementPayload,
    db: Session = Depends(get_db),
    user: User = Depends(can_reserve),
) -> dict:
    """Create a component-scoped semi requirement from frozen BOM facts.

    The generated requirement is deliberately an implementation detail: users
    choose a component snapshot, never the old whole/cover/base identity.
    """
    snapshot = db.get(SalesOrderItemBomComponent, bom_snapshot_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="组件快照不存在")
    _require_order_item_customer_access(db, snapshot.sales_order_item_id, user)
    try:
        component_type = _bom_snapshot_component_type(
            snapshot,
            payload.component_type,
        )
        physical_facts = _bom_snapshot_physical_facts(
            snapshot,
            component_type,
        )
        coverage = component_inventory_coverage(
            db,
            snapshot.id,
            component_type=component_type,
        )
        required = (
            component_effective_required_piece_qty(db, snapshot)
            * _bom_snapshot_physical_pieces_per_component(
                snapshot,
                component_type,
            )
        )
        physical_pieces_per_component = (
            _bom_snapshot_physical_pieces_per_component(
                snapshot,
                component_type,
            )
        )
        if required <= coverage["total_piece_quantity"]:
            raise WarehouseInventoryError("该组件已由库存全额覆盖", 409)
        row = save_order_item_semi_requirement(
            db,
            order_item_id=snapshot.sales_order_item_id,
            sales_order_item_bom_component_id=snapshot.id,
            component_type=component_type,
            board_length_mm=int(physical_facts["board_length_mm"] or 0),
            board_width_mm=int(physical_facts["board_width_mm"] or 0),
            material_code=str(snapshot.snapshot_component_material or "").strip(),
            flute_type=str(snapshot.snapshot_component_flute_type or "").strip(),
            pieces_per_box=physical_pieces_per_component,
            stock_yield_per_sheet=payload.stock_yield_per_sheet,
            required_piece_quantity=required,
            operator_id=user.id,
        )
        db.commit()
        return _semi_requirement_dict(row)
    except WarehouseInventoryError as error:
        db.rollback()
        _handle(error)
    except IntegrityError as error:
        db.rollback()
        _handle_integrity(error)


@router.post("/semi-finished/products/{product_id}/candidates")
def semi_product_candidates(
    product_id: int,
    payload: SemiProductCandidatePayload,
    db: Session = Depends(get_db),
    user: User = Depends(can_view_reservations),
) -> dict:
    require_customer_access(payload.customer_id, user, db)
    try:
        rows = semi_finished_candidates_for_product(
            db,
            product_id=product_id,
            customer_bound_only=True,
            **payload.model_dump(exclude={"stage"}),
        )
        if payload.stage == "order":
            rows = [row for row in rows if row.cut_plan is None]
        rows = _visible_semi_candidates(rows, user, db)
        return {
            "product_id": product_id,
            "items": _semi_candidate_dicts(db, rows),
        }
    except WarehouseInventoryError as error:
        _handle(error)


@router.post("/semi-finished/products/{product_id}/inventory")
def semi_product_inventory_browser(
    product_id: int,
    payload: SemiProductCandidatePayload,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=50),
    db: Session = Depends(get_db),
    user: User = Depends(can_view_reservations),
) -> dict:
    require_customer_access(payload.customer_id, user, db)
    try:
        page_info = {}
        rows = browse_semi_finished_inventory_for_product(
            db,
            page=page, page_size=page_size, page_info=page_info,
            visible_customer_ids=_visible_customer_ids(user, db),
            product_id=product_id,
            **payload.model_dump(exclude={"stage"}),
        )
        if payload.stage == "order":
            rows = [row for row in rows if row.cut_plan is None]
        rows = _visible_semi_candidates(rows, user, db)
        return {
            "product_id": product_id,
            "page":page, "page_size":page_size, "total":page_info["total"],
            "items": _semi_candidate_dicts(db, rows),
        }
    except WarehouseInventoryError as error:
        _handle(error)


@router.get("/semi-finished/requirements/{requirement_id}/candidates")
def semi_requirement_candidates(
    requirement_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(can_view_reservations),
) -> dict:
    _require_requirement_customer_access(db, requirement_id, user)
    try:
        requirement = db.get(OrderItemSemiRequirement, requirement_id)
        if requirement is None:
            raise WarehouseInventoryError("半成品需求不存在", 404)
        credited = active_semi_requirement_credited_quantity(db, requirement.id)
        rows = semi_finished_inventory_candidates(db, requirement.id)
        rows = _visible_semi_candidates(rows, user, db)
        return {
            "requirement": _semi_requirement_dict(requirement),
            "credited_requirement_quantity": credited,
            "remaining_requirement_quantity": max(
                requirement.required_piece_quantity - credited, 0
            ),
            "items": _semi_candidate_dicts(db, rows),
        }
    except WarehouseInventoryError as error:
        _handle(error)


@router.get("/semi-finished/requirements/{requirement_id}/inventory")
def semi_requirement_inventory_browser(
    requirement_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(can_view_reservations),
) -> dict:
    _require_requirement_customer_access(db, requirement_id, user)
    try:
        rows = browse_semi_finished_inventory(db, requirement_id)
        rows = _visible_semi_candidates(rows, user, db)
        return {"items": _semi_candidate_dicts(db, rows)}
    except WarehouseInventoryError as error:
        _handle(error)


@router.post("/semi-finished/requirements/{requirement_id}/confirm-match")
def confirm_semi_requirement_match(
    requirement_id: int,
    payload: SemiMatchConfirmPayload,
    db: Session = Depends(get_db),
    user: User = Depends(can_reserve),
) -> dict:
    _require_requirement_customer_access(db, requirement_id, user)
    _require_lot_customer_access(db, payload.inventory_lot_id, user)
    try:
        result = confirm_semi_finished_match(
            db,
            requirement_id=requirement_id,
            operator_id=user.id,
            **payload.model_dump(),
        )
        db.commit()
        return {
            "rule_id": result.rule.id,
            "product_id": result.mapping.product_id,
            "source": "learned",
            "signature_differences": list(result.signature_differences),
            "warning_codes": list(result.warning_codes),
        }
    except WarehouseInventoryError as error:
        db.rollback()
        _handle(error)
    except IntegrityError as error:
        db.rollback()
        _handle_integrity(error)


@router.post("/semi-finished/requirements/{requirement_id}/reserve")
def reserve_semi_requirement(
    requirement_id: int,
    payload: SemiReservationPayload,
    db: Session = Depends(get_db),
    user: User = Depends(can_reserve),
) -> dict:
    _require_requirement_customer_access(db, requirement_id, user)
    for lot in payload.lots:
        _require_lot_customer_access(db, lot.lot_id, user)
    try:
        result = reserve_semi_finished_inventory(
            db,
            requirement_id=requirement_id,
            requested_requirement_quantity=payload.requested_requirement_quantity,
            lots=[SemiFinishedLotVersion(**row.model_dump()) for row in payload.lots],
            operator_id=user.id,
            idempotency_key=payload.idempotency_key,
            confirmed=payload.confirmed,
            override=payload.override,
            warning_acknowledged_codes=payload.warning_acknowledged_codes,
        )
        db.commit()
        return {
            "requested_requirement_quantity": result.requested_requirement_quantity,
            "allocated_requirement_quantity": result.allocated_requirement_quantity,
            "unallocated_requirement_quantity": result.unallocated_requirement_quantity,
            "reservations": [
                _reservation_dict(row, db) for row in result.reservations
            ],
        }
    except WarehouseInventoryError as error:
        db.rollback()
        _handle(error)
    except IntegrityError as error:
        db.rollback()
        _handle_integrity(error)


@router.get("/semi-finished/requirements/{requirement_id}/reservations")
def list_semi_requirement_reservations(
    requirement_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(can_view_reservations),
) -> dict:
    _require_requirement_customer_access(db, requirement_id, user)
    query = select(InventoryReservation).where(
            InventoryReservation.semi_requirement_id == requirement_id,
            InventoryReservation.reservation_type == "semi_order",
        )
    visible_customer_ids = _visible_customer_ids(user, db)
    if visible_customer_ids is not None:
        query = query.join(
            InventoryLot,
            InventoryLot.id == InventoryReservation.inventory_lot_id,
        ).where(_visible_lot_condition(visible_customer_ids))
    rows = db.scalars(query.order_by(InventoryReservation.id)).all()
    return {"items": [_reservation_dict(row, db) for row in rows]}


@router.post("/semi-finished/reservations/{reservation_id}/release")
def release_semi_reservation(
    reservation_id: int,
    payload: SemiReleasePayload,
    db: Session = Depends(get_db),
    user: User = Depends(can_reserve),
) -> dict:
    _require_reservation_customer_access(db, reservation_id, user)
    try:
        result = release_semi_finished_reservation(
            db,
            reservation_id=reservation_id,
            operator_id=user.id,
            **payload.model_dump(),
        )
        db.commit()
        return _reservation_dict(result.reservation, db)
    except WarehouseInventoryError as error:
        db.rollback()
        _handle(error)
    except IntegrityError as error:
        db.rollback()
        _handle_integrity(error)


@router.post("/semi-finished/reservations/{reservation_id}/consume")
def consume_semi_reservation(
    reservation_id: int,
    payload: SemiConsumePayload,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    _require_reservation_customer_access(db, reservation_id, user)
    try:
        result = consume_semi_finished_reservation(
            db,
            reservation_id=reservation_id,
            operator_id=user.id,
            **payload.model_dump(),
        )
        db.commit()
        response = _reservation_dict(result.reservation, db)
        response["delivery_inventory_allocation_id"] = (
            result.allocation.id if result.allocation else None
        )
        return response
    except WarehouseInventoryError as error:
        db.rollback()
        _handle(error)
    except IntegrityError as error:
        db.rollback()
        _handle_integrity(error)


@router.post("/semi-finished/reservations/{reservation_id}/reverse-consume")
def reverse_semi_reservation_consumption(
    reservation_id: int,
    payload: SemiReverseConsumePayload,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    _require_reservation_customer_access(db, reservation_id, user)
    try:
        result = reverse_semi_finished_consumption(
            db,
            reservation_id=reservation_id,
            operator_id=user.id,
            **payload.model_dump(),
        )
        db.commit()
        return _reservation_dict(result.reservation, db)
    except WarehouseInventoryError as error:
        db.rollback()
        _handle(error)
    except IntegrityError as error:
        db.rollback()
        _handle_integrity(error)


def _semi_product_dimensions(
    product: Product,
    component_type: str,
) -> tuple[int | None, int | None]:
    if component_type == "base":
        return product.base_report_length_mm, product.base_report_width_mm
    return product.report_length_mm, product.report_width_mm


def _semi_product_material_code(product: Product) -> str:
    return (
        product.default_material_code
        or (product.material.code if product.material is not None else None)
        or product.legacy_material_text
        or ""
    ).strip()


def _semi_lot_assignment_response(
    db: Session,
    *,
    lot_id: int,
    keyword: str | None,
    limit: int,
) -> dict:
    lot = db.scalar(_lot_query().where(InventoryLot.id == lot_id))
    if lot is None or lot.inventory_type != "semi_finished":
        raise WarehouseInventoryError("半成品库存批次不存在", 404)
    detail = lot.semi_finished_detail
    if detail is None:
        raise WarehouseInventoryError("半成品库存批次缺少明细", 409)
    if detail.owner_customer_id is None:
        raise WarehouseInventoryError("请先为半成品库存指定归属客户", 409)
    allowed_ids = set(semi_finished_lot_allowed_product_ids(db, lot.id))
    query = (
        select(Product)
        .options(selectinload(Product.material))
        .where(
            Product.customer_id == detail.owner_customer_id,
            Product.is_active.is_(True),
            Product.deleted_at.is_(None),
            func.upper(func.trim(Product.flute_type)) == detail.flute_type,
        )
    )
    text = (keyword or "").strip()
    if text:
        pattern = f"%{text}%"
        query = query.where(
            or_(
                Product.product_code.like(pattern),
                Product.customer_material_code.like(pattern),
                Product.product_name.like(pattern),
                Product.die_cut_path.like(pattern),
            )
        )
    else:
        report_length_column = (
            Product.base_report_length_mm
            if detail.component_type == "base"
            else Product.report_length_mm
        )
        report_width_column = (
            Product.base_report_width_mm
            if detail.component_type == "base"
            else Product.report_width_mm
        )
        query = query.where(
            or_(
                Product.id.in_(allowed_ids),
                and_(
                    report_length_column == detail.board_length_mm,
                    report_width_column == detail.board_width_mm,
                    func.upper(func.trim(Product.flute_type)) == detail.flute_type,
                ),
            )
        )
    products = db.scalars(
        query.order_by(Product.product_code, Product.id).limit(limit)
    ).all()
    items = []
    for product in products:
        report_length, report_width = _semi_product_dimensions(
            product, detail.component_type
        )
        material_code = _semi_product_material_code(product)
        recommended = all(
            (
                report_length == detail.board_length_mm,
                report_width == detail.board_width_mm,
                normalize_material_code(material_code)
                == detail.normalized_material_code,
                (product.flute_type or "").strip().upper() == detail.flute_type,
            )
        )
        items.append(
            {
                "id": product.id,
                "product_code": product.product_code,
                "customer_material_code": product.customer_material_code,
                "product_name": product.product_name,
                "specification": product_dimension_specification(product),
                "report_length_mm": report_length,
                "report_width_mm": report_width,
                "material_code": material_code or None,
                "flute_type": product.flute_type,
                "template_location": (
                    product.mold_tool.rack_location
                    if product.mold_tool is not None
                    else product.die_cut_path
                ),
                "mold_code": (
                    product.mold_tool.mold_code
                    if product.mold_tool is not None
                    else None
                ),
                "assigned": product.id in allowed_ids,
                "recommended": recommended,
            }
        )
    items.sort(
        key=lambda row: (
            not row["assigned"],
            not row["recommended"],
            row["product_code"],
        )
    )
    return {
        "lot_id": lot.id,
        "lot_number": lot.lot_number,
        "customer_id": detail.owner_customer_id,
        "customer_name": detail.owner_customer_name_snapshot,
        "material_code": detail.material_code_snapshot,
        "flute_type": detail.flute_type,
        "board_length_mm": detail.board_length_mm,
        "board_width_mm": detail.board_width_mm,
        "component_type": detail.component_type,
        "binding_scope": "lot",
        "allowed_product_ids": sorted(allowed_ids),
        "version": lot.version,
        # Compatibility alias for clients which have not switched field names yet.
        "assigned_product_ids": sorted(allowed_ids),
        "items": items,
        "mapping_scope": "仅当前半成品库存批次",
    }


@router.get("/lots/{lot_id}/product-assignments")
def get_semi_lot_product_assignments(
    lot_id: int,
    q: str | None = None,
    limit: int = Query(default=200, ge=1, le=500),
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    _require_lot_customer_access(db, lot_id, user)
    try:
        return _semi_lot_assignment_response(
            db, lot_id=lot_id, keyword=q, limit=limit
        )
    except WarehouseInventoryError as error:
        _handle(error)


@router.put("/lots/{lot_id}/product-assignments")
def update_semi_lot_product_assignments(
    lot_id: int,
    payload: SemiProductAssignmentsPayload,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    try:
        lot = replace_semi_finished_lot_allowed_products(
            db,
            inventory_lot_id=lot_id,
            product_ids=payload.product_ids,
            expected_version=payload.expected_version,
            operator_id=user.id,
        )
        db.add(
            OperationLog(
                user_id=user.id,
                username=user.username,
                role=user.role,
                action="UPDATE",
                resource=f"warehouse/semi-lot/{lot_id}/product-assignments",
                entity_type="semi_finished_lot_allowed_product",
                entity_id=lot.id,
                description="更新半成品批次级适用成品款号",
                details=json.dumps(
                    {
                        "lot_id": lot_id,
                        "product_ids": payload.product_ids,
                        "expected_version": payload.expected_version,
                        "version": lot.version,
                        "binding_scope": "lot",
                    },
                    ensure_ascii=False,
                ),
            )
        )
        db.commit()
        return _semi_lot_assignment_response(
            db, lot_id=lot_id, keyword=None, limit=500
        )
    except WarehouseInventoryError as error:
        db.rollback()
        _handle(error)
    except IntegrityError as error:
        db.rollback()
        _handle_integrity(error)


def _floor3_pallet_query():
    return select(InventoryPallet).options(
        selectinload(InventoryPallet.items).selectinload(
            InventoryPalletItem.inventory_lot
        ).selectinload(InventoryLot.finished_detail),
        selectinload(InventoryPallet.items).selectinload(
            InventoryPalletItem.product
        ),
    )


def _floor3_get_pallet(db: Session, pallet_id: int) -> InventoryPallet:
    row = db.scalar(
        _floor3_pallet_query().where(InventoryPallet.id == pallet_id)
    )
    if row is None:
        raise HTTPException(status_code=404, detail="栈板不存在")
    if row.location_id is not None:
        location = db.get(WarehouseLocation, row.location_id)
        if (
            location is None
            or location.warehouse_floor != 3
            or location.source_version not in {"V11", "CURRENT_MAP"}
        ):
            raise HTTPException(status_code=404, detail="三楼实测区域栈板不存在")
    return row


def _floor3_pallet_response(
    db: Session,
    pallet: InventoryPallet,
    user: User,
) -> dict:
    loaded = db.scalar(
        _floor3_pallet_query().where(InventoryPallet.id == pallet.id)
    ) or pallet
    visible_customer_ids = _visible_customer_ids(user, db)
    return _floor3_pallet_dict(
        loaded,
        visible_customer_ids=visible_customer_ids,
        customer_names=_floor3_customer_names(db, [loaded]),
    )


@router.post("/floor3/layout/areas/{area_code}/slots", status_code=201)
def create_floor3_layout_slot(
    area_code: str,
    payload: Floor3LayoutCreateSlotPayload,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    WAREHOUSE_TWIN_LAYOUT_TRANSACTION_LOCK.acquire()
    try:
        _assert_legacy_floor3_write_allowed(db, area_code=area_code)
        location = create_layout_slot(
            db,
            area_code=area_code,
            location_code=payload.location_code,
            location_name=payload.location_name,
            left_pct=payload.left_pct,
            top_pct=payload.top_pct,
            width_pct=payload.width_pct,
            height_pct=payload.height_pct,
            z_index=payload.z_index,
            operator_id=user.id,
            layout_kind="physical_pallet",
        )
        _floor3_layout_log(
            db,
            request=request,
            user=user,
            action="CREATE",
            location=location,
            description="创建三楼互动地图物理栈板位",
            details={"area_code": location.area_code, "location_code": location.location_code},
        )
        db.commit()
        return {
            "location": _location_dict_for_db(db, location),
            "layout": _floor3_layout_dict(location.floor3_layout),
        }
    except Floor3LocationError as error:
        db.rollback()
        raise HTTPException(status_code=error.status_code, detail=str(error)) from error
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(status_code=409, detail="货位编码或布局已存在") from error
    finally:
        WAREHOUSE_TWIN_LAYOUT_TRANSACTION_LOCK.release()


@router.post("/floor3/layout/areas/{area_code}/location-count")
def set_floor3_area_location_count(
    area_code: str,
    payload: Floor3AreaLocationCountPayload,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    WAREHOUSE_TWIN_LAYOUT_TRANSACTION_LOCK.acquire()
    try:
        _assert_legacy_floor3_write_allowed(db, area_code=area_code)
        result = adjust_area_location_count(
            db,
            area_code=area_code,
            target_count=payload.target_count,
            operator_id=user.id,
        )
        actions: list[dict] = []
        for action, rows, description in (
            ("created", result.created, "按区域目标数量自动创建三楼待布局库位"),
            ("enabled", result.enabled, "按区域目标数量重新启用三楼空库位"),
            ("disabled", result.disabled, "按区域目标数量逻辑停用三楼空库位"),
        ):
            for location in rows:
                _floor3_layout_log(
                    db,
                    request=request,
                    user=user,
                    action="CREATE" if action == "created" else "UPDATE",
                    location=location,
                    description=description,
                    details={
                        "area_code": result.area_code,
                        "target_count": result.target_count,
                        "location_code": location.location_code,
                        "location_count_action": action,
                    },
                )
                actions.append(
                    {
                        "action": action,
                        "location": _location_dict_for_db(db, location),
                        "layout": _floor3_layout_dict(location.floor3_layout),
                    }
                )
        db.commit()
        return {
            "area_code": result.area_code,
            "target_count": result.target_count,
            "active_count": result.active_count,
            "created_count": len(result.created),
            "enabled_count": len(result.enabled),
            "disabled_count": len(result.disabled),
            "items": actions,
            "message": (
                "新增库位已生成待布局草稿，请在二维地图中确认位置并保存。"
                if result.created
                else "区域库位数量已按管理员确认结果更新。"
            ),
        }
    except Floor3LocationError as error:
        db.rollback()
        raise HTTPException(status_code=error.status_code, detail=str(error)) from error
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(status_code=409, detail="区域库位编号或布局发生冲突，请刷新后重试") from error
    finally:
        WAREHOUSE_TWIN_LAYOUT_TRANSACTION_LOCK.release()


@router.patch("/floor3/layout/areas/{area_code}")
def patch_floor3_layout_area(
    area_code: str,
    payload: Floor3LayoutAreaPatchPayload,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    WAREHOUSE_TWIN_LAYOUT_TRANSACTION_LOCK.acquire()
    try:
        _assert_legacy_floor3_write_allowed(db, area_code=area_code)
        requested_ids = [slot.location_id for slot in payload.slots]
        before_rows = list(
            db.scalars(
                select(WarehouseLocation)
                .options(selectinload(WarehouseLocation.floor3_layout))
                .where(WarehouseLocation.id.in_(requested_ids))
            ).all()
        )
        before_by_location_id = {
            row.id: _floor3_layout_dict(row.floor3_layout)
            for row in before_rows
            if row.floor3_layout is not None
        }
        layouts = update_layout_area(
            db,
            area_code=area_code,
            slots=[slot.model_dump() for slot in payload.slots],
            operator_id=user.id,
        )
        locations = {
            layout.location_id: db.get(WarehouseLocation, layout.location_id)
            for layout in layouts
        }
        for layout in layouts:
            location = locations[layout.location_id]
            assert location is not None
            _floor3_layout_log(
                db,
                request=request,
                user=user,
                action="UPDATE",
                location=location,
                description="批量更新三楼互动地图布局",
                details={
                    "area_code": area_code.strip().upper(),
                    "before": before_by_location_id.get(layout.location_id),
                    "after": _floor3_layout_dict(layout),
                },
            )
        db.commit()
        return {"items": [_floor3_layout_dict(layout) for layout in layouts]}
    except Floor3LocationError as error:
        db.rollback()
        raise HTTPException(status_code=error.status_code, detail=str(error)) from error
    finally:
        WAREHOUSE_TWIN_LAYOUT_TRANSACTION_LOCK.release()


def _layout_geometry_payload(location: WarehouseLocation) -> dict:
    layout = location.floor3_layout
    if layout is None:
        raise WarehouseAreaActivationError(
            f"货位 {location.location_code} 缺少二维位置，请先核对区域货位",
            status_code=409,
        )
    return {
        "location_id": location.id,
        "left_pct": float(layout.left_pct),
        "top_pct": float(layout.top_pct),
        "width_pct": float(layout.width_pct),
        "height_pct": float(layout.height_pct),
        "z_index": layout.z_index,
        "expected_version": layout.version,
        "layout_kind": layout.layout_kind,
    }


def _location_layout_state(row: WarehouseLocation) -> dict:
    return {
        "location_id": row.id,
        "is_active": row.is_active,
        "placement_status": row.placement_status,
        "source_version": row.source_version,
        "layout": _floor3_layout_dict(row.floor3_layout),
    }


def _occupied_location_ids(db: Session, location_ids: list[int]) -> set[int]:
    if not location_ids:
        return set()
    pallet_ids = set(
        db.scalars(
            select(InventoryPallet.location_id).where(
                InventoryPallet.location_id.in_(location_ids),
                InventoryPallet.is_current.is_(True),
            )
        ).all()
    )
    inventory_ids = set(
        db.scalars(
            select(InventoryLot.warehouse_location_id).where(
                InventoryLot.warehouse_location_id.in_(location_ids),
                InventoryLot.status.in_(("active", "frozen")),
                (
                    InventoryLot.quantity_available
                    + InventoryLot.quantity_reserved
                    + InventoryLot.quantity_damaged
                )
                > 0,
            )
        ).all()
    )
    return {
        int(location_id)
        for location_id in [*pallet_ids, *inventory_ids]
        if location_id is not None
    }


def _assert_legacy_floor3_write_allowed(
    db: Session,
    *,
    area_code: str,
) -> None:
    """Prevent old V11 endpoints from bypassing formal-area safety gates."""

    _claim_floor_projection_for_layout_write(db, floor_code="3F")
    area = db.scalar(
        select(WarehouseArea)
        .join(WarehouseFloor, WarehouseArea.floor_id == WarehouseFloor.id)
        .options(selectinload(WarehouseArea.storage_policy))
        .where(
            WarehouseFloor.floor_number == 3,
            func.upper(WarehouseArea.area_code) == area_code.strip().upper(),
        )
    )
    if area is not None and (
        area.construction_status == "archived"
        or (
            area.storage_policy is not None
            and area.storage_policy.status == "archived"
        )
    ):
        raise Floor3LocationError(
            "该区域已经归档，旧版三楼入口不能恢复或修改。",
            status_code=409,
        )
    if area is not None and area.storage_policy is not None:
        raise Floor3LocationError(
            "该区域已进入正式区域管理，请从区域规划入口维护货位",
            status_code=409,
        )


def _assert_published_ground_plan_area_unlocked(
    db: Session,
    *,
    area_id: int,
) -> None:
    plan_id = db.scalar(
        select(WarehouseGroundLayoutPlan.id).where(
            WarehouseGroundLayoutPlan.area_id == int(area_id),
            WarehouseGroundLayoutPlan.status == "published",
        )
    )
    if plan_id is not None:
        raise WarehouseAreaActivationError(
            "该区域已有正式发布的地堆排位；请通过地堆排位变更流程调整，"
            "不能从旧空间布局入口改动库位数量、状态或坐标",
            status_code=409,
        )


def _reduce_published_ground_plan_empty_slots(
    db: Session,
    *,
    plan: WarehouseGroundLayoutPlan,
    policy: WarehouseAreaStoragePolicy,
    area: WarehouseArea,
    expected_plan_version: int | None,
    active_location_ids_before: set[int],
    disabled_location_ids: set[int],
    operator_id: int,
) -> dict:
    """Retire empty surplus slots without altering surviving ground positions."""

    if expected_plan_version is None:
        raise WarehouseAreaActivationError(
            "缺少地堆排位版本，请刷新后重试", status_code=409
        )
    if plan.status != "published" or plan.version != expected_plan_version:
        raise WarehouseAreaActivationError(
            "地堆排位已被其他操作更新，请刷新后重试", status_code=409
        )
    if plan.published_map_revision != policy.published_map_revision:
        raise WarehouseAreaActivationError(
            "地堆排位与正式地图版本不一致，请刷新后重试", status_code=409
        )
    if not disabled_location_ids:
        raise WarehouseAreaActivationError(
            "本次没有可停用的空货位，不能变更地堆排位", status_code=409
        )

    plan_slots = list(plan.slots)
    plan_location_ids = {int(slot.location_id) for slot in plan_slots}
    if plan_location_ids != active_location_ids_before:
        raise WarehouseAreaActivationError(
            "正式地堆排位与当前启用货位不一致，请先核对后重试", status_code=409
        )
    if not disabled_location_ids.issubset(plan_location_ids):
        raise WarehouseAreaActivationError(
            "待停用货位不属于当前地堆排位，请刷新后重试", status_code=409
        )
    occupancy_location_ids = set(
        db.scalars(
            select(WarehouseGroundOccupancySlot.location_id)
            .join(WarehouseGroundOccupancy)
            .where(
                WarehouseGroundOccupancySlot.location_id.in_(disabled_location_ids),
                WarehouseGroundOccupancySlot.status == "active",
                WarehouseGroundOccupancy.status == "active",
            )
        ).all()
    )
    if occupancy_location_ids:
        raise WarehouseAreaActivationError(
            "待停用货位仍有活动地堆占用，不能调整容量", status_code=409
        )

    retained_slots = [
        slot for slot in plan_slots if int(slot.location_id) not in disabled_location_ids
    ]
    if not retained_slots:
        raise WarehouseAreaActivationError(
            "正式地堆排位不能通过货位数量入口清空，请使用区域归档流程",
            status_code=409,
        )
    fingerprint_slots: list[dict] = []
    for slot in retained_slots:
        layout = slot.location.floor3_layout
        if layout is None:
            raise WarehouseAreaActivationError(
                "已发布地堆货位缺少地图坐标，请停止操作并核对", status_code=409
            )
        fingerprint_slots.append(
            {
                "route_sequence": int(slot.route_sequence),
                "row_no": int(slot.row_no),
                "slot_no": int(slot.slot_no),
                "location_code": slot.location.location_code,
                "x_mm": Decimal(str(slot.x_mm)),
                "y_mm": Decimal(str(slot.y_mm)),
                "width_mm": int(slot.width_mm),
                "depth_mm": int(slot.depth_mm),
                "left_pct": float(layout.left_pct),
                "top_pct": float(layout.top_pct),
                "width_pct": float(layout.width_pct),
                "height_pct": float(layout.height_pct),
                "existing_location_id": int(slot.location_id),
                "existing_layout_version": int(layout.version),
            }
        )

    for slot in plan_slots:
        if int(slot.location_id) in disabled_location_ids:
            db.delete(slot)
    plan.target_slot_count = len(retained_slots)
    plan.preview_fingerprint = ground_preview_fingerprint(
        area_id=area.id,
        policy_version=policy.version,
        map_revision=str(policy.published_map_revision or ""),
        configuration=_ground_plan_configuration(plan),
        slots=fingerprint_slots,
    )
    plan.version += 1
    plan.updated_by = operator_id
    plan.updated_at = beijing_now_naive()
    db.flush()
    return {
        "plan_id": int(plan.id),
        "plan_version": int(plan.version),
        "target_slot_count": int(plan.target_slot_count),
        "removed_location_ids": sorted(disabled_location_ids),
    }


def _extend_published_ground_plan_empty_slots(
    db: Session,
    *,
    plan: WarehouseGroundLayoutPlan,
    policy: WarehouseAreaStoragePolicy,
    area: WarehouseArea,
    floor_layout: dict,
    expected_plan_version: int | None,
    active_location_ids_before: set[int],
    added_locations: list[WarehouseLocation],
    operator_id: int,
) -> dict:
    """Add measured pallet slots without moving existing published slots."""

    if expected_plan_version is None:
        raise WarehouseAreaActivationError("缺少地堆排位版本，请刷新后重试", status_code=409)
    if plan.status != "published" or plan.version != expected_plan_version:
        raise WarehouseAreaActivationError("地堆排位已被其他操作更新，请刷新后重试", status_code=409)
    if plan.published_map_revision != policy.published_map_revision:
        raise WarehouseAreaActivationError("地堆排位与正式地图版本不一致，请刷新后重试", status_code=409)
    if not added_locations:
        raise WarehouseAreaActivationError("本次没有新增空货位，不能变更地堆排位", status_code=409)
    plan_slots = list(plan.slots)
    if {int(slot.location_id) for slot in plan_slots} != active_location_ids_before:
        raise WarehouseAreaActivationError("正式地堆排位与当前启用货位不一致，请先核对后重试", status_code=409)
    if not policy.map_feature_id:
        raise WarehouseAreaActivationError("区域尚未绑定正式地图边界，不能增加地堆货位", status_code=409)

    reserved = [_layout_geometry_payload(slot.location) for slot in plan_slots]
    try:
        new_slots = confirmed_capacity_slots_for_zone(
            floor_layout,
            feature_id=policy.map_feature_id,
            target_count=len(added_locations),
            prefer_standard_pallet_slots=True,
            reserved_slots=reserved,
        )
        submitted = [
            *reserved,
            *[
                {**slot, "location_id": int(location.id)}
                for location, slot in zip(added_locations, new_slots, strict=True)
            ],
        ]
        measured = validate_capacity_layout_slots_for_zone(
            floor_layout, feature_id=policy.map_feature_id, slots=submitted
        )
    except Floor1CandidatePlanningError as error:
        raise WarehouseAreaActivationError(str(error), status_code=error.status_code) from error
    if len(new_slots) != len(added_locations):
        raise WarehouseAreaActivationError("新增地堆货位生成不完整，请刷新后重试", status_code=409)

    feature = next(
        (item for item in floor_layout.get("features") or []
         if item.get("feature_kind") == "zone" and str(item.get("id") or "") == str(policy.map_feature_id)),
        None,
    )
    tolerance = max(1.0, float(_percent_round_trip_epsilon((feature or {}).get("points") or [])))
    new_measured = measured[len(reserved):]
    for slot in new_measured:
        size = (float(slot["width_mm"]), float(slot["depth_mm"]))
        if not ((abs(size[0] - 1200) <= tolerance and abs(size[1] - 1000) <= tolerance)
                or (abs(size[0] - 1000) <= tolerance and abs(size[1] - 1200) <= tolerance)):
            raise WarehouseAreaActivationError(
                "当前区域没有足够的1200×1000毫米实测空栈板位；不能把逻辑点位冒充正式地堆排位",
                status_code=409,
            )

    now = beijing_now_naive()
    for location, slot in zip(added_locations, new_slots, strict=True):
        layout = location.floor3_layout
        if layout is None:
            raise WarehouseAreaActivationError("新增货位缺少地图坐标，请刷新后重试", status_code=409)
        layout.left_pct = Decimal(str(slot["left_pct"]))
        layout.top_pct = Decimal(str(slot["top_pct"]))
        layout.width_pct = Decimal(str(slot["width_pct"]))
        layout.height_pct = Decimal(str(slot["height_pct"]))
        layout.layout_kind = "physical_pallet"
        layout.source_type = "seeded"
        layout.updated_by = operator_id
        layout.updated_at = now
        location.placement_status = "placed"

    all_locations = [*(slot.location for slot in plan_slots), *added_locations]
    numbered = number_ground_physical_slots(
        [
            {**actual, "existing_location_id": int(location.id)}
            for location, actual in zip(all_locations, measured, strict=True)
        ],
        numbering_origin=plan.numbering_origin,
        row_direction=plan.row_direction,
        slot_direction=plan.slot_direction,
        row_start_no=plan.row_start_no,
        slot_start_no=plan.slot_start_no,
    )
    for slot in plan_slots:
        db.delete(slot)
    db.flush()
    fingerprint_slots: list[dict] = []
    locations_by_id = {int(location.id): location for location in all_locations}
    for numbered_slot in numbered:
        location_id = int(numbered_slot["existing_location_id"])
        location = locations_by_id[location_id]
        layout = location.floor3_layout
        assert layout is not None
        db.add(WarehouseGroundLayoutSlot(
            plan_id=plan.id, location_id=location_id,
            route_sequence=int(numbered_slot["route_sequence"]),
            row_no=int(numbered_slot["row_no"]), slot_no=int(numbered_slot["slot_no"]),
            x_mm=Decimal(str(numbered_slot["x_mm"])), y_mm=Decimal(str(numbered_slot["y_mm"])),
            width_mm=int(round(float(numbered_slot["width_mm"]))),
            depth_mm=int(round(float(numbered_slot["depth_mm"]))),
        ))
        fingerprint_slots.append({
            **numbered_slot, "location_code": location.location_code,
            "left_pct": float(layout.left_pct), "top_pct": float(layout.top_pct),
            "width_pct": float(layout.width_pct), "height_pct": float(layout.height_pct),
            "existing_location_id": location_id,
            "existing_layout_version": int(layout.version),
        })
    plan.target_slot_count = len(all_locations)
    plan.preview_fingerprint = ground_preview_fingerprint(
        area_id=area.id, policy_version=policy.version,
        map_revision=str(policy.published_map_revision or ""),
        configuration=_ground_plan_configuration(plan), slots=fingerprint_slots,
    )
    plan.version += 1
    plan.updated_by = operator_id
    plan.updated_at = now
    db.flush()
    return {"plan_id": int(plan.id), "plan_version": int(plan.version),
            "target_slot_count": int(plan.target_slot_count),
            "added_location_ids": sorted(int(location.id) for location in added_locations)}


def _assert_published_ground_plan_location_unlocked(
    db: Session,
    *,
    location_id: int,
) -> None:
    plan_id = db.scalar(
        select(WarehouseGroundLayoutPlan.id)
        .join(
            WarehouseGroundLayoutSlot,
            WarehouseGroundLayoutSlot.plan_id == WarehouseGroundLayoutPlan.id,
        )
        .where(
            WarehouseGroundLayoutSlot.location_id == int(location_id),
            WarehouseGroundLayoutPlan.status == "published",
        )
    )
    if plan_id is not None:
        raise WarehouseAreaActivationError(
            "该库位属于正式发布的地堆排位；请通过地堆排位变更流程调整，"
            "不能从旧空间布局入口启用、停用或移动",
            status_code=409,
        )


def _claim_empty_location_for_reflow(
    db: Session,
    location: WarehouseLocation,
) -> bool:
    """Serialize auto-layout with pallet and inventory occupancy writes."""

    result = db.execute(
        update(WarehouseLocation)
        .where(
            WarehouseLocation.id == location.id,
            WarehouseLocation.is_active.is_(True),
            ~select(InventoryPallet.id)
            .where(
                InventoryPallet.location_id == location.id,
                InventoryPallet.is_current.is_(True),
            )
            .exists(),
            ~select(InventoryLot.id)
            .where(
                InventoryLot.warehouse_location_id == location.id,
                (
                    InventoryLot.quantity_available
                    + InventoryLot.quantity_reserved
                    + InventoryLot.quantity_damaged
                )
                > 0,
            )
            .exists(),
        )
        .values(
            # Guarded no-op: this is the same location-row claim protocol used
            # before binding inventory or a current pallet.
            is_active=WarehouseLocation.is_active,
            updated_at=WarehouseLocation.updated_at,
        )
        .execution_options(synchronize_session=False)
    )
    return result.rowcount == 1


def _area_layout_context(
    db: Session,
    *,
    floor_code: str,
    area_code: str,
) -> tuple[WarehouseFloor, WarehouseArea, WarehouseAreaStoragePolicy | None]:
    floor = warehouse_floor_for_code(db, floor_code)
    if floor is None:
        raise WarehouseAreaActivationError("正式仓库楼层不存在", status_code=404)
    area = db.scalar(
        select(WarehouseArea)
        .options(selectinload(WarehouseArea.storage_policy))
        .where(
            WarehouseArea.floor_id == floor.id,
            func.upper(WarehouseArea.area_code) == area_code.strip().upper(),
        )
    )
    if area is None:
        raise WarehouseAreaActivationError("正式仓库区域不存在", status_code=404)
    assert_area_not_archived(area)
    return floor, area, area.storage_policy


def _sync_formal_area_location_count(
    db: Session,
    *,
    area: WarehouseArea,
    policy: WarehouseAreaStoragePolicy,
    target_count: int,
    operator_id: int,
    increment_policy_version: bool,
) -> None:
    """Keep the formal area ledger in lockstep with its active location set."""

    assert_area_not_archived(area)
    db.flush()
    if increment_policy_version:
        expected_version = policy.version
        result = db.execute(
            update(WarehouseAreaStoragePolicy)
            .where(
                WarehouseAreaStoragePolicy.id == policy.id,
                WarehouseAreaStoragePolicy.version == expected_version,
            )
            .values(
                version=expected_version + 1,
                updated_by=operator_id,
                updated_at=beijing_now_naive(),
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            raise WarehouseAreaActivationError(
                "区域设置已被其他操作更新，请刷新后重试", status_code=409
            )
        db.expire(policy)
    area.planned_location_count = target_count
    area.construction_status = "enabled" if policy.status == "published" else "layout_building"
    db.flush()


def _assert_expected_area_layout_versions(
    db: Session,
    *,
    floor_code: str,
    area_code: str,
    source_version: str,
    expected_versions: dict[int, int],
) -> None:
    floor, area, _policy = _area_layout_context(
        db, floor_code=floor_code, area_code=area_code
    )
    rows = list(
        db.scalars(
            select(WarehouseLocation)
            .options(selectinload(WarehouseLocation.floor3_layout))
            .where(
                WarehouseLocation.warehouse_floor == floor.floor_number,
                func.upper(WarehouseLocation.area_code) == area.area_code.upper(),
                WarehouseLocation.source_version == source_version,
                WarehouseLocation.is_active.is_(True),
            )
            .order_by(WarehouseLocation.sort_order, WarehouseLocation.id)
        ).all()
    )
    current_versions = {
        row.id: row.floor3_layout.version
        for row in rows
        if row.floor3_layout is not None
    }
    if len(current_versions) != len(rows) or current_versions != expected_versions:
        raise WarehouseAreaActivationError(
            "区域货位数量或位置已被其他操作更新，请刷新后重试",
            status_code=409,
        )


def _validate_current_area_layout(
    db: Session,
    *,
    floor_code: str,
    area_code: str,
    source_version: str,
    allow_spatial_conflicts: bool = False,
) -> list[dict]:
    floor, area, policy = _area_layout_context(
        db, floor_code=floor_code, area_code=area_code
    )
    if policy is None or policy.status != "published":
        # Draft areas may not yet exist in the published measured map.  Their
        # coordinates remain non-operational drafts until publish performs the
        # complete spatial validation.
        return []
    if not policy.map_feature_id:
        raise WarehouseAreaActivationError(
            "正式区域尚未绑定地图区域，不能保存货位位置", status_code=409
        )
    rows = [
        row
        for row in formal_area_location_rows(db, floor=floor, area=area)
        if row.is_active
        and row.source_version == source_version
        and row.storage_type == "ground"
    ]
    slots = [_layout_geometry_payload(row) for row in rows]
    try:
        floor_layout = load_warehouse_twin_floor(f"{floor.floor_number}F")
        if (
            policy.published_map_revision
            and str(floor_layout.get("revision") or "")
            != policy.published_map_revision
        ):
            raise WarehouseAreaActivationError(
                "正式地图版本与区域设置不一致，请刷新后重试", status_code=409
            )
        return validate_capacity_layout_slots_for_zone(
            floor_layout,
            feature_id=policy.map_feature_id,
            slots=slots,
            allow_spatial_conflicts=allow_spatial_conflicts,
        )
    except (Floor1CandidatePlanningError, WarehouseTwinLayoutNotFoundError, ValueError) as error:
        status_code = getattr(error, "status_code", 409)
        raise WarehouseAreaActivationError(str(error), status_code=status_code) from error


def _validate_published_area_layouts_for_floor(
    db: Session,
    *,
    floor_code: str,
    deferred_feature_id: str | None = None,
    allow_spatial_conflicts_for_feature_ids: set[str] | None = None,
) -> None:
    floor = warehouse_floor_for_code(db, floor_code)
    if floor is None:
        # A legacy map-only deployment has no formal area policies or
        # production location ledger to validate yet.
        return
    areas = list(
        db.scalars(
            select(WarehouseArea)
            .join(WarehouseAreaStoragePolicy)
            .options(selectinload(WarehouseArea.storage_policy))
            .where(
                WarehouseArea.floor_id == floor.id,
                WarehouseAreaStoragePolicy.status == "published",
            )
            .order_by(WarehouseArea.area_code)
        ).all()
    )
    for area in areas:
        policy = area.storage_policy
        if policy is None or policy.map_feature_id == deferred_feature_id:
            continue
        sources = set(
            db.scalars(
                select(WarehouseLocation.source_version)
                .where(
                    WarehouseLocation.warehouse_floor == floor.floor_number,
                    func.upper(WarehouseLocation.area_code)
                    == area.area_code.upper(),
                    WarehouseLocation.storage_type == "ground",
                    WarehouseLocation.is_active.is_(True),
                )
                .distinct()
            ).all()
        ) - {None, ""}
        if not sources:
            continue
        if len(sources) != 1 or not sources.issubset(
            {"V11", AREA_LOCATION_SOURCE_VERSION}
        ):
            raise WarehouseAreaActivationError(
                f"{area.area_code} 区货位来源不一致，不能发布地图",
                status_code=409,
            )
        _validate_current_area_layout(
            db,
            floor_code=floor.floor_code,
            area_code=area.area_code,
            source_version=next(iter(sources)),
            allow_spatial_conflicts=policy.map_feature_id in (
                allow_spatial_conflicts_for_feature_ids or set()
            ),
        )


def _reflow_area_locations(
    db: Session,
    *,
    floor_code: str,
    area_code: str,
    source_version: str,
    operator_id: int,
    adopt_historical_layouts: bool = False,
) -> dict:
    floor, area, policy = formal_area(
        db, floor_code=floor_code, area_code=area_code
    )
    if policy.status != "published":
        raise WarehouseAreaActivationError(
            "区域尚未正式发布，不能自动排布货位", status_code=409
        )
    rows = [
        row
        for row in formal_area_location_rows(db, floor=floor, area=area)
        if row.is_active
        and row.source_version == source_version
        and row.storage_type == "ground"
    ]
    fixed_rows: list[WarehouseLocation] = []
    auto_rows: list[WarehouseLocation] = []
    for row in rows:
        layout = row.floor3_layout
        if layout is None:
            raise WarehouseAreaActivationError(
                f"货位 {row.location_code} 缺少二维位置，不能自动排布",
                status_code=409,
            )
        historical_system_candidate = (
            layout.source_type == "manual" and layout.version == 1
        )
        manually_fixed = layout.source_type == "manual" and (
            layout.version > 1
            or (historical_system_candidate and not adopt_historical_layouts)
        )
        if manually_fixed or not _claim_empty_location_for_reflow(db, row):
            fixed_rows.append(row)
        else:
            auto_rows.append(row)
    occupied_ids = _occupied_location_ids(db, [row.id for row in rows])
    historical_adopted_count = sum(
        1
        for row in auto_rows
        if row.floor3_layout is not None
        and row.floor3_layout.source_type == "manual"
        and row.floor3_layout.version == 1
    )

    try:
        floor_layout = load_warehouse_twin_floor(f"{floor.floor_number}F")
        if (
            policy.published_map_revision
            and str(floor_layout.get("revision") or "")
            != policy.published_map_revision
        ):
            raise WarehouseAreaActivationError(
                "正式地图版本已变化，请刷新后重试", status_code=409
            )
        slots = confirmed_capacity_slots_for_zone(
            floor_layout,
            feature_id=policy.map_feature_id,
            target_count=len(auto_rows),
            prefer_standard_pallet_slots=True,
            reserved_slots=[_layout_geometry_payload(row) for row in fixed_rows],
        )
    except (Floor1CandidatePlanningError, WarehouseTwinLayoutNotFoundError, ValueError) as error:
        status_code = getattr(error, "status_code", 409)
        raise WarehouseAreaActivationError(str(error), status_code=status_code) from error

    changes: list[dict] = []
    for row, slot in zip(auto_rows, slots, strict=True):
        layout = row.floor3_layout
        assert layout is not None
        before = _floor3_layout_dict(layout)
        after_geometry = {
            "left_pct": Decimal(str(slot["left_pct"])),
            "top_pct": Decimal(str(slot["top_pct"])),
            "width_pct": Decimal(str(slot["width_pct"])),
            "height_pct": Decimal(str(slot["height_pct"])),
        }
        after_layout_kind = (
            "logical_anchor"
            if slot.get("capacity_confirmed") is True
            else "physical_pallet"
        )
        changed = any(
            getattr(layout, key) != value for key, value in after_geometry.items()
        ) or layout.source_type != "seeded" or layout.layout_kind != after_layout_kind
        if not changed:
            continue
        expected_version = layout.version
        update_result = db.execute(
            update(Floor3LocationLayout)
            .where(
                Floor3LocationLayout.id == layout.id,
                Floor3LocationLayout.version == expected_version,
            )
            .values(
                **after_geometry,
                source_type="seeded",
                layout_kind=after_layout_kind,
                version=expected_version + 1,
                updated_by=operator_id,
                updated_at=beijing_now_naive(),
            )
            .execution_options(synchronize_session=False)
        )
        if update_result.rowcount != 1:
            raise WarehouseAreaActivationError(
                "货位位置已被其他操作更新，请刷新后重试", status_code=409
            )
        row.placement_status = "placed"
        db.flush()
        db.expire(layout)
        changes.append(
            {
                "location": row,
                "before": before,
                "after": _floor3_layout_dict(layout),
            }
        )

    _validate_current_area_layout(
        db,
        floor_code=floor.floor_code,
        area_code=area.area_code,
        source_version=source_version,
    )
    return {
        "changes": changes,
        "active_count": len(rows),
        "auto_count": len(auto_rows),
        "fixed_count": len(fixed_rows),
        "occupied_count": len(occupied_ids),
        "historical_adopted_count": historical_adopted_count,
        "logical_anchor_count": sum(
            1 for slot in slots if slot.get("capacity_confirmed") is True
        ),
    }


def _ground_layout_context(
    db: Session, *, floor_code: str, area_code: str
) -> tuple[WarehouseFloor, WarehouseArea, WarehouseAreaStoragePolicy]:
    floor, area, policy = formal_area(
        db,
        floor_code=floor_code.strip().upper(),
        area_code=area_code.strip().upper(),
    )
    if (
        floor.construction_status != "enabled"
        or area.construction_status != "enabled"
        or policy.status != "published"
        or policy.storage_layout not in {"pallet_ground", "mixed"}
        or "finished" not in set(policy_inventory_types(policy))
    ):
        raise WarehouseGroundSlotError(
            "GROUND_AREA_NOT_OPERATIONAL",
            "只有已启用、已发布且允许成品地堆的区域可以维护排位。",
        )
    legacy_fin_area = (
        re.fullmatch(r"FIN-00([1-3])", area.area_code.upper())
        if floor.floor_number == 1
        else None
    )
    if (
        not area.address_zone_code or not area.address_subzone_no
    ) and legacy_fin_area is None:
        raise WarehouseGroundSlotError(
            "GROUND_AREA_ADDRESS_REQUIRED",
            "请先按 P1-86 为区域确认 A～G 大区和两位数子区。",
        )
    return floor, area, policy


def _legacy_fin_ground_locations(
    db: Session,
    *,
    floor: WarehouseFloor,
    area: WarehouseArea,
) -> list[WarehouseLocation]:
    return list(
        db.scalars(
            select(WarehouseLocation)
            .join(
                Floor3LocationLayout,
                Floor3LocationLayout.location_id == WarehouseLocation.id,
            )
            .where(
                WarehouseLocation.warehouse_floor == floor.floor_number,
                func.upper(WarehouseLocation.area_code) == area.area_code.upper(),
                WarehouseLocation.is_active.is_(True),
                WarehouseLocation.placement_status == "placed",
                WarehouseLocation.storage_type == "ground",
                WarehouseLocation.source_version.in_(("TWIN_V1", "CURRENT_MAP")),
                Floor3LocationLayout.layout_kind.in_(("physical_pallet", "unknown")),
            )
            .order_by(WarehouseLocation.sort_order, WarehouseLocation.id)
            .options(selectinload(WarehouseLocation.floor3_layout))
        )
    )


def _ground_slot_preview_for_area(
    db: Session,
    *,
    floor: WarehouseFloor,
    area: WarehouseArea,
    policy: WarehouseAreaStoragePolicy,
    floor_layout: dict,
    configuration: dict,
) -> list[dict]:
    if area.address_zone_code and area.address_subzone_no:
        return build_ground_slot_preview(
            floor_layout,
            feature_id=policy.map_feature_id,
            floor_number=floor.floor_number,
            zone_code=str(area.address_zone_code),
            subzone_no=int(area.address_subzone_no),
            **configuration,
        )

    legacy_fin_match = (
        re.fullmatch(r"FIN-00([1-3])", area.area_code.upper())
        if floor.floor_number == 1
        else None
    )
    if legacy_fin_match is None:
        raise WarehouseGroundSlotError(
            "GROUND_AREA_ADDRESS_REQUIRED",
            "请先按 P1-86 为区域确认 A～G 大区和两位数子区。",
        )
    existing_locations = _legacy_fin_ground_locations(
        db,
        floor=floor,
        area=area,
    )
    target_slot_count = int(configuration["target_slot_count"])
    if len(existing_locations) != target_slot_count:
        raise WarehouseGroundSlotError(
            "GROUND_FIN_EXISTING_SLOT_COUNT_MISMATCH",
            (
                f"{area.area_code} 已有 {len(existing_locations)} 个真实实测地堆位置；"
                f"为保护现有库存，本次位置数量必须保持为 {len(existing_locations)}。"
            ),
        )
    preview = build_ground_slot_preview(
        floor_layout,
        feature_id=policy.map_feature_id,
        floor_number=floor.floor_number,
        zone_code="F",
        subzone_no=int(legacy_fin_match.group(1)),
        **configuration,
    )
    remaining = list(existing_locations)
    adopted: list[dict] = []
    geometry_keys = ("left_pct", "top_pct", "width_pct", "height_pct")
    for slot in preview:
        closest = min(
            remaining,
            key=lambda location: sum(
                abs(
                    float(getattr(location.floor3_layout, key))
                    - float(slot[key])
                )
                for key in geometry_keys
            ),
        )
        maximum_delta = max(
            abs(float(getattr(closest.floor3_layout, key)) - float(slot[key]))
            for key in geometry_keys
        )
        if maximum_delta > 0.05:
            raise WarehouseGroundSlotError(
                "GROUND_FIN_EXISTING_GEOMETRY_MISMATCH",
                (
                    f"{area.area_code} 的既有真实位置与当前实测地图不一致；"
                    "请先核对地图位置，禁止重复生成重叠位置。"
                ),
            )
        remaining.remove(closest)
        adopted.append(
            {
                **slot,
                "location_code": closest.location_code,
                "location_name": employee_location_name(
                    closest,
                    area=area,
                    floor=floor,
                ),
                "location_master_name": closest.location_name,
                "existing_location_id": int(closest.id),
                "existing_layout_version": int(closest.floor3_layout.version),
            }
        )
    return adopted


def _ground_plan_configuration(plan: WarehouseGroundLayoutPlan) -> dict:
    return {
        "target_slot_count": plan.target_slot_count,
        "numbering_origin": plan.numbering_origin,
        "row_direction": plan.row_direction,
        "slot_direction": plan.slot_direction,
        "row_start_no": plan.row_start_no,
        "slot_start_no": plan.slot_start_no,
    }


def _ground_preview_for_plan(
    db: Session,
    plan: WarehouseGroundLayoutPlan,
    *,
    floor: WarehouseFloor,
    area: WarehouseArea,
    policy: WarehouseAreaStoragePolicy,
    floor_layout: dict,
) -> tuple[list[dict], str]:
    slots = _ground_slot_preview_for_area(
        db,
        floor=floor,
        area=area,
        policy=policy,
        floor_layout=floor_layout,
        configuration=_ground_plan_configuration(plan),
    )
    fingerprint = ground_preview_fingerprint(
        area_id=area.id,
        policy_version=policy.version,
        map_revision=str(policy.published_map_revision or ""),
        configuration=_ground_plan_configuration(plan),
        slots=slots,
    )
    return slots, fingerprint


def _ground_published_plan_payload(
    db: Session,
    plan: WarehouseGroundLayoutPlan,
    *,
    replayed: bool,
) -> dict:
    current = db.scalar(
        select(WarehouseGroundLayoutPlan)
        .where(WarehouseGroundLayoutPlan.id == plan.id)
        .options(
            selectinload(WarehouseGroundLayoutPlan.area).selectinload(
                WarehouseArea.floor
            ),
            selectinload(WarehouseGroundLayoutPlan.slots)
            .selectinload(WarehouseGroundLayoutSlot.location)
            .selectinload(WarehouseLocation.floor3_layout)
        )
        .execution_options(populate_existing=True)
    )
    assert current is not None
    return {
        "plan_id": current.id,
        "plan_version": current.version,
        "status": current.status,
        "preview_fingerprint": current.preview_fingerprint,
        "published_map_revision": current.published_map_revision,
        "idempotent_replay": replayed,
        "writes_inventory": False,
        "slots": [
            {
                "location_id": row.location_id,
                "location_name": employee_location_name(
                    row.location,
                    area=current.area,
                    floor=current.area.floor,
                ),
                "location_master_name": row.location.location_name,
                "row_no": row.row_no,
                "slot_no": row.slot_no,
                "route_sequence": row.route_sequence,
                "layout_version": int(row.location.floor3_layout.version),
                "width_mm": row.width_mm,
                "depth_mm": row.depth_mm,
            }
            for row in current.slots
        ],
    }


@router.post("/ground-layout/floors/{floor_code}/areas/{area_code}/draft")
def save_ground_layout_draft(
    floor_code: str,
    area_code: str,
    payload: GroundLayoutDraftPayload,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    with GROUND_STORAGE_TRANSACTION_LOCK:
        try:
            _claim_floor_projection_for_layout_write(
                db,
                floor_code=floor_code,
            )
            floor, area, policy = _ground_layout_context(
                db, floor_code=floor_code, area_code=area_code
            )
            if policy.version != payload.expected_policy_version:
                raise WarehouseGroundSlotError(
                    "GROUND_POLICY_STALE", "区域设置已变化，请刷新后重试。"
                )
            if policy.published_map_revision != payload.expected_map_revision:
                raise WarehouseGroundSlotError(
                    "GROUND_MAP_STALE", "正式地图版本已变化，请刷新后重试。"
                )
            floor_layout = load_warehouse_twin_floor(f"{floor.floor_number}F")
            if str(floor_layout.get("revision") or "") != payload.expected_map_revision:
                raise WarehouseGroundSlotError(
                    "GROUND_MAP_STALE", "地图文件与区域发布版本不一致，请先重新发布地图。"
                )
            configuration = {
                key: value
                for key, value in payload.model_dump().items()
                if key
                in {
                    "target_slot_count",
                    "numbering_origin",
                    "row_direction",
                    "slot_direction",
                    "row_start_no",
                    "slot_start_no",
                }
            }
            slots = _ground_slot_preview_for_area(
                db,
                floor=floor,
                area=area,
                policy=policy,
                floor_layout=floor_layout,
                configuration=configuration,
            )
            fingerprint = ground_preview_fingerprint(
                area_id=area.id,
                policy_version=policy.version,
                map_revision=payload.expected_map_revision,
                configuration=configuration,
                slots=slots,
            )
            plan = db.scalar(
                select(WarehouseGroundLayoutPlan)
                .where(WarehouseGroundLayoutPlan.area_id == area.id)
                .with_for_update()
            )
            if plan is not None and plan.status == "published":
                raise WarehouseGroundSlotError(
                    "GROUND_LAYOUT_ALREADY_PUBLISHED",
                    "该区域已发布地堆排位；为保护占用与历史，请通过后续变更流程调整。",
                )
            now = beijing_now_naive()
            if plan is None:
                if payload.expected_plan_version is not None:
                    raise WarehouseGroundSlotError(
                        "GROUND_PLAN_STALE", "地堆排位草稿不存在，请刷新后重试。"
                    )
                plan = WarehouseGroundLayoutPlan(
                    area_id=area.id,
                    status="draft",
                    draft_map_revision=payload.expected_map_revision,
                    preview_fingerprint=fingerprint,
                    version=1,
                    updated_by=user.id,
                    **configuration,
                )
                db.add(plan)
            else:
                if payload.expected_plan_version != plan.version:
                    raise WarehouseGroundSlotError(
                        "GROUND_PLAN_STALE", "地堆排位草稿已变化，请刷新后重试。"
                    )
                for key, value in configuration.items():
                    setattr(plan, key, value)
                plan.draft_map_revision = payload.expected_map_revision
                plan.preview_fingerprint = fingerprint
                plan.version += 1
                plan.updated_by = user.id
                plan.updated_at = now
            db.flush()
            db.commit()
            return {
                "plan_id": plan.id,
                "plan_version": plan.version,
                "status": "draft",
                "preview_fingerprint": fingerprint,
                "writes_inventory": False,
                "writes_pallets": False,
                "slots": slots,
                "message": "地堆排位预览已保存；尚未发布，不可用于入库或转位。",
            }
        except (WarehouseGroundSlotError, WarehouseAreaActivationError) as error:
            db.rollback()
            raise HTTPException(
                status_code=getattr(error, "status_code", 409),
                detail={"code": getattr(error, "code", "GROUND_LAYOUT_INVALID"), "message": str(error)},
            ) from error
        except IntegrityError as error:
            db.rollback()
            raise HTTPException(status_code=409, detail="地堆排位草稿发生并发冲突，请刷新后重试") from error


@router.post("/ground-layout/floors/{floor_code}/areas/{area_code}/publish")
def publish_ground_layout(
    floor_code: str,
    area_code: str,
    payload: GroundLayoutPublishPayload,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    with GROUND_STORAGE_TRANSACTION_LOCK:
        try:
            _claim_floor_projection_for_layout_write(
                db,
                floor_code=floor_code,
            )
            floor, area, policy = _ground_layout_context(
                db, floor_code=floor_code, area_code=area_code
            )
            plan = db.scalar(
                select(WarehouseGroundLayoutPlan)
                .where(WarehouseGroundLayoutPlan.area_id == area.id)
                .with_for_update()
            )
            if plan is None:
                raise WarehouseGroundSlotError(
                    "GROUND_LAYOUT_DRAFT_REQUIRED", "请先保存地堆排位预览。", status_code=404
                )
            request_hash = ground_canonical_hash(
                {
                    "plan_id": plan.id,
                    "expected_plan_version": payload.expected_plan_version,
                    "preview_fingerprint": payload.preview_fingerprint,
                }
            )
            if plan.status == "published":
                if (
                    plan.publish_idempotency_key == payload.idempotency_key
                    and plan.publish_request_hash == request_hash
                    and plan.published_by == user.id
                ):
                    return _ground_published_plan_payload(db, plan, replayed=True)
                raise WarehouseGroundSlotError(
                    "GROUND_LAYOUT_ALREADY_PUBLISHED", "该区域地堆排位已经发布。"
                )
            if plan.version != payload.expected_plan_version:
                raise WarehouseGroundSlotError(
                    "GROUND_PLAN_STALE", "地堆排位草稿已变化，请刷新后重试。"
                )
            if plan.preview_fingerprint != payload.preview_fingerprint:
                raise WarehouseGroundSlotError(
                    "GROUND_PREVIEW_TAMPERED", "地堆排位预览校验值不一致，请重新生成预览。"
                )
            floor_layout = load_warehouse_twin_floor(f"{floor.floor_number}F")
            slots, fingerprint = _ground_preview_for_plan(
                db,
                plan,
                floor=floor,
                area=area,
                policy=policy,
                floor_layout=floor_layout,
            )
            if (
                str(floor_layout.get("revision") or "") != plan.draft_map_revision
                or policy.published_map_revision != plan.draft_map_revision
                or fingerprint != plan.preview_fingerprint
            ):
                raise WarehouseGroundSlotError(
                    "GROUND_PREVIEW_STALE", "区域、地图或排位预览已变化，请重新生成。"
                )
            existing_managed = int(
                db.scalar(
                    select(func.count(WarehouseGroundLayoutSlot.id))
                    .join(WarehouseGroundLayoutPlan)
                    .where(WarehouseGroundLayoutPlan.area_id == area.id)
                )
                or 0
            )
            if existing_managed:
                raise WarehouseGroundSlotError(
                    "GROUND_LAYOUT_ALREADY_MATERIALIZED", "该区域已有地堆排位位置，禁止重复生成。"
                )
            materialized_locations: list[WarehouseLocation] = []
            adopted_existing_location_ids: list[int] = []
            for slot in slots:
                existing_location_id = slot.get("existing_location_id")
                if existing_location_id is not None:
                    location = db.scalar(
                        select(WarehouseLocation)
                        .where(WarehouseLocation.id == int(existing_location_id))
                        .options(selectinload(WarehouseLocation.floor3_layout))
                        .with_for_update()
                    )
                    if (
                        location is None
                        or location.floor3_layout is None
                        or not location.is_active
                        or location.warehouse_floor != floor.floor_number
                        or str(location.area_code or "").upper()
                        != area.area_code.upper()
                        or location.location_code != slot["location_code"]
                        or int(location.floor3_layout.version)
                        != int(slot["existing_layout_version"])
                    ):
                        raise WarehouseGroundSlotError(
                            "GROUND_FIN_EXISTING_LOCATION_STALE",
                            "FIN 既有真实位置已变化，请刷新并重新生成地堆排位预览。",
                        )
                    adopted_existing_location_ids.append(int(location.id))
                else:
                    location = WarehouseLocation(
                        location_code=slot["location_code"],
                        location_name=slot["location_name"],
                        warehouse_type="finished",
                        is_active=True,
                        warehouse_floor=floor.floor_number,
                        area_code=area.area_code,
                        storage_type="ground",
                        sort_order=int(slot["route_sequence"]),
                        is_temporary=False,
                        source_version=AREA_LOCATION_SOURCE_VERSION,
                        address_kind="ground_slot",
                        address_area_id=area.id,
                        ground_row_no=int(slot["row_no"]),
                        slot_no=int(slot["slot_no"]),
                        address_version=1,
                        placement_status="placed",
                    )
                    location.floor3_layout = Floor3LocationLayout(
                        left_pct=Decimal(str(slot["left_pct"])),
                        top_pct=Decimal(str(slot["top_pct"])),
                        width_pct=Decimal(str(slot["width_pct"])),
                        height_pct=Decimal(str(slot["height_pct"])),
                        z_index=int(slot["route_sequence"]),
                        version=1,
                        source_type="seeded",
                        layout_kind="physical_pallet",
                        created_by=user.id,
                        updated_by=user.id,
                    )
                    db.add(location)
                    db.flush()
                db.add(
                    WarehouseGroundLayoutSlot(
                        plan_id=plan.id,
                        location_id=location.id,
                        route_sequence=int(slot["route_sequence"]),
                        row_no=int(slot["row_no"]),
                        slot_no=int(slot["slot_no"]),
                        x_mm=Decimal(str(slot["x_mm"])),
                        y_mm=Decimal(str(slot["y_mm"])),
                        width_mm=int(slot["width_mm"]),
                        depth_mm=int(slot["depth_mm"]),
                    )
                )
                materialized_locations.append(location)
            now = beijing_now_naive()
            plan.status = "published"
            plan.published_map_revision = plan.draft_map_revision
            plan.publish_idempotency_key = payload.idempotency_key
            plan.publish_request_hash = request_hash
            plan.published_by = user.id
            plan.published_at = now
            plan.updated_at = now
            plan.version += 1
            area.planned_location_count = len(materialized_locations)
            area.planned_pallet_capacity = max(
                int(area.planned_pallet_capacity or 0), len(materialized_locations)
            )
            policy.version += 1
            policy.updated_by = user.id
            policy.updated_at = now
            append_audit_event(
                db,
                request=request,
                actor=user,
                event_category="business",
                result="success",
                source="web",
                module_code="warehouse",
                action_code="warehouse.ground_layout.publish",
                legacy_action="PUBLISH_GROUND_LAYOUT",
                resource="WarehouseGroundLayoutPlan",
                entity_type="warehouse_ground_layout_plan",
                entity_id=plan.id,
                object_ref=f"ground-layout:{floor.floor_code}:{area.area_code}",
                description="发布实测地堆排位",
                details={
                    "slot_count": len(materialized_locations),
                    "location_ids": [row.id for row in materialized_locations],
                    "adopted_existing_location_ids": adopted_existing_location_ids,
                    "preview_fingerprint": plan.preview_fingerprint,
                    "inventory_changed": False,
                    "pallet_changed": False,
                },
            )
            db.flush()
            db.commit()
            return _ground_published_plan_payload(db, plan, replayed=False)
        except (WarehouseGroundSlotError, WarehouseAreaActivationError) as error:
            db.rollback()
            raise HTTPException(
                status_code=getattr(error, "status_code", 409),
                detail={"code": getattr(error, "code", "GROUND_LAYOUT_INVALID"), "message": str(error)},
            ) from error
        except IntegrityError as error:
            db.rollback()
            raise HTTPException(status_code=409, detail="地堆排位发布发生重码或并发冲突，请刷新后重试") from error


@router.get("/ground-storage/candidates")
def list_ground_storage_candidates(
    floor_code: str,
    area_code: str,
    customer_id: int = Query(gt=0),
    product_id: int = Query(gt=0),
    incoming_quantity: int = Query(gt=0),
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    require_customer_access(customer_id, user, db)
    product = db.get(Product, product_id)
    if product is None or product.deleted_at is not None or product.customer_id != customer_id:
        raise HTTPException(status_code=409, detail="所选产品不属于当前客户或已停用")
    try:
        plan = published_ground_plan(
            db,
            floor_code=floor_code,
            area_code=area_code,
            required_inventory_type="finished",
        )
        items = ground_candidate_rows(
            db,
            plan=plan,
            customer_id=customer_id,
            product_id=product_id,
            incoming_quantity=incoming_quantity,
            can_view_occupied_details=has_unrestricted_customer_access(user, db),
            floor_layout=load_warehouse_twin_floor(
                f"{int(plan.area.floor.floor_number)}F"
            ),
        )
        return {
            "floor_name": plan.area.floor.floor_name,
            "area_name": employee_area_name(
                plan.area,
                floor_number=plan.area.floor.floor_number,
            ),
            "area_master_name": plan.area.area_name,
            "plan_version": plan.version,
            "published_map_revision": plan.published_map_revision,
            "incoming_quantity": incoming_quantity,
            "legend": {
                "empty": {"color": "green", "label": "空位，可存放"},
                "same_product": {"color": "blue", "label": "同款可共位（人工选择）"},
                "unavailable": {"color": "gray", "label": "未发布、用途不符或容量不足"},
                "conflict": {"color": "red", "label": "冲突，不可选择"},
            },
            "items": items,
        }
    except WarehouseGroundSlotError as error:
        raise HTTPException(
            status_code=error.status_code,
            detail={"code": error.code, "message": error.message},
        ) from error


def _ground_slot_for_location(
    db: Session, location_id: int
) -> tuple[WarehouseGroundLayoutPlan, WarehouseGroundLayoutSlot]:
    slot = db.scalar(
        select(WarehouseGroundLayoutSlot)
        .where(WarehouseGroundLayoutSlot.location_id == location_id)
        .options(
            selectinload(WarehouseGroundLayoutSlot.plan)
            .selectinload(WarehouseGroundLayoutPlan.area)
            .selectinload(WarehouseArea.floor),
            selectinload(WarehouseGroundLayoutSlot.plan)
            .selectinload(WarehouseGroundLayoutPlan.area)
            .selectinload(WarehouseArea.storage_policy),
            selectinload(WarehouseGroundLayoutSlot.location)
            .selectinload(WarehouseLocation.floor3_layout),
        )
    )
    if slot is None or slot.plan.status != "published":
        raise WarehouseGroundSlotError(
            "GROUND_TARGET_NOT_PUBLISHED", "所选位置不是已发布地堆排位。", status_code=404
        )
    plan = published_ground_plan(
        db,
        floor_code=slot.plan.area.floor.floor_code,
        area_code=slot.plan.area.area_code,
        required_inventory_type="finished",
    )
    slot = next(row for row in plan.slots if row.location_id == location_id)
    return plan, slot


def _ground_mutation_replay(
    db: Session,
    *,
    idempotency_key: str,
    request_hash: str,
    actor_user_id: int,
) -> tuple[WarehouseGroundPlacementMutation | None, dict | None]:
    mutation = db.scalar(
        select(WarehouseGroundPlacementMutation).where(
            WarehouseGroundPlacementMutation.idempotency_key == idempotency_key
        )
    )
    if mutation is None:
        return None, None
    if mutation.request_hash != request_hash or mutation.actor_user_id != actor_user_id:
        raise WarehouseGroundSlotError(
            "GROUND_IDEMPOTENCY_CONFLICT", "同一请求标识已用于其他地图存放业务。"
        )
    occupancy = db.scalar(
        select(WarehouseGroundOccupancy)
        .where(WarehouseGroundOccupancy.id == mutation.occupancy_id)
        .options(
            selectinload(WarehouseGroundOccupancy.slots),
            selectinload(WarehouseGroundOccupancy.pallet)
            .selectinload(InventoryPallet.items)
            .selectinload(InventoryPalletItem.inventory_lot),
        )
    )
    lot = db.get(InventoryLot, mutation.result_lot_id)
    if occupancy is None or lot is None:
        raise WarehouseGroundSlotError(
            "GROUND_REPLAY_FACT_MISSING", "地图存放重放事实不完整，请管理员核对。"
        )
    primary_location = occupancy.primary_location
    primary_context = (
        load_warehouse_location_projection_contexts(
            db, [primary_location]
        ).get(int(primary_location.id), {})
        if primary_location is not None
        else {}
    )
    readable_location_name = (
        employee_location_name(
            primary_location,
            area=primary_context.get("area"),
            floor=primary_context.get("floor"),
        )
        if primary_location is not None
        else None
    )
    return mutation, {
        "message": "相同请求已成功处理，本次返回原结果。",
        "idempotent_replay": True,
        "lot_id": lot.id,
        "lot_number": lot.lot_number,
        "quantity": int(lot.quantity_available or 0) + int(lot.quantity_reserved or 0),
        "location_name": readable_location_name,
        "location_master_name": (
            primary_location.location_name if primary_location is not None else None
        ),
        "occupancy": ground_occupancy_payload(occupancy),
    }


def _validate_ground_target(
    db: Session,
    *,
    plan: WarehouseGroundLayoutPlan,
    primary_slot: WarehouseGroundLayoutSlot,
    secondary_location_id: int | None,
    expected_primary_version: int,
    expected_secondary_version: int | None,
    customer_id: int,
    product_id: int,
    quantity: int,
) -> tuple[WarehouseGroundOccupancy | None, WarehouseGroundLayoutSlot | None]:
    if (
        primary_slot.location.floor3_layout is None
        or primary_slot.location.floor3_layout.version != expected_primary_version
    ):
        raise WarehouseGroundSlotError(
            "GROUND_TARGET_STALE", "所选位置布局已变化，请重新点选。"
        )
    secondary_slot = None
    if secondary_location_id is not None:
        secondary_slot = next(
            (row for row in plan.slots if row.location_id == secondary_location_id), None
        )
        if (
            secondary_slot is None
            or secondary_slot.location.floor3_layout is None
            or secondary_slot.location.floor3_layout.version != expected_secondary_version
        ):
            raise WarehouseGroundSlotError(
                "GROUND_SECONDARY_STALE", "大型货物第二位置已变化，请重新点选。"
            )
        positions = effective_ground_slot_geometries(
            plan,
            load_warehouse_twin_floor(f"{int(plan.area.floor.floor_number)}F"),
        )
        if not ground_slots_adjacent(primary_slot, secondary_slot, positions=positions):
            raise WarehouseGroundSlotError(
                "GROUND_SLOTS_NOT_ADJACENT", "大型货物只能选择两个相邻地堆位置。"
            )
    occupancy = active_ground_occupancy_for_location(db, primary_slot.location_id)
    if occupancy is not None:
        if secondary_slot is not None:
            raise WarehouseGroundSlotError(
                "GROUND_LARGE_TARGET_OCCUPIED", "大型货物的两个位置都必须为空。"
            )
        if (
            occupancy.primary_location_id != primary_slot.location_id
            or occupancy.customer_id != customer_id
            or occupancy.product_id != product_id
        ):
            raise WarehouseGroundSlotError(
                "GROUND_TARGET_CONFLICT", "该位置不是同客户同存货编码的可共位位置。"
            )
        if occupancy_physical_quantity(occupancy) + quantity > occupancy.capacity_quantity:
            raise WarehouseGroundSlotError(
                "GROUND_TARGET_CAPACITY_FULL", "同款位置剩余容量不足。"
            )
    if secondary_slot is not None and active_ground_occupancy_for_location(
        db, secondary_slot.location_id
    ) is not None:
        raise WarehouseGroundSlotError(
            "GROUND_SECONDARY_OCCUPIED", "大型货物第二位置已被占用。"
        )
    for slot in [primary_slot, secondary_slot]:
        if slot is None:
            continue
        issue = operational_location_issue(
            db,
            slot.location,
            warehouse_types={"finished", "shared"},
            pallet_storage_only=True,
            require_published=True,
            require_map_geometry=True,
            required_inventory_type="finished",
        )
        if issue:
            raise WarehouseGroundSlotError("GROUND_TARGET_UNAVAILABLE", issue)
    return occupancy, secondary_slot


@router.post("/ground-storage/finished-inbound", status_code=201)
def ground_finished_inbound(
    payload: GroundFinishedInboundPayload,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    require_customer_access(payload.customer_id, user, db)
    product = db.get(Product, payload.product_id)
    if product is None or product.deleted_at is not None or product.customer_id != payload.customer_id:
        raise HTTPException(status_code=409, detail="所选产品不属于当前客户或已停用")
    request_hash = ground_canonical_hash(payload.model_dump(mode="json"))
    with GROUND_STORAGE_TRANSACTION_LOCK:
        try:
            _mutation, replay = _ground_mutation_replay(
                db,
                idempotency_key=payload.idempotency_key,
                request_hash=request_hash,
                actor_user_id=user.id,
            )
            if replay is not None:
                return replay
            plan, primary_slot = _ground_slot_for_location(db, payload.location_id)
            occupancy, secondary_slot = _validate_ground_target(
                db,
                plan=plan,
                primary_slot=primary_slot,
                secondary_location_id=payload.secondary_location_id,
                expected_primary_version=payload.expected_layout_version,
                expected_secondary_version=payload.expected_secondary_layout_version,
                customer_id=payload.customer_id,
                product_id=payload.product_id,
                quantity=payload.quantity,
            )
            if occupancy is not None and payload.capacity_quantity != occupancy.capacity_quantity:
                raise WarehouseGroundSlotError(
                    "GROUND_CAPACITY_STALE", "该位置容量已变化，请刷新后重试。"
                )
            lock_pairs = [(primary_slot.location_id, payload.expected_layout_version)]
            if secondary_slot is not None:
                lock_pairs.append(
                    (secondary_slot.location_id, int(payload.expected_secondary_layout_version))
                )
            for location_id, expected_version in sorted(lock_pairs):
                if not claim_active_placed_location(
                    db, location_id, expected_layout_version=expected_version
                ):
                    raise WarehouseGroundSlotError(
                        "GROUND_TARGET_STALE", "所选位置已变化，请重新点选。"
                    )
            occupancy, secondary_slot = _validate_ground_target(
                db,
                plan=plan,
                primary_slot=primary_slot,
                secondary_location_id=payload.secondary_location_id,
                expected_primary_version=payload.expected_layout_version,
                expected_secondary_version=payload.expected_secondary_layout_version,
                customer_id=payload.customer_id,
                product_id=payload.product_id,
                quantity=payload.quantity,
            )
            lot = manual_finished_in(
                db,
                customer_id=payload.customer_id,
                product_id=payload.product_id,
                location_id=primary_slot.location_id,
                quantity=payload.quantity,
                stock_date=payload.stock_date,
                source_type="manual",
                remarks=payload.remarks,
                operator_id=user.id,
                idempotency_key=payload.idempotency_key,
                pallet_id=occupancy.pallet_id if occupancy is not None else None,
                expected_layout_version=payload.expected_layout_version,
                ground_secondary_location_id=(
                    secondary_slot.location_id if secondary_slot is not None else None
                ),
                ground_capacity_quantity=payload.capacity_quantity,
                movement_reason="地图点选成品入库",
            )
            if lot.pallet_item is None:
                bind_finished_lot_to_floor3_pallet(
                    db,
                    lot=lot,
                    operator_id=user.id,
                    pallet_id=occupancy.pallet_id if occupancy is not None else None,
                    allow_operational_location=True,
                )
            pallet_item = lot.pallet_item
            if pallet_item is None:
                raise WarehouseGroundSlotError(
                    "GROUND_PALLET_BINDING_FAILED", "成品批次未能绑定内部空间身份。"
                )
            if occupancy is None:
                occupancy = active_ground_occupancy_for_pallet(
                    db, int(pallet_item.pallet_id)
                )
            if occupancy is None:
                occupancy = WarehouseGroundOccupancy(
                    pallet_id=pallet_item.pallet_id,
                    primary_location_id=primary_slot.location_id,
                    customer_id=payload.customer_id,
                    product_id=payload.product_id,
                    footprint_kind="double" if secondary_slot is not None else "single",
                    capacity_quantity=payload.capacity_quantity,
                    status="active",
                    version=1,
                    created_by=user.id,
                )
                db.add(occupancy)
                db.flush()
                db.add(
                    WarehouseGroundOccupancySlot(
                        occupancy_id=occupancy.id,
                        location_id=primary_slot.location_id,
                        slot_sequence=1,
                        status="active",
                    )
                )
                if secondary_slot is not None:
                    db.add(
                        WarehouseGroundOccupancySlot(
                            occupancy_id=occupancy.id,
                            location_id=secondary_slot.location_id,
                            slot_sequence=2,
                            status="active",
                        )
                    )
                db.flush()
            db.add(
                WarehouseGroundPlacementMutation(
                    idempotency_key=payload.idempotency_key,
                    request_hash=request_hash,
                    actor_user_id=user.id,
                    operation="finished_inbound",
                    result_lot_id=lot.id,
                    occupancy_id=occupancy.id,
                )
            )
            append_audit_event(
                db,
                request=request,
                actor=user,
                event_category="business",
                result="success",
                source="web",
                module_code="warehouse",
                action_code="warehouse.ground.finished_inbound",
                legacy_action="GROUND_FINISHED_INBOUND",
                resource="InventoryLot",
                entity_type="inventory_lot",
                entity_id=lot.id,
                object_ref=lot.lot_number,
                customer_id=payload.customer_id,
                description="地图点选成品入库",
                details={
                    "location_ids": [
                        primary_slot.location_id,
                        *([secondary_slot.location_id] if secondary_slot else []),
                    ],
                    "quantity": payload.quantity,
                    "capacity_quantity": payload.capacity_quantity,
                    "idempotency_key": payload.idempotency_key,
                },
            )
            db.flush()
            db.commit()
            db.refresh(occupancy)
            return {
                "message": "已按地图所选中文位置保存成品；库存批次和来源保持独立。",
                "idempotent_replay": False,
                "lot_id": lot.id,
                "lot_number": lot.lot_number,
                "quantity": payload.quantity,
                "location_name": employee_location_name(
                    primary_slot.location,
                    area=plan.area,
                    floor=plan.area.floor,
                ),
                "location_master_name": primary_slot.location.location_name,
                "occupancy": ground_occupancy_payload(occupancy),
            }
        except (WarehouseGroundSlotError, WarehouseInventoryError) as error:
            db.rollback()
            raise HTTPException(
                status_code=getattr(error, "status_code", 409),
                detail={"code": getattr(error, "code", "GROUND_STORAGE_INVALID"), "message": str(error)},
            ) from error
        except IntegrityError as error:
            db.rollback()
            raise HTTPException(status_code=409, detail="地图存放发生并发冲突，请刷新后重试") from error


@router.post("/ground-storage/lots/{lot_id}/transfer")
def ground_finished_lot_transfer(
    lot_id: int,
    payload: GroundFinishedTransferPayload,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    source_lot = _require_lot_customer_access(db, lot_id, user)
    detail = source_lot.finished_detail
    if detail is None or detail.owner_customer_id is None:
        raise HTTPException(status_code=409, detail="只有客户归属明确的正式成品可以地图转位")
    request_hash = ground_canonical_hash(
        {"source_lot_id": lot_id, **payload.model_dump(mode="json")}
    )
    with GROUND_STORAGE_TRANSACTION_LOCK:
        try:
            _mutation, replay = _ground_mutation_replay(
                db,
                idempotency_key=payload.idempotency_key,
                request_hash=request_hash,
                actor_user_id=user.id,
            )
            if replay is not None:
                return replay
            plan, primary_slot = _ground_slot_for_location(db, payload.location_id)
            occupancy, secondary_slot = _validate_ground_target(
                db,
                plan=plan,
                primary_slot=primary_slot,
                secondary_location_id=payload.secondary_location_id,
                expected_primary_version=payload.expected_layout_version,
                expected_secondary_version=payload.expected_secondary_layout_version,
                customer_id=int(detail.owner_customer_id),
                product_id=int(detail.product_id),
                quantity=payload.quantity,
            )
            if occupancy is not None and payload.capacity_quantity != occupancy.capacity_quantity:
                raise WarehouseGroundSlotError(
                    "GROUND_CAPACITY_STALE", "该位置容量已变化，请刷新后重试。"
                )
            lock_pairs = [(primary_slot.location_id, payload.expected_layout_version)]
            if secondary_slot is not None:
                lock_pairs.append(
                    (secondary_slot.location_id, int(payload.expected_secondary_layout_version))
                )
            for location_id, expected_version in sorted(lock_pairs):
                if not claim_active_placed_location(
                    db, location_id, expected_layout_version=expected_version
                ):
                    raise WarehouseGroundSlotError(
                        "GROUND_TARGET_STALE", "所选位置已变化，请重新点选。"
                    )
            occupancy, secondary_slot = _validate_ground_target(
                db,
                plan=plan,
                primary_slot=primary_slot,
                secondary_location_id=payload.secondary_location_id,
                expected_primary_version=payload.expected_layout_version,
                expected_secondary_version=payload.expected_secondary_layout_version,
                customer_id=int(detail.owner_customer_id),
                product_id=int(detail.product_id),
                quantity=payload.quantity,
            )
            source_pallet_id = source_lot.pallet_item.pallet_id if source_lot.pallet_item else None
            source_occupancy = (
                db.scalar(
                    select(WarehouseGroundOccupancy)
                    .where(
                        WarehouseGroundOccupancy.pallet_id == source_pallet_id,
                        WarehouseGroundOccupancy.status == "active",
                    )
                    .options(selectinload(WarehouseGroundOccupancy.slots))
                )
                if source_pallet_id is not None
                else None
            )
            result = transfer_finished_lot_between_locations(
                db,
                lot_id=lot_id,
                expected_version=payload.expected_lot_version,
                quantity=payload.quantity,
                location_id=primary_slot.location_id,
                operator_id=user.id,
                idempotency_key=payload.idempotency_key,
                require_empty_target=False,
                expected_target_layout_version=payload.expected_layout_version,
                ground_secondary_location_id=(
                    secondary_slot.location_id if secondary_slot is not None else None
                ),
                ground_capacity_quantity=payload.capacity_quantity,
            )
            target_item = result.target_lot.pallet_item
            if target_item is None:
                raise WarehouseGroundSlotError(
                    "GROUND_PALLET_BINDING_FAILED", "转位批次未能绑定内部空间身份。"
                )
            if source_occupancy is not None and source_occupancy.primary_location_id != primary_slot.location_id:
                now = beijing_now_naive()
                source_occupancy.status = "released"
                source_occupancy.version += 1
                source_occupancy.released_by = user.id
                source_occupancy.released_at = now
                for row in source_occupancy.slots:
                    row.status = "released"
                    row.released_at = now
                db.flush()
            if occupancy is None:
                occupancy = active_ground_occupancy_for_pallet(
                    db, int(target_item.pallet_id)
                )
            if occupancy is None:
                occupancy = WarehouseGroundOccupancy(
                    pallet_id=target_item.pallet_id,
                    primary_location_id=primary_slot.location_id,
                    customer_id=int(detail.owner_customer_id),
                    product_id=int(detail.product_id),
                    footprint_kind="double" if secondary_slot is not None else "single",
                    capacity_quantity=payload.capacity_quantity,
                    status="active",
                    version=1,
                    created_by=user.id,
                )
                db.add(occupancy)
                db.flush()
                for sequence, slot in enumerate([primary_slot, secondary_slot], start=1):
                    if slot is not None:
                        db.add(
                            WarehouseGroundOccupancySlot(
                                occupancy_id=occupancy.id,
                                location_id=slot.location_id,
                                slot_sequence=sequence,
                                status="active",
                            )
                        )
                db.flush()
            db.add(
                WarehouseGroundPlacementMutation(
                    idempotency_key=payload.idempotency_key,
                    request_hash=request_hash,
                    actor_user_id=user.id,
                    operation="lot_transfer",
                    source_lot_id=lot_id,
                    result_lot_id=result.target_lot.id,
                    occupancy_id=occupancy.id,
                )
            )
            append_audit_event(
                db,
                request=request,
                actor=user,
                event_category="business",
                result="success",
                source="web",
                module_code="warehouse",
                action_code="warehouse.ground.lot_transfer",
                legacy_action="GROUND_LOT_TRANSFER",
                resource="InventoryLotTransfer",
                entity_type="inventory_lot_transfer",
                entity_id=result.transfer.id,
                object_ref=f"inventory_lot_transfer:{result.transfer.id}",
                customer_id=int(detail.owner_customer_id),
                description="地图点选成品转位",
                details={
                    "source_lot_id": lot_id,
                    "target_lot_id": result.target_lot.id,
                    "location_ids": [
                        primary_slot.location_id,
                        *([secondary_slot.location_id] if secondary_slot else []),
                    ],
                    "quantity": payload.quantity,
                    "idempotency_key": payload.idempotency_key,
                },
            )
            db.flush()
            db.commit()
            db.refresh(occupancy)
            return {
                "message": "已按地图所选位置完成转位；库存总量、批次来源和预占保持守恒。",
                "idempotent_replay": result.replayed,
                "lot_id": result.target_lot.id,
                "lot_number": result.target_lot.lot_number,
                "quantity": payload.quantity,
                "location_name": employee_location_name(
                    primary_slot.location,
                    area=plan.area,
                    floor=plan.area.floor,
                ),
                "location_master_name": primary_slot.location.location_name,
                "occupancy": ground_occupancy_payload(occupancy),
            }
        except (WarehouseGroundSlotError, WarehouseInventoryError) as error:
            db.rollback()
            raise HTTPException(
                status_code=getattr(error, "status_code", 409),
                detail={"code": getattr(error, "code", "GROUND_STORAGE_INVALID"), "message": str(error)},
            ) from error
        except IntegrityError as error:
            db.rollback()
            raise HTTPException(status_code=409, detail="地图转位发生并发冲突，请刷新后重试") from error


@router.post("/spatial-layout/floors/{floor_code}/areas/{area_code}/location-count")
def set_activated_area_location_count(
    floor_code: str,
    area_code: str,
    payload: Floor3AreaLocationCountPayload,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    WAREHOUSE_TWIN_LAYOUT_TRANSACTION_LOCK.acquire()
    try:
        _claim_floor_projection_for_layout_write(db, floor_code=floor_code)
        route = resolve_area_location_management(
            db, floor_code=floor_code, area_code=area_code
        )
        _floor, _area, area_policy = _area_layout_context(
            db, floor_code=route.floor_code, area_code=route.area_code
        )
        if area_policy is None:
            raise WarehouseAreaActivationError(
                "历史区域尚未完成正式区域确认，请先在区域规划中确认并发布",
                status_code=409,
            )
        if payload.expected_layout_versions is None:
            raise WarehouseAreaActivationError(
                "缺少完整货位版本快照，请刷新后重试", status_code=409
            )
        if payload.expected_policy_version is None:
            raise WarehouseAreaActivationError(
                "缺少区域设置版本，请刷新后重试", status_code=409
            )
        if area_policy.version != payload.expected_policy_version:
            raise WarehouseAreaActivationError(
                "区域设置已被其他操作更新，请刷新后重试", status_code=409
            )
        if area_policy.status == "published":
            if payload.expected_map_revision is None:
                raise WarehouseAreaActivationError(
                    "缺少正式地图版本，请刷新后重试", status_code=409
                )
            if area_policy.published_map_revision != payload.expected_map_revision:
                raise WarehouseAreaActivationError(
                    "正式地图版本已变化，请刷新后重试", status_code=409
                )
        _assert_expected_area_layout_versions(
            db,
            floor_code=route.floor_code,
            area_code=route.area_code,
            source_version=route.source_version,
            expected_versions=payload.expected_layout_versions,
        )
        before_rows = list(
            db.scalars(
                select(WarehouseLocation)
                .options(selectinload(WarehouseLocation.floor3_layout))
                .where(
                    WarehouseLocation.warehouse_floor == _floor.floor_number,
                    func.upper(WarehouseLocation.area_code) == _area.area_code.upper(),
                    WarehouseLocation.source_version == route.source_version,
                )
            ).all()
        )
        before_by_location_id = {
            row.id: _location_layout_state(row) for row in before_rows
        }
        active_ground_location_ids_before = {
            int(row.id)
            for row in before_rows
            if row.is_active and row.storage_type == "ground"
        }
        published_ground_plan = db.scalar(
            select(WarehouseGroundLayoutPlan)
            .where(
                WarehouseGroundLayoutPlan.area_id == _area.id,
                WarehouseGroundLayoutPlan.status == "published",
            )
            .options(
                selectinload(WarehouseGroundLayoutPlan.slots)
                .selectinload(WarehouseGroundLayoutSlot.location)
                .selectinload(WarehouseLocation.floor3_layout)
            )
            .with_for_update()
        )
        allows_published_ground_reduction = bool(
            published_ground_plan is not None
            and area_policy.status == "published"
            and area_policy.storage_layout == "pallet_ground"
            and payload.target_count < len(active_ground_location_ids_before)
        )
        allows_published_ground_extension = bool(
            published_ground_plan is not None
            and area_policy.status == "published"
            and area_policy.storage_layout == "pallet_ground"
            and payload.target_count > len(active_ground_location_ids_before)
        )
        is_published_ground_noop = bool(
            published_ground_plan is not None
            and area_policy.status == "published"
            and area_policy.storage_layout == "pallet_ground"
            and payload.target_count == len(active_ground_location_ids_before)
        )
        if (
            published_ground_plan is not None
            and not allows_published_ground_reduction
            and not allows_published_ground_extension
            and not is_published_ground_noop
        ):
            _assert_published_ground_plan_area_unlocked(db, area_id=_area.id)
        ground_capacity_before = (
            {
                "planned_location_count": _area.planned_location_count,
                "planned_pallet_capacity": _area.planned_pallet_capacity,
                "confirmed_pallet_capacity": _area.confirmed_pallet_capacity,
                "capacity_review_status": _area.capacity_review_status,
                "capacity_eligible": _area.capacity_eligible,
                "ground_plan_version": published_ground_plan.version,
                "ground_plan_slot_count": len(active_ground_location_ids_before),
            }
            if allows_published_ground_reduction or allows_published_ground_extension
            else None
        )
        if route.management_mode == "floor3_v11":
            result = adjust_area_location_count(
                db,
                area_code=route.area_code,
                target_count=payload.target_count,
                operator_id=user.id,
            )
            _sync_formal_area_location_count(
                db,
                area=_area,
                policy=area_policy,
                target_count=result.active_count,
                operator_id=user.id,
                increment_policy_version=bool(
                    result.created or result.enabled or result.disabled
                ),
            )
        else:
            result = adjust_activated_area_location_count(
                db,
                floor_code=route.floor_code,
                area_code=route.area_code,
                target_count=payload.target_count,
                operator_id=user.id,
                allow_empty_historical_retirement=allows_published_ground_reduction,
            )
        reflow_result: dict | None = None
        count_changed = bool(result.created or result.enabled or result.disabled)
        if (
            count_changed
            and area_policy.status == "published"
            and area_policy.storage_layout != "rack"
            and not allows_published_ground_extension
        ):
            reflow_result = _reflow_area_locations(
                db,
                floor_code=route.floor_code,
                area_code=route.area_code,
                source_version=route.source_version,
                operator_id=user.id,
            )
        ground_plan_result: dict | None = None
        if allows_published_ground_reduction:
            assert published_ground_plan is not None
            ground_plan_result = _reduce_published_ground_plan_empty_slots(
                db,
                plan=published_ground_plan,
                policy=area_policy,
                area=_area,
                expected_plan_version=payload.expected_ground_plan_version,
                active_location_ids_before=active_ground_location_ids_before,
                disabled_location_ids={int(row.id) for row in result.disabled},
                operator_id=user.id,
            )
            assert ground_capacity_before is not None
            _area.planned_pallet_capacity = result.active_count
            _area.confirmed_pallet_capacity = result.active_count
            _area.capacity_review_status = "confirmed"
            _area.capacity_eligible = result.active_count > 0
            _apply_capacity_review(_area, user=user, review_changed=True)
            _warehouse_capacity_log(
                db,
                request=request,
                user=user,
                action="warehouse.ground_layout.capacity_reduce",
                entity_type="warehouse_ground_layout_plan",
                entity_id=published_ground_plan.id,
                object_ref=f"ground-layout:{route.floor_code}:{result.area_code}",
                before=ground_capacity_before,
                after={
                    "planned_location_count": _area.planned_location_count,
                    "planned_pallet_capacity": _area.planned_pallet_capacity,
                    "confirmed_pallet_capacity": _area.confirmed_pallet_capacity,
                    "capacity_review_status": _area.capacity_review_status,
                    "capacity_eligible": _area.capacity_eligible,
                    "ground_plan_version": ground_plan_result["plan_version"],
                    "ground_plan_slot_count": ground_plan_result["target_slot_count"],
                    "disabled_location_ids": ground_plan_result["removed_location_ids"],
                    "inventory_changed": False,
                    "pallet_binding_changed": False,
                },
            )
        elif allows_published_ground_extension:
            assert published_ground_plan is not None
            floor_layout = load_warehouse_twin_floor(route.floor_code)
            ground_plan_result = _extend_published_ground_plan_empty_slots(
                db,
                plan=published_ground_plan,
                policy=area_policy,
                area=_area,
                floor_layout=floor_layout,
                expected_plan_version=payload.expected_ground_plan_version,
                active_location_ids_before=active_ground_location_ids_before,
                added_locations=[*result.created, *result.enabled],
                operator_id=user.id,
            )
            assert ground_capacity_before is not None
            _area.planned_pallet_capacity = result.active_count
            _area.confirmed_pallet_capacity = result.active_count
            _area.capacity_review_status = "confirmed"
            _area.capacity_eligible = result.active_count > 0
            _apply_capacity_review(_area, user=user, review_changed=True)
            _warehouse_capacity_log(
                db,
                request=request,
                user=user,
                action="warehouse.ground_layout.capacity_extend",
                entity_type="warehouse_ground_layout_plan",
                entity_id=published_ground_plan.id,
                object_ref=f"ground-layout:{route.floor_code}:{result.area_code}",
                before=ground_capacity_before,
                after={
                    "planned_location_count": _area.planned_location_count,
                    "planned_pallet_capacity": _area.planned_pallet_capacity,
                    "confirmed_pallet_capacity": _area.confirmed_pallet_capacity,
                    "capacity_review_status": _area.capacity_review_status,
                    "capacity_eligible": _area.capacity_eligible,
                    "ground_plan_version": ground_plan_result["plan_version"],
                    "ground_plan_slot_count": ground_plan_result["target_slot_count"],
                    "added_location_ids": ground_plan_result["added_location_ids"],
                    "inventory_changed": False,
                    "pallet_binding_changed": False,
                },
            )
        actions: list[dict] = []
        for action, rows in (
            ("created", result.created),
            ("enabled", result.enabled),
            ("disabled", result.disabled),
        ):
            for location in rows:
                _floor3_layout_log(
                    db,
                    request=request,
                    user=user,
                    action="CREATE" if action == "created" else "UPDATE",
                    location=location,
                    description="按权威区域管理路径维护库存库位数量",
                    details={
                        "floor_code": route.floor_code,
                        "area_code": result.area_code,
                        "target_count": result.target_count,
                        "location_code": location.location_code,
                        "location_count_action": action,
                        "before": before_by_location_id.get(location.id),
                        "after": _location_layout_state(location),
                    },
                )
                actions.append(
                    {
                        "action": action,
                        "location": _location_dict_for_db(db, location),
                        "layout": _floor3_layout_dict(location.floor3_layout),
                    }
                )
        for change in (reflow_result or {}).get("changes", []):
            location = change["location"]
            _floor3_layout_log(
                db,
                request=request,
                user=user,
                action="UPDATE",
                location=location,
                description="区域货位数量变化后自动均匀排布空闲系统货位",
                details={
                    "floor_code": route.floor_code,
                    "area_code": route.area_code,
                    "before": change["before"],
                    "after": change["after"],
                    "inventory_changed": False,
                    "pallet_binding_changed": False,
                },
            )
        db.commit()
        return {
            **area_location_management_payload(route),
            "policy_version": area_policy.version if area_policy is not None else None,
            "published_map_revision": (
                area_policy.published_map_revision
                if area_policy is not None
                else None
            ),
            "ground_plan_id": (
                ground_plan_result["plan_id"] if ground_plan_result is not None else None
            ),
            "ground_plan_version": (
                ground_plan_result["plan_version"] if ground_plan_result is not None else None
            ),
            "area_code": result.area_code,
            "target_count": result.target_count,
            "active_count": result.active_count,
            "created_count": len(result.created),
            "enabled_count": len(result.enabled),
            "disabled_count": len(result.disabled),
            "items": actions,
            "auto_arranged_count": len((reflow_result or {}).get("changes", [])),
            "fixed_count": (reflow_result or {}).get("fixed_count", 0),
            "occupied_count": (reflow_result or {}).get("occupied_count", 0),
            "logical_anchor_count": (reflow_result or {}).get("logical_anchor_count", 0),
            "historical_adopted_count": (reflow_result or {}).get(
                "historical_adopted_count", 0
            ),
            "message": (
                "新增货位已在当前实测区域内均匀排布并落位；库存、栈板和货物均未移动。"
                if reflow_result is not None and result.created
                else
                "新增库位已进入待布局草稿；拖到实际位置、保存并发布后才会进入生产入库候选。"
                if result.created
                else "区域货位数量及空闲系统货位排布已核对；库存、栈板和货物均未移动。"
                if reflow_result is not None
                else "区域库位数量已更新；重新发布前不会改变员工入库候选。"
            ),
        }
    except (WarehouseAreaActivationError, Floor3LocationError) as error:
        db.rollback()
        raise HTTPException(status_code=error.status_code, detail=str(error)) from error
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(
            status_code=409, detail="区域库位编号或布局发生冲突，请刷新后重试"
        ) from error
    finally:
        WAREHOUSE_TWIN_LAYOUT_TRANSACTION_LOCK.release()


@router.post("/spatial-layout/floors/{floor_code}/areas/{area_code}/auto-arrange")
def auto_arrange_activated_area_locations(
    floor_code: str,
    area_code: str,
    payload: AreaLocationAutoArrangePayload,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    """Evenly reflow only empty, auto-managed ground locations."""

    WAREHOUSE_TWIN_LAYOUT_TRANSACTION_LOCK.acquire()
    try:
        _claim_floor_projection_for_layout_write(db, floor_code=floor_code)
        route = resolve_area_location_management(
            db, floor_code=floor_code, area_code=area_code
        )
        _floor, area, policy = formal_area(
            db, floor_code=route.floor_code, area_code=route.area_code
        )
        _assert_published_ground_plan_area_unlocked(db, area_id=area.id)
        if policy.storage_layout == "rack":
            raise WarehouseAreaActivationError(
                "自动均匀排布仅适用于栈板地堆货位；货架位请按实测货架维护",
                status_code=409,
            )
        _assert_expected_area_layout_versions(
            db,
            floor_code=route.floor_code,
            area_code=route.area_code,
            source_version=route.source_version,
            expected_versions=payload.expected_layout_versions,
        )
        if payload.expected_policy_version is None:
            raise WarehouseAreaActivationError(
                "缺少区域设置版本，请刷新后重试", status_code=409
            )
        if policy.version != payload.expected_policy_version:
            raise WarehouseAreaActivationError(
                "区域设置已被其他操作更新，请刷新后重试", status_code=409
            )
        if payload.expected_map_revision is None:
            raise WarehouseAreaActivationError(
                "缺少正式地图版本，请刷新后重试", status_code=409
            )
        if policy.published_map_revision != payload.expected_map_revision:
            raise WarehouseAreaActivationError(
                "正式地图版本已变化，请刷新后重试", status_code=409
            )
        result = _reflow_area_locations(
            db,
            floor_code=route.floor_code,
            area_code=route.area_code,
            source_version=route.source_version,
            operator_id=user.id,
            adopt_historical_layouts=payload.adopt_historical_layouts,
        )
        _sync_formal_area_location_count(
            db,
            area=area,
            policy=policy,
            target_count=result["active_count"],
            operator_id=user.id,
            increment_policy_version=bool(result["changes"]),
        )
        for change in result["changes"]:
            location = change["location"]
            _floor3_layout_log(
                db,
                request=request,
                user=user,
                action="UPDATE",
                location=location,
                description="管理员确认自动均匀排布空闲系统货位",
                details={
                    "floor_code": route.floor_code,
                    "area_code": route.area_code,
                    "before": change["before"],
                    "after": change["after"],
                    "fixed_count": result["fixed_count"],
                    "occupied_count": result["occupied_count"],
                    "inventory_changed": False,
                    "pallet_binding_changed": False,
                },
            )
        db.commit()
        return {
            **area_location_management_payload(route),
            "area_id": area.id,
            "policy_version": policy.version,
            "published_map_revision": policy.published_map_revision,
            "active_count": result["active_count"],
            "auto_arranged_count": len(result["changes"]),
            "fixed_count": result["fixed_count"],
            "occupied_count": result["occupied_count"],
            "logical_anchor_count": result["logical_anchor_count"],
            "historical_adopted_count": result["historical_adopted_count"],
            "items": [
                _floor3_layout_dict(change["location"].floor3_layout)
                for change in result["changes"]
            ],
            "message": (
                f"已均匀排布 {len(result['changes'])} 个空闲系统货位；"
                f"保留 {result['fixed_count']} 个固定或占用货位，库存、栈板和货物均未移动。"
            ),
        }
    except (WarehouseAreaActivationError, Floor3LocationError) as error:
        db.rollback()
        raise HTTPException(status_code=error.status_code, detail=str(error)) from error
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(
            status_code=409, detail="区域货位排布发生并发冲突，请刷新后重试"
        ) from error
    finally:
        WAREHOUSE_TWIN_LAYOUT_TRANSACTION_LOCK.release()


@router.get("/spatial-layout/floors/{floor_code}/areas/{area_code}/management")
def get_area_location_management(
    floor_code: str,
    area_code: str,
    db: Session = Depends(get_db),
    _user: User = Depends(admin_only),
) -> dict:
    try:
        route = resolve_area_location_management(
            db, floor_code=floor_code, area_code=area_code
        )
        _floor, _area, policy = _area_layout_context(
            db, floor_code=route.floor_code, area_code=route.area_code
        )
        management = area_location_management_payload(route)
        if policy is None:
            management["available_actions"] = []
        else:
            published_plan = db.scalar(
                select(WarehouseGroundLayoutPlan).where(
                WarehouseGroundLayoutPlan.area_id == _area.id,
                WarehouseGroundLayoutPlan.status == "published",
            )
            )
            if published_plan is not None:
                management["available_actions"] = ["location_count", "published_layout", "disable_empty"]
                management["ground_plan_id"] = published_plan.id
                management["ground_plan_version"] = published_plan.version
                management["spatial_layout_locked_reason"] = None
        return {
            **management,
            "policy_version": policy.version if policy is not None else None,
            "published_map_revision": (
                policy.published_map_revision if policy is not None else None
            ),
            "requires_area_confirmation": policy is None,
        }
    except WarehouseAreaActivationError as error:
        raise HTTPException(status_code=error.status_code, detail=str(error)) from error


@router.patch(
    "/ground-layout/floors/{floor_code}/areas/{area_code}/published-positions"
)
def patch_published_ground_layout_positions(
    floor_code: str,
    area_code: str,
    payload: PublishedGroundLayoutPositionPatchPayload,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    """Persist freely dragged pallet gaps in the one published spatial fact.

    The stable location identity, pallets, lots, reservations and quantities are
    untouched.  The location layout and its published measured ground plan are
    updated together under the same persistent floor writer claim.
    """

    request_hash = ground_canonical_hash(payload.model_dump(mode="json"))
    with GROUND_STORAGE_TRANSACTION_LOCK:
        WAREHOUSE_TWIN_LAYOUT_TRANSACTION_LOCK.acquire()
        try:
            _claim_floor_projection_for_layout_write(db, floor_code=floor_code)
            floor, area, policy = _ground_layout_context(
                db, floor_code=floor_code, area_code=area_code
            )
            plan = db.scalar(
                select(WarehouseGroundLayoutPlan)
                .where(
                    WarehouseGroundLayoutPlan.area_id == area.id,
                    WarehouseGroundLayoutPlan.status == "published",
                )
                .options(
                    selectinload(WarehouseGroundLayoutPlan.slots)
                    .selectinload(WarehouseGroundLayoutSlot.location)
                    .selectinload(WarehouseLocation.floor3_layout)
                )
                .execution_options(populate_existing=True)
            )
            if plan is None:
                raise WarehouseGroundSlotError(
                    "GROUND_PLAN_MISSING", "当前区域没有已发布的真实地堆排位。", status_code=404
                )
            replay_log = db.scalar(
                select(OperationLog).where(
                    OperationLog.action_code
                    == "warehouse.ground_layout.positions_update",
                    OperationLog.entity_type == "warehouse_ground_layout_plan",
                    OperationLog.entity_id == plan.id,
                    OperationLog.request_id == payload.idempotency_key,
                )
            )
            if replay_log is not None:
                details = json.loads(replay_log.details or "{}")
                if details.get("request_hash") != request_hash:
                    raise WarehouseGroundSlotError(
                        "GROUND_LAYOUT_IDEMPOTENCY_CONFLICT",
                        "同一保存编号已用于不同货位布局，请刷新后重试。",
                    )
                return {
                    **details.get("result", {}),
                    "idempotent_replay": True,
                    "writes_inventory": False,
                }
            if policy.version != payload.expected_policy_version:
                raise WarehouseGroundSlotError(
                    "GROUND_POLICY_STALE", "区域设置已变化，请刷新后重试。"
                )
            if (
                policy.status != "published"
                or policy.published_map_revision != payload.expected_map_revision
            ):
                raise WarehouseGroundSlotError(
                    "GROUND_MAP_STALE", "正式地图已变化，请刷新后重新调整货位。"
                )
            if plan.version != payload.expected_plan_version:
                raise WarehouseGroundSlotError(
                    "GROUND_PLAN_STALE", "地堆排位已被其他操作更新，请刷新后重试。"
                )
            if plan.published_map_revision != payload.expected_map_revision:
                raise WarehouseGroundSlotError(
                    "GROUND_MAP_STALE", "地堆排位与正式地图版本不一致，请刷新后重试。"
                )
            plan_slots_by_location = {row.location_id: row for row in plan.slots}
            submitted = {row.location_id: row for row in payload.slots}
            if not set(submitted).issubset(plan_slots_by_location):
                raise WarehouseGroundSlotError(
                    "GROUND_LOCATION_SET_STALE", "提交中包含不属于当前区域的货位。"
                )
            all_layout_payloads: list[dict] = []
            before: dict[int, dict] = {}
            for ground_slot in plan.slots:
                location = ground_slot.location
                layout = location.floor3_layout
                if layout is None:
                    raise WarehouseGroundSlotError(
                        "GROUND_LOCATION_LAYOUT_MISSING", "已发布货位缺少地图坐标，请先刷新地图。"
                    )
                change = submitted.get(location.id)
                if change is not None and layout.version != change.expected_version:
                    raise WarehouseGroundSlotError(
                        "GROUND_LOCATION_LAYOUT_STALE", "货位坐标已变化，请刷新后重试。"
                    )
                before[location.id] = _floor3_layout_dict(layout)
                values = change.model_dump(mode="json") if change is not None else _layout_geometry_payload(location)
                all_layout_payloads.append(values)
            floor_layout = load_warehouse_twin_floor(f"{floor.floor_number}F")
            if str(floor_layout.get("revision") or "") != payload.expected_map_revision:
                raise WarehouseGroundSlotError(
                    "GROUND_MAP_STALE", "运行地图与正式排位版本不一致，请刷新后重试。"
                )
            if not policy.map_feature_id:
                raise WarehouseGroundSlotError(
                    "GROUND_MAP_FEATURE_MISSING", "区域尚未绑定正式地图边界。"
                )
            measured = validate_capacity_layout_slots_for_zone(
                floor_layout,
                feature_id=policy.map_feature_id,
                slots=all_layout_payloads,
            )
            feature = next(
                (
                    row
                    for row in floor_layout.get("features") or []
                    if row.get("feature_kind") == "zone"
                    and str(row.get("id") or "") == str(policy.map_feature_id)
                ),
                None,
            )
            if feature is None:
                raise WarehouseGroundSlotError(
                    "GROUND_MAP_FEATURE_MISSING",
                    "正式地图中已找不到该区域边界，请刷新地图并重新规划。",
                )
            points = feature.get("points") or []
            tolerance = max(1.0, float(_percent_round_trip_epsilon(points)))
            for row in measured:
                size = (float(row["width_mm"]), float(row["depth_mm"]))
                if not (
                    abs(size[0] - 1200) <= tolerance and abs(size[1] - 1000) <= tolerance
                ) and not (
                    abs(size[0] - 1000) <= tolerance and abs(size[1] - 1200) <= tolerance
                ):
                    raise WarehouseGroundSlotError(
                        "GROUND_SLOT_SIZE_CHANGED",
                        "拖动只能改变货位位置，不能缩放标准栈板货位。",
                    )
            measured_by_location = {
                int(source["location_id"]): {**actual, "existing_location_id": int(source["location_id"])}
                for source, actual in zip(all_layout_payloads, measured, strict=True)
            }
            numbered = number_ground_physical_slots(
                list(measured_by_location.values()),
                numbering_origin=plan.numbering_origin,
                row_direction=plan.row_direction,
                slot_direction=plan.slot_direction,
                row_start_no=plan.row_start_no,
                slot_start_no=plan.slot_start_no,
            )
            numbered_by_location = {
                int(row["existing_location_id"]): row for row in numbered
            }
            changed_ids = set(submitted)
            now = beijing_now_naive()
            for location_id in changed_ids:
                ground_slot = plan_slots_by_location[location_id]
                layout = ground_slot.location.floor3_layout
                assert layout is not None
                change = submitted[location_id]
                layout.left_pct = change.left_pct
                layout.top_pct = change.top_pct
                layout.width_pct = change.width_pct
                layout.height_pct = change.height_pct
                layout.z_index = change.z_index
                layout.source_type = "manual"
                layout.version += 1
                layout.updated_by = user.id
                layout.updated_at = now
            # Recreate the plan slots to avoid transient unique collisions while
            # route and row numbers are re-derived from the saved real positions.
            for row in list(plan.slots):
                db.delete(row)
            db.flush()
            fingerprint_slots: list[dict] = []
            for location_id, numbered_row in sorted(
                numbered_by_location.items(), key=lambda item: item[1]["route_sequence"]
            ):
                location = plan_slots_by_location[location_id].location
                db.add(
                    WarehouseGroundLayoutSlot(
                        plan_id=plan.id,
                        location_id=location_id,
                        route_sequence=int(numbered_row["route_sequence"]),
                        row_no=int(numbered_row["row_no"]),
                        slot_no=int(numbered_row["slot_no"]),
                        x_mm=Decimal(str(numbered_row["x_mm"])),
                        y_mm=Decimal(str(numbered_row["y_mm"])),
                        width_mm=int(round(float(numbered_row["width_mm"]))),
                        depth_mm=int(round(float(numbered_row["depth_mm"]))),
                    )
                )
                fingerprint_slots.append(
                    {
                        **numbered_row,
                        "location_code": location.location_code,
                        "left_pct": float(location.floor3_layout.left_pct),
                        "top_pct": float(location.floor3_layout.top_pct),
                        "width_pct": float(location.floor3_layout.width_pct),
                        "height_pct": float(location.floor3_layout.height_pct),
                        "existing_location_id": location_id,
                        "existing_layout_version": int(location.floor3_layout.version),
                    }
                )
            plan.preview_fingerprint = ground_preview_fingerprint(
                area_id=area.id,
                policy_version=policy.version,
                map_revision=payload.expected_map_revision,
                configuration=_ground_plan_configuration(plan),
                slots=fingerprint_slots,
            )
            plan.version += 1
            plan.updated_by = user.id
            plan.updated_at = now
            result = {
                "area_code": area.area_code,
                "changed_location_count": len(changed_ids),
                "plan_id": plan.id,
                "plan_version": plan.version,
                "published_map_revision": plan.published_map_revision,
                "message": f"已保存 {len(changed_ids)} 个货位的现场位置；库存、栈板和产品数量均未改变。",
            }
            db.add(
                OperationLog(
                    user_id=user.id,
                    username=user.username,
                    role=user.role,
                    action="UPDATE",
                    resource=f"warehouse/ground-layout/plans/{plan.id}/positions",
                    entity_type="warehouse_ground_layout_plan",
                    entity_id=plan.id,
                    description="管理员按现场实际拖动并保存已发布地堆货位",
                    event_category="warehouse",
                    result="success",
                    source="web",
                    module_code="warehouse",
                    action_code="warehouse.ground_layout.positions_update",
                    actor_user_id_snapshot=user.id,
                    operator_name_snapshot=user.username,
                    object_ref=f"ground-plan:{plan.id}",
                    request_id=payload.idempotency_key,
                    schema_version=1,
                    details=json.dumps(
                        {
                            "request_hash": request_hash,
                            "before": {str(key): value for key, value in before.items() if key in changed_ids},
                            "result": result,
                            "writes_inventory": False,
                        },
                        ensure_ascii=False,
                        default=str,
                    ),
                    ip_address=request.client.host if request.client else None,
                    user_agent=request.headers.get("user-agent"),
                )
            )
            db.commit()
            return {**result, "idempotent_replay": False, "writes_inventory": False}
        except (WarehouseGroundSlotError, WarehouseAreaActivationError, Floor1CandidatePlanningError, WarehouseTwinLayoutNotFoundError, ValueError) as error:
            db.rollback()
            raise HTTPException(
                status_code=getattr(error, "status_code", 409), detail=str(error)
            ) from error
        except IntegrityError as error:
            db.rollback()
            raise HTTPException(
                status_code=409, detail="货位排位发生并发冲突，请刷新后重试。"
            ) from error
        finally:
            WAREHOUSE_TWIN_LAYOUT_TRANSACTION_LOCK.release()


@router.patch("/spatial-layout/floors/{floor_code}/areas/{area_code}")
def patch_activated_area_location_layout(
    floor_code: str,
    area_code: str,
    payload: Floor3LayoutAreaPatchPayload,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    WAREHOUSE_TWIN_LAYOUT_TRANSACTION_LOCK.acquire()
    try:
        _claim_floor_projection_for_layout_write(db, floor_code=floor_code)
        route = resolve_area_location_management(
            db, floor_code=floor_code, area_code=area_code
        )
        _floor, _area, policy = _area_layout_context(
            db, floor_code=route.floor_code, area_code=route.area_code
        )
        _assert_published_ground_plan_area_unlocked(db, area_id=_area.id)
        if policy is None:
            raise WarehouseAreaActivationError(
                "历史区域尚未完成正式区域确认，请先在区域规划中确认并发布",
                status_code=409,
            )
        if payload.expected_policy_version is None:
            raise WarehouseAreaActivationError(
                "缺少区域设置版本，请刷新后重试", status_code=409
            )
        if policy.version != payload.expected_policy_version:
            raise WarehouseAreaActivationError(
                "区域设置已被其他操作更新，请刷新后重试", status_code=409
            )
        if policy.status == "published" and payload.expected_map_revision is None:
            raise WarehouseAreaActivationError(
                "缺少正式地图版本，请刷新后重试", status_code=409
            )
        if (
            payload.expected_map_revision is not None
            and policy.published_map_revision != payload.expected_map_revision
        ):
            raise WarehouseAreaActivationError(
                "正式地图版本已变化，请刷新后重试", status_code=409
            )
        requested_ids = [slot.location_id for slot in payload.slots]
        before_rows = list(
            db.scalars(
                select(WarehouseLocation)
                .options(selectinload(WarehouseLocation.floor3_layout))
                .where(
                    WarehouseLocation.id.in_(requested_ids),
                    WarehouseLocation.warehouse_floor == _floor.floor_number,
                    func.upper(WarehouseLocation.area_code) == _area.area_code.upper(),
                    WarehouseLocation.source_version == route.source_version,
                )
            ).all()
        )
        before_by_location_id = {
            row.id: _floor3_layout_dict(row.floor3_layout)
            for row in before_rows
            if row.floor3_layout is not None
        }
        if route.management_mode == "floor3_v11":
            layouts = update_layout_area(
                db,
                area_code=route.area_code,
                slots=[slot.model_dump() for slot in payload.slots],
                operator_id=user.id,
            )
            active_count = int(
                db.scalar(
                    select(func.count(WarehouseLocation.id)).where(
                        WarehouseLocation.warehouse_floor == _floor.floor_number,
                        func.upper(WarehouseLocation.area_code) == _area.area_code.upper(),
                        WarehouseLocation.source_version == route.source_version,
                        WarehouseLocation.is_active.is_(True),
                    )
                )
                or 0
            )
            _sync_formal_area_location_count(
                db,
                area=_area,
                policy=policy,
                target_count=active_count,
                operator_id=user.id,
                increment_policy_version=True,
            )
        else:
            layouts = update_area_location_layout(
                db,
                floor_code=route.floor_code,
                area_code=route.area_code,
                slots=[slot.model_dump() for slot in payload.slots],
                operator_id=user.id,
            )
        _validate_current_area_layout(
            db,
            floor_code=route.floor_code,
            area_code=route.area_code,
            source_version=route.source_version,
        )
        locations = {
            layout.location_id: db.get(WarehouseLocation, layout.location_id)
            for layout in layouts
        }
        for layout in layouts:
            location = locations[layout.location_id]
            assert location is not None
            _floor3_layout_log(
                db,
                request=request,
                user=user,
                action="UPDATE",
                location=location,
                description="保存正式区域库位二维位置",
                details={
                    "floor_code": floor_code.strip().upper(),
                    "area_code": area_code.strip().upper(),
                    "before": before_by_location_id.get(layout.location_id),
                    "after": _floor3_layout_dict(layout),
                    "inventory_changed": False,
                    "pallet_binding_changed": False,
                },
            )
        db.commit()
        return {
            **area_location_management_payload(route),
            "policy_version": policy.version,
            "published_map_revision": policy.published_map_revision,
            "items": [_floor3_layout_dict(layout) for layout in layouts],
        }
    except (WarehouseAreaActivationError, Floor3LocationError) as error:
        db.rollback()
        raise HTTPException(status_code=error.status_code, detail=str(error)) from error
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(
            status_code=409, detail="区域布局发生冲突，请刷新后重试"
        ) from error
    finally:
        WAREHOUSE_TWIN_LAYOUT_TRANSACTION_LOCK.release()


@router.post("/spatial-layout/locations/{location_id}/disable")
def disable_activated_area_location(
    location_id: int,
    payload: Floor3LayoutSlotStatePayload,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    WAREHOUSE_TWIN_LAYOUT_TRANSACTION_LOCK.acquire()
    try:
        floor_number = db.scalar(
            select(WarehouseLocation.warehouse_floor).where(
                WarehouseLocation.id == location_id
            )
        )
        if floor_number is not None:
            _claim_floor_projection_for_layout_write(
                db, floor_code=f"{int(floor_number)}F"
            )
        existing, route = resolve_location_management(db, location_id=location_id)
        before = _location_layout_state(existing)
        floor, area, policy = _area_layout_context(
            db, floor_code=route.floor_code, area_code=route.area_code
        )
        capacity_before = {"planned_pallet_capacity": area.planned_pallet_capacity,
                           "confirmed_pallet_capacity": area.confirmed_pallet_capacity,
                           "planned_location_count": area.planned_location_count}
        retained_plan = None
        if payload.retire_published_ground_slot:
            retained_plan = db.scalar(select(WarehouseGroundLayoutPlan).join(
                WarehouseGroundLayoutSlot,
                WarehouseGroundLayoutSlot.plan_id == WarehouseGroundLayoutPlan.id,
            ).where(WarehouseGroundLayoutSlot.location_id == location_id,
                    WarehouseGroundLayoutPlan.status == "published"))
            if (retained_plan is None or retained_plan.area_id != area.id
                    or retained_plan.version != payload.expected_ground_plan_version):
                raise WarehouseAreaActivationError("原排位版本或归属已变化，请刷新后重试", status_code=409)
            occupied = db.scalar(select(WarehouseGroundOccupancySlot.id).where(
                WarehouseGroundOccupancySlot.location_id == location_id,
                WarehouseGroundOccupancySlot.status == "active",
            ).limit(1))
            if occupied is not None:
                raise WarehouseAreaActivationError("该货位仍被栈板占用，不能停用（包括跨位栈板）", status_code=409)
        else:
            _assert_published_ground_plan_location_unlocked(db, location_id=location_id)
        if policy is None:
            raise WarehouseAreaActivationError(
                "历史区域尚未完成正式区域确认，请先在区域规划中确认并发布",
                status_code=409,
            )
        if payload.expected_policy_version is None:
            raise WarehouseAreaActivationError(
                "缺少区域设置版本，请刷新后重试", status_code=409
            )
        if policy.version != payload.expected_policy_version:
            raise WarehouseAreaActivationError(
                "区域设置已被其他操作更新，请刷新后重试", status_code=409
            )
        if policy.status == "published" and payload.expected_map_revision is None:
            raise WarehouseAreaActivationError(
                "缺少正式地图版本，请刷新后重试", status_code=409
            )
        if (
            payload.expected_map_revision is not None
            and policy.published_map_revision != payload.expected_map_revision
        ):
            raise WarehouseAreaActivationError(
                "正式地图版本已变化，请刷新后重试", status_code=409
            )

        if route.management_mode == "floor3_v11":
            location = set_layout_slot_active(
                db,
                location_id=location_id,
                is_active=False,
                expected_version=payload.expected_version,
                operator_id=user.id,
            )
            active_count = int(
                db.scalar(
                    select(func.count(WarehouseLocation.id)).where(
                        WarehouseLocation.warehouse_floor == floor.floor_number,
                        func.upper(WarehouseLocation.area_code) == area.area_code.upper(),
                        WarehouseLocation.source_version == route.source_version,
                        WarehouseLocation.is_active.is_(True),
                    )
                )
                or 0
            )
            _sync_formal_area_location_count(
                db,
                area=area,
                policy=policy,
                target_count=active_count,
                operator_id=user.id,
                increment_policy_version=True,
            )
        else:
            location = set_area_location_active(
                db,
                location_id=location_id,
                is_active=False,
                expected_version=payload.expected_version,
                operator_id=user.id,
            )
        active_count = int(area.planned_location_count or 0)
        if existing.storage_type == "ground" and policy.storage_layout == "pallet_ground":
            area.planned_pallet_capacity = active_count
            area.confirmed_pallet_capacity = active_count if active_count else None
            area.capacity_eligible = active_count > 0
            area.capacity_review_status = "confirmed" if active_count else "excluded"
            _apply_capacity_review(area, user=user, review_changed=True)
            _warehouse_capacity_log(
                db, request=request, user=user, action="warehouse.location.empty_retire",
                entity_type="warehouse_area", entity_id=area.id, object_ref=f"{route.floor_code}/{area.area_code}",
                before=capacity_before, after={"planned_location_count": active_count,
                    "planned_pallet_capacity": area.planned_pallet_capacity,
                    "confirmed_pallet_capacity": area.confirmed_pallet_capacity,
                    "retired_location_id": location_id, "inventory_changed": False},
            )
        if retained_plan is not None:
            from app.services.warehouse_ground_map_application import record_map_applications
            floor_layout = load_warehouse_twin_floor(route.floor_code)
            record_map_applications(db, floor_layout=floor_layout, previous_floor_layout=floor_layout,
                actor=user, operation_key=f"empty-location-retire:{location_id}:{location.floor3_layout.version}",
                request=request, retired_location_ids={location_id})
        _floor3_layout_log(
            db,
            request=request,
            user=user,
            action="UPDATE",
            location=location,
            description="逻辑停用正式区域空库位",
            details={
                "is_active": False,
                "retained_ground_plan_id": retained_plan.id if retained_plan else None,
                "retained_ground_plan_version": retained_plan.version if retained_plan else None,
                "source_version": route.source_version,
                "before": before,
                "after": _location_layout_state(location),
            },
        )
        db.commit()
        return {
            **area_location_management_payload(route),
            "policy_version": policy.version,
            "published_map_revision": policy.published_map_revision,
            "active_location_count": active_count,
            "location": _location_dict_for_db(db, location),
            "layout": _floor3_layout_dict(location.floor3_layout),
        }
    except (WarehouseAreaActivationError, Floor3LocationError) as error:
        db.rollback()
        raise HTTPException(status_code=error.status_code, detail=str(error)) from error
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="货位状态与库存或实体栈板引用发生冲突，请刷新后重试",
        ) from error
    finally:
        WAREHOUSE_TWIN_LAYOUT_TRANSACTION_LOCK.release()


@router.post("/spatial-layout/locations/{location_id}/enable")
def enable_activated_area_location(
    location_id: int,
    payload: Floor3LayoutSlotStatePayload,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    WAREHOUSE_TWIN_LAYOUT_TRANSACTION_LOCK.acquire()
    try:
        floor_number = db.scalar(
            select(WarehouseLocation.warehouse_floor).where(
                WarehouseLocation.id == location_id
            )
        )
        if floor_number is not None:
            _claim_floor_projection_for_layout_write(
                db, floor_code=f"{int(floor_number)}F"
            )
        existing, route = resolve_location_management(db, location_id=location_id)
        before = _location_layout_state(existing)
        floor, area, policy = _area_layout_context(
            db, floor_code=route.floor_code, area_code=route.area_code
        )
        _assert_published_ground_plan_location_unlocked(
            db, location_id=location_id
        )
        if policy is None:
            raise WarehouseAreaActivationError(
                "历史区域尚未完成正式区域确认，请先在区域规划中确认并发布",
                status_code=409,
            )
        if payload.expected_policy_version is None:
            raise WarehouseAreaActivationError(
                "缺少区域设置版本，请刷新后重试", status_code=409
            )
        if policy.version != payload.expected_policy_version:
            raise WarehouseAreaActivationError(
                "区域设置已被其他操作更新，请刷新后重试", status_code=409
            )
        if policy.status == "published" and payload.expected_map_revision is None:
            raise WarehouseAreaActivationError(
                "缺少正式地图版本，请刷新后重试", status_code=409
            )
        if (
            payload.expected_map_revision is not None
            and policy.published_map_revision != payload.expected_map_revision
        ):
            raise WarehouseAreaActivationError(
                "正式地图版本已变化，请刷新后重试", status_code=409
            )
        if route.management_mode == "floor3_v11":
            location = set_layout_slot_active(
                db,
                location_id=location_id,
                is_active=True,
                expected_version=payload.expected_version,
                operator_id=user.id,
            )
            active_count = int(
                db.scalar(
                    select(func.count(WarehouseLocation.id)).where(
                        WarehouseLocation.warehouse_floor == floor.floor_number,
                        func.upper(WarehouseLocation.area_code) == area.area_code.upper(),
                        WarehouseLocation.source_version == route.source_version,
                        WarehouseLocation.is_active.is_(True),
                    )
                )
                or 0
            )
            _sync_formal_area_location_count(
                db,
                area=area,
                policy=policy,
                target_count=active_count,
                operator_id=user.id,
                increment_policy_version=True,
            )
        else:
            location = set_area_location_active(
                db,
                location_id=location_id,
                is_active=True,
                expected_version=payload.expected_version,
                operator_id=user.id,
            )
        _validate_current_area_layout(
            db,
            floor_code=route.floor_code,
            area_code=route.area_code,
            source_version=route.source_version,
        )
        _floor3_layout_log(
            db,
            request=request,
            user=user,
            action="UPDATE",
            location=location,
            description="逻辑启用正式区域空库位",
            details={
                "is_active": True,
                "source_version": route.source_version,
                "before": before,
                "after": _location_layout_state(location),
            },
        )
        db.commit()
        return {
            **area_location_management_payload(route),
            "policy_version": policy.version,
            "published_map_revision": policy.published_map_revision,
            "location": _location_dict_for_db(db, location),
            "layout": _floor3_layout_dict(location.floor3_layout),
        }
    except (WarehouseAreaActivationError, Floor3LocationError) as error:
        db.rollback()
        raise HTTPException(status_code=error.status_code, detail=str(error)) from error
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="货位状态与库存或实体栈板引用发生冲突，请刷新后重试",
        ) from error
    finally:
        WAREHOUSE_TWIN_LAYOUT_TRANSACTION_LOCK.release()


@router.post("/floor3/layout/slots/{location_id}/disable")
def disable_floor3_layout_slot(
    location_id: int,
    payload: Floor3LayoutSlotStatePayload,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    WAREHOUSE_TWIN_LAYOUT_TRANSACTION_LOCK.acquire()
    try:
        existing = db.get(WarehouseLocation, location_id)
        if existing is not None and existing.area_code:
            _assert_legacy_floor3_write_allowed(db, area_code=existing.area_code)
        location = set_layout_slot_active(
            db,
            location_id=location_id,
            is_active=False,
            expected_version=payload.expected_version,
            operator_id=user.id,
        )
        _floor3_layout_log(
            db,
            request=request,
            user=user,
            action="UPDATE",
            location=location,
            description="停用三楼互动地图物理栈板位",
            details={"expected_version": payload.expected_version, "is_active": False},
        )
        db.commit()
        return {"location": _location_dict_for_db(db, location), "layout": _floor3_layout_dict(location.floor3_layout)}
    except Floor3LocationError as error:
        db.rollback()
        raise HTTPException(status_code=error.status_code, detail=str(error)) from error
    finally:
        WAREHOUSE_TWIN_LAYOUT_TRANSACTION_LOCK.release()


@router.post("/floor3/layout/slots/{location_id}/enable")
def enable_floor3_layout_slot(
    location_id: int,
    payload: Floor3LayoutSlotStatePayload,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    WAREHOUSE_TWIN_LAYOUT_TRANSACTION_LOCK.acquire()
    try:
        existing = db.get(WarehouseLocation, location_id)
        if existing is not None and existing.area_code:
            _assert_legacy_floor3_write_allowed(db, area_code=existing.area_code)
        location = set_layout_slot_active(
            db,
            location_id=location_id,
            is_active=True,
            expected_version=payload.expected_version,
            operator_id=user.id,
        )
        _floor3_layout_log(
            db,
            request=request,
            user=user,
            action="UPDATE",
            location=location,
            description="启用三楼互动地图物理栈板位",
            details={"expected_version": payload.expected_version, "is_active": True},
        )
        db.commit()
        return {"location": _location_dict_for_db(db, location), "layout": _floor3_layout_dict(location.floor3_layout)}
    except Floor3LocationError as error:
        db.rollback()
        raise HTTPException(status_code=error.status_code, detail=str(error)) from error
    finally:
        WAREHOUSE_TWIN_LAYOUT_TRANSACTION_LOCK.release()


@router.get("/floor3/locations")
def list_floor3_locations(
    q: str | None = None,
    customer_id: int | None = Query(default=None, gt=0),
    area_code: str | None = None,
    occupancy: str | None = None,
    include_inactive: bool = False,
    page_size: int = Query(default=500, ge=1, le=500),
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    if customer_id is not None:
        require_customer_access(customer_id, user, db)
    visible_customer_ids = _visible_customer_ids(user, db)

    query = select(WarehouseLocation).options(
        selectinload(WarehouseLocation.floor3_layout),
        selectinload(WarehouseLocation.address_area).selectinload(WarehouseArea.floor),
    ).where(
        WarehouseLocation.warehouse_floor == 3,
        WarehouseLocation.source_version.in_(("V11", "CURRENT_MAP")),
    )
    if not include_inactive:
        query = query.where(WarehouseLocation.is_active.is_(True))
    if area_code:
        query = query.where(WarehouseLocation.area_code == area_code.strip())

    current_location_ids = select(InventoryPallet.location_id).where(
        InventoryPallet.is_current.is_(True),
        InventoryPallet.location_id.is_not(None),
    )
    if customer_id is not None:
        customer_item_condition = InventoryPalletItem.customer_id == customer_id
        if visible_customer_ids is not None:
            customer_item_condition = _floor3_item_scope_condition({customer_id})
        customer_location_ids = (
            select(InventoryPallet.location_id)
            .join(
                InventoryPalletItem,
                InventoryPalletItem.pallet_id == InventoryPallet.id,
            )
            .where(
                InventoryPallet.is_current.is_(True),
                InventoryPallet.location_id.is_not(None),
                customer_item_condition,
            )
        )
        query = query.where(WarehouseLocation.id.in_(customer_location_ids))
    if occupancy == "occupied":
        query = query.where(WarehouseLocation.id.in_(current_location_ids))
    elif occupancy == "empty":
        query = query.where(WarehouseLocation.id.not_in(current_location_ids))
    elif occupancy:
        raise HTTPException(status_code=400, detail="货位占用状态无效")

    keyword = (q or "").strip()
    if keyword:
        pattern = f"%{keyword}%"
        pallet_search_conditions = [
            InventoryPalletItem.inventory_code.like(pattern),
            InventoryPalletItem.order_no.like(pattern),
            InventoryPalletItem.product_name.like(pattern),
            Customer.name.like(pattern),
            Customer.customer_code.like(pattern),
        ]
        if visible_customer_ids is None:
            pallet_search_conditions.append(InventoryPallet.pallet_code.like(pattern))
        matching_location_ids = (
            select(InventoryPallet.location_id)
            .outerjoin(
                InventoryPalletItem,
                InventoryPalletItem.pallet_id == InventoryPallet.id,
            )
            .outerjoin(Customer, Customer.id == InventoryPalletItem.customer_id)
            .where(
                InventoryPallet.is_current.is_(True),
                InventoryPallet.location_id.is_not(None),
                or_(*pallet_search_conditions),
            )
        )
        if visible_customer_ids is not None:
            matching_location_ids = matching_location_ids.where(
                _floor3_item_scope_condition(visible_customer_ids)
            )
        query = query.where(
            or_(
                WarehouseLocation.location_code.like(pattern),
                WarehouseLocation.location_name.like(pattern),
                WarehouseLocation.area_code.like(pattern),
                WarehouseLocation.id.in_(matching_location_ids),
            )
        )

    locations = db.scalars(
        query.order_by(
            WarehouseLocation.sort_order,
            WarehouseLocation.location_code,
        ).limit(page_size)
    ).all()
    location_ids = [row.id for row in locations]
    pallets = (
        db.scalars(
            _floor3_pallet_query().where(
                InventoryPallet.location_id.in_(location_ids),
                InventoryPallet.is_current.is_(True),
            )
        ).all()
        if location_ids
        else []
    )
    pallets_by_location = {row.location_id: row for row in pallets}
    customer_names = _floor3_customer_names(db, pallets)
    projection_contexts = load_warehouse_location_projection_contexts(
        db, locations
    )
    return {
        "items": [
            _floor3_location_dict(
                row,
                pallet=pallets_by_location.get(row.id),
                visible_customer_ids=visible_customer_ids,
                customer_names=customer_names,
                projection_context=projection_contexts.get(int(row.id)),
            )
            for row in locations
        ],
        "total": len(locations),
    }


@router.get("/floor3/locations/{location_id}")
def get_floor3_location(
    location_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    location = db.scalar(
        select(WarehouseLocation).options(
            selectinload(WarehouseLocation.floor3_layout),
            selectinload(WarehouseLocation.address_area).selectinload(WarehouseArea.floor),
        ).where(
            WarehouseLocation.id == location_id,
            WarehouseLocation.warehouse_floor == 3,
            WarehouseLocation.source_version.in_(("V11", "CURRENT_MAP")),
        )
    )
    if location is None:
        raise HTTPException(status_code=404, detail="三楼货位不存在")
    pallet = db.scalar(
        _floor3_pallet_query().where(
            InventoryPallet.location_id == location.id,
            InventoryPallet.is_current.is_(True),
        )
    )
    visible_customer_ids = _visible_customer_ids(user, db)
    customer_names = _floor3_customer_names(db, [pallet] if pallet else [])
    history_rows = db.scalars(
        select(InventoryLocationMovement)
        .where(
            or_(
                InventoryLocationMovement.from_location_id == location.id,
                InventoryLocationMovement.to_location_id == location.id,
            )
        )
        .order_by(
            InventoryLocationMovement.moved_at.desc(),
            InventoryLocationMovement.id.desc(),
        )
        .limit(50)
    ).all()
    history_pallet_ids = {row.pallet_id for row in history_rows}
    history_pallets = (
        db.scalars(
            _floor3_pallet_query().where(InventoryPallet.id.in_(history_pallet_ids))
        ).all()
        if history_pallet_ids
        else []
    )
    history_pallet_map = {row.id: row for row in history_pallets}
    location_ids = {
        value
        for movement in history_rows
        for value in (movement.from_location_id, movement.to_location_id)
        if value is not None
    }
    history_locations = (
        {
            row.id: row
            for row in db.scalars(
                select(WarehouseLocation)
                .options(
                    selectinload(WarehouseLocation.address_area).selectinload(
                        WarehouseArea.floor
                    )
                )
                .where(WarehouseLocation.id.in_(location_ids))
            ).all()
        }
        if location_ids
        else {}
    )
    projection_contexts = load_warehouse_location_projection_contexts(
        db,
        [location, *history_locations.values()],
    )
    history = []
    for movement in history_rows:
        history_pallet = history_pallet_map.get(movement.pallet_id)
        if history_pallet is None:
            continue
        if visible_customer_ids is not None and any(
            not _floor3_item_visible(item, visible_customer_ids)
            for item in history_pallet.items
        ):
            continue
        history.append(
            {
                "id": movement.id,
                "pallet_id": movement.pallet_id,
                "pallet_code": history_pallet.pallet_code,
                "movement_type": movement.movement_type,
                "from_location_id": movement.from_location_id,
                "from_location_code": (
                    history_locations[movement.from_location_id].location_code
                    if movement.from_location_id in history_locations
                    else None
                ),
                "from_location_name": employee_location_name(
                    history_locations.get(movement.from_location_id),
                    area=(
                        projection_contexts.get(
                            int(movement.from_location_id or 0), {}
                        ).get("area")
                    ),
                    floor=(
                        projection_contexts.get(
                            int(movement.from_location_id or 0), {}
                        ).get("floor")
                    ),
                    area_sequence=projection_contexts.get(
                        int(movement.from_location_id or 0), {}
                    ).get("area_sequence"),
                ),
                "to_location_id": movement.to_location_id,
                "to_location_code": (
                    history_locations[movement.to_location_id].location_code
                    if movement.to_location_id in history_locations
                    else None
                ),
                "to_location_name": employee_location_name(
                    history_locations.get(movement.to_location_id),
                    area=(
                        projection_contexts.get(
                            int(movement.to_location_id or 0), {}
                        ).get("area")
                    ),
                    floor=(
                        projection_contexts.get(
                            int(movement.to_location_id or 0), {}
                        ).get("floor")
                    ),
                    area_sequence=projection_contexts.get(
                        int(movement.to_location_id or 0), {}
                    ).get("area_sequence"),
                ),
                "operator_id": movement.operator_id,
                "moved_at": beijing_naive_to_api(movement.moved_at),
                "remarks": movement.remarks,
            }
        )
    return {
        **_floor3_location_dict(
            location,
            pallet=pallet,
            visible_customer_ids=visible_customer_ids,
            customer_names=customer_names,
            projection_context=projection_contexts.get(int(location.id)),
        ),
        "movement_history": history,
    }


@router.get("/floor3/product-candidates")
def floor3_product_candidates(
    q: str = Query(default="", max_length=150),
    customer_id: int | None = Query(default=None, gt=0),
    limit: int = Query(default=30, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(_can_locate_twin),
) -> dict:
    from app.services.stocktake_spec_search import parse_dimensions, dimension_score
    keyword = q.strip()
    dimensions = parse_dimensions(keyword)
    if not keyword and customer_id is None:
        raise HTTPException(status_code=400, detail="请先选择客户，或输入存货编码、订单号或产品名称")
    if customer_id is not None:
        require_customer_access(customer_id, user, db)
    visible_customer_ids = _visible_customer_ids(user, db)
    pattern = f"%{keyword}%"

    order_rows_query = (
        select(
            OrderItem.product_id,
            Order.customer_id,
            Order.order_number,
            Order.customer_po,
            OrderItem.item_order_number,
        )
        .join(Order, Order.id == OrderItem.order_id)
        .where(
            or_(
                Order.order_number.like(pattern),
                Order.customer_po.like(pattern),
                OrderItem.item_order_number.like(pattern),
            )
        )
    )
    if customer_id is not None:
        order_rows_query = order_rows_query.where(Order.customer_id == customer_id)
    elif visible_customer_ids is not None:
        order_rows_query = order_rows_query.where(
            Order.customer_id.in_(visible_customer_ids)
        )
    order_rows = db.execute(order_rows_query.limit(limit * 5)).all()
    order_product_ids = {row.product_id for row in order_rows}

    product_query = (
        select(Product, Customer)
        .join(Customer, Customer.id == Product.customer_id)
        .where(
            Product.is_active.is_(True),
            Product.deleted_at.is_(None),
            or_(
                Product.product_code.like(pattern),
                Product.customer_material_code.like(pattern),
                Product.product_name.like(pattern),
                (cast(Product.length_mm, String) + "×" + cast(Product.width_mm, String) + "×" + func.coalesce(cast(Product.height_mm, String), "")).like("%" + "".join(keyword.split()).replace("*", "×").replace("x", "×").replace("X", "×") + "%"),
                Customer.name.like(pattern),
                Product.id.in_(order_product_ids) if order_product_ids else False,
            ),
        )
    )
    if customer_id is not None:
        product_query = product_query.where(Product.customer_id == customer_id)
    elif visible_customer_ids is not None:
        product_query = product_query.where(Product.customer_id.in_(visible_customer_ids))
    if dimensions:
        # Rank the entire visible product scope before limiting; a late exact match must win.
        product_query = select(Product, Customer).join(Customer, Customer.id == Product.customer_id).where(
            Product.is_active.is_(True), Product.deleted_at.is_(None),
            Product.length_mm > 0, Product.width_mm > 0)
        if len(dimensions) == 3:
            product_query = product_query.where(Product.height_mm > 0)
        if customer_id is not None:
            product_query = product_query.where(Product.customer_id == customer_id)
        elif visible_customer_ids is not None:
            product_query = product_query.where(Product.customer_id.in_(visible_customer_ids))
        products = db.execute(product_query).all()
    else:
        products = db.execute(product_query.limit(limit * 3)).all()

    orders_by_product: dict[int, list[str]] = {}
    exact_order_product_ids: set[int] = set()
    keyword_folded = keyword.casefold()
    for row in order_rows:
        values = [row.order_number, row.customer_po, row.item_order_number]
        labels = [str(value) for value in values if value]
        orders_by_product.setdefault(row.product_id, []).extend(labels)
        if any(str(value).strip().casefold() == keyword_folded for value in values if value):
            exact_order_product_ids.add(row.product_id)

    candidates = []
    for product, customer in products:
        code_values = [product.product_code, product.customer_material_code]
        code_exact = any(
            str(value).strip().casefold() == keyword_folded
            for value in code_values
            if value
        )
        name_exact = product.product_name.strip().casefold() == keyword_folded
        order_exact = product.id in exact_order_product_ids
        if code_exact:
            priority, match_type = 0, "customer_inventory_code_exact"
        elif order_exact:
            priority, match_type = 1, "customer_order_exact"
        elif name_exact:
            priority, match_type = 2, "customer_product_name_exact"
        else:
            priority, match_type = 3, "fuzzy_candidate"
        score = dimension_score(dimensions, product) if dimensions else None
        candidates.append(
            {
                "priority": (0 if score == 100 else 3) if dimensions else priority,
                "match_score": score,
                "customer_short_name": customer.chinese_short_name,
                "match_type": ("specification_exact" if score == 100 else "specification_similar") if dimensions else match_type,
                "is_exact": score == 100 if dimensions else priority < 3,
                "product_id": product.id,
                "customer_id": customer.id,
                "customer_name": customer.name,
                "product_code": product.product_code,
                "customer_material_code": product.customer_material_code,
                "product_name": product.product_name,
                "specification": product_dimension_specification(product),
                "matched_order_numbers": sorted(
                    set(orders_by_product.get(product.id, []))
                )[:10],
                "is_tianhua": "天华" in customer.name,
            }
        )
    candidates.sort(
        key=lambda row: (
            -(row["match_score"] or 0) if dimensions else row["priority"],
            row["customer_name"],
            row["product_code"] or "",
            row["product_id"],
        )
    )
    items = candidates[:limit]
    return {
        "items": items,
        "total": len(items),
        "selection_required": bool(items),
        "auto_bind_allowed": False,
        "message": "请选择确认的产品；系统不会自动猜测或创建产品。",
    }


def _twin_finished_target_location(
    db: Session,
    location_id: int,
    *,
    require_empty: bool = False,
) -> WarehouseLocation:
    location = db.get(WarehouseLocation, location_id)
    if location is None:
        raise HTTPException(status_code=404, detail="目标货位不存在")
    if location.warehouse_floor not in {1, 3, 4}:
        raise HTTPException(status_code=409, detail="只能选择一楼、三楼或四楼地图中的正式货位")
    # 4F is imported as a planning-only scan.  It must not become an inventory
    # destination merely because a stale/manual ledger row happens to be
    # active and marked ``placed``.  Requiring the published projection here
    # keeps every caller below fail-closed until site calibration and the
    # normal map/area publication workflow have both completed.  Keep the
    # existing 1F/3F compatibility path unchanged.
    require_floor4_publication = location.warehouse_floor == 4
    issue = operational_location_issue(
        db,
        location,
        warehouse_types={"finished", "shared"},
        require_published=require_floor4_publication,
        require_map_geometry=require_floor4_publication,
        required_inventory_type=(
            "finished" if require_floor4_publication else None
        ),
    )
    if issue:
        raise HTTPException(status_code=409, detail=f"目标货位不可用：{issue}")
    if require_empty:
        current_pallet = db.scalar(
            select(InventoryPallet.id).where(
                InventoryPallet.location_id == location.id,
                InventoryPallet.is_current.is_(True),
            )
        )
        if current_pallet is not None:
            raise HTTPException(status_code=409, detail="目标货位已有实体栈板，请选择空货位")
    return location


def _twin_location_live_lots(db: Session, location_id: int) -> list[InventoryLot]:
    physical_quantity = (
        InventoryLot.quantity_available
        + InventoryLot.quantity_reserved
        + InventoryLot.quantity_damaged
    )
    return list(
        db.scalars(
            _lot_query().where(
                InventoryLot.warehouse_location_id == location_id,
                InventoryLot.status.in_(("active", "frozen")),
                physical_quantity > 0,
            )
        ).unique().all()
    )


def _twin_assert_finished_merge_compatible(
    db: Session,
    *,
    location_id: int,
    customer_id: int,
    product_id: int,
    current_pallet: InventoryPallet | None,
) -> None:
    """An occupied map location may only receive the exact same finished product."""

    managed_ground_slot = db.scalar(
        select(WarehouseGroundLayoutSlot.id)
        .join(WarehouseGroundLayoutPlan)
        .where(
            WarehouseGroundLayoutSlot.location_id == location_id,
            WarehouseGroundLayoutPlan.status == "published",
        )
        .limit(1)
    )
    if managed_ground_slot is not None:
        raise HTTPException(
            status_code=409,
            detail="该位置已纳入地堆排位，请使用地图点选存放入口校验容量和占用关系",
        )

    if current_pallet is not None:
        for item in current_pallet.items:
            if (
                item.item_type != "finished"
                or item.customer_id != customer_id
                or item.product_id != product_id
            ):
                raise HTTPException(
                    status_code=409,
                    detail="当前库位已有其他客户或其他纸箱，只能选择空位或同客户同存货编码库位",
                )
    for lot in _twin_location_live_lots(db, location_id):
        detail = lot.finished_detail
        if (
            lot.inventory_type != "finished"
            or detail is None
            or detail.owner_customer_id != customer_id
            or detail.product_id != product_id
        ):
            raise HTTPException(
                status_code=409,
                detail="当前库位已有其他客户、库存类型或纸箱，只能合并到同产品库位",
            )


def _twin_assert_semi_finished_merge_compatible(
    db: Session,
    *,
    location_id: int,
    customer_id: int,
    product_id: int,
) -> None:
    """Semi-finished co-location requires the same customer and confirmed product binding."""

    for lot in _twin_location_live_lots(db, location_id):
        detail = lot.semi_finished_detail
        if (
            lot.inventory_type != "semi_finished"
            or detail is None
            or detail.owner_customer_id != customer_id
            or product_id not in set(semi_finished_lot_allowed_product_ids(db, lot.id))
        ):
            raise HTTPException(
                status_code=409,
                detail="当前库位已有不同客户、不同半成品款号或其他库存类型，请改选兼容库位",
            )


@router.get("/twin-operations/location-product-candidates")
def twin_location_product_candidates(
    location_id: int = Query(gt=0),
    q: str = Query(default="", max_length=150),
    limit: int = Query(default=30, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    """List existing not-yet-delivered production stock for one chosen location."""

    location = _twin_finished_target_location(db, location_id, require_empty=True)
    live_quantity = InventoryLot.quantity_available + InventoryLot.quantity_reserved
    query = (
        select(
            InventoryLot,
            FinishedGoodsInventoryDetail,
            WarehouseLocation,
            Customer,
            Product,
        )
        .join(
            FinishedGoodsInventoryDetail,
            FinishedGoodsInventoryDetail.inventory_lot_id == InventoryLot.id,
        )
        .join(
            WarehouseLocation,
            WarehouseLocation.id == InventoryLot.warehouse_location_id,
        )
        .outerjoin(Customer, Customer.id == FinishedGoodsInventoryDetail.owner_customer_id)
        .outerjoin(Product, Product.id == FinishedGoodsInventoryDetail.product_id)
        .where(
            WarehouseLocation.location_code == "F1-DISPATCH-01",
            InventoryLot.inventory_type == "finished",
            InventoryLot.status == "active",
            InventoryLot.source_type.in_(("production_completion", "transfer")),
            InventoryLot.source_ref_type == "production_completion",
            InventoryLot.source_ref_id.is_not(None),
            ~InventoryLot.pallet_item.has(),
            live_quantity > 0,
        )
    )
    visible_customer_ids = _visible_customer_ids(user, db)
    if visible_customer_ids is not None:
        query = query.where(
            FinishedGoodsInventoryDetail.owner_customer_id.in_(visible_customer_ids)
        )
    keyword = q.strip()
    if keyword:
        pattern = f"%{keyword}%"
        query = query.where(
            or_(
                InventoryLot.lot_number.like(pattern),
                FinishedGoodsInventoryDetail.inventory_code_snapshot.like(pattern),
                FinishedGoodsInventoryDetail.product_name_snapshot.like(pattern),
                FinishedGoodsInventoryDetail.owner_customer_name_snapshot.like(pattern),
                Customer.name.like(pattern),
                Product.product_code.like(pattern),
                Product.customer_material_code.like(pattern),
                Product.product_name.like(pattern),
            )
        )
    rows = db.execute(
        query.order_by(InventoryLot.stock_date.desc(), InventoryLot.id.desc()).limit(limit)
    ).all()
    projection_contexts = load_warehouse_location_projection_contexts(
        db,
        [location, *[row[2] for row in rows]],
    )

    def readable_location_name(row: WarehouseLocation) -> str:
        context = projection_contexts.get(int(row.id), {})
        return employee_location_name(
            row,
            area=context.get("area"),
            floor=context.get("floor"),
            area_sequence=context.get("area_sequence"),
        )

    items = []
    for lot, detail, source_location, customer, product in rows:
        available = int(lot.quantity_available or 0)
        reserved = int(lot.quantity_reserved or 0)
        items.append(
            {
                "lot_id": lot.id,
                "lot_number": lot.lot_number,
                "version": lot.version,
                "customer_id": detail.owner_customer_id,
                "customer_name": (
                    customer.name
                    if customer is not None
                    else detail.owner_customer_name_snapshot
                ),
                "product_id": detail.product_id,
                "inventory_code": detail.inventory_code_snapshot,
                "product_name": detail.product_name_snapshot,
                "available_quantity": available,
                "reserved_quantity": reserved,
                "total_quantity": available + reserved,
                "unit": lot.unit,
                "source_location_id": source_location.id,
                "source_location_code": source_location.location_code,
                "source_location_name": readable_location_name(source_location),
                "source_location_master_name": source_location.location_name,
                "stock_date": lot.stock_date,
            }
        )
    return {
        "target_location": {
            "location_id": location.id,
            "location_code": location.location_code,
            "location_name": readable_location_name(location),
            "location_master_name": location.location_name,
            "area_code": location.area_code,
        },
        "items": items,
        "total": len(items),
        "message": "从当前空货位选择已完工未送货产品；确认后只移动原库存，不增加数量。",
    }


@router.get("/twin-operations/pending-relocation/preview")
def preview_twin_pending_relocation(db: Session = Depends(get_db), user: User = Depends(admin_only)) -> dict:
    from app.services.warehouse_relocation_pending import preview_pending_relocation
    return preview_pending_relocation(db)


@router.post("/twin-operations/pending-relocation/reset")
def reset_twin_pending_relocation(payload: PendingRelocationResetPayload, request: Request,
                                 db: Session = Depends(get_db), user: User = Depends(admin_only)) -> dict:
    from app.services.warehouse_relocation_pending import PendingRelocationError, reset_to_pending_relocation
    try:
        result = reset_to_pending_relocation(db, actor=user, request=request,
            expected_fingerprint=payload.expected_fingerprint, operation_key=payload.idempotency_key)
        db.commit()
        return result
    except PendingRelocationError as error:
        db.rollback()
        raise HTTPException(status_code=error.status_code, detail=str(error)) from error
    except Exception:
        db.rollback()
        raise


@router.post("/twin-operations/pending-lots/{lot_id}/place")
def place_twin_pending_lot(lot_id: int, payload: TwinStagingPlacementPayload, request: Request,
                           db: Session = Depends(get_db), user: User = Depends(admin_only)) -> dict:
    from app.services.warehouse_inventory import transfer_pending_finished_lot
    target = _twin_finished_target_location(db, payload.location_id)
    source = _require_lot_customer_access(db, lot_id, user)
    before = _inventory_lot_audit_state(source)
    try:
        result = transfer_pending_finished_lot(db, lot_id=lot_id, expected_version=payload.expected_version,
            quantity=payload.quantity, location_id=target.id, operator_id=user.id,
            idempotency_key=payload.idempotency_key, expected_target_layout_version=payload.expected_layout_version,
            expected_target_address_version=payload.expected_address_version,
            expected_target_map_revision=payload.expected_map_revision)
        if not result.replayed:
            from app.services.receipt_putaway import remember_stocktake
            remember_stocktake(db, result.target_lot, user.id)
            append_audit_event(db, request=request, actor=user, event_category="business", result="success",
                source="web", module_code="warehouse", action_code="warehouse.recount.pending.place",
                resource="InventoryLotTransfer", entity_id=result.transfer.id,
                description="待归位已有货物放入所选货位，未新增库存",
                details={"before": before, "source_after": _inventory_lot_audit_state(result.source_lot),
                         "target_after": _inventory_lot_audit_state(result.target_lot),
                         "idempotency_key": payload.idempotency_key})
        db.commit()
        return {"message": "已归位，库存总数未增加", "replayed": result.replayed,
                "target_lot_id": result.target_lot.id, "target_location_id": target.id,
                "quantity": payload.quantity}
    except (WarehouseInventoryError, Floor3LocationError) as error:
        db.rollback()
        raise HTTPException(status_code=error.status_code, detail=str(error)) from error
    except Exception:
        db.rollback()
        raise


@router.post("/twin-operations/staging-lots/{lot_id}/place")
def place_twin_staging_lot(
    lot_id: int,
    payload: TwinStagingPlacementPayload,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    """Move an existing floor-one staging lot into the selected empty map location."""

    target = _twin_finished_target_location(db, payload.location_id)
    source_lot = _require_lot_customer_access(db, lot_id, user)
    before = _inventory_lot_audit_state(source_lot)
    customer_id, customer_name = _inventory_lot_audit_customer(source_lot)
    try:
        result = transfer_staging_finished_lot(
            db,
            lot_id=lot_id,
            expected_version=payload.expected_version,
            quantity=payload.quantity,
            location_id=target.id,
            operator_id=user.id,
            idempotency_key=payload.idempotency_key,
            expected_target_layout_version=payload.expected_layout_version,
        )
        pallet_item = result.target_lot.pallet_item
        if pallet_item is None:
            raise WarehouseInventoryError("转入批次未能绑定目标实体栈板", 409)
        pallet = _floor3_get_pallet(db, pallet_item.pallet_id)
        if not result.replayed:
            append_audit_event(
                db,
                request=request,
                actor=user,
                event_category="business",
                result="success",
                source="web",
                module_code="warehouse",
                action_code="warehouse.twin.staging_lot.place",
                legacy_action="TWIN_PLACE_STAGING_LOT",
                resource="InventoryLotTransfer",
                entity_type="inventory_lot_transfer",
                entity_id=result.transfer.id,
                object_ref=f"inventory_lot_transfer:{result.transfer.id}",
                customer_id=customer_id,
                customer_name=customer_name,
                description="数字孪生空货位确认接收一楼待送成品",
                details={
                    "source_lot_id": lot_id,
                    "target_lot_id": result.target_lot.id,
                    "quantity": payload.quantity,
                    "source_location_id": result.transfer.source_location_id,
                    "target_location_id": result.transfer.target_location_id,
                    "before": before,
                    "source_after": _inventory_lot_audit_state(result.source_lot),
                    "target_after": _inventory_lot_audit_state(result.target_lot),
                    "idempotency_key": payload.idempotency_key,
                },
            )
            _floor3_log(
                db,
                request=request,
                user=user,
                action="UPDATE",
                pallet=pallet,
                description="数字孪生货位主动选择已完工未送产品",
                details={
                    "source_lot_id": lot_id,
                    "target_lot_id": result.target_lot.id,
                    "quantity": payload.quantity,
                    "location_id": target.id,
                    "idempotency_key": payload.idempotency_key,
                },
            )
        db.commit()
        return {
            "message": "已将一楼待送产品转入当前货位，库存总数未改变",
            "idempotent_replay": result.replayed,
            "transfer": _lot_location_transfer_dict(
                db,
                result.transfer,
                source_lot=result.source_lot,
                target_lot=result.target_lot,
                replayed=result.replayed,
            ),
            "pallet": _floor3_pallet_response(db, pallet, user),
        }
    except WarehouseInventoryError as error:
        db.rollback()
        _handle(error)
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="待送批次、目标货位或请求标识已变化，请刷新后重试",
        ) from error


@router.post("/twin-operations/finished-inbound", status_code=201)
def create_twin_finished_inbound(
    payload: TwinFinishedInboundPayload,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    """Create one formal finished lot and its physical pallet from the map."""

    item = Floor3PalletItemPayload(
        customer_id=payload.customer_id,
        product_id=payload.product_id,
        item_type="finished",
        quantity=Decimal(payload.quantity),
        unit="boxes",
        match_status="matched",
        create_finished_inventory=True,
        stock_date=payload.stock_date,
        idempotency_key=payload.idempotency_key,
        remarks=payload.remarks,
    )
    _require_floor3_item_customer_access(db, item, user)
    existing_movement = db.scalar(
        select(InventoryMovement).where(
            InventoryMovement.idempotency_key == payload.idempotency_key,
            InventoryMovement.movement_type == "manual_in",
        )
    )
    replayed = existing_movement is not None
    if existing_movement is not None:
        existing_lot = db.get(InventoryLot, existing_movement.inventory_lot_id)
        existing_detail = existing_lot.finished_detail if existing_lot is not None else None
        if (
            existing_lot is None
            or existing_detail is None
            or existing_lot.warehouse_location_id != payload.location_id
            or existing_detail.owner_customer_id != payload.customer_id
            or existing_detail.product_id != payload.product_id
            or int(existing_movement.quantity or 0) != payload.quantity
        ):
            raise HTTPException(status_code=409, detail="同一请求标识已用于其他库存补录")
    try:
        current_pallet = db.scalar(
            _floor3_pallet_query().where(
                InventoryPallet.location_id == payload.location_id,
                InventoryPallet.is_current.is_(True),
            )
        )
        _twin_assert_finished_merge_compatible(
            db,
            location_id=payload.location_id,
            customer_id=payload.customer_id,
            product_id=payload.product_id,
            current_pallet=current_pallet,
        )
        if current_pallet is None:
            row = create_pallet(
                db,
                location_id=payload.location_id,
                pallet_code=payload.pallet_code,
                items=[item.model_dump()],
                remarks=payload.remarks,
                operator_id=user.id,
                expected_layout_version=payload.expected_layout_version,
            )
        else:
            lot = manual_finished_in(
                db,
                customer_id=payload.customer_id,
                product_id=payload.product_id,
                location_id=payload.location_id,
                quantity=payload.quantity,
                stock_date=payload.stock_date,
                source_type="manual",
                remarks=payload.remarks,
                operator_id=user.id,
                idempotency_key=payload.idempotency_key,
                pallet_id=current_pallet.id,
                expected_layout_version=payload.expected_layout_version,
            )
            row = _floor3_get_pallet(
                db,
                lot.pallet_item.pallet_id if lot.pallet_item is not None else current_pallet.id,
            )
        if not replayed:
            _floor3_log(
                db,
                request=request,
                user=user,
                action="CREATE",
                pallet=row,
                description="数字孪生地图确认入成品仓",
                details={
                    "location_id": payload.location_id,
                    "customer_id": payload.customer_id,
                    "product_id": payload.product_id,
                    "quantity": payload.quantity,
                    "stock_date": payload.stock_date,
                    "idempotency_key": payload.idempotency_key,
                },
            )
        db.commit()
        return {
            "message": "已从地图确认补录到当前库位" if current_pallet is not None else "已从地图确认入成品仓",
            "idempotent_replay": replayed,
            "pallet": _floor3_pallet_response(db, row, user),
        }
    except Floor3LocationError as error:
        db.rollback()
        raise HTTPException(status_code=error.status_code, detail=str(error)) from error
    except WarehouseInventoryError as error:
        db.rollback()
        _handle(error)
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="目标货位、栈板或入库幂等键已发生冲突，请刷新后重试",
        ) from error


@router.post("/twin-operations/semi-finished-inbound", status_code=201)
def create_twin_semi_finished_inbound(
    payload: TwinSemiFinishedInboundPayload,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    """Create a product-bound semi-finished lot at one map-selected 1F location."""

    require_customer_access(payload.customer_id, user, db)
    location = db.get(WarehouseLocation, payload.location_id)
    if location is None:
        raise HTTPException(status_code=404, detail="目标货位不存在")
    issue = operational_location_issue(
        db,
        location,
        warehouse_types={"semi_finished", "shared"},
        pallet_storage_only=True,
    )
    if issue:
        raise HTTPException(status_code=409, detail=f"目标货位不可用：{issue}")
    product = db.get(Product, payload.product_id)
    if (
        product is None
        or product.deleted_at is not None
        or not product.is_active
        or product.customer_id != payload.customer_id
    ):
        raise HTTPException(status_code=409, detail="所选常用箱不属于当前客户或已停用")

    material = product.material
    material_code = str(
        (material.code if material is not None else None)
        or product.default_material_code
        or product.legacy_material_text
        or ""
    ).strip()
    layer_count = product.layer_count or (material.layer_count if material is not None else None)
    flute_type = str(product.flute_type or "").strip().upper()
    board_length = product.report_length_mm or product.default_cardboard_length
    board_width = product.report_width_mm or product.default_cardboard_width
    if not material_code or not layer_count or not flute_type or not board_length or not board_width:
        raise HTTPException(
            status_code=409,
            detail="该常用箱缺少材质、楞型或报料长宽，不能直接补录半成品",
        )

    existing_movement = db.scalar(
        select(InventoryMovement).where(
            InventoryMovement.idempotency_key == payload.idempotency_key,
            InventoryMovement.movement_type == "manual_in",
        )
    )
    replayed = existing_movement is not None
    if existing_movement is not None:
        existing_lot = db.get(InventoryLot, existing_movement.inventory_lot_id)
        existing_detail = existing_lot.semi_finished_detail if existing_lot is not None else None
        if (
            existing_lot is None
            or existing_detail is None
            or existing_lot.warehouse_location_id != payload.location_id
            or existing_detail.owner_customer_id != payload.customer_id
            or payload.product_id not in set(semi_finished_lot_allowed_product_ids(db, existing_lot.id))
            or int(existing_movement.quantity or 0) != payload.quantity
        ):
            raise HTTPException(status_code=409, detail="同一请求标识已用于其他半成品补录")
    try:
        _twin_assert_semi_finished_merge_compatible(
            db,
            location_id=payload.location_id,
            customer_id=payload.customer_id,
            product_id=payload.product_id,
        )
        crease_text = str(product.crease_type or "").strip()
        sheet_type = (
            "creased_sheet"
            if "压线" in crease_text
            else "net_sheet"
            if "净" in crease_text
            else "raw_board"
        )
        lot = manual_semi_finished_in(
            db,
            location_id=payload.location_id,
            quantity=payload.quantity,
            stock_date=payload.stock_date,
            source_type="manual",
            material_code=material_code,
            material_id=product.material_id,
            layer_count=int(layer_count),
            flute_type=flute_type,
            board_length_mm=int(board_length),
            board_width_mm=int(board_width),
            sheet_type=sheet_type,
            supplier_name=None,
            customer_id=payload.customer_id,
            crease_type=product.crease_type,
            crease_left_mm=product.crease_left_mm,
            crease_middle_mm=product.crease_middle_mm,
            crease_right_mm=product.crease_right_mm,
            cutting_note=product.report_notes,
            remarks=payload.remarks,
            operator_id=user.id,
            idempotency_key=payload.idempotency_key,
            expected_layout_version=payload.expected_layout_version,
            movement_reason="数字孪生地图半成品差异补录",
        )
        lot = replace_semi_finished_lot_allowed_products(
            db,
            inventory_lot_id=lot.id,
            product_ids=[payload.product_id],
            expected_version=lot.version,
            operator_id=user.id,
        )
        if not replayed:
            _append_inventory_lot_audit(
                db,
                request=request,
                user=user,
                action_code="warehouse.twin.semi_finished.manual_in",
                row=lot,
                before=None,
                reason=payload.remarks,
                idempotency_key=payload.idempotency_key,
            )
        db.commit()
        return {
            "message": "已从地图确认补录半成品；库存数量与真实位置已保存",
            "idempotent_replay": replayed,
            "lot": _lot_dict_for_db(db, lot),
        }
    except WarehouseInventoryError as error:
        db.rollback()
        _handle(error)
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="目标货位、半成品或请求标识已发生冲突，请刷新后重试",
        ) from error


@router.post("/twin-operations/temporary-finished-inbound", status_code=201)
def create_twin_temporary_finished_inbound(
    payload: TwinTemporaryFinishedInboundPayload,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    """Create one explicitly marked temporary product and its first stock lot atomically."""

    target = _twin_finished_target_location(db, payload.location_id)
    require_customer_access(payload.customer_id, user, db)
    existing_movement = db.scalar(
        select(InventoryMovement).where(
            InventoryMovement.idempotency_key == payload.idempotency_key,
            InventoryMovement.movement_type == "manual_in",
        )
    )
    if existing_movement is not None:
        lot = db.get(InventoryLot, existing_movement.inventory_lot_id)
        detail = lot.finished_detail if lot is not None else None
        pallet_item = lot.pallet_item if lot is not None else None
        live_quantity = (
            int(lot.quantity_available or 0) + int(lot.quantity_reserved or 0)
            if lot is not None
            else 0
        )
        if (
            lot is None
            or detail is None
            or pallet_item is None
            or lot.warehouse_location_id != target.id
            or detail.owner_customer_id != payload.customer_id
            or (detail.inventory_code_snapshot or "").strip() != payload.inventory_code
            or live_quantity != payload.quantity
        ):
            raise HTTPException(status_code=409, detail="同一请求标识已用于其他临时产品入位")
        pallet = _floor3_get_pallet(db, pallet_item.pallet_id)
        return {
            "message": "该临时产品已完成入位",
            "idempotent_replay": True,
            "temporary_product_id": detail.product_id,
            "pallet": _floor3_pallet_response(db, pallet, user),
        }

    customer = db.get(Customer, payload.customer_id)
    if customer is None or not customer.is_active:
        raise HTTPException(status_code=404, detail="客户不存在或已停用")
    inventory_code = clean_code(payload.inventory_code)
    product_name = payload.product_name.strip()
    duplicate = db.scalar(
        select(Product).where(
            Product.customer_id == customer.id,
            Product.product_name == product_name,
            or_(
                func.lower(Product.product_code) == inventory_code.casefold(),
                func.lower(Product.customer_material_code) == inventory_code.casefold(),
            ),
        )
    )
    if duplicate is not None:
        raise HTTPException(
            status_code=409,
            detail="该客户已有相同存货编码和产品名称，请返回“ERP 已有产品”选择现有档案",
        )

    temporary_remark = f"[仓库临时建档] {payload.reason}"
    product = Product(
        customer_id=customer.id,
        product_code=inventory_code,
        customer_material_code=inventory_code,
        product_name=product_name,
        unit="只",
        box_category="normal",
        remark=temporary_remark,
        is_active=True,
        manual_modified=True,
        manual_modified_at=beijing_now_naive(),
    )
    try:
        db.add(product)
        db.flush()
        record_versioned_create(
            db,
            object_type="product",
            entity=product,
            user=user,
            reason="仓库临时产品建档",
            source="api.warehouse.twin.temporary_finished_inbound",
        )
        audit_master_change(
            db,
            user=user,
            action="CREATE_TEMPORARY",
            resource="Product",
            resource_id=product.id,
            details={
                "customer_id": customer.id,
                "product_code": product.product_code,
                "product_name": product.product_name,
                "reason": payload.reason,
                "source": "warehouse_twin_empty_location",
            },
        )
        current_pallet = db.scalar(
            _floor3_pallet_query().where(
                InventoryPallet.location_id == target.id,
                InventoryPallet.is_current.is_(True),
            )
        )
        lot = manual_finished_in(
            db,
            customer_id=customer.id,
            product_id=product.id,
            location_id=target.id,
            quantity=payload.quantity,
            stock_date=payload.stock_date,
            source_type="manual",
            remarks=temporary_remark,
            operator_id=user.id,
            idempotency_key=payload.idempotency_key,
            pallet_id=current_pallet.id if current_pallet is not None else None,
            pallet_code=payload.pallet_code if current_pallet is None else None,
            require_empty_pallet=current_pallet is None,
            expected_layout_version=payload.expected_layout_version,
            movement_reason="仓库临时产品盘点入位",
        )
        if lot.pallet_item is None:
            raise WarehouseInventoryError("临时产品库存未能绑定当前货位", 409)
        pallet = _floor3_get_pallet(db, lot.pallet_item.pallet_id)
        _floor3_log(
            db,
            request=request,
            user=user,
            action="CREATE",
            pallet=pallet,
            description="数字孪生货位确认临时产品建档并入位",
            details={
                "location_id": target.id,
                "customer_id": customer.id,
                "product_id": product.id,
                "inventory_code": inventory_code,
                "quantity": payload.quantity,
                "reason": payload.reason,
                "idempotency_key": payload.idempotency_key,
            },
        )
        db.commit()
        return {
            "message": "临时产品已明确建档并放入当前货位",
            "idempotent_replay": False,
            "temporary_product_id": product.id,
            "pallet": _floor3_pallet_response(db, pallet, user),
        }
    except Floor3LocationError as error:
        db.rollback()
        raise HTTPException(status_code=error.status_code, detail=str(error)) from error
    except WarehouseInventoryError as error:
        db.rollback()
        _handle(error)
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="临时产品编码、目标货位或请求标识已发生冲突，请刷新后重试",
        ) from error


@router.post("/twin-operations/move-batches")
def confirm_twin_movement_batch(
    payload: TwinMovementBatchPayload,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    """Atomically confirm the page movement draft without a second stock ledger."""

    items = [
        WarehouseMovementBatchItem(
            client_item_id=str(item.client_item_id),
            operation=item.operation,
            target_location_id=item.target_location_id,
            expected_version=item.expected_version,
            expected_target_layout_version=item.expected_target_layout_version,
            pallet_id=item.pallet_id,
            lot_id=item.lot_id,
            quantity=item.quantity,
            remarks=item.remarks,
        )
        for item in payload.items
    ]
    request_hash = movement_batch_request_hash(
        batch_id=payload.idempotency_key,
        items=items,
    )
    with WAREHOUSE_MOVEMENT_BATCH_LOCK:
        try:
            replay = movement_batch_replay(
                db,
                batch_id=payload.idempotency_key,
                request_hash=request_hash,
                actor_user_id=user.id,
            )
            if replay is not None:
                return {
                    **replay,
                    "idempotent_replay": True,
                    "request_hash": request_hash,
                }

            # Scope checks intentionally run before any quantity or location write.
            for item in payload.items:
                if item.operation == "pallet_move":
                    pallet = load_movable_pallet(db, int(item.pallet_id))
                    _require_floor3_pallet_customer_access(db, pallet, user)
                else:
                    _require_lot_customer_access(db, int(item.lot_id), user)

            result = execute_warehouse_movement_batch(
                db,
                batch_id=payload.idempotency_key,
                items=items,
                operator_id=user.id,
            )
            audit_result = {**result, "request_hash": request_hash}
            append_audit_event(
                db,
                request=request,
                actor=user,
                event_category="business",
                result="success",
                source="web",
                module_code="warehouse",
                action_code=BATCH_AUDIT_ACTION_CODE,
                legacy_action="MOVE_BATCH",
                resource="warehouse/twin-operations/move-batches",
                entity_type="warehouse_movement_batch",
                object_ref=payload.idempotency_key,
                batch_id=payload.idempotency_key,
                description="仓库移货页面草稿已一次确认并原子提交",
                details={
                    "request_hash": request_hash,
                    "result": audit_result,
                },
            )
            db.commit()
            return {
                **audit_result,
                "idempotent_replay": False,
            }
        except WarehouseMovementBatchError as error:
            db.rollback()
            raise HTTPException(
                status_code=error.status_code,
                detail=str(error),
            ) from error
        except IntegrityError as error:
            db.rollback()
            raise HTTPException(
                status_code=409,
                detail="移货目标、版本或幂等记录已被其他请求更新，请刷新后重试",
            ) from error
        except HTTPException:
            db.rollback()
            raise
        except Exception:
            db.rollback()
            raise


@router.get("/twin-operations/initial-stock-context")
def get_initial_stock_context(
    location_id: int, product_id: int,
    db: Session = Depends(get_db), user: User = Depends(can_submit_stocktake),
) -> dict:
    from app.services.initial_stocktake import initial_stock_context
    from app.services.stocktake import StocktakeError
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="首次盘点入库仅管理员可操作")
    product = db.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="产品不存在")
    require_customer_access(product.customer_id, user, db)
    try:
        return initial_stock_context(db, location_id=location_id, product_id=product_id)
    except StocktakeError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post("/twin-operations/stocktake-batches")
def confirm_twin_stocktake_batch(
    payload: TwinStocktakeBatchPayload,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(can_submit_stocktake),
) -> dict:
    """Atomically confirm one map stocktake draft into the formal ledger."""

    if any(item.operation == "add" for item in payload.items) and user.role != "admin":
        raise HTTPException(
            status_code=403,
            detail="盘点补录会增加正式库存，只能由管理员确认；普通盘点人员仍可调减或定位现有库存。",
        )

    items = [
        WarehouseStocktakeBatchItem(
            client_item_id=item.client_item_id,
            operation=item.operation,
            location_id=item.location_id,
            expected_layout_version=item.expected_layout_version,
            quantity=item.quantity,
            inventory_type=item.inventory_type,
            unit=item.unit,
            customer_id=item.customer_id,
            product_id=item.product_id,
            stock_date=item.stock_date,
            lot_id=item.lot_id,
            expected_version=item.expected_version,
            source_kind=item.source_kind,
            stock_stage=item.stock_stage,
        )
        for item in payload.items
    ]
    request_hash = stocktake_batch_request_hash(
        batch_id=payload.idempotency_key,
        items=items,
    )
    if payload.initial_inventory_snapshot is not None:
        if len(items) != 1 or items[0].operation != "add" or items[0].inventory_type != "finished":
            raise HTTPException(status_code=422, detail="首次盘点入库每次只保存一个货位的一款成品")
        request_hash = hashlib.sha256(json.dumps({
            "batch_hash": request_hash,
            "initial_inventory_snapshot": payload.initial_inventory_snapshot,
            "existing_inventory_acknowledged": payload.existing_inventory_acknowledged,
        }, sort_keys=True).encode()).hexdigest()
    with WAREHOUSE_STOCKTAKE_BATCH_LOCK:
        try:
            replay = stocktake_batch_replay(
                db,
                batch_id=payload.idempotency_key,
                request_hash=request_hash,
                actor_user_id=user.id,
            )

            # Customer scope is checked on every current item even for replay.
            for item in items:
                customer_id = stocktake_item_customer_id(db, item)
                require_customer_access(customer_id, user, db, request)

            if replay is not None:
                return {
                    **replay,
                    "idempotent_replay": True,
                    "request_hash": request_hash,
                }

            if payload.initial_inventory_snapshot is not None:
                from app.services.initial_stocktake import initial_stock_context
                from app.services.stocktake import StocktakeError
                try:
                    context = initial_stock_context(db, location_id=items[0].location_id,
                                                    product_id=items[0].product_id)
                except StocktakeError as error:
                    raise HTTPException(status_code=409, detail=str(error)) from error
                if context["snapshot"] != payload.initial_inventory_snapshot:
                    raise HTTPException(status_code=409, detail="库存或货位已变化，请重新核对后保存")
                if not context["can_add"]:
                    raise HTTPException(status_code=409, detail=context["block_reason"])
                if context["existing_quantity"] and not payload.existing_inventory_acknowledged:
                    raise HTTPException(status_code=409, detail="此产品已有系统库存，请先核对是否只是需要移货归位")
            result = execute_warehouse_stocktake_batch(
                db,
                batch_id=payload.idempotency_key,
                items=items,
                operator_id=user.id,
            )
            audit_result = {**result, "request_hash": request_hash}
            append_audit_event(
                db,
                request=request,
                actor=user,
                event_category="business",
                result="success",
                source="web",
                module_code="warehouse",
                action_code=STOCKTAKE_BATCH_ACTION_CODE,
                legacy_action="STOCKTAKE_BATCH",
                resource="warehouse/twin-operations/stocktake-batches",
                entity_type="warehouse_stocktake_batch",
                object_ref=payload.idempotency_key,
                batch_id=payload.idempotency_key,
                description="仓库盘点草稿已一次确认并原子写入正式库存流水",
                details=stocktake_batch_audit_details(
                    request_hash=request_hash,
                    result=audit_result,
                ),
            )
            db.commit()
            return {
                **audit_result,
                "idempotent_replay": False,
            }
        except WarehouseStocktakeBatchError as error:
            db.rollback()
            raise HTTPException(
                status_code=error.status_code,
                detail=str(error),
            ) from error
        except Floor3LocationError as error:
            db.rollback()
            raise HTTPException(
                status_code=error.status_code,
                detail=str(error),
            ) from error
        except WarehouseInventoryError as error:
            db.rollback()
            _handle(error)
        except IntegrityError as error:
            db.rollback()
            raise HTTPException(
                status_code=409,
                detail="盘点库存、版本、货位或幂等记录已发生冲突，请刷新后重试",
            ) from error
        except (OperationalError, sqlite3.OperationalError) as error:
            db.rollback()
            rendered = str(error).lower()
            if any(
                marker in rendered
                for marker in (
                    "database is locked",
                    "database table is locked",
                    "database schema is locked",
                    "sqlite_busy",
                    "sqlite_locked",
                )
            ):
                raise HTTPException(
                    status_code=409,
                    detail="盘点库存正在被其他操作更新，请稍后刷新后重试",
                ) from error
            raise
        except HTTPException:
            db.rollback()
            raise
        except Exception:
            db.rollback()
            raise


@router.post("/twin-operations/pallets/{pallet_id}/move")
def move_twin_formal_pallet(
    pallet_id: int,
    payload: TwinPalletMovePayload,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    """Move a formal pallet and all linked lots from the admin-only map UI."""

    current = _floor3_get_pallet(db, pallet_id)
    _require_floor3_pallet_customer_access(db, current, user)
    from_location_id = current.location_id
    try:
        result = move_pallet(
            db,
            pallet_id=pallet_id,
            expected_version=payload.expected_version,
            to_location_id=payload.to_location_id,
            remarks=payload.remarks,
            operator_id=user.id,
            idempotency_key=payload.idempotency_key,
            require_published_target=has_space_ledger(db),
            expected_target_layout_version=payload.expected_target_layout_version,
        )
        if not result.replayed:
            _floor3_log(
                db,
                request=request,
                user=user,
                action="UPDATE",
                pallet=result.pallet,
                description="数字孪生地图确认正式栈板移位",
                details={
                    "from_location_id": from_location_id,
                    "to_location_id": payload.to_location_id,
                    "expected_version": payload.expected_version,
                    "idempotency_key": payload.idempotency_key,
                },
            )
        db.commit()
        return {
            "message": "正式栈板已从地图确认移位",
            "idempotent_replay": result.replayed,
            "pallet": _floor3_pallet_response(db, result.pallet, user),
        }
    except Floor3LocationError as error:
        db.rollback()
        raise HTTPException(status_code=error.status_code, detail=str(error)) from error
    except WarehouseInventoryError as error:
        db.rollback()
        _handle(error)
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="目标货位已被占用或栈板版本已变化，请刷新后重试",
        ) from error


@router.post("/twin-operations/lots/{lot_id}/quantity-correction")
def correct_twin_inventory_lot_quantity(
    lot_id: int,
    payload: TwinLotQuantityCorrectionPayload,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    lot = db.scalar(_lot_query().where(InventoryLot.id == lot_id))
    if lot is None:
        raise HTTPException(status_code=404, detail="库存批次不存在")
    _require_lot_customer_access(db, lot.id, user)
    location = lot.location
    if (
        location is None
        or location.warehouse_floor != 3
        or location.source_version not in {"V11", "CURRENT_MAP"}
    ):
        raise HTTPException(status_code=409, detail="地图库存纠偏只允许三楼已接入库位")
    if payload.action == "remove":
        if lot.quantity_reserved > 0:
            raise HTTPException(status_code=409, detail="该货物仍有预占，必须先释放预占后才能移除")
        if lot.quantity_damaged > 0:
            raise HTTPException(status_code=409, detail="该批次仍有报损数量，不能从地图直接移除")
        if lot.quantity_available <= 0:
            raise HTTPException(status_code=409, detail="该货物当前没有可移除数量")
        quantity_delta = -int(lot.quantity_available)
    else:
        quantity = int(payload.quantity or 0)
        if quantity > lot.quantity_available:
            raise HTTPException(status_code=409, detail="减少数量不能大于当前可用库存")
        quantity_delta = -quantity

    before = _inventory_lot_audit_state(lot)
    replayed = _inventory_operation_replayed(db, payload.idempotency_key)
    try:
        row = mutate_lot(
            db,
            lot_id=lot.id,
            operation="adjust",
            expected_version=payload.expected_version,
            operator_id=user.id,
            quantity=quantity_delta,
            reason=payload.reason,
            idempotency_key=payload.idempotency_key,
        )
        if payload.action == "remove" and not replayed:
            row.status = "closed"
            db.flush()
        if not replayed:
            customer_id, customer_name = _inventory_lot_audit_customer(row)
            _append_inventory_lot_audit(
                db,
                request=request,
                user=user,
                action_code=f"warehouse.twin_lot.{payload.action}",
                row=row,
                before=before,
                reason=payload.reason,
                idempotency_key=payload.idempotency_key,
                customer_id=customer_id,
                customer_name=customer_name,
            )
        db.commit()
        return {
            "message": "货物已受控移除并保留历史流水" if payload.action == "remove" else "库存数量已按管理员确认减少",
            "idempotent_replay": replayed,
            "lot": _lot_dict_for_db(db, row),
        }
    except WarehouseInventoryError as error:
        db.rollback()
        _handle(error)
    except IntegrityError as error:
        db.rollback()
        _handle_integrity(error)


@router.post("/pallets", status_code=201)
def create_floor3_pallet(
    payload: Floor3PalletCreatePayload,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    for item in payload.items:
        _require_floor3_item_customer_access(db, item, user)
    location = db.get(WarehouseLocation, payload.location_id)
    if location is None:
        raise HTTPException(status_code=404, detail="目标货位不存在")
    item_types = {item.item_type for item in payload.items}
    use_measured_area_location = not (
        location.warehouse_floor == 3 and location.source_version == "V11"
    )
    if use_measured_area_location and len(item_types) != 1:
        raise HTTPException(
            status_code=409,
            detail="实测区域内一块新栈板只能登记一种货物类型，请分开入位",
        )
    required_inventory_type = next(iter(item_types)) if item_types else None
    try:
        row = create_pallet(
            db,
            location_id=payload.location_id,
            pallet_code=payload.pallet_code,
            items=[item.model_dump() for item in payload.items],
            remarks=payload.remarks,
            operator_id=user.id,
            allow_operational_location=use_measured_area_location,
            require_published_location=use_measured_area_location,
            required_inventory_type=(
                required_inventory_type if use_measured_area_location else None
            ),
            require_no_live_inventory=use_measured_area_location,
            expected_layout_version=payload.expected_layout_version,
        )
        _floor3_log(
            db,
            request=request,
            user=user,
            action="CREATE",
            pallet=row,
            description="创建三楼物理栈板并绑定现场内容",
            details={
                "location_id": payload.location_id,
                "item_count": len(payload.items),
            },
        )
        db.commit()
        return {
            "message": "已绑定到三楼货位",
            "pallet": _floor3_pallet_response(db, row, user),
        }
    except Floor3LocationError as error:
        db.rollback()
        raise HTTPException(status_code=error.status_code, detail=str(error)) from error
    except WarehouseInventoryError as error:
        db.rollback()
        _handle(error)
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="栈板编号已存在，或该货位已被其他栈板占用，请刷新后重试",
        ) from error


@router.post("/pallets/{pallet_id}/items")
def add_floor3_pallet_item(
    pallet_id: int,
    payload: Floor3PalletAddItemPayload,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    current = _floor3_get_pallet(db, pallet_id)
    _require_floor3_pallet_customer_access(db, current, user)
    _require_floor3_item_customer_access(db, payload.item, user)
    try:
        if payload.item.create_finished_inventory:
            existing = db.scalar(
                select(InventoryMovement).where(
                    InventoryMovement.idempotency_key == payload.item.idempotency_key,
                    InventoryMovement.movement_type == "manual_in",
                )
            )
            if existing is not None:
                existing_lot = db.get(InventoryLot, existing.inventory_lot_id)
                if (
                    existing_lot is None
                    or existing_lot.pallet_item is None
                    or existing_lot.pallet_item.pallet_id != pallet_id
                ):
                    raise Floor3LocationError(
                        "幂等键已用于其它物理栈板，不能重复追加", status_code=409
                    )
            elif current.version != payload.expected_version:
                raise Floor3LocationError(
                    "栈板已被其他操作更新，请刷新后重试", status_code=409
                )
            row_lot = manual_finished_in(
                db,
                customer_id=payload.item.customer_id,
                product_id=payload.item.product_id,
                location_id=current.location_id,
                quantity=int(payload.item.quantity),
                stock_date=payload.item.stock_date,
                source_type="manual",
                remarks=payload.item.remarks,
                operator_id=user.id,
                idempotency_key=payload.item.idempotency_key,
                pallet_id=pallet_id,
            )
            row = _floor3_get_pallet(db, pallet_id)
        else:
            row = add_pallet_item(
                db,
                pallet_id=pallet_id,
                expected_version=payload.expected_version,
                item=payload.item.model_dump(),
                operator_id=user.id,
            )
        _floor3_log(
            db,
            request=request,
            user=user,
            action="UPDATE",
            pallet=row,
            description="增加同栈板产品",
            details={
                "product_id": payload.item.product_id,
                "quantity": payload.item.quantity,
                "expected_version": payload.expected_version,
                "create_finished_inventory": payload.item.create_finished_inventory,
            },
        )
        db.commit()
        return {
            "message": "已增加同栈板产品",
            "pallet": _floor3_pallet_response(db, row, user),
        }
    except Floor3LocationError as error:
        db.rollback()
        raise HTTPException(status_code=error.status_code, detail=str(error)) from error
    except WarehouseInventoryError as error:
        db.rollback()
        _handle(error)
    except IntegrityError as error:
        db.rollback()
        _handle_integrity(error)


@router.post("/pallets/{pallet_id}/items/{item_id}/promote-finished")
def promote_floor3_snapshot_to_finished(
    pallet_id: int,
    item_id: int,
    payload: Floor3PalletPromoteFinishedPayload,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    current = _floor3_get_pallet(db, pallet_id)
    _require_floor3_pallet_customer_access(db, current, user)
    source_item = next((item for item in current.items if item.id == item_id), None)
    source_details = (
        {
            "customer_id": source_item.customer_id,
            "product_id": source_item.product_id,
            "inventory_code": source_item.inventory_code,
            "product_name": source_item.product_name,
            "quantity": str(source_item.quantity),
            "item_type": source_item.item_type,
            "unit": source_item.unit,
            "location_id": current.location_id,
            "pallet_code": current.pallet_code,
        }
        if source_item is not None
        else {}
    )
    try:
        row, lot, replayed = convert_snapshot_to_finished_lot(
            db,
            pallet_id=pallet_id,
            item_id=item_id,
            expected_version=payload.expected_version,
            idempotency_key=payload.idempotency_key,
            stock_date=payload.stock_date,
            operator_id=user.id,
        )
        if not replayed:
            _floor3_log(
                db,
                request=request,
                user=user,
                action="UPDATE",
                pallet=row,
                description="现场快照转正式成品库存",
                details={
                    "item_id": item_id,
                    "inventory_lot_id": lot.id,
                    "stock_date": payload.stock_date,
                    "idempotency_key": payload.idempotency_key,
                    "source_snapshot": source_details,
                },
            )
        db.commit()
        return {
            "message": "已转为正式成品库存",
            "replayed": replayed,
            "lot": _lot_dict_for_db(db, lot),
            "pallet": _floor3_pallet_response(db, row, user),
        }
    except Floor3LocationError as error:
        db.rollback()
        raise HTTPException(status_code=error.status_code, detail=str(error)) from error
    except WarehouseInventoryError as error:
        db.rollback()
        _handle(error)
    except IntegrityError as error:
        db.rollback()
        _handle_integrity(error)


@router.post("/pallets/{pallet_id}/move")
def move_floor3_pallet(
    pallet_id: int,
    payload: Floor3PalletMovePayload,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    current = _floor3_get_pallet(db, pallet_id)
    _require_floor3_pallet_customer_access(db, current, user)
    from_location_id = current.location_id
    try:
        result = move_pallet(
            db,
            pallet_id=pallet_id,
            expected_version=payload.expected_version,
            to_location_id=payload.to_location_id,
            remarks=payload.remarks,
            operator_id=user.id,
            idempotency_key=payload.idempotency_key,
            require_published_target=has_space_ledger(db),
            expected_target_layout_version=payload.expected_target_layout_version,
        )
        if not result.replayed:
            _floor3_log(
                db,
                request=request,
                user=user,
                action="UPDATE",
                pallet=result.pallet,
                description="三楼栈板移位",
                details={
                    "from_location_id": from_location_id,
                    "to_location_id": payload.to_location_id,
                    "expected_version": payload.expected_version,
                    "idempotency_key": payload.idempotency_key,
                },
            )
        db.commit()
        return {
            "message": "栈板已移位",
            "pallet": _floor3_pallet_response(db, result.pallet, user),
            "idempotent_replay": result.replayed,
        }
    except Floor3LocationError as error:
        db.rollback()
        raise HTTPException(status_code=error.status_code, detail=str(error)) from error
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(status_code=409, detail="目标货位已被占用，请刷新后重试") from error


@router.post("/pallets/merge-batches")
def merge_warehouse_pallet_batch(
    payload: PalletMergeBatchPayload,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    """Merge the selected system pallets in one atomic stock transaction."""

    sources = [
        PalletMergeBatchSource(
            client_item_id=source.client_item_id,
            pallet_id=source.pallet_id,
            expected_version=source.expected_version,
        )
        for source in payload.sources
    ]
    request_hash = pallet_merge_batch_request_hash(
        batch_id=payload.idempotency_key,
        target_pallet_id=payload.target_pallet_id,
        expected_target_version=payload.expected_target_version,
        sources=sources,
    )
    with PALLET_MERGE_BATCH_LOCK:
        try:
            replay = pallet_merge_batch_replay(
                db,
                batch_id=payload.idempotency_key,
                request_hash=request_hash,
                actor_user_id=user.id,
            )

            # Scope checks intentionally precede strict inventory validation so
            # a scoped account cannot probe another customer's pallet state.
            # They also run before returning an exact replay: actor ownership
            # does not replace the user's current customer authorization.
            selected_ids = [
                payload.target_pallet_id,
                *(source.pallet_id for source in payload.sources),
            ]
            selected_pallets = list(
                db.scalars(
                    _floor3_pallet_query().where(
                        InventoryPallet.id.in_(selected_ids)
                    )
                ).all()
            )
            by_id = {int(pallet.id): pallet for pallet in selected_pallets}
            for pallet_id in selected_ids:
                pallet = by_id.get(int(pallet_id))
                if pallet is None:
                    raise HTTPException(status_code=404, detail="栈板不存在")
                _require_floor3_pallet_customer_access(db, pallet, user)

            if replay is not None:
                require_customer_access(int(replay["customer_id"]), user, db)
                return {
                    **replay,
                    "idempotent_replay": True,
                    "request_hash": request_hash,
                }

            result = execute_pallet_merge_batch(
                db,
                batch_id=payload.idempotency_key,
                target_pallet_id=payload.target_pallet_id,
                expected_target_version=payload.expected_target_version,
                sources=sources,
                operator_id=user.id,
            )
            audit_result = {**result, "request_hash": request_hash}
            append_audit_event(
                db,
                request=request,
                actor=user,
                event_category="business",
                result="success",
                source="web",
                module_code="warehouse",
                action_code=PALLET_MERGE_BATCH_ACTION_CODE,
                legacy_action="MERGE_BATCH",
                resource="warehouse/pallets/merge-batches",
                entity_type="inventory_pallet_merge_batch",
                entity_id=payload.target_pallet_id,
                object_ref=payload.idempotency_key,
                batch_id=payload.idempotency_key,
                description="多块系统栈板已一次确认并原子合并",
                details={
                    "request_hash": request_hash,
                    "result": audit_result,
                },
            )
            db.commit()
            return {
                **audit_result,
                "idempotent_replay": False,
            }
        except PalletMergeBatchError as error:
            db.rollback()
            raise HTTPException(
                status_code=error.status_code,
                detail=str(error),
            ) from error
        except IntegrityError as error:
            db.rollback()
            raise HTTPException(
                status_code=409,
                detail="栈板、批次、版本或幂等记录已被其他请求更新，请刷新后重试",
            ) from error
        except HTTPException:
            db.rollback()
            raise
        except Exception:
            db.rollback()
            raise


@router.post("/pallets/{pallet_id}/merge-all")
def merge_floor3_pallet_remaining_goods(
    pallet_id: int,
    payload: Floor3PalletMergePayload,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    source = _floor3_get_pallet(db, pallet_id)
    target = _floor3_get_pallet(db, payload.target_pallet_id)
    _require_floor3_pallet_customer_access(db, source, user)
    _require_floor3_pallet_customer_access(db, target, user)
    source_location_id = source.location_id
    target_location_id = target.location_id
    try:
        result = merge_pallet_remaining_goods(
            db,
            source_pallet_id=pallet_id,
            target_pallet_id=payload.target_pallet_id,
            expected_source_version=payload.expected_version,
            expected_target_version=payload.expected_target_version,
            operator_id=user.id,
            idempotency_key=payload.idempotency_key,
        )
        if not result.replayed:
            common_details = {
                "source_pallet_id": pallet_id,
                "target_pallet_id": payload.target_pallet_id,
                "source_location_id": source_location_id,
                "target_location_id": target_location_id,
                "moved_item_count": result.moved_item_count,
                "expected_source_version": payload.expected_version,
                "expected_target_version": payload.expected_target_version,
                "idempotency_key": payload.idempotency_key,
            }
            _floor3_log(
                db,
                request=request,
                user=user,
                action="UPDATE",
                pallet=result.source_pallet,
                description="源栈板全部剩余货物已合并并释放",
                details=common_details,
            )
            _floor3_log(
                db,
                request=request,
                user=user,
                action="UPDATE",
                pallet=result.target_pallet,
                description="目标栈板接收全部零散货",
                details=common_details,
            )
        db.commit()
        return {
            "message": "零散货已全部合并，源栈板已释放",
            "source_pallet": _floor3_pallet_response(
                db, result.source_pallet, user
            ),
            "target_pallet": _floor3_pallet_response(
                db, result.target_pallet, user
            ),
            "moved_item_count": result.moved_item_count,
            "idempotent_replay": result.replayed,
        }
    except Floor3LocationError as error:
        db.rollback()
        raise HTTPException(status_code=error.status_code, detail=str(error)) from error
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="栈板或库存状态已变化，请刷新后重试",
        ) from error


@router.post("/pallets/{pallet_id}/clear")
def clear_floor3_pallet(
    pallet_id: int,
    payload: Floor3PalletClearPayload,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    remarks = (payload.remarks or "").strip() or "清空三楼货位栈板（系统记录）"
    current = _floor3_get_pallet(db, pallet_id)
    _require_floor3_pallet_customer_access(db, current, user)
    from_location_id = current.location_id
    try:
        row = clear_pallet(
            db,
            pallet_id=pallet_id,
            expected_version=payload.expected_version,
            remarks=remarks,
            operator_id=user.id,
        )
        _floor3_log(
            db,
            request=request,
            user=user,
            action="UPDATE",
            pallet=row,
            description="清空三楼货位的当前栈板",
            details={
                "from_location_id": from_location_id,
                "reason": remarks,
                "expected_version": payload.expected_version,
            },
        )
        db.commit()
        return {
            "message": "货位已清空，历史记录已保留",
            "pallet": _floor3_pallet_response(db, row, user),
        }
    except Floor3LocationError as error:
        db.rollback()
        raise HTTPException(status_code=error.status_code, detail=str(error)) from error
    except IntegrityError as error:
        db.rollback()
        _handle_integrity(error)


@router.post("/pallets/{pallet_id}/relocation-flag")
def set_floor3_pallet_relocation_flag(
    pallet_id: int,
    payload: Floor3PalletRelocationPayload,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    remarks = (payload.remarks or "").strip() or (
        "标记三楼栈板待归位（系统记录）"
        if payload.needs_relocation
        else "现场确认三楼栈板已归位（系统记录）"
        if payload.placement_confirmed
        else "取消三楼栈板待归位标记（系统记录）"
    )
    current = _floor3_get_pallet(db, pallet_id)
    _require_floor3_pallet_customer_access(db, current, user)
    try:
        row = set_pallet_relocation(
            db,
            pallet_id=pallet_id,
            expected_version=payload.expected_version,
            needs_relocation=payload.needs_relocation,
            placement_confirmed=payload.placement_confirmed,
            operator_id=user.id,
        )
        _floor3_log(
            db,
            request=request,
            user=user,
            action="UPDATE",
            pallet=row,
            description=(
                "标记三楼栈板待归位"
                if payload.needs_relocation
                else "现场确认三楼栈板已归位"
            ),
            details={
                "needs_relocation": payload.needs_relocation,
                "placement_confirmed": payload.placement_confirmed,
                "reason": remarks,
                "expected_version": payload.expected_version,
            },
        )
        db.commit()
        return {
            "message": (
                "已标记待归位"
                if payload.needs_relocation
                else "已确认当前固定货位归位"
            ),
            "pallet": _floor3_pallet_response(db, row, user),
        }
    except Floor3LocationError as error:
        db.rollback()
        raise HTTPException(status_code=error.status_code, detail=str(error)) from error
    except IntegrityError as error:
        db.rollback()
        _handle_integrity(error)


def _warehouse_area_stats(db: Session, area: WarehouseArea) -> dict:
    location_match = and_(
        WarehouseLocation.warehouse_floor == area.floor.floor_number,
        func.upper(WarehouseLocation.area_code) == area.area_code,
    )
    recorded_location_count = int(
        db.scalar(select(func.count(WarehouseLocation.id)).where(location_match)) or 0
    )
    laid_out_location_count = int(
        db.scalar(
            select(func.count(WarehouseLocation.id)).where(
                location_match,
                WarehouseLocation.is_active.is_(True),
                or_(
                    WarehouseLocation.placement_status == "placed",
                    WarehouseLocation.placement_status.is_(None),
                ),
            )
        )
        or 0
    )
    pending_layout_count = int(
        db.scalar(
            select(func.count(WarehouseLocation.id)).where(
                location_match,
                WarehouseLocation.is_active.is_(True),
                WarehouseLocation.placement_status == "unplaced",
            )
        )
        or 0
    )
    retired_location_count = int(
        db.scalar(
            select(func.count(WarehouseLocation.id)).where(
                location_match,
                WarehouseLocation.is_active.is_(False),
            )
        )
        or 0
    )
    occupied_pallet_count = int(
        db.scalar(
            select(func.count(InventoryPallet.id))
            .join(
                WarehouseLocation,
                WarehouseLocation.id == InventoryPallet.location_id,
            )
            .where(location_match, InventoryPallet.is_current.is_(True))
        )
        or 0
    )
    return {
        "recorded_location_count": recorded_location_count,
        "laid_out_location_count": laid_out_location_count,
        "pending_layout_count": pending_layout_count,
        "retired_location_count": retired_location_count,
        "occupied_pallet_count": occupied_pallet_count,
    }


def _warehouse_area_dict(db: Session, row: WarehouseArea) -> dict:
    policy = row.storage_policy
    readable_area_name = employee_area_name(
        row,
        floor_number=row.floor.floor_number,
    )
    return {
        "id": row.id,
        "floor_id": row.floor_id,
        "floor_code": row.floor.floor_code,
        "floor_name": row.floor.floor_name,
        "floor_number": row.floor.floor_number,
        "area_code": row.area_code,
        "area_name": row.area_name,
        "area_master_name": row.area_name,
        "employee_area_name": readable_area_name,
        "address_zone_code": row.address_zone_code,
        "address_subzone_no": row.address_subzone_no,
        "address_version": int(row.address_version or 1),
        "current_address_code": (
            f"{row.floor.floor_number}F-{row.address_zone_code}{int(row.address_subzone_no):02d}"
            if row.address_zone_code and row.address_subzone_no
            else None
        ),
        "current_address_name": (
            f"{row.floor.floor_name} {row.address_zone_code}{int(row.address_subzone_no)}区"
            if row.address_zone_code and row.address_subzone_no
            else readable_area_name
        ),
        "planned_location_count": row.planned_location_count,
        "planned_pallet_capacity": row.planned_pallet_capacity,
        "capacity_review_status": row.capacity_review_status,
        "capacity_eligible": row.capacity_eligible,
        "confirmed_pallet_capacity": row.confirmed_pallet_capacity,
        "capacity_reviewed_by": row.capacity_reviewed_by,
        "capacity_reviewed_at": (
            beijing_naive_to_api(row.capacity_reviewed_at)
            if row.capacity_reviewed_at
            else None
        ),
        "construction_status": row.construction_status,
        "remarks": row.remarks,
        "storage_policy": (
            {
                "map_feature_id": policy.map_feature_id,
                "allowed_inventory_types": policy_inventory_types(policy),
                "storage_layout": policy.storage_layout,
                "status": policy.status,
                "draft_map_revision": policy.draft_map_revision,
                "published_map_revision": policy.published_map_revision,
                "version": policy.version,
            }
            if policy is not None
            else None
        ),
        **_warehouse_area_stats(db, row),
    }


def _warehouse_floor_dict(
    db: Session,
    row: WarehouseFloor,
    *,
    include_archived: bool = False,
) -> dict:
    areas = sorted(
        (
            item
            for item in row.areas
            if include_archived
            or (
                item.construction_status != "archived"
                and not (
                    item.storage_policy is not None
                    and item.storage_policy.status == "archived"
                )
            )
        ),
        key=lambda item: (item.area_code, item.id),
    )
    area_items = [_warehouse_area_dict(db, area) for area in areas]
    occupied_pallet_count = sum(
        area["occupied_pallet_count"] for area in area_items
    )
    capacity = warehouse_capacity_summary(
        row,
        occupied_pallets=occupied_pallet_count,
        visible=True,
    )
    return {
        "id": row.id,
        "floor_code": row.floor_code,
        "floor_name": row.floor_name,
        "floor_number": row.floor_number,
        "construction_status": row.construction_status,
        "planning_reference_pallet_capacity": row.planning_reference_pallet_capacity,
        "remarks": row.remarks,
        "area_count": len(area_items),
        "planned_location_count": sum(
            area["planned_location_count"] for area in area_items
        ),
        "recorded_location_count": sum(
            area["recorded_location_count"] for area in area_items
        ),
        "planned_pallet_capacity": sum(
            area["planned_pallet_capacity"] for area in area_items
        ),
        "occupied_pallet_count": occupied_pallet_count,
        "laid_out_location_count": sum(
            area["laid_out_location_count"] for area in area_items
        ),
        "pending_layout_count": sum(
            area["pending_layout_count"] for area in area_items
        ),
        "capacity": capacity,
        "areas": area_items,
    }


def _require_registered_area(
    db: Session,
    *,
    floor_number: int | None,
    area_code: str | None,
) -> WarehouseArea | None:
    if floor_number is None and area_code is None:
        return None
    if floor_number is None or area_code is None:
        raise HTTPException(status_code=409, detail="请先同时选择楼层和区域")
    area = db.scalar(
        select(WarehouseArea)
        .options(selectinload(WarehouseArea.storage_policy))
        .join(WarehouseFloor, WarehouseFloor.id == WarehouseArea.floor_id)
        .where(
            WarehouseFloor.floor_number == floor_number,
            WarehouseArea.area_code == area_code,
        )
    )
    if area is None:
        raise HTTPException(
            status_code=409,
            detail="该楼层区域尚未建立台账，请先新增楼层和区域。",
        )
    if area.construction_status == "archived" or (
        area.storage_policy is not None and area.storage_policy.status == "archived"
    ):
        raise HTTPException(
            status_code=409,
            detail="该区域已经归档，普通库位维护不能恢复或修改。",
        )
    return area


def _require_location_source_not_archived(
    db: Session,
    location: WarehouseLocation,
) -> WarehouseArea | None:
    if location.address_area_id is not None:
        area = db.scalar(
            select(WarehouseArea)
            .where(WarehouseArea.id == location.address_area_id)
            .options(selectinload(WarehouseArea.storage_policy))
        )
        if area is None:
            raise HTTPException(
                status_code=409,
                detail="该库位的正式区域归属已失效，请先完成地址治理。",
            )
        if area.construction_status == "archived" or (
            area.storage_policy is not None
            and area.storage_policy.status == "archived"
        ):
            raise HTTPException(
                status_code=409,
                detail="该库位属于已归档区域，普通库位维护不能恢复或修改。",
        )
        return area
    area = db.scalar(
        select(WarehouseArea)
        .join(WarehouseFloor, WarehouseFloor.id == WarehouseArea.floor_id)
        .where(
            WarehouseFloor.floor_number == location.warehouse_floor,
            func.upper(WarehouseArea.area_code)
            == str(location.area_code or "").strip().upper(),
        )
        .options(selectinload(WarehouseArea.storage_policy))
    )
    if area is None:
        if location.source_version == "V11":
            return None
        raise HTTPException(
            status_code=409,
            detail="该库位的正式区域归属已失效，请先完成地址治理。",
        )
    if area.construction_status == "archived" or (
        area.storage_policy is not None
        and area.storage_policy.status == "archived"
    ):
        raise HTTPException(
            status_code=409,
            detail="该库位属于已归档区域，普通库位维护不能恢复或修改。",
        )
    return area


def _capacity_reviewer_name(user: User) -> str:
    return (user.display_name or user.real_name or user.username).strip()


def _apply_capacity_review(
    row: WarehouseArea,
    *,
    user: User,
    review_changed: bool,
) -> None:
    if row.capacity_review_status == "pending":
        row.capacity_eligible = False
        row.confirmed_pallet_capacity = None
        row.capacity_reviewed_by = None
        row.capacity_reviewed_at = None
    elif review_changed or row.capacity_reviewed_at is None:
        row.capacity_reviewed_by = _capacity_reviewer_name(user)
        row.capacity_reviewed_at = beijing_now_naive()


def _warehouse_capacity_log(
    db: Session,
    *,
    request: Request,
    user: User,
    action: str,
    entity_type: str,
    entity_id: int,
    object_ref: str,
    before: dict | None,
    after: dict,
) -> None:
    append_audit_event(
        db,
        request=request,
        actor=user,
        event_category="system",
        result="success",
        source="web",
        module_code="warehouse",
        action_code=action,
        legacy_action="CAPACITY_UPDATE",
        resource=f"warehouse/capacity/{entity_type}/{entity_id}",
        entity_type=entity_type,
        entity_id=entity_id,
        object_ref=object_ref,
        description="仓储容量台账已更新",
        details={"before": before, "after": after},
    )


@router.get("/factory-maps/floors/{floor_code}")
def get_factory_floor_map(
    floor_code: str,
    _user: User = Depends(can_read),
) -> dict:
    try:
        return load_factory_map(floor_code)
    except FactoryMapNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.get("/twin-layout/floors/{floor_code}")
def get_warehouse_twin_floor_layout(
    floor_code: str,
    db: Session = Depends(get_db),
    _user: User = Depends(_can_locate_twin),
) -> dict:
    try:
        layout = overlay_formal_area_bindings(
            db,
            floor_code=floor_code,
            floor_layout=load_warehouse_twin_floor(floor_code),
        )
        return {**layout, "standard_pallet": standard_pallet_contract()}
    except WarehouseTwinLayoutNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.get("/twin-layout/floors/{floor_code}/draft")
def get_warehouse_twin_floor_layout_draft(
    floor_code: str,
    db: Session = Depends(get_db),
    _user: User = Depends(admin_only),
) -> dict:
    try:
        layout = overlay_formal_area_bindings(
            db,
            floor_code=floor_code,
            floor_layout=load_warehouse_twin_layout_draft(floor_code),
            include_draft=True,
        )
        return {**layout, "standard_pallet": standard_pallet_contract()}
    except WarehouseTwinLayoutEditError as error:
        _handle_twin_layout_edit_error(error)


class TwinRackLayoutFields(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    x_mm: float = Field(ge=-10_000_000, le=10_000_000)
    y_mm: float = Field(ge=-10_000_000, le=10_000_000)
    width_mm: float = Field(gt=0, le=200_000)
    depth_mm: float = Field(gt=0, le=200_000)
    height_mm: float = Field(gt=0, le=100_000)
    levels: int = Field(ge=1, le=20)
    level_heights_mm: list[float] = Field(max_length=19)
    cargo_rows: int = Field(ge=3, le=5)
    level_cell_counts: list[int] | None = Field(default=None, max_length=20)
    bays: int = Field(default=1, ge=1, le=50)
    access_side: Literal["north", "south", "east", "west", "both"] = "south"
    min_aisle_width_mm: float = Field(default=1500, ge=0, le=20_000)
    rotation_deg: Literal[0, 90, 180, 270] = 0
    color: str = Field(default="#38bdf8", pattern=r"^#[0-9A-Fa-f]{6}$")


class TwinRackLayoutCreatePayload(TwinRackLayoutFields):
    expected_revision: str = Field(min_length=1, max_length=64)
    operation_key: str = Field(min_length=8, max_length=120)
    area_feature_id: str = Field(min_length=1, max_length=80)


class TwinRackLayoutUpdatePayload(TwinRackLayoutFields):
    expected_revision: str = Field(min_length=1, max_length=64)
    expected_version: int = Field(ge=1)
    operation_key: str = Field(min_length=8, max_length=120)


class TwinLayoutFeatureCreatePayload(BaseModel):
    expected_revision: str = Field(min_length=1, max_length=64)
    operation_key: str = Field(min_length=8, max_length=120)
    feature_kind: Literal["zone", "aisle"]
    points: list[tuple[float, float]] = Field(min_length=2, max_length=64)
    width_mm: float | None = Field(default=None, gt=0, le=20_000)
    direction: Literal["one_way", "two_way"] | None = None


class TwinGroundLocationDraftPoint(BaseModel):
    location_id: int = Field(ge=1)
    expected_version: int = Field(ge=1)
    x_mm: float = Field(allow_inf_nan=False)
    y_mm: float = Field(allow_inf_nan=False)


class TwinLayoutFeatureGeometryPayload(BaseModel):
    expected_revision: str = Field(min_length=1, max_length=64)
    expected_version: int = Field(ge=1)
    operation_key: str = Field(min_length=8, max_length=120)
    points: list[tuple[float, float]] = Field(min_length=2, max_length=64)
    ground_locations: list[TwinGroundLocationDraftPoint] | None = Field(default=None, max_length=500)


class TwinZoneStoragePolicyPayload(BaseModel):
    expected_revision: str = Field(min_length=1, max_length=64)
    expected_version: int = Field(ge=1)
    operation_key: str = Field(min_length=8, max_length=120)
    allowed_inventory_types: list[
        Literal[
            "finished",
            "semi_finished",
            "raw_material",
            "mold",
            "print_plate",
            "temporary_turnover",
        ]
    ] = Field(min_length=1, max_length=6)
    storage_layout: Literal["rack", "pallet_ground", "mixed", "functional"]
    erp_area_code: str = Field(min_length=1, max_length=30)
    area_name: str | None = Field(default=None, max_length=100)
    existing_area_id: int | None = Field(default=None, ge=1)
    max_rack_count: int | None = Field(default=None, ge=0, le=500)
    pallet_rotation_deg: Literal[0, 90] | None = None

    @field_validator("erp_area_code")
    @classmethod
    def normalize_erp_area_code(cls, value: str) -> str:
        normalized = (value or "").strip().upper()
        if not normalized:
            raise ValueError("正式区域编号不能为空")
        return normalized

    @field_validator("area_name")
    @classmethod
    def normalize_area_name(cls, value: str | None) -> str | None:
        normalized = (value or "").strip()
        return normalized or None


class TwinZoneConfirmAreaPayload(BaseModel):
    expected_revision: str = Field(min_length=1, max_length=64)
    expected_published_revision: str = Field(min_length=1, max_length=64)
    expected_version: int = Field(ge=1)
    # One-step confirmation derives ``-policy`` and ``-publish`` child keys;
    # reserve room for the longest suffix so a request accepted here can never
    # fail later with an internal Pydantic error.
    operation_key: str = Field(min_length=8, max_length=112)
    primary_inventory_type: Literal[
        "finished",
        "semi_finished",
        "raw_material",
        "mold",
        "print_plate",
        "temporary_turnover",
    ]
    storage_layout: Literal["rack", "pallet_ground", "functional"]
    max_pallet_capacity: int = Field(ge=0, le=500)
    pallet_rotation_deg: Literal[0, 90] = 0
    erp_area_code: str = Field(min_length=1, max_length=30)
    area_name: str | None = Field(default=None, max_length=100)
    existing_area_id: int | None = Field(default=None, ge=1)
    confirmed: Literal[True]

    @field_validator("erp_area_code")
    @classmethod
    def normalize_erp_area_code(cls, value: str) -> str:
        normalized = (value or "").strip().upper()
        if not normalized:
            raise ValueError("正式区域编号不能为空")
        return normalized

    @field_validator("area_name")
    @classmethod
    def normalize_area_name(cls, value: str | None) -> str | None:
        normalized = (value or "").strip()
        return normalized or None


class TwinZoneGeometryPayload(BaseModel):
    expected_revision: str = Field(min_length=1, max_length=64)
    expected_version: int = Field(ge=1)
    operation_key: str = Field(min_length=8, max_length=120)
    points: list[tuple[float, float]] = Field(min_length=3, max_length=64)


class TwinLayoutDraftValidatePayload(BaseModel):
    expected_revision: str = Field(min_length=1, max_length=64)


class TwinFloor4FreightElevatorCalibrationPayload(BaseModel):
    expected_revision: str = Field(min_length=1, max_length=64)
    operation_key: str = Field(min_length=8, max_length=120)
    source_points: list[tuple[float, float]] = Field(min_length=3, max_length=3)
    calibration_mode: Literal["corner_rigid", "doorway_heading"] = "corner_rigid"
    confirmed: Literal[True]


class LegacyRackBindingSelection(BaseModel):
    binding_key: str = Field(min_length=3, max_length=120)
    map_rack_id: str = Field(min_length=1, max_length=80)

    @field_validator("binding_key", "map_rack_id")
    @classmethod
    def normalize_binding_identity(cls, value: str) -> str:
        return value.strip()


class TwinLayoutDraftPublishPayload(BaseModel):
    expected_published_revision: str = Field(min_length=1, max_length=64)
    expected_draft_revision: str = Field(min_length=1, max_length=64)
    operation_key: str = Field(min_length=8, max_length=120)
    legacy_rack_binding_fingerprint: str | None = Field(
        default=None, min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    legacy_rack_bindings: list[LegacyRackBindingSelection] = Field(
        default_factory=list, max_length=100
    )
    legacy_rack_bindings_confirmed: bool = False

    @model_validator(mode="after")
    def validate_legacy_rack_binding_confirmation(self):
        has_binding_facts = bool(
            self.legacy_rack_binding_fingerprint or self.legacy_rack_bindings
        )
        if has_binding_facts and not self.legacy_rack_bindings_confirmed:
            raise ValueError("旧货位绑定必须由管理员明确确认")
        if self.legacy_rack_bindings_confirmed and not self.legacy_rack_binding_fingerprint:
            raise ValueError("旧货位绑定缺少预览指纹")
        return self


class TwinZoneGeometryApplyPayload(BaseModel):
    expected_revision: str = Field(min_length=1, max_length=64)
    expected_published_revision: str = Field(min_length=1, max_length=64)
    expected_version: int = Field(ge=1)
    operation_key: str = Field(min_length=8, max_length=110)


class TwinNoGoRemovalPayload(BaseModel):
    expected_revision: str = Field(min_length=1, max_length=64)
    expected_published_revision: str = Field(min_length=1, max_length=64)
    feature_ids: list[str] = Field(min_length=1, max_length=100)
    operation_key: str = Field(min_length=8, max_length=110)

    @field_validator("feature_ids")
    @classmethod
    def normalize_feature_ids(cls, values: list[str]) -> list[str]:
        normalized = list(dict.fromkeys(str(item or "").strip() for item in values))
        if any(not item for item in normalized):
            raise ValueError("禁放区对象编号不能为空")
        return normalized


class RackLevelLabelPrintPayload(BaseModel):
    floor_code: str = Field(min_length=2, max_length=30)
    map_rack_id: str = Field(min_length=1, max_length=80)
    expected_map_revision: str = Field(min_length=1, max_length=64)
    template_version: Literal["rack_level_80x40_v1"] = "rack_level_80x40_v1"
    source: Literal["region_planning"] = "region_planning"
    idempotency_key: str = Field(min_length=8, max_length=120)

    @field_validator("floor_code")
    @classmethod
    def normalize_floor_code(cls, value: str) -> str:
        return value.strip().upper()

    @field_validator("map_rack_id", "idempotency_key")
    @classmethod
    def normalize_label_identity(cls, value: str) -> str:
        return value.strip()


class TwinAreaRackNumberingPayload(BaseModel):
    expected_revision: str = Field(min_length=1, max_length=64)
    operation_key: str = Field(min_length=8, max_length=120)


class TwinLayoutDraftDiscardPayload(BaseModel):
    expected_revision: str = Field(min_length=1, max_length=64)


class TwinLayoutDraftRebuildPayload(BaseModel):
    expected_published_revision: str = Field(min_length=1, max_length=64)
    operation_key: str = Field(min_length=8, max_length=120)


class Floor1FormalCandidateConfirmPayload(BaseModel):
    expected_map_revision: str = Field(min_length=1, max_length=64)
    expected_plan_fingerprint: str = Field(min_length=64, max_length=64)
    expected_formal_state_fingerprint: str = Field(min_length=64, max_length=64)
    operation_key: str = Field(min_length=8, max_length=120)
    confirmed: Literal[True]


def _handle_twin_layout_edit_error(error: WarehouseTwinLayoutEditError) -> None:
    if isinstance(error, WarehouseTwinLayoutEditNotFoundError):
        raise HTTPException(status_code=404, detail=str(error)) from error
    if isinstance(error, WarehouseTwinLayoutEditConflictError):
        raise HTTPException(status_code=409, detail=str(error)) from error
    raise HTTPException(status_code=422, detail=str(error)) from error


def _twin_layout_asset_log(
    db: Session,
    *,
    request: Request,
    user: User,
    action: str,
    entity_type: str,
    entity_id: str | None,
    description: str,
    details: dict,
) -> None:
    db.add(
        OperationLog(
            user_id=user.id,
            username=user.username,
            role=user.role,
            action=action,
            resource=f"warehouse/twin-layout/{entity_type}/{entity_id or 'unknown'}",
            entity_type=entity_type,
            entity_id=None,
            description=description,
            details=json.dumps(details, ensure_ascii=False, default=str),
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )
    )


@router.get("/twin-layout/floors/1F/formal-candidates")
def preview_floor1_formal_candidates(
    db: Session = Depends(get_db),
    _user: User = Depends(admin_only),
) -> dict:
    try:
        floor_layout = overlay_formal_area_bindings(
            db,
            floor_code="1F",
            floor_layout=load_warehouse_twin_floor("1F"),
            include_draft=False,
        )
        plan = build_floor1_formal_candidate_plan(floor_layout)
        return {
            **plan,
            "formal_state": inspect_floor1_formal_candidate_state(db, plan=plan),
        }
    except (Floor1CandidatePlanningError, WarehouseTwinLayoutNotFoundError) as error:
        status_code = getattr(error, "status_code", 404)
        raise HTTPException(status_code=status_code, detail=str(error)) from error


@router.post("/twin-layout/floors/1F/formal-candidates/confirm")
def confirm_floor1_formal_candidates(
    payload: Floor1FormalCandidateConfirmPayload,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    try:
        _claim_floor_projection_for_layout_write(db, floor_code="1F")
        floor_layout = overlay_formal_area_bindings(
            db,
            floor_code="1F",
            floor_layout=load_warehouse_twin_floor("1F"),
            include_draft=False,
        )
        result = confirm_floor1_formal_candidate_plan(
            db,
            floor_layout=floor_layout,
            expected_revision=payload.expected_map_revision,
            expected_fingerprint=payload.expected_plan_fingerprint,
            expected_formal_state_fingerprint=(
                payload.expected_formal_state_fingerprint
            ),
            operator_id=user.id,
            reviewer_name=_capacity_reviewer_name(user),
        )
        if result.applied:
            _twin_layout_asset_log(
                db,
                request=request,
                user=user,
                action="FLOOR1_FORMAL_CANDIDATES_CONFIRM",
                entity_type="floor1_formal_candidate_plan",
                entity_id=result.plan["plan_fingerprint"],
                description="管理员一次确认一楼实体区域、容量与正式库位候选",
                details={
                    "operation_key": payload.operation_key,
                    "map_revision": result.plan["map_revision"],
                    "plan_fingerprint": result.plan["plan_fingerprint"],
                    "area_count": len(result.areas),
                    "formal_location_count": len(result.locations),
                    "archived_legacy_area_count": len(
                        result.archived_legacy_areas
                    ),
                    "long_term_pallet_capacity": result.plan["long_term_pallet_capacity"],
                    "inventory_changed": False,
                },
            )
        db.commit()
        return {
            **result.plan,
            "applied": result.applied,
            "area_count": len(result.areas),
            "created_location_count": len(result.locations),
            "archived_legacy_area_count": len(result.archived_legacy_areas),
            "message": (
                "一楼实体区域、容量与适用正式库位已确认启用"
                if result.applied
                else "该版本的一楼区域候选已确认，无需重复生成"
            ),
        }
    except (Floor1CandidatePlanningError, WarehouseTwinLayoutNotFoundError) as error:
        db.rollback()
        status_code = getattr(error, "status_code", 404)
        raise HTTPException(status_code=status_code, detail=str(error)) from error
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="区域编号、地图绑定或正式库位已存在，请刷新候选并核对冲突",
        ) from error


def _rack_layout_values(payload: TwinRackLayoutFields) -> dict:
    return payload.model_dump(
        exclude={"expected_revision", "expected_version", "operation_key", "area_feature_id"},
        exclude_none=True,
    )


def _formal_area_identity_blockers(db: Session, floor_code: str) -> list[str]:
    """Verify that a draft which selected an existing area still points to it."""

    normalized = floor_code.strip().upper()
    floor = warehouse_floor_for_code(db, normalized)
    if floor is None:
        return []
    draft = load_warehouse_twin_layout_draft(normalized)
    areas = list(
        db.scalars(
            select(WarehouseArea)
            .where(WarehouseArea.floor_id == floor.id)
            .options(selectinload(WarehouseArea.storage_policy))
        ).all()
    )
    areas_by_id = {area.id: area for area in areas}
    areas_by_code = {area.area_code.upper(): area for area in areas}
    policies_by_feature = {
        policy.map_feature_id: policy
        for policy in db.scalars(
            select(WarehouseAreaStoragePolicy)
            .join(WarehouseArea)
            .where(WarehouseArea.floor_id == floor.id)
            .options(selectinload(WarehouseAreaStoragePolicy.area))
        ).all()
    }
    feature_area_code_counts: dict[str, int] = {}
    for item in draft.get("features") or []:
        if item.get("feature_kind") != "zone":
            continue
        code = str(item.get("erp_area_code") or "").strip().upper()
        if code:
            feature_area_code_counts[code] = feature_area_code_counts.get(code, 0) + 1
    blockers: list[str] = []
    for feature in draft.get("features") or []:
        if feature.get("feature_kind") != "zone" or not feature.get("id"):
            continue
        feature_id = str(feature["id"])
        area_code = str(feature.get("erp_area_code") or "").strip().upper()
        if not area_code:
            continue
        raw_area_id = feature.get("formal_area_id")
        raw_floor_id = feature.get("formal_floor_id")
        has_area_id = raw_area_id not in (None, "")
        has_floor_id = raw_floor_id not in (None, "")
        if has_area_id != has_floor_id:
            blockers.append(f"{area_code} 区域草稿的正式区域身份不完整")
            continue
        policy = policies_by_feature.get(feature_id)
        area_by_code = areas_by_code.get(area_code)
        if has_area_id:
            try:
                expected_area_id = int(raw_area_id)
                expected_floor_id = int(raw_floor_id)
            except (TypeError, ValueError):
                blockers.append(f"{area_code} 区域草稿的正式区域身份无效")
                continue
            expected_area = areas_by_id.get(expected_area_id)
            if (
                expected_area is None
                or expected_area.floor_id != floor.id
                or expected_floor_id != floor.id
                or expected_area.area_code.upper() != area_code
                or area_by_code is None
                or area_by_code.id != expected_area_id
            ):
                blockers.append(f"{area_code} 区域草稿对应的正式区域身份已变化")
                continue
            if (
                expected_area.storage_policy is not None
                and expected_area.storage_policy.map_feature_id != feature_id
            ):
                blockers.append(f"{area_code} 正式区域已绑定其他地图区域")
                continue
            if policy is not None and policy.area_id != expected_area_id:
                blockers.append(f"{area_code} 地图区域已绑定其他正式区域")
        elif (
            area_by_code is not None
            and area_by_code.storage_policy is None
            and policy is None
        ):
            if not floor3_v11_map_binding_is_proven(
                db,
                floor=floor,
                feature=feature,
                area=area_by_code,
                feature_area_code_count=feature_area_code_counts.get(area_code, 0),
            ):
                blockers.append(f"{area_code} 已是现有未绑定区域，必须返回区域设置明确选择")
    return blockers


@router.post("/twin-layout/floors/{floor_code}/draft/validate")
def validate_twin_layout_draft(
    floor_code: str,
    payload: TwinLayoutDraftValidatePayload,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    with WAREHOUSE_TWIN_LAYOUT_TRANSACTION_LOCK:
        _claim_floor_projection_for_layout_write(db, floor_code=floor_code)
        draft_snapshot = snapshot_warehouse_twin_layout_draft()
        result = None
        tombstone_projection = None
        try:
            validation_revision, tombstone_projection = (
                _apply_archived_area_tombstones_before_validation(
                    db,
                    floor_code=floor_code,
                    expected_revision=payload.expected_revision,
                )
            )
            identity_blockers = _formal_area_identity_blockers(db, floor_code)
            if identity_blockers:
                raise HTTPException(
                    status_code=409,
                    detail="正式区域身份核验未通过："
                    + "；".join(identity_blockers[:5]),
                )
            geometry_blockers = _formal_location_zone_geometry_blockers(db, floor_code)
            if geometry_blockers:
                raise HTTPException(
                    status_code=409,
                    detail="正式货位坐标核验未通过："
                    + "；".join(geometry_blockers[:5]),
                )
            mold_relocation_warnings: list[str] = []
            if floor_code.strip().upper() == "1F":
                mold_relocations = plan_mold_rack_layout_relocations(
                    db,
                    load_effective_warehouse_twin_floor_for_edit("1F"),
                )
                mold_relocation_warnings = mold_rack_layout_relocation_warnings(
                    mold_relocations
                )
            result = validate_warehouse_twin_layout_draft(
                floor_code,
                expected_revision=validation_revision,
                additional_warnings=mold_relocation_warnings,
            )
            _twin_layout_asset_log(
                db,
                request=request,
                user=user,
                action="TWIN_LAYOUT_DRAFT_VALIDATE",
                entity_type="twin_layout_draft",
                entity_id=floor_code.upper(),
                description="管理员校验仓库地图草稿",
                details={
                    **result.value,
                    "archive_tombstone_projection": tombstone_projection,
                },
            )
            db.commit()
        except WarehouseTwinLayoutEditError as error:
            db.rollback()
            if tombstone_projection is not None or (
                result is not None and result.applied
            ):
                restore_warehouse_twin_layout_draft(draft_snapshot)
            _handle_twin_layout_edit_error(error)
        except Exception:
            db.rollback()
            if tombstone_projection is not None or (
                result is not None and result.applied
            ):
                restore_warehouse_twin_layout_draft(draft_snapshot)
            raise
        return {
            **result.value,
            "applied": result.applied,
            "archive_tombstone_projection": tombstone_projection,
        }


@router.post("/twin-layout/floors/{floor_code}/draft/calibrate-freight-elevator")
def calibrate_twin_floor4_freight_elevator(
    floor_code: str,
    payload: TwinFloor4FreightElevatorCalibrationPayload,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    with WAREHOUSE_TWIN_LAYOUT_TRANSACTION_LOCK:
        draft_snapshot = snapshot_warehouse_twin_layout_draft()
        result = None
        try:
            result = calibrate_floor4_freight_elevator(
                floor_code,
                expected_revision=payload.expected_revision,
                operation_key=payload.operation_key,
                source_points=payload.source_points,
                calibration_mode=payload.calibration_mode,
            )
            if result.applied:
                recalibrated = result.value.get("recalibrated") is True
                _twin_layout_asset_log(
                    db,
                    request=request,
                    user=user,
                    action=(
                        "TWIN_LAYOUT_FLOOR4_RECALIBRATE"
                        if recalibrated
                        else "TWIN_LAYOUT_FLOOR4_CALIBRATE"
                    ),
                    entity_type="twin_layout_calibration",
                    entity_id="4F:LIFT-002",
                    description=(
                        "管理员重新完成四楼三点货梯标定"
                        if recalibrated
                        else "管理员完成四楼三点货梯标定"
                    ),
                    details={
                        "floor_code": floor_code.strip().upper(),
                        "revision": result.floor_revision,
                        "applied": result.applied,
                        **result.value,
                    },
                )
            db.commit()
        except WarehouseTwinLayoutEditError as error:
            db.rollback()
            if result is not None and result.applied:
                restore_warehouse_twin_layout_draft(draft_snapshot)
            _handle_twin_layout_edit_error(error)
        except Exception:
            db.rollback()
            if result is not None and result.applied:
                restore_warehouse_twin_layout_draft(draft_snapshot)
            raise
        return {
            "item": result.value,
            "revision": result.floor_revision,
            "applied": result.applied,
        }


def _map_feature_points_match(
    published_feature: Mapping,
    draft_feature: Mapping,
    *,
    tolerance_mm: Decimal = Decimal("0.001"),
) -> bool:
    """Compare one measured feature without treating numeric formatting as movement."""

    published_points = list(published_feature.get("points") or [])
    draft_points = list(draft_feature.get("points") or [])
    if len(published_points) != len(draft_points):
        return False
    try:
        return all(
            len(published_point) >= 2
            and len(draft_point) >= 2
            and abs(
                Decimal(str(published_point[0])) - Decimal(str(draft_point[0]))
            )
            <= tolerance_mm
            and abs(
                Decimal(str(published_point[1])) - Decimal(str(draft_point[1]))
            )
            <= tolerance_mm
            for published_point, draft_point in zip(
                published_points, draft_points, strict=True
            )
        )
    except (ArithmeticError, TypeError, ValueError):
        return False


def _formal_location_zone_geometry_blockers(
    db: Session,
    floor_code: str,
    *,
    draft_layout: Mapping | None = None,
    published_layout: Mapping | None = None,
) -> list[str]:
    """Reject zone movement that would reinterpret saved percentage positions.

    Formal ground locations store their rectangles relative to the published
    zone.  Moving or resizing that zone without an explicit coordinate rebase
    would therefore move every physical location even though no location save
    occurred.  Keep that operation fail-closed until a dedicated, atomic rebase
    flow is used.
    """

    normalized = floor_code.strip().upper()
    floor = warehouse_floor_for_code(db, normalized)
    if floor is None:
        return []
    draft = draft_layout or load_warehouse_twin_layout_draft(normalized)
    mapped_area_codes = {
        str(value or "").strip().upper()
        for value in db.scalars(
            select(WarehouseLocation.area_code)
            .join(
                Floor3LocationLayout,
                Floor3LocationLayout.location_id == WarehouseLocation.id,
            )
            .where(
                WarehouseLocation.warehouse_floor == floor.floor_number,
                WarehouseLocation.storage_type == "ground",
                WarehouseLocation.is_active.is_(True),
            )
            .distinct()
        ).all()
        if str(value or "").strip()
    }
    if not mapped_area_codes:
        return []
    if published_layout is None:
        try:
            published = load_published_warehouse_twin_floor_for_edit(normalized)
        except WarehouseTwinLayoutEditNotFoundError:
            return ["正式地图缺失，无法核对已有正式货位的绝对坐标"]
    else:
        published = published_layout
    from app.services.warehouse_location_geometry_draft import validate_adjustments
    try:
        checked_adjustments = validate_adjustments(db, floor_code=normalized, draft=draft, published=published)
    except (WarehouseTwinLayoutEditError, Floor1CandidatePlanningError) as error:
        return [str(error)]
    draft_features = {
        str(item.get("id") or ""): item
        for item in draft.get("features") or []
        if item.get("feature_kind") == "zone" and item.get("id")
    }
    published_features = {
        str(item.get("id") or ""): item
        for item in published.get("features") or []
        if item.get("feature_kind") == "zone" and item.get("id")
    }
    policies = list(
        db.scalars(
            select(WarehouseAreaStoragePolicy)
            .join(WarehouseArea)
            .where(
                WarehouseArea.floor_id == floor.id,
                WarehouseAreaStoragePolicy.status == "published",
            )
            .options(selectinload(WarehouseAreaStoragePolicy.area))
        ).all()
    )
    blockers: list[str] = []
    for policy in policies:
        area_code = policy.area.area_code.strip().upper()
        if area_code not in mapped_area_codes:
            continue
        published_feature = published_features.get(policy.map_feature_id)
        draft_feature = draft_features.get(policy.map_feature_id)
        if published_feature is None or draft_feature is None:
            continue
        if policy.map_feature_id in checked_adjustments:
            continue
        if not _map_feature_points_match(published_feature, draft_feature):
            blockers.append(
                f"{area_code} 区已有正式货位，不能随区域边界一起移动或缩放；"
                "请放弃该区域几何草稿，改为逐个调整货位"
            )
    return blockers


def _formal_area_publish_blockers(
    db: Session,
    floor_code: str,
    *,
    defer_location_readiness_for_feature_id: str | None = None,
) -> list[str]:
    normalized = floor_code.strip().upper()
    floor = warehouse_floor_for_code(db, normalized)
    if floor is None:
        return []
    draft = load_warehouse_twin_layout_draft(normalized)
    features = {
        str(item.get("id") or ""): item
        for item in draft.get("features") or []
        if item.get("feature_kind") == "zone" and item.get("id")
    }
    precise_rack_feature_ids = {
        str(item.get("area_feature_id") or "").strip()
        for item in draft.get("racks") or []
        if item.get("id") and str(item.get("area_feature_id") or "").strip()
    }
    policies = list(
        db.scalars(
            select(WarehouseAreaStoragePolicy)
            .join(WarehouseArea)
            .where(WarehouseArea.floor_id == floor.id)
            .options(selectinload(WarehouseAreaStoragePolicy.area))
        ).all()
    )
    policies = [
        policy
        for policy in policies
        if policy.status != "archived"
        and policy.area.construction_status != "archived"
    ]
    all_areas = list(
        db.scalars(
            select(WarehouseArea)
            .where(WarehouseArea.floor_id == floor.id)
            .options(selectinload(WarehouseArea.storage_policy))
        ).all()
    )
    all_areas = [
        area
        for area in all_areas
        if area.construction_status != "archived"
        and (
            area.storage_policy is None
            or area.storage_policy.status != "archived"
        )
    ]
    customer_preferred_area_ids = {
        int(area_id)
        for area_id in db.scalars(
            select(CustomerFinishedStoragePreference.warehouse_area_id)
            .join(
                WarehouseArea,
                WarehouseArea.id
                == CustomerFinishedStoragePreference.warehouse_area_id,
            )
            .where(WarehouseArea.floor_id == floor.id)
        ).all()
    }
    blockers: list[str] = _formal_location_zone_geometry_blockers(
        db,
        normalized,
        draft_layout=draft,
    )
    policies_by_feature = {policy.map_feature_id: policy for policy in policies}
    areas_by_code = {area.area_code.upper(): area for area in all_areas}
    feature_area_code_counts: dict[str, int] = {}
    for item in features.values():
        code = str(item.get("erp_area_code") or "").strip().upper()
        if code:
            feature_area_code_counts[code] = feature_area_code_counts.get(code, 0) + 1
    for feature_id, feature in features.items():
        area_code = str(feature.get("erp_area_code") or "").strip().upper()
        if not area_code:
            continue
        policy = policies_by_feature.get(feature_id)
        area = areas_by_code.get(area_code)
        if policy is not None and policy.area.area_code.upper() != area_code:
            blockers.append(f"{area_code} 地图区域已绑定其他正式区域")
            continue
        if area is not None and area.storage_policy is not None:
            if area.storage_policy.map_feature_id != feature_id:
                blockers.append(f"{area_code} 正式区域已绑定其他地图区域")
                continue
        requested_type_set = {
            str(value).strip()
            for value in feature.get("allowed_inventory_types") or []
        }
        if (
            area is not None
            and int(area.id) in customer_preferred_area_ids
            and "finished" not in requested_type_set
        ):
            blockers.append(
                f"{area_code} 区仍是客户默认成品区域，区域策略必须保留成品用途"
            )
        if area is None:
            orphaned = db.scalar(
                select(WarehouseLocation.id).where(
                    WarehouseLocation.warehouse_floor == floor.floor_number,
                    func.upper(WarehouseLocation.area_code) == area_code,
                ).limit(1)
            )
            if orphaned is not None:
                blockers.append(f"{area_code} 区仍有未纳入正式区域台账的历史库位")
            blockers.extend(
                _zone_asset_and_production_blockers(
                    db,
                    floor_layout=draft,
                    feature=feature,
                    area_code=area_code,
                    fail_closed_on_mapping_error=False,
                )
            )
            continue
        if area.storage_policy is None:
            location_rows = formal_area_location_rows(db, floor=floor, area=area)
            projected_legacy_binding = floor3_v11_map_binding_is_proven(
                db,
                floor=floor,
                feature=feature,
                area=area,
                feature_area_code_count=feature_area_code_counts.get(area_code, 0),
            )
            if feature.get("legacy_v11_name_only") is True:
                try:
                    marker_area_id = int(feature.get("formal_area_id"))
                    marker_floor_id = int(feature.get("formal_floor_id"))
                except (TypeError, ValueError):
                    marker_area_id = marker_floor_id = 0
                marker_safe = bool(
                    projected_legacy_binding
                    and marker_area_id == area.id
                    and marker_floor_id == floor.id
                    and str(feature.get("formal_area_name") or "").strip()
                    and legacy_v11_name_only_change_is_safe(
                        db,
                        floor=floor,
                        area=area,
                        feature=feature,
                        feature_area_code_count=feature_area_code_counts.get(
                            area_code, 0
                        ),
                        requested_inventory_types=list(
                            feature.get("allowed_inventory_types") or []
                        ),
                        requested_storage_layout=str(
                            feature.get("storage_layout") or ""
                        ),
                        current_feature=feature,
                    )
                )
                if marker_safe:
                    continue
                blockers.append(
                    f"{area_code} 区域名称草稿已失去旧版实测区域的唯一身份"
                )
                continue
            has_explicit_policy_change = bool(
                feature.get("allowed_inventory_types")
                or feature.get("formal_area_id") not in (None, "")
                or feature.get("formal_floor_id") not in (None, "")
            )
            if projected_legacy_binding and not has_explicit_policy_change:
                continue
            if location_rows:
                raw_area_id = feature.get("formal_area_id")
                if str(raw_area_id or "") != str(area.id):
                    blockers.append(f"{area_code} 区与实测地图的正式身份尚未确认")
                else:
                    for message in unbound_area_location_transition_blockers(
                        db,
                        floor=floor,
                        area=area,
                        requested_inventory_types=list(
                            feature.get("allowed_inventory_types") or []
                        ),
                        requested_storage_layout=str(
                            feature.get("storage_layout") or ""
                        ),
                    ):
                        blockers.append(f"{area.area_name or area_code}：{message}")
            blockers.extend(
                _zone_asset_and_production_blockers(
                    db,
                    floor_layout=draft,
                    feature=feature,
                    area_code=area_code,
                    fail_closed_on_mapping_error=False,
                )
            )
            continue
        requested_types = list(feature.get("allowed_inventory_types") or [])
        requested_layout = str(feature.get("storage_layout") or "")
        semantic_change = (
            set(policy_inventory_types(area.storage_policy)) != set(requested_types)
            or area.storage_policy.storage_layout != requested_layout
            or area.storage_policy.map_feature_id != feature_id
        )
        if semantic_change:
            blockers.extend(
                _zone_asset_and_production_blockers(
                    db,
                    floor_layout=draft,
                    feature=feature,
                    area_code=area_code,
                    fail_closed_on_mapping_error=False,
                )
            )
        for message in policy_location_transition_blockers(
            db,
            floor=floor,
            area=area,
            policy=area.storage_policy,
            requested_inventory_types=requested_types,
            requested_storage_layout=requested_layout,
        ):
            blockers.append(f"{area.area_name or area_code}：{message}")
    for policy in policies:
        area = policy.area
        feature = features.get(policy.map_feature_id)
        if feature is None:
            blockers.append(f"{area.area_code} 区绑定的地图区域已不存在")
            continue
        feature_area_code = str(feature.get("erp_area_code") or "").strip().upper()
        if policy.status != "published" and not feature_area_code:
            # Keep an unrelated advanced policy draft isolated from the
            # current one-step area confirmation.
            continue
        if feature_area_code and feature_area_code != area.area_code.upper():
            blockers.append(f"{area.area_code} 区的地图正式区域编号不一致")
        if (
            area.planned_location_count
            and policy.map_feature_id != defer_location_readiness_for_feature_id
            # A rack publish reconciles its formal cells later in the same
            # transaction.  Pre-publish rows may therefore contain both the
            # precise cells and empty planning anchors that the rack sync will
            # retire.  Do not let the older generic count gate deadlock that
            # safe reconciliation; occupied anchors and removed cells remain
            # fail-closed in sync_published_rack_cells().
            and not (
                policy.storage_layout == "rack"
                and policy.map_feature_id in precise_rack_feature_ids
            )
        ):
            try:
                route = resolve_area_location_management(
                    db,
                    floor_code=floor.floor_code,
                    area_code=area.area_code,
                )
            except WarehouseAreaActivationError as error:
                blockers.append(str(error))
                continue
            source_version = route.source_version
            active_rows = list(
                db.scalars(
                    select(WarehouseLocation).where(
                        WarehouseLocation.warehouse_floor == floor.floor_number,
                        WarehouseLocation.area_code == area.area_code,
                        WarehouseLocation.source_version == source_version,
                        WarehouseLocation.is_active.is_(True),
                    )
                ).all()
            )
            if len(active_rows) != area.planned_location_count:
                blockers.append(
                    f"{area.area_code} 区计划 {area.planned_location_count} 个库位，当前有效 {len(active_rows)} 个"
                )
            elif any(row.placement_status != "placed" for row in active_rows):
                blockers.append(f"{area.area_code} 区仍有库位未完成布局")
    return blockers


def _mapped_live_production_task_ids(db: Session) -> set[int]:
    """Return the exact live pending-task identities used by production UI."""

    return {
        int(row["id"])
        for row in list_production_task_dashboard_rows(
            db,
            allowed_customer_ids=None,
            status=PENDING,
        )
    }


def _zone_asset_and_production_blockers(
    db: Session,
    *,
    floor_layout: dict,
    feature: dict,
    area_code: str,
    fail_closed_on_mapping_error: bool,
) -> list[str]:
    feature_id = str(feature.get("id") or "")
    feature_code = str(feature.get("feature_code") or "")
    blockers: list[str] = []
    for mold in db.scalars(
        select(MoldTool).where(
            or_(MoldTool.is_active.is_(True), MoldTool.archive_status == "archived")
        )
    ).all():
        codes = set(
            mold_location_feature_codes(mold.rack_location, floor_layout=floor_layout)
        ) | set(_twin_reference_feature_codes("mold", mold.rack_location))
        guide = describe_mold_location(mold.rack_location)
        exact_archive = (
            guide.get("floor") == floor_layout.get("floor_code")
            and str(guide.get("area") or "").upper() == area_code
        )
        occupied = (
            exact_archive
            if mold.archive_status == "archived"
            else feature_id in codes or feature_code in codes
        )
        if occupied:
            blockers.append("区域内仍有实体模具，不能改变区域策略")
            break
    for plate in db.scalars(
        select(PrintingPlate).where(PrintingPlate.status.in_(("active", "damaged")))
    ).all():
        guide = describe_printing_plate_location(plate.rack_location)
        codes = set(
            _twin_reference_feature_codes("printing_plate", plate.rack_location)
        )
        if guide.get("kind") == "plate_rack":
            codes.add("ZONE-1F-PLATE-002")
        if feature_id in codes or feature_code in codes:
            blockers.append("区域内仍有启用或受损挂板，不能改变区域策略")
            break
    # Production-task placement is an explicit 1F-only visual projection: all
    # public mapping endpoints load the 1F layout.  Requiring its optional
    # isolated database while archiving a 3F/4F area freezes unrelated empty
    # warehouse areas even though no supported workflow can map tasks there.
    if str(floor_layout.get("floor_code") or "").strip().upper() == "1F":
        try:
            mappings = list_production_projection_mappings(
                str(floor_layout.get("layout_id") or ""), floor_layout
            )
        except (WarehouseTwinProductionError, sqlite3.Error, OSError):
            if fail_closed_on_mapping_error:
                blockers.append("生产任务地图占用状态暂无法核对")
        else:
            pending_ids = _mapped_live_production_task_ids(db)
            pallets = {
                str(item.get("id")): item for item in floor_layout.get("pallets") or []
            }
            for mapping in mappings:
                if int(mapping.get("source_task_id") or 0) not in pending_ids:
                    continue
                target_matches = (
                    mapping.get("target_kind") == "zone"
                    and str(mapping.get("target_id")) == feature_id
                )
                if mapping.get("target_kind") == "pallet":
                    pallet = pallets.get(str(mapping.get("target_id"))) or {}
                    target_matches = (
                        str(pallet.get("zone_id") or "") == feature_id
                        or str(pallet.get("zone_code") or "") == feature_code
                    )
                if target_matches:
                    blockers.append("区域内仍有待生产任务地图占用，不能改变区域策略")
                    break
    return blockers


def _claim_floor_projection_for_layout_write(
    db: Session,
    *,
    floor_code: str,
) -> None:
    """Serialize a runtime-map change with every inventory destination write."""

    normalized_floor = floor_code.strip().upper()
    floor = warehouse_floor_for_code(db, normalized_floor)
    if floor is None:
        return
    floor_id = int(floor.id)
    floor_stored_code = floor.floor_code.strip().upper()
    floor_number = int(floor.floor_number)
    try:
        floor_claimed = claim_warehouse_floor_projection_by_id(
            db,
            floor_id=floor_id,
        )
    except OperationalError as error:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="该楼层正在执行入库、移位或地图发布，请稍后刷新重试",
        ) from error
    if not floor_claimed:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="仓库楼层台账已变化，请刷新后重新发布",
        )
    current_floor = db.scalar(
        select(WarehouseFloor)
        .where(WarehouseFloor.id == floor_id)
        .execution_options(populate_existing=True)
    )
    if (
        current_floor is None
        or current_floor.floor_code.strip().upper() != floor_stored_code
        or int(current_floor.floor_number) != floor_number
    ):
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="仓库楼层身份已变化，请刷新后重新操作",
        )
    resolved_floor = warehouse_floor_for_code(db, normalized_floor)
    if resolved_floor is None or int(resolved_floor.id) != floor_id:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="仓库楼层路由已变化，请刷新后重新操作",
        )
    # All spatial reads below occur after this persistent claim.  Canonical
    # projection loaders use ``populate_existing`` for their exact objects;
    # never expire unrelated dirty business rows in this shared transaction.


def _claim_floor_numbers_for_layout_write(
    db: Session,
    *floor_numbers: int | None,
) -> None:
    for floor_number in sorted(
        {int(value) for value in floor_numbers if value is not None}
    ):
        _claim_floor_projection_for_layout_write(
            db,
            floor_code=f"{floor_number}F",
        )


def _claim_floor_ids_for_layout_write(
    db: Session,
    *floor_ids: int | None,
) -> None:
    """Claim stable floor rows before editing floor or area identities."""

    for floor_id in sorted({int(value) for value in floor_ids if value is not None}):
        try:
            floor_claimed = claim_warehouse_floor_projection_by_id(
                db,
                floor_id=floor_id,
            )
        except OperationalError as error:
            db.rollback()
            raise HTTPException(
                status_code=409,
                detail="该楼层正在执行区域归档、入库或地图调整，请稍后刷新重试",
            ) from error
        if not floor_claimed:
            db.rollback()
            raise HTTPException(
                status_code=409,
                detail="仓库楼层台账已变化，请刷新后重试",
            )


@router.get("/twin-layout/floors/{floor_code}/draft/rack-cell-bindings/preview")
def preview_twin_layout_legacy_rack_bindings(
    floor_code: str,
    expected_revision: str = Query(min_length=1, max_length=64),
    db: Session = Depends(get_db),
    _user: User = Depends(admin_only),
) -> dict:
    """Preview exact old-location bindings; this endpoint is strictly read-only."""

    try:
        floor_layout = load_warehouse_twin_layout_draft(floor_code)
    except WarehouseTwinLayoutEditError as error:
        _handle_twin_layout_edit_error(error)
    revision = str(floor_layout.get("revision") or "").strip()
    if revision != expected_revision:
        raise HTTPException(
            status_code=409,
            detail="仓库布局草稿已变化，请刷新后重新预览旧货位绑定。",
        )
    return preview_legacy_rack_cell_bindings(db, floor_layout=floor_layout)


@router.post("/twin-layout/floors/{floor_code}/zones/{feature_id}/apply-geometry")
def apply_twin_zone_geometry(
    floor_code: str, feature_id: str, payload: TwinZoneGeometryApplyPayload,
    request: Request, db: Session = Depends(get_db), user: User = Depends(admin_only),
) -> dict:
    """Apply one saved ground-zone geometry without publishing unrelated drafts."""
    from app.services.warehouse_location_geometry_draft import (
        verify_adjustment, rebase_verified_adjustments,
    )
    floor_code = floor_code.strip().upper()
    request_hash = hashlib.sha256(json.dumps({
        **payload.model_dump(), 'floor_code': floor_code, 'feature_id': feature_id,
    }, sort_keys=True).encode()).hexdigest()
    with WAREHOUSE_TWIN_LAYOUT_TRANSACTION_LOCK:
        replay = db.scalar(select(OperationLog).where(
            OperationLog.action_code == 'warehouse.zone_geometry.apply',
            OperationLog.request_id == payload.operation_key,
        ))
        if replay:
            details = json.loads(replay.details or '{}')
            if details.get('request_hash') != request_hash or replay.user_id != user.id:
                raise HTTPException(status_code=409, detail='同一应用编号不能用于不同调整')
            return {**details['result'], 'applied': False, 'idempotent_replay': True}
        _claim_floor_projection_for_layout_write(db, floor_code=floor_code)
        snapshot = snapshot_warehouse_twin_publish_state()
        result = None
        try:
            published = load_published_warehouse_twin_floor_for_edit(floor_code)
            draft = load_warehouse_twin_layout_draft(floor_code)
            if (published['revision'] != payload.expected_published_revision
                    or draft['revision'] != payload.expected_revision):
                raise HTTPException(status_code=409, detail='地图版本已变化，未应用；请刷新核对')
            feature = next((f for f in draft['features'] if f['id'] == feature_id), None)
            if not feature or feature.get('feature_kind') != 'zone':
                raise HTTPException(status_code=404, detail='区域不存在')
            if int(feature.get('version') or 1) != payload.expected_version:
                raise HTTPException(status_code=409, detail='区域版本已变化，未应用；请刷新核对')
            pending = {}
            for item in draft['features']:
                value = verify_adjustment(db, floor_code=floor_code, feature=item, published=published)
                if value:
                    pending[item['id']] = value
            published_feature = next(
                (item for item in published['features'] if item.get('id') == feature_id),
                None,
            )
            boundary_changed = bool(
                published_feature
                and feature.get('points') != published_feature.get('points')
            )
            if feature_id not in pending and not boundary_changed:
                raise HTTPException(status_code=409, detail='本区域没有已保存的位置调整；请先调整区域或货位')
            policies = list(db.scalars(select(WarehouseAreaStoragePolicy).join(WarehouseArea).join(WarehouseFloor)
                .where(WarehouseFloor.floor_code == floor_code)))
            metadata_fields = {'version', 'published_map_revision', 'updated_at', 'updated_by', 'published_at', 'published_by'}
            def policy_facts():
                return {p.id: {c.name: getattr(p, c.name) for c in WarehouseAreaStoragePolicy.__table__.columns
                               if c.name not in metadata_fields} for p in policies}
            policy_before = policy_facts()
            context = begin_warehouse_twin_one_step_publish(
                floor_code, feature_id, expected_effective_revision=payload.expected_revision,
                expected_published_revision=payload.expected_published_revision, geometry_only=True,
            )
            validation = validate_warehouse_twin_layout_draft(
                floor_code, expected_revision=context.published_floor_revision,
            )
            if validation.value.get('blockers'):
                raise HTTPException(status_code=409, detail='本区域调整未应用：' + '；'.join(validation.value['blockers'][:5]))
            result = _publish_twin_layout_draft_locked(
                floor_code=floor_code, payload=TwinLayoutDraftPublishPayload(
                    expected_published_revision=payload.expected_published_revision,
                    expected_draft_revision=context.published_floor_revision,
                    operation_key=payload.operation_key + '-zone',
                ), request=request, db=db, user=user, commit=False, floor_projection_claimed=True,
                allow_archived_tombstone_cleanup=True,
            )
            if policy_facts() != policy_before:
                raise HTTPException(status_code=409, detail='当前还涉及区域用途变更，位置调整未应用；请先核对用途草稿')
            pending.pop(feature_id, None)
            remaining = rebase_verified_adjustments(db, floor_code=floor_code,
                remaining=pending, published=load_published_warehouse_twin_floor_for_edit(floor_code))
            preserved = rebase_warehouse_twin_advanced_draft_after_one_step(
                context, floor_code, feature_id, geometry_only=True, remaining_location_drafts=remaining,
            )
            result = {**result, 'feature_id': feature_id, 'other_drafts_preserved': preserved,
                      'inventory_changed': False, 'scope': 'zone_geometry'}
            db.add(OperationLog(user_id=user.id, username=user.username, role=user.role,
                action='UPDATE', resource=f'warehouse/{floor_code}/zones/{feature_id}/geometry',
                entity_type='warehouse_zone_geometry', description='应用当前区域与货位几何，保留其他草稿',
                action_code='warehouse.zone_geometry.apply', request_id=payload.operation_key,
                details=json.dumps({'request_hash': request_hash, 'result': result}, ensure_ascii=False)))
            db.commit()
            return result
        except Exception as error:
            db.rollback()
            restore_warehouse_twin_publish_state(snapshot, backup_name=result.get('backup_name') if result else None)
            if isinstance(error, WarehouseTwinLayoutEditError):
                _handle_twin_layout_edit_error(error)
            raise


@router.post("/twin-layout/floors/{floor_code}/no-go-features/remove")
def remove_twin_no_go_features(
    floor_code: str,
    payload: TwinNoGoRemovalPayload,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    """Remove explicit no-go overlays without publishing unrelated drafts."""

    floor_code = floor_code.strip().upper()
    request_hash = hashlib.sha256(
        json.dumps(
            {**payload.model_dump(), "floor_code": floor_code},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    with WAREHOUSE_TWIN_LAYOUT_TRANSACTION_LOCK:
        replay = db.scalar(
            select(OperationLog).where(
                OperationLog.action_code == "warehouse.no_go.remove",
                OperationLog.request_id == payload.operation_key,
            )
        )
        if replay is not None:
            details = json.loads(replay.details or "{}")
            if details.get("request_hash") != request_hash or replay.user_id != user.id:
                raise HTTPException(
                    status_code=409,
                    detail="同一清理编号不能用于不同禁放区清单",
                )
            return {
                **details["result"],
                "applied": False,
                "idempotent_replay": True,
            }

        _claim_floor_projection_for_layout_write(db, floor_code=floor_code)
        snapshot = snapshot_warehouse_twin_publish_state()
        result = None
        try:
            context = begin_warehouse_twin_no_go_removal_publish(
                floor_code,
                payload.feature_ids,
                expected_effective_revision=payload.expected_revision,
                expected_published_revision=payload.expected_published_revision,
            )
            validation = validate_warehouse_twin_layout_draft(
                floor_code,
                expected_revision=context.published_floor_revision,
            )
            if validation.value.get("blockers"):
                raise HTTPException(
                    status_code=409,
                    detail="禁放区未删除："
                    + "；".join(validation.value["blockers"][:5]),
                )
            published = publish_warehouse_twin_layout_draft(
                floor_code,
                expected_published_revision=payload.expected_published_revision,
                expected_draft_revision=context.published_floor_revision,
                operation_key=payload.operation_key + "-publish",
            )
            result = {**published.value, "applied": published.applied}
            preserved = rebase_warehouse_twin_advanced_draft_after_no_go_removal(
                context,
                floor_code,
            )
            response = {
                **result,
                "floor_code": floor_code,
                "removed_feature_ids": list(context.removed_feature_ids),
                "removed_feature_count": len(context.removed_feature_ids),
                "other_drafts_preserved": preserved,
                "inventory_changed": False,
                "scope": "no_go_features",
            }
            db.add(
                OperationLog(
                    user_id=user.id,
                    username=user.username,
                    role=user.role,
                    action="DELETE",
                    resource=f"warehouse/{floor_code}/no-go-features",
                    entity_type="warehouse_no_go_features",
                    description="受控删除指定禁放区，保留其他管理员草稿",
                    action_code="warehouse.no_go.remove",
                    request_id=payload.operation_key,
                    details=json.dumps(
                        {"request_hash": request_hash, "result": response},
                        ensure_ascii=False,
                    ),
                )
            )
            db.commit()
            return response
        except Exception as error:
            db.rollback()
            restore_warehouse_twin_publish_state(
                snapshot,
                backup_name=result.get("backup_name") if result else None,
            )
            if isinstance(error, WarehouseTwinLayoutEditError):
                _handle_twin_layout_edit_error(error)
            raise


@router.post("/twin-layout/floors/{floor_code}/racks/{rack_id}/apply")
def apply_twin_rack_layout(
    floor_code: str,
    rack_id: str,
    payload: TwinZoneGeometryApplyPayload,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    """Apply one saved rack and its formal cells without publishing other drafts."""

    floor_code = floor_code.strip().upper()
    request_hash = hashlib.sha256(
        json.dumps(
            {**payload.model_dump(), "floor_code": floor_code, "rack_id": rack_id},
            sort_keys=True,
        ).encode()
    ).hexdigest()
    with WAREHOUSE_TWIN_LAYOUT_TRANSACTION_LOCK:
        replay = db.scalar(
            select(OperationLog).where(
                OperationLog.action_code == "warehouse.rack.apply",
                OperationLog.request_id == payload.operation_key,
            )
        )
        if replay:
            details = json.loads(replay.details or "{}")
            if details.get("request_hash") != request_hash or replay.user_id != user.id:
                raise HTTPException(status_code=409, detail="同一应用编号不能用于不同货架调整")
            return {**details["result"], "applied": False, "idempotent_replay": True}
        _claim_floor_projection_for_layout_write(db, floor_code=floor_code)
        snapshot = snapshot_warehouse_twin_publish_state()
        result = None
        try:
            published = load_published_warehouse_twin_floor_for_edit(floor_code)
            draft = load_warehouse_twin_layout_draft(floor_code)
            if (
                published["revision"] != payload.expected_published_revision
                or draft["revision"] != payload.expected_revision
            ):
                raise HTTPException(status_code=409, detail="地图版本已变化，货架未应用；请刷新核对")
            rack = next((item for item in draft.get("racks") or [] if item.get("id") == rack_id), None)
            if not rack:
                raise HTTPException(status_code=404, detail="货架不存在")
            if int(rack.get("version") or 1) != payload.expected_version:
                raise HTTPException(status_code=409, detail="货架版本已变化，未应用；请刷新核对")
            published_rack = next(
                (item for item in published.get("racks") or [] if item.get("id") == rack_id),
                None,
            )
            if published_rack == rack:
                raise HTTPException(status_code=409, detail="本货架没有已保存的调整")
            context = begin_warehouse_twin_one_step_rack_publish(
                floor_code,
                rack_id,
                expected_effective_revision=payload.expected_revision,
                expected_published_revision=payload.expected_published_revision,
            )
            validation = validate_warehouse_twin_layout_draft(
                floor_code, expected_revision=context.published_floor_revision,
            )
            if validation.value.get("blockers"):
                raise HTTPException(
                    status_code=409,
                    detail="本货架调整未应用：" + "；".join(validation.value["blockers"][:5]),
                )
            result = _publish_twin_layout_draft_locked(
                floor_code=floor_code,
                payload=TwinLayoutDraftPublishPayload(
                    expected_published_revision=payload.expected_published_revision,
                    expected_draft_revision=context.published_floor_revision,
                    operation_key=payload.operation_key + "-rack",
                ),
                request=request,
                db=db,
                user=user,
                commit=False,
                floor_projection_claimed=True,
                allow_archived_tombstone_cleanup=True,
            )
            preserved = rebase_warehouse_twin_advanced_rack_after_one_step(
                context, floor_code, rack_id,
            )
            result = {
                **result,
                "rack_id": rack_id,
                "other_drafts_preserved": preserved,
                "inventory_changed": False,
                "scope": "rack_layout",
            }
            db.add(
                OperationLog(
                    user_id=user.id,
                    username=user.username,
                    role=user.role,
                    action="UPDATE",
                    resource=f"warehouse/{floor_code}/racks/{rack_id}/layout",
                    entity_type="warehouse_rack_layout",
                    description="应用当前货架布局并保留其他草稿",
                    action_code="warehouse.rack.apply",
                    request_id=payload.operation_key,
                    details=json.dumps(
                        {"request_hash": request_hash, "result": result}, ensure_ascii=False,
                    ),
                )
            )
            db.commit()
            return result
        except Exception as error:
            db.rollback()
            restore_warehouse_twin_publish_state(
                snapshot, backup_name=result.get("backup_name") if result else None,
            )
            if isinstance(error, WarehouseTwinLayoutEditError):
                _handle_twin_layout_edit_error(error)
            raise


@router.post("/twin-layout/floors/{floor_code}/draft/publish")
def publish_twin_layout_draft(
    floor_code: str,
    payload: TwinLayoutDraftPublishPayload,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    with WAREHOUSE_TWIN_LAYOUT_TRANSACTION_LOCK:
        return _publish_twin_layout_draft_locked(
            floor_code=floor_code,
            payload=payload,
            request=request,
            db=db,
            user=user,
        )


def _publish_twin_layout_draft_locked(
    *,
    floor_code: str,
    payload: TwinLayoutDraftPublishPayload,
    request: Request,
    db: Session,
    user: User,
    commit: bool = True,
    defer_location_readiness_for_feature_id: str | None = None,
    floor_projection_claimed: bool = False,
    allow_archived_tombstone_cleanup: bool = False,
) -> dict:
    if not floor_projection_claimed:
        _claim_floor_projection_for_layout_write(db, floor_code=floor_code)

    sanitized_revision, tombstone_projection = (
        _apply_archived_area_tombstones_before_validation(
            db,
            floor_code=floor_code,
            expected_revision=payload.expected_draft_revision,
        )
    )
    effective_draft_revision = payload.expected_draft_revision
    if tombstone_projection is not None and not allow_archived_tombstone_cleanup:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail=(
                "归档区域已从当前地图草稿剔除；请按新版本重新校验后发布。"
                f"新草稿版本：{sanitized_revision}"
            ),
        )
    if tombstone_projection is not None:
        cleanup_validation = validate_warehouse_twin_layout_draft(
            floor_code,
            expected_revision=sanitized_revision,
        )
        if cleanup_validation.value.get("blockers"):
            raise HTTPException(
                status_code=409,
                detail=(
                    "归档区域已清理，但当前区域仍未通过校验："
                    + "；".join(cleanup_validation.value["blockers"][:5])
                ),
            )
        effective_draft_revision = str(
            cleanup_validation.value.get("draft_revision") or sanitized_revision
        )

    mold_relocations = []
    mold_relocation_warnings: list[str] = []
    if floor_code.strip().upper() == "1F":
        mold_relocations = plan_mold_rack_layout_relocations(
            db,
            load_effective_warehouse_twin_floor_for_edit("1F"),
        )
        mold_relocation_warnings = mold_rack_layout_relocation_warnings(
            mold_relocations
        )
    blockers = _formal_area_publish_blockers(
        db,
        floor_code,
        defer_location_readiness_for_feature_id=(
            defer_location_readiness_for_feature_id
        ),
    )
    if blockers:
        raise HTTPException(
            status_code=409,
            detail="正式区域尚未完成：" + "；".join(blockers[:5]),
        )
    published_floor_before = load_published_warehouse_twin_floor_for_edit(floor_code)
    coordinate_draft = load_warehouse_twin_layout_draft(floor_code)
    publish_snapshot = snapshot_warehouse_twin_publish_state()
    result = None
    try:
        result = publish_warehouse_twin_layout_draft(
            floor_code,
            expected_published_revision=payload.expected_published_revision,
            expected_draft_revision=effective_draft_revision,
            operation_key=payload.operation_key,
            additional_warnings=mold_relocation_warnings,
            mold_location_reassignment_count=len(mold_relocations),
        )
        if result.applied:
            for relocation in mold_relocations:
                movement_key = "layout-publish:" + hashlib.sha256(
                    (
                        f"{payload.operation_key}|{relocation.mold_tool_id}|"
                        f"{relocation.to_location}"
                    ).encode("utf-8")
                ).hexdigest()[:48]
                confirm_mold_location_move(
                    db,
                    mold_code=relocation.mold_code,
                    target_location=relocation.to_location,
                    expected_version=relocation.expected_version,
                    idempotency_key=movement_key,
                    actor_id=user.id,
                    source="layout_publish",
                    note=f"地图发布自动归位：{relocation.reason}",
                )
        coordinate_adjustments = []
        if result.applied:
            from app.services.warehouse_location_geometry_draft import apply_adjustments
            coordinate_adjustments = apply_adjustments(db, floor_code=floor_code.upper(),
                draft=coordinate_draft, published=published_floor_before,
                new_revision=str(result.value.get("published_revision") or ""))
        published_policies = publish_floor_area_policies(
            db,
            floor_code=floor_code,
            published_revision=str(result.value.get("published_revision") or ""),
                operator_id=user.id,
                published_features=list(
                    load_warehouse_twin_floor(floor_code).get("features") or []
                ),
                defer_location_readiness_for_feature_id=(
                    defer_location_readiness_for_feature_id
                ),
                reconcile_rack_cell_feature_ids={
                    str(rack.get("area_feature_id") or "").strip()
                    for rack in coordinate_draft.get("racks") or []
                    if str(rack.get("id") or "").strip()
                    and str(rack.get("area_feature_id") or "").strip()
                },
            )
        rack_cell_sync = sync_published_rack_cells(
            db,
            floor_layout=load_warehouse_twin_floor(floor_code),
            operator_id=user.id,
            expected_legacy_binding_fingerprint=(
                payload.legacy_rack_binding_fingerprint
                if payload.legacy_rack_bindings_confirmed
                else None
            ),
            confirmed_legacy_bindings=(
                [item.model_dump() for item in payload.legacy_rack_bindings]
                if payload.legacy_rack_bindings_confirmed
                else None
            ),
        )
        rack_master_changed = any(
            (
                rack_cell_sync.created_location_ids,
                rack_cell_sync.enabled_location_ids,
                rack_cell_sync.disabled_location_ids,
                rack_cell_sync.updated_location_ids,
                rack_cell_sync.bound_legacy_location_ids,
            )
        )
        from app.services.warehouse_ground_map_application import previously_verified_area_features
        preserved_area_features = previously_verified_area_features(
            db, floor_layout=load_warehouse_twin_floor(floor_code),
            previous_floor_layout=published_floor_before,
        )
        _validate_published_area_layouts_for_floor(
            db,
            floor_code=floor_code,
            deferred_feature_id=defer_location_readiness_for_feature_id,
            allow_spatial_conflicts_for_feature_ids={
                str(item.get("feature_id") or "") for item in coordinate_adjustments
            } | preserved_area_features,
        )
        from app.services.warehouse_ground_map_application import record_map_applications
        ground_map_application_count = record_map_applications(
            db, floor_layout=load_warehouse_twin_floor(floor_code), actor=user,
            operation_key=payload.operation_key, request=request, previous_floor_layout=published_floor_before,
            coordinate_adjustments=coordinate_adjustments,
        )
        legacy_name_update_count = int(
            getattr(published_policies, "legacy_name_update_count", 0)
        )
        formal_master_changed = bool(
            published_policies or legacy_name_update_count or rack_master_changed
        )
        if result.applied or formal_master_changed or ground_map_application_count:
            _twin_layout_asset_log(
            db,
            request=request,
            user=user,
            action="TWIN_LAYOUT_PUBLISH",
            entity_type="twin_layout",
            entity_id=floor_code.upper(),
            description="管理员发布已校验的仓库地图草稿",
            details={
                **result.value,
                "ground_map_application_count": ground_map_application_count,
                "location_coordinate_adjustments": coordinate_adjustments,
                "formal_area_count": len(published_policies),
                "formal_areas": [
                    {
                        "area_code": policy.area.area_code,
                        "allowed_inventory_types": policy_inventory_types(policy),
                        "planned_location_count": policy.area.planned_location_count,
                    }
                    for policy in published_policies
                ],
                "legacy_area_name_update_count": legacy_name_update_count,
                "legacy_area_name_updated_codes": list(
                    getattr(
                        published_policies,
                        "legacy_name_updated_area_codes",
                        (),
                    )
                ),
                "location_master_changed": formal_master_changed,
                "rack_cell_created_location_ids": list(
                    rack_cell_sync.created_location_ids
                ),
                "rack_cell_enabled_location_ids": list(
                    rack_cell_sync.enabled_location_ids
                ),
                "rack_cell_disabled_location_ids": list(
                    rack_cell_sync.disabled_location_ids
                ),
                "rack_cell_updated_location_ids": list(
                    rack_cell_sync.updated_location_ids
                ),
                "rack_cell_bound_legacy_location_ids": list(
                    rack_cell_sync.bound_legacy_location_ids
                ),
                "inventory_changed": False,
            },
            )
            if commit:
                db.commit()
    except WarehouseTwinLayoutEditError as error:
        try:
            restore_warehouse_twin_publish_state(
                publish_snapshot,
                backup_name=(result.value.get("backup_name") if result is not None else None),
            )
        finally:
            db.rollback()
        _handle_twin_layout_edit_error(error)
    except (WarehouseAreaActivationError, WarehouseRackCellSyncError) as error:
        try:
            restore_warehouse_twin_publish_state(
                publish_snapshot,
                backup_name=(result.value.get("backup_name") if result is not None else None),
            )
        finally:
            db.rollback()
        raise HTTPException(status_code=error.status_code, detail=str(error)) from error
    except MoldLocationError as error:
        try:
            restore_warehouse_twin_publish_state(
                publish_snapshot,
                backup_name=(result.value.get("backup_name") if result is not None else None),
            )
        finally:
            db.rollback()
        raise HTTPException(status_code=error.status_code, detail=str(error)) from error
    except Exception:
        try:
            restore_warehouse_twin_publish_state(
                publish_snapshot,
                backup_name=(result.value.get("backup_name") if result is not None else None),
            )
        finally:
            db.rollback()
        raise
    return {
        **result.value,
        "applied": result.applied,
        "formal_area_count": len(published_policies),
        "legacy_area_name_update_count": int(
            getattr(published_policies, "legacy_name_update_count", 0)
        ),
        "bound_legacy_location_ids": list(
            rack_cell_sync.bound_legacy_location_ids
        ),
    }


def _rack_level_label_job_payload(
    row: WarehouseRackLevelLabelPrintJob,
    *,
    replayed: bool = False,
) -> dict:
    try:
        labels = json.loads(row.labels_json)
    except (TypeError, json.JSONDecodeError):
        labels = []
    return {
        "id": row.id,
        "floor_code": row.floor_code,
        "map_revision": row.map_revision,
        "area_id": row.area_id,
        "map_feature_id": row.map_feature_id,
        "map_rack_id": row.map_rack_id,
        "floor_name": row.floor_name_snapshot,
        "area_name": row.area_name_snapshot,
        "rack_name": row.rack_name_snapshot,
        "level_count": row.level_count,
        "labels": labels,
        "template_version": row.template_version,
        "source": row.source,
        "created_at": beijing_naive_to_api(row.created_at),
        "replayed": replayed,
    }


@router.post("/rack-level-labels/prints")
def register_rack_level_label_print(
    payload: RackLevelLabelPrintPayload,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    """Freeze one 80 x 40 mm label per configured rack level.

    Registration is intentionally independent from mold labels.  It records a
    wording snapshot only and never changes warehouse inventory.
    """

    with WAREHOUSE_TWIN_LAYOUT_TRANSACTION_LOCK:
        try:
            published = load_warehouse_twin_floor(payload.floor_code)
        except WarehouseTwinLayoutNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        map_revision = str(published.get("revision") or "").strip()
        if map_revision != payload.expected_map_revision:
            raise HTTPException(
                status_code=409,
                detail="正式地图版本已变化，请刷新后重新打印货架层标签。",
            )
        rack = next(
            (
                item
                for item in published.get("racks") or []
                if str(item.get("id") or "").strip() == payload.map_rack_id
            ),
            None,
        )
        if rack is None:
            raise HTTPException(status_code=404, detail="正式地图中找不到该货架")
        map_feature_id = str(rack.get("area_feature_id") or "").strip()
        rack_code = str(rack.get("rack_code") or "").strip()
        rack_name = str(rack.get("name") or rack_code or "").strip()
        levels = int(rack.get("levels") or 0)
        level_cell_counts = rack.get("level_cell_counts")
        if (
            not map_feature_id
            or not rack_code
            or not rack_name
            or levels <= 0
            or not isinstance(level_cell_counts, list)
            or len(level_cell_counts) != levels
            or any(int(value or 0) <= 0 for value in level_cell_counts)
        ):
            raise HTTPException(
                status_code=409,
                detail="该货架的区域、名称或逐层格数尚未配置完整，不能打印。",
            )
        area = db.scalar(
            select(WarehouseArea)
            .join(WarehouseFloor, WarehouseFloor.id == WarehouseArea.floor_id)
            .join(
                WarehouseAreaStoragePolicy,
                WarehouseAreaStoragePolicy.area_id == WarehouseArea.id,
            )
            .options(
                selectinload(WarehouseArea.floor),
                selectinload(WarehouseArea.storage_policy),
            )
            .where(
                func.upper(WarehouseFloor.floor_code) == payload.floor_code,
                WarehouseAreaStoragePolicy.map_feature_id == map_feature_id,
                WarehouseAreaStoragePolicy.status == "published",
                WarehouseAreaStoragePolicy.published_map_revision == map_revision,
            )
        )
        if area is None or area.storage_policy is None:
            raise HTTPException(
                status_code=409,
                detail="该货架所在区域尚未按当前地图版本正式发布。",
            )
        if location_warehouse_type_for_inventory_types(
            policy_inventory_types(area.storage_policy)
        ) is not None:
            expected_cell_count = sum(int(value) for value in level_cell_counts)
            actual_cell_count = int(
                db.scalar(
                    select(func.count(WarehouseLocation.id)).where(
                        WarehouseLocation.map_rack_id == payload.map_rack_id,
                        WarehouseLocation.address_area_id == area.id,
                        WarehouseLocation.warehouse_floor == area.floor.floor_number,
                        WarehouseLocation.is_active.is_(True),
                    )
                )
                or 0
            )
            if actual_cell_count != expected_cell_count:
                raise HTTPException(
                    status_code=409,
                    detail="正式货架层格尚未同步完整，请先重新发布地图。",
                )
        labels = [
            {
                "level_no": level_no,
                "floor_name": area.floor.floor_name,
                "area_name": area.area_name,
                "rack_name": rack_name,
                "display_text": f"{area.area_name}　{rack_name}　第{level_no}层",
            }
            for level_no in range(1, levels + 1)
        ]
        request_facts = {
            "floor_code": payload.floor_code,
            "map_rack_id": payload.map_rack_id,
            "map_revision": map_revision,
            "template_version": payload.template_version,
            "source": payload.source,
        }
        request_hash = hashlib.sha256(
            json.dumps(
                request_facts,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        existing = db.scalar(
            select(WarehouseRackLevelLabelPrintJob).where(
                WarehouseRackLevelLabelPrintJob.idempotency_key
                == payload.idempotency_key
            )
        )
        if existing is not None:
            if existing.request_hash != request_hash or existing.created_by != user.id:
                raise HTTPException(
                    status_code=409,
                    detail="该打印请求号已被不同内容使用，请刷新后重试。",
                )
            return _rack_level_label_job_payload(existing, replayed=True)
        row = WarehouseRackLevelLabelPrintJob(
            floor_code=payload.floor_code,
            map_revision=map_revision,
            area_id=area.id,
            map_feature_id=map_feature_id,
            map_rack_id=payload.map_rack_id,
            map_rack_code=rack_code,
            floor_name_snapshot=area.floor.floor_name,
            area_name_snapshot=area.area_name,
            rack_name_snapshot=rack_name,
            level_count=levels,
            labels_json=json.dumps(labels, ensure_ascii=False, separators=(",", ":")),
            template_version=payload.template_version,
            source=payload.source,
            idempotency_key=payload.idempotency_key,
            request_hash=request_hash,
            created_by=user.id,
        )
        db.add(row)
        db.flush()
        append_audit_event(
            db,
            request=request,
            actor=user,
            event_category="business",
            result="success",
            source="web",
            module_code="warehouse",
            action_code="warehouse.rack_level_labels.print",
            legacy_action="RACK_LEVEL_LABEL_PRINT",
            resource=f"warehouse/rack-level-labels/prints/{row.id}",
            entity_type="warehouse_rack_level_label_print_job",
            entity_id=row.id,
            object_ref=f"{payload.floor_code}:{payload.map_rack_id}",
            description="登记货架层标签打印快照；未改变库存事实",
            details={
                "map_revision": map_revision,
                "area_id": area.id,
                "level_count": levels,
                "template_version": payload.template_version,
                "inventory_changed": False,
            },
        )
        try:
            db.commit()
        except IntegrityError as error:
            db.rollback()
            raise HTTPException(
                status_code=409,
                detail="货架层标签打印请求已登记，请刷新后查看。",
            ) from error
        db.refresh(row)
        return _rack_level_label_job_payload(row)


@router.get("/rack-level-labels/prints/{print_job_id}")
def get_rack_level_label_print(
    print_job_id: int,
    db: Session = Depends(get_db),
    _user: User = Depends(admin_only),
) -> dict:
    row = db.get(WarehouseRackLevelLabelPrintJob, print_job_id)
    if row is None:
        raise HTTPException(status_code=404, detail="货架层标签打印记录不存在")
    return _rack_level_label_job_payload(row)


@router.post("/twin-layout/floors/{floor_code}/draft/discard")
def discard_twin_layout_draft(
    floor_code: str,
    payload: TwinLayoutDraftDiscardPayload,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    with WAREHOUSE_TWIN_LAYOUT_TRANSACTION_LOCK:
        _claim_floor_projection_for_layout_write(db, floor_code=floor_code)
        draft_snapshot = snapshot_warehouse_twin_layout_draft()
        result = None
        tombstone_projection = None
        sanitized_revision = ""
        try:
            result = discard_warehouse_twin_layout_draft(
                floor_code,
                expected_revision=payload.expected_revision,
            )
            effective_floor = load_effective_warehouse_twin_floor_for_edit(floor_code)
            sanitized_revision, tombstone_projection = (
                _apply_archived_area_tombstones_before_validation(
                    db,
                    floor_code=floor_code,
                    expected_revision=str(effective_floor.get("revision") or ""),
                )
            )
            if result.applied or tombstone_projection is not None:
                _twin_layout_asset_log(
                    db,
                    request=request,
                    user=user,
                    action="TWIN_LAYOUT_DRAFT_DISCARD",
                    entity_type="twin_layout_draft",
                    entity_id=floor_code.upper(),
                    description="管理员放弃仓库地图草稿",
                    details={
                        **result.value,
                        "tombstone_projection": tombstone_projection,
                        "revision": sanitized_revision,
                    },
                )
                db.commit()
        except WarehouseTwinLayoutEditError as error:
            db.rollback()
            if (result is not None and result.applied) or tombstone_projection is not None:
                restore_warehouse_twin_layout_draft(draft_snapshot)
            _handle_twin_layout_edit_error(error)
        except Exception:
            db.rollback()
            if (result is not None and result.applied) or tombstone_projection is not None:
                restore_warehouse_twin_layout_draft(draft_snapshot)
            raise
        return {
            **result.value,
            "revision": sanitized_revision,
            "tombstone_projection": tombstone_projection,
            "applied": result.applied or tombstone_projection is not None,
        }


@router.post("/twin-layout/floors/{floor_code}/draft/rebuild-stale")
def rebuild_stale_twin_layout_draft(
    floor_code: str,
    payload: TwinLayoutDraftRebuildPayload,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    """Explicitly abandon an obsolete draft and seed one from the live map."""

    with WAREHOUSE_TWIN_LAYOUT_TRANSACTION_LOCK:
        _claim_floor_projection_for_layout_write(db, floor_code=floor_code)
        draft_snapshot = snapshot_warehouse_twin_layout_draft()
        result = None
        tombstone_projection = None
        sanitized_revision = ""
        try:
            result = rebuild_stale_warehouse_twin_layout_draft(
                floor_code,
                expected_published_revision=payload.expected_published_revision,
            )
            effective_floor = load_effective_warehouse_twin_floor_for_edit(floor_code)
            sanitized_revision, tombstone_projection = (
                _apply_archived_area_tombstones_before_validation(
                    db,
                    floor_code=floor_code,
                    expected_revision=str(effective_floor.get("revision") or ""),
                )
            )
            if result.applied or tombstone_projection is not None:
                _twin_layout_asset_log(
                    db,
                    request=request,
                    user=user,
                    action="TWIN_LAYOUT_STALE_DRAFT_REBUILD",
                    entity_type="twin_layout_draft",
                    entity_id=floor_code.upper(),
                    description="管理员放弃过期仓库地图草稿并从当前正式地图重建",
                    details={
                        **result.value,
                        "operation_key": payload.operation_key,
                        "tombstone_projection": tombstone_projection,
                        "revision": sanitized_revision,
                    },
                )
                db.commit()
        except WarehouseTwinLayoutEditError as error:
            db.rollback()
            if (result is not None and result.applied) or tombstone_projection is not None:
                restore_warehouse_twin_layout_draft(draft_snapshot)
            _handle_twin_layout_edit_error(error)
        except Exception:
            db.rollback()
            if (result is not None and result.applied) or tombstone_projection is not None:
                restore_warehouse_twin_layout_draft(draft_snapshot)
            raise
        return {
            **result.value,
            "revision": sanitized_revision,
            "tombstone_projection": tombstone_projection,
            "applied": result.applied or tombstone_projection is not None,
        }


@router.post("/twin-layout/floors/{floor_code}/features", status_code=201)
def create_twin_layout_feature(
    floor_code: str,
    payload: TwinLayoutFeatureCreatePayload,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    with WAREHOUSE_TWIN_LAYOUT_TRANSACTION_LOCK:
        _claim_floor_projection_for_layout_write(db, floor_code=floor_code)
        draft_snapshot = snapshot_warehouse_twin_layout_draft()
        mutation = None
        try:
            mutation = create_warehouse_twin_feature(
                floor_code,
                expected_revision=payload.expected_revision,
                operation_key=payload.operation_key,
                feature_kind=payload.feature_kind,
                points=[list(point) for point in payload.points],
                width_mm=payload.width_mm,
                direction=payload.direction,
            )
            if mutation.applied:
                _twin_layout_asset_log(
                    db,
                    request=request,
                    user=user,
                    action="TWIN_LAYOUT_FEATURE_CREATE",
                    entity_type="twin_layout_feature",
                    entity_id=str(mutation.value.get("id") or ""),
                    description="二维仓库规划新增区域或通道",
                    details={
                        "floor_code": floor_code,
                        "feature": mutation.value,
                        "inventory_changed": False,
                        "published_map_changed": False,
                    },
                )
                db.commit()
        except WarehouseTwinLayoutEditError as error:
            db.rollback()
            if mutation is not None and mutation.applied:
                restore_warehouse_twin_layout_draft(draft_snapshot)
            _handle_twin_layout_edit_error(error)
        except Exception:
            db.rollback()
            if mutation is not None and mutation.applied:
                restore_warehouse_twin_layout_draft(draft_snapshot)
            raise
        return {
            "item": mutation.value,
            "revision": mutation.floor_revision,
            "applied": mutation.applied,
        }


@router.patch("/twin-layout/floors/{floor_code}/features/{feature_id}/geometry")
def update_twin_layout_feature_geometry(
    floor_code: str,
    feature_id: str,
    payload: TwinLayoutFeatureGeometryPayload,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    with WAREHOUSE_TWIN_LAYOUT_TRANSACTION_LOCK:
        _claim_floor_projection_for_layout_write(db, floor_code=floor_code)
        _assert_twin_feature_not_archived(
            db,
            floor_code=floor_code,
            feature_id=feature_id,
        )
        draft_snapshot = snapshot_warehouse_twin_layout_draft()
        mutation = None
        try:
            from app.services.warehouse_location_geometry_draft import prepare_adjustment, adjust_slot_positions
            published_geometry = load_published_warehouse_twin_floor_for_edit(floor_code)

            def prepare_ground(feature):
                adjustment = prepare_adjustment(db, floor_code=floor_code.upper(),
                    feature=feature, published=published_geometry)
                if payload.ground_locations is not None:
                    if adjustment is None:
                        raise WarehouseTwinLayoutEditConflictError("区域没有可调整的正式地堆货位")
                    adjustment = adjust_slot_positions(adjustment,
                        [point.model_dump() for point in payload.ground_locations],
                        floor_bounds=published_geometry['bounds_mm'])
                return adjustment

            mutation = update_warehouse_twin_feature_geometry(
                floor_code,
                feature_id,
                expected_revision=payload.expected_revision,
                expected_version=payload.expected_version,
                operation_key=payload.operation_key,
                points=[list(point) for point in payload.points],
                prepare_ground=prepare_ground,
                request_hash=ground_canonical_hash(payload.model_dump(mode="json")),
            )
            if mutation.applied:
                _twin_layout_asset_log(
                    db,
                    request=request,
                    user=user,
                    action="TWIN_LAYOUT_FEATURE_GEOMETRY_UPDATE",
                    entity_type="twin_layout_feature",
                    entity_id=feature_id,
                    description="二维仓库规划调整区域或通道位置",
                    details={
                        "floor_code": floor_code,
                        "feature": mutation.value,
                        "inventory_changed": False,
                        "published_map_changed": False,
                    },
                )
                db.commit()
        except WarehouseTwinLayoutEditError as error:
            db.rollback()
            if mutation is not None and mutation.applied:
                restore_warehouse_twin_layout_draft(draft_snapshot)
            _handle_twin_layout_edit_error(error)
        except Exception:
            db.rollback()
            if mutation is not None and mutation.applied:
                restore_warehouse_twin_layout_draft(draft_snapshot)
            raise
        return {
            "item": mutation.value,
            "revision": mutation.floor_revision,
            "applied": mutation.applied,
        }


def _archived_area_tombstones_for_floor(
    db: Session,
    *,
    floor_code: str,
) -> list[dict]:
    return archived_area_tombstones_for_floor(db, floor_code=floor_code)


def _assert_twin_feature_not_archived(
    db: Session,
    *,
    floor_code: str,
    feature_id: str,
    area_code: str | None = None,
) -> None:
    identities = archived_area_identity_sets(db, floor_code=floor_code)
    normalized_feature_id = str(feature_id or "").strip()
    normalized_area_code = str(area_code or "").strip().upper()
    if not normalized_area_code and normalized_feature_id:
        try:
            floor_layout = load_effective_warehouse_twin_floor_for_edit(floor_code)
        except WarehouseTwinLayoutEditError as error:
            _handle_twin_layout_edit_error(error)
        feature = next(
            (
                item
                for item in floor_layout.get("features") or []
                if str(item.get("id") or "").strip() == normalized_feature_id
            ),
            None,
        )
        if feature is not None:
            normalized_area_code = str(
                feature.get("erp_area_code") or ""
            ).strip().upper()
    if (
        normalized_feature_id in identities["feature_ids"]
        or normalized_feature_id.upper() in identities["feature_codes"]
        or normalized_area_code in identities["area_codes"]
        or normalized_area_code in identities["feature_codes"]
    ):
        raise HTTPException(
            status_code=409,
            detail="该区域已经归档，普通地图编辑不能恢复或修改。",
        )


def _assert_twin_rack_not_archived(
    db: Session,
    *,
    floor_code: str,
    rack_id: str,
) -> None:
    identities = archived_area_identity_sets(db, floor_code=floor_code)
    if str(rack_id or "").strip() in identities["rack_ids"]:
        raise HTTPException(
            status_code=409,
            detail="该区域已经归档，普通地图编辑不能恢复或修改。",
        )
    try:
        floor_layout = load_effective_warehouse_twin_floor_for_edit(floor_code)
    except WarehouseTwinLayoutEditError as error:
        _handle_twin_layout_edit_error(error)
    rack = next(
        (
            item
            for item in floor_layout.get("racks") or []
            if str(item.get("id") or "").strip() == str(rack_id).strip()
        ),
        None,
    )
    if rack is None:
        return
    feature_id = str(rack.get("area_feature_id") or "").strip()
    area_code = str(rack.get("area_code") or "").strip().upper()
    if not feature_id and area_code:
        matching_features = [
            item
            for item in floor_layout.get("features") or []
            if item.get("feature_kind") == "zone"
            and str(item.get("erp_area_code") or "").strip().upper() == area_code
        ]
        if len(matching_features) == 1:
            feature_id = str(matching_features[0].get("id") or "").strip()
    _assert_twin_feature_not_archived(
        db,
        floor_code=floor_code,
        feature_id=feature_id,
        area_code=area_code,
    )


def _apply_archived_area_tombstones_before_validation(
    db: Session,
    *,
    floor_code: str,
    expected_revision: str,
) -> tuple[str, dict | None]:
    tombstones = _archived_area_tombstones_for_floor(db, floor_code=floor_code)
    if not tombstones:
        return expected_revision, None
    floor_layout = load_effective_warehouse_twin_floor_for_edit(floor_code)
    actual_revision = str(floor_layout.get("revision") or "")
    if actual_revision != str(expected_revision or ""):
        raise HTTPException(
            status_code=409,
            detail="布局草稿已更新，请刷新后重新校验",
        )
    partition = partition_archived_area_layout(
        floor_layout,
        tombstones=tombstones,
    )
    if not any(
        partition[key]
        for key in ("removed_features", "removed_racks", "removed_pallets")
    ):
        return expected_revision, None
    identity = json.dumps(
        sorted(
            (
                str(item.get("feature_id") or ""),
                str(item.get("area_code") or "").upper(),
            )
            for item in tombstones
        ),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    operation_key = "archive-tombstone-" + hashlib.sha256(
        f"{floor_code.upper()}|{actual_revision}|{identity}".encode("utf-8")
    ).hexdigest()[:48]
    mutation = apply_warehouse_twin_archived_area_tombstones(
        floor_code,
        expected_revision=actual_revision,
        operation_key=operation_key,
        tombstones=tombstones,
    )
    return mutation.floor_revision, mutation.value


def _formal_area_archive_location_rows(
    db: Session,
    *,
    area: WarehouseArea,
    owned_map_rack_ids: set[str],
) -> tuple[list[WarehouseLocation], list[str]]:
    floor_number = int(area.floor.floor_number)
    area_code = area.area_code.strip().upper()
    rows = list(
        db.scalars(
            select(WarehouseLocation)
            .where(
                or_(
                    WarehouseLocation.address_area_id == area.id,
                    and_(
                        WarehouseLocation.address_area_id.is_(None),
                        WarehouseLocation.warehouse_floor == floor_number,
                        func.upper(WarehouseLocation.area_code) == area_code,
                    ),
                )
            )
            .options(selectinload(WarehouseLocation.floor3_layout))
            .order_by(WarehouseLocation.id)
        ).all()
    )
    structured_drift = sum(
        1
        for row in rows
        if row.address_area_id == area.id
        and (
            row.warehouse_floor != floor_number
            or str(row.area_code or "").strip().upper() != area_code
        )
    )
    foreign_identity_drift = int(
        db.scalar(
            select(func.count(WarehouseLocation.id)).where(
                WarehouseLocation.address_area_id.is_not(None),
                WarehouseLocation.address_area_id != area.id,
                WarehouseLocation.warehouse_floor == floor_number,
                func.upper(WarehouseLocation.area_code) == area_code,
            )
        )
        or 0
    )
    blockers: list[str] = []
    if structured_drift:
        blockers.append(f"仍有正式地址归属与楼层/区域编码不一致的库位 {structured_drift} 个")
    if foreign_identity_drift:
        blockers.append(f"仍有同编码但归属其他正式区域的库位 {foreign_identity_drift} 个")
    rack_identity_drift = sum(
        1
        for row in rows
        if str(row.map_rack_id or "").strip()
        and str(row.map_rack_id or "").strip() not in owned_map_rack_ids
    )
    if rack_identity_drift:
        blockers.append(f"仍有库位绑定其他区域地图货架 {rack_identity_drift} 个")
    canonical_location_ids = {int(row.id) for row in rows}
    foreign_rack_location_count = (
        int(
            db.scalar(
                select(func.count(WarehouseLocation.id)).where(
                    WarehouseLocation.map_rack_id.in_(owned_map_rack_ids),
                    WarehouseLocation.id.not_in(canonical_location_ids),
                )
            )
            or 0
        )
        if owned_map_rack_ids
        else 0
    )
    if foreign_rack_location_count:
        blockers.append(
            f"仍有其他区域库位绑定本区地图货架 {foreign_rack_location_count} 个"
        )
    return rows, blockers


def _formal_area_archive_blockers(
    db: Session,
    *,
    floor_layout: dict,
    feature: dict,
    area: WarehouseArea,
    locations: list[WarehouseLocation],
) -> list[str]:
    if area.area_code.upper() == "DISPATCH" or str(feature.get("id") or "").upper() == "F1-DISPATCH-01":
        return ["一楼直接待送业务锚点不能作为普通空区域归档"]
    location_ids = [int(row.id) for row in locations]
    blockers: list[str] = []
    try:
        customer_default_count = int(
            db.scalar(
                select(func.count())
                .select_from(CustomerFinishedStoragePreference)
                .where(
                    CustomerFinishedStoragePreference.warehouse_area_id
                    == int(area.id)
                )
            )
            or 0
        )
    except SQLAlchemyError:
        blockers.append("客户默认成品区域引用暂无法核对")
    else:
        if customer_default_count:
            blockers.append(f"仍有客户默认成品区域配置 {customer_default_count} 条")
    if location_ids:
        lot_count, lot_quantity = db.execute(
            select(
                func.count(InventoryLot.id),
                func.coalesce(
                    func.sum(
                        InventoryLot.quantity_available
                        + InventoryLot.quantity_reserved
                        + InventoryLot.quantity_damaged
                    ),
                    0,
                ),
            ).where(
                InventoryLot.warehouse_location_id.in_(location_ids),
                (
                    InventoryLot.quantity_available
                    + InventoryLot.quantity_reserved
                    + InventoryLot.quantity_damaged
                )
                > 0,
            )
        ).one()
        if int(lot_count or 0):
            blockers.append(
                f"仍有库存 {int(lot_count)} 批，共 {int(lot_quantity or 0)} 个物理单位"
            )
        reservation_count = int(
            db.scalar(
                select(func.count(InventoryReservation.id))
                .join(
                    InventoryLot,
                    InventoryLot.id == InventoryReservation.inventory_lot_id,
                )
                .where(
                    InventoryLot.warehouse_location_id.in_(location_ids),
                    InventoryReservation.status.in_(("active", "partial")),
                    InventoryReservation.reserved_stock_quantity
                    > InventoryReservation.consumed_stock_quantity
                    + InventoryReservation.released_stock_quantity,
                )
            )
            or 0
        )
        if reservation_count:
            blockers.append(f"仍有未完成库存预占 {reservation_count} 条")
        receipt_return_count = int(
            db.scalar(
                select(func.count(OrderedFinishedReceiptReturn.id))
                .outerjoin(
                    InventoryLot,
                    InventoryLot.id
                    == OrderedFinishedReceiptReturn.return_inventory_lot_id,
                )
                .where(
                    OrderedFinishedReceiptReturn.return_location_id.in_(location_ids),
                    OrderedFinishedReceiptReturn.status == "active",
                    # A return fact keeps its original receiving location for
                    # audit.  It must not keep an otherwise empty area alive
                    # after its return lot has been formally transferred away.
                    # A missing return lot remains a fail-closed blocker.
                    or_(
                        InventoryLot.id.is_(None),
                        InventoryLot.warehouse_location_id.in_(location_ids),
                    ),
                )
            )
            or 0
        )
        if receipt_return_count:
            blockers.append(f"仍有活动订单成品退回记录 {receipt_return_count} 条")
        pallet_count = int(
            db.scalar(
                select(func.count(InventoryPallet.id)).where(
                    InventoryPallet.location_id.in_(location_ids),
                    InventoryPallet.is_current.is_(True),
                )
            )
            or 0
        )
        if pallet_count:
            blockers.append(f"仍有活动栈板 {pallet_count} 块")
        occupancy_count = int(
            db.scalar(
                select(func.count(func.distinct(WarehouseGroundOccupancy.id)))
                .outerjoin(
                    WarehouseGroundOccupancySlot,
                    WarehouseGroundOccupancySlot.occupancy_id
                    == WarehouseGroundOccupancy.id,
                )
                .where(
                    WarehouseGroundOccupancy.status == "active",
                    or_(
                        WarehouseGroundOccupancy.primary_location_id.in_(location_ids),
                        and_(
                            WarehouseGroundOccupancySlot.location_id.in_(location_ids),
                            WarehouseGroundOccupancySlot.status == "active",
                        ),
                    ),
                )
            )
            or 0
        )
        if occupancy_count:
            blockers.append(f"仍有活动地堆占用 {occupancy_count} 条")
        stocktake_count = int(
            db.scalar(
                select(func.count(StocktakeOrder.id)).where(
                    StocktakeOrder.location_id.in_(location_ids),
                    StocktakeOrder.status.in_(("draft", "submitted")),
                )
            )
            or 0
        )
        if stocktake_count:
            blockers.append(f"仍有未完成盘点 {stocktake_count} 单")
        onboarding_count = int(
            db.scalar(
                select(func.count(InventoryOnboardingLine.id))
                .join(
                    InventoryOnboardingBatch,
                    InventoryOnboardingBatch.id == InventoryOnboardingLine.batch_id,
                )
                .where(
                    InventoryOnboardingLine.location_id.in_(location_ids),
                    InventoryOnboardingBatch.status.in_(("draft", "submitted")),
                )
            )
            or 0
        )
        if onboarding_count:
            blockers.append(f"仍有待处理库存补录 {onboarding_count} 条")
        default_policy_count = int(
            db.scalar(
                select(func.count(InventoryStockPolicy.id)).where(
                    InventoryStockPolicy.default_location_id.in_(location_ids),
                    InventoryStockPolicy.active.is_(True),
                )
            )
            or 0
        )
        if default_policy_count:
            blockers.append(f"仍有库存预警/补库默认位置 {default_policy_count} 条")
        replenishment_count = int(
            db.scalar(
                select(func.count(StockReplenishmentOrderItem.id))
                .join(
                    StockReplenishmentOrder,
                    StockReplenishmentOrder.id
                    == StockReplenishmentOrderItem.replenishment_order_id,
                )
                .where(
                    StockReplenishmentOrderItem.location_id.in_(location_ids),
                    StockReplenishmentOrder.status.in_(
                        ("draft", "confirmed", "partially_stocked")
                    ),
                )
            )
            or 0
        )
        if replenishment_count:
            blockers.append(f"仍有活动补库明细 {replenishment_count} 条")
        unmatched_count = int(
            db.scalar(
                select(func.count(WarehouseUnmatchedInventoryObservation.id)).where(
                    WarehouseUnmatchedInventoryObservation.observed_location_id.in_(
                        location_ids
                    ),
                    WarehouseUnmatchedInventoryObservation.status == "open",
                )
            )
            or 0
        )
        if unmatched_count:
            blockers.append(f"仍有现场实物待核对标记 {unmatched_count} 条")
        discrepancy_count = int(
            db.scalar(
                select(func.count(WarehouseLocationDiscrepancy.id)).where(
                    WarehouseLocationDiscrepancy.status == "open",
                    or_(
                        WarehouseLocationDiscrepancy.registered_location_id.in_(
                            location_ids
                        ),
                        WarehouseLocationDiscrepancy.observed_location_id.in_(
                            location_ids
                        ),
                    ),
                )
            )
            or 0
        )
        if discrepancy_count:
            blockers.append(f"仍有未处理库位差异 {discrepancy_count} 条")
    feature_id = str(feature.get("id") or "").strip()
    feature_code = str(feature.get("feature_code") or "").strip().upper()
    area_code = area.area_code.strip().upper()
    mapped_pallet_count = sum(
        1
        for pallet in floor_layout.get("pallets") or []
        if str(pallet.get("zone_id") or "").strip() == feature_id
        or str(pallet.get("zone_code") or "").strip().upper()
        in {feature_code, area_code} - {""}
    )
    if mapped_pallet_count:
        blockers.append(f"地图区域仍有栈板对象 {mapped_pallet_count} 块")
    asset_blockers = _zone_asset_and_production_blockers(
        db,
        floor_layout=floor_layout,
        feature=feature,
        area_code=area.area_code.upper(),
        fail_closed_on_mapping_error=True,
    )
    blockers.extend(
        message.replace("不能改变区域策略", "不能删除区域")
        for message in asset_blockers
    )
    return list(dict.fromkeys(blockers))



def _formal_rack_archive_location_rows(
    db: Session,
    *,
    floor_code: str,
    rack_id: str,
) -> tuple[list[WarehouseLocation], list[str]]:
    """Resolve exactly the active formal slots owned by one published rack."""

    try:
        floor_number = int(str(floor_code).strip().upper().removesuffix("F"))
    except ValueError as error:
        raise HTTPException(status_code=404, detail="数字孪生楼层不存在") from error
    rows = list(db.scalars(
        select(WarehouseLocation).where(
            WarehouseLocation.map_rack_id == rack_id,
            WarehouseLocation.is_active.is_(True),
        ).order_by(WarehouseLocation.id)
    ).all())
    blockers: list[str] = []
    if any(int(row.warehouse_floor or 0) != floor_number for row in rows):
        blockers.append("存在绑定该地图货架但楼层身份不一致的正式库位")
    return rows, blockers


def _formal_rack_archive_blockers(
    db: Session,
    *,
    locations: list[WarehouseLocation],
    rack_id: str,
    rack_code: str | None,
    rack_name: str | None = None,
) -> list[str]:
    """Fail closed on every live fact that would make an empty-rack delete unsafe."""

    location_ids = [int(row.id) for row in locations]
    blockers: list[str] = []
    lot_count, lot_quantity = db.execute(
        select(
            func.count(InventoryLot.id),
            func.coalesce(func.sum(
                InventoryLot.quantity_available
                + InventoryLot.quantity_reserved
                + InventoryLot.quantity_damaged
            ), 0),
        ).where(
            InventoryLot.warehouse_location_id.in_(location_ids),
            (InventoryLot.quantity_available + InventoryLot.quantity_reserved + InventoryLot.quantity_damaged) > 0,
        )
    ).one()
    if int(lot_count or 0):
        blockers.append(f"仍有库存 {int(lot_count)} 批，共 {int(lot_quantity or 0)} 个物理单位")
    checks = (
        (select(func.count(InventoryReservation.id)).join(
            InventoryLot, InventoryLot.id == InventoryReservation.inventory_lot_id
        ).where(
            InventoryLot.warehouse_location_id.in_(location_ids),
            InventoryReservation.status.in_(("active", "partial")),
            InventoryReservation.reserved_stock_quantity > InventoryReservation.consumed_stock_quantity + InventoryReservation.released_stock_quantity,
        ), "仍有未完成库存预占 {} 条"),
        (select(func.count(InventoryPallet.id)).where(
            InventoryPallet.location_id.in_(location_ids), InventoryPallet.is_current.is_(True),
        ), "仍有活动栈板 {} 块"),
        (select(func.count(InventoryStockPolicy.id)).where(
            InventoryStockPolicy.default_location_id.in_(location_ids), InventoryStockPolicy.active.is_(True),
        ), "仍有库存预警/补库默认位置 {} 条"),
        (select(func.count(ProductStoragePreference.product_id)).where(
            ProductStoragePreference.location_id.in_(location_ids),
        ), "仍有产品默认入库位置 {} 条"),
        (select(func.count(StocktakeOrder.id)).where(
            StocktakeOrder.location_id.in_(location_ids), StocktakeOrder.status.in_(("draft", "submitted")),
        ), "仍有未完成盘点 {} 单"),
        (select(func.count(InventoryOnboardingLine.id)).join(
            InventoryOnboardingBatch, InventoryOnboardingBatch.id == InventoryOnboardingLine.batch_id
        ).where(
            InventoryOnboardingLine.location_id.in_(location_ids), InventoryOnboardingBatch.status.in_(("draft", "submitted")),
        ), "仍有待处理库存补录 {} 条"),
        (select(func.count(StockReplenishmentOrderItem.id)).join(
            StockReplenishmentOrder, StockReplenishmentOrder.id == StockReplenishmentOrderItem.replenishment_order_id
        ).where(
            StockReplenishmentOrderItem.location_id.in_(location_ids),
            StockReplenishmentOrder.status.in_(("draft", "confirmed", "partially_stocked")),
        ), "仍有活动补库明细 {} 条"),
        (select(func.count(WarehouseUnmatchedInventoryObservation.id)).where(
            WarehouseUnmatchedInventoryObservation.observed_location_id.in_(location_ids),
            WarehouseUnmatchedInventoryObservation.status == "open",
        ), "仍有现场实物待核对标记 {} 条"),
        (select(func.count(WarehouseLocationDiscrepancy.id)).where(
            WarehouseLocationDiscrepancy.status == "open",
            or_(WarehouseLocationDiscrepancy.registered_location_id.in_(location_ids), WarehouseLocationDiscrepancy.observed_location_id.in_(location_ids)),
        ), "仍有未处理库位差异 {} 条"),
    )
    for statement, wording in checks:
        count = int(db.scalar(statement) or 0)
        if count:
            blockers.append(wording.format(count))
    # rack_code is a stable internal map key, while physical mold/plate
    # locations normally use the displayed rack name (for example F7-1).
    text_markers = {str(rack_id).strip().upper(), str(rack_code or "").strip().upper(), str(rack_name or "").strip().upper()} - {""}
    def references_rack(location_text: str | None) -> bool:
        text = str(location_text or "").strip().upper()
        return any(re.search(r"(?<![A-Z0-9])" + re.escape(marker) + r"(?![A-Z0-9])", text) for marker in text_markers)
    for mold in db.scalars(select(MoldTool).where(or_(MoldTool.is_active.is_(True), MoldTool.archive_status == "archived"))).all():
        if references_rack(mold.rack_location):
            blockers.append("仍有实体模具引用该货架，不能删除")
            break
    for plate in db.scalars(select(PrintingPlate).where(PrintingPlate.status.in_(("active", "damaged")))).all():
        if references_rack(plate.rack_location):
            blockers.append("仍有启用或受损挂板引用该货架，不能删除")
            break
    return list(dict.fromkeys(blockers))

def _formal_area_archive_replay_response(
    policy: WarehouseAreaStoragePolicy,
    *,
    floor_code: str,
    feature_id: str,
) -> dict:
    try:
        snapshot = json.loads(policy.archive_feature_snapshot_json or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        raise HTTPException(
            status_code=409,
            detail="区域归档快照损坏，已停止回放，请先完成受控修复。",
        )
    stored_result = snapshot.get("result") if isinstance(snapshot, dict) else None
    if not isinstance(stored_result, dict) or not isinstance(
        stored_result.get("item"), dict
    ) or not str(stored_result.get("revision") or "").strip():
        raise HTTPException(
            status_code=409,
            detail="区域归档回放结果不完整，已停止回放，请先完成受控修复。",
        )
    stored_item = dict(stored_result["item"])
    return {
        "item": {
            **stored_item,
            "id": feature_id,
            "feature_kind": "zone",
            "deleted": True,
            "archived": True,
            "formal_area_id": policy.area_id,
            "inactive_location_count": int(
                stored_item.get("inactive_location_count") or 0
            ),
        },
        "revision": (
            str(stored_result.get("revision") or "")
            if isinstance(stored_result, dict)
            else ""
        ),
        "applied": False,
        "idempotent_replay": True,
    }


def _pure_draft_feature_delete_blocker(
    db: Session,
    *,
    floor_code: str,
    feature_id: str,
) -> str | None:
    """Keep legacy formal identities out of the unbound draft-delete path."""

    normalized_floor = floor_code.strip().upper()
    effective_floor = load_effective_warehouse_twin_floor_for_edit(normalized_floor)
    feature = next(
        (
            item
            for item in effective_floor.get("features") or []
            if str(item.get("id") or "") == feature_id
        ),
        None,
    )
    if feature is None or feature.get("feature_kind") != "zone":
        return None
    if (
        feature.get("formal_area_id") not in (None, "")
        or feature.get("formal_floor_id") not in (None, "")
    ):
        return "该区域已有正式身份，不能作为纯规划草稿删除，请先治理正式绑定。"
    floor = warehouse_floor_for_code(db, normalized_floor)
    area_code = str(feature.get("erp_area_code") or "").strip().upper()
    if floor is None or not area_code:
        return None
    formal_area = db.scalar(
        select(WarehouseArea.id).where(
            WarehouseArea.floor_id == floor.id,
            func.upper(WarehouseArea.area_code) == area_code,
        )
    )
    formal_location = db.scalar(
        select(WarehouseLocation.id)
        .where(
            WarehouseLocation.warehouse_floor == floor.floor_number,
            func.upper(WarehouseLocation.area_code) == area_code,
        )
        .limit(1)
    )
    if formal_area is not None or formal_location is not None:
        return "该旧版实测区域已有正式区域或货位台账，不能作为纯规划草稿删除，请先治理正式绑定。"
    return None


@router.delete("/twin-layout/floors/{floor_code}/features/{feature_id}")
def delete_twin_layout_feature(
    floor_code: str,
    feature_id: str,
    expected_revision: str = Query(min_length=1, max_length=64),
    expected_version: int = Query(ge=1),
    operation_key: str = Query(min_length=8, max_length=120),
    expected_policy_version: int | None = Query(default=None, ge=1),
    expected_published_revision: str | None = Query(
        default=None, min_length=1, max_length=64
    ),
    retire_ground_plan: bool = Query(default=False),
    expected_ground_plan_version: int | None = Query(default=None, ge=1),
    request: Request = None,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    # Direct service-level tests and internal callers bypass FastAPI's query
    # parsing, so optional Query defaults may arrive as parameter descriptors.
    # Normalize those descriptors without weakening real HTTP validation.
    if not isinstance(expected_policy_version, int):
        expected_policy_version = None
    if not isinstance(expected_published_revision, str):
        expected_published_revision = None
    if not isinstance(retire_ground_plan, bool):
        retire_ground_plan = False
    if not isinstance(expected_ground_plan_version, int):
        expected_ground_plan_version = None
    with WAREHOUSE_TWIN_LAYOUT_TRANSACTION_LOCK:
        normalized_floor = floor_code.strip().upper()
        request_facts = {
            "floor_code": normalized_floor,
            "feature_id": feature_id,
            "expected_revision": expected_revision,
            "expected_version": expected_version,
            "expected_policy_version": expected_policy_version,
            "expected_published_revision": expected_published_revision,
            "retire_ground_plan": retire_ground_plan,
            "expected_ground_plan_version": expected_ground_plan_version,
        }
        request_hash = hashlib.sha256(
            json.dumps(
                request_facts,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        operation_policy = db.scalar(
            select(WarehouseAreaStoragePolicy)
            .options(
                selectinload(WarehouseAreaStoragePolicy.area).selectinload(
                    WarehouseArea.floor
                )
            )
            .where(
                WarehouseAreaStoragePolicy.archive_operation_key == operation_key
            )
        )
        if operation_policy is not None:
            if (
                operation_policy.status == "archived"
                and operation_policy.map_feature_id == feature_id
                and operation_policy.area.floor.floor_code == normalized_floor
                and operation_policy.archive_request_hash == request_hash
            ):
                return _formal_area_archive_replay_response(
                    operation_policy,
                    floor_code=normalized_floor,
                    feature_id=feature_id,
                )
            raise HTTPException(
                status_code=409,
                detail="该操作键已被其他区域或不同请求使用，请刷新后重新操作。",
            )
        _claim_floor_projection_for_layout_write(db, floor_code=floor_code)
        formal_policy = db.scalar(
            select(WarehouseAreaStoragePolicy)
            .options(
                selectinload(WarehouseAreaStoragePolicy.area).selectinload(
                    WarehouseArea.floor
                )
            )
            .where(
                WarehouseAreaStoragePolicy.map_feature_id == feature_id
            )
            .execution_options(populate_existing=True)
        )
        if formal_policy is not None:
            if formal_policy.area.floor.floor_code != normalized_floor:
                raise HTTPException(status_code=404, detail="区域不存在或不属于当前楼层")
            if formal_policy.area.floor.construction_status != "enabled":
                raise HTTPException(
                    status_code=409,
                    detail="楼层尚未处于正式启用状态，不能归档区域。",
                )
            if formal_policy.status == "archived":
                if (
                    formal_policy.archive_operation_key == operation_key
                    and formal_policy.archive_request_hash == request_hash
                ):
                    return _formal_area_archive_replay_response(
                        formal_policy,
                        floor_code=normalized_floor,
                        feature_id=feature_id,
                    )
                raise HTTPException(
                    status_code=409,
                    detail="该正式区域已经归档，请刷新地图后继续操作。",
                )
            if formal_policy.status != "published":
                raise HTTPException(
                    status_code=409,
                    detail="该正式区域仍是绑定草稿，请先发布或放弃草稿，不能直接归档。",
                )
            if formal_policy.area.construction_status != "enabled":
                raise HTTPException(
                    status_code=409,
                    detail="正式区域状态不一致，请先完成区域治理后再归档。",
                )
            if expected_policy_version != formal_policy.version:
                raise HTTPException(
                    status_code=409,
                    detail="正式区域设置版本已变化，请刷新后重试。",
                )
            if not expected_published_revision:
                raise HTTPException(
                    status_code=409,
                    detail="缺少当前正式地图版本，请刷新后重试。",
                )
            published_floor = load_published_warehouse_twin_floor_for_edit(floor_code)
            actual_published_revision = str(published_floor.get("revision") or "")
            if (
                expected_published_revision != actual_published_revision
                or formal_policy.published_map_revision != actual_published_revision
            ):
                raise HTTPException(
                    status_code=409,
                    detail="正式地图版本已变化，请刷新后重新删除。",
                )
            effective_floor = load_effective_warehouse_twin_floor_for_edit(floor_code)
            actual_effective_revision = str(effective_floor.get("revision") or "")
            if actual_effective_revision != str(expected_revision or ""):
                raise HTTPException(
                    status_code=409,
                    detail="布局草稿已变化，请刷新后重新归档。",
                )
            feature = next(
                (
                    item
                    for item in effective_floor.get("features") or []
                    if str(item.get("id") or "") == feature_id
                ),
                None,
            )
            if feature is None or feature.get("feature_kind") != "zone":
                raise HTTPException(status_code=404, detail="区域不存在或已被移除")
            try:
                actual_feature_version = int(feature.get("version") or 1)
            except (TypeError, ValueError):
                actual_feature_version = 0
            if actual_feature_version != expected_version:
                raise HTTPException(
                    status_code=409,
                    detail="区域布局版本已变化，请刷新后重新归档。",
                )
            area = formal_policy.area
            published_feature = next(
                (
                    item
                    for item in published_floor.get("features") or []
                    if str(item.get("id") or "") == feature_id
                ),
                None,
            )
            if (
                published_feature is None
                or published_feature.get("feature_kind") != "zone"
            ):
                raise HTTPException(
                    status_code=409,
                    detail="正式地图中的区域身份已变化，请先完成区域治理。",
                )
            for label, candidate in (
                ("当前草稿", feature),
                ("正式地图", published_feature),
            ):
                candidate_area_code = str(
                    candidate.get("erp_area_code") or ""
                ).strip().upper()
                if candidate_area_code and candidate_area_code != area.area_code.upper():
                    raise HTTPException(
                        status_code=409,
                        detail=f"{label}区域编号与正式区域不一致，请先完成治理。",
                    )
                raw_area_id = candidate.get("formal_area_id")
                raw_floor_id = candidate.get("formal_floor_id")
                if raw_area_id not in (None, "") and str(raw_area_id) != str(area.id):
                    raise HTTPException(
                        status_code=409,
                        detail=f"{label}绑定的正式区域身份不一致，请先完成治理。",
                    )
                if raw_floor_id not in (None, "") and str(raw_floor_id) != str(area.floor_id):
                    raise HTTPException(
                        status_code=409,
                        detail=f"{label}绑定的正式楼层身份不一致，请先完成治理。",
                    )
            archive_partition = partition_archived_area_layout(
                effective_floor,
                tombstones=[
                    {"feature_id": feature_id, "area_code": area.area_code}
                ],
            )
            published_archive_partition = partition_archived_area_layout(
                published_floor,
                tombstones=[
                    {"feature_id": feature_id, "area_code": area.area_code}
                ],
            )
            for label, partition in (
                ("当前草稿", archive_partition),
                ("正式地图", published_archive_partition),
            ):
                removed_feature_ids = [
                    str(item.get("id") or "")
                    for item in partition["removed_features"]
                ]
                if removed_feature_ids != [feature_id]:
                    raise HTTPException(
                        status_code=409,
                        detail=f"{label}存在重复或漂移的区域身份，请先完成治理。",
                    )
            owned_map_rack_ids = {
                str(item.get("id") or "").strip()
                for partition in (
                    archive_partition,
                    published_archive_partition,
                )
                for item in partition["removed_racks"]
                if str(item.get("id") or "").strip()
            }
            locations, location_identity_blockers = _formal_area_archive_location_rows(
                db,
                area=area,
                owned_map_rack_ids=owned_map_rack_ids,
            )
            blockers = [*location_identity_blockers, *_formal_area_archive_blockers(
                db,
                floor_layout=effective_floor,
                feature=feature,
                area=area,
                locations=locations,
            )]
            ground_plan = db.scalar(
                select(WarehouseGroundLayoutPlan)
                .where(WarehouseGroundLayoutPlan.area_id == area.id)
                .options(selectinload(WarehouseGroundLayoutPlan.slots))
            )
            ground_plan_retirement = (
                db.scalar(
                    select(WarehouseGroundLayoutPlanRetirement).where(
                        WarehouseGroundLayoutPlanRetirement.plan_id
                        == ground_plan.id
                    )
                )
                if ground_plan is not None
                else None
            )
            if ground_plan is not None:
                if ground_plan.status == "draft":
                    blockers.append("仍有未发布地堆排位草稿，请先放弃该草稿")
                elif ground_plan_retirement is not None:
                    blockers.append("地堆排位已退役但区域仍未归档，请先完成一致性治理")
                elif not retire_ground_plan:
                    blockers.append("仍有已发布地堆排位方案，不能直接归档，请先完成受控处置")
                elif expected_ground_plan_version != ground_plan.version:
                    blockers.append("地堆排位版本已变化，请刷新后重试")
                else:
                    location_ids = {int(row.id) for row in locations}
                    foreign_slot_count = sum(
                        int(slot.location_id) not in location_ids
                        for slot in ground_plan.slots
                    )
                    if foreign_slot_count:
                        blockers.append(
                            f"地堆排位仍引用其他区域库位 {foreign_slot_count} 个"
                        )
            if blockers:
                raise HTTPException(
                    status_code=409,
                    detail=f"{area.area_name} 不能删除：" + "；".join(blockers[:8]),
                )
            archive_snapshot = {
                "effective_layout_objects": {
                    "features": archive_partition["removed_features"],
                    "racks": archive_partition["removed_racks"],
                    "pallets": archive_partition["removed_pallets"],
                },
                "published_layout_objects": {
                    "features": published_archive_partition["removed_features"],
                    "racks": published_archive_partition["removed_racks"],
                    "pallets": published_archive_partition["removed_pallets"],
                },
                "effective_map_revision": actual_effective_revision,
                "published_map_revision": actual_published_revision,
                "area": {
                    "id": area.id,
                    "area_code": area.area_code,
                    "area_name": area.area_name,
                    "construction_status": area.construction_status,
                    "planned_location_count": area.planned_location_count,
                    "planned_pallet_capacity": area.planned_pallet_capacity,
                    "capacity_review_status": area.capacity_review_status,
                    "capacity_eligible": area.capacity_eligible,
                    "confirmed_pallet_capacity": area.confirmed_pallet_capacity,
                    "capacity_reviewed_by": area.capacity_reviewed_by,
                    "capacity_reviewed_at": (
                        area.capacity_reviewed_at.isoformat()
                        if area.capacity_reviewed_at is not None
                        else None
                    ),
                },
                "policy": {
                    "id": formal_policy.id,
                    "status": formal_policy.status,
                    "version": formal_policy.version,
                    "published_map_revision": formal_policy.published_map_revision,
                },
                "ground_layout_plan": (
                    None
                    if ground_plan is None
                    else {
                        "id": ground_plan.id,
                        "status": ground_plan.status,
                        "version": ground_plan.version,
                        "published_map_revision": ground_plan.published_map_revision,
                        "target_slot_count": ground_plan.target_slot_count,
                        "preview_fingerprint": ground_plan.preview_fingerprint,
                        "slots": [
                            {
                                "id": int(slot.id),
                                "location_id": int(slot.location_id),
                                "route_sequence": int(slot.route_sequence),
                                "row_no": int(slot.row_no),
                                "slot_no": int(slot.slot_no),
                                "x_mm": str(slot.x_mm),
                                "y_mm": str(slot.y_mm),
                                "width_mm": int(slot.width_mm),
                                "depth_mm": int(slot.depth_mm),
                            }
                            for slot in ground_plan.slots
                        ],
                    }
                ),
                "locations": [
                    {
                        "id": int(row.id),
                        "location_code": row.location_code,
                        "is_active": bool(row.is_active),
                        "source_version": row.source_version,
                        "address_version": row.address_version,
                        "placement_status": row.placement_status,
                        "layout_version": (
                            row.floor3_layout.version
                            if row.floor3_layout is not None
                            else None
                        ),
                    }
                    for row in locations
                ],
            }
            latest_locations, latest_identity_blockers = (
                _formal_area_archive_location_rows(
                    db,
                    area=area,
                    owned_map_rack_ids=owned_map_rack_ids,
                )
            )
            if [int(row.id) for row in latest_locations] != [
                int(row.id) for row in locations
            ]:
                latest_identity_blockers.append(
                    "区域库位集合刚被其他操作更新，请刷新后重试"
                )
            latest_blockers = [
                *latest_identity_blockers,
                *_formal_area_archive_blockers(
                    db,
                    floor_layout=effective_floor,
                    feature=feature,
                    area=area,
                    locations=latest_locations,
                ),
            ]
            latest_ground_plan = db.scalar(
                select(WarehouseGroundLayoutPlan)
                .where(WarehouseGroundLayoutPlan.area_id == area.id)
                .options(selectinload(WarehouseGroundLayoutPlan.slots))
                .execution_options(populate_existing=True)
            )
            if ground_plan is None:
                if latest_ground_plan is not None:
                    latest_blockers.append(
                        "区域地堆排位事实刚被其他操作更新，请刷新后重试"
                    )
            elif (
                latest_ground_plan is None
                or latest_ground_plan.status != "published"
                or latest_ground_plan.id != ground_plan.id
                or latest_ground_plan.version != expected_ground_plan_version
                or [int(slot.id) for slot in latest_ground_plan.slots]
                != [int(slot.id) for slot in ground_plan.slots]
            ):
                latest_blockers.append("区域地堆排位事实刚被其他操作更新，请刷新后重试")
            elif db.scalar(
                select(WarehouseGroundLayoutPlanRetirement.id).where(
                    WarehouseGroundLayoutPlanRetirement.plan_id
                    == latest_ground_plan.id
                )
            ) is not None:
                latest_blockers.append("区域地堆排位退役事实刚被其他操作更新，请刷新后重试")
            if latest_blockers:
                raise HTTPException(
                    status_code=409,
                    detail=f"{area.area_name} 不能删除："
                    + "；".join(list(dict.fromkeys(latest_blockers))[:8]),
                )
            try:
                now = beijing_now_naive()
                retired_ground_plan_id = None
                if ground_plan is not None:
                    retirement_operation_key = (
                        "ground-retire-"
                        + hashlib.sha256(operation_key.encode("utf-8")).hexdigest()
                    )
                    db.add(
                        WarehouseGroundLayoutPlanRetirement(
                            plan_id=ground_plan.id,
                            area_id=area.id,
                            operation_key=retirement_operation_key,
                            request_hash=request_hash,
                            reason="正式区域归档时退役空区域旧地堆排位",
                            snapshot_json=json.dumps(
                                archive_snapshot["ground_layout_plan"],
                                ensure_ascii=False,
                                sort_keys=True,
                                separators=(",", ":"),
                            ),
                            retired_by=user.id,
                            retired_at=now,
                        )
                    )
                    db.flush()
                    retired_ground_plan_id = int(ground_plan.id)
                inactive_count = 0
                for location in locations:
                    if not location.is_active:
                        continue
                    if not _claim_empty_location_for_reflow(db, location):
                        raise HTTPException(
                            status_code=409,
                            detail="区域内货位刚被占用，请刷新后先完成移货。",
                        )
                    location.is_active = False
                    location.updated_at = now
                    inactive_count += 1
                result_item = {
                    "id": feature_id,
                    "feature_code": feature.get("feature_code"),
                    "feature_kind": "zone",
                    "deleted": True,
                    "archived": True,
                    "formal_area_id": area.id,
                    "inactive_location_count": inactive_count,
                    "archived_child_rack_count": len(
                        archive_partition["removed_racks"]
                    ),
                    "archived_child_pallet_count": len(
                        archive_partition["removed_pallets"]
                    ),
                    "draft_changed": False,
                    "inventory_changed": False,
                    "published_map_changed": False,
                    "retired_ground_plan_id": retired_ground_plan_id,
                }
                archive_snapshot["result"] = {
                    "item": result_item,
                    "revision": actual_effective_revision,
                }
                update_result = db.execute(
                    update(WarehouseAreaStoragePolicy)
                    .where(
                        WarehouseAreaStoragePolicy.id == formal_policy.id,
                        WarehouseAreaStoragePolicy.version == expected_policy_version,
                        WarehouseAreaStoragePolicy.status == "published",
                    )
                    .values(
                        status="archived",
                        version=formal_policy.version + 1,
                        updated_by=user.id,
                        updated_at=now,
                        archived_at=now,
                        archived_by=user.id,
                        archive_operation_key=operation_key,
                        archive_request_hash=request_hash,
                        archive_feature_snapshot_json=json.dumps(
                            archive_snapshot,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                    )
                    .execution_options(synchronize_session=False)
                )
                if update_result.rowcount != 1:
                    raise HTTPException(
                        status_code=409,
                        detail="区域设置已被其他操作更新，请刷新后重试。",
                    )
                area.construction_status = "archived"
                area.capacity_review_status = "excluded"
                area.capacity_eligible = False
                area.confirmed_pallet_capacity = None
                area.capacity_reviewed_by = _capacity_reviewer_name(user)
                area.capacity_reviewed_at = now
                _twin_layout_asset_log(
                    db,
                    request=request,
                    user=user,
                    action="TWIN_LAYOUT_FORMAL_AREA_ARCHIVE",
                    entity_type="warehouse_area",
                    entity_id=str(area.id),
                    description="管理员归档完全空闲的正式仓库区域",
                    details={
                        "floor_code": floor_code,
                        "feature_id": feature_id,
                        "area_code": area.area_code,
                        "policy_version_before": expected_policy_version,
                        "published_map_revision": actual_published_revision,
                        "inactive_location_count": inactive_count,
                        "archived_child_rack_count": len(
                            archive_partition["removed_racks"]
                        ),
                        "archived_child_pallet_count": len(
                            archive_partition["removed_pallets"]
                        ),
                        "draft_changed": False,
                        "inventory_changed": False,
                        "retired_ground_plan_id": retired_ground_plan_id,
                    },
                )
                db.commit()
            except IntegrityError as error:
                db.rollback()
                operation_collision = db.scalar(
                    select(WarehouseAreaStoragePolicy)
                    .options(
                        selectinload(WarehouseAreaStoragePolicy.area).selectinload(
                            WarehouseArea.floor
                        )
                    )
                    .where(
                        WarehouseAreaStoragePolicy.archive_operation_key
                        == operation_key
                    )
                )
                if operation_collision is not None:
                    if (
                        operation_collision.status == "archived"
                        and operation_collision.map_feature_id == feature_id
                        and operation_collision.area.floor.floor_code
                        == normalized_floor
                        and operation_collision.archive_request_hash == request_hash
                    ):
                        return _formal_area_archive_replay_response(
                            operation_collision,
                            floor_code=normalized_floor,
                            feature_id=feature_id,
                        )
                    raise HTTPException(
                        status_code=409,
                        detail="该操作键已被其他归档请求占用，请刷新后重新操作。",
                    ) from error
                raise
            except Exception:
                db.rollback()
                raise
            return {
                "item": result_item,
                "revision": actual_effective_revision,
                "applied": True,
                "idempotent_replay": False,
            }
        pure_draft_blocker = _pure_draft_feature_delete_blocker(
            db,
            floor_code=normalized_floor,
            feature_id=feature_id,
        )
        if pure_draft_blocker:
            raise HTTPException(status_code=409, detail=pure_draft_blocker)
        draft_snapshot = snapshot_warehouse_twin_layout_draft()
        mutation = None
        try:
            mutation = delete_warehouse_twin_feature(
                floor_code,
                feature_id,
                expected_revision=expected_revision,
                expected_version=expected_version,
                operation_key=operation_key,
            )
            if mutation.applied:
                _twin_layout_asset_log(
                    db,
                    request=request,
                    user=user,
                    action="TWIN_LAYOUT_FEATURE_DELETE",
                    entity_type="twin_layout_feature",
                    entity_id=feature_id,
                    description="二维仓库规划删除未启用区域或通道",
                    details={"floor_code": floor_code, **mutation.value},
                )
                db.commit()
        except WarehouseTwinLayoutEditError as error:
            db.rollback()
            if mutation is not None and mutation.applied:
                restore_warehouse_twin_layout_draft(draft_snapshot)
            _handle_twin_layout_edit_error(error)
        except Exception:
            db.rollback()
            if mutation is not None and mutation.applied:
                restore_warehouse_twin_layout_draft(draft_snapshot)
            raise
        return {
            "item": mutation.value,
            "revision": mutation.floor_revision,
            "applied": mutation.applied,
        }


@router.post("/twin-layout/floors/{floor_code}/racks", status_code=201)
def create_twin_layout_rack(
    floor_code: str,
    payload: TwinRackLayoutCreatePayload,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    with WAREHOUSE_TWIN_LAYOUT_TRANSACTION_LOCK:
        _claim_floor_projection_for_layout_write(db, floor_code=floor_code)
        _assert_twin_feature_not_archived(
            db,
            floor_code=floor_code,
            feature_id=payload.area_feature_id,
        )
        draft_snapshot = snapshot_warehouse_twin_layout_draft()
        mutation = None
        try:
            mutation = create_warehouse_twin_rack(
                floor_code,
                expected_revision=payload.expected_revision,
                operation_key=payload.operation_key,
                area_feature_id=payload.area_feature_id,
                values=_rack_layout_values(payload),
            )
            if mutation.applied:
                _twin_layout_asset_log(
                    db,
                    request=request,
                    user=user,
                    action="TWIN_RACK_CREATE",
                    entity_type="twin_rack_layout",
                    entity_id=str(mutation.value.get("id") or ""),
                    description="二维库位布局新增货架",
                    details={
                        "floor_code": floor_code,
                        "rack": mutation.value,
                        "inventory_changed": False,
                    },
                )
                db.commit()
        except WarehouseTwinLayoutEditError as error:
            db.rollback()
            if mutation is not None and mutation.applied:
                restore_warehouse_twin_layout_draft(draft_snapshot)
            _handle_twin_layout_edit_error(error)
        except Exception:
            db.rollback()
            if mutation is not None and mutation.applied:
                restore_warehouse_twin_layout_draft(draft_snapshot)
            raise
        return {
            "item": mutation.value,
            "revision": mutation.floor_revision,
            "applied": mutation.applied,
        }


@router.post("/twin-layout/floors/{floor_code}/zones/{feature_id}/number-racks")
def number_twin_area_racks(
    floor_code: str, feature_id: str, payload: TwinAreaRackNumberingPayload,
    request: Request, db: Session = Depends(get_db), user: User = Depends(admin_only),
) -> dict:
    with WAREHOUSE_TWIN_LAYOUT_TRANSACTION_LOCK:
        _claim_floor_projection_for_layout_write(db, floor_code=floor_code)
        _assert_twin_feature_not_archived(db, floor_code=floor_code, feature_id=feature_id)
        snapshot = snapshot_warehouse_twin_layout_draft()
        mutation = None
        try:
            mutation = number_warehouse_twin_area_racks(floor_code,
                expected_revision=payload.expected_revision, operation_key=payload.operation_key,
                area_feature_id=feature_id)
            if mutation.applied:
                _twin_layout_asset_log(db, request=request, user=user,
                    action="TWIN_RACK_NUMBER_AREA", entity_type="twin_area_layout",
                    entity_id=feature_id, description="按区域从左到右编号货架显示名称，仅保存草稿",
                    details={"floor_code": floor_code, **mutation.value, "inventory_changed": False})
                db.commit()
        except Exception as error:
            db.rollback()
            if mutation is not None and mutation.applied:
                restore_warehouse_twin_layout_draft(snapshot)
            if isinstance(error, WarehouseTwinLayoutEditError):
                _handle_twin_layout_edit_error(error)
            raise
        return {"item": mutation.value, "revision": mutation.floor_revision, "applied": mutation.applied}


@router.patch("/twin-layout/floors/{floor_code}/racks/{rack_id}")
def update_twin_layout_rack(
    floor_code: str,
    rack_id: str,
    payload: TwinRackLayoutUpdatePayload,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    with WAREHOUSE_TWIN_LAYOUT_TRANSACTION_LOCK:
        _claim_floor_projection_for_layout_write(db, floor_code=floor_code)
        _assert_twin_rack_not_archived(
            db,
            floor_code=floor_code,
            rack_id=rack_id,
        )
        draft_snapshot = snapshot_warehouse_twin_layout_draft()
        mutation = None
        try:
            mutation = update_warehouse_twin_rack(
                floor_code,
                rack_id,
                expected_revision=payload.expected_revision,
                expected_version=payload.expected_version,
                operation_key=payload.operation_key,
                values=_rack_layout_values(payload),
            )
            if mutation.applied:
                _twin_layout_asset_log(
                    db,
                    request=request,
                    user=user,
                    action="TWIN_RACK_UPDATE",
                    entity_type="twin_rack_layout",
                    entity_id=rack_id,
                    description="二维库位布局修改货架参数",
                    details={
                        "floor_code": floor_code,
                        "rack": mutation.value,
                        "inventory_changed": False,
                    },
                )
                db.commit()
        except WarehouseTwinLayoutEditError as error:
            db.rollback()
            if mutation is not None and mutation.applied:
                restore_warehouse_twin_layout_draft(draft_snapshot)
            _handle_twin_layout_edit_error(error)
        except Exception:
            db.rollback()
            if mutation is not None and mutation.applied:
                restore_warehouse_twin_layout_draft(draft_snapshot)
            raise
        return {
            "item": mutation.value,
            "revision": mutation.floor_revision,
            "applied": mutation.applied,
        }


@router.delete("/twin-layout/floors/{floor_code}/racks/{rack_id}")
def delete_twin_layout_rack(
    floor_code: str,
    rack_id: str,
    expected_revision: str = Query(min_length=1, max_length=64),
    expected_published_revision: str = Query(min_length=1, max_length=64),
    expected_version: int = Query(ge=1),
    operation_key: str = Query(min_length=8, max_length=120),
    request: Request = None,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    """Immediately retire one published, fully empty rack and its formal slots."""

    normalized_floor = floor_code.strip().upper()
    request_fingerprint = hashlib.sha256(json.dumps({
        "floor_code": normalized_floor,
        "rack_id": str(rack_id).strip(),
        "expected_revision": expected_revision,
        "expected_published_revision": expected_published_revision,
        "expected_version": expected_version,
    }, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    with WAREHOUSE_TWIN_LAYOUT_TRANSACTION_LOCK:
        _claim_floor_projection_for_layout_write(db, floor_code=normalized_floor)
        _assert_twin_rack_not_archived(db, floor_code=normalized_floor, rack_id=rack_id)
        published_floor = load_published_warehouse_twin_floor_for_edit(normalized_floor)
        # Authenticate/authorize and claim the floor before this branch, but
        # replay before stale-version/rack-presence gates: the first response
        # may have been lost after a committed delete.
        replay_receipt = next((item for item in published_floor.get("layout_edit_receipts") or []
                               if item.get("operation_key") == operation_key), None)
        if replay_receipt is not None:
            # A receipt in the map is not enough: a process interruption after
            # file replacement but before DB commit must never masquerade as a
            # successful deletion.  Stop for controlled recovery instead.
            replay_active_slots = int(db.scalar(select(func.count(WarehouseLocation.id)).where(
                WarehouseLocation.map_rack_id == str(rack_id),
                WarehouseLocation.is_active.is_(True),
            )) or 0)
            if replay_active_slots:
                raise HTTPException(status_code=409, detail="货架删除回放发现正式库位尚未归档，请先完成受控恢复。")
            replay_audited = False
            for audit in db.scalars(select(OperationLog).where(
                OperationLog.action == "TWIN_RACK_DELETE_PUBLISHED",
            )).all():
                try:
                    details = json.loads(audit.details or "{}")
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                if details.get("operation_key") == operation_key and details.get("request_fingerprint") == request_fingerprint:
                    replay_audited = True
                    break
            if not replay_audited:
                raise HTTPException(status_code=409, detail="货架删除回放缺少已提交审计，请先完成受控恢复。")
            try:
                mutation = delete_published_warehouse_twin_rack(
                    normalized_floor, rack_id,
                    expected_revision=expected_revision,
                    expected_published_revision=expected_published_revision,
                    expected_version=expected_version,
                    operation_key=operation_key,
                    request_fingerprint=request_fingerprint,
                    inactive_location_count=0,
                )
            except WarehouseTwinLayoutEditError as error:
                _handle_twin_layout_edit_error(error)
            return {
                "item": mutation.value,
                "revision": mutation.floor_revision,
                "published_revision": mutation.published_revision,
                "applied": mutation.applied,
                "idempotent_replay": not mutation.applied,
            }
        published_racks = [item for item in published_floor.get("racks") or [] if str(item.get("id") or "") == str(rack_id)]
        if not published_racks:
            # A newly drawn rack has never become a formal spatial object.
            # Replay the draft receipt before all existence/version checks, as
            # the successful first response may have been lost in transit.
            effective_floor = load_effective_warehouse_twin_floor_for_edit(normalized_floor)
            draft_receipt = next((item for item in effective_floor.get("layout_edit_receipts") or []
                                  if item.get("operation_key") == operation_key), None)
            if draft_receipt is not None:
                replay_audited = False
                for audit in db.scalars(select(OperationLog).where(
                    OperationLog.action == "TWIN_RACK_DELETE_DRAFT",
                )).all():
                    try:
                        details = json.loads(audit.details or "{}")
                    except (TypeError, ValueError, json.JSONDecodeError):
                        continue
                    if details.get("operation_key") == operation_key and details.get("request_fingerprint") == request_fingerprint:
                        replay_audited = True
                        break
                if not replay_audited:
                    raise HTTPException(status_code=409, detail="货架草稿删除回放缺少已提交审计，请先完成受控恢复。")
                try:
                    mutation = delete_warehouse_twin_rack(
                        normalized_floor, rack_id, expected_revision=expected_revision,
                        expected_version=expected_version, operation_key=operation_key,
                    )
                except WarehouseTwinLayoutEditError as error:
                    _handle_twin_layout_edit_error(error)
                replay_item = dict(mutation.value)
                replay_item.setdefault("inactive_location_count", 0)
                replay_item.setdefault("published_map_changed", False)
                replay_item.setdefault("inventory_changed", False)
                return {"item": replay_item, "revision": mutation.floor_revision,
                        "published_revision": str(published_floor.get("revision") or ""),
                        "applied": mutation.applied, "idempotent_replay": not mutation.applied}
            if str(published_floor.get("revision") or "") != expected_published_revision:
                raise HTTPException(status_code=409, detail="正式地图已更新，请刷新后重新删除。")
            draft_racks = [item for item in effective_floor.get("racks") or [] if str(item.get("id") or "") == str(rack_id)]
            if len(draft_racks) != 1:
                raise HTTPException(status_code=404, detail="货架不存在或已被删除。")
            mapped_count = int(db.scalar(select(func.count(WarehouseLocation.id)).where(
                WarehouseLocation.map_rack_id == str(rack_id), WarehouseLocation.is_active.is_(True),
            )) or 0)
            if mapped_count:
                raise HTTPException(status_code=409, detail="未发布货架已被正式库位引用，请先完成一致性治理。")
            draft_snapshot = snapshot_warehouse_twin_layout_draft()
            mutation = None
            try:
                mutation = delete_warehouse_twin_rack(
                    normalized_floor, rack_id, expected_revision=expected_revision,
                    expected_version=expected_version, operation_key=operation_key,
                )
                if mutation.applied:
                    mutation.value.update({
                        "inactive_location_count": 0,
                        "published_map_changed": False,
                        "inventory_changed": False,
                    })
                    _twin_layout_asset_log(
                        db, request=request, user=user, action="TWIN_RACK_DELETE_DRAFT",
                        entity_type="twin_rack_layout", entity_id=str(rack_id),
                        description="管理员删除未发布二维库位货架草稿",
                        details={"floor_code": normalized_floor, **mutation.value,
                                 "operation_key": operation_key,
                                 "request_fingerprint": request_fingerprint,
                                 "published_revision": str(published_floor.get("revision") or "")},
                    )
                    db.commit()
                return {"item": mutation.value, "revision": mutation.floor_revision,
                        "published_revision": str(published_floor.get("revision") or ""),
                        "applied": mutation.applied, "idempotent_replay": not mutation.applied}
            except WarehouseTwinLayoutEditError as error:
                db.rollback()
                if mutation is not None and mutation.applied:
                    restore_warehouse_twin_layout_draft(draft_snapshot)
                _handle_twin_layout_edit_error(error)
            except Exception:
                db.rollback()
                if mutation is not None and mutation.applied:
                    restore_warehouse_twin_layout_draft(draft_snapshot)
                raise
        if str(published_floor.get("revision") or "") != expected_published_revision:
            raise HTTPException(status_code=409, detail="正式地图已更新，请刷新后重新删除。")
        if len(published_racks) != 1:
            raise HTTPException(status_code=409, detail="正式地图中的货架身份已变化，请刷新后重新删除。")
        rack = published_racks[0]
        locations, identity_blockers = _formal_rack_archive_location_rows(
            db, floor_code=normalized_floor, rack_id=str(rack_id),
        )
        blockers = [*identity_blockers, *_formal_rack_archive_blockers(
            db, locations=locations, rack_id=str(rack_id), rack_code=rack.get("rack_code"), rack_name=rack.get("name"),
        )]
        if blockers:
            raise HTTPException(status_code=409, detail="该货架不能删除：" + "；".join(blockers[:8]))
        layout_snapshot = snapshot_warehouse_twin_publish_state()
        mutation = None
        try:
            mutation = delete_published_warehouse_twin_rack(
                normalized_floor, rack_id,
                expected_revision=expected_revision,
                expected_published_revision=expected_published_revision,
                expected_version=expected_version,
                operation_key=operation_key,
                request_fingerprint=request_fingerprint,
                inactive_location_count=len(locations),
            )
            if mutation.applied:
                # Claim a second time after map writes: a concurrent inbound,
                # pallet placement, or reservation cannot slip into an empty
                # slot between the initial dependency scan and retirement.
                inactive_location_count = 0
                now = beijing_now_naive()
                for location in locations:
                    if not _claim_empty_location_for_reflow(db, location):
                        raise HTTPException(status_code=409, detail="货架内库位刚被占用，请刷新后先完成移货。")
                    location.is_active = False
                    location.updated_at = now
                    inactive_location_count += 1
                if inactive_location_count != mutation.value.get("inactive_location_count"):
                    raise HTTPException(status_code=409, detail="货架库位集合刚被其他操作更新，请刷新后重试。")
                _twin_layout_asset_log(
                    db, request=request, user=user,
                    action="TWIN_RACK_DELETE_PUBLISHED",
                    entity_type="twin_rack_layout", entity_id=str(rack_id),
                    description="管理员删除正式空货架并归档关联空货位",
                    details={
                        "floor_code": normalized_floor, **mutation.value,
                        "operation_key": operation_key,
                        "request_fingerprint": request_fingerprint,
                        "published_revision": mutation.published_revision,
                        "draft_revision": mutation.floor_revision,
                    },
                )
                db.commit()
            return {
                "item": mutation.value,
                "revision": mutation.floor_revision,
                "published_revision": mutation.published_revision,
                "applied": mutation.applied,
                "idempotent_replay": not mutation.applied,
            }
        except WarehouseTwinLayoutEditError as error:
            db.rollback()
            if mutation is not None and mutation.applied:
                restore_warehouse_twin_publish_state(layout_snapshot)
            _handle_twin_layout_edit_error(error)
        except Exception:
            db.rollback()
            if mutation is not None and mutation.applied:
                restore_warehouse_twin_publish_state(layout_snapshot)
            raise


@router.patch('/twin-layout/floors/{floor_code}/zones/{feature_id}/geometry')
def update_twin_zone_geometry(
    floor_code: str, feature_id: str, payload: TwinZoneGeometryPayload,
    request: Request, db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    with WAREHOUSE_TWIN_LAYOUT_TRANSACTION_LOCK:
        return _update_twin_zone_geometry_locked(
            floor_code=floor_code,
            feature_id=feature_id,
            payload=payload,
            request=request,
            db=db,
            user=user,
        )


def _update_twin_zone_geometry_locked(
    *,
    floor_code: str,
    feature_id: str,
    payload: TwinZoneGeometryPayload,
    request: Request,
    db: Session,
    user: User,
) -> dict:
    _claim_floor_projection_for_layout_write(db, floor_code=floor_code)
    _assert_twin_feature_not_archived(
        db,
        floor_code=floor_code,
        feature_id=feature_id,
    )
    draft_snapshot = snapshot_warehouse_twin_layout_draft()
    mutation = None
    try:
        mutation = update_warehouse_twin_zone_geometry(
            floor_code, feature_id,
            expected_revision=payload.expected_revision,
            expected_version=payload.expected_version,
            operation_key=payload.operation_key,
            points=[list(point) for point in payload.points],
        )
        if mutation.applied:
            _twin_layout_asset_log(
                db, request=request, user=user,
                action='TWIN_ZONE_GEOMETRY_UPDATE',
                entity_type='twin_zone_geometry', entity_id=feature_id,
                description='二维库位布局修改区域实测边界',
                details={'floor_code': floor_code, 'zone': mutation.value,
                         'inventory_changed': False, 'location_binding_changed': False},
            )
            db.commit()
    except WarehouseTwinLayoutEditError as error:
        db.rollback()
        if mutation is not None and mutation.applied:
            restore_warehouse_twin_layout_draft(draft_snapshot)
        _handle_twin_layout_edit_error(error)
    except Exception:
        db.rollback()
        if mutation is not None and mutation.applied:
            restore_warehouse_twin_layout_draft(draft_snapshot)
        raise
    return {'item': mutation.value, 'revision': mutation.floor_revision,
            'applied': mutation.applied}


@router.patch('/twin-layout/floors/{floor_code}/zones/{feature_id}/storage-policy')
def update_twin_zone_storage_policy(
    floor_code: str,
    feature_id: str,
    payload: TwinZoneStoragePolicyPayload,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    with WAREHOUSE_TWIN_LAYOUT_TRANSACTION_LOCK:
        return _update_twin_zone_storage_policy_locked(
            floor_code=floor_code,
            feature_id=feature_id,
            payload=payload,
            request=request,
            db=db,
            user=user,
        )


def _update_twin_zone_storage_policy_locked(
    *,
    floor_code: str,
    feature_id: str,
    payload: TwinZoneStoragePolicyPayload,
    request: Request,
    db: Session,
    user: User,
    commit: bool = True,
    allow_legacy_v11_name_only: bool = True,
) -> dict:
    if commit:
        _claim_floor_projection_for_layout_write(db, floor_code=floor_code)
    requested_area_code = str(payload.erp_area_code or "").strip().upper()
    _assert_twin_feature_not_archived(
        db,
        floor_code=floor_code,
        feature_id=feature_id,
        area_code=requested_area_code,
    )
    formal_area: WarehouseArea | None = None
    policy: WarehouseAreaStoragePolicy | None = None
    try:
        effective_floor = load_effective_warehouse_twin_floor_for_edit(floor_code)
    except WarehouseTwinLayoutEditError as error:
        _handle_twin_layout_edit_error(error)
    effective_feature = next(
        (item for item in effective_floor.get('features') or []
         if str(item.get('id') or '') == feature_id),
        None,
    )
    if effective_feature is None or effective_feature.get('feature_kind') != 'zone':
        raise HTTPException(status_code=404, detail='区域不存在或已被删除')
    current_types = list(effective_feature.get('allowed_inventory_types') or [])
    current_layout = str(effective_feature.get('storage_layout') or '')
    current_area_code = str(effective_feature.get('erp_area_code') or '').strip().upper()
    if current_area_code and requested_area_code != current_area_code:
        raise HTTPException(
            status_code=409,
            detail='地图区域已绑定正式区域；如需更换，请先在独立治理流程核对库存、库位和历史身份',
        )
    floor = warehouse_floor_for_code(db, floor_code)
    if floor is None:
        raise HTTPException(status_code=409, detail='请先建立正式仓库楼层台账')
    formal_area = db.scalar(
        select(WarehouseArea)
        .options(selectinload(WarehouseArea.storage_policy))
        .where(
            WarehouseArea.floor_id == floor.id,
            func.upper(WarehouseArea.area_code) == requested_area_code,
        )
    )
    if formal_area is not None and (
        formal_area.construction_status == "archived"
        or (
            formal_area.storage_policy is not None
            and formal_area.storage_policy.status == "archived"
        )
    ):
        raise HTTPException(
            status_code=409,
            detail="该区域已经归档，普通区域设置不能恢复或修改。",
        )
    if (
        formal_area is not None
        and formal_area.storage_policy is None
        and payload.existing_area_id is None
    ):
        raise HTTPException(
            status_code=409,
            detail="该编号对应现有未绑定区域，请从现有区域列表明确选择后再保存",
        )
    if payload.existing_area_id is not None:
        selected_area = db.scalar(
            select(WarehouseArea)
            .options(selectinload(WarehouseArea.storage_policy))
            .where(
                WarehouseArea.id == payload.existing_area_id,
                WarehouseArea.floor_id == floor.id,
            )
        )
        if selected_area is None:
            raise HTTPException(status_code=409, detail="所选现有区域已变化或不属于当前楼层")
        if selected_area.construction_status == "archived" or (
            selected_area.storage_policy is not None
            and selected_area.storage_policy.status == "archived"
        ):
            raise HTTPException(
                status_code=409,
                detail="所选区域已经归档，普通区域设置不能恢复或修改。",
            )
        if selected_area.area_code.upper() != requested_area_code:
            raise HTTPException(status_code=409, detail="所选现有区域与正式区域编号不一致")
        if formal_area is None or formal_area.id != selected_area.id:
            raise HTTPException(status_code=409, detail="正式区域编号已被其他区域占用")
        if selected_area.storage_policy is not None and not (
            selected_area.storage_policy.map_feature_id == feature_id
            and selected_area.storage_policy.status == "draft"
        ):
            raise HTTPException(status_code=409, detail="所选现有区域已被绑定，请刷新后重新选择")
        formal_area = selected_area
    if formal_area is None:
        orphaned_formal_locations = list(
            db.scalars(
                select(WarehouseLocation).where(
                    WarehouseLocation.warehouse_floor == floor.floor_number,
                    func.upper(WarehouseLocation.area_code) == requested_area_code,
                )
            ).all()
        )
        if orphaned_formal_locations:
            raise HTTPException(
                status_code=409,
                detail="该区域编号已有未纳入正式区域台账的历史库位，请先完成治理核对",
            )
    formal_policy = formal_area.storage_policy if formal_area is not None else None
    if (
        formal_area is not None
        and "finished" not in set(payload.allowed_inventory_types)
        and db.scalar(
            select(CustomerFinishedStoragePreference.id)
            .where(
                CustomerFinishedStoragePreference.warehouse_area_id
                == formal_area.id
            )
            .limit(1)
        )
        is not None
    ):
        raise HTTPException(
            status_code=409,
            detail=(
                f"{formal_area.area_code} 区仍是客户默认成品区域，"
                "请先在客户资料中移除后再取消成品用途"
            ),
        )
    formal_types = (
        policy_inventory_types(formal_policy) if formal_policy is not None else []
    )
    formal_layout = (
        str(formal_policy.storage_layout or "") if formal_policy is not None else ""
    )
    same_code_feature_count = sum(
        1
        for item in effective_floor.get("features") or []
        if item.get("feature_kind") == "zone"
        and str(item.get("erp_area_code") or "").strip().upper()
        == requested_area_code
    )
    legacy_v11_name_only = bool(
        allow_legacy_v11_name_only
        and formal_area is not None
        and formal_area.storage_policy is None
        and payload.existing_area_id == formal_area.id
        and current_area_code == requested_area_code
        and payload.area_name
        and legacy_v11_name_only_change_is_safe(
            db,
            floor=floor,
            area=formal_area,
            feature=effective_feature,
            feature_area_code_count=same_code_feature_count,
            requested_inventory_types=list(payload.allowed_inventory_types),
            requested_storage_layout=payload.storage_layout,
            current_feature=effective_feature,
        )
    )
    semantic_change = (
        not legacy_v11_name_only
        and (
            set(current_types) != set(payload.allowed_inventory_types)
            or current_layout != payload.storage_layout
            or bool(requested_area_code and requested_area_code != current_area_code)
            or (
                formal_policy is not None
                and (
                    set(formal_types) != set(payload.allowed_inventory_types)
                    or formal_layout != payload.storage_layout
                    or formal_policy.map_feature_id != feature_id
                )
            )
        )
    )
    occupancy_blockers: list[str] = []
    if semantic_change:
        for mold in db.scalars(select(MoldTool).where(
            or_(MoldTool.is_active.is_(True), MoldTool.archive_status == 'archived')
        )).all():
            codes = set(mold_location_feature_codes(
                mold.rack_location, floor_layout=effective_floor
            )) | set(_twin_reference_feature_codes('mold', mold.rack_location))
            guide = describe_mold_location(mold.rack_location)
            exact_archive = (
                guide.get('floor') == effective_floor.get('floor_code')
                and str(guide.get('area') or '').upper()
                in {current_area_code, requested_area_code}
            )
            occupied = exact_archive if mold.archive_status == 'archived' else (
                feature_id in codes or str(effective_feature.get('feature_code') or '') in codes
            )
            if occupied:
                occupancy_blockers.append('区域内仍有实体模具，不能改变区域策略')
                break
    if semantic_change:
        plates = db.scalars(select(PrintingPlate).where(
            PrintingPlate.status.in_(('active', 'damaged'))
        )).all()
        feature_code = str(effective_feature.get('feature_code') or '')
        for plate in plates:
            guide = describe_printing_plate_location(plate.rack_location)
            codes = set(_twin_reference_feature_codes('printing_plate', plate.rack_location))
            if guide.get('kind') == 'plate_rack':
                codes.add('ZONE-1F-PLATE-002')
            if feature_id in codes or feature_code in codes:
                occupancy_blockers.append('区域内仍有启用或受损挂板，不能改变区域策略')
                break
    if semantic_change:
        try:
            mappings = list_production_projection_mappings(
                str(effective_floor.get('layout_id') or ''), effective_floor
            )
        except (WarehouseTwinProductionError, sqlite3.Error, OSError):
            # The isolated production projection is an optional visual aid,
            # not the formal warehouse ledger.  Its absence must not freeze
            # an otherwise safe area confirmation.  Real inventory, pallets,
            # molds and printing plates are checked above; when projection
            # data exists, pending mapped tasks remain a blocker below.
            pass
        else:
            pending_ids = _mapped_live_production_task_ids(db)
            pallets = {str(item.get('id')): item for item in effective_floor.get('pallets') or []}
            for mapping in mappings:
                if int(mapping.get('source_task_id') or 0) not in pending_ids:
                    continue
                target_matches = mapping.get('target_kind') == 'zone' and str(mapping.get('target_id')) == feature_id
                if mapping.get('target_kind') == 'pallet':
                    pallet = pallets.get(str(mapping.get('target_id'))) or {}
                    target_matches = (
                        str(pallet.get('zone_id') or '') == feature_id
                        or str(pallet.get('zone_code') or '') == str(effective_feature.get('feature_code') or '')
                    )
                if target_matches:
                    occupancy_blockers.append('区域内仍有待生产任务地图占用，不能改变区域策略')
                    break
    if occupancy_blockers:
        raise HTTPException(status_code=409, detail='区域用途修改被阻止：' + '；'.join(occupancy_blockers))
    if requested_area_code:
        feature_policy = db.scalar(
            select(WarehouseAreaStoragePolicy).where(
                WarehouseAreaStoragePolicy.map_feature_id == feature_id
            )
        )
        if feature_policy is not None and (
            formal_area is None or feature_policy.area_id != formal_area.id
        ):
            raise HTTPException(status_code=409, detail="该地图区域已绑定其他正式区域")
        if (
            formal_area is not None
            and formal_area.storage_policy is not None
            and formal_area.storage_policy.map_feature_id != feature_id
        ):
            raise HTTPException(status_code=409, detail="正式区域已绑定其他地图区域")
    selected_existing_area_id = formal_area.id if formal_area is not None else None
    if semantic_change and formal_area is not None and formal_area.storage_policy is not None:
        blockers = policy_location_transition_blockers(
            db, floor=floor, area=formal_area, policy=formal_area.storage_policy,
            requested_inventory_types=list(payload.allowed_inventory_types),
            requested_storage_layout=payload.storage_layout,
        )
        if blockers:
            raise HTTPException(
                status_code=409,
                detail='区域用途修改被阻止：' + '；'.join(blockers),
            )
    if (
        formal_area is not None
        and formal_area.storage_policy is None
        and not legacy_v11_name_only
    ):
        blockers = unbound_area_location_transition_blockers(
            db,
            floor=floor,
            area=formal_area,
            requested_inventory_types=list(payload.allowed_inventory_types),
            requested_storage_layout=payload.storage_layout,
        )
        if blockers:
            raise HTTPException(
                status_code=409,
                detail='区域用途修改被阻止：' + '；'.join(blockers),
            )
    draft_snapshot = snapshot_warehouse_twin_layout_draft()
    mutation = None
    try:
        mutation = update_warehouse_twin_zone_policy(
            floor_code,
            feature_id,
            expected_revision=payload.expected_revision,
            expected_version=payload.expected_version,
            operation_key=payload.operation_key,
            allowed_inventory_types=list(payload.allowed_inventory_types),
            storage_layout=payload.storage_layout,
            erp_area_code=payload.erp_area_code,
            area_name=payload.area_name,
            formal_area_id=selected_existing_area_id,
            formal_floor_id=(formal_area.floor_id if formal_area is not None else None),
            max_rack_count=payload.max_rack_count,
            pallet_rotation_deg=payload.pallet_rotation_deg,
            legacy_v11_name_only=legacy_v11_name_only,
        )
        mapped_area_code = str(mutation.value.get("erp_area_code") or "").strip().upper()
        if mapped_area_code != requested_area_code:
            raise HTTPException(status_code=409, detail='地图草稿与本次正式区域编号不一致')
        mutation.value["formal_binding_status"] = "draft"
        mutation.value["formal_policy_status"] = (
            formal_area.storage_policy.status
            if formal_area is not None and formal_area.storage_policy is not None
            else None
        )
        if selected_existing_area_id is not None:
            mutation.value["area_master_name"] = formal_area.area_name
            mutation.value["employee_area_name"] = employee_area_name(
                mutation.value,
                area_code=formal_area.area_code,
                floor_number=floor.floor_number,
            )
            mutation.value["formal_construction_status"] = formal_area.construction_status
            mutation.value["planned_pallet_capacity"] = formal_area.planned_pallet_capacity
            mutation.value["capacity_review_status"] = formal_area.capacity_review_status
            mutation.value["capacity_eligible"] = formal_area.capacity_eligible
            mutation.value["confirmed_pallet_capacity"] = formal_area.confirmed_pallet_capacity
        if not mutation.applied:
            return {
                'item': mutation.value,
                'revision': mutation.floor_revision,
                'applied': False,
                'formal_area': (
                    _warehouse_area_dict(db, formal_area)
                    if formal_area is not None else None
                ),
            }
        if mutation.applied:
            _twin_layout_asset_log(
                db,
                request=request,
                user=user,
                action="TWIN_ZONE_POLICY_UPDATE",
                entity_type="twin_zone_policy",
                entity_id=feature_id,
                description="二维库位布局修改区域存放策略草稿",
                details={
                    "floor_code": floor_code,
                    "zone": mutation.value,
                    "formal_area_code": mapped_area_code or None,
                    "formal_binding_status": "draft",
                    "formal_sql_changed": False,
                    "inventory_changed": False,
                },
            )
            if commit:
                db.commit()
    except WarehouseTwinLayoutEditError as error:
        db.rollback()
        if mutation is not None and mutation.applied:
            restore_warehouse_twin_layout_draft(draft_snapshot)
        _handle_twin_layout_edit_error(error)
    except Exception:
        db.rollback()
        if mutation is not None and mutation.applied:
            restore_warehouse_twin_layout_draft(draft_snapshot)
        raise
    return {
        "item": mutation.value,
        "revision": mutation.floor_revision,
        "applied": mutation.applied,
        "formal_area": (
            _warehouse_area_dict(db, formal_area) if formal_area is not None else None
        ),
    }


def _ensure_one_step_pallet_locations(
    db: Session,
    *,
    floor_layout: dict,
    feature_id: str,
    area: WarehouseArea,
    inventory_type: str,
    storage_layout: str,
    target_count: int,
    operator_id: int,
) -> tuple[list[WarehouseLocation], int, dict]:
    location_warehouse_type = location_warehouse_type_for_inventory_types(
        [inventory_type]
    )
    if location_warehouse_type is None:
        return [], 0, {}
    floor = area.floor
    policy = area.storage_policy
    if floor is None or policy is None or policy.status != "published":
        raise WarehouseAreaActivationError("区域尚未完成正式发布，不能生成空货位", status_code=409)
    desired_storage_type = (
        "rack" if storage_layout == "rack" else "ground"
    )
    existing = formal_area_location_rows(db, floor=floor, area=area)
    if storage_layout == "rack":
        precise_racks = [
            rack for rack in floor_layout.get("racks", [])
            if rack.get("id") and rack.get("area_feature_id") == feature_id
            and isinstance(rack.get("level_cell_counts"), list)
            and len(rack["level_cell_counts"]) == int(rack.get("levels") or 0)
        ]
        if precise_racks:
            # The form capacity is a rack count. Published rack synchronization
            # already maintains the actual locations by level/cell; compare
            # those locations with cells, never with the number of racks.
            target_count = sum(sum(rack["level_cell_counts"]) for rack in precise_racks)
            rack_ids = {str(rack["id"]) for rack in precise_racks}
            active = [row for row in existing if row.is_active]
            if len(active) != target_count or any(
                row.map_rack_id not in rack_ids for row in active
            ):
                raise WarehouseAreaActivationError(
                    "实测货架格位与启用货位尚未同步，请先保存货架层数和格数", status_code=409,
                )
    if any(not str(row.source_version or "").strip() for row in existing):
        raise WarehouseAreaActivationError(
            "该区域存在未标明来源的货位，已停止一次确认；请先核对正式货位台账",
            status_code=409,
        )
    existing_sources = {
        str(row.source_version or "").strip() for row in existing
    } - {""}
    if len(existing_sources) > 1 or existing_sources - {
        "V11",
        "TWIN_V1",
        AREA_LOCATION_SOURCE_VERSION,
    }:
        raise WarehouseAreaActivationError(
            "该区域货位来源不一致，已停止一次确认；请先核对正式货位台账",
            status_code=409,
        )
    if storage_layout == "pallet_ground":
        incompatible = [
            row
            for row in existing
            if row.is_active
            and (
                row.warehouse_type != location_warehouse_type
                or row.storage_type != desired_storage_type
            )
        ]
        if incompatible:
            raise WarehouseAreaActivationError(
                "该区域已有用途或存放方式不同的启用货位；请先核对，系统不会覆盖真实位置",
                status_code=409,
            )
        source_version = next(
            iter(existing_sources), AREA_LOCATION_SOURCE_VERSION
        )
        if source_version == "V11":
            count_result = adjust_area_location_count(
                db,
                area_code=area.area_code,
                target_count=target_count,
                operator_id=operator_id,
                allow_empty_historical_retirement=target_count == 0,
            )
            _sync_formal_area_location_count(
                db,
                area=area,
                policy=policy,
                target_count=count_result.active_count,
                operator_id=operator_id,
                increment_policy_version=bool(
                    count_result.created
                    or count_result.enabled
                    or count_result.disabled
                ),
            )
        else:
            count_result = adjust_activated_area_location_count(
                db,
                floor_code=floor.floor_code,
                area_code=area.area_code,
                target_count=target_count,
                operator_id=operator_id,
                allow_empty_historical_retirement=target_count == 0,
            )
        reflow_result = _reflow_area_locations(
            db,
            floor_code=floor.floor_code,
            area_code=area.area_code,
            source_version=source_version,
            operator_id=operator_id,
        )
        return list(count_result.created), count_result.active_count, {
            "source_version": source_version,
            "created_ids": [row.id for row in count_result.created],
            "enabled_ids": [row.id for row in count_result.enabled],
            "disabled_ids": [row.id for row in count_result.disabled],
            "reflow": reflow_result,
        }
    if target_count <= 0 and not any(row.is_active for row in existing):
        return [], 0, {}
    if existing:
        reusable = [
            row
            for row in existing
            if row.is_active
            and row.placement_status == "placed"
            and row.floor3_layout is not None
            and row.warehouse_type == location_warehouse_type
            and row.storage_type == desired_storage_type
        ]
        active_rows = [row for row in existing if row.is_active]
        if (
            active_rows
            and len(reusable) == len(active_rows)
            and target_count == len(active_rows)
        ):
            area.planned_location_count = len(active_rows)
            return [], len(active_rows), {
                "source_version": next(iter(existing_sources), ""),
                "created_ids": [],
                "enabled_ids": [],
                "disabled_ids": [],
            }
        if len(reusable) == len(active_rows) and target_count != len(active_rows):
            raise WarehouseAreaActivationError(
                "货架位必须按实测货架格位维护；确认容量与现有启用货架位数量不一致",
                status_code=409,
            )
        raise WarehouseAreaActivationError(
            "该区域已有未落位或不可用的位置，请先完成位置核对；系统不会覆盖真实位置",
            status_code=409,
        )
    try:
        planned_slots = confirmed_capacity_slots_for_zone(
            floor_layout,
            feature_id=feature_id,
            target_count=target_count,
            prefer_standard_pallet_slots=storage_layout == "pallet_ground",
        )
    except Floor1CandidatePlanningError as error:
        raise WarehouseAreaActivationError(str(error), status_code=error.status_code) from error
    next_sort = int(db.scalar(select(func.max(WarehouseLocation.sort_order))) or 0) + 1
    created: list[WarehouseLocation] = []
    for serial, slot in enumerate(planned_slots, start=1):
        row = WarehouseLocation(
            location_code=f"{floor.floor_code.upper()}-{area.area_code.upper()}-L{serial:03d}",
            location_name=f"{area.area_name} {serial:03d} 号位",
            warehouse_type=location_warehouse_type,
            warehouse_floor=floor.floor_number,
            area_code=area.area_code.upper(),
            storage_type=desired_storage_type,
            sort_order=next_sort,
            is_temporary=False,
            source_version=AREA_LOCATION_SOURCE_VERSION,
            placement_status="placed",
        )
        row.floor3_layout = Floor3LocationLayout(
            left_pct=Decimal(str(slot["left_pct"])),
            top_pct=Decimal(str(slot["top_pct"])),
            width_pct=Decimal(str(slot["width_pct"])),
            height_pct=Decimal(str(slot["height_pct"])),
            z_index=0,
            version=1,
            source_type="seeded",
            layout_kind=(
                "logical_anchor"
                if slot.get("capacity_confirmed") is True
                else "physical_pallet"
            ),
            created_by=operator_id,
            updated_by=operator_id,
        )
        db.add(row)
        created.append(row)
        next_sort += 1
    area.planned_location_count = target_count
    db.flush()
    return created, target_count, {
        "source_version": AREA_LOCATION_SOURCE_VERSION,
        "created_ids": [row.id for row in created],
        "enabled_ids": [],
        "disabled_ids": [],
    }


def _ensure_one_step_ground_plan(
    db: Session,
    *,
    floor_layout: dict,
    feature_id: str,
    area: WarehouseArea,
    storage_layout: str,
    location_count: int,
    operation_key: str,
    operator_id: int,
    allow_reconfigure_existing: bool = False,
    preserve_existing_locations: bool = False,
) -> dict:
    """Materialize real one-step ground positions in the canonical plan ledger.

    A measured zone confirmation used to create ``TWIN_V1`` locations and
    advertise them as available without creating the published ground plan
    required by every inventory write.  Only positions that match an actual
    1200x1000 measured pallet footprint are operational.  Capacity-only
    logical anchors remain visible planning facts, but are deliberately not
    returned as available inventory destinations.
    """

    existing_plan = db.scalar(
        select(WarehouseGroundLayoutPlan)
        .where(WarehouseGroundLayoutPlan.area_id == area.id)
        .options(selectinload(WarehouseGroundLayoutPlan.slots))
        .with_for_update()
    )
    if storage_layout != "pallet_ground":
        if existing_plan is not None:
            raise WarehouseAreaActivationError(
                "该区域已有正式地堆排位，不能从一次确认改成其他布局",
                status_code=409,
            )
        return {
            "available_location_count": max(0, int(location_count)),
            "ground_plan_id": None,
            "ground_plan_status": None,
            "location_readiness_issue": None,
        }
    if location_count <= 0:
        if existing_plan is not None and not allow_reconfigure_existing:
            raise WarehouseAreaActivationError(
                "该区域已有正式地堆排位，不能从一次确认清空或停用其库位",
                status_code=409,
            )
        if existing_plan is not None:
            if db.scalar(
                select(WarehouseGroundLayoutPlanRetirement.id).where(
                    WarehouseGroundLayoutPlanRetirement.plan_id == existing_plan.id
                )
            ) is not None:
                raise WarehouseAreaActivationError(
                    "该区域地堆排位已经退役，不能继续修改容量",
                    status_code=409,
                )
            return {
                "available_location_count": 0,
                "ground_plan_id": int(existing_plan.id),
                "ground_plan_status": "retained_empty",
                "location_readiness_issue": None,
            }
        return {
            "available_location_count": 0,
            "ground_plan_id": None,
            "ground_plan_status": None,
            "location_readiness_issue": None,
        }

    floor = area.floor
    policy = area.storage_policy
    current_revision = str(floor_layout.get("revision") or "").strip()
    if (
        floor is None
        or policy is None
        or policy.status != "published"
        or str(policy.map_feature_id or "").strip() != str(feature_id).strip()
        or str(policy.published_map_revision or "").strip() != current_revision
        or not current_revision
    ):
        raise WarehouseAreaActivationError(
            "区域地图身份尚未完成正式发布，不能生成地堆排位",
            status_code=409,
        )

    rows = list(
        db.scalars(
            select(WarehouseLocation)
            .where(
                WarehouseLocation.warehouse_floor == floor.floor_number,
                func.upper(WarehouseLocation.area_code) == area.area_code.upper(),
                WarehouseLocation.is_active.is_(True),
                WarehouseLocation.placement_status == "placed",
                WarehouseLocation.storage_type == "ground",
            )
            .options(selectinload(WarehouseLocation.floor3_layout))
            .order_by(WarehouseLocation.sort_order, WarehouseLocation.id)
        ).all()
    )
    if len(rows) != int(location_count):
        raise WarehouseAreaActivationError(
            "区域货位数量在确认过程中发生变化，请刷新后重试",
            status_code=409,
        )

    non_physical = [
        row
        for row in rows
        if row.floor3_layout is None
        or row.floor3_layout.layout_kind != "physical_pallet"
    ]
    if non_physical:
        if existing_plan is not None:
            raise WarehouseAreaActivationError(
                "该区域正式地堆排位与当前库位几何已经漂移，不能降级为规划位置",
                status_code=409,
            )
        return {
            "available_location_count": 0,
            "ground_plan_id": None,
            "ground_plan_status": "planning_only",
            "location_readiness_issue": (
                "确认容量中含非标准实测栈板位；这些位置仅作规划展示，"
                "不能用于收料、入库或移位"
            ),
        }

    layout_slots = [_layout_geometry_payload(row) for row in rows]
    try:
        validated_slots = validate_capacity_layout_slots_for_zone(
            floor_layout,
            feature_id=feature_id,
            slots=layout_slots,
        )
    except Floor1CandidatePlanningError as error:
        raise WarehouseAreaActivationError(
            str(error), status_code=error.status_code
        ) from error

    feature = next(
        (
            row
            for row in floor_layout.get("features") or []
            if row.get("feature_kind") == "zone"
            and str(row.get("id") or "") == str(feature_id)
        ),
        None,
    )
    points = (feature or {}).get("points") or []
    geometry_epsilon = _percent_round_trip_epsilon(points)
    pallet_contract = standard_pallet_contract()
    standard_width = int(pallet_contract["width_mm"])
    standard_depth = int(pallet_contract["depth_mm"])
    matched_slots: list[dict] = []
    for row, layout_slot, actual_slot in zip(
        rows, layout_slots, validated_slots, strict=True
    ):
        layout = row.floor3_layout
        assert layout is not None
        actual_width = float(actual_slot["width_mm"])
        actual_depth = float(actual_slot["depth_mm"])
        standard_orientation = (
            abs(actual_width - standard_width) <= geometry_epsilon
            and abs(actual_depth - standard_depth) <= geometry_epsilon
        )
        rotated_orientation = (
            abs(actual_width - standard_depth) <= geometry_epsilon
            and abs(actual_depth - standard_width) <= geometry_epsilon
        )
        if not standard_orientation and not rotated_orientation:
            if existing_plan is not None:
                raise WarehouseAreaActivationError(
                    "该区域正式地堆排位与当前库位几何已经漂移，不能降级为规划位置",
                    status_code=409,
                )
            return {
                "available_location_count": 0,
                "ground_plan_id": None,
                "ground_plan_status": "planning_only",
                "location_readiness_issue": (
                    "确认位置无法按当前地图还原为1200×1000毫米标准栈板位；"
                    "这些位置不能用于收料、入库或移位"
                ),
            }
        width_mm = standard_width if standard_orientation else standard_depth
        depth_mm = standard_depth if standard_orientation else standard_width
        matched_slots.append(
            {
                **layout_slot,
                "x_mm": Decimal(str(actual_slot["x_mm"])).quantize(
                    Decimal("0.001")
                ),
                "y_mm": Decimal(str(actual_slot["y_mm"])).quantize(
                    Decimal("0.001")
                ),
                "width_mm": width_mm,
                "depth_mm": depth_mm,
                "location_code": row.location_code,
                "existing_location_id": int(row.id),
                "existing_layout_version": int(layout.version),
            }
        )
    try:
        matched_slots = number_ground_physical_slots(
            matched_slots,
            numbering_origin="south",
            row_direction="from_aisle_inward",
            slot_direction="left_to_right",
        )
    except WarehouseGroundSlotError as error:
        raise WarehouseAreaActivationError(
            error.message, status_code=error.status_code
        ) from error

    configuration = {
        "target_slot_count": len(rows),
        "numbering_origin": "south",
        "row_direction": "from_aisle_inward",
        "slot_direction": "left_to_right",
        "row_start_no": 1,
        "slot_start_no": 1,
    }
    fingerprint = ground_preview_fingerprint(
        area_id=area.id,
        policy_version=policy.version,
        map_revision=current_revision,
        configuration=configuration,
        slots=matched_slots,
    )
    expected_location_ids = {int(row.id) for row in rows}
    if existing_plan is not None:
        existing_location_ids = {
            int(slot.location_id) for slot in existing_plan.slots
        }
        expected_by_location_id = {
            int(slot["existing_location_id"]): slot for slot in matched_slots
        }
        epsilon = Decimal(str(geometry_epsilon))
        geometry_matches = all(
            (
                expected := expected_by_location_id.get(int(slot.location_id))
            )
            is not None
            and abs(Decimal(str(slot.x_mm)) - Decimal(str(expected["x_mm"])))
            <= epsilon
            and abs(Decimal(str(slot.y_mm)) - Decimal(str(expected["y_mm"])))
            <= epsilon
            and int(slot.width_mm) == int(expected["width_mm"])
            and int(slot.depth_mm) == int(expected["depth_mm"])
            and int(slot.route_sequence) == int(expected["route_sequence"])
            and int(slot.row_no) == int(expected["row_no"])
            and int(slot.slot_no) == int(expected["slot_no"])
            for slot in existing_plan.slots
        )
        if (
            existing_plan.status == "published"
            and str(existing_plan.published_map_revision or "").strip()
            == current_revision
            and existing_location_ids == expected_location_ids
            and int(existing_plan.target_slot_count) == len(rows)
            and existing_plan.numbering_origin == configuration["numbering_origin"]
            and existing_plan.row_direction == configuration["row_direction"]
            and existing_plan.slot_direction == configuration["slot_direction"]
            and int(existing_plan.row_start_no) == configuration["row_start_no"]
            and int(existing_plan.slot_start_no) == configuration["slot_start_no"]
            and existing_plan.preview_fingerprint == fingerprint
            and geometry_matches
        ):
            return {
                "available_location_count": len(rows),
                "ground_plan_id": int(existing_plan.id),
                "ground_plan_status": "published",
                "location_readiness_issue": None,
            }
        if not allow_reconfigure_existing:
            raise WarehouseAreaActivationError(
                "该区域已有其他或已漂移的地堆排位事实，已停止一次确认以避免覆盖真实位置",
                status_code=409,
            )
        if db.scalar(
            select(WarehouseGroundLayoutPlanRetirement.id).where(
                WarehouseGroundLayoutPlanRetirement.plan_id == existing_plan.id
            )
        ) is not None:
            raise WarehouseAreaActivationError(
                "该区域地堆排位已经退役，不能继续修改容量",
                status_code=409,
            )
        if (preserve_existing_locations and existing_plan.status == "published"
                and existing_location_ids == expected_location_ids):
            # Published plan/slot rows are immutable. Same-location orientation
            # edits update only empty location geometry and receive a new map
            # application receipt below; never delete/recreate the original plan.
            return {
                "available_location_count": len(rows),
                "ground_plan_id": int(existing_plan.id),
                "ground_plan_status": "published",
                "location_readiness_issue": None,
                "preserved_original_plan": True,
            }
        existing_plan.slots.clear()
        db.flush()
        for slot in matched_slots:
            existing_plan.slots.append(
                WarehouseGroundLayoutSlot(
                    location_id=int(slot["existing_location_id"]),
                    route_sequence=int(slot["route_sequence"]),
                    row_no=int(slot["row_no"]),
                    slot_no=int(slot["slot_no"]),
                    x_mm=Decimal(str(slot["x_mm"])),
                    y_mm=Decimal(str(slot["y_mm"])),
                    width_mm=int(slot["width_mm"]),
                    depth_mm=int(slot["depth_mm"]),
                )
            )
        existing_plan.target_slot_count = len(rows)
        existing_plan.numbering_origin = configuration["numbering_origin"]
        existing_plan.row_direction = configuration["row_direction"]
        existing_plan.slot_direction = configuration["slot_direction"]
        existing_plan.row_start_no = configuration["row_start_no"]
        existing_plan.slot_start_no = configuration["slot_start_no"]
        existing_plan.draft_map_revision = current_revision
        existing_plan.published_map_revision = current_revision
        existing_plan.preview_fingerprint = fingerprint
        existing_plan.version += 1
        existing_plan.publish_idempotency_key = (
            "one-step-ground:"
            + hashlib.sha256(operation_key.encode("utf-8")).hexdigest()
        )
        existing_plan.publish_request_hash = ground_canonical_hash(
            {
                "operation_key": operation_key,
                "area_id": area.id,
                "map_revision": current_revision,
                "preview_fingerprint": fingerprint,
            }
        )
        now = beijing_now_naive()
        existing_plan.updated_by = operator_id
        existing_plan.published_by = operator_id
        existing_plan.updated_at = now
        existing_plan.published_at = now
        db.flush()
        return {
            "available_location_count": len(rows),
            "ground_plan_id": int(existing_plan.id),
            "ground_plan_status": "published",
            "location_readiness_issue": None,
        }

    idempotency_key = (
        "one-step-ground:"
        + hashlib.sha256(operation_key.encode("utf-8")).hexdigest()
    )
    request_hash = ground_canonical_hash(
        {
            "operation_key": operation_key,
            "area_id": area.id,
            "map_revision": current_revision,
            "preview_fingerprint": fingerprint,
        }
    )
    now = beijing_now_naive()
    plan = WarehouseGroundLayoutPlan(
        area_id=area.id,
        status="published",
        draft_map_revision=current_revision,
        published_map_revision=current_revision,
        preview_fingerprint=fingerprint,
        version=1,
        publish_idempotency_key=idempotency_key,
        publish_request_hash=request_hash,
        updated_by=operator_id,
        published_by=operator_id,
        published_at=now,
        updated_at=now,
        **configuration,
    )
    db.add(plan)
    db.flush()
    for slot in matched_slots:
        db.add(
            WarehouseGroundLayoutSlot(
                plan_id=plan.id,
                location_id=int(slot["existing_location_id"]),
                route_sequence=int(slot["route_sequence"]),
                row_no=int(slot["row_no"]),
                slot_no=int(slot["slot_no"]),
                x_mm=Decimal(str(slot["x_mm"])),
                y_mm=Decimal(str(slot["y_mm"])),
                width_mm=int(slot["width_mm"]),
                depth_mm=int(slot["depth_mm"]),
            )
        )
    db.flush()
    return {
        "available_location_count": len(rows),
        "ground_plan_id": int(plan.id),
        "ground_plan_status": "published",
        "location_readiness_issue": None,
    }


@router.post('/twin-layout/floors/{floor_code}/zones/{feature_id}/confirm-area')
def confirm_twin_zone_area(
    floor_code: str,
    feature_id: str,
    payload: TwinZoneConfirmAreaPayload,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    """Confirm one measured zone and publish its formal storage result atomically."""

    with WAREHOUSE_TWIN_LAYOUT_TRANSACTION_LOCK:
        _claim_floor_projection_for_layout_write(db, floor_code=floor_code)
        publish_snapshot = snapshot_warehouse_twin_publish_state()
        result: dict | None = None
        advanced_draft_preserved = False
        try:
            effective_floor = load_effective_warehouse_twin_floor_for_edit(floor_code)
            expected_revision = str(effective_floor.get('revision') or '')
            if expected_revision != payload.expected_revision:
                raise HTTPException(
                    status_code=409,
                    detail='地图或区域已被其他操作更新，请刷新后重新确认',
                )
            current_feature = next(
                (
                    item
                    for item in effective_floor.get('features') or []
                    if str(item.get('id') or '') == feature_id
                ),
                None,
            )
            if current_feature is None or current_feature.get('feature_kind') != 'zone':
                raise HTTPException(status_code=404, detail='区域不存在或已被删除')
            if payload.expected_revision != payload.expected_published_revision:
                draft_control = effective_floor.get('draft_control') or {}
                if str(draft_control.get('published_revision') or '') != payload.expected_published_revision:
                    raise HTTPException(status_code=409, detail='正式地图版本不一致，请刷新后重试')
            try:
                one_step_context = begin_warehouse_twin_one_step_publish(
                    floor_code,
                    feature_id,
                    expected_effective_revision=payload.expected_revision,
                    expected_published_revision=payload.expected_published_revision,
                )
            except WarehouseTwinLayoutEditError as error:
                _handle_twin_layout_edit_error(error)
            policy_result = _update_twin_zone_storage_policy_locked(
                floor_code=floor_code,
                feature_id=feature_id,
                payload=TwinZoneStoragePolicyPayload(
                    expected_revision=one_step_context.published_floor_revision,
                    expected_version=one_step_context.published_feature_version,
                    operation_key=f'{payload.operation_key}-policy',
                    allowed_inventory_types=[payload.primary_inventory_type],
                    storage_layout=payload.storage_layout,
                    pallet_rotation_deg=payload.pallet_rotation_deg,
                    erp_area_code=payload.erp_area_code,
                    area_name=payload.area_name,
                    existing_area_id=payload.existing_area_id,
                    max_rack_count=(
                        payload.max_pallet_capacity
                        if payload.storage_layout == 'rack'
                        else None
                    ),
                ),
                request=request,
                db=db,
                user=user,
                commit=False,
                allow_legacy_v11_name_only=False,
            )
            draft_revision = str(policy_result.get('revision') or '')
            identity_blockers = _formal_area_identity_blockers(db, floor_code)
            if identity_blockers:
                raise HTTPException(
                    status_code=409,
                    detail='正式区域身份校验未通过：' + '；'.join(identity_blockers[:5]),
                )
            validation = validate_warehouse_twin_layout_draft(
                floor_code,
                expected_revision=draft_revision,
            )
            if validation.value.get('blockers'):
                raise HTTPException(
                    status_code=409,
                    detail='区域无法启用：' + '；'.join(validation.value['blockers'][:5]),
                )
            result = _publish_twin_layout_draft_locked(
                floor_code=floor_code,
                payload=TwinLayoutDraftPublishPayload(
                    expected_published_revision=payload.expected_published_revision,
                    expected_draft_revision=draft_revision,
                    operation_key=f'{payload.operation_key}-publish',
                ),
                request=request,
                db=db,
                user=user,
                commit=False,
                defer_location_readiness_for_feature_id=feature_id,
                floor_projection_claimed=True,
                allow_archived_tombstone_cleanup=True,
            )
            advanced_draft_preserved = rebase_warehouse_twin_advanced_draft_after_one_step(
                one_step_context,
                floor_code,
                feature_id,
                published_feature_snapshot=policy_result['item'],
            )
            area = db.scalar(
                select(WarehouseArea)
                .join(WarehouseAreaStoragePolicy)
                .where(WarehouseAreaStoragePolicy.map_feature_id == feature_id)
                .options(
                    selectinload(WarehouseArea.floor),
                    selectinload(WarehouseArea.storage_policy),
                )
            )
            if area is None or area.area_code.upper() != payload.erp_area_code:
                raise HTTPException(status_code=409, detail='区域发布后正式身份回读失败')
            before_capacity = {
                'planned_pallet_capacity': area.planned_pallet_capacity,
                'capacity_review_status': area.capacity_review_status,
                'capacity_eligible': area.capacity_eligible,
                'confirmed_pallet_capacity': area.confirmed_pallet_capacity,
                'capacity_reviewed_by': area.capacity_reviewed_by,
                'capacity_reviewed_at': area.capacity_reviewed_at,
            }
            area.planned_pallet_capacity = payload.max_pallet_capacity
            area.capacity_review_status = (
                'confirmed' if payload.max_pallet_capacity > 0 else 'excluded'
            )
            area.capacity_eligible = payload.max_pallet_capacity > 0
            area.confirmed_pallet_capacity = (
                payload.max_pallet_capacity if payload.max_pallet_capacity > 0 else None
            )
            _apply_capacity_review(area, user=user, review_changed=True)
            # An explicitly reused area may have had its empty relationship
            # loaded before the publish step created the policy.  Refresh that
            # identity before creating physical pallet positions.
            db.expire(area, ['storage_policy'])
            published_floor_layout = load_warehouse_twin_floor(floor_code)
            existing_ground_plan_id = db.scalar(
                select(WarehouseGroundLayoutPlan.id).where(
                    WarehouseGroundLayoutPlan.area_id == area.id
                )
            )
            active_location_count = sum(
                1
                for row in formal_area_location_rows(
                    db, floor=area.floor, area=area
                )
                if row.is_active
            )
            if (
                existing_ground_plan_id is None
                or active_location_count != payload.max_pallet_capacity
                or (
                    payload.storage_layout == 'pallet_ground'
                    and current_feature.get('pallet_rotation_deg', 0) != payload.pallet_rotation_deg
                )
            ):
                (
                    created_locations,
                    planned_location_count,
                    location_layout_result,
                ) = _ensure_one_step_pallet_locations(
                    db,
                    floor_layout=published_floor_layout,
                    feature_id=feature_id,
                    area=area,
                    inventory_type=payload.primary_inventory_type,
                    storage_layout=payload.storage_layout,
                    target_count=payload.max_pallet_capacity,
                    operator_id=user.id,
                )
            else:
                created_locations = []
                planned_location_count = active_location_count
                location_layout_result = {
                    "source_version": AREA_LOCATION_SOURCE_VERSION,
                    "enabled_ids": [],
                    "disabled_ids": [],
                    "reflow": None,
                }
            ground_readiness = _ensure_one_step_ground_plan(
                db,
                floor_layout=published_floor_layout,
                feature_id=feature_id,
                area=area,
                storage_layout=payload.storage_layout,
                location_count=planned_location_count,
                operation_key=payload.operation_key,
                operator_id=user.id,
                allow_reconfigure_existing=existing_ground_plan_id is not None,
                preserve_existing_locations=(
                    existing_ground_plan_id is not None
                    and active_location_count == payload.max_pallet_capacity
                ),
            )
            if ground_readiness.get("preserved_original_plan"):
                from app.services.warehouse_ground_map_application import record_map_applications
                record_map_applications(
                    db, floor_layout=published_floor_layout,
                    previous_floor_layout=published_floor_layout,
                    actor=user, operation_key=f"{payload.operation_key}-orientation",
                    request=request,
                )
            available_location_count = int(
                ground_readiness["available_location_count"]
            )
            for change in (location_layout_result.get("reflow") or {}).get(
                "changes", []
            ):
                location = change["location"]
                _floor3_layout_log(
                    db,
                    request=request,
                    user=user,
                    action="UPDATE",
                    location=location,
                    description="区域一次确认后均匀排布空闲系统货位",
                    details={
                        "floor_code": floor_code,
                        "area_code": area.area_code,
                        "before": change["before"],
                        "after": change["after"],
                        "inventory_changed": False,
                        "pallet_binding_changed": False,
                    },
                )
            after_capacity = {
                'planned_pallet_capacity': area.planned_pallet_capacity,
                'capacity_review_status': area.capacity_review_status,
                'capacity_eligible': area.capacity_eligible,
                'confirmed_pallet_capacity': area.confirmed_pallet_capacity,
                'capacity_reviewed_by': area.capacity_reviewed_by,
                'capacity_reviewed_at': area.capacity_reviewed_at,
            }
            _warehouse_capacity_log(
                db,
                request=request,
                user=user,
                action='warehouse_area_one_step_confirm',
                entity_type='warehouse_area',
                entity_id=area.id,
                object_ref=f'{area.floor.floor_code}/{area.area_code}',
                before=before_capacity,
                after=after_capacity,
            )
            _twin_layout_asset_log(
                db,
                request=request,
                user=user,
                action='TWIN_ZONE_ONE_STEP_CONFIRM',
                entity_type='twin_zone',
                entity_id=feature_id,
                description='管理员一次确认并启用仓库区域',
                details={
                    'floor_code': floor_code,
                    'area_id': area.id,
                    'area_code': area.area_code,
                    'primary_inventory_type': payload.primary_inventory_type,
                    'storage_layout': payload.storage_layout,
                    'max_pallet_capacity': payload.max_pallet_capacity,
                    'created_location_count': len(created_locations),
                    'available_location_count': available_location_count,
                    'ground_plan_id': ground_readiness.get('ground_plan_id'),
                    'ground_plan_status': ground_readiness.get('ground_plan_status'),
                    'location_readiness_issue': ground_readiness.get(
                        'location_readiness_issue'
                    ),
                    'location_source_version': location_layout_result.get('source_version'),
                    'enabled_location_ids': location_layout_result.get('enabled_ids', []),
                    'disabled_location_ids': location_layout_result.get('disabled_ids', []),
                    'reflowed_location_ids': [
                        change['location'].id
                        for change in (location_layout_result.get('reflow') or {}).get('changes', [])
                    ],
                    'historical_adopted_count': (
                        location_layout_result.get('reflow') or {}
                    ).get('historical_adopted_count', 0),
                    'published_revision': result.get('published_revision'),
                    'advanced_draft_preserved': advanced_draft_preserved,
                    'inventory_changed': False,
                    'pallet_binding_changed': False,
                },
            )
            db.flush()
            db.expire(area, ['storage_policy'])
            area_payload = _warehouse_area_dict(db, area)
            response_payload = {
                **result,
                'area': area_payload,
                'message': (
                    f'{area.area_code} {area.area_name} 已确认并启用'
                    + (
                        f'；已生成 {available_location_count} 个可移动空货位'
                        if available_location_count
                        else ''
                    )
                    + (
                        f"；{ground_readiness['location_readiness_issue']}"
                        if ground_readiness.get('location_readiness_issue')
                        else ''
                    )
                ),
                'advanced_draft_preserved': advanced_draft_preserved,
                'created_location_count': len(created_locations),
                'available_location_count': available_location_count,
                'ground_plan_id': ground_readiness.get('ground_plan_id'),
                'ground_plan_status': ground_readiness.get('ground_plan_status'),
                'location_readiness_issue': ground_readiness.get(
                    'location_readiness_issue'
                ),
                'inventory_changed': False,
                'pallet_binding_changed': False,
            }
            db.commit()
            return response_payload
        except HTTPException:
            try:
                restore_warehouse_twin_publish_state(
                    publish_snapshot,
                    backup_name=(result.get('backup_name') if result else None),
                )
            finally:
                db.rollback()
            raise
        except (WarehouseTwinLayoutEditError, WarehouseAreaActivationError) as error:
            try:
                restore_warehouse_twin_publish_state(
                    publish_snapshot,
                    backup_name=(result.get('backup_name') if result else None),
                )
            finally:
                db.rollback()
            status_code = getattr(error, 'status_code', 409)
            raise HTTPException(status_code=status_code, detail=str(error)) from error
        except IntegrityError as error:
            try:
                restore_warehouse_twin_publish_state(
                    publish_snapshot,
                    backup_name=(result.get('backup_name') if result else None),
                )
            finally:
                db.rollback()
            raise HTTPException(
                status_code=409,
                detail='货位状态与库存或实体栈板引用发生冲突，请刷新后重试',
            ) from error
        except Exception:
            try:
                restore_warehouse_twin_publish_state(
                    publish_snapshot,
                    backup_name=(result.get('backup_name') if result else None),
                )
            finally:
                db.rollback()
            raise


class TwinProductionMappingPayload(BaseModel):
    target_kind: Literal["pallet", "zone"]
    target_id: str = Field(min_length=1, max_length=80)
    version: int | None = Field(default=None, ge=1)


def _production_task_dates(
    db: Session,
    task_ids: list[int],
) -> dict[int, dict[str, str | None]]:
    if not task_ids:
        return {}
    rows = db.execute(
        select(
            ProductionTask.id,
            ProductionTask.updated_at,
            ProductionTask.created_at,
            Order.delivery_date,
        )
        .join(OrderItem, OrderItem.id == ProductionTask.order_item_id)
        .join(Order, Order.id == OrderItem.order_id)
        .where(ProductionTask.id.in_(task_ids))
    ).all()
    return {
        int(task_id): {
            "task_updated_at": utc_naive_to_api(updated_at or created_at),
            "delivery_date": delivery_date.isoformat() if delivery_date else None,
        }
        for task_id, updated_at, created_at, delivery_date in rows
    }


def _current_visible_production_tasks(user: User, db: Session) -> list[dict]:
    return list_production_tasks(
        db,
        allowed_customer_ids=_visible_customer_ids(user, db),
        status=PENDING,
    )


def _visible_production_task_ids(user: User, db: Session) -> set[int] | None:
    visible_customer_ids = _visible_customer_ids(user, db)
    if visible_customer_ids is None:
        return None
    return set(
        db.scalars(
            select(ProductionTask.id)
            .join(OrderItem, OrderItem.id == ProductionTask.order_item_id)
            .join(Order, Order.id == OrderItem.order_id)
            .where(Order.customer_id.in_(visible_customer_ids))
        ).all()
    )


def _raise_twin_production_error(error: WarehouseTwinProductionError) -> None:
    raise HTTPException(status_code=error.status_code, detail=str(error)) from error


@router.get("/twin-production/layouts/{layout_id}/tasks")
def get_warehouse_twin_production_tasks(
    layout_id: str,
    user: User = Depends(can_read),
    _orders_user: User = Depends(can_read_orders),
    db: Session = Depends(get_db),
) -> dict:
    try:
        floor = overlay_formal_area_bindings(
            db,
            floor_code="1F",
            floor_layout=load_warehouse_twin_floor("1F"),
            include_draft=False,
        )
        if str(floor["layout_id"]) != layout_id:
            raise HTTPException(status_code=404, detail="一楼数字孪生布局不存在")
        tasks = _current_visible_production_tasks(user, db)
        dates = _production_task_dates(db, [int(item["id"]) for item in tasks])
        payload = build_production_projection(
            floor=floor, tasks=tasks, task_dates=dates
        )
        visible_task_ids = _visible_production_task_ids(user, db)
        if visible_task_ids is not None:
            payload["stale_mappings"] = [
                item
                for item in payload["stale_mappings"]
                if int(item["source_task_id"]) in visible_task_ids
            ]
        return payload
    except WarehouseTwinLayoutNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except WarehouseTwinProductionError as error:
        _raise_twin_production_error(error)


@router.put("/twin-production/layouts/{layout_id}/tasks/{source_task_id}")
def put_warehouse_twin_production_mapping(
    layout_id: str,
    source_task_id: int,
    body: TwinProductionMappingPayload,
    user: User = Depends(can_operate),
    _orders_user: User = Depends(can_read_orders),
    db: Session = Depends(get_db),
) -> dict:
    with WAREHOUSE_TWIN_LAYOUT_TRANSACTION_LOCK:
        try:
            _claim_floor_projection_for_layout_write(db, floor_code="1F")
            floor = overlay_formal_area_bindings(
                db,
                floor_code="1F",
                floor_layout=load_warehouse_twin_floor("1F"),
                include_draft=False,
            )
            if str(floor["layout_id"]) != layout_id:
                raise HTTPException(status_code=404, detail="一楼数字孪生布局不存在")
            current_ids = {
                int(item["id"]) for item in _current_visible_production_tasks(user, db)
            }
            if source_task_id not in current_ids:
                raise HTTPException(
                    status_code=409,
                    detail="该任务已不在当前账号可见的ERP待生产清单中，请刷新",
                )
            result = save_production_projection_mapping(
                floor=floor,
                source_task_id=source_task_id,
                target_kind=body.target_kind,
                target_id=body.target_id,
                expected_version=body.version,
            )
            db.commit()
            return result
        except WarehouseTwinLayoutNotFoundError as error:
            db.rollback()
            raise HTTPException(status_code=404, detail=str(error)) from error
        except WarehouseTwinProductionError as error:
            db.rollback()
            _raise_twin_production_error(error)
        except Exception:
            db.rollback()
            raise


@router.delete(
    "/twin-production/layouts/{layout_id}/tasks/{source_task_id}",
    status_code=204,
)
def delete_warehouse_twin_production_mapping(
    layout_id: str,
    source_task_id: int,
    version: int = Query(ge=1),
    user: User = Depends(can_operate),
    _orders_user: User = Depends(can_read_orders),
    db: Session = Depends(get_db),
):
    try:
        floor = load_warehouse_twin_floor("1F")
        if str(floor["layout_id"]) != layout_id:
            raise HTTPException(status_code=404, detail="一楼数字孪生布局不存在")
        # Customer scope is evaluated even for unbinding so scoped users cannot
        # use stale task ids as a side channel.
        visible_ids = {
            int(item["id"]) for item in _current_visible_production_tasks(user, db)
        }
        if source_task_id not in visible_ids:
            raise HTTPException(
                status_code=409,
                detail="该任务已不在当前账号可见的ERP待生产清单中，请刷新",
            )
        delete_production_projection_mapping(
            layout_id=layout_id,
            source_task_id=source_task_id,
            expected_version=version,
        )
    except WarehouseTwinLayoutNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except WarehouseTwinProductionError as error:
        _raise_twin_production_error(error)


def _twin_dashboard_source_rows(
    db: Session,
    user: User,
) -> tuple[
    list[InventoryLot],
    list[WarehouseLocation],
    list[InventoryPallet],
    list[WarehouseFloor],
    set[int] | None,
]:
    visible_customer_ids = _twin_locator_visible_customer_ids(db, user)
    lot_query = _lot_query(require_formal_location=False)
    if visible_customer_ids is not None:
        lot_query = lot_query.where(_visible_lot_condition(visible_customer_ids))
    lot_query = lot_query.options(
        selectinload(InventoryLot.finished_detail).selectinload(
            FinishedGoodsInventoryDetail.customer
        ),
        selectinload(InventoryLot.semi_finished_detail).selectinload(
            SemiFinishedInventoryDetail.customer
        ),
    )
    lots = list(db.scalars(lot_query.order_by(InventoryLot.id)).unique().all())
    locations = list(
        db.scalars(
            select(WarehouseLocation)
            .where(
                _formal_inventory_location_condition(),
                WarehouseLocation.is_active.is_(True),
                ~select(WarehouseArea.id)
                .where(
                    WarehouseArea.id == WarehouseLocation.address_area_id,
                    WarehouseArea.construction_status == "archived",
                )
                .exists(),
                ~select(WarehouseAreaStoragePolicy.id)
                .where(
                    WarehouseAreaStoragePolicy.area_id
                    == WarehouseLocation.address_area_id,
                    WarehouseAreaStoragePolicy.status == "archived",
                )
                .exists(),
            )
            .options(
                selectinload(WarehouseLocation.floor3_layout),
                selectinload(WarehouseLocation.address_area).selectinload(
                    WarehouseArea.floor
                ),
            )
            .order_by(WarehouseLocation.sort_order, WarehouseLocation.location_code)
        ).all()
    )
    pallets = list(
        db.scalars(
            select(InventoryPallet)
            .join(WarehouseLocation, WarehouseLocation.id == InventoryPallet.location_id)
            .where(
                InventoryPallet.is_current.is_(True),
                InventoryPallet.status == "active",
                WarehouseLocation.is_active.is_(True),
                _formal_inventory_location_condition(),
                ~select(WarehouseArea.id)
                .where(
                    WarehouseArea.id == WarehouseLocation.address_area_id,
                    WarehouseArea.construction_status == "archived",
                )
                .exists(),
                ~select(WarehouseAreaStoragePolicy.id)
                .where(
                    WarehouseAreaStoragePolicy.area_id
                    == WarehouseLocation.address_area_id,
                    WarehouseAreaStoragePolicy.status == "archived",
                )
                .exists(),
            )
            .options(selectinload(InventoryPallet.items))
            .order_by(InventoryPallet.id)
        ).unique().all()
    )
    floors = list(
        db.scalars(
            select(WarehouseFloor)
            .options(selectinload(WarehouseFloor.areas))
            .order_by(WarehouseFloor.floor_number)
        ).unique().all()
    )
    return lots, locations, pallets, floors, visible_customer_ids


@router.get("/twin-dashboard/overview")
def get_warehouse_twin_dashboard(
    days: int = Query(default=30),
    db: Session = Depends(get_db),
    user: User = Depends(_can_locate_twin),
    dispatch_idle_days: int = Query(default=3, ge=1, le=30),
) -> dict:
    if days not in {7, 30, 90}:
        raise HTTPException(status_code=422, detail="时间范围仅支持7、30或90天")
    lots, locations, pallets, floors, visible_customer_ids = (
        _twin_dashboard_source_rows(db, user)
    )
    decrease_issues = stocktake_decrease_issues(db, lots)
    return build_warehouse_twin_dashboard(
        db,
        lots=lots,
        locations=locations,
        pallets=pallets,
        floors=floors,
        visible_customer_ids=visible_customer_ids,
        days=days,
        as_of=beijing_today(),
        dispatch_idle_days=dispatch_idle_days,
        stocktake_decrease_issues=decrease_issues,
    )


@router.get("/capacity/summary")
def get_warehouse_capacity_summary(
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    if _twin_locator_visible_customer_ids(db, user) is not None:
        return {
            "visible": False,
            "notice": "当前账号按客户范围查看库存，不显示全仓容量。",
            "floors": [],
        }
    floors = list(
        db.scalars(
            select(WarehouseFloor)
            .options(selectinload(WarehouseFloor.areas))
            .where(WarehouseFloor.floor_number.in_((1, 3, 4)))
            .order_by(WarehouseFloor.floor_number)
        ).all()
    )
    occupied_by_floor = {
        int(floor_number): int(count or 0)
        for floor_number, count in db.execute(
            select(
                WarehouseLocation.warehouse_floor,
                func.count(InventoryPallet.id),
            )
            .join(
                InventoryPallet,
                InventoryPallet.location_id == WarehouseLocation.id,
            )
            .where(
                InventoryPallet.is_current.is_(True),
                InventoryPallet.status == "active",
                pallet_has_physical_goods_condition(InventoryPallet.id),
                _formal_inventory_location_condition(),
                WarehouseLocation.warehouse_floor.in_((1, 3, 4)),
            )
            .group_by(WarehouseLocation.warehouse_floor)
        ).all()
        if floor_number is not None
    }
    floor_items = []
    for floor in floors:
        capacity = warehouse_capacity_summary(
            floor,
            occupied_pallets=occupied_by_floor.get(floor.floor_number, 0),
            visible=True,
        )
        floor_items.append(
            {
                "floor_code": floor.floor_code,
                "floor_name": floor.floor_name,
                "floor_number": floor.floor_number,
                "capacity": capacity,
            }
        )
    all_confirmed = bool(floor_items) and all(
        row["capacity"]["confirmed"] for row in floor_items
    )
    if not all_confirmed:
        for row in floor_items:
            row["capacity"] = suppress_capacity_metrics_until_all_confirmed(
                row["capacity"]
            )
    reference_total = sum(
        int(row["capacity"]["reference_pallet_capacity"] or 0) for row in floor_items
    )
    occupied_total = sum(int(row["capacity"]["occupied_pallets"] or 0) for row in floor_items)
    tightest = max(
        (
            row
            for row in floor_items
            if row["capacity"]["utilization_percent"] is not None
        ),
        key=lambda row: float(row["capacity"]["utilization_percent"]),
        default=None,
    )
    forecast = (
        build_warehouse_capacity_forecast(db, horizon=7, as_of=beijing_today())
        if all_confirmed
        else None
    )
    forecast_tightest = max(
        (
            row
            for row in (forecast or {}).get("floors", [])
            if row["peak_utilization_percent"] is not None
        ),
        key=lambda row: float(row["peak_utilization_percent"]),
        default=None,
    )
    return {
        "visible": True,
        "confirmed": all_confirmed,
        "planned_pallet_capacity": sum(
            int(row["capacity"]["planned_pallet_capacity"] or 0) for row in floor_items
        ),
        "reference_pallet_capacity": reference_total or None,
        "occupied_pallets": occupied_total,
        "empty_pallet_slots": max(reference_total - occupied_total, 0) if all_confirmed else None,
        "utilization_percent": (
            round(occupied_total * 100 / reference_total, 1)
            if all_confirmed and reference_total else None
        ),
        "tightest_floor_code": tightest["floor_code"] if tightest else None,
        "tightest_floor_utilization_percent": (
            tightest["capacity"]["utilization_percent"] if tightest else None
        ),
        "alert_count": sum(
            row["capacity"]["alert_level"] not in {"normal", "unknown"}
            for row in floor_items
        ) if all_confirmed else 0,
        "forecast_7d_complete": forecast["forecast_complete"] if forecast else False,
        "forecast_7d_status_label": forecast["forecast_status_label"] if forecast else "现场安全容量待确认",
        "forecast_7d_peak_floor_code": (
            forecast_tightest["floor_code"] if forecast_tightest else None
        ),
        "forecast_7d_peak_utilization_percent": (
            forecast_tightest["peak_utilization_percent"] if forecast_tightest else None
        ),
        "forecast_7d_action_count": len(forecast["actions"]) if forecast else 0,
        "floors": floor_items,
        "notice": (
            "只读取正式栈板和已确认安全容量。"
            if all_confirmed
            else "现场安全容量待确认；规划值仅供整理参考，不参与满载率、空位、阈值或预测。"
        ),
    }


def _capacity_forecast_request_hash(payload: dict) -> str:
    rendered = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def _capacity_forecast_scope_key(effect: str, floor_id: int | None) -> str:
    return "none" if effect == "no_storage" else f"floor:{int(floor_id or 0)}"


def _capacity_forecast_audit(
    db: Session,
    *,
    request: Request,
    user: User,
    action_code: str,
    plan: WarehouseCapacityForecastPlan,
    before: dict | None,
    after: dict,
) -> None:
    append_audit_event(
        db,
        request=request,
        actor=user,
        event_category="system",
        result="success",
        source="web",
        module_code="warehouse",
        action_code=action_code,
        legacy_action="CAP_FORECAST",
        resource=f"warehouse/capacity-forecast/{plan.id}",
        entity_type="warehouse_capacity_forecast_plan",
        entity_id=plan.id,
        object_ref=f"{plan.source_type}:{plan.source_id}:{plan.scope_key}",
        description="仓储容量预测计划已更新；不改变任何库存事实",
        details={"before": before, "after": after},
    )


@router.get("/capacity/forecast")
def get_warehouse_capacity_forecast(
    horizon: int = Query(default=7),
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    if horizon not in {7, 14, 30}:
        raise HTTPException(status_code=422, detail="容量预测仅支持 7、14、30 天")
    if _twin_locator_visible_customer_ids(db, user) is not None:
        return {
            "visible": False,
            "notice": "当前账号按客户范围查看库存，不显示全仓容量预测。",
            "horizon_days": horizon,
            "floors": [],
            "plans": [],
            "missing_sources": [],
            "stale_plans": [],
            "actions": [],
        }
    floors = list(
        db.scalars(
            select(WarehouseFloor)
            .options(selectinload(WarehouseFloor.areas))
            .where(WarehouseFloor.floor_number.in_((1, 3, 4)))
            .order_by(WarehouseFloor.floor_number)
        ).all()
    )
    if not floors or any(
        not warehouse_capacity_summary(floor, occupied_pallets=0, visible=True)["confirmed"]
        for floor in floors
    ):
        return {
            "visible": True,
            "confirmed": False,
            "notice": "现场安全容量尚未全部确认；预测暂不发布。",
            "horizon_days": horizon,
            "forecast_complete": False,
            "forecast_status_label": "现场安全容量待确认",
            "floors": [],
            "plans": [],
            "missing_sources": [],
            "stale_plans": [],
            "actions": [],
        }
    return build_warehouse_capacity_forecast(
        db,
        horizon=horizon,
        as_of=beijing_today(),
    )


@router.post("/capacity/forecast-plans")
def save_warehouse_capacity_forecast_plan(
    payload: WarehouseCapacityForecastPlanPayload,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    today = beijing_today()
    if payload.planned_date < today - timedelta(days=30) or payload.planned_date > today + timedelta(days=365):
        raise HTTPException(status_code=422, detail="预测日期只能填写近 30 天到未来 365 天")
    source = resolve_capacity_forecast_source(db, payload.source_type, payload.source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="对应业务单据不存在")
    if not source["valid"]:
        raise HTTPException(status_code=409, detail="对应业务单据已完成、作废或数量无效，不能再加入预测")
    floor = None
    if payload.floor_id is not None:
        floor = db.get(WarehouseFloor, payload.floor_id)
        if floor is None or floor.floor_number not in {1, 3, 4}:
            raise HTTPException(
                status_code=422,
                detail="容量预测只允许选择已建档的一楼、三楼或四楼",
            )
    scope_key = _capacity_forecast_scope_key(payload.effect, payload.floor_id)
    request_payload = payload.model_dump(mode="json", exclude={"operation_key"})
    request_hash = _capacity_forecast_request_hash(request_payload)
    row = db.scalar(
        select(WarehouseCapacityForecastPlan).where(
            WarehouseCapacityForecastPlan.source_type == payload.source_type,
            WarehouseCapacityForecastPlan.source_id == payload.source_id,
            WarehouseCapacityForecastPlan.scope_key == scope_key,
        )
    )
    before = None
    if row is not None:
        if row.last_operation_key == payload.operation_key:
            if row.last_request_hash != request_hash:
                raise HTTPException(status_code=409, detail="同一操作键不能用于不同的预测内容")
            return {
                "ok": True,
                "replayed": True,
                "plan": serialize_capacity_forecast_plan(row, floor=floor, source_valid=True),
            }
        if payload.expected_version is None or payload.expected_version != row.version:
            raise HTTPException(status_code=409, detail="预测资料已被修改，请刷新后再保存")
        before = serialize_capacity_forecast_plan(
            row,
            floor=db.get(WarehouseFloor, row.floor_id) if row.floor_id else None,
            source_valid=True,
        )
        row.source_number_snapshot = source["source_number"]
        row.source_label_snapshot = source["source_label"]
        row.effect = payload.effect
        row.floor_id = payload.floor_id
        row.planned_date = payload.planned_date
        row.pallet_slots = payload.pallet_slots
        row.status = "active"
        row.cancelled_by = None
        row.cancelled_at = None
        row.version += 1
        row.updated_by = user.id
        row.updated_at = beijing_now_naive()
        row.last_operation_key = payload.operation_key
        row.last_request_hash = request_hash
    else:
        if payload.expected_version is not None:
            raise HTTPException(status_code=409, detail="预测资料不存在，请刷新后重新操作")
        row = WarehouseCapacityForecastPlan(
            source_type=payload.source_type,
            source_id=payload.source_id,
            source_number_snapshot=source["source_number"],
            source_label_snapshot=source["source_label"],
            effect=payload.effect,
            floor_id=payload.floor_id,
            scope_key=scope_key,
            planned_date=payload.planned_date,
            pallet_slots=payload.pallet_slots,
            status="active",
            version=1,
            last_operation_key=payload.operation_key,
            last_request_hash=request_hash,
            created_by=user.id,
            updated_by=user.id,
            updated_at=beijing_now_naive(),
        )
        db.add(row)
    try:
        db.flush()
        after = serialize_capacity_forecast_plan(row, floor=floor, source_valid=True)
        _capacity_forecast_audit(
            db,
            request=request,
            user=user,
            action_code="warehouse_capacity_forecast_save",
            plan=row,
            before=before,
            after=after,
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="预测资料已存在，请刷新后再保存") from exc
    db.refresh(row)
    return {
        "ok": True,
        "replayed": False,
        "plan": serialize_capacity_forecast_plan(row, floor=floor, source_valid=True),
    }


@router.post("/capacity/forecast-plans/{plan_id}/cancel")
def cancel_warehouse_capacity_forecast_plan(
    plan_id: int,
    payload: WarehouseCapacityForecastCancelPayload,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    row = db.get(WarehouseCapacityForecastPlan, plan_id)
    if row is None:
        raise HTTPException(status_code=404, detail="预测资料不存在")
    request_hash = _capacity_forecast_request_hash(
        {"plan_id": plan_id, "expected_version": payload.expected_version, "action": "cancel"}
    )
    if row.last_operation_key == payload.operation_key:
        if row.last_request_hash != request_hash:
            raise HTTPException(status_code=409, detail="同一操作键不能用于不同操作")
        return {"ok": True, "replayed": True, "plan_id": row.id, "version": row.version}
    if row.status != "active":
        raise HTTPException(status_code=409, detail="该预测资料已经取消")
    if row.version != payload.expected_version:
        raise HTTPException(status_code=409, detail="预测资料已被修改，请刷新后再取消")
    before = serialize_capacity_forecast_plan(
        row,
        floor=db.get(WarehouseFloor, row.floor_id) if row.floor_id else None,
        source_valid=None,
    )
    row.status = "cancelled"
    row.cancelled_by = user.id
    row.cancelled_at = beijing_now_naive()
    row.updated_by = user.id
    row.updated_at = row.cancelled_at
    row.version += 1
    row.last_operation_key = payload.operation_key
    row.last_request_hash = request_hash
    db.flush()
    after = serialize_capacity_forecast_plan(
        row,
        floor=db.get(WarehouseFloor, row.floor_id) if row.floor_id else None,
        source_valid=None,
    )
    _capacity_forecast_audit(
        db,
        request=request,
        user=user,
        action_code="warehouse_capacity_forecast_cancel",
        plan=row,
        before=before,
        after=after,
    )
    db.commit()
    return {"ok": True, "replayed": False, "plan_id": row.id, "version": row.version}


@router.get("/twin-dashboard/search")
def search_warehouse_twin_inventory(
    keyword: str | None = Query(default=None, max_length=150),
    inventory_code: str | None = Query(default=None, max_length=150),
    page_size: int = Query(default=100, ge=1, le=500),
    after_lot_id: int | None = Query(default=None, gt=0),
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    effective_keyword = str(keyword or inventory_code or "").strip()
    if len(effective_keyword) < 2:
        raise HTTPException(status_code=422, detail="全仓查找至少输入2个字符")
    query = (
        _lot_query(require_formal_location=False)
        .where(InventoryLot.status.in_(("active", "frozen")))
        .options(
            selectinload(InventoryLot.finished_detail).selectinload(
                FinishedGoodsInventoryDetail.customer
            ),
            selectinload(InventoryLot.semi_finished_detail).selectinload(
                SemiFinishedInventoryDetail.customer
            ),
        )
    )
    visible_customer_ids = _visible_customer_ids(user, db)
    if visible_customer_ids is not None:
        query = query.where(_visible_lot_condition(visible_customer_ids))
    from app.services.warehouse_search_paging import search_lot_page
    today = beijing_today()
    lots, pagination = search_lot_page(db, query, keyword=effective_keyword,
        as_of=today, page_size=page_size, after_lot_id=after_lot_id)
    result = build_inventory_code_search_results(
        lots=lots,
        keyword=effective_keyword,
        as_of=today,
        location_projection_contexts=load_warehouse_location_projection_contexts(
            db,
            [row.location for row in lots if row.location is not None],
        ),
    )
    result["pagination"] = pagination
    try:
        location_match = resolve_location_address(db, effective_keyword)
        matched_location = db.get(
            WarehouseLocation,
            int(location_match["location_id"]),
        )
        if matched_location is not None:
            match_context = load_warehouse_location_projection_contexts(
                db, [matched_location]
            ).get(int(matched_location.id), {})
            match_projection = warehouse_location_projection(
                matched_location,
                **match_context,
            )
            location_match.update(match_projection)
            location_match["current"] = location_address_payload(
                matched_location,
                area=match_context.get("area"),
                floor=match_context.get("floor"),
                position_status=str(match_projection["position_status"]),
                area_sequence=(int(match_context["area_sequence"]) if match_context.get("area_sequence") else None),
            )
        result["location_match"] = location_match
    except WarehouseLocationAddressError as error:
        result["location_match"] = None
        result["location_lookup_issue"] = (
            {"code": error.code, "message": error.message}
            if error.code != "WAREHOUSE_ADDRESS_NOT_FOUND"
            else None
        )
    return result


def _twin_reference_feature_codes(kind: str, location_text: str | None) -> list[str]:
    normalized = str(location_text or "").strip().upper()
    candidates = (
        ("ZONE-1F-MOLD-001", "ZONE-1F-MOLD-002")
        if kind == "mold"
        else ("ZONE-1F-PLATE-001", "ZONE-1F-PLATE-002")
    )
    return [code for code in candidates if code in normalized]


def _assert_asset_location_operational(
    db: Session,
    *,
    asset_kind: Literal["mold", "printing_plate"],
    location_text: str,
    claim_floor: bool = False,
) -> None:
    normalized = str(location_text or "").strip().upper()
    if asset_kind == "printing_plate":
        try:
            normalize_printing_plate_location(normalized)
        except PrintingPlateLocationError:
            return
    floor_code = warehouse_asset_location_floor(
        asset_kind=asset_kind,
        location_text=normalized,
    )
    if claim_floor and floor_code:
        _claim_floor_projection_for_layout_write(db, floor_code=floor_code)
    try:
        assert_warehouse_asset_location_not_archived(
            db,
            asset_kind=asset_kind,
            location_text=normalized,
        )
    except ArchivedWarehouseAreaTargetError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    if asset_kind == "printing_plate":
        floor_code = "1F"
        raw_floor = load_warehouse_twin_floor(floor_code)
        active_floor = overlay_formal_area_bindings(
            db,
            floor_code=floor_code,
            floor_layout=raw_floor,
            include_draft=False,
        )
        feature_code = "ZONE-1F-PLATE-002"
        raw_has_target = any(
            str(item.get("feature_code") or "").strip().upper() == feature_code
            for item in raw_floor.get("features") or []
        )
        active_has_target = any(
            str(item.get("feature_code") or "").strip().upper() == feature_code
            for item in active_floor.get("features") or []
        )
        if raw_has_target and not active_has_target:
            raise HTTPException(
                status_code=409,
                detail="该挂板区域已经归档，不能新增、启用或移入挂板。",
            )
        return

    guide = describe_mold_location(normalized)
    floor_code = str(guide.get("floor") or "").strip().upper()
    if not floor_code:
        return
    if guide.get("kind") == "archive_area":
        floor = warehouse_floor_for_code(db, floor_code)
        area_code = str(guide.get("area") or "").strip().upper()
        if floor is None or not area_code:
            return
        area = db.scalar(
            select(WarehouseArea)
            .where(
                WarehouseArea.floor_id == floor.id,
                func.upper(WarehouseArea.area_code) == area_code,
            )
            .options(selectinload(WarehouseArea.storage_policy))
        )
        if area is not None and (
            area.construction_status == "archived"
            or (
                area.storage_policy is not None
                and area.storage_policy.status == "archived"
            )
        ):
            raise HTTPException(
                status_code=409,
                detail="该模具封存区域已经归档，不能再移入模具。",
            )
        return
    if floor_code != "1F":
        return
    raw_floor = load_warehouse_twin_floor("1F")
    active_floor = overlay_formal_area_bindings(
        db,
        floor_code="1F",
        floor_layout=raw_floor,
        include_draft=False,
    )
    raw_codes = set(
        mold_location_feature_codes(normalized, floor_layout=raw_floor)
    )
    active_codes = set(
        mold_location_feature_codes(normalized, floor_layout=active_floor)
    )
    if raw_codes and not raw_codes.issubset(active_codes):
        raise HTTPException(
            status_code=409,
            detail="该模具货架所在区域已经归档，不能新增、恢复或移入模具。",
        )


def _twin_locator_visible_customer_ids(
    db: Session,
    user: User,
) -> set[int] | None:
    if has_permission(user, "deliveries.pick") and not has_permission(
        user, "warehouse.view"
    ):
        return set(
            db.scalars(
                select(DeliveryPickTask.customer_id).where(
                    DeliveryPickTask.assigned_to == user.id,
                    DeliveryPickTask.status != "dispatched",
                )
            ).all()
        )
    return _visible_customer_ids(user, db)


def _twin_reference_area_resources(db: Session, keyword: str) -> list[dict]:
    needle = keyword.casefold()
    subtype_labels = {"mold": "模具", "printing_plate": "印刷版 模板"}
    resources: list[dict] = []
    for floor_code in ("1F", "3F", "4F"):
        try:
            floor = overlay_formal_area_bindings(
                db,
                floor_code=floor_code,
                floor_layout=load_warehouse_twin_floor(floor_code),
                include_draft=False,
            )
        except WarehouseTwinLayoutNotFoundError:
            continue
        for feature in floor.get("features") or []:
            subtype = str(feature.get("subtype") or "")
            if subtype not in subtype_labels:
                continue
            searchable = " ".join(
                str(value or "")
                for value in (
                    feature.get("feature_code"),
                    feature.get("name"),
                    subtype,
                    subtype_labels[subtype],
                )
            ).casefold()
            if needle not in searchable:
                continue
            feature_code = str(feature.get("feature_code") or "")
            resources.append(
                {
                    "resource_id": f"area:{feature.get('id')}",
                    "kind": "mold_area" if subtype == "mold" else "printing_plate_area",
                    "primary_code": feature_code,
                    "title": feature.get("name") or subtype_labels[subtype],
                    "subtitle": "已确认功能区域",
                    "floor_code": floor_code,
                    "area_code": feature.get("erp_area_code"),
                    "location_id": None,
                    "location_code": feature_code,
                    "pallet_id": None,
                    "feature_codes": [feature_code],
                    "map_status": "mapped",
                    "prompt": f"地图已高亮 {feature.get('name') or feature_code}。",
                }
            )
    return resources


def _twin_mold_resources(
    db: Session,
    user: User,
    keyword: str,
    visible_customer_ids: set[int] | None,
) -> list[dict]:
    try:
        floor1_layout = overlay_formal_area_bindings(
            db,
            floor_code="1F",
            floor_layout=load_warehouse_twin_floor("1F"),
            include_draft=False,
        )
    except WarehouseTwinLayoutNotFoundError:
        floor1_layout = None
    response = list_mold_tools(
        q=keyword,
        customer_ids=None,
        include_inactive=False,
        limit=100,
        page=None,
        page_size=100,
        db=db,
        user=user,
    )
    resources: list[dict] = []
    for mold in response.get("items") or []:
        visible_products = [
            item
            for item in (
                mold.get("binding_history")
                if mold.get("archive_status") == "archived"
                else mold.get("products")
            ) or []
            if visible_customer_ids is None
            or item.get("customer_id") in visible_customer_ids
        ]
        if visible_customer_ids is not None and not visible_products:
            continue
        guide = describe_mold_location(str(mold.get("rack_location") or ""))
        location_text = str(mold.get("rack_location") or "")
        feature_codes = list(
            dict.fromkeys(
                [
                    *_twin_reference_feature_codes("mold", location_text),
                    *(
                        mold_location_feature_codes(
                            location_text,
                            floor_layout=floor1_layout,
                        )
                        if floor1_layout is not None
                        else []
                    ),
                ]
            )
        )
        product_summary = "、".join(
            str(item.get("product_code") or item.get("product_name") or "")
            for item in visible_products[:3]
        )
        resources.append(
            {
                "resource_id": f"mold:{mold.get('id')}",
                "kind": "mold",
                "primary_code": mold.get("mold_code"),
                "title": mold.get("display_name") or mold.get("mold_name") or "模具",
                "subtitle": (
                    f"封存待复用 · {product_summary or '无历史绑定'}"
                    if mold.get("archive_status") == "archived"
                    else product_summary or "未关联产品"
                ),
                "floor_code": guide.get("floor") or "TEXT",
                "area_code": guide.get("area"),
                "location_id": None,
                "location_code": mold.get("rack_location"),
                "pallet_id": None,
                "feature_codes": feature_codes,
                "map_status": "mapped" if feature_codes else "text_only",
                "prompt": guide.get("prompt"),
            }
        )
    return resources


def _twin_printing_plate_resources(
    db: Session,
    user: User,
    keyword: str,
    visible_customer_ids: set[int] | None,
) -> list[dict]:
    try:
        active_floor = overlay_formal_area_bindings(
            db,
            floor_code="1F",
            floor_layout=load_warehouse_twin_floor("1F"),
            include_draft=False,
        )
    except WarehouseTwinLayoutNotFoundError:
        active_floor = {"features": []}
    active_feature_codes = {
        str(item.get("feature_code") or "").strip().upper()
        for item in active_floor.get("features") or []
        if str(item.get("feature_code") or "").strip()
    }
    response = list_printing_plates(
        q=keyword,
        customer_id=None,
        include_inactive=False,
        limit=100,
        db=db,
        user=user,
    )
    resources: list[dict] = []
    for plate in response.get("items") or []:
        if (
            visible_customer_ids is not None
            and plate.get("customer_id") not in visible_customer_ids
        ):
            continue
        guide = plate.get("location_guide") or describe_printing_plate_location(
            str(plate.get("rack_location") or "")
        )
        feature_codes = (
            ["ZONE-1F-PLATE-002"]
            if guide.get("kind") == "plate_rack"
            and "ZONE-1F-PLATE-002" in active_feature_codes
            else []
        )
        product_summary = "、".join(
            str(item.get("product_code") or item.get("product_name") or "")
            for item in (plate.get("products") or [])[:3]
        )
        subtitle_parts = [
            str(plate.get("customer_name") or "").strip(),
            str(plate.get("color_name") or "").strip(),
            product_summary,
        ]
        resources.append(
            {
                "resource_id": f"printing-plate:{plate.get('id')}",
                "kind": "printing_plate",
                "primary_code": plate.get("plate_code"),
                "title": plate.get("plate_name") or "印刷挂板",
                "subtitle": " · ".join(value for value in subtitle_parts if value),
                "floor_code": guide.get("floor") or "TEXT",
                "area_code": "ZONE-1F-PLATE-002" if feature_codes else None,
                "location_id": None,
                "location_code": plate.get("rack_location"),
                "pallet_id": None,
                "feature_codes": feature_codes,
                "map_status": "mapped" if feature_codes else "text_only",
                "prompt": guide.get("prompt"),
            }
        )
    return resources


def _twin_pick_task_resources(
    db: Session,
    user: User,
    keyword: str,
    visible_customer_ids: set[int] | None,
) -> tuple[list[dict], list[dict]]:
    from app.api.deliveries import _pick_task_response

    query = (
        select(DeliveryPickTask)
        .join(Delivery, Delivery.id == DeliveryPickTask.delivery_id)
        .options(
            selectinload(DeliveryPickTask.items),
            selectinload(DeliveryPickTask.customer),
            selectinload(DeliveryPickTask.delivery),
        )
        .where(DeliveryPickTask.status != "dispatched")
        .order_by(DeliveryPickTask.id.desc())
        .limit(250)
    )
    if visible_customer_ids is not None:
        query = query.where(DeliveryPickTask.customer_id.in_(visible_customer_ids))
    if not has_permission(user, "deliveries.execute"):
        query = query.where(DeliveryPickTask.assigned_to == user.id)
    needle = keyword.casefold()
    matched: list[DeliveryPickTask] = []
    for task in db.scalars(query).unique().all():
        searchable = " ".join(
            [
                str(task.delivery.delivery_number if task.delivery else ""),
                str(task.customer.name if task.customer else ""),
                *[
                    f"{item.product_code_snapshot or ''} {item.product_name_snapshot or ''} {item.specification_snapshot or ''}"
                    for item in task.items
                ],
            ]
        ).casefold()
        if needle in searchable:
            matched.append(task)
        if len(matched) >= 20:
            break

    resources: list[dict] = []
    task_rows: list[dict] = []
    for task in matched:
        task_payload = _pick_task_response(db, task)
        task_rows.append(
            {
                "task_id": task_payload["id"],
                "delivery_number": task_payload["delivery_number"],
                "customer_name": task_payload["customer_name"],
                "status": task_payload["status"],
                "location_plan_complete": task_payload["location_plan_complete"],
                "location_group_count": len(task_payload["location_groups"]),
            }
        )
        for group in task_payload["location_groups"]:
            floor_number = group.get("warehouse_floor")
            floor_code = f"{floor_number}F" if floor_number is not None else "TEXT"
            map_status = group.get("map_status") or "text_only"
            resources.append(
                {
                    "resource_id": f"pick:{task.id}:{group.get('key')}",
                    "kind": "delivery_pick",
                    "primary_code": task_payload["delivery_number"],
                    "title": group.get("label") or "送货拿货位置",
                    "subtitle": f"{task_payload['customer_name']} · 建议第 {group.get('recommended_sequence')} 站 · {group.get('total_pick_quantity')} 个",
                    "floor_code": floor_code,
                    "area_code": group.get("area_code"),
                    "location_id": group.get("location_id"),
                    "location_code": group.get("location_code"),
                    "pallet_id": group.get("pallet_id"),
                    "feature_codes": [],
                    "map_status": map_status,
                    "prompt": (
                        f"按建议顺序前往：{group.get('label')}。"
                        if map_status == "mapped"
                        else f"{group.get('label')}；该位置当前只能文字指引，请现场核对。"
                    ),
                }
            )
    return resources, task_rows


@router.get("/twin-operations/catalog-search")
def warehouse_catalog_search(
    keyword: str = Query(min_length=2, max_length=150),
    page_size: int = Query(default=100, ge=1, le=500),
    after_product_id: int | None = Query(default=None, gt=0),
    db: Session = Depends(get_db),
    user: User = Depends(_can_locate_twin),
) -> dict:
    if len(keyword.strip()) < 2:
        raise HTTPException(status_code=422, detail="至少输入2个字符")
    from app.services.warehouse_product_search import catalog_without_stock
    return catalog_without_stock(db, keyword=keyword.strip(),
        visible_customer_ids=_twin_locator_visible_customer_ids(db, user),
        page_size=page_size, after_product_id=after_product_id)


@router.get("/twin-operations/locate")
def locate_warehouse_twin_objects(
    keyword: str = Query(default="", max_length=150),
    search_type: Literal["all", "inventory", "finished", "mold", "printing_plate"] = Query(default="all"),
    search_floor: Literal["ALL", "1F", "3F", "4F", "UNLOCATED"] = Query(default="ALL"),
    customer_id: int | None = Query(default=None, gt=0),
    page_size: int = Query(default=100, ge=1, le=500),
    after_lot_id: int | None = Query(default=None, gt=0),
    db: Session = Depends(get_db),
    user: User = Depends(_can_locate_twin),
) -> dict:
    """Unified read-only locator for inventory, pick tasks, molds and plates."""

    effective_keyword = keyword.strip()
    if search_type in {"finished", "inventory"} and customer_id is not None:
        require_customer_access(customer_id, user, db)
    if len(effective_keyword) < 2 and not (
        search_type == "finished" and customer_id is not None
    ):
        raise HTTPException(status_code=422, detail="全仓查找至少输入2个字符")
    query = (
        _lot_query(require_formal_location=False)
        .where(InventoryLot.status.in_(("active", "frozen")))
        .options(
            selectinload(InventoryLot.finished_detail).selectinload(
                FinishedGoodsInventoryDetail.customer
            ),
            selectinload(InventoryLot.semi_finished_detail).selectinload(
                SemiFinishedInventoryDetail.customer
            ),
        )
    )
    visible_customer_ids = _twin_locator_visible_customer_ids(db, user)
    if visible_customer_ids is not None:
        query = query.where(_visible_lot_condition(visible_customer_ids))
    if search_type == "finished":
        query = query.where(InventoryLot.inventory_type == "finished")
    if customer_id is not None:
        query = query.where(InventoryLot.id.in_(select(
            FinishedGoodsInventoryDetail.inventory_lot_id).where(
                FinishedGoodsInventoryDetail.owner_customer_id == customer_id)))
    today = beijing_today()
    lots = []
    pagination = {"page_size": page_size, "has_more": False,
                  "next_after_lot_id": None, "counts_scope": "page"}
    if search_floor != "ALL":
        # Logical pending locations may have a nominal floor, but are not placed.
        pending_condition = or_(WarehouseLocation.warehouse_floor.is_(None),
                                WarehouseLocation.placement_status == "unplaced")
        floor_condition = (pending_condition if search_floor == "UNLOCATED" else
                           or_(pending_condition, WarehouseLocation.warehouse_floor == int(search_floor[0])))
        query = query.where(or_(InventoryLot.warehouse_location_id.is_(None),
            InventoryLot.warehouse_location_id.in_(select(WarehouseLocation.id).where(floor_condition))))
    if search_type in {"all", "inventory", "finished"}:
        from app.services.warehouse_search_paging import search_lot_page
        lots, pagination = search_lot_page(db, query, keyword=effective_keyword,
            as_of=today, page_size=page_size, after_lot_id=after_lot_id)
    inventory = build_inventory_code_search_results(
        lots=lots,
        keyword=effective_keyword,
        as_of=today,
        location_projection_contexts=load_warehouse_location_projection_contexts(
            db,
            [row.location for row in lots if row.location is not None],
        ),
    )
    pick_resources: list[dict] = []
    pick_tasks: list[dict] = []
    if search_type == "all":
        pick_resources, pick_tasks = _twin_pick_task_resources(
            db, user, effective_keyword, visible_customer_ids
        )
    area_resources = _twin_reference_area_resources(db, effective_keyword)
    resources: list[dict] = []
    if search_type in {"all", "mold"}:
        resources.extend(
            row for row in area_resources if row["kind"] == "mold_area"
        )
        resources.extend(
            _twin_mold_resources(db, user, effective_keyword, visible_customer_ids)
        )
    if search_type in {"all", "printing_plate"}:
        resources.extend(
            row for row in area_resources if row["kind"] == "printing_plate_area"
        )
        resources.extend(
            _twin_printing_plate_resources(
                db, user, effective_keyword, visible_customer_ids
            )
        )
    if search_type == "all":
        resources.extend(pick_resources)
    pending_receipts = []
    if search_type == "inventory" and after_lot_id is None:
        from app.services.warehouse_pending_receipts import pending_receipt_search
        pending_receipts = pending_receipt_search(db, keyword=effective_keyword,
            visible_customer_ids=({customer_id} if customer_id is not None else visible_customer_ids))
    return {
        **inventory,
        "pending_receipts": pending_receipts,
        "search_type": search_type,
        "customer_id": customer_id,
        "result_count": len(inventory["items"]) + len(resources),
        "inventory_result_count": len(inventory["items"]),
        "pagination": pagination,
        "resource_result_count": len(resources),
        "resources": resources,
        "pick_tasks": pick_tasks,
        "notice": (
            "只显示当前账号有权查看的库存、拿货任务、模具和印刷版位置；"
            "没有已确认坐标的结果只提供文字指引。"
        ),
    }


@router.get('/twin-operations/product-quantities/{lot_id}')
def get_selected_product_quantities(lot_id: int, db: Session = Depends(get_db),
    user: User = Depends(_can_locate_twin)) -> dict:
    from app.services.warehouse_product_quantities import selected_product_quantities
    return selected_product_quantities(db, seed_lot_id=lot_id,
        visible_customer_ids=_twin_locator_visible_customer_ids(db, user))


@router.get("/space/floors")
def list_warehouse_floors(
    include_archived: bool = Query(default=False),
    db: Session = Depends(get_db),
    _user: User = Depends(can_read),
) -> dict:
    rows = db.scalars(
        select(WarehouseFloor)
        .options(
            selectinload(WarehouseFloor.areas).selectinload(WarehouseArea.floor),
            selectinload(WarehouseFloor.areas).selectinload(WarehouseArea.storage_policy),
        )
        .order_by(WarehouseFloor.floor_number, WarehouseFloor.id)
    ).all()
    items = [
        _warehouse_floor_dict(db, row, include_archived=include_archived)
        for row in rows
    ]
    if items and not all(item["capacity"]["confirmed"] for item in items):
        for item in items:
            item["capacity"] = suppress_capacity_metrics_until_all_confirmed(
                item["capacity"]
            )
    return {"items": items}


@router.post("/space/floors", status_code=201)
def create_warehouse_floor(
    payload: WarehouseFloorPayload,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    row = WarehouseFloor(**payload.model_dump())
    db.add(row)
    try:
        db.flush()
        if row.planning_reference_pallet_capacity:
            _warehouse_capacity_log(
                db,
                request=request,
                user=user,
                action="warehouse_floor_capacity_create",
                entity_type="warehouse_floor",
                entity_id=row.id,
                object_ref=row.floor_code,
                before=None,
                after={
                    "planning_reference_pallet_capacity": row.planning_reference_pallet_capacity
                },
            )
        db.commit()
        db.refresh(row)
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(
            status_code=409, detail="楼层编码或楼层序号已存在"
        ) from error
    return _warehouse_floor_dict(db, row)


@router.put("/space/floors/{floor_id}")
def update_warehouse_floor(
    floor_id: int,
    payload: WarehouseFloorPayload,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    row = db.get(WarehouseFloor, floor_id)
    if row is None:
        raise HTTPException(status_code=404, detail="楼层不存在")
    _claim_floor_ids_for_layout_write(db, row.id)
    row = db.scalar(
        select(WarehouseFloor)
        .where(WarehouseFloor.id == floor_id)
        .execution_options(populate_existing=True)
    )
    if row is None:
        db.rollback()
        raise HTTPException(status_code=409, detail="楼层台账已变化，请刷新后重试")
    if (
        row.construction_status == "enabled"
        and payload.construction_status != "enabled"
        and db.scalar(
            select(CustomerFinishedStoragePreference.id)
            .join(
                WarehouseArea,
                WarehouseArea.id
                == CustomerFinishedStoragePreference.warehouse_area_id,
            )
            .where(WarehouseArea.floor_id == row.id)
            .limit(1)
        )
        is not None
    ):
        raise HTTPException(
            status_code=409,
            detail="该楼层仍是客户默认成品区域，请先在客户资料中移除后再停用。",
        )
    floor_identity_changed = (
        row.floor_code != payload.floor_code
        or row.floor_number != payload.floor_number
    )
    archived_area_exists = db.scalar(
        select(WarehouseArea.id)
        .outerjoin(WarehouseAreaStoragePolicy)
        .where(
            WarehouseArea.floor_id == row.id,
            or_(
                WarehouseArea.construction_status == "archived",
                WarehouseAreaStoragePolicy.status == "archived",
            ),
        )
        .limit(1)
    ) is not None
    if floor_identity_changed and archived_area_exists:
        raise HTTPException(
            status_code=409,
            detail="该楼层存在已归档区域，楼层编码和序号不能通过普通台账修改。",
        )
    if (
        floor_identity_changed
        and db.scalar(
            select(WarehouseArea.id)
            .where(
                WarehouseArea.floor_id == row.id,
                WarehouseArea.address_zone_code.is_not(None),
            )
            .limit(1)
        )
        is not None
    ):
        raise HTTPException(
            status_code=409,
            detail="该楼层已有结构化位置地址，楼层身份不能通过普通台账编辑修改。",
        )
    before_capacity = row.planning_reference_pallet_capacity
    for key, value in payload.model_dump().items():
        setattr(row, key, value)
    try:
        if before_capacity != row.planning_reference_pallet_capacity:
            _warehouse_capacity_log(
                db,
                request=request,
                user=user,
                action="warehouse_floor_capacity_update",
                entity_type="warehouse_floor",
                entity_id=row.id,
                object_ref=row.floor_code,
                before={"planning_reference_pallet_capacity": before_capacity},
                after={
                    "planning_reference_pallet_capacity": row.planning_reference_pallet_capacity
                },
            )
        db.commit()
        db.refresh(row)
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(
            status_code=409, detail="楼层编码或楼层序号已存在"
        ) from error
    return _warehouse_floor_dict(db, row)


@router.post("/space/areas", status_code=201)
def create_warehouse_area(
    payload: WarehouseAreaPayload,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    floor = db.get(WarehouseFloor, payload.floor_id)
    if floor is None:
        raise HTTPException(status_code=404, detail="楼层不存在")
    row = WarehouseArea(**payload.model_dump())
    _apply_capacity_review(row, user=user, review_changed=True)
    db.add(row)
    try:
        db.flush()
        _warehouse_capacity_log(
            db,
            request=request,
            user=user,
            action="warehouse_area_capacity_create",
            entity_type="warehouse_area",
            entity_id=row.id,
            object_ref=f"{floor.floor_code}/{row.area_code}",
            before=None,
            after={
                "planned_pallet_capacity": row.planned_pallet_capacity,
                "capacity_review_status": row.capacity_review_status,
                "capacity_eligible": row.capacity_eligible,
                "confirmed_pallet_capacity": row.confirmed_pallet_capacity,
                "capacity_reviewed_by": row.capacity_reviewed_by,
                "capacity_reviewed_at": row.capacity_reviewed_at,
            },
        )
        db.commit()
        db.refresh(row)
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(
            status_code=409, detail="该楼层的区域编码已存在"
        ) from error
    return _warehouse_area_dict(db, row)


@router.put("/space/areas/{area_id}")
def update_warehouse_area(
    area_id: int,
    payload: WarehouseAreaPayload,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    row = db.scalar(
        select(WarehouseArea)
        .where(WarehouseArea.id == area_id)
        .options(selectinload(WarehouseArea.storage_policy))
    )
    if row is None:
        raise HTTPException(status_code=404, detail="区域不存在")
    source_floor_id = int(row.floor_id)
    _claim_floor_ids_for_layout_write(db, source_floor_id, payload.floor_id)
    row = db.scalar(
        select(WarehouseArea)
        .where(WarehouseArea.id == area_id)
        .options(selectinload(WarehouseArea.storage_policy))
        .execution_options(populate_existing=True)
    )
    if row is None or int(row.floor_id) != source_floor_id:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="区域所属楼层已变化，请刷新后重新编辑。",
        )
    if row.construction_status == "archived" or (
        row.storage_policy is not None
        and row.storage_policy.status == "archived"
    ):
        raise HTTPException(
            status_code=409,
            detail="该区域已经归档，普通编辑不能恢复；如需恢复，请走单独的受控恢复流程。",
        )
    if row.address_zone_code and (
        row.floor_id != payload.floor_id or row.area_code != payload.area_code
    ):
        raise HTTPException(
            status_code=409,
            detail="该区域已启用结构化地址，请通过地址治理预览和一次确认修改。",
        )
    floor = db.scalar(
        select(WarehouseFloor)
        .where(WarehouseFloor.id == payload.floor_id)
        .execution_options(populate_existing=True)
    )
    if floor is None:
        raise HTTPException(status_code=404, detail="楼层不存在")
    is_customer_preferred = db.scalar(
        select(CustomerFinishedStoragePreference.id)
        .where(CustomerFinishedStoragePreference.warehouse_area_id == row.id)
        .limit(1)
    ) is not None
    if is_customer_preferred and (
        payload.construction_status != "enabled"
        or floor.construction_status != "enabled"
    ):
        raise HTTPException(
            status_code=409,
            detail="该区域仍是客户默认成品区域，请先在客户资料中移除后再停用或迁入未启用楼层。",
        )
    before_capacity = {
        "planned_pallet_capacity": row.planned_pallet_capacity,
        "capacity_review_status": row.capacity_review_status,
        "capacity_eligible": row.capacity_eligible,
        "confirmed_pallet_capacity": row.confirmed_pallet_capacity,
        "capacity_reviewed_by": row.capacity_reviewed_by,
        "capacity_reviewed_at": row.capacity_reviewed_at,
    }
    review_before = (
        row.capacity_review_status,
        row.capacity_eligible,
        row.confirmed_pallet_capacity,
    )
    for key, value in payload.model_dump().items():
        setattr(row, key, value)
    review_changed = review_before != (
        row.capacity_review_status,
        row.capacity_eligible,
        row.confirmed_pallet_capacity,
    )
    _apply_capacity_review(row, user=user, review_changed=review_changed)
    try:
        after_capacity = {
            "planned_pallet_capacity": row.planned_pallet_capacity,
            "capacity_review_status": row.capacity_review_status,
            "capacity_eligible": row.capacity_eligible,
            "confirmed_pallet_capacity": row.confirmed_pallet_capacity,
            "capacity_reviewed_by": row.capacity_reviewed_by,
            "capacity_reviewed_at": row.capacity_reviewed_at,
        }
        if before_capacity != after_capacity:
            _warehouse_capacity_log(
                db,
                request=request,
                user=user,
                action="warehouse_area_capacity_update",
                entity_type="warehouse_area",
                entity_id=row.id,
                object_ref=f"{floor.floor_code}/{row.area_code}",
                before=before_capacity,
                after=after_capacity,
            )
        db.commit()
        db.refresh(row)
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(
            status_code=409, detail="该楼层的区域编码已存在"
        ) from error
    return _warehouse_area_dict(db, row)


def _raise_location_address_error(error: WarehouseLocationAddressError) -> None:
    raise HTTPException(
        status_code=error.status_code,
        detail={"code": error.code, "message": error.message},
    ) from error


@router.post("/location-addresses/preview")
def preview_warehouse_location_address_change(
    payload: WarehouseAddressChangePayload,
    db: Session = Depends(get_db),
    _user: User = Depends(admin_only),
) -> dict:
    try:
        return build_address_change_preview(db, payload.command())
    except WarehouseLocationAddressError as error:
        _raise_location_address_error(error)


@router.post("/location-addresses/confirm")
def confirm_warehouse_location_address_change(
    payload: WarehouseAddressConfirmPayload,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    try:
        response, replayed = confirm_address_change(
            db,
            payload.command(),
            preview_fingerprint=payload.preview_fingerprint,
            idempotency_key=payload.idempotency_key,
            actor_user_id=int(user.id),
        )
        if replayed:
            return response
        append_audit_event(
            db,
            request=request,
            actor=user,
            event_category="system",
            result="success",
            source="web",
            module_code="warehouse",
            action_code="warehouse_location_address_change",
            legacy_action="LOCATION_RENAME",
            resource="warehouse/location-addresses",
            entity_type="warehouse_location_address_mutation",
            object_ref=response["target_ref"],
            batch_id=payload.idempotency_key[:64],
            description="仓库位置当前地址已更新；稳定位置、库存数量和流水未改变",
            details={
                "before": response["before"],
                "after": response["after"],
                "impacts": response["impacts"],
                "writes_inventory": False,
                "writes_movements": False,
            },
        )
        db.commit()
        return response
    except WarehouseLocationAddressError as error:
        db.rollback()
        _raise_location_address_error(error)
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail={
                "code": "WAREHOUSE_ADDRESS_CONFLICT",
                "message": "地址、旧码或操作键已被占用，请刷新后重试。",
            },
        ) from error
    except Exception:
        db.rollback()
        raise


@router.get("/location-addresses/resolve")
def resolve_warehouse_location_address(
    value: str = Query(min_length=1, max_length=120),
    db: Session = Depends(get_db),
    _user: User = Depends(can_read),
) -> dict:
    try:
        return resolve_location_address(db, value)
    except WarehouseLocationAddressError as error:
        _raise_location_address_error(error)


@router.get("/location-addresses/{location_id}/aliases")
def list_warehouse_location_aliases(
    location_id: int,
    db: Session = Depends(get_db),
    _user: User = Depends(admin_only),
) -> dict:
    if db.get(WarehouseLocation, location_id) is None:
        raise HTTPException(status_code=404, detail="位置不存在")
    rows = db.scalars(
        select(WarehouseLocationAlias)
        .where(WarehouseLocationAlias.location_id == location_id)
        .order_by(WarehouseLocationAlias.id)
    ).all()
    return {
        "items": [
            {
                "alias_text": row.alias_text,
                "alias_kind": row.alias_kind,
                "created_at": utc_naive_to_api(row.created_at),
            }
            for row in rows
        ]
    }


@router.get("/locations")
def list_locations(
    include_inactive: bool = False,
    db: Session = Depends(get_db),
    _user: User = Depends(can_read),
) -> dict:
    query = select(WarehouseLocation).where(_formal_inventory_location_condition())
    if not include_inactive:
        query = query.where(WarehouseLocation.is_active.is_(True))
    rows = db.scalars(query.order_by(WarehouseLocation.location_code)).all()
    projection_contexts = load_warehouse_location_projection_contexts(db, rows)
    return {
        "items": [
            _location_dict(row, projection_contexts.get(int(row.id)))
            for row in rows
        ]
    }


def _semi_candidate_dicts(
    db: Session,
    rows: list[SemiFinishedCandidate],
) -> list[dict]:
    projection_contexts = load_warehouse_location_projection_contexts(
        db,
        [row.lot.location for row in rows if row.lot.location is not None],
    )
    return [
        _semi_candidate_dict(
            row,
            (
                projection_contexts.get(int(row.lot.warehouse_location_id))
                if row.lot.warehouse_location_id is not None
                else None
            ),
        )
        for row in rows
    ]


def _require_printable_location_label(
    row: WarehouseLocation,
    projection_context: Mapping[str, object],
) -> None:
    projection = warehouse_location_projection(row, **dict(projection_context))
    if (
        not row.is_active
        or row.placement_status != "placed"
        or row.is_temporary
        or row.storage_type == "temporary_aisle"
        or projection["position_status"] != "mapped"
    ):
        raise HTTPException(
            status_code=409,
            detail="仅已发布到当前实测地图的正式位置可以打印位置标签",
        )


def _location_label_dict(
    row: WarehouseLocation,
    request: Request,
    projection_context: Mapping[str, object],
) -> dict:
    _require_printable_location_label(row, projection_context)
    floor = projection_context.get("floor")
    area = projection_context.get("area")
    floor_number = int(row.warehouse_floor or 0)
    floor_text = {1: "一楼", 2: "二楼", 3: "三楼", 4: "四楼"}.get(
        floor_number,
        f"{floor_number}楼" if floor_number else "楼层待确认",
    )
    area_text = employee_area_name(
        area,
        area_code=row.area_code,
        floor_number=floor_number,
        fallback_name="区域待确认",
    )
    readable_location = employee_location_name(
        row,
        area=area,
        floor=floor,
        area_sequence=projection_context.get("area_sequence"),
    )
    lookup_url = location_mobile_url(row.id, origin=load_settings().browser_url)
    print_address_code = row.location_code
    if (
        row.address_kind == "rack_slot"
        and row.warehouse_floor
        and row.area_code
        and row.rack_code
        and row.level_no
        and row.slot_no
    ):
        print_address_code = (
            f"{int(row.warehouse_floor)}F-{str(row.area_code).strip().upper()}-"
            f"{str(row.rack_code).strip().upper()}-"
            f"{int(row.level_no)}-{int(row.slot_no)}"
        )
    return {
        **_location_dict(row, projection_context),
        "floor_text": floor_text,
        "area_text": area_text,
        "area_master_name": area.area_name if area is not None else None,
        "display_path": readable_location,
        "print_address_code": print_address_code,
        "layout_version": row.floor3_layout.version if row.floor3_layout else None,
        "lookup_url": lookup_url,
        "qr_data_url": qr_data_url(lookup_url),
    }


@router.get("/location-label-workbench")
def get_location_label_workbench(
    floor_code: str | None = Query(default=None, min_length=2, max_length=12),
    db: Session = Depends(get_db),
    _user: User = Depends(can_read),
) -> dict:
    """List printable rack cells projected from the current published map.

    Retired, draft-only, temporary, unplaced and historical-alias locations are
    deliberately absent.  This keeps the low-frequency print workbench on the
    same spatial baseline as the operational map without deleting audit facts.
    """

    normalized_floor = str(floor_code or "").strip().upper()
    requested_floor_codes = ["1F", "3F", "4F"]
    if normalized_floor:
        match = re.fullmatch(r"(\d{1,2})F", normalized_floor)
        if match is None:
            raise HTTPException(status_code=422, detail="楼层筛选格式无效")
        requested_floor_codes = [normalized_floor]

    formal_floors = list(
        db.scalars(
            select(WarehouseFloor)
            .options(
                selectinload(WarehouseFloor.areas).selectinload(
                    WarehouseArea.storage_policy
                )
            )
            .where(func.upper(WarehouseFloor.floor_code).in_(requested_floor_codes))
        ).unique()
    )
    formal_floors_by_code = {
        str(row.floor_code or "").strip().upper(): row for row in formal_floors
    }
    formal_areas_by_feature: dict[str, WarehouseArea] = {}
    formal_areas_by_code: dict[tuple[str, str], WarehouseArea] = {}
    for formal_floor in formal_floors:
        current_floor_code = str(formal_floor.floor_code or "").strip().upper()
        for area in formal_floor.areas:
            if area.construction_status == "archived" or (
                area.storage_policy is not None
                and area.storage_policy.status == "archived"
            ):
                continue
            formal_areas_by_code[(current_floor_code, area.area_code.upper())] = area
            policy = area.storage_policy
            feature_id = (
                str(policy.map_feature_id or "").strip() if policy is not None else ""
            )
            if feature_id and policy.status == "published":
                formal_areas_by_feature[feature_id] = area

    floors_by_code: dict[str, dict] = {}
    racks_by_id: dict[str, dict] = {}
    for current_floor_code in requested_floor_codes:
        try:
            layout = load_warehouse_twin_floor(current_floor_code)
        except WarehouseTwinLayoutNotFoundError:
            continue
        layout = overlay_formal_area_bindings(
            db,
            floor_code=current_floor_code,
            floor_layout=layout,
            include_draft=False,
        )
        floor_model = formal_floors_by_code.get(current_floor_code)
        current_floor = {
            "floor_code": current_floor_code,
            "floor_name": (
                str(floor_model.floor_name or "").strip()
                if floor_model is not None
                else str(layout.get("name") or current_floor_code).strip()
            ),
            "areas_by_key": {},
        }
        floors_by_code[current_floor_code] = current_floor
        zone_features = {
            str(item.get("id") or "").strip(): item
            for item in layout.get("features") or []
            if item.get("feature_kind") == "zone"
            and str(item.get("id") or "").strip()
        }
        zone_features_by_area: dict[str, list[dict]] = {}
        for feature in zone_features.values():
            feature_area_code = str(feature.get("erp_area_code") or "").strip().upper()
            if feature_area_code:
                zone_features_by_area.setdefault(feature_area_code, []).append(feature)

        for rack in layout.get("racks") or []:
            rack_id = str(rack.get("id") or "").strip()
            if not rack_id:
                continue
            map_feature_id = str(rack.get("area_feature_id") or "").strip()
            map_feature = zone_features.get(map_feature_id)
            formal_area = formal_areas_by_feature.get(map_feature_id)
            area_code = str(rack.get("area_code") or "").strip().upper()
            if not area_code and map_feature is not None:
                area_code = str(map_feature.get("erp_area_code") or "").strip().upper()
            if formal_area is None and area_code:
                formal_area = formal_areas_by_code.get(
                    (current_floor_code, area_code)
                )
            if formal_area is not None:
                area_code = formal_area.area_code.upper()
                policy = formal_area.storage_policy
                if not map_feature_id and policy is not None and policy.status == "published":
                    map_feature_id = str(policy.map_feature_id or "").strip()
                    map_feature = zone_features.get(map_feature_id)
            if not map_feature_id and area_code:
                candidates = zone_features_by_area.get(area_code, [])
                if len(candidates) == 1:
                    map_feature = candidates[0]
                    map_feature_id = str(map_feature.get("id") or "").strip()

            area_key = map_feature_id or (f"area:{area_code}" if area_code else "unassigned")
            area_entry = current_floor["areas_by_key"].setdefault(
                area_key,
                {
                    "area_id": formal_area.id if formal_area is not None else None,
                    "area_code": area_code or "未归区",
                    "area_name": (
                        employee_area_name(
                            formal_area,
                            area_code=area_code,
                            floor_number=(
                                floor_model.floor_number
                                if floor_model is not None
                                else int(current_floor_code[:-1])
                            ),
                        )
                        if formal_area is not None
                        else str(
                            (map_feature or {}).get("name")
                            or (f"{area_code} 地图区域" if area_code else "地图未归区货架")
                        ).strip()
                    ),
                    "map_feature_id": map_feature_id or None,
                    "published_map_revision": str(layout.get("revision") or "").strip(),
                    "racks_by_id": {},
                },
            )
            try:
                level_count = max(int(rack.get("levels") or 0), 0)
            except (TypeError, ValueError):
                level_count = 0
            raw_counts = rack.get("level_cell_counts")
            planned_cell_count = 0
            if isinstance(raw_counts, list) and len(raw_counts) == level_count:
                try:
                    planned_cell_count = sum(max(int(value), 0) for value in raw_counts)
                except (TypeError, ValueError):
                    planned_cell_count = 0
            rack_entry = {
                "map_rack_id": rack_id,
                "rack_code": "",
                "rack_name": str(rack.get("name") or "未命名货架").strip(),
                "level_count": level_count,
                "planned_cell_count": planned_cell_count,
                "locations": [],
            }
            area_entry["racks_by_id"][rack_id] = rack_entry
            racks_by_id[rack_id] = rack_entry

    query = select(WarehouseLocation).where(
        _formal_inventory_location_condition(),
        WarehouseLocation.is_active.is_(True),
        WarehouseLocation.is_temporary.is_(False),
        WarehouseLocation.placement_status == "placed",
        WarehouseLocation.address_kind == "rack_slot",
        WarehouseLocation.map_rack_id.is_not(None),
        WarehouseLocation.rack_code.is_not(None),
        WarehouseLocation.level_no.is_not(None),
        WarehouseLocation.slot_no.is_not(None),
    )
    if normalized_floor:
        query = query.where(WarehouseLocation.warehouse_floor == int(match.group(1)))
    rows = list(
        db.scalars(
            query.order_by(
                WarehouseLocation.warehouse_floor,
                WarehouseLocation.area_code,
                WarehouseLocation.rack_code,
                WarehouseLocation.level_no,
                WarehouseLocation.slot_no,
                WarehouseLocation.id,
            )
        ).all()
    )
    projection_contexts = load_warehouse_location_projection_contexts(db, rows)
    location_count = 0

    for row in rows:
        rack_id = str(row.map_rack_id or "").strip()
        current_rack = racks_by_id.get(rack_id)
        if current_rack is None:
            # A location bound to a rack that is no longer on the published map
            # remains auditable, but is not offered as a current printable rack.
            continue
        context = projection_contexts.get(int(row.id), {})
        try:
            _require_printable_location_label(row, context)
        except HTTPException:
            continue
        payload = _location_dict(row, context)
        current_rack["rack_code"] = str(row.rack_code or "").strip().upper()
        print_address_code = (
            f"{int(row.warehouse_floor or 0)}F-{str(row.area_code or '').strip().upper()}-"
            f"{str(row.rack_code or '').strip().upper()}-"
            f"{int(row.level_no or 0)}-{int(row.slot_no or 0)}"
        )
        current_rack["locations"].append(
            {
                "location_id": int(row.id),
                "level_no": int(row.level_no or 0),
                "slot_no": int(row.slot_no or 0),
                "print_address_code": print_address_code,
                "location_name": payload.get("employee_location_name"),
            }
        )
        location_count += 1

    floors: list[dict] = []
    rack_count = 0
    for current_floor in floors_by_code.values():
        areas: list[dict] = []
        for current_area in current_floor.pop("areas_by_key").values():
            racks: list[dict] = []
            for current_rack in current_area.pop("racks_by_id").values():
                current_rack["locations"].sort(
                    key=lambda item: (item["level_no"], item["slot_no"], item["location_id"])
                )
                current_rack["location_ids"] = [
                    item["location_id"] for item in current_rack["locations"]
                ]
                current_rack["level_count"] = max(
                    int(current_rack.get("level_count") or 0),
                    max(
                        (item["level_no"] for item in current_rack["locations"]),
                        default=0,
                    ),
                )
                current_rack["cell_count"] = len(current_rack["locations"])
                current_rack["label_ready"] = bool(current_rack["locations"])
                current_rack["setup_status"] = (
                    "ready"
                    if current_rack["locations"]
                    else (
                        "binding_pending"
                        if int(current_rack.get("planned_cell_count") or 0) > 0
                        else "cell_plan_pending"
                    )
                )
                racks.append(current_rack)
                rack_count += 1
            racks.sort(key=lambda item: (str(item["rack_name"]), str(item["map_rack_id"])))
            current_area["racks"] = racks
            areas.append(current_area)
        areas.sort(key=lambda item: (str(item["area_code"]), str(item["area_name"])))
        current_floor["areas"] = areas
        floors.append(current_floor)
    floors.sort(key=lambda item: str(item["floor_code"]))
    return {
        "floors": floors,
        "rack_count": rack_count,
        "location_count": location_count,
        "source": "current_published_measured_map",
    }


def _shelf_label_content(db: Session, row: WarehouseLocation, user: User) -> dict | None:
    from app.models.fixed_shelf import ShelfBinding
    from app.services.fixed_shelf import profile_info
    from app.api.deps import has_unrestricted_customer_access, customer_scope_ids
    binding = db.get(ShelfBinding, row.id)
    if binding is None:
        return None
    product = db.get(Product, binding.product_id)
    if not has_unrestricted_customer_access(user, db) and product.customer_id not in customer_scope_ids(user, db):
        return {"restricted": True}
    return {**profile_info(db, product), "binding_priority": binding.priority}


def _with_shelf_label(db, row, user, label, *, information_only=False):
    if information_only:
        from app.services.rack_information_labels import rack_information_contents
        contents = rack_information_contents(db, row.id, lambda customer_id: require_customer_access(customer_id, user, db))
        from app.services.mobile_shelf_labels import mobile_url
        url = mobile_url(load_settings().browser_url, row.id)
        label.update(shelf_contents=contents, lookup_url=url, qr_data_url=qr_data_url(url), information_only=True)
        return label
    content = _shelf_label_content(db, row, user)
    label['shelf_content'] = content
    if content and not content.get('restricted'):
        # Printed labels must remain reachable after an alternate-port preview.
        url = mobile_absolute_url(
            f'/sp/{row.id}/{content["product_id"]}/{content["version"]}/{row.address_version}',
            origin=load_settings().browser_url,
        )
        label.update(lookup_url=url, qr_data_url=qr_data_url(url))
    return label


@router.get("/locations/labels")
def get_location_labels(
    request: Request,
    location_ids: str = Query(min_length=1, max_length=8000),
    content: Literal["default", "shelf-information"] = "default",
    db: Session = Depends(get_db),
    _user: User = Depends(can_read),
) -> dict:
    raw_ids = [part.strip() for part in location_ids.split(",") if part.strip()]
    if not raw_ids or any(not part.isdigit() or int(part) <= 0 for part in raw_ids):
        raise HTTPException(status_code=422, detail="位置批量标签参数无效")
    ordered_ids = list(dict.fromkeys(int(part) for part in raw_ids))
    if len(ordered_ids) > 500:
        raise HTTPException(status_code=422, detail="一次最多打印 500 个位置")
    rows = db.scalars(
        select(WarehouseLocation)
        .options(
            selectinload(WarehouseLocation.floor3_layout),
            selectinload(WarehouseLocation.address_area).selectinload(
                WarehouseArea.floor
            ),
        )
        .where(WarehouseLocation.id.in_(ordered_ids))
    ).all()
    rows_by_id = {row.id: row for row in rows}
    if any(location_id not in rows_by_id for location_id in ordered_ids):
        raise HTTPException(status_code=404, detail="所选位置已变化，请返回台账重新选择")
    ordered_rows = [rows_by_id[location_id] for location_id in ordered_ids]
    projection_contexts = load_warehouse_location_projection_contexts(
        db, ordered_rows
    )
    for row in ordered_rows:
        _require_printable_location_label(
            row,
            projection_contexts.get(int(row.id), {}),
        )
    return {
        "items": [
            _with_shelf_label(db, row, _user, _location_label_dict(
                row,
                request,
                projection_contexts.get(int(row.id), {}),
            ), information_only=content == "shelf-information")
            for row in ordered_rows
        ],
        "count": len(ordered_rows),
    }


@router.get("/locations/{location_id}/label")
def get_location_label(
    location_id: int,
    request: Request,
    content: Literal["default", "shelf-information"] = "default",
    db: Session = Depends(get_db),
    _user: User = Depends(can_read),
) -> dict:
    row = db.scalar(
        select(WarehouseLocation)
        .options(
            selectinload(WarehouseLocation.floor3_layout),
            selectinload(WarehouseLocation.address_area).selectinload(
                WarehouseArea.floor
            ),
        )
        .where(WarehouseLocation.id == location_id)
    )
    if row is None:
        raise HTTPException(status_code=404, detail="位置不存在")
    projection_context = load_warehouse_location_projection_contexts(
        db, [row]
    ).get(int(row.id), {})
    return _with_shelf_label(db, row, _user, _location_label_dict(row, request, projection_context), information_only=content == "shelf-information")


def _mobile_shelf_location(db, location_id):
    from app.services.mobile_shelf_labels import compact_rack_title, readable_address, print_address
    row = db.get(WarehouseLocation, location_id)
    if row is None:
        raise HTTPException(404, "货位不存在")
    context = load_warehouse_location_projection_contexts(db, [row]).get(row.id, {})
    _require_printable_location_label(row, context)
    path = employee_location_name(row, area=context.get("area"), floor=context.get("floor"),
                                  area_sequence=context.get("area_sequence"))
    title, position, address = readable_address(dict(display_path=path, level_no=row.level_no, slot_no=row.slot_no))
    result = dict(location_id=row.id, title=title, position=position, address=address,
                  **print_address(dict(display_path=path, level_no=row.level_no, slot_no=row.slot_no)))
    if row.address_kind == "rack_slot" and row.address_area_id and (row.map_rack_id or row.rack_code):
        # Group physical racks by stable identity, never by their display names.
        rack_identity = [row.address_area_id, "map" if row.map_rack_id else "address",
                         row.map_rack_id or row.rack_code]
        result["rack_key"] = hashlib.sha256(json.dumps(rack_identity).encode()).hexdigest()
        result["rack_label"] = compact_rack_title(row.rack_display_name or result["compact_title"])
    return row, result


@router.get("/locations/{location_id}/mobile-label")
def mobile_shelf_label(location_id: int, response: Response, lot_id: int | None = Query(default=None, gt=0),
                       db: Session = Depends(get_db), user: User = Depends(can_read)):
    from app.services.mobile_shelf_labels import product_key, product_fields, mobile_url
    row, result = _mobile_shelf_location(db, location_id)
    key = None
    if lot_id is not None:
        lot = _require_lot_customer_access(db, lot_id, user)
        if lot.warehouse_location_id != row.id or lot.quantity_available + lot.quantity_reserved + lot.quantity_damaged <= 0:
            raise HTTPException(409, "产品已不在本格，请刷新")
        if lot.inventory_type != "finished" or not lot.finished_detail or not lot.finished_detail.product_id:
            raise HTTPException(409, "请先核对成品身份")
        fields = product_fields(db, lot)
        customer = db.get(Customer, lot.finished_detail.owner_customer_id) if lot.finished_detail.owner_customer_id else None
        if customer and not customer.chinese_short_name:
            raise HTTPException(409, "请补充客户中文简称")
        if fields["code"] == "待补充" or fields["name"] == "待补充":
            raise HTTPException(409, "请补充存货编码和产品名称")
        key = product_key(lot)
        result["product"] = fields
    url = mobile_url(load_settings().browser_url, row.id, key)
    result.update(lookup_url=url, qr_data_url=qr_data_url(url))
    if result.get("rack_key") and row.map_rack_id:
        rack_url = rack_mobile_url(f"{row.warehouse_floor}F", row.map_rack_id, origin=load_settings().browser_url)
        result.update(rack_lookup_url=rack_url,
                      rack_qr_data_url=qr_data_url(rack_url))
    response.headers["Cache-Control"] = "no-store"
    return result


@router.get("/locations/{location_id}/scan")
def mobile_shelf_scan(location_id: int, response: Response,
                      product: str | None = Query(default=None, pattern=r"^[a-f0-9]{24}$"),
                      db: Session = Depends(get_db), user: User = Depends(can_read)):
    from app.services.mobile_shelf_labels import product_key, product_fields
    from app.services.warehouse_reading_identity import shelf_merge_identity
    _, result = _mobile_shelf_location(db, location_id)
    query = select(InventoryLot).options(selectinload(InventoryLot.finished_detail),
        selectinload(InventoryLot.semi_finished_detail)).where(
        InventoryLot.warehouse_location_id == location_id,
        InventoryLot.quantity_available + InventoryLot.quantity_reserved + InventoryLot.quantity_damaged > 0)
    scope = _visible_customer_ids(user, db)
    if scope is not None:
        query = query.where(_visible_lot_condition(scope))
    groups = {}
    for lot in db.scalars(query.order_by(InventoryLot.id)):
        key = product_key(lot)
        if product and key != product:
            continue
        identity = shelf_merge_identity(lot)
        detail = lot.finished_detail
        mergeable = bool(detail and detail.inventory_code_snapshot and detail.product_name_snapshot and identity["box_style"] and identity["is_bom_component"] is False)
        display_key = (detail.owner_customer_id, detail.product_id, detail.inventory_code_snapshot,
                       detail.product_name_snapshot, identity["box_style"], lot.unit) if mergeable else ("lot", lot.id)
        if display_key not in groups:
            from app.services.warehouse_display_units import lot_display_unit
            groups[display_key] = dict(key=key, **product_fields(db, lot), unit=lot_display_unit(lot),
                               quantity=0, available=0, reserved=0, damaged=0, lots=[])
        item = groups[display_key]
        item["quantity"] += lot.quantity_available + lot.quantity_reserved + lot.quantity_damaged
        item["available"] += lot.quantity_available
        item["reserved"] += lot.quantity_reserved
        item["damaged"] += lot.quantity_damaged
        item["lots"].append(dict(id=lot.id, quantity=lot.quantity_available + lot.quantity_reserved + lot.quantity_damaged, status=lot.status))
    result.update(items=list(groups.values()), filtered=bool(product),
                  refreshed_at=beijing_naive_to_api(beijing_now_naive()))
    response.headers["Cache-Control"] = "no-store"
    return result


@router.get("/location-candidates")
def list_location_candidates(
    inventory_type: Literal["finished", "semi_finished"] = "finished",
    empty_only: bool = False,
    pallet_storage_only: bool = False,
    include_hierarchy: bool = True,
    published_only: bool = False,
    db: Session = Depends(get_db),
    _user: User = Depends(can_read),
) -> dict:
    warehouse_types = (
        {"finished", "shared"}
        if inventory_type == "finished"
        else {"semi_finished", "shared"}
    )
    rows = list_operational_locations(
        db,
        warehouse_types=warehouse_types,
        empty_only=empty_only,
        pallet_storage_only=pallet_storage_only,
    )
    if published_only:
        occupied_pallet_counts = {
            (int(floor_number), str(code or "").strip().upper()): int(count)
            for floor_number, code, count in db.execute(
                select(
                    WarehouseLocation.warehouse_floor,
                    func.upper(WarehouseLocation.area_code),
                    func.count(InventoryPallet.id),
                )
                .join(
                    InventoryPallet,
                    InventoryPallet.location_id == WarehouseLocation.id,
                )
                .where(InventoryPallet.is_current.is_(True))
                .group_by(
                    WarehouseLocation.warehouse_floor,
                    func.upper(WarehouseLocation.area_code),
                )
            ).all()
            if floor_number is not None
        }
        rows = [
            row
            for row in rows
            if operational_location_issue(
                db,
                row.location,
                warehouse_types=warehouse_types,
                pallet_storage_only=pallet_storage_only,
                require_published=True,
                require_map_geometry=True,
                required_inventory_type=inventory_type,
                require_empty=empty_only,
                projection_context=row.projection_context,
                known_occupied=row.occupied,
                area_occupied_pallet_count=occupied_pallet_counts.get(
                    (
                        int(row.location.warehouse_floor or 0),
                        str(row.location.area_code or "").strip().upper(),
                    ),
                    0,
                ),
            )
            is None
        ]
    items = [operational_location_payload(row) for row in rows]
    if not include_hierarchy:
        return {"items": items}
    floors: dict[int, dict] = {}
    for item in items:
        floor_number = item["warehouse_floor"]
        area_code = item["area_code"]
        if floor_number is None or not area_code:
            continue
        floor = floors.setdefault(
            int(floor_number),
            {
                "id": item["floor_id"],
                "floor_code": item["floor_code"],
                "floor_name": item["floor_name"],
                "floor_number": int(floor_number),
                "areas": {},
            },
        )
        area = floor["areas"].setdefault(
            str(area_code),
            {
                "id": item["area_id"],
                "area_code": area_code,
                "area_name": item["area_name"],
                "locations": [],
            },
        )
        area["locations"].append(item)
    floor_items = []
    for floor_number in sorted(floors):
        floor = floors[floor_number]
        floor["areas"] = [
            floor["areas"][area_code]
            for area_code in sorted(floor["areas"])
        ]
        floor_items.append(floor)
    return {"items": items, "floors": floor_items}


@router.get("/references/customers")
def reference_customers(
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    query = select(Customer).where(Customer.is_active.is_(True))
    visible_customer_ids = _visible_customer_ids(user, db)
    if visible_customer_ids is not None:
        query = query.where(Customer.id.in_(visible_customer_ids))
    rows = db.scalars(
        query.order_by(Customer.customer_number, Customer.id)
    ).all()
    return {
        "items": [
            {
                "id": row.id,
                "name": row.name,
                "chinese_short_name": row.chinese_short_name,
                "customer_code": row.customer_code,
                "customer_number": row.customer_number,
            }
            for row in rows
        ]
    }


@router.get("/references/products")
def reference_products(
    customer_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    require_customer_access(customer_id, user, db)
    rows = db.scalars(
        select(Product)
        .where(
            Product.customer_id == customer_id,
            Product.is_active.is_(True),
            Product.deleted_at.is_(None),
        )
        .order_by(Product.product_code, Product.id)
    ).all()
    return {
        "items": [
            {
                "id": row.id,
                "product_code": row.product_code,
                "product_name": row.product_name,
                "specification": product_dimension_specification(row),
            }
            for row in rows
        ]
    }


def _mold_customer_scope(user: User, db: Session) -> set[int] | None:
    if has_unrestricted_customer_access(user, db):
        return None
    return customer_scope_ids(user, db)


def _printing_plate_product_filter(plate_id: int):
    return or_(
        Product.printing_plate_1_id == plate_id,
        Product.printing_plate_2_id == plate_id,
        Product.printing_plate_3_id == plate_id,
    )


def _printing_plate_products(
    db: Session,
    plate_id: int,
    allowed_customer_ids: set[int] | None,
) -> list[Product]:
    query = select(Product).where(
        _printing_plate_product_filter(plate_id),
        Product.deleted_at.is_(None),
        Product.is_active.is_(True),
    )
    if allowed_customer_ids is not None:
        query = query.where(Product.customer_id.in_(allowed_customer_ids))
    return list(db.scalars(query.order_by(Product.product_code, Product.id)).all())


def _printing_plate_products_by_plate(
    db: Session,
    plate_ids: list[int],
    allowed_customer_ids: set[int] | None,
) -> dict[int, list[Product]]:
    result = {plate_id: [] for plate_id in plate_ids}
    if not plate_ids:
        return result
    query = select(Product).where(
        or_(
            Product.printing_plate_1_id.in_(plate_ids),
            Product.printing_plate_2_id.in_(plate_ids),
            Product.printing_plate_3_id.in_(plate_ids),
        ),
        Product.deleted_at.is_(None),
        Product.is_active.is_(True),
    )
    if allowed_customer_ids is not None:
        query = query.where(Product.customer_id.in_(allowed_customer_ids))
    products = db.scalars(query.order_by(Product.product_code, Product.id)).all()
    for product in products:
        for plate_id in {
            product.printing_plate_1_id,
            product.printing_plate_2_id,
            product.printing_plate_3_id,
        }:
            if plate_id in result:
                result[plate_id].append(product)
    return result


def _printing_plate_binding_counts_by_plate(
    db: Session,
    plate_ids: list[int],
) -> dict[int, int]:
    result = {plate_id: 0 for plate_id in plate_ids}
    if not plate_ids:
        return result
    rows = db.execute(
        select(
            Product.printing_plate_1_id,
            Product.printing_plate_2_id,
            Product.printing_plate_3_id,
        ).where(
            or_(
                Product.printing_plate_1_id.in_(plate_ids),
                Product.printing_plate_2_id.in_(plate_ids),
                Product.printing_plate_3_id.in_(plate_ids),
            )
        )
    )
    for values in rows:
        for plate_id in set(values):
            if plate_id in result:
                result[plate_id] += 1
    return result


def _printing_plate_reuse_dict(row: PrintingPlateResinReuse) -> dict:
    return {
        "id": row.id,
        "printing_plate_id": row.printing_plate_id,
        "plate_code": row.plate_code_snapshot,
        "from_customer_id": row.from_customer_id,
        "from_customer_name": row.from_customer_name_snapshot,
        "from_plate_name": row.from_plate_name_snapshot,
        "from_color_name": row.from_color_name_snapshot,
        "to_customer_id": row.to_customer_id,
        "to_customer_name": row.to_customer_name_snapshot,
        "to_plate_name": row.to_plate_name_snapshot,
        "to_color_name": row.to_color_name_snapshot,
        "rack_location": row.rack_location_snapshot,
        "actor_id": row.actor_id,
        "actor_username": row.actor_username_snapshot,
        "reused_at": utc_naive_to_api(row.reused_at),
        "expected_version": row.expected_version,
        "resulting_version": row.resulting_version,
        "old_resin_removed": row.old_resin_removed,
        "new_resin_mounted": row.new_resin_mounted,
    }


def _printing_plate_reuse_summaries(
    db: Session,
    plate_ids: list[int],
) -> tuple[dict[int, int], dict[int, PrintingPlateResinReuse]]:
    counts = {plate_id: 0 for plate_id in plate_ids}
    latest: dict[int, PrintingPlateResinReuse] = {}
    if not plate_ids:
        return counts, latest
    for plate_id, count in db.execute(
        select(
            PrintingPlateResinReuse.printing_plate_id,
            func.count(PrintingPlateResinReuse.id),
        )
        .where(PrintingPlateResinReuse.printing_plate_id.in_(plate_ids))
        .group_by(PrintingPlateResinReuse.printing_plate_id)
    ):
        counts[int(plate_id)] = int(count or 0)
    latest_ids = (
        select(func.max(PrintingPlateResinReuse.id).label("id"))
        .where(PrintingPlateResinReuse.printing_plate_id.in_(plate_ids))
        .group_by(PrintingPlateResinReuse.printing_plate_id)
        .subquery()
    )
    rows = db.scalars(
        select(PrintingPlateResinReuse).join(
            latest_ids, latest_ids.c.id == PrintingPlateResinReuse.id
        )
    ).all()
    for row in rows:
        latest.setdefault(row.printing_plate_id, row)
    return counts, latest


def _printing_plate_dict(
    db: Session,
    row: PrintingPlate,
    allowed_customer_ids: set[int] | None = None,
    *,
    time_archive: dict | None = None,
    products: list[Product] | None = None,
    binding_count: int | None = None,
    reuse_count: int | None = None,
    latest_reuse: PrintingPlateResinReuse | None = None,
) -> dict:
    products = products if products is not None else _printing_plate_products(
        db, row.id, allowed_customer_ids
    )
    binding_count = (
        printing_plate_binding_count(db, row.id)
        if binding_count is None
        else binding_count
    )
    if reuse_count is None:
        reuse_counts, latest_reuses = _printing_plate_reuse_summaries(db, [row.id])
        reuse_count = reuse_counts[row.id]
        latest_reuse = latest_reuses.get(row.id)
    reuse_blockers = []
    if row.status != "active":
        reuse_blockers.append("只有启用且未报损的实体挂板才能换版复用")
    if "-L2-" not in row.rack_location.upper():
        reuse_blockers.append("请先移动到挂板区货架第 2 层")
    if binding_count:
        reuse_blockers.append(f"仍有 {binding_count} 个常用箱绑定")
    return {
        "id": row.id,
        "plate_code": row.plate_code,
        "customer_id": row.customer_id,
        "customer_name": row.customer.name if row.customer else None,
        "plate_name": row.plate_name,
        "color_name": row.color_name,
        "rack_location": row.rack_location,
        "location_guide": describe_printing_plate_location(row.rack_location),
        "status": row.status,
        "version": row.version,
        "location_version": row.location_version,
        "last_location_confirmed_at": (
            utc_naive_to_api(row.last_location_confirmed_at)
            if row.last_location_confirmed_at
            else None
        ),
        "last_location_confirmed_by": row.last_location_confirmed_by,
        "remarks": row.remarks,
        "product_count": len(products),
        "binding_count": binding_count,
        "products": [
            {
                "id": product.id,
                "customer_id": product.customer_id,
                "product_code": product.product_code,
                "product_name": product.product_name,
            }
            for product in products
        ],
        "created_at": utc_naive_to_api(row.created_at),
        "updated_at": utc_naive_to_api(row.updated_at) if row.updated_at else None,
        "time_archive": time_archive,
        "resin_reuse_count": reuse_count,
        "latest_resin_reuse": (
            _printing_plate_reuse_dict(latest_reuse)
            if latest_reuse is not None
            else None
        ),
        "resin_reuse_candidate": {
            "eligible": not reuse_blockers,
            "blockers": reuse_blockers,
        },
    }


def _printing_plate_or_404(db: Session, plate_id: int) -> PrintingPlate:
    row = db.scalar(
        select(PrintingPlate)
        .options(selectinload(PrintingPlate.customer))
        .where(PrintingPlate.id == plate_id)
    )
    if row is None:
        raise HTTPException(status_code=404, detail="挂板不存在")
    return row


def _next_printing_plate_code(db: Session) -> str:
    next_number = int(db.scalar(select(func.coalesce(func.max(PrintingPlate.id), 0))) or 0) + 1
    return f"PL{next_number:06d}"


@router.get("/printing-plates")
def list_printing_plates(
    q: str | None = None,
    customer_id: int | None = Query(default=None, gt=0),
    include_inactive: bool = False,
    limit: int = Query(default=200, ge=1, le=500),
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    allowed_customer_ids = _mold_customer_scope(user, db)
    if allowed_customer_ids == set():
        return {"items": []}
    query = select(PrintingPlate).options(selectinload(PrintingPlate.customer))
    if allowed_customer_ids is not None:
        query = query.where(PrintingPlate.customer_id.in_(allowed_customer_ids))
    if customer_id is not None:
        require_customer_access(customer_id, current_user=user, db=db)
        query = query.where(PrintingPlate.customer_id == customer_id)
    if not include_inactive:
        query = query.where(PrintingPlate.status == "active")
    keyword = (q or "").strip()
    if keyword:
        pattern = f"%{keyword}%"
        linked_product_match = (
            select(Product.id)
            .where(
                or_(
                    Product.printing_plate_1_id == PrintingPlate.id,
                    Product.printing_plate_2_id == PrintingPlate.id,
                    Product.printing_plate_3_id == PrintingPlate.id,
                ),
                or_(
                    Product.product_code.like(pattern),
                    Product.customer_material_code.like(pattern),
                    Product.product_name.like(pattern),
                ),
            )
            .exists()
        )
        reuse_history_match = (
            select(PrintingPlateResinReuse.id)
            .where(
                PrintingPlateResinReuse.printing_plate_id == PrintingPlate.id,
                or_(
                    PrintingPlateResinReuse.plate_code_snapshot.like(pattern),
                    PrintingPlateResinReuse.from_customer_name_snapshot.like(pattern),
                    PrintingPlateResinReuse.from_plate_name_snapshot.like(pattern),
                    PrintingPlateResinReuse.from_color_name_snapshot.like(pattern),
                    PrintingPlateResinReuse.to_customer_name_snapshot.like(pattern),
                    PrintingPlateResinReuse.to_plate_name_snapshot.like(pattern),
                    PrintingPlateResinReuse.to_color_name_snapshot.like(pattern),
                ),
            )
            .exists()
        )
        query = query.where(
            or_(
                PrintingPlate.plate_code.like(pattern),
                PrintingPlate.plate_name.like(pattern),
                PrintingPlate.color_name.like(pattern),
                PrintingPlate.rack_location.like(pattern),
                PrintingPlate.remarks.like(pattern),
                PrintingPlate.customer.has(Customer.name.like(pattern)),
                linked_product_match,
                reuse_history_match,
            )
        )
    rows = db.scalars(
        query.order_by(
            PrintingPlate.rack_location,
            PrintingPlate.plate_code,
        ).limit(limit)
    ).all()
    products_by_plate = _printing_plate_products_by_plate(
        db, [row.id for row in rows], allowed_customer_ids
    )
    plate_ids = [row.id for row in rows]
    binding_counts = _printing_plate_binding_counts_by_plate(db, plate_ids)
    reuse_counts, latest_reuses = _printing_plate_reuse_summaries(db, plate_ids)
    archives = build_printing_plate_time_archives(
        db,
        rows,
        products_by_plate=products_by_plate,
        allowed_customer_ids=allowed_customer_ids,
    )
    return {
        "items": [
            _printing_plate_dict(
                db,
                row,
                allowed_customer_ids,
                time_archive=archives.get(row.id),
                products=products_by_plate.get(row.id, []),
                binding_count=binding_counts.get(row.id, 0),
                reuse_count=reuse_counts.get(row.id, 0),
                latest_reuse=latest_reuses.get(row.id),
            )
            for row in rows
        ]
    }


@router.post("/printing-plates", status_code=201)
def create_printing_plate(
    payload: PrintingPlateCreatePayload,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    customer = db.get(Customer, payload.customer_id)
    if customer is None:
        raise HTTPException(status_code=400, detail="客户不存在")
    _assert_asset_location_operational(
        db,
        asset_kind="printing_plate",
        location_text=payload.rack_location,
        claim_floor=True,
    )
    location = normalize_printing_plate_location(payload.rack_location)
    occupied = db.scalar(
        select(PrintingPlate.id).where(
            PrintingPlate.status.in_(("active", "damaged")),
            PrintingPlate.rack_location == location,
        )
    )
    if occupied is not None:
        raise HTTPException(status_code=409, detail="该挂板格位已被占用")
    row = PrintingPlate(
        plate_code=_next_printing_plate_code(db),
        customer_id=payload.customer_id,
        plate_name=payload.plate_name,
        color_name=payload.color_name,
        rack_location=location,
        remarks=payload.remarks,
        created_by=user.id,
        updated_by=user.id,
    )
    db.add(row)
    try:
        db.commit()
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="挂板编号或格位冲突，请刷新后重试",
        ) from error
    db.refresh(row)
    return _printing_plate_dict(db, row)


@router.put("/printing-plates/{plate_id}")
def update_printing_plate(
    plate_id: int,
    payload: PrintingPlateUpdatePayload,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    row = _printing_plate_or_404(db, plate_id)
    normalized_location = normalize_printing_plate_location(payload.rack_location)
    if normalized_location != row.rack_location:
        raise HTTPException(
            status_code=409,
            detail="挂板位置不能在档案编辑中直接修改，请使用挂板编号 + 位置码移动确认",
        )
    claimed = db.execute(
        update(PrintingPlate)
        .where(
            PrintingPlate.id == plate_id,
            PrintingPlate.version == payload.expected_version,
        )
        .values(
            plate_name=payload.plate_name,
            color_name=payload.color_name,
            remarks=payload.remarks,
            version=payload.expected_version + 1,
            updated_by=user.id,
        )
        .execution_options(synchronize_session=False)
    )
    if claimed.rowcount != 1:
        db.rollback()
        raise HTTPException(status_code=409, detail="挂板资料已变化，请刷新后重试")
    db.commit()
    return _printing_plate_dict(db, _printing_plate_or_404(db, plate_id))


@router.put("/printing-plates/{plate_id}/status")
def update_printing_plate_status(
    plate_id: int,
    payload: PrintingPlateStatusPayload,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    row = _printing_plate_or_404(db, plate_id)
    if payload.status != "active" and db.scalar(
        select(Product.id).where(_printing_plate_product_filter(plate_id)).limit(1)
    ) is not None:
        raise HTTPException(status_code=409, detail="该挂板仍绑定常用箱，请先解除绑定")
    if payload.status == "active":
        _assert_asset_location_operational(
            db,
            asset_kind="printing_plate",
            location_text=row.rack_location,
            claim_floor=True,
        )
        occupant = db.scalar(
            select(PrintingPlate.id).where(
                PrintingPlate.id != plate_id,
                PrintingPlate.status.in_(("active", "damaged")),
                PrintingPlate.rack_location == row.rack_location,
            )
        )
        if occupant is not None:
            raise HTTPException(status_code=409, detail="当前格位已被其他挂板占用")
    claimed = db.execute(
        update(PrintingPlate)
        .where(
            PrintingPlate.id == plate_id,
            PrintingPlate.version == payload.expected_version,
        )
        .values(
            status=payload.status,
            version=payload.expected_version + 1,
            updated_by=user.id,
        )
        .execution_options(synchronize_session=False)
    )
    if claimed.rowcount != 1:
        db.rollback()
        raise HTTPException(status_code=409, detail="挂板资料已变化，请刷新后重试")
    db.commit()
    return _printing_plate_dict(db, _printing_plate_or_404(db, plate_id))


def _printing_plate_reuse_target_customer(
    db: Session,
    customer_id: int,
) -> Customer:
    customer = db.get(Customer, customer_id)
    if customer is None:
        raise HTTPException(status_code=400, detail="新树脂版客户不存在")
    if not customer.is_active:
        raise HTTPException(status_code=409, detail="新树脂版客户已停用，不能登记新用途")
    return customer


def _printing_plate_reuse_preview_dict(
    db: Session,
    preview: PrintingPlateResinReusePreview,
) -> dict:
    return {
        "plate": _printing_plate_dict(
            db,
            preview.plate,
            binding_count=preview.binding_count,
        ),
        "target_customer": {
            "id": preview.target_customer.id,
            "customer_code": preview.target_customer.customer_code,
            "name": preview.target_customer.name,
        },
        "expected_version": preview.plate.version,
        "eligible": preview.eligible,
        "binding_count": preview.binding_count,
        "blockers": list(preview.blockers),
        "physical_rule": (
            "实体挂板编号保持不变；确认旧树脂版已撕除、新树脂版已贴好后，"
            "系统只更新当前用途并永久保留换版历史。"
        ),
    }


@router.post("/printing-plates/{plate_id}/resin-reuse/preview")
def preview_printing_plate_resin_reuse_endpoint(
    plate_id: int,
    payload: PrintingPlateResinReusePreviewPayload,
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    plate = _printing_plate_or_404(db, plate_id)
    target_customer = _printing_plate_reuse_target_customer(
        db, payload.target_customer_id
    )
    require_customer_access(plate.customer_id, current_user=user, db=db)
    require_customer_access(target_customer.id, current_user=user, db=db)
    preview = preview_printing_plate_resin_reuse(
        db,
        plate=plate,
        target_customer=target_customer,
    )
    return _printing_plate_reuse_preview_dict(db, preview)


@router.post("/printing-plates/{plate_id}/resin-reuse/confirm")
def confirm_printing_plate_resin_reuse_endpoint(
    plate_id: int,
    payload: PrintingPlateResinReuseConfirmPayload,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    try:
        plate = _printing_plate_or_404(db, plate_id)
        target_customer = _printing_plate_reuse_target_customer(
            db, payload.target_customer_id
        )
        result = confirm_printing_plate_resin_reuse(
            db,
            plate=plate,
            target_customer=target_customer,
            target_plate_name=payload.target_plate_name,
            target_color_name=payload.target_color_name,
            expected_version=payload.expected_version,
            idempotency_key=payload.idempotency_key,
            actor_id=user.id,
            actor_username=user.username,
            old_resin_removed=payload.old_resin_removed,
            new_resin_mounted=payload.new_resin_mounted,
        )
        if not result.replayed:
            db.add(
                OperationLog(
                    user_id=user.id,
                    username=user.username,
                    role=user.role,
                    action="UPDATE",
                    resource=(
                        f"warehouse/printing-plates/{plate_id}/resin-reuse"
                    ),
                    entity_type="printing_plate",
                    entity_id=plate_id,
                    description="现场确认挂板旧树脂版撕除并换版复用",
                    details=json.dumps(
                        {
                            "reuse_id": result.reuse.id,
                            "plate_code": result.reuse.plate_code_snapshot,
                            "from_customer_id": result.reuse.from_customer_id,
                            "to_customer_id": result.reuse.to_customer_id,
                            "from_plate_name": result.reuse.from_plate_name_snapshot,
                            "to_plate_name": result.reuse.to_plate_name_snapshot,
                            "from_color_name": result.reuse.from_color_name_snapshot,
                            "to_color_name": result.reuse.to_color_name_snapshot,
                            "rack_location": result.reuse.rack_location_snapshot,
                            "expected_version": result.reuse.expected_version,
                            "resulting_version": result.reuse.resulting_version,
                            "idempotency_key": result.reuse.idempotency_key,
                        },
                        ensure_ascii=False,
                    ),
                    ip_address=request.client.host if request.client else None,
                    user_agent=request.headers.get("user-agent"),
                )
            )
        db.commit()
        current_plate = _printing_plate_or_404(db, plate_id)
        return {
            "message": (
                "该换版确认已处理，本次未重复写入"
                if result.replayed
                else "挂板换版复用已确认；实体挂板编号和第 2 层位置保持不变"
            ),
            "plate": _printing_plate_dict(db, current_plate),
            "reuse": _printing_plate_reuse_dict(result.reuse),
            "idempotent_replay": result.replayed,
        }
    except PrintingPlateResinReuseError as error:
        db.rollback()
        raise HTTPException(status_code=error.status_code, detail=str(error)) from error
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="挂板换版资料、版本或幂等键冲突，请重新预览",
        ) from error


@router.get("/printing-plates/{plate_id}/resin-reuses")
def list_printing_plate_resin_reuses(
    plate_id: int,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    plate = _printing_plate_or_404(db, plate_id)
    require_customer_access(plate.customer_id, current_user=user, db=db)
    query = select(PrintingPlateResinReuse).where(
        PrintingPlateResinReuse.printing_plate_id == plate_id
    )
    total = int(
        db.scalar(
            select(func.count(PrintingPlateResinReuse.id)).where(
                PrintingPlateResinReuse.printing_plate_id == plate_id
            )
        )
        or 0
    )
    rows = db.scalars(
        query.order_by(
            PrintingPlateResinReuse.reused_at.desc(),
            PrintingPlateResinReuse.id.desc(),
        )
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return {
        "items": [_printing_plate_reuse_dict(row) for row in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


def _printing_plate_preview_dict(
    db: Session, preview: PrintingPlateLocationPreview
) -> dict:
    return {
        "plate": _printing_plate_dict(db, preview.plate),
        "target_location": preview.target_location,
        "target_guide": preview.target_guide,
        "expected_version": preview.plate.location_version,
        "same_location": preview.same_location,
        "can_confirm": preview.occupant is None,
        "occupancy_conflict": (
            {
                "printing_plate_id": preview.occupant.id,
                "plate_code": preview.occupant.plate_code,
            }
            if preview.occupant is not None
            else None
        ),
    }


@router.post("/printing-plates/location-movement/preview")
def preview_printing_plate_location_movement(
    payload: PrintingPlateLocationPreviewPayload,
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    try:
        _assert_asset_location_operational(
            db,
            asset_kind="printing_plate",
            location_text=payload.target_location,
        )
        preview = preview_printing_plate_move(
            db,
            plate_code=payload.plate_code,
            target_location=payload.target_location,
        )
        require_customer_access(preview.plate.customer_id, current_user=user, db=db)
        return _printing_plate_preview_dict(db, preview)
    except PrintingPlateLocationError as error:
        raise HTTPException(status_code=error.status_code, detail=str(error)) from error


@router.post("/printing-plates/location-movement/confirm")
def confirm_printing_plate_location_movement(
    payload: PrintingPlateLocationConfirmPayload,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    try:
        _assert_asset_location_operational(
            db,
            asset_kind="printing_plate",
            location_text=payload.target_location,
            claim_floor=True,
        )
        preview = preview_printing_plate_move(
            db,
            plate_code=payload.plate_code,
            target_location=payload.target_location,
        )
        require_customer_access(preview.plate.customer_id, current_user=user, db=db)
        result = confirm_printing_plate_move(
            db,
            plate_code=payload.plate_code,
            target_location=payload.target_location,
            expected_version=payload.expected_version,
            idempotency_key=payload.idempotency_key,
            actor_id=user.id,
            source=payload.source,
            note=payload.note,
        )
        if result.movement is not None and not result.replayed:
            db.add(
                OperationLog(
                    user_id=user.id,
                    username=user.username,
                    role=user.role,
                    action="UPDATE",
                    resource=f"warehouse/printing-plates/{result.plate.id}/location",
                    entity_type="printing_plate",
                    entity_id=result.plate.id,
                    description="双码确认挂板位置移动",
                    details=json.dumps(
                        {
                            "movement_id": result.movement.id,
                            "plate_code": result.movement.plate_code_snapshot,
                            "from_location": result.movement.from_location,
                            "to_location": result.movement.to_location,
                            "expected_version": result.movement.expected_version,
                            "resulting_version": result.movement.resulting_version,
                            "idempotency_key": result.movement.idempotency_key,
                        },
                        ensure_ascii=False,
                    ),
                    ip_address=request.client.host if request.client else None,
                    user_agent=request.headers.get("user-agent"),
                )
            )
        db.commit()
        return {
            "message": "挂板已在目标位置，无需移动" if result.no_change else "挂板位置移动已确认",
            "plate": _printing_plate_dict(db, result.plate),
            "movement": (
                {
                    "id": result.movement.id,
                    "from_location": result.movement.from_location,
                    "to_location": result.movement.to_location,
                    "expected_version": result.movement.expected_version,
                    "resulting_version": result.movement.resulting_version,
                }
                if result.movement is not None
                else None
            ),
            "idempotent_replay": result.replayed,
            "no_change": result.no_change,
        }
    except PrintingPlateLocationError as error:
        db.rollback()
        raise HTTPException(status_code=error.status_code, detail=str(error)) from error
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(status_code=409, detail="挂板位置或幂等键冲突，请重新预览") from error


def _visible_mold_customer_links(
    row: MoldTool,
    allowed_customer_ids: set[int] | None,
) -> list[MoldToolCustomer]:
    return sorted(
        (
            link
            for link in row.customer_links
            if allowed_customer_ids is None or link.customer_id in allowed_customer_ids
        ),
        key=lambda link: (
            link.display_order is None,
            link.display_order or 99,
            link.customer.name,
            link.id,
        ),
    )


def _mold_display_name(
    row: MoldTool,
    allowed_customer_ids: set[int] | None = None,
) -> str:
    if row.identity_status != "frozen" or not row.label_name:
        return row.mold_name
    primary_links = [
        link
        for link in _visible_mold_customer_links(row, allowed_customer_ids)
        if link.display_order is not None
    ]
    if not primary_links:
        parts = [row.label_name, row.chinese_short_name]
        return " ".join(str(value).strip() for value in parts if value)
    short_names = [
        str(link.customer.chinese_short_name or "").strip() or "简称待完善"
        for link in primary_links
    ]
    try:
        return compose_mold_display_name(
            short_names,
            row.label_name,
            row.chinese_short_name,
        )
    except MoldIdentityError:
        return row.mold_name


def _mold_customer_dict(link: MoldToolCustomer) -> dict:
    return {
        "customer_id": link.customer_id,
        "customer_name": link.customer.name,
        "customer_code": link.customer.customer_code,
        "chinese_short_name": link.customer.chinese_short_name,
        "display_order": link.display_order,
    }


def _formal_mold_identity(payload: MoldToolPayload, db: Session) -> tuple[str, str | None, list[Customer]]:
    try:
        label_name = normalize_mold_label_name(payload.label_name)
        chinese_short_name = normalize_mold_chinese_short_name(
            payload.chinese_short_name
        )
    except MoldIdentityError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    associations = payload.customers or []
    customers = db.scalars(
        select(Customer).where(
            Customer.id.in_([item.customer_id for item in associations]),
            Customer.is_active.is_(True),
        )
    ).all()
    customer_by_id = {row.id: row for row in customers}
    if len(customer_by_id) != len(associations):
        raise HTTPException(status_code=409, detail="所选客户不存在或已停用，请刷新后重试")
    primary = sorted(
        (item for item in associations if item.display_order is not None),
        key=lambda item: item.display_order or 99,
    )
    primary_short_names: list[str] = []
    for item in primary:
        customer = customer_by_id[item.customer_id]
        short_name = str(customer.chinese_short_name or "").strip()
        if not short_name:
            raise HTTPException(
                status_code=409,
                detail=f"客户“{customer.name}”尚未维护中文简称，请先到客户资料补充",
            )
        primary_short_names.append(short_name)
    try:
        display_name = compose_mold_display_name(
            primary_short_names,
            label_name,
            chinese_short_name,
        )
    except MoldIdentityError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    ordered_customers = [customer_by_id[item.customer_id] for item in associations]
    return display_name, chinese_short_name, ordered_customers


def _mold_master_request_hash(
    *,
    action: str,
    mold_id: int | None,
    payload: MoldToolPayload,
) -> str:
    canonical = {
        "action": action,
        "mold_id": mold_id,
        "expected_version": payload.expected_version,
        "label_name": payload.label_name,
        "chinese_short_name": payload.chinese_short_name,
        "customers": sorted(
            (
                {
                    "customer_id": item.customer_id,
                    "display_order": item.display_order,
                }
                for item in (payload.customers or [])
            ),
            key=lambda item: item["customer_id"],
        ),
        "rack_location": payload.rack_location,
        "remarks": payload.remarks,
    }
    # Omit the new field entirely for legacy requests.  Their already-issued
    # idempotency keys must keep the exact request hash across this release.
    if payload.label_overrides is not None:
        canonical["label_overrides"] = payload.label_overrides.as_dict()
    return hashlib.sha256(
        json.dumps(
            canonical,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _mold_master_replay(
    db: Session,
    *,
    action: str,
    idempotency_key: str,
    request_hash: str,
    actor_id: int,
) -> dict | None:
    mutation = db.scalar(
        select(MoldMasterMutation).where(
            MoldMasterMutation.idempotency_key == idempotency_key
        )
    )
    if mutation is None:
        return None
    if (
        mutation.action != action
        or mutation.request_hash != request_hash
        or mutation.actor_id != actor_id
    ):
        raise HTTPException(status_code=409, detail="模具资料保存凭证已被其他请求使用")
    try:
        snapshot = json.loads(mutation.result_snapshot_json)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise HTTPException(
            status_code=409,
            detail="模具保存回放记录损坏，请联系管理员核对",
        ) from error
    if not isinstance(snapshot, dict) or int(snapshot.get("id") or 0) != int(
        mutation.mold_tool_id
    ):
        raise HTTPException(
            status_code=409,
            detail="模具保存回放记录与模具身份不一致，请联系管理员核对",
        )
    return snapshot


def _replace_mold_customer_links(
    db: Session,
    *,
    row: MoldTool,
    payload: MoldToolPayload,
    customers: list[Customer],
    actor_id: int,
) -> None:
    associations = payload.customers or []
    customer_by_id = {customer.id: customer for customer in customers}
    for link in list(row.customer_links):
        db.delete(link)
    db.flush()
    for item in associations:
        row.customer_links.append(
            MoldToolCustomer(
                customer=customer_by_id[item.customer_id],
                display_order=item.display_order,
                created_by=actor_id,
            )
        )


def _visible_mold_products(
    row: MoldTool,
    allowed_customer_ids: set[int] | None,
) -> list[Product]:
    products = sorted(
        (
            product
            for product in row.products
            if product.deleted_at is None and product.is_active
            and (
                allowed_customer_ids is None
                or product.customer_id in allowed_customer_ids
            )
        ),
        key=lambda product: (product.customer.name if product.customer else "", product.product_code, product.id),
    )
    return products


def _historical_visible_mold_products(
    row: MoldTool,
    allowed_customer_ids: set[int] | None,
) -> list[Product]:
    """Return historical bindings without crossing the caller's customer scope."""

    return sorted(
        (
            product
            for product in row.products
            if (
                allowed_customer_ids is None
                or product.customer_id in allowed_customer_ids
            )
        ),
        key=lambda product: (
            product.customer.name if product.customer else "",
            product.product_code,
            product.id,
        ),
    )


def _require_mold_customer_scope(
    row: MoldTool,
    allowed_customer_ids: set[int] | None,
    *,
    include_historical: bool = False,
) -> None:
    visible_products = (
        _historical_visible_mold_products(row, allowed_customer_ids)
        if include_historical
        else _visible_mold_products(row, allowed_customer_ids)
    )
    visible_customer_links = _visible_mold_customer_links(
        row,
        allowed_customer_ids,
    )
    if (
        allowed_customer_ids is not None
        and not visible_products
        and not visible_customer_links
    ):
        raise HTTPException(status_code=403, detail="无客户访问权限")


def _mold_binding_dict(row: MoldTool, product: Product) -> dict:
    material = product.material
    customer_name = product.customer.name if product.customer else None
    customer_code = product.customer.customer_code if product.customer else None
    material_code = (
        material.code
        if material is not None and str(material.code or "").strip()
        else product.default_material_code or product.legacy_material_text
    )
    formal_customer_link = next(
        (
            link
            for link in row.customer_links
            if link.customer_id == product.customer_id
        ),
        None,
    )
    return {
        "id": product.id,
        "customer_id": product.customer_id,
        "customer_code": customer_code,
        "customer_name": customer_name,
        "customer_short_name": (
            formal_customer_link.customer.chinese_short_name
            if row.identity_status == "frozen" and formal_customer_link is not None
            else mold_customer_short_name(
                row.mold_name,
                customer_name,
                customer_code,
            )
        ),
        "product_code": product.product_code,
        "product_name": product.product_name,
        "customer_material_code": product.customer_material_code,
        "version": product.version,
        "specification": " × ".join(
            str(round(value))
            for value in (product.length_mm, product.width_mm, product.height_mm)
            if value is not None
        ),
        "report_specification": " × ".join(
            str(round(value))
            for value in (product.report_length_mm, product.report_width_mm)
            if value is not None
        ),
        "material_code": material_code,
        "material_composition": (
            material.paper_composition if material is not None else None
        ),
        "layer_count": (
            product.layer_count
            if product.layer_count is not None
            else (material.layer_count if material is not None else None)
        ),
        "flute_type": (
            product.flute_type
            or (material.flute_type if material is not None else None)
        ),
        "production_process": product.production_process,
        "direction_note": product.report_notes,
    }


def _mold_live_binding_dict(row: MoldTool, product: Product) -> dict:
    """Project only the non-sensitive product facts needed by the scan page."""

    payload = _mold_binding_dict(row, product)
    return {
        "product_id": payload["id"],
        **{
            key: payload[key]
            for key in (
                "customer_name",
                "customer_short_name",
                "product_code",
                "product_name",
                "specification",
                "report_specification",
                "material_code",
                "material_composition",
                "layer_count",
                "flute_type",
            )
        },
    }


def _mold_tool_dict(
    row: MoldTool,
    allowed_customer_ids: set[int] | None = None,
    *,
    label_print_status: dict | None = None,
    time_archive: dict | None = None,
    historical_products: list[Product] | None = None,
) -> dict:
    products = _visible_mold_products(row, allowed_customer_ids)
    historical_products = (
        (
            _historical_visible_mold_products(row, allowed_customer_ids)
            if allowed_customer_ids is None
            else products
        )
        if historical_products is None
        else historical_products
    )
    archive_candidate = (
        mold_archive_candidate(row) if allowed_customer_ids is None else None
    )
    customer_links = _visible_mold_customer_links(row, allowed_customer_ids)
    associated_customers = [_mold_customer_dict(link) for link in customer_links]
    primary_customers = [
        item for item in associated_customers if item["display_order"] is not None
    ]
    display_name = _mold_display_name(row, allowed_customer_ids)
    return {
        "id": row.id,
        "mold_code": row.mold_code,
        "internal_code": row.mold_code,
        "mold_name": display_name,
        "display_name": display_name,
        "label_name": row.label_name,
        "chinese_short_name": row.chinese_short_name,
        # A restricted customer scope may read the mold row, but must not see
        # an administrator's label-only free text for another linked customer.
        # The edit preview itself is limited to unrestricted accounts.
        "label_overrides": (
            parse_label_overrides(getattr(row, "label_overrides_json", None))
            if allowed_customer_ids is None
            else {field: None for field in LABEL_OVERRIDE_FIELDS}
        ),
        "identity_status": row.identity_status,
        "identity_ready": row.identity_status == "frozen",
        "identity_issue": (
            None
            if row.identity_status == "frozen"
            else "客户与标签三字段待完善"
        ),
        "version": row.version,
        "associated_customers": associated_customers,
        "primary_customers": primary_customers,
        "rack_location": row.rack_location,
        "location_guide": describe_mold_location(row.rack_location),
        "location_version": row.location_version,
        "repair_status": row.repair_status,
        "repair_status_label": "待维修" if row.repair_status == "needs_repair" else "正常",
        "repair_version": row.repair_version,
        "last_location_confirmed_at": (
            utc_naive_to_api(row.last_location_confirmed_at)
            if row.last_location_confirmed_at
            else None
        ),
        "last_location_confirmed_by": row.last_location_confirmed_by,
        "remarks": row.remarks,
        "is_active": row.is_active,
        "archive_status": row.archive_status,
        "archived_at": utc_naive_to_api(row.archived_at) if row.archived_at else None,
        "archived_by": row.archived_by,
        "archive_reason": row.archive_reason,
        "pre_archive_location": row.pre_archive_location,
        "restored_at": utc_naive_to_api(row.restored_at) if row.restored_at else None,
        "restored_by": row.restored_by,
        "archive_candidate": archive_candidate,
        "archive_area_code": MOLD_ARCHIVE_AREA_CODE,
        "product_count": len(products),
        "products": [_mold_binding_dict(row, product) for product in products],
        "binding_history": [
            {
                "id": product.id,
                "customer_id": product.customer_id,
                "customer_name": product.customer.name if product.customer else None,
                "product_code": product.product_code,
                "product_name": product.product_name,
                "is_active": bool(product.is_active and product.deleted_at is None),
                "deleted_at": utc_naive_to_api(product.deleted_at) if product.deleted_at else None,
            }
            for product in historical_products
        ],
        "created_at": utc_naive_to_api(row.created_at),
        "updated_at": utc_naive_to_api(row.updated_at) if row.updated_at else None,
        "time_archive": time_archive,
        "label_print_status": label_print_status
        or {
            "printed": False,
            "label": "未打印",
            "last_printed_at": None,
            "last_printed_by": None,
            "print_count": 0,
        },
    }


def _mold_label_print_statuses(
    db: Session,
    mold_ids: list[int],
) -> dict[int, dict]:
    if not mold_ids:
        return {}
    rows = db.execute(
        select(
            MoldLabelPrintJobItem.mold_tool_id,
            MoldLabelPrintJob.printed_at,
            MoldLabelPrintJob.printed_by_username,
        )
        .join(
            MoldLabelPrintJob,
            MoldLabelPrintJob.id == MoldLabelPrintJobItem.print_job_id,
        )
        .where(MoldLabelPrintJobItem.mold_tool_id.in_(mold_ids))
        .order_by(
            MoldLabelPrintJobItem.mold_tool_id,
            MoldLabelPrintJob.printed_at.desc(),
            MoldLabelPrintJob.id.desc(),
        )
    ).all()
    result: dict[int, dict] = {}
    for mold_id, printed_at, printed_by_username in rows:
        current = result.get(mold_id)
        if current is None:
            current = {
                "printed": True,
                "label": "已打印",
                "last_printed_at": (
                    utc_naive_to_api(printed_at) if printed_at else None
                ),
                "last_printed_by": printed_by_username,
                "print_count": 0,
            }
            result[mold_id] = current
        current["print_count"] += 1
    return result


def _mold_tools_query(
    *,
    db: Session,
    user: User,
    q: str | None,
    include_inactive: bool,
    customer_ids: set[int] | None = None,
    customer_keyword: str | None = None,
    product_code: str | None = None,
    rack_location: str | None = None,
    include_unprinted: bool = True,
    include_repair: bool = True,
    include_archived: bool = True,
):
    allowed_customer_ids = _mold_customer_scope(user, db)
    supplied_customer_ids = set(customer_ids or set())
    requested_customer_ids = set(supplied_customer_ids)
    if allowed_customer_ids is not None:
        requested_customer_ids &= allowed_customer_ids
    query = select(MoldTool).options(
        selectinload(MoldTool.customer_links).selectinload(MoldToolCustomer.customer),
        selectinload(MoldTool.products).selectinload(Product.customer),
        selectinload(MoldTool.products).selectinload(Product.material),
    )
    if allowed_customer_ids is not None:
        query = query.where(
            or_(
                MoldTool.id.in_(
                    select(MoldToolCustomer.mold_tool_id).where(
                        MoldToolCustomer.customer_id.in_(allowed_customer_ids)
                    )
                ),
                MoldTool.id.in_(
                select(Product.mold_tool_id).where(
                    Product.mold_tool_id.is_not(None),
                    Product.deleted_at.is_(None),
                    Product.is_active.is_(True),
                    Product.customer_id.in_(allowed_customer_ids),
                ),
                ),
            )
        )
    if not include_inactive:
        query = query.where(
            or_(
                MoldTool.is_active.is_(True),
                MoldTool.archive_status == "archived",
            )
        )
    if not include_archived:
        query = query.where(MoldTool.archive_status != "archived")
    if not include_repair:
        query = query.where(MoldTool.repair_status == "normal")
    if not include_unprinted:
        query = query.where(
            MoldTool.id.in_(select(MoldLabelPrintJobItem.mold_tool_id))
        )

    keyword = (q or "").strip()
    normalized_customer_keyword = (customer_keyword or "").strip()
    legacy_customer_ids = (
        requested_customer_ids
        if supplied_customer_ids and not normalized_customer_keyword and keyword
        else set()
    )
    if normalized_customer_keyword or (supplied_customer_ids and not keyword):
        customer_matches = []
        if normalized_customer_keyword:
            customer_pattern = f"%{normalized_customer_keyword}%"
            customer_matches.extend(
                (
                    MoldTool.id.in_(
                        select(MoldToolCustomer.mold_tool_id)
                        .join(Customer, Customer.id == MoldToolCustomer.customer_id)
                        .where(
                            *(
                                [
                                    MoldToolCustomer.customer_id.in_(
                                        allowed_customer_ids
                                    )
                                ]
                                if allowed_customer_ids is not None
                                else []
                            ),
                            or_(
                                Customer.name.like(customer_pattern),
                                Customer.chinese_short_name.like(customer_pattern),
                                Customer.customer_code.like(customer_pattern),
                            ),
                        )
                    ),
                    MoldTool.id.in_(
                        select(Product.mold_tool_id)
                        .join(Customer, Customer.id == Product.customer_id)
                        .where(
                            Product.mold_tool_id.is_not(None),
                            Product.deleted_at.is_(None),
                            Product.is_active.is_(True),
                            *(
                                [Product.customer_id.in_(allowed_customer_ids)]
                                if allowed_customer_ids is not None
                                else []
                            ),
                            or_(
                                Customer.name.like(customer_pattern),
                                Customer.chinese_short_name.like(customer_pattern),
                                Customer.customer_code.like(customer_pattern),
                            ),
                        )
                    ),
                )
            )
        if requested_customer_ids:
            customer_matches.extend(
                (
                    MoldTool.id.in_(
                        select(MoldToolCustomer.mold_tool_id).where(
                            MoldToolCustomer.customer_id.in_(requested_customer_ids)
                        )
                    ),
                    MoldTool.id.in_(
                        select(Product.mold_tool_id).where(
                            Product.mold_tool_id.is_not(None),
                            Product.deleted_at.is_(None),
                            Product.is_active.is_(True),
                            Product.customer_id.in_(requested_customer_ids),
                        )
                    ),
                )
            )
        query = query.where(or_(*customer_matches) if customer_matches else False)

    normalized_product_code = (product_code or "").strip()
    if normalized_product_code:
        product_pattern = f"%{normalized_product_code}%"
        query = query.where(
            MoldTool.id.in_(
                select(Product.mold_tool_id).where(
                    Product.mold_tool_id.is_not(None),
                    Product.deleted_at.is_(None),
                    Product.is_active.is_(True),
                    *(
                        [Product.customer_id.in_(allowed_customer_ids)]
                        if allowed_customer_ids is not None
                        else []
                    ),
                    or_(
                        Product.product_code.like(product_pattern),
                        Product.customer_material_code.like(product_pattern),
                        Product.product_name.like(product_pattern),
                    ),
                )
            )
        )

    normalized_rack_location = (rack_location or "").strip()
    if normalized_rack_location:
        query = query.where(
            MoldTool.rack_location.like(f"%{normalized_rack_location}%")
        )
    if keyword:
        linked_scope_filters = []
        if allowed_customer_ids is not None:
            linked_scope_filters.append(
                Product.customer_id.in_(allowed_customer_ids)
            )
        product_state_filters = (
            [Product.deleted_at.is_(None), Product.is_active.is_(True)]
            if allowed_customer_ids is not None
            else []
        )
        # A word may match any operational identity field, while multiple
        # words narrow the same single search box.  Named filters above remain
        # independent AND conditions.
        keyword_clauses = []
        for token in re.split(r"\s+", keyword):
            if not token:
                continue
            pattern = f"%{token}%"
            linked_molds = (
                select(Product.mold_tool_id)
                .join(Customer, Customer.id == Product.customer_id)
                .where(
                    Product.mold_tool_id.is_not(None),
                    *product_state_filters,
                    *linked_scope_filters,
                    or_(
                        Product.product_code.like(pattern),
                        Product.customer_material_code.like(pattern),
                        Product.product_name.like(pattern),
                        Customer.name.like(pattern),
                        Customer.chinese_short_name.like(pattern),
                        Customer.customer_code.like(pattern),
                    ),
                )
            )
            associated_molds = (
                select(MoldToolCustomer.mold_tool_id)
                .join(Customer, Customer.id == MoldToolCustomer.customer_id)
                .where(
                    *(
                        [MoldToolCustomer.customer_id.in_(allowed_customer_ids)]
                        if allowed_customer_ids is not None
                        else []
                    ),
                    or_(
                        Customer.name.like(pattern),
                        Customer.chinese_short_name.like(pattern),
                        Customer.customer_code.like(pattern),
                    ),
                )
            )
            keyword_clauses.append(
                or_(
                    MoldTool.mold_code.like(pattern),
                    MoldTool.mold_name.like(pattern),
                    MoldTool.label_name.like(pattern),
                    MoldTool.chinese_short_name.like(pattern),
                    MoldTool.rack_location.like(pattern),
                    MoldTool.remarks.like(pattern),
                    MoldTool.id.in_(linked_molds),
                    MoldTool.id.in_(associated_molds),
                    *(
                        [
                            MoldTool.id.in_(
                                select(MoldToolCustomer.mold_tool_id).where(
                                    MoldToolCustomer.customer_id.in_(legacy_customer_ids)
                                )
                            ),
                            MoldTool.id.in_(
                                select(Product.mold_tool_id).where(
                                    Product.mold_tool_id.is_not(None),
                                    Product.deleted_at.is_(None),
                                    Product.is_active.is_(True),
                                    Product.customer_id.in_(legacy_customer_ids),
                                )
                            ),
                        ]
                        if legacy_customer_ids
                        else []
                    ),
                )
            )
        if keyword_clauses:
            query = query.where(and_(*keyword_clauses))
    return query, allowed_customer_ids


@router.get("/molds")
def list_mold_tools(
    q: str | None = None,
    customer_keyword: Annotated[str | None, Query(max_length=150)] = None,
    customer_ids: str | None = Query(default=None, max_length=400),
    product_code: Annotated[str | None, Query(max_length=150)] = None,
    rack_location: Annotated[str | None, Query(max_length=150)] = None,
    include_inactive: bool = False,
    include_archived: bool = True,
    include_unprinted: bool = True,
    include_repair: bool = True,
    sort_by: Literal["location", "updated_desc", "status_attention"] = "location",
    limit: int = Query(default=200, ge=1, le=500),
    page: int | None = Query(default=None, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    resolved_customer_ids: set[int] = set()
    for value in str(customer_ids or "").split(","):
        normalized = value.strip()
        if not normalized:
            continue
        if not normalized.isdigit() or int(normalized) <= 0:
            raise HTTPException(status_code=422, detail="客户搜索条件无效")
        resolved_customer_ids.add(int(normalized))
    if len(resolved_customer_ids) > 50:
        raise HTTPException(status_code=422, detail="客户搜索条件过多")
    query, allowed_customer_ids = _mold_tools_query(
        db=db,
        user=user,
        q=q,
        include_inactive=include_inactive,
        customer_ids=resolved_customer_ids,
        customer_keyword=customer_keyword,
        product_code=product_code,
        rack_location=rack_location,
        include_unprinted=include_unprinted,
        include_repair=include_repair,
        include_archived=include_archived,
    )
    if allowed_customer_ids == set():
        return {
            "items": [],
            "total": 0,
            "page": page or 1,
            "page_size": page_size if page is not None else limit,
        }
    total = int(
        db.scalar(
            select(func.count()).select_from(query.order_by(None).subquery())
        )
        or 0
    )
    if sort_by == "status_attention":
        printed = (
            select(MoldLabelPrintJobItem.id)
            .where(MoldLabelPrintJobItem.mold_tool_id == MoldTool.id)
            .exists()
        )
        status_rank = case(
            (~printed, 0),
            (MoldTool.repair_status == "needs_repair", 1),
            (MoldTool.archive_status == "archived", 2),
            (MoldTool.is_active.is_(False), 3),
            else_=4,
        )
        ordered_query = query.order_by(
            status_rank,
            func.coalesce(MoldTool.updated_at, MoldTool.created_at).desc(),
            MoldTool.id.desc(),
        )
    elif sort_by == "updated_desc":
        ordered_query = query.order_by(
            func.coalesce(MoldTool.updated_at, MoldTool.created_at).desc(),
            MoldTool.id.desc(),
        )
    else:
        ordered_query = query.order_by(
            MoldTool.rack_location,
            MoldTool.mold_code,
            MoldTool.id,
        )
    if page is not None:
        ordered_query = ordered_query.offset((page - 1) * page_size).limit(
            page_size
        )
    else:
        ordered_query = ordered_query.limit(limit)
    rows = db.scalars(ordered_query).unique().all()
    label_print_statuses = _mold_label_print_statuses(
        db,
        [row.id for row in rows],
    )
    return {
        "items": [
            _mold_tool_dict(
                row,
                allowed_customer_ids,
                label_print_status=label_print_statuses.get(row.id),
            )
            for row in rows
        ],
        "total": total,
        "page": page or 1,
        "page_size": page_size if page is not None else limit,
    }


@router.get("/molds/{mold_id}/detail")
def get_mold_tool_detail(
    mold_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    allowed_customer_ids = _mold_customer_scope(user, db)
    row = db.scalar(
        select(MoldTool)
        .options(
            selectinload(MoldTool.customer_links).selectinload(
                MoldToolCustomer.customer
            ),
            selectinload(MoldTool.products).selectinload(Product.customer),
        )
        .where(MoldTool.id == mold_id)
    )
    if row is None:
        raise HTTPException(status_code=404, detail="模具不存在")
    _require_mold_customer_scope(
        row,
        allowed_customer_ids,
        include_historical=True,
    )
    historical_products = _historical_visible_mold_products(
        row,
        allowed_customer_ids,
    )
    archive = build_mold_detail_timeline(
        db,
        row,
        products=historical_products,
        allowed_customer_ids=allowed_customer_ids,
    )
    result = _mold_tool_dict(
        row,
        allowed_customer_ids,
        time_archive={key: value for key, value in archive.items() if key != "timeline"},
        historical_products=historical_products,
    )
    result["timeline"] = archive["timeline"]
    return result


@router.get("/molds/{mold_id}/bound-products")
def list_mold_bound_products(
    mold_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    """Return current bindable rows without requiring label-print eligibility."""

    allowed_customer_ids = _mold_customer_scope(user, db)
    row = db.scalar(
        select(MoldTool)
        .options(
            selectinload(MoldTool.customer_links).selectinload(
                MoldToolCustomer.customer
            ),
            selectinload(MoldTool.products).selectinload(Product.customer),
            selectinload(MoldTool.products).selectinload(Product.material),
        )
        .where(MoldTool.id == mold_id)
    )
    if row is None:
        raise HTTPException(status_code=404, detail="模具不存在")
    _require_mold_customer_scope(row, allowed_customer_ids)
    products = _visible_mold_products(row, allowed_customer_ids)
    return {
        "mold": _mold_tool_dict(row, allowed_customer_ids),
        "items": [_mold_binding_dict(row, product) for product in products],
    }


@router.get("/molds/by-map-area")
def list_mold_tools_by_map_area(
    floor_code: Literal["1F", "3F", "4F"] = Query(default="1F"),
    feature_code: str = Query(min_length=1, max_length=100),
    q: str | None = Query(default=None, max_length=150),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(_can_locate_twin),
) -> dict:
    """List real mold masters assigned to one measured-map mold area."""

    try:
        floor = overlay_formal_area_bindings(
            db,
            floor_code=floor_code,
            floor_layout=load_warehouse_twin_floor(floor_code),
            include_draft=False,
        )
    except WarehouseTwinLayoutNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    normalized_feature_code = feature_code.strip().upper()
    feature = next(
        (
            row
            for row in (floor.get("features") or [])
            if str(row.get("feature_code") or "").strip().upper()
            == normalized_feature_code
        ),
        None,
    )
    if feature is None:
        raise HTTPException(status_code=404, detail="实测地图区域不存在")
    if "mold" not in str(feature.get("subtype") or "").casefold():
        raise HTTPException(status_code=422, detail="该实测地图区域不是模具区域")

    rack_codes = sorted(
        {
            str(row.get("mold_rack_code") or "").strip().upper()
            for row in (floor.get("racks") or [])
            if str(row.get("area_code") or "").strip().upper()
            == normalized_feature_code
            and str(row.get("mold_rack_code") or "").strip()
        }
    )
    family = str(feature.get("mold_location_family") or "").strip().upper()
    family_match = re.search(r"-(R\d+)(?:-|$)", family)
    if family_match:
        rack_codes = sorted({*rack_codes, family_match.group(1)})

    query, allowed_customer_ids = _mold_tools_query(
        db=db,
        user=user,
        q=q,
        include_inactive=False,
    )
    if allowed_customer_ids == set() or not rack_codes:
        return {
            "floor_code": floor_code,
            "feature_code": normalized_feature_code,
            "area_name": feature.get("name") or normalized_feature_code,
            "rack_codes": rack_codes,
            "items": [],
            "total": 0,
            "page": page,
            "page_size": page_size,
        }

    location_filters = []
    for rack_code in rack_codes:
        prefix = f"{floor_code}-M-{rack_code}"
        normalized_location = func.upper(func.trim(MoldTool.rack_location))
        location_filters.extend(
            (
                normalized_location == prefix,
                normalized_location.like(f"{prefix}-%"),
            )
        )
    query = query.where(or_(*location_filters))
    total = int(
        db.scalar(
            select(func.count()).select_from(query.order_by(None).subquery())
        )
        or 0
    )
    rows = db.scalars(
        query.order_by(
            MoldTool.rack_location,
            MoldTool.mold_code,
            MoldTool.id,
        )
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).unique().all()
    return {
        "floor_code": floor_code,
        "feature_code": normalized_feature_code,
        "area_name": feature.get("name") or normalized_feature_code,
        "rack_codes": rack_codes,
        "items": [
            _mold_tool_dict(row, allowed_customer_ids)
            for row in rows
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.get("/molds/by-map-rack")
def list_mold_tools_by_map_rack(
    floor_code: Literal["1F", "3F", "4F"] = Query(default="1F"),
    rack_id: str = Query(min_length=1, max_length=100),
    q: str | None = Query(default=None, max_length=150),
    db: Session = Depends(get_db),
    user: User = Depends(_can_locate_twin),
) -> dict:
    """Project real mold masters into one published measured-map rack."""

    try:
        floor = overlay_formal_area_bindings(
            db,
            floor_code=floor_code,
            floor_layout=load_warehouse_twin_floor(floor_code),
            include_draft=False,
        )
    except WarehouseTwinLayoutNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    rack = next(
        (
            row
            for row in (floor.get("racks") or [])
            if str(row.get("id") or "") == rack_id.strip()
        ),
        None,
    )
    if rack is None:
        raise HTTPException(status_code=404, detail="实测地图货架不存在")
    structure = mold_rack_structure(rack)
    rack_code = str(structure.get("mold_rack_code") or "").strip().upper()
    if not rack_code:
        raise HTTPException(status_code=422, detail="该货架不是模具资产货架")

    query, allowed_customer_ids = _mold_tools_query(
        db=db,
        user=user,
        q=q,
        include_inactive=False,
    )
    normalized_location = func.upper(func.trim(MoldTool.rack_location))
    prefix = f"{floor_code}-M-{rack_code}"
    query = query.where(
        or_(
            normalized_location == prefix,
            normalized_location.like(f"{prefix}-%"),
        )
    )
    total = int(
        db.scalar(select(func.count()).select_from(query.order_by(None).subquery()))
        or 0
    )
    rows = db.scalars(
        query.order_by(MoldTool.rack_location, MoldTool.mold_code, MoldTool.id)
        .limit(500)
    ).unique().all()
    return {
        "floor_code": floor_code,
        "rack": structure,
        "items": [_mold_tool_dict(row, allowed_customer_ids) for row in rows],
        "total": total,
        "truncated": total > len(rows),
    }


def _mold_location_preview_dict(
    preview: MoldLocationPreview,
    allowed_customer_ids: set[int] | None = None,
) -> dict:
    visible_occupants = [
        occupant
        for occupant in preview.occupants
        if allowed_customer_ids is None
        or bool(_visible_mold_products(occupant, allowed_customer_ids))
    ]
    return {
        "mold": _mold_tool_dict(preview.mold, allowed_customer_ids),
        "target_location": preview.target_location,
        "target_guide": preview.target_guide,
        "expected_version": preview.mold.location_version,
        "same_location": preview.same_location,
        # A rack/level/grid is a storage category, not a fixed left-to-right
        # slot. Multiple active molds may therefore share the same code.
        "can_confirm": True,
        "occupancy_conflict": None,
        "co_located_count": len(visible_occupants),
        "co_located_molds": [
            {
                "mold_tool_id": occupant.id,
                "mold_code": occupant.mold_code,
                "mold_name": occupant.mold_name,
            }
            for occupant in visible_occupants
        ],
    }


def _mold_location_movement_dict(row: MoldLocationMovement) -> dict:
    return {
        "id": row.id,
        "mold_tool_id": row.mold_tool_id,
        "mold_code": row.mold_code_snapshot,
        "from_location": row.from_location,
        "to_location": row.to_location,
        "actor_id": row.actor_id,
        "moved_at": utc_naive_to_api(row.moved_at),
        "idempotency_key": row.idempotency_key,
        "expected_version": row.expected_version,
        "resulting_version": row.resulting_version,
        "source": row.source,
        "note": row.note,
    }


def _mold_location_move_response(
    result: MoldLocationMoveResult,
    allowed_customer_ids: set[int] | None = None,
) -> dict:
    return {
        "message": (
            "模具已在目标位置，无需移动"
            if result.no_change
            else "模具位置移动已确认"
        ),
        "mold": _mold_tool_dict(result.mold, allowed_customer_ids),
        "movement": (
            _mold_location_movement_dict(result.movement)
            if result.movement is not None
            else None
        ),
        "idempotent_replay": result.replayed,
        "no_change": result.no_change,
    }


def _append_mold_location_move_log(
    db: Session,
    *,
    request: Request,
    user: User,
    result: MoldLocationMoveResult,
    description: str,
) -> None:
    if result.replayed or result.no_change or result.movement is None:
        return
    movement = result.movement
    db.add(
        OperationLog(
            user_id=user.id,
            username=user.username,
            role=user.role,
            action="UPDATE",
            resource=f"warehouse/molds/{result.mold.id}/location",
            entity_type="mold_tool",
            entity_id=result.mold.id,
            description=description,
            details=json.dumps(
                {
                    "movement_id": movement.id,
                    "mold_code": movement.mold_code_snapshot,
                    "from_location": movement.from_location,
                    "to_location": movement.to_location,
                    "expected_version": movement.expected_version,
                    "resulting_version": movement.resulting_version,
                    "idempotency_key": movement.idempotency_key,
                    "source": movement.source,
                    "note": movement.note,
                },
                ensure_ascii=False,
            ),
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )
    )


def _require_mold_archive_operator(user: User) -> None:
    if user.role not in {"admin", "boss"}:
        raise HTTPException(status_code=403, detail="只有管理员或老板可以封存和恢复模具")


def _mold_archive_response(result: MoldArchiveResult) -> dict:
    return {
        "message": "模具已封存待复用" if result.action == "archive" else "模具已恢复启用",
        "mold": _mold_tool_dict(result.mold),
        "movement": _mold_location_movement_dict(result.movement),
        "idempotent_replay": result.replayed,
    }


def _mold_repair_event_dict(row: MoldRepairEvent) -> dict:
    return {
        "id": row.id,
        "mold_tool_id": row.mold_tool_id,
        "mold_code": row.mold_code_snapshot,
        "before_status": row.before_status,
        "after_status": row.after_status,
        "expected_version": row.expected_version,
        "resulting_version": row.resulting_version,
        "operator_id": row.actor_id,
        "operator_name": row.actor_username_snapshot,
        "occurred_at": beijing_naive_to_api(row.occurred_at),
        "idempotency_key": row.idempotency_key,
    }


@router.post("/molds/{mold_id}/repair-status")
def update_mold_repair_status(
    mold_id: int,
    payload: MoldRepairStatusPayload,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    row = db.scalar(
        select(MoldTool)
        .options(selectinload(MoldTool.products))
        .where(MoldTool.id == mold_id)
    )
    if row is None:
        raise HTTPException(status_code=404, detail="模具不存在")
    _require_mold_customer_scope(row, _mold_customer_scope(user, db), include_historical=True)
    try:
        result = change_mold_repair_status(
            db,
            mold_id=mold_id,
            target_status=payload.target_status,
            expected_version=payload.expected_version,
            idempotency_key=payload.idempotency_key,
            actor=user,
        )
        if not result.replayed:
            append_audit_event(
                db,
                request=request,
                actor=user,
                event_category="business",
                result="success",
                source="web",
                module_code="warehouse",
                action_code=(
                    "mold.repair.start"
                    if payload.target_status == "needs_repair"
                    else "mold.repair.complete"
                ),
                legacy_action="MOLD_REPAIR_STATUS",
                resource="MoldTool",
                entity_type="mold_tool",
                entity_id=mold_id,
                object_ref=result.mold.mold_code,
                description=("模具标记待维修" if payload.target_status == "needs_repair" else "模具维修完毕"),
                details={
                    "event_id": result.event.id,
                    "before_status": result.event.before_status,
                    "after_status": result.event.after_status,
                    "expected_version": result.event.expected_version,
                    "resulting_version": result.event.resulting_version,
                    "idempotency_key": result.event.idempotency_key,
                },
            )
        db.commit()
        return {
            "message": "模具已标记待维修" if payload.target_status == "needs_repair" else "模具维修已完成",
            "mold": _mold_tool_dict(result.mold),
            "event": _mold_repair_event_dict(result.event),
            "idempotent_replay": result.replayed,
        }
    except MoldRepairError as error:
        db.rollback()
        raise HTTPException(status_code=error.status_code, detail=str(error)) from error
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(status_code=409, detail="模具维修状态已变化，请刷新后重试") from error


def _append_mold_archive_log(
    db: Session,
    *,
    request: Request,
    user: User,
    result: MoldArchiveResult,
) -> None:
    if result.replayed:
        return
    movement = result.movement
    db.add(
        OperationLog(
            user_id=user.id,
            username=user.username,
            role=user.role,
            action="ARCHIVE" if result.action == "archive" else "RESTORE",
            resource=f"warehouse/molds/{result.mold.id}/{result.action}",
            entity_type="mold_tool",
            entity_id=result.mold.id,
            description="模具封存待复用" if result.action == "archive" else "模具恢复启用",
            details=json.dumps(
                {
                    "movement_id": movement.id,
                    "mold_code": movement.mold_code_snapshot,
                    "from_location": movement.from_location,
                    "to_location": movement.to_location,
                    "expected_version": movement.expected_version,
                    "resulting_version": movement.resulting_version,
                    "idempotency_key": movement.idempotency_key,
                    "reason": movement.note,
                },
                ensure_ascii=False,
            ),
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )
    )


@router.post("/molds/{mold_id}/archive")
def archive_mold(
    mold_id: int,
    payload: MoldArchiveConfirmPayload,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(can_archive),
) -> dict:
    _require_mold_archive_operator(user)
    try:
        _assert_asset_location_operational(
            db,
            asset_kind="mold",
            location_text="3F-M-ARCHIVE-AB2-N",
            claim_floor=True,
        )
        result = archive_mold_tool(
            db,
            mold_id=mold_id,
            expected_version=payload.expected_version,
            idempotency_key=payload.idempotency_key,
            actor_id=user.id,
            reason=payload.reason,
        )
        _append_mold_archive_log(db, request=request, user=user, result=result)
        db.commit()
        return _mold_archive_response(result)
    except MoldLocationError as error:
        db.rollback()
        raise HTTPException(status_code=error.status_code, detail=str(error)) from error
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(status_code=409, detail="模具封存事实已变化，请刷新后重试") from error


@router.post("/molds/{mold_id}/restore")
def restore_mold(
    mold_id: int,
    payload: MoldRestoreConfirmPayload,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(can_archive),
) -> dict:
    _require_mold_archive_operator(user)
    try:
        _assert_asset_location_operational(
            db,
            asset_kind="mold",
            location_text=payload.target_location,
            claim_floor=True,
        )
        result = restore_mold_tool(
            db,
            mold_id=mold_id,
            target_location=payload.target_location,
            expected_version=payload.expected_version,
            idempotency_key=payload.idempotency_key,
            actor_id=user.id,
        )
        _append_mold_archive_log(db, request=request, user=user, result=result)
        db.commit()
        return _mold_archive_response(result)
    except MoldLocationError as error:
        db.rollback()
        raise HTTPException(status_code=error.status_code, detail=str(error)) from error
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(status_code=409, detail="模具恢复事实已变化，请刷新后重试") from error


@router.get("/molds/location-options")
def get_mold_location_options(
    db: Session = Depends(get_db),
    _user: User = Depends(can_read),
) -> dict:
    floor_layout = overlay_formal_area_bindings(
        db,
        floor_code="1F",
        floor_layout=load_warehouse_twin_floor("1F"),
        include_draft=False,
    )
    return {
        "floor_code": "1F",
        "position_order": None,
        "position_numbers_are_dynamic": False,
        "storage_rule": "rack_level_grid",
        "racks": one_floor_mold_location_options(floor_layout=floor_layout),
    }


@router.post("/molds/location-movement/preview")
def preview_mold_location_movement(
    payload: MoldLocationPreviewPayload,
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    try:
        _assert_asset_location_operational(
            db,
            asset_kind="mold",
            location_text=payload.target_location,
        )
        allowed_customer_ids = _mold_customer_scope(user, db)
        preview = preview_mold_location_move(
            db,
            mold_code=payload.mold_code,
            target_location=payload.target_location,
        )
        _require_mold_customer_scope(preview.mold, allowed_customer_ids)
        return _mold_location_preview_dict(preview, allowed_customer_ids)
    except MoldLocationError as error:
        raise HTTPException(status_code=error.status_code, detail=str(error)) from error


@router.post("/molds/location-movement/confirm")
def confirm_mold_location_movement(
    payload: MoldLocationConfirmPayload,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    try:
        _assert_asset_location_operational(
            db,
            asset_kind="mold",
            location_text=payload.target_location,
            claim_floor=True,
        )
        allowed_customer_ids = _mold_customer_scope(user, db)
        scoped_preview = preview_mold_location_move(
            db,
            mold_code=payload.mold_code,
            target_location=payload.target_location,
        )
        _require_mold_customer_scope(scoped_preview.mold, allowed_customer_ids)
        result = confirm_mold_location_move(
            db,
            mold_code=payload.mold_code,
            target_location=payload.target_location,
            expected_version=payload.expected_version,
            idempotency_key=payload.idempotency_key,
            actor_id=user.id,
            source=payload.source,
            note=payload.note,
        )
        _append_mold_location_move_log(
            db,
            request=request,
            user=user,
            result=result,
            description="双码确认模具位置移动",
        )
        db.commit()
        return _mold_location_move_response(result, allowed_customer_ids)
    except MoldLocationError as error:
        db.rollback()
        raise HTTPException(status_code=error.status_code, detail=str(error)) from error
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="模具位置已变化或幂等键冲突，请重新预览",
        ) from error


def _mold_live_url(mold_id: int) -> str:
    """Build the permanent label URL from the configured ERP browser origin."""

    return mold_mobile_url(mold_id, origin=load_settings().browser_url)


def _label_customer(row: MoldTool, products: list[Product]) -> tuple[str, str | None]:
    if row.identity_status == "frozen":
        primary_links = [
            link
            for link in _visible_mold_customer_links(row, None)
            if link.display_order is not None
        ]
        short_names = [
            str(link.customer.chinese_short_name or "").strip()
            for link in primary_links
        ]
        if primary_links and all(short_names):
            return "/".join(short_names), None
        return "待完善", None
    customers = {
        (product.customer.name, product.customer.customer_code)
        for product in products
        if product.customer is not None
    }
    if len(customers) != 1:
        return ("多客户" if customers else "待完善", None)
    customer_name, customer_code = next(iter(customers))
    return (
        mold_customer_short_name(row.mold_name, customer_name, customer_code)
        or "待完善",
        customer_code,
    )


def _label_mold_number(row: MoldTool, products: list[Product]) -> str:
    if row.identity_status == "frozen" and row.label_name:
        return " ".join(
            value
            for value in (row.label_name, row.chinese_short_name)
            if value
        )
    customers = {
        (product.customer.name, product.customer.customer_code)
        for product in products
        if product.customer is not None
    }
    if len(customers) != 1:
        return row.mold_code or "待完善"
    customer_name, customer_code = next(iter(customers))
    return mold_label_display_number(
        row.mold_name,
        row.mold_code,
        customer_name,
        customer_code,
    )


def _label_mold_name(row: MoldTool, products: list[Product]) -> str:
    """Return the physical handwritten mold label without merging its short name."""

    if row.identity_status == "frozen" and row.label_name:
        return str(row.label_name).strip()
    return ""


def _label_mold_chinese_short_name(row: MoldTool) -> str:
    return str(row.chinese_short_name or "").strip()


def _label_identity(row: MoldTool, products: list[Product]) -> str:
    if row.identity_status == "frozen":
        return _mold_display_name(row, None)
    customer_name, _customer_code = _label_customer(row, products)
    return f"{customer_name}{_label_mold_number(row, products)}"


def _label_display_report_specification(products: list[Product]) -> str:
    values = _label_dimension_rows(products, "report_specification")
    complete = _ordered_label_values([value for value in values if value])
    missing = any(not value for value in values)
    if not complete:
        return "待完善"
    if missing:
        return " / ".join([*complete, "部分尺寸未填"])
    return " / ".join(complete)


def _label_display_cutting_mode(products: list[Product]) -> str:
    # The legacy normalizer fills blank input with "一开一" for old labels.
    # V8 editor defaults must instead surface missing source facts.
    values = [
        normalize_cutting_mode(product.default_cutting_mode)
        if str(product.default_cutting_mode or "").strip()
        else ""
        for product in products
    ]
    complete = _ordered_label_values([value for value in values if value])
    missing = any(not value for value in values)
    if not complete:
        return "待完善"
    if missing:
        return " / ".join([*complete, "部分开料未填"])
    return " / ".join(complete)


def _mold_label_auto_fields(row: MoldTool, products: list[Product]) -> dict[str, str]:
    product_names = _ordered_label_values(
        [str(product.product_name or "").strip() for product in products]
    )
    return {
        "display_identity": _label_identity(row, products),
        "product_name": " / ".join(product_names) if product_names else "待完善",
        "report_specification": _label_display_report_specification(products),
        "cutting_mode": _label_display_cutting_mode(products),
        "remarks": "",
    }


def _mold_label_content_projection(row: MoldTool, products: list[Product]) -> dict:
    overrides = parse_label_overrides(row.label_overrides_json)
    auto_fields = _mold_label_auto_fields(row, products)
    effective = apply_label_overrides(auto_fields, overrides)
    return {
        "label_overrides": overrides,
        "label_auto_fields": auto_fields,
        "label_display_identity": effective["display_identity"],
        "label_display_product_name": effective["product_name"],
        "label_display_report_specification": effective["report_specification"],
        "label_display_cutting_mode": effective["cutting_mode"],
        "label_display_remarks": effective["remarks"],
    }


def _ordered_label_values(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        normalized = str(value or "").strip()
        if normalized and normalized not in result:
            result.append(normalized)
    return result


def _label_customer_rows(row: MoldTool, products: list[Product]) -> list[str]:
    values: list[str] = []
    for product in products:
        customer = product.customer
        if customer is None:
            values.append("")
            continue
        short_name = str(customer.chinese_short_name or "").strip()
        if not short_name:
            short_name = (
                mold_customer_short_name(
                    row.mold_name,
                    customer.name,
                    customer.customer_code,
                )
                or ""
            )
        values.append(short_name)
    return values


def _label_customer_values(row: MoldTool, products: list[Product]) -> list[str]:
    """Return every bound customer's verified Chinese short name in product order."""

    return _ordered_label_values(_label_customer_rows(row, products))


def _label_dimension_rows(products: list[Product], field: str) -> list[str]:
    if field == "specification":
        dimensions = []
        for product in products:
            required = (product.length_mm, product.width_mm)
            if product.height_mm is not None:
                required = (*required, product.height_mm)
            dimensions.append(required)
    else:
        dimensions = [
            (product.report_length_mm, product.report_width_mm)
            for product in products
        ]

    def format_value(value) -> str:
        number = Decimal(str(value))
        if number == number.to_integral_value():
            return str(int(number))
        return format(number.normalize(), "f")

    return [
        " × ".join(format_value(value) for value in row)
        if row and all(value is not None and value > 0 for value in row)
        else ""
        for row in dimensions
    ]


def _label_dimension_values(products: list[Product], field: str) -> list[str]:
    return _ordered_label_values(_label_dimension_rows(products, field))


def _label_dimension(products: list[Product], field: str) -> str:
    values = _label_dimension_rows(products, field)
    unique_values = set(values)
    if len(unique_values) == 1 and values and values[0]:
        return values[0]
    if len(unique_values) > 1:
        return "多款见扫码"
    return ""


def _label_representative_dimension(
    products: list[Product],
    field: Literal["specification", "report_specification"],
) -> tuple[str, Product | None]:
    """Choose one stable dimension and its representative product.

    The most common dimension wins.  When counts tie (including exactly two
    different bound products), prefer the longer edge, then larger footprint,
    and finally the lowest stable product code.  All products and dimensions
    remain available through the QR/detail payload.
    """

    rows = _label_dimension_rows(products, field)
    grouped: dict[str, dict[str, object]] = {}
    for product, value in zip(products, rows, strict=True):
        if not value:
            continue
        if field == "specification":
            dimensions = (
                Decimal(str(product.length_mm or 0)),
                Decimal(str(product.width_mm or 0)),
                Decimal(str(product.height_mm or 0)),
            )
        else:
            dimensions = (
                Decimal(str(product.report_length_mm or 0)),
                Decimal(str(product.report_width_mm or 0)),
            )
        length, width = dimensions[:2]
        entry = grouped.setdefault(
            value,
            {
                "count": 0,
                "long_edge": Decimal("0"),
                "area": Decimal("0"),
                "product_code": str(product.product_code or "").strip().casefold(),
                "products": [],
            },
        )
        entry["count"] = int(entry["count"]) + 1
        entry["long_edge"] = max(
            Decimal(str(entry["long_edge"])), *dimensions
        )
        entry["area"] = max(Decimal(str(entry["area"])), length * width)
        entry["products"].append(product)
        product_code = str(product.product_code or "").strip().casefold()
        if product_code and (
            not entry["product_code"] or product_code < entry["product_code"]
        ):
            entry["product_code"] = product_code
    if not grouped:
        return "", None
    ranked = sorted(
        grouped.items(),
        key=lambda item: (
            -int(item[1]["count"]),
            -Decimal(str(item[1]["long_edge"])),
            -Decimal(str(item[1]["area"])),
            str(item[1]["product_code"]),
            item[0],
        ),
    )
    selected_value, selected_entry = ranked[0]
    selected_products = sorted(
        selected_entry["products"],
        key=lambda product: (
            str(product.product_code or "").strip().casefold(),
            int(product.id or 0),
        ),
    )
    return selected_value, (selected_products[0] if selected_products else None)


def _label_representative_report_specification(products: list[Product]) -> str:
    return _label_representative_dimension(products, "report_specification")[0]


def _label_representative_product_specification(products: list[Product]) -> str:
    _, representative = _label_representative_dimension(
        products,
        "report_specification",
    )
    if representative is None:
        return ""
    return _label_dimension_rows([representative], "specification")[0]


def _label_compact_inventory_code(products: list[Product]) -> str:
    _, representative = _label_representative_dimension(
        products,
        "report_specification",
    )
    if representative is None:
        return ""
    product_code = str(representative.product_code or "").strip()
    if len(products) <= 1:
        return product_code
    return f"{product_code} 等{len(products)}款"


def _label_flute_rows(products: list[Product]) -> list[str]:
    return [
        str(
            product.flute_type
            or (product.material.flute_type if product.material is not None else "")
            or ""
        )
        .strip()
        .upper()
        for product in products
    ]


def _label_flute_values(products: list[Product]) -> list[str]:
    return _ordered_label_values(_label_flute_rows(products))


def _label_flute_type(products: list[Product]) -> str:
    values = _label_flute_rows(products)
    if not values or not any(values):
        return ""
    if any(not value for value in values) or len(set(values)) != 1:
        return "多款见扫码"
    return values[0]


def _label_cutting_rows(products: list[Product]) -> list[str]:
    return [
        normalize_cutting_mode(product.default_cutting_mode)
        for product in products
    ]


def _label_cutting_values(products: list[Product]) -> list[str]:
    return _ordered_label_values(_label_cutting_rows(products))


def _label_cutting_mode(products: list[Product]) -> str:
    values = _label_cutting_rows(products)
    if not values:
        return ""
    if len(set(values)) != 1:
        return "多款见扫码"
    return values[0]


def _label_shared_value(values: list[str], product_count: int) -> str:
    """Project a shared-mold fact without claiming one product represents all."""

    normalized = [str(value or "").strip() for value in values]
    if product_count <= 1:
        return normalized[0] if normalized and normalized[0] else "待完善"
    if not normalized or any(not value for value in normalized):
        return f"待完善（共用{product_count}款）"
    if len(set(normalized)) == 1:
        return normalized[0]
    return f"共用{product_count}款"


def _label_compact_product_name(products: list[Product]) -> str:
    """Use a stable representative name while retaining a shared-mold count."""

    names = [str(product.product_name or "").strip() for product in products]
    if len(products) <= 1:
        return names[0] if names else ""
    if not names or any(not name for name in names):
        return f"待完善（共用{len(products)}款）"
    _dimension, representative = _label_representative_dimension(
        products,
        "report_specification",
    )
    if representative is None:
        representative = min(
            products,
            key=lambda product: (
                str(product.product_code or "").strip().casefold(),
                int(product.id or 0),
            ),
        )
    name = str(representative.product_name or "").strip()
    return f"{name} 等{len(products)}款" if name else f"待完善（共用{len(products)}款）"


def _label_v7_join_product_facts(products: list[Product], field: str) -> str:
    """Render every V7 binding, never a representative product or count."""

    values = [str(getattr(product, field, "") or "").strip() for product in products]
    if not values or any(not value for value in values):
        return "待完善"
    return " / ".join(values)


def _label_v7_consistent_value(values: list[str]) -> str:
    """Fail closed when supposedly shared production facts disagree."""

    normalized = [str(value or "").strip() for value in values]
    if not normalized or any(not value for value in normalized):
        return "待完善"
    return normalized[0] if len(set(normalized)) == 1 else "待完善"


def _label_rack_location(value: str | None, *, maximum_characters: int = 16) -> str:
    """Fit the V7 position line while making every omitted suffix visible."""

    location = str(value or "").strip()
    if len(location) <= maximum_characters:
        return location
    return f"{location[: maximum_characters - 1]}…"


_LABEL_PRINTABLE_IDENTITY_LIMIT = 40


def _mold_label_printability_error(
    row: MoldTool,
    products: list[Product],
    template_version: str = MOLD_LABEL_TEMPLATE_40X30,
) -> str | None:
    """Return a human-fixable reason instead of printing clipped facts."""

    display_name = _mold_display_name(row, None)
    if not row.is_active or row.archive_status != "active":
        return f"模具 {display_name} 已停用或归档，不能打印使用标签"
    if not products:
        return f"模具 {display_name} 尚未绑定有效常用箱，不能打印使用标签"
    identity = _label_identity(row, products)
    identity_limit = (
        _LABEL_PRINTABLE_IDENTITY_LIMIT
        if row.identity_status == "frozen"
        else 24
    )
    if (
        template_version == MOLD_LABEL_TEMPLATE_40X30
        and len(identity) > identity_limit
    ):
        return (
            f"模具 {display_name} 的标签内容过长，"
            "请核对客户简称、标签名称和中文简写"
        )
    # The 40×80 paper now uses the same six business facts as the approved
    # single-label page.  Paper choice must not introduce additional master-
    # data gates (handwritten name, cutting mode, inventory code, etc.) that
    # make batch printing fail even though the same mold can print singly.
    # Text fitting remains fail-closed in the millimetre layout renderer.
    return None


def _mold_label_dict(
    row: MoldTool,
    allowed_customer_ids: set[int] | None,
    template_version: str = MOLD_LABEL_TEMPLATE_40X30,
    *,
    preview_only: bool = False,
) -> dict:
    products = _visible_mold_products(row, allowed_customer_ids)
    if not products and row.identity_status == "frozen":
        # Discontinuing a SKU does not remove its physical mold. Keep the
        # production/selection filters active-only; only asset labels may use
        # an existing, non-deleted binding backed by formal customer identity.
        associated_customer_ids = {link.customer_id for link in row.customer_links}
        products = [
            product
            for product in _historical_visible_mold_products(row, allowed_customer_ids)
            if product.deleted_at is None
            and product.customer_id in associated_customer_ids
        ]
    lookup_url = _mold_live_url(row.id)
    qr = qrcode.QRCode(
        version=2,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=3,
        border=4,
    )
    qr.add_data(lookup_url)
    qr.make(fit=True)
    # The 13.9 mm square provides 111 printer dots at 203 dpi. Including
    # four quiet modules per edge, versions 2/3 require 99/111 dots at 3/module.
    # Keep the physical readability bound, not the old LAN URL's exact version.
    if len(qr.get_matrix()) * 3 > int(13.9 * 203 / 25.4):
        raise HTTPException(
            status_code=409,
            detail=(
                "固定二维码地址超出 40×30 标签的 203dpi 清晰度契约，"
                "请先核对 ERP_BROWSER_URL 配置"
            ),
        )
    image = qr.make_image(fill_color="black", back_color="white")
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    basics = _mold_tool_dict(row, allowed_customer_ids)
    printability_error = _mold_label_printability_error(
        row,
        products,
        template_version,
    )
    if printability_error and not preview_only:
        raise HTTPException(status_code=409, detail=printability_error)
    result = {
        "mold_code": row.mold_code,
        "rack_location": row.rack_location,
        "location_guide": basics["location_guide"],
        "is_active": row.is_active,
        "printable": printability_error is None,
        "printability_error": printability_error,
        "product_count": len(products),
        "label_identity": _label_identity(row, products),
        "label_customer_name": _label_customer(row, products)[0],
        "label_mold_number": _label_mold_number(row, products),
        "label_mold_name": _label_mold_name(row, products),
        "label_mold_chinese_short_name": _label_mold_chinese_short_name(row),
        "label_product_specification": _label_dimension(
            products, "specification"
        ),
        "label_report_specification": _label_dimension(
            products, "report_specification"
        ),
        "label_flute_type": _label_flute_type(products),
        "lookup_url": lookup_url,
        "qr_data_url": (
            "data:image/png;base64,"
            + base64.b64encode(buffer.getvalue()).decode("ascii")
        ),
    }
    if template_version == MOLD_LABEL_TEMPLATE_80X40:
        shared_mold = len(products) > 1
        customer_names = _label_customer_values(row, products)
        product_specifications = _label_dimension_values(
            products,
            "specification",
        )
        report_specifications = _label_dimension_values(
            products,
            "report_specification",
        )
        flute_types = _label_flute_values(products)
        cutting_modes = _label_cutting_values(products)
        label_products = [
            {
                "product_code": str(product.product_code or "").strip(),
                "product_name": str(product.product_name or "").strip(),
            }
            for product in products
        ]
        product_count = len(products)
        result.update(
            {
                "template_version": template_version,
                "template_label": mold_label_template_label(template_version),
                "label_projection_mode": (
                    "shared_mold" if shared_mold else "single_product"
                ),
                "label_customer_names": customer_names,
                "label_product_specifications": product_specifications,
                "label_inventory_codes": [
                    product["product_code"] for product in label_products
                ],
                "label_report_specifications": report_specifications,
                "label_flute_types": flute_types,
                "label_cutting_modes": cutting_modes,
                "label_products": label_products,
                "label_cutting_mode": _label_v7_consistent_value(
                    _label_cutting_rows(products)
                ),
                "label_inventory_code": _label_v7_join_product_facts(
                    products, "product_code"
                ),
                "label_product_name": _label_v7_join_product_facts(
                    products, "product_name"
                ),
                "label_report_specification": _label_v7_consistent_value(
                    _label_dimension_rows(products, "report_specification")
                ),
                "label_product_specification": _label_v7_consistent_value(
                    _label_dimension_rows(products, "specification")
                ),
                "label_flute_type": _label_v7_consistent_value(
                    _label_flute_rows(products)
                ),
                "label_rack_location": _label_rack_location(row.rack_location),
            }
        )
        result.update(_mold_label_content_projection(row, products))
    return result


_MOLD_TASK_STATUS_LABELS = {
    "waiting_material": "待收料",
    "pending": "待生产",
    "completed": "已完成",
    "not_required": "无需生产",
}


def _current_mold_binding_starts(
    db: Session,
    products: list[Product],
    mold_id: int,
) -> dict[int, datetime]:
    """Return the proven start of each product's current mold binding period."""

    product_ids = [product.id for product in products]
    if not product_ids:
        return {}
    versions = db.execute(
        select(
            MasterDataObjectVersion.object_id,
            MasterDataObjectVersion.created_at,
            MasterDataObjectVersion.snapshot_json,
        )
        .where(
            MasterDataObjectVersion.object_type == "product",
            MasterDataObjectVersion.object_id.in_(product_ids),
        )
        .order_by(
            MasterDataObjectVersion.object_id,
            MasterDataObjectVersion.version,
        )
    ).all()
    by_product: dict[int, list[tuple[datetime, dict]]] = {}
    for product_id, created_at, snapshot_json in versions:
        try:
            snapshot = json.loads(snapshot_json)
        except (TypeError, ValueError):
            continue
        if isinstance(snapshot, dict):
            by_product.setdefault(int(product_id), []).append(
                (created_at, snapshot)
            )

    result: dict[int, datetime] = {}
    for product in products:
        start: datetime | None = None
        linked_before = False
        for created_at, snapshot in by_product.get(product.id, []):
            linked = snapshot.get("mold_tool_id") == mold_id
            if linked and not linked_before:
                start = created_at
            elif not linked:
                start = None
            linked_before = linked
        if linked_before and start is not None:
            result[product.id] = start
    return result


def _mold_live_task_ids(
    db: Session,
    *,
    mold: MoldTool,
    products: list[Product],
    allowed_customer_ids: set[int] | None,
) -> tuple[list[int], list[str]]:
    warnings: list[str] = []
    task_ids: set[int] = set()
    starts = _current_mold_binding_starts(db, products, mold.id)
    missing_evidence = sorted(
        product.product_code
        for product in products
        if product.id not in starts
    )
    if missing_evidence:
        warnings.append(
            "部分常用箱缺少可证明的当前模具绑定起点，未倒推其历史订单："
            + "、".join(missing_evidence)
        )
    for product in products:
        binding_start = starts.get(product.id)
        if binding_start is None:
            continue
        query = (
            select(ProductionTask.id)
            .join(OrderItem, OrderItem.id == ProductionTask.order_item_id)
            .join(Order, Order.id == OrderItem.order_id)
            .where(
                ProductionTask.sales_order_item_bom_component_id.is_(None),
                OrderItem.product_id == product.id,
                ProductionTask.created_at >= binding_start,
                *order_item_forward_fulfillment_sql_conditions(
                    order_status_column=Order.status,
                    ordered_quantity_column=OrderItem.quantity,
                    delivered_quantity_column=OrderItem.delivered_quantity,
                    is_force_closed_column=OrderItem.is_force_closed,
                ),
            )
        )
        if allowed_customer_ids is not None:
            query = query.where(Order.customer_id.in_(allowed_customer_ids))
        task_ids.update(int(value) for value in db.scalars(query).all())

    component_query = (
        select(ProductionTask.id)
        .join(
            SalesOrderItemBomComponent,
            SalesOrderItemBomComponent.id
            == ProductionTask.sales_order_item_bom_component_id,
        )
        .join(OrderItem, OrderItem.id == ProductionTask.order_item_id)
        .join(Order, Order.id == OrderItem.order_id)
        .where(
            SalesOrderItemBomComponent.snapshot_mold_tool_id == mold.id,
            *order_item_forward_fulfillment_sql_conditions(
                order_status_column=Order.status,
                ordered_quantity_column=OrderItem.quantity,
                delivered_quantity_column=OrderItem.delivered_quantity,
                is_force_closed_column=OrderItem.is_force_closed,
            ),
        )
    )
    if allowed_customer_ids is not None:
        component_query = component_query.where(
            Order.customer_id.in_(allowed_customer_ids)
        )
    task_ids.update(int(value) for value in db.scalars(component_query).all())
    return sorted(task_ids), warnings


def _material_locations_for_task(
    db: Session,
    *,
    task: ProductionTask,
    item: OrderItem,
) -> list[dict]:
    query = (
        select(InventoryReservation, InventoryLot, WarehouseLocation)
        .join(InventoryLot, InventoryLot.id == InventoryReservation.inventory_lot_id)
        .join(
            WarehouseLocation,
            WarehouseLocation.id == InventoryLot.warehouse_location_id,
        )
        .where(
            InventoryReservation.order_item_id == item.id,
            InventoryReservation.reservation_type == "semi_order",
            InventoryReservation.status.notin_(("cancelled", "released", "consumed")),
            InventoryReservation.reserved_stock_quantity
            > InventoryReservation.consumed_stock_quantity
            + InventoryReservation.released_stock_quantity,
            InventoryReservation.credited_requirement_quantity.is_not(None),
            InventoryReservation.credited_requirement_quantity
            > InventoryReservation.consumed_requirement_quantity
            + InventoryReservation.released_requirement_quantity,
            InventoryLot.inventory_type == "semi_finished",
            InventoryLot.status.in_(("active", "frozen")),
        )
    )
    if task.sales_order_item_bom_component_id is None:
        query = query.where(
            InventoryReservation.sales_order_item_bom_component_id.is_(None)
        )
    else:
        query = query.where(
            InventoryReservation.sales_order_item_bom_component_id
            == task.sales_order_item_bom_component_id
        )
    reservation_rows = db.execute(
        query.order_by(InventoryLot.stock_date, InventoryLot.id)
    ).all()
    projection_contexts = load_warehouse_location_projection_contexts(
        db,
        [location for _reservation, _lot, location in reservation_rows],
    )
    locations: list[dict] = []
    for reservation, lot, location in reservation_rows:
        remaining = max(
            int(reservation.reserved_stock_quantity or 0)
            - int(reservation.consumed_stock_quantity or 0)
            - int(reservation.released_stock_quantity or 0),
            0,
        )
        if remaining <= 0:
            continue
        remaining_requirement = max(
            int(reservation.credited_requirement_quantity or 0)
            - int(reservation.consumed_requirement_quantity or 0)
            - int(reservation.released_requirement_quantity or 0),
            0,
        )
        if remaining_requirement <= 0:
            continue
        locations.append(
            {
                "location_code": location.location_code,
                "location_name": employee_location_name(
                    location,
                    area=projection_contexts.get(int(location.id), {}).get("area"),
                    floor=projection_contexts.get(int(location.id), {}).get("floor"),
                    area_sequence=projection_contexts.get(
                        int(location.id), {}
                    ).get("area_sequence"),
                ),
                "availability_state": (
                    "available" if lot.status == "active" else "frozen"
                ),
            }
        )
    return list(
        {
            (
                row["location_code"],
                row["location_name"],
                row["availability_state"],
            ): row
            for row in locations
        }.values()
    )


_INACTIVE_REQUISITION_SOURCE_STATUSES = frozenset(
    {
        "cancelled",
        "canceled",
        "voided",
        "withdrawn",
        "invalid",
        "已取消",
        "已作废",
        "已撤回",
    }
)


def _current_requisition_source_ids(
    db: Session,
    *,
    item: OrderItem,
    requisitions: list[RequisitionItem],
) -> set[int]:
    """Return current receivable source rows while retaining facts separately."""

    if not requisitions:
        return set()
    confirmed_supplier = False
    latest_supplier_requisition_id: int | None = None
    if item.supplier_order_number:
        confirmed_supplier = db.scalar(
            select(SupplierRequisitionOrderItem.id)
            .join(
                SupplierRequisitionOrder,
                SupplierRequisitionOrder.id
                == SupplierRequisitionOrderItem.supplier_order_id,
            )
            .where(
                SupplierRequisitionOrder.order_number
                == item.supplier_order_number,
                SupplierRequisitionOrder.status == "confirmed",
                SupplierRequisitionOrderItem.order_item_id == item.id,
                SupplierRequisitionOrderItem.status == "active",
            )
            .limit(1)
        ) is not None
    if confirmed_supplier:
        latest_supplier_requisition_id = db.scalar(
            select(func.max(RequisitionItem.requisition_id))
            .join(Requisition, Requisition.id == RequisitionItem.requisition_id)
            .where(
                RequisitionItem.order_item_id == item.id,
                Requisition.status == "supplier_requisition_created",
            )
        )
    return {
        requisition.id
        for requisition in requisitions
        if (
            requisition.status == "有效"
            if not confirmed_supplier
            else requisition.status == "supplier_requisition_created"
            and requisition.requisition_id == latest_supplier_requisition_id
        )
    }


def _material_sources_for_task(
    db: Session,
    *,
    task: ProductionTask,
    item: OrderItem,
) -> tuple[list[dict], str | None]:
    """Build receipt facts per physical cardboard source without cross-counting."""

    receipt_rows = db.scalars(
        select(IncomingReceiptItem)
        .join(IncomingReceipt, IncomingReceipt.id == IncomingReceiptItem.receipt_id)
        .where(
            IncomingReceiptItem.order_item_id == item.id,
            IncomingReceiptItem.status == "posted",
            IncomingReceipt.status == "posted",
        )
        .order_by(IncomingReceiptItem.id)
    ).all()
    receipts_by_requisition: dict[int | None, list[IncomingReceiptItem]] = {}
    for receipt in receipt_rows:
        receipts_by_requisition.setdefault(receipt.requisition_item_id, []).append(
            receipt
        )

    warnings: list[str] = []
    requisition_entries: list[
        tuple[RequisitionItem, RequisitionItemBomSource | None]
    ]
    if task.sales_order_item_bom_component_id is not None:
        requisition_entries = db.execute(
            select(RequisitionItem, RequisitionItemBomSource)
            .join(
                RequisitionItemBomSource,
                RequisitionItemBomSource.requisition_item_id
                == RequisitionItem.id,
            )
            .where(
                RequisitionItem.order_item_id == item.id,
                RequisitionItemBomSource.sales_order_item_bom_component_id
                == task.sales_order_item_bom_component_id
            )
            .order_by(RequisitionItem.id)
        ).all()
    else:
        linked_bom_source = (
            select(RequisitionItemBomSource.id)
            .where(
                RequisitionItemBomSource.requisition_item_id
                == RequisitionItem.id
            )
            .exists()
        )
        requisition_rows = db.scalars(
            select(RequisitionItem)
            .where(
                RequisitionItem.order_item_id == item.id,
                ~linked_bom_source,
            )
            .order_by(RequisitionItem.id)
        ).all()
        requisition_entries = [(row, None) for row in requisition_rows]

    current_source_ids = _current_requisition_source_ids(
        db,
        item=item,
        requisitions=[row for row, _source in requisition_entries],
    )
    sources: list[dict] = []
    for requisition, bom_source in requisition_entries:
        source_receipts = receipts_by_requisition.get(requisition.id, [])
        inactive = (
            str(requisition.status or "").strip().lower()
            in _INACTIVE_REQUISITION_SOURCE_STATUSES
        )
        has_preserved_fact = bool(source_receipts) or requisition.status == "已入库"
        is_current = requisition.id in current_source_ids and (
            bom_source is None or bom_source.active_guard == 1
        )
        if inactive and not has_preserved_fact:
            continue
        if not is_current and not has_preserved_fact:
            continue
        legacy_received = not source_receipts and requisition.status == "已入库"
        legacy_received_quantity = (
            int(requisition.requisition_qty or 0) or None
            if legacy_received
            else None
        )
        sources.append(
            {
                "requisition": requisition,
                "receipts": source_receipts,
                "planned_fallback": (
                    None
                    if legacy_received
                    else int(requisition.requisition_qty or 0) or None
                ),
                "legacy_received": legacy_received,
                "legacy_received_quantity": legacy_received_quantity,
                "legacy_unattributed": False,
                "is_current": is_current,
                "has_preserved_fact": has_preserved_fact,
                "component_type": (
                    str(bom_source.component_type or "whole").strip().lower()
                    if bom_source is not None
                    else None
                ),
                "source_conflict": False,
            }
        )

    if task.sales_order_item_bom_component_id is not None and sources:
        # One BOM snapshot has one physical source per whole/cover/base kind.  A
        # superseded source with no receipt fact must not duplicate the current
        # active_guard row.  A historical fact may replace an empty current row;
        # multiple facts for the same physical kind are ambiguous and fail closed.
        normalized: list[dict] = []
        ambiguous = False
        by_component: dict[str, list[dict]] = {}
        for source in sources:
            by_component.setdefault(source["component_type"] or "whole", []).append(
                source
            )
        for component_sources in by_component.values():
            factual = [
                source
                for source in component_sources
                if source["has_preserved_fact"]
            ]
            current = [source for source in component_sources if source["is_current"]]
            if len(factual) == 1:
                normalized.append(factual[0])
            elif len(factual) > 1:
                normalized.extend(factual)
                ambiguous = True
            elif len(current) == 1:
                normalized.append(current[0])
            elif len(current) > 1:
                normalized.extend(current)
                ambiguous = True

        whole_sources = [
            source for source in normalized if source["component_type"] == "whole"
        ]
        split_sources = [
            source
            for source in normalized
            if source["component_type"] in {"cover", "base"}
        ]
        if whole_sources and split_sources:
            whole_has_fact = any(
                source["has_preserved_fact"] for source in whole_sources
            )
            split_has_fact = any(
                source["has_preserved_fact"] for source in split_sources
            )
            if whole_has_fact and not split_has_fact:
                normalized = whole_sources
            elif split_has_fact and not whole_has_fact:
                normalized = split_sources
            else:
                ambiguous = True
        if ambiguous:
            for source in normalized:
                source["source_conflict"] = True
            warnings.append(
                "该模切组件同时存在无法安全合并的整片与盖片/底片，或同一物理料来源存在多条收料事实；"
                "本页不计算精确收料数量，请人工核对。"
            )
        sources = normalized

    direct_receipts = receipts_by_requisition.get(None, [])
    if task.sales_order_item_bom_component_id is None and direct_receipts:
        if sources:
            warnings.append(
                "该订单同时存在旧直收与具体报料来源，收料数量存在来源冲突；"
                "本页不计算精确收料数量，请人工核对。"
            )
            for source in sources:
                source["source_conflict"] = True
        sources.append(
            {
                "requisition": None,
                "receipts": direct_receipts,
                "planned_fallback": int(item.requisition_qty or 0) or None,
                "legacy_received": False,
                "legacy_received_quantity": None,
                "legacy_unattributed": False,
                "is_current": False,
                "has_preserved_fact": True,
                "component_type": None,
                "source_conflict": bool(sources),
            }
        )
    elif task.sales_order_item_bom_component_id is None and not sources:
        legacy_received = item.material_status == "received"
        sources.append(
            {
                "requisition": None,
                "receipts": [],
                "planned_fallback": (
                    None
                    if legacy_received
                    else int(item.requisition_qty or 0) or None
                ),
                "legacy_received": legacy_received,
                "legacy_received_quantity": (
                    int(item.requisition_qty or 0) or None
                    if legacy_received
                    else None
                ),
                "legacy_unattributed": False,
                "is_current": not legacy_received,
                "has_preserved_fact": legacy_received,
                "component_type": None,
                "source_conflict": False,
            }
        )
    elif (
        task.sales_order_item_bom_component_id is not None
        and not sources
        and item.material_status == "received"
    ):
        sources.append(
            {
                "requisition": None,
                "receipts": [],
                "planned_fallback": None,
                "legacy_received": True,
                "legacy_received_quantity": None,
                "legacy_unattributed": True,
                "is_current": False,
                "has_preserved_fact": True,
                "component_type": None,
                "source_conflict": False,
            }
        )
        warnings.append("历史组合订单只有父单收料状态，无法归属到具体模切组件。")
    return sources, "；".join(warnings) or None


def _material_facts_for_task(
    db: Session,
    *,
    task: ProductionTask,
    item: OrderItem,
) -> dict:
    locations = _material_locations_for_task(db, task=task, item=item)
    sources, warning = _material_sources_for_task(db, task=task, item=item)
    calculated: list[dict] = []
    for source in sources:
        receipts: list[IncomingReceiptItem] = source["receipts"]
        latest = receipts[-1] if receipts else None
        legacy_received = bool(source["legacy_received"])
        planned = None if legacy_received else (
            int(latest.planned_quantity or 0)
            if latest is not None
            else source["planned_fallback"]
        )
        received = (
            source["legacy_received_quantity"]
            if legacy_received
            else sum(int(receipt.received_quantity or 0) for receipt in receipts)
        )
        accepted_short = bool(
            latest is not None and latest.resolution_action == "accept_short"
        )
        closed = legacy_received or accepted_short or bool(
            planned is not None and received is not None and received >= planned
        )
        calculated.append(
            {
                "planned": planned,
                "received": received,
                "has_modern_receipt": bool(receipts),
                "accepted_short": accepted_short,
                "legacy_received": legacy_received,
                "legacy_unattributed": bool(source["legacy_unattributed"]),
                "source_conflict": bool(source["source_conflict"]),
                "closed": closed,
            }
        )

    source_conflict = any(source["source_conflict"] for source in calculated)
    legacy_unattributed = any(
        source["legacy_unattributed"] for source in calculated
    )
    any_modern = any(source["has_modern_receipt"] for source in calculated)
    any_legacy = any(source["legacy_received"] for source in calculated)
    legacy_received_quantity = (
        sum(int(source["received"]) for source in calculated if source["legacy_received"])
        if any_legacy
        and all(
            source["received"] is not None
            for source in calculated
            if source["legacy_received"]
        )
        else None
    )
    any_received = any((source["received"] or 0) > 0 for source in calculated)
    all_closed = bool(calculated) and all(source["closed"] for source in calculated)
    accepted_short = any(source["accepted_short"] for source in calculated)
    planned_quantities_known = bool(calculated) and all(
        source["planned"] is not None for source in calculated
    )
    received_quantities_known = bool(calculated) and all(
        source["received"] is not None for source in calculated
    )
    planned_total = (
        sum(int(source["planned"]) for source in calculated)
        if planned_quantities_known and not source_conflict and not legacy_unattributed
        else None
    )
    received_total = (
        sum(int(source["received"]) for source in calculated)
        if received_quantities_known and not source_conflict and not legacy_unattributed
        else None
    )
    remaining = None
    if (
        planned_quantities_known
        and received_quantities_known
        and not source_conflict
        and not legacy_unattributed
    ):
        remaining = sum(
            0
            if source["closed"]
            else max(int(source["planned"]) - int(source["received"]), 0)
            for source in calculated
        )
    physical_shortage_quantity = None
    if accepted_short and not source_conflict and not legacy_unattributed:
        physical_shortage_quantity = sum(
            max(int(source["planned"]) - int(source["received"]), 0)
            for source in calculated
            if source["accepted_short"]
            and source["planned"] is not None
            and source["received"] is not None
        )
        if all_closed:
            # A short-receipt decision closes the business requirement even
            # when another closed legacy source no longer has its original
            # planned quantity available for an exact physical total.
            remaining = 0

    if task.status == "not_required":
        state, state_label = "not_required", "无需来料（库存覆盖）"
    elif source_conflict:
        state, state_label = "source_conflict", "收料来源冲突，需人工核对"
    elif legacy_unattributed:
        state, state_label = (
            "legacy_received_unattributed",
            "历史父单已收料（无法归属当前模切组件）",
        )
    elif all_closed and accepted_short:
        state, state_label = "received_accept_short", "已短收结单"
    elif any_legacy and all_closed:
        state = "legacy_received"
        state_label = (
            f"已收料（历史实收 {received_total} 张，原计划未记录）"
            if received_total is not None
            else "已收料（历史实收数量未记录）"
        )
    elif all_closed:
        state, state_label = "received", "已收料"
    elif any_modern or any_received or any_legacy:
        state, state_label = "partially_received", "部分收料"
    elif locations:
        state, state_label = "covered_by_inventory", "已备料（库存预占）"
    else:
        state, state_label = "not_received", "未收料"

    receipt_source = (
        "conflicting_sources"
        if source_conflict
        else "legacy_status_unattributed"
        if legacy_unattributed
        else "posted_receipt_items"
        if any_modern
        else "legacy_status"
        if any_legacy
        else "no_receipt_fact"
    )
    receipt_proven = any_modern or any_received or any_legacy
    return {
        "visibility": "visible",
        "state": state,
        "state_label": state_label,
        "planned_quantity": planned_total,
        "received_quantity": received_total,
        "legacy_received_quantity": legacy_received_quantity,
        "remaining_quantity": remaining,
        "physical_shortage_quantity": physical_shortage_quantity,
        "quantity_unit": "sheets",
        "receipt_source": receipt_source,
        "location_state": (
            "recorded"
            if locations
            else "not_recorded"
            if receipt_proven
            else "not_applicable"
        ),
        "location_display": (
            "已收料，系统尚无可证明的材料库位"
            if receipt_proven and not locations
            else None
        ),
        "locations": locations,
        "warning": warning,
    }


def _mold_live_tasks(
    db: Session,
    *,
    task_ids: list[int],
    allowed_customer_ids: set[int] | None,
    include_incoming: bool,
) -> list[dict]:
    if not task_ids:
        return []
    task_rows = list_production_tasks(
        db,
        allowed_customer_ids=allowed_customer_ids,
        task_ids=task_ids,
    )
    tasks_by_id = {
        row.id: (row, item, order)
        for row, item, order in db.execute(
            select(ProductionTask, OrderItem, Order)
            .join(OrderItem, OrderItem.id == ProductionTask.order_item_id)
            .join(Order, Order.id == OrderItem.order_id)
            .where(ProductionTask.id.in_(task_ids))
        ).all()
    }
    result: list[dict] = []
    for payload in task_rows:
        task, item, order = tasks_by_id[int(payload["id"])]
        material: dict = {"visibility": "hidden_by_permission"}
        if include_incoming:
            material = _material_facts_for_task(db, task=task, item=item)
        result.append(
            {
                "production_task_id": int(task.id),
                "production_task_version": int(task.version),
                "order_id": int(order.id),
                "order_item_id": int(item.id),
                "order_number": order.order_number,
                "customer_po": order.customer_po,
                "order_status": order.status,
                "order_status_label": persisted_order_status_label(order.status),
                "delivery_date": order.delivery_date.isoformat()
                if order.delivery_date
                else None,
                "product_id": payload.get("product_id"),
                "product_code": payload.get("product_code"),
                "product_name": payload.get("product_name"),
                "customer_id": payload.get("customer_id"),
                "customer_name": payload.get("customer_name"),
                "item_order_number": payload.get("item_order_number"),
                "specification": payload.get("specification"),
                "material_specification": payload.get("material"),
                "flute_type": payload.get("flute"),
                "special_process": payload.get("special_process"),
                "production_process": payload.get("production_process"),
                "production_notes": payload.get("production_notes"),
                "printing_situation": payload.get("printing_situation"),
                "printing_plate_mode": payload.get("printing_plate_mode"),
                "printing_colors": payload.get("printing_colors") or [],
                "printing_colors_frozen": payload.get("printing_colors_frozen"),
                "printing_plates": payload.get("printing_plates") or [],
                "is_component_task": bool(payload.get("is_component_task")),
                "order_quantity": int(payload.get("order_quantity") or 0),
                "parent_order_quantity": int(
                    payload.get("parent_order_quantity") or 0
                ),
                "production_quantity_unit": payload.get(
                    "production_quantity_unit"
                ),
                "task_status": payload.get("status"),
                "task_status_label": (
                    "已完工待送"
                    if payload.get("status") == "completed"
                    else _MOLD_TASK_STATUS_LABELS.get(
                        str(payload.get("status")),
                        str(payload.get("status") or ""),
                    )
                ),
                "planned_quantity": int(payload.get("planned_quantity") or 0),
                "actual_output_quantity": int(
                    payload.get("actual_output_quantity") or 0
                ),
                "linkage_basis": (
                    "bom_order_snapshot"
                    if payload.get("is_component_task")
                    else "versioned_current_product_binding"
                ),
                "material": material,
            }
        )
    return result


def _mold_scan_event_dict(row: MoldScanEvent) -> dict:
    location_guide = describe_mold_location(row.mold_location_snapshot)
    return {
        "id": int(row.id),
        "mold_tool_id": int(row.mold_tool_id),
        "mold_code": row.mold_code_snapshot,
        "mold_location": row.mold_location_snapshot,
        "mold_location_display": location_guide["prompt"],
        "mold_location_guide": location_guide,
        "mold_location_version": int(row.mold_location_version_snapshot),
        "production_task_id": int(row.production_task_id_snapshot),
        "production_task_version": int(row.production_task_version_snapshot),
        "production_task_status": row.production_task_status_snapshot,
        "linkage_basis": row.linkage_basis_snapshot,
        "order_id": int(row.sales_order_id_snapshot),
        "order_number": row.sales_order_number_snapshot,
        "order_item_id": int(row.sales_order_item_id_snapshot),
        "customer_id": int(row.customer_id_snapshot),
        "customer_name": row.customer_name_snapshot,
        "product_code": row.product_code_snapshot,
        "product_name": row.product_name_snapshot,
        "scanned_by": row.scanned_by_name_snapshot,
        "scanned_at": beijing_naive_to_api(row.scanned_at),
        "source": row.source,
    }


def _mold_scan_history(
    db: Session,
    *,
    mold_id: int,
    allowed_customer_ids: set[int] | None,
    production_task_id: int | None = None,
) -> dict:
    conditions = [MoldScanEvent.mold_tool_id == mold_id]
    if production_task_id is not None:
        conditions.append(
            MoldScanEvent.production_task_id_snapshot == production_task_id
        )
    if allowed_customer_ids is not None:
        conditions.append(
            MoldScanEvent.customer_id_snapshot.in_(allowed_customer_ids)
        )
    total = int(
        db.scalar(
            select(func.count(MoldScanEvent.id)).where(*conditions)
        )
        or 0
    )
    rows = db.scalars(
        select(MoldScanEvent)
        .where(*conditions)
        .order_by(MoldScanEvent.scanned_at.desc(), MoldScanEvent.id.desc())
        .limit(50)
    ).all()
    return {
        "total": total,
        "items": [_mold_scan_event_dict(row) for row in rows],
    }


@router.get("/molds/live/{mold_id}")
def get_mold_live_status(
    mold_id: int,
    response: Response,
    production_task_id: int | None = Query(default=None, gt=0),
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    response.headers["Cache-Control"] = "private, no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Vary"] = "Cookie"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    row = db.scalar(
        select(MoldTool)
        .options(
            selectinload(MoldTool.customer_links).selectinload(
                MoldToolCustomer.customer
            ),
            selectinload(MoldTool.products).selectinload(Product.customer),
            selectinload(MoldTool.products).selectinload(Product.material),
        )
        .where(MoldTool.id == mold_id)
    )
    if row is None:
        raise HTTPException(
            status_code=404,
            detail="查询对象不存在",
            headers={"Cache-Control": "private, no-store, max-age=0"},
        )
    allowed_customer_ids = _mold_customer_scope(user, db)
    products = _visible_mold_products(row, allowed_customer_ids)
    snapshot_customer_access = False
    if allowed_customer_ids is not None and not products:
        snapshot_customer_access = db.scalar(
            select(ProductionTask.id)
            .join(
                SalesOrderItemBomComponent,
                SalesOrderItemBomComponent.id
                == ProductionTask.sales_order_item_bom_component_id,
            )
            .join(OrderItem, OrderItem.id == ProductionTask.order_item_id)
            .join(Order, Order.id == OrderItem.order_id)
            .where(
                SalesOrderItemBomComponent.snapshot_mold_tool_id == row.id,
                Order.customer_id.in_(allowed_customer_ids),
                *order_item_forward_fulfillment_sql_conditions(
                    order_status_column=Order.status,
                    ordered_quantity_column=OrderItem.quantity,
                    delivered_quantity_column=OrderItem.delivered_quantity,
                    is_force_closed_column=OrderItem.is_force_closed,
                ),
            )
            .limit(1)
        ) is not None
        if not snapshot_customer_access:
            raise HTTPException(
                status_code=404,
                detail="查询对象不存在",
                headers={"Cache-Control": "private, no-store, max-age=0"},
            )
    basics = _mold_tool_dict(row, allowed_customer_ids)
    dynamic_allowed = (
        has_permission(user, "orders.view")
        and has_permission(user, "production.die_cut.view")
    )
    if production_task_id is not None and not dynamic_allowed:
        raise HTTPException(status_code=403, detail="当前账号无模具生产任务查看权限")
    task_ids: list[int] = []
    warnings: list[str] = []
    tasks: list[dict] = []
    if dynamic_allowed:
        task_ids, warnings = _mold_live_task_ids(
            db,
            mold=row,
            products=products,
            allowed_customer_ids=allowed_customer_ids,
        )
        if production_task_id is not None:
            if production_task_id not in task_ids:
                raise HTTPException(
                    status_code=409,
                    detail="所选生产任务与当前模具不匹配、已结束或已发生变化，请重新扫描",
                )
            task_ids = [production_task_id]
        tasks = _mold_live_tasks(
            db,
            task_ids=task_ids,
            allowed_customer_ids=allowed_customer_ids,
            include_incoming=has_permission(user, "incoming.view"),
        )
    response_products = products
    task_context = None
    if production_task_id is not None:
        if len(tasks) != 1:
            raise HTTPException(status_code=409, detail="生产任务已变化，请重新扫描")
        selected_task = tasks[0]
        selected_product_id = selected_task.get("product_id")
        response_products = [
            product
            for product in products
            if selected_product_id is not None and product.id == selected_product_id
        ]
        task_context = {
            "production_task_id": int(selected_task["production_task_id"]),
            "product_id": selected_product_id,
            "product_code": selected_task.get("product_code"),
        }
        warnings = []
    scan_history = (
        _mold_scan_history(
            db,
            mold_id=row.id,
            allowed_customer_ids=allowed_customer_ids,
            production_task_id=production_task_id,
        )
        if dynamic_allowed
        else {"total": None, "items": []}
    )
    return {
        "schema_version": "mold-live-v4",
        "as_of": beijing_naive_to_api(beijing_now_naive()),
        "read_only": True,
        "mode": (
            "restricted"
            if not dynamic_allowed
            else "task_context"
            if task_context is not None
            else "current_orders"
            if tasks
            else "mold_master"
        ),
        "task_visibility": (
            "visible" if dynamic_allowed else "hidden_by_permission"
        ),
        "task_context": task_context,
        "mold": {
            "display_name": _mold_display_name(row, allowed_customer_ids),
            "label_identity": _label_identity(row, products),
            "identity_status": row.identity_status,
            "rack_location": row.rack_location,
            "location_guide": basics["location_guide"],
            "location_version": row.location_version,
            "repair_status": row.repair_status,
            "repair_status_label": "待维修" if row.repair_status == "needs_repair" else "正常",
            "repair_version": row.repair_version,
            "is_active": row.is_active,
            "archive_status": row.archive_status,
        },
        "bindings": {
            "total": len(response_products),
            "items": [
                _mold_live_binding_dict(row, product)
                for product in response_products
            ],
        },
        "current_orders": {
            "total": len(tasks) if dynamic_allowed else None,
            "items": tasks if dynamic_allowed else [],
        },
        "scan_recording_allowed": dynamic_allowed,
        "scan_history": scan_history,
        "warnings": warnings,
    }


@router.post("/molds/live/{mold_id}/scan-events")
def register_mold_scan_event(
    mold_id: int,
    payload: MoldScanEventPayload,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    """Record one authenticated fixed-QR scan without changing production state."""

    response.headers["Cache-Control"] = "private, no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Vary"] = "Cookie"
    if not (
        has_permission(user, "orders.view")
        and has_permission(user, "production.die_cut.view")
    ):
        raise HTTPException(status_code=403, detail="当前账号无模具生产任务扫码权限")

    row = db.scalar(
        select(MoldTool)
        .options(
            selectinload(MoldTool.products).selectinload(Product.customer),
            selectinload(MoldTool.products).selectinload(Product.material),
        )
        .where(MoldTool.id == mold_id)
    )
    if row is None:
        raise HTTPException(status_code=404, detail="查询对象不存在")
    allowed_customer_ids = _mold_customer_scope(user, db)
    products = _visible_mold_products(row, allowed_customer_ids)
    task_ids, _warnings = _mold_live_task_ids(
        db,
        mold=row,
        products=products,
        allowed_customer_ids=allowed_customer_ids,
    )
    if allowed_customer_ids is not None and not products and not task_ids:
        raise HTTPException(status_code=404, detail="查询对象不存在")

    request_hash = hashlib.sha256(
        json.dumps(
            {
                "mold_tool_id": int(row.id),
                "production_task_id": int(payload.production_task_id),
                "expected_mold_location_version": int(
                    payload.expected_mold_location_version
                ),
                "scanned_by": int(user.id),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()

    def replay(existing: MoldScanEvent) -> dict:
        if existing.request_hash != request_hash:
            raise HTTPException(
                status_code=409,
                detail="扫码凭证已用于另一笔任务，请重新扫描模具二维码",
            )
        return {"replayed": True, "event": _mold_scan_event_dict(existing)}

    existing = db.scalar(
        select(MoldScanEvent).where(
            MoldScanEvent.idempotency_key == payload.idempotency_key
        )
    )
    if existing is not None:
        return replay(existing)
    if int(row.location_version) != payload.expected_mold_location_version:
        raise HTTPException(
            status_code=409,
            detail="模具位置已变化，请刷新并重新核对现场位置后再登记",
        )
    if payload.production_task_id not in task_ids:
        raise HTTPException(
            status_code=409,
            detail="该生产任务已变化、已结束或没有可证明的模具绑定，请刷新后重新选择",
        )

    task_payloads = _mold_live_tasks(
        db,
        task_ids=[payload.production_task_id],
        allowed_customer_ids=allowed_customer_ids,
        include_incoming=False,
    )
    if len(task_payloads) != 1:
        raise HTTPException(status_code=409, detail="生产任务已变化，请刷新后重新选择")
    task_payload = task_payloads[0]
    event = MoldScanEvent(
        mold_tool_id=row.id,
        mold_code_snapshot=row.mold_code,
        mold_location_snapshot=row.rack_location,
        mold_location_version_snapshot=int(row.location_version),
        production_task_id=int(task_payload["production_task_id"]),
        production_task_id_snapshot=int(task_payload["production_task_id"]),
        production_task_version_snapshot=int(
            task_payload["production_task_version"]
        ),
        production_task_status_snapshot=str(task_payload["task_status"]),
        linkage_basis_snapshot=str(task_payload["linkage_basis"]),
        sales_order_id=int(task_payload["order_id"]),
        sales_order_id_snapshot=int(task_payload["order_id"]),
        sales_order_number_snapshot=str(task_payload["order_number"]),
        sales_order_item_id=int(task_payload["order_item_id"]),
        sales_order_item_id_snapshot=int(task_payload["order_item_id"]),
        customer_id_snapshot=int(task_payload["customer_id"]),
        customer_name_snapshot=str(task_payload["customer_name"]),
        product_code_snapshot=task_payload.get("product_code"),
        product_name_snapshot=task_payload.get("product_name"),
        scanned_by=user.id,
        scanned_by_name_snapshot=user.username,
        scanned_at=beijing_now_naive(),
        source="fixed_qr",
        idempotency_key=payload.idempotency_key,
        request_hash=request_hash,
    )
    try:
        db.add(event)
        db.flush()
        db.add(
            OperationLog(
                user_id=user.id,
                username=user.username,
                role=user.role,
                action="SCAN",
                resource=f"warehouse/molds/{row.id}/scan-events",
                entity_type="mold_scan_event",
                entity_id=event.id,
                description="扫描固定模具二维码并关联生产任务",
                details=json.dumps(
                    {
                        "mold_tool_id": row.id,
                        "mold_code": row.mold_code,
                        "mold_location": row.rack_location,
                        "mold_location_version": row.location_version,
                        "production_task_id": task_payload["production_task_id"],
                        "production_task_version": task_payload[
                            "production_task_version"
                        ],
                        "linkage_basis": task_payload["linkage_basis"],
                        "order_id": task_payload["order_id"],
                        "order_number": task_payload["order_number"],
                        "idempotency_key": payload.idempotency_key,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                ip_address=request.client.host if request.client else None,
                user_agent=request.headers.get("user-agent"),
                event_category="business_operation",
                result="success",
                source="mobile_qr",
                module_code="warehouse_mold",
                action_code="MOLD_TASK_QR_SCAN",
                actor_user_id_snapshot=user.id,
                operator_name_snapshot=user.username,
                object_ref=f"mold_scan_event:{event.id}",
                customer_id_snapshot=int(task_payload["customer_id"]),
                customer_name_snapshot=str(task_payload["customer_name"]),
                schema_version=1,
            )
        )
        db.commit()
        db.refresh(event)
    except IntegrityError as error:
        db.rollback()
        concurrent = db.scalar(
            select(MoldScanEvent).where(
                MoldScanEvent.idempotency_key == payload.idempotency_key
            )
        )
        if concurrent is not None:
            return replay(concurrent)
        raise HTTPException(
            status_code=409,
            detail="扫码任务已变化，请刷新后重新扫描",
        ) from error
    return {"replayed": False, "event": _mold_scan_event_dict(event)}


def _execute_mold_label_layout_operation(
    db: Session,
    *,
    user: User,
    operation_kind: Literal["save_and_publish", "restore_default", "rollback"],
    payload: MoldLabelLayoutSaveRequest | MoldLabelLayoutReleaseRequest,
) -> dict:
    if operation_kind == "save_and_publish":
        if not isinstance(payload, MoldLabelLayoutSaveRequest):
            raise MoldLabelLayoutError("模具标签布局保存请求无效")
        return save_and_publish_mold_label_layout(
            db,
            layout=payload.layout.model_dump(),
            expected_release_version=payload.expected_release_version,
            operation_key=payload.operation_key,
            actor_id=user.id,
        )
    if not isinstance(payload, MoldLabelLayoutReleaseRequest):
        raise MoldLabelLayoutError("模具标签布局版本请求无效")
    common = {
        "expected_release_version": payload.expected_release_version,
        "operation_key": payload.operation_key,
        "actor_id": user.id,
    }
    return (
        restore_default_mold_label_layout(db, **common)
        if operation_kind == "restore_default"
        else rollback_mold_label_layout(db, **common)
    )


def _apply_mold_label_layout_write(
    db: Session,
    *,
    request: Request,
    user: User,
    operation_kind: Literal["save_and_publish", "restore_default", "rollback"],
    payload: MoldLabelLayoutSaveRequest | MoldLabelLayoutReleaseRequest,
) -> dict:
    with _MOLD_LABEL_LAYOUT_WRITE_LOCK:
        try:
            before = effective_mold_label_layout(db)
        except MoldLabelLayoutError as error:
            db.rollback()
            raise HTTPException(status_code=409, detail=str(error)) from error
        try:
            result = _execute_mold_label_layout_operation(
                db,
                user=user,
                operation_kind=operation_kind,
                payload=payload,
            )
            if not result["replayed"]:
                descriptions = {
                    "save_and_publish": "保存并发布40×80模具标签布局",
                    "restore_default": "恢复并发布40×80模具标签默认布局",
                    "rollback": "回滚并发布上一版40×80模具标签布局",
                }
                after = result["published"]
                append_audit_event(
                    db,
                    event_category="business",
                    result="success",
                    source="web",
                    module_code="warehouse",
                    action_code=f"warehouse.mold_label_layout.{operation_kind}",
                    legacy_action="MOLD_LABEL_LAYOUT",
                    resource="MoldLabelLayoutRevision",
                    request=request,
                    actor=user,
                    entity_type="mold_label_layout",
                    object_ref="mold_label_layout:40x80",
                    batch_id=payload.operation_key,
                    description=descriptions[operation_kind],
                    details={
                        "operation_kind": operation_kind,
                        "before_version": before["version"],
                        "release_version": after["version"],
                        "layout_hash": after["layout_hash"],
                        "diff": mold_label_layout_diff_summary(
                            before["layout"], after["layout"]
                        ),
                    },
                )
            db.commit()
            return result
        except MoldLabelLayoutConflict as error:
            db.rollback()
            raise HTTPException(status_code=409, detail=str(error)) from error
        except MoldLabelLayoutError as error:
            db.rollback()
            raise HTTPException(status_code=422, detail=str(error)) from error
        except IntegrityError as error:
            db.rollback()
            try:
                replay = _execute_mold_label_layout_operation(
                    db,
                    user=user,
                    operation_kind=operation_kind,
                    payload=payload,
                )
                if replay.get("replayed"):
                    return replay
                db.rollback()
            except MoldLabelLayoutConflict as replay_error:
                db.rollback()
                raise HTTPException(status_code=409, detail=str(replay_error)) from error
            except MoldLabelLayoutError as replay_error:
                db.rollback()
                raise HTTPException(status_code=422, detail=str(replay_error)) from error
            raise HTTPException(status_code=409, detail="模具标签布局已被其他请求更新，请重新加载") from error
        except Exception:
            db.rollback()
            raise


@router.get("/molds/label-layout")
def get_effective_mold_label_layout(
    db: Session = Depends(get_db),
    _user: User = Depends(can_read),
) -> dict:
    try:
        return effective_mold_label_layout(db)
    except MoldLabelLayoutError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.get("/molds/label-layout/admin")
def get_mold_label_layout_admin_state(
    db: Session = Depends(get_db),
    _user: User = Depends(admin_only),
) -> dict:
    try:
        return mold_label_layout_admin_state(db)
    except MoldLabelLayoutError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post("/molds/label-layout/admin/publish")
def post_mold_label_layout_publish(
    payload: MoldLabelLayoutSaveRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    return _apply_mold_label_layout_write(
        db,
        request=request,
        user=user,
        operation_kind="save_and_publish",
        payload=payload,
    )


@router.post("/molds/label-layout/admin/restore-default")
def post_mold_label_layout_restore_default(
    payload: MoldLabelLayoutReleaseRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    return _apply_mold_label_layout_write(
        db,
        request=request,
        user=user,
        operation_kind="restore_default",
        payload=payload,
    )


@router.post("/molds/label-layout/admin/rollback")
def post_mold_label_layout_rollback(
    payload: MoldLabelLayoutReleaseRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    return _apply_mold_label_layout_write(
        db,
        request=request,
        user=user,
        operation_kind="rollback",
        payload=payload,
    )


def _mold_label_print_job_layout(job: MoldLabelPrintJob) -> dict | None:
    if job.template_version == MOLD_LABEL_TEMPLATE_40X30:
        if any(
            value is not None
            for value in (
                job.label_layout_version,
                job.label_layout_payload_json,
                job.label_layout_payload_hash,
            )
        ):
            raise HTTPException(status_code=409, detail="40×30模具标签打印任务布局快照异常")
        return None
    try:
        return load_mold_label_layout_snapshot(
            version=job.label_layout_version,
            payload_json=job.label_layout_payload_json,
            payload_hash=job.label_layout_payload_hash,
        )
    except MoldLabelLayoutError as error:
        raise HTTPException(
            status_code=409,
            detail=(
                "该历史40×80模具标签任务没有可验证的冻结布局，"
                "不能按当前新版式冒充补打，请重新登记打印"
            ),
        ) from error


def _mold_label_print_job_for_replay(
    db: Session,
    *,
    print_job_id: int,
    template_version: str,
    mold_ids: list[int],
) -> MoldLabelPrintJob:
    job = db.scalar(
        select(MoldLabelPrintJob)
        .options(selectinload(MoldLabelPrintJob.items))
        .where(MoldLabelPrintJob.id == print_job_id)
    )
    if job is None:
        raise HTTPException(status_code=404, detail="模具标签打印任务不存在")
    stored_ids = [item.mold_tool_id for item in job.items]
    if job.template_version != template_version or stored_ids != mold_ids:
        raise HTTPException(
            status_code=409,
            detail="打印任务的模具、顺序或纸型与当前页面不一致，请从仓库重新打开",
        )
    return job


@router.get("/molds/labels")
def get_mold_labels(
    response: Response,
    mold_ids: str = Query(min_length=1, max_length=1200),
    print_job_id: int | None = Query(default=None, gt=0),
    template_version: Literal["mold_40x30_v1", "mold_80x40_v1"] = Query(
        default=MOLD_LABEL_TEMPLATE_40X30
    ),
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    response.headers["Cache-Control"] = "private, no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Vary"] = "Cookie"
    raw_ids = [part.strip() for part in mold_ids.split(",") if part.strip()]
    if not raw_ids or any(not part.isdigit() or int(part) <= 0 for part in raw_ids):
        raise HTTPException(status_code=422, detail="模具批量标签参数无效")
    ordered_ids = list(dict.fromkeys(int(part) for part in raw_ids))
    if len(ordered_ids) > 100:
        raise HTTPException(status_code=422, detail="一次最多打印 100 件模具")
    if (
        template_version == MOLD_LABEL_TEMPLATE_80X40
        and print_job_id is None
    ):
        raise HTTPException(
            status_code=409,
            detail="40×80模具标签必须从已登记的冻结打印任务打开",
        )
    print_job = None
    frozen_layout = None
    if print_job_id is not None:
        print_job = _mold_label_print_job_for_replay(
            db,
            print_job_id=print_job_id,
            template_version=template_version,
            mold_ids=ordered_ids,
        )
        frozen_layout = _mold_label_print_job_layout(print_job)
    rows = db.scalars(
        select(MoldTool)
        .options(
            selectinload(MoldTool.customer_links).selectinload(
                MoldToolCustomer.customer
            ),
            selectinload(MoldTool.products).selectinload(Product.customer),
            selectinload(MoldTool.products).selectinload(Product.material),
        )
        .where(MoldTool.id.in_(ordered_ids))
    ).unique().all()
    rows_by_id = {row.id: row for row in rows}
    missing_ids = [mold_id for mold_id in ordered_ids if mold_id not in rows_by_id]
    if missing_ids:
        raise HTTPException(status_code=404, detail="所选模具已变化，请返回列表重新选择")
    allowed_customer_ids = _mold_customer_scope(user, db)
    if allowed_customer_ids is not None:
        raise HTTPException(
            status_code=403,
            detail="实体模具标签仅允许全客户范围的仓库账号打印",
        )
    ordered_rows = [rows_by_id[mold_id] for mold_id in ordered_ids]
    for row in ordered_rows:
        _require_mold_customer_scope(row, allowed_customer_ids)
    return {
        "items": [
            _mold_label_dict(row, allowed_customer_ids, template_version)
            for row in ordered_rows
        ],
        "count": len(ordered_rows),
        "template_version": template_version,
        "label_layout": frozen_layout,
        "print_job": (
            _mold_label_print_job_dict(print_job, replayed=True)
            if print_job is not None
            else None
        ),
    }


def _mold_label_print_job_dict(
    job: MoldLabelPrintJob,
    *,
    replayed: bool,
) -> dict:
    layout = _mold_label_print_job_layout(job)
    return {
        "print_job_id": job.id,
        "source": job.source,
        "template_version": job.template_version,
        "template_label": mold_label_template_label(job.template_version),
        "count": job.item_count,
        "mold_ids": [item.mold_tool_id for item in job.items],
        "printed_at": utc_naive_to_api(job.printed_at) if job.printed_at else None,
        "printed_by": job.printed_by_username,
        "replayed": replayed,
        "label_layout": layout,
    }


@router.post("/molds/label-prints")
def register_mold_label_print(
    payload: MoldLabelPrintRegisterPayload,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    allowed_customer_ids = _mold_customer_scope(user, db)
    if allowed_customer_ids is not None:
        raise HTTPException(
            status_code=403,
            detail="实体模具标签仅允许全客户范围的仓库账号打印",
        )
    existing = db.scalar(
        select(MoldLabelPrintJob)
        .options(selectinload(MoldLabelPrintJob.items))
        .where(MoldLabelPrintJob.idempotency_key == payload.idempotency_key)
    )
    if existing is not None:
        existing_ids = [item.mold_tool_id for item in existing.items]
        if (
            existing.source != payload.source
            or existing.template_version != payload.template_version
            or existing_ids != payload.mold_ids
        ):
            raise HTTPException(
                status_code=409,
                detail="该打印凭证已用于另一组模具，请刷新列表后重新操作",
            )
        return _mold_label_print_job_dict(existing, replayed=True)

    rows = db.scalars(
        select(MoldTool)
        .options(
            selectinload(MoldTool.customer_links).selectinload(
                MoldToolCustomer.customer
            ),
            selectinload(MoldTool.products).selectinload(Product.customer),
            selectinload(MoldTool.products).selectinload(Product.material),
        )
        .where(MoldTool.id.in_(payload.mold_ids))
    ).unique().all()
    rows_by_id = {row.id: row for row in rows}
    if any(mold_id not in rows_by_id for mold_id in payload.mold_ids):
        raise HTTPException(status_code=404, detail="所选模具已变化，请返回列表重新选择")
    ordered_rows = [rows_by_id[mold_id] for mold_id in payload.mold_ids]
    for row in ordered_rows:
        _require_mold_customer_scope(row, allowed_customer_ids)
        # Reuse the existing label serializer as the single source of truth for
        # active/binding/size/identity printability.  No print fact is written
        # when any selected label is invalid.
        _mold_label_dict(row, allowed_customer_ids, payload.template_version)

    layout_envelope = None
    if payload.template_version == MOLD_LABEL_TEMPLATE_80X40:
        try:
            layout_envelope = effective_mold_label_layout(db)
        except MoldLabelLayoutError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
    job = MoldLabelPrintJob(
        idempotency_key=payload.idempotency_key,
        source=payload.source,
        item_count=len(ordered_rows),
        template_version=payload.template_version,
        label_layout_version=(
            int(layout_envelope["version"]) if layout_envelope is not None else None
        ),
        label_layout_payload_json=(
            canonical_mold_label_layout_json(layout_envelope["layout"])
            if layout_envelope is not None
            else None
        ),
        label_layout_payload_hash=(
            str(layout_envelope["layout_hash"])
            if layout_envelope is not None
            else None
        ),
        printed_by=user.id,
        printed_by_username=user.username,
    )
    db.add(job)
    try:
        db.flush()
        for item_order, row in enumerate(ordered_rows, start=1):
            db.add(
                MoldLabelPrintJobItem(
                    print_job_id=job.id,
                    mold_tool_id=row.id,
                    item_order=item_order,
                    mold_code_snapshot=row.mold_code,
                    rack_location_snapshot=row.rack_location,
                )
            )
        db.add(
            OperationLog(
                user_id=user.id,
                username=user.username,
                role=user.role,
                action="PRINT",
                resource="warehouse/molds/label-prints",
                entity_type="mold_label_print_job",
                entity_id=job.id,
                description=(
                    "批量打印模具标签" if payload.source == "batch" else "打印模具标签"
                ),
                details=json.dumps(
                    {
                        "source": payload.source,
                        "template_version": payload.template_version,
                        "mold_ids": payload.mold_ids,
                        "mold_codes": [row.mold_code for row in ordered_rows],
                        "label_layout_version": (
                            layout_envelope["version"]
                            if layout_envelope is not None
                            else None
                        ),
                        "label_layout_hash": (
                            layout_envelope["layout_hash"]
                            if layout_envelope is not None
                            else None
                        ),
                        "idempotency_key": payload.idempotency_key,
                    },
                    ensure_ascii=False,
                ),
                ip_address=request.client.host if request.client else None,
                user_agent=request.headers.get("user-agent"),
                event_category="business_operation",
                result="success",
                source="api",
                module_code="warehouse",
                action_code="mold.label.print",
                actor_user_id_snapshot=user.id,
                operator_name_snapshot=user.username,
                object_ref=f"mold_label_print_job:{job.id}",
                request_id=request.headers.get("x-request-id"),
                batch_id=payload.idempotency_key if payload.source == "batch" else None,
                schema_version=1,
            )
        )
        db.commit()
    except IntegrityError:
        db.rollback()
        replay = db.scalar(
            select(MoldLabelPrintJob)
            .options(selectinload(MoldLabelPrintJob.items))
            .where(MoldLabelPrintJob.idempotency_key == payload.idempotency_key)
        )
        if replay is None:
            raise
        replay_ids = [item.mold_tool_id for item in replay.items]
        if (
            replay.source != payload.source
            or replay.template_version != payload.template_version
            or replay_ids != payload.mold_ids
        ):
            raise HTTPException(
                status_code=409,
                detail="该打印凭证已用于另一组模具，请刷新列表后重新操作",
            )
        return _mold_label_print_job_dict(replay, replayed=True)
    db.refresh(job)
    return _mold_label_print_job_dict(job, replayed=False)


@router.get("/molds/{mold_id}/label")
def get_mold_label(
    mold_id: int,
    response: Response,
    print_job_id: int | None = Query(default=None, gt=0),
    template_version: Literal["mold_40x30_v1", "mold_80x40_v1"] = Query(
        default=MOLD_LABEL_TEMPLATE_40X30
    ),
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    response.headers["Cache-Control"] = "private, no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Vary"] = "Cookie"
    row = db.scalar(
        select(MoldTool)
        .options(
            selectinload(MoldTool.customer_links).selectinload(
                MoldToolCustomer.customer
            ),
            selectinload(MoldTool.products).selectinload(Product.customer),
            selectinload(MoldTool.products).selectinload(Product.material),
        )
        .where(MoldTool.id == mold_id)
    )
    if row is None:
        raise HTTPException(status_code=404, detail="模具不存在")
    allowed_customer_ids = _mold_customer_scope(user, db)
    if allowed_customer_ids is not None:
        raise HTTPException(
            status_code=403,
            detail="实体模具标签仅允许全客户范围的仓库账号打印",
        )
    _require_mold_customer_scope(row, allowed_customer_ids)
    if (
        template_version == MOLD_LABEL_TEMPLATE_80X40
        and print_job_id is None
    ):
        raise HTTPException(
            status_code=409,
            detail="40×80模具标签必须从已登记的冻结打印任务打开",
        )
    print_job = None
    frozen_layout = None
    if print_job_id is not None:
        print_job = _mold_label_print_job_for_replay(
            db,
            print_job_id=print_job_id,
            template_version=template_version,
            mold_ids=[mold_id],
        )
        frozen_layout = _mold_label_print_job_layout(print_job)
    result = _mold_label_dict(row, allowed_customer_ids, template_version)
    if template_version == MOLD_LABEL_TEMPLATE_80X40:
        assert print_job is not None
        result["label_layout"] = frozen_layout
        result["print_job"] = _mold_label_print_job_dict(
            print_job,
            replayed=True,
        )
    return result


@router.get("/molds/{mold_id}/label-preview")
def get_mold_label_preview(
    mold_id: int,
    response: Response,
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    """Return the current V8 label projection without registering a print job."""

    response.headers["Cache-Control"] = "private, no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Vary"] = "Cookie"
    row = db.scalar(
        select(MoldTool)
        .options(
            selectinload(MoldTool.customer_links).selectinload(
                MoldToolCustomer.customer
            ),
            selectinload(MoldTool.products).selectinload(Product.customer),
            selectinload(MoldTool.products).selectinload(Product.material),
        )
        .where(MoldTool.id == mold_id)
    )
    if row is None:
        raise HTTPException(status_code=404, detail="模具不存在")
    allowed_customer_ids = _mold_customer_scope(user, db)
    if allowed_customer_ids is not None:
        raise HTTPException(
            status_code=403,
            detail="实体模具标签仅允许全客户范围的仓库账号预览",
        )
    _require_mold_customer_scope(row, allowed_customer_ids)
    try:
        label_layout = effective_mold_label_layout(db)
    except MoldLabelLayoutError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    result = _mold_label_dict(
        row,
        allowed_customer_ids,
        MOLD_LABEL_TEMPLATE_80X40,
        preview_only=True,
    )
    result.update(
        {
            "row": _mold_tool_dict(row, allowed_customer_ids),
            "label_layout": label_layout,
            "print_job": None,
            "preview_only": True,
        }
    )
    return result


@router.get("/molds/code-preview")
def preview_mold_code(
    mold_name: str = Query(min_length=1, max_length=200),
    customer_initials: str = Query(min_length=1, max_length=20),
    db: Session = Depends(get_db),
    _user: User = Depends(admin_only),
) -> dict:
    try:
        mold_code, parts = next_available_mold_code(
            db,
            mold_name=mold_name,
            customer_initials=customer_initials,
        )
    except MoldIdentityError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return {
        "mold_code": mold_code,
        "customer_label": parts.customer_label,
        "customer_initials": parts.customer_initials,
        "inventory_code": parts.inventory_code,
    }


@router.get("/molds/binding-products")
def search_mold_binding_products(
    customer_id: int = Query(gt=0),
    q: str | None = None,
    limit: int = Query(default=100, ge=1, le=200),
    db: Session = Depends(get_db),
    _user: User = Depends(admin_only),
) -> dict:
    customer = db.scalar(
        select(Customer).where(
            Customer.id == customer_id,
            Customer.is_active.is_(True),
        )
    )
    if customer is None:
        raise HTTPException(status_code=404, detail="客户不存在或已停用")
    query = (
        select(Product, MoldTool)
        .outerjoin(MoldTool, MoldTool.id == Product.mold_tool_id)
        .where(
            Product.customer_id == customer_id,
            Product.is_active.is_(True),
            Product.deleted_at.is_(None),
        )
    )
    keyword = (q or "").strip()
    if keyword:
        pattern = f"%{keyword}%"
        query = query.where(
            or_(
                Product.product_code.like(pattern),
                Product.customer_material_code.like(pattern),
                Product.product_name.like(pattern),
            )
        )
    rows = db.execute(
        query.order_by(
            Product.customer_material_code,
            Product.product_code,
            Product.product_name,
            Product.id,
        ).limit(limit)
    ).all()
    return {
        "customer": {"id": customer.id, "name": customer.name},
        "items": [
            {
                "id": product.id,
                "version": product.version,
                "product_code": product.product_code,
                "customer_material_code": product.customer_material_code,
                "product_name": product.product_name,
                "production_process": product.production_process,
                "mold_tool_id": mold.id if mold is not None else None,
                "mold_code": mold.mold_code if mold is not None else None,
                "mold_name": mold.mold_name if mold is not None else None,
            }
            for product, mold in rows
        ],
    }


@router.post("/molds", status_code=201)
def create_mold_tool(
    payload: MoldToolPayload,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    _assert_asset_location_operational(
        db,
        asset_kind="mold",
        location_text=payload.rack_location,
        claim_floor=True,
    )
    formal_identity = payload.customers is not None
    with _MOLD_CODE_WRITE_LOCK:
        if formal_identity:
            request_hash = _mold_master_request_hash(
                action="create",
                mold_id=None,
                payload=payload,
            )
            replay = _mold_master_replay(
                db,
                action="create",
                idempotency_key=payload.idempotency_key or "",
                request_hash=request_hash,
                actor_id=user.id,
            )
            if replay is not None:
                result = dict(replay)
                result["idempotent_replay"] = True
                return result
            display_name, chinese_short_name, customers = _formal_mold_identity(
                payload,
                db,
            )
            try:
                internal_code = next_available_internal_mold_code(db)
                row = MoldTool(
                    mold_code=internal_code,
                    mold_name=display_name,
                    label_name=normalize_mold_label_name(payload.label_name),
                    chinese_short_name=chinese_short_name,
                    label_overrides_json=(
                        canonical_label_overrides(payload.label_overrides.as_dict())
                        if payload.label_overrides is not None
                        else None
                    ),
                    identity_status="frozen",
                    version=1,
                    rack_location=payload.rack_location,
                    remarks=payload.remarks,
                    created_by=user.id,
                    updated_by=user.id,
                )
                db.add(row)
                db.flush()
                _replace_mold_customer_links(
                    db,
                    row=row,
                    payload=payload,
                    customers=customers,
                    actor_id=user.id,
                )
                db.flush()
                result_snapshot = _mold_tool_dict(row)
                mutation = MoldMasterMutation(
                    mold_tool=row,
                    action="create",
                    idempotency_key=payload.idempotency_key or "",
                    request_hash=request_hash,
                    actor_id=user.id,
                    result_version=1,
                    result_snapshot_json=json.dumps(
                        result_snapshot,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                )
                db.add(mutation)
                append_audit_event(
                    db,
                    request=request,
                    actor=user,
                    event_category="business",
                    result="success",
                    source="web",
                    module_code="warehouse",
                    action_code="mold.master.create",
                    legacy_action="CREATE",
                    resource="warehouse/molds",
                    entity_type="mold_tool",
                    entity_id=row.id,
                    object_ref=row.mold_code,
                    description="新增模具正式客户与标签资料",
                    details={
                        "identity_status": "frozen",
                        "version": 1,
                        "customer_ids": [customer.id for customer in customers],
                        "primary_customer_ids": [
                            item.customer_id
                            for item in (payload.customers or [])
                            if item.display_order is not None
                        ],
                        "label_overrides": parse_label_overrides(
                            row.label_overrides_json
                        ),
                        "idempotency_key": payload.idempotency_key,
                    },
                )
                db.commit()
            except (IntegrityError, MoldIdentityError) as error:
                db.rollback()
                replay = _mold_master_replay(
                    db,
                    action="create",
                    idempotency_key=payload.idempotency_key or "",
                    request_hash=request_hash,
                    actor_id=user.id,
                )
                if replay is not None:
                    result = dict(replay)
                    result["idempotent_replay"] = True
                    return result
                detail = (
                    str(error)
                    if isinstance(error, MoldIdentityError)
                    else "模具资料保存冲突，请刷新后重试"
                )
                raise HTTPException(status_code=409, detail=detail) from error
            db.refresh(row)
            row = db.scalar(
                select(MoldTool)
                .options(
                    selectinload(MoldTool.customer_links).selectinload(
                        MoldToolCustomer.customer
                    ),
                    selectinload(MoldTool.products).selectinload(Product.customer),
                    selectinload(MoldTool.products).selectinload(Product.material),
                )
                .where(MoldTool.id == row.id)
            )
            result = _mold_tool_dict(row)
            result["idempotent_replay"] = False
            return result

        values = payload.model_dump(
            exclude={
                "customer_initials",
                "label_name",
                "chinese_short_name",
                "label_overrides",
                "customers",
                "expected_version",
                "idempotency_key",
                "expected_location_version",
                "location_idempotency_key",
                "physical_move_confirmed",
                "location_note",
            }
        )
        if payload.customer_initials:
            try:
                values["mold_code"], _parts = next_available_mold_code(
                    db,
                    mold_name=payload.mold_name,
                    customer_initials=payload.customer_initials,
                )
            except MoldIdentityError as error:
                raise HTTPException(status_code=422, detail=str(error)) from error
        elif not payload.mold_code:
            raise HTTPException(
                status_code=422,
                detail="模具名称生成编号所需的客户拼音缩写缺失",
            )
        row = MoldTool(
            **values,
            identity_status="legacy_unset",
            created_by=user.id,
            updated_by=user.id,
        )
        db.add(row)
        try:
            db.commit()
        except IntegrityError as error:
            db.rollback()
            raise HTTPException(status_code=409, detail="模具编号已存在") from error
    db.refresh(row)
    return _mold_tool_dict(row)


@router.put("/molds/{mold_id}")
def update_mold_tool(
    mold_id: int,
    payload: MoldToolPayload,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    row = db.scalar(
        select(MoldTool)
        .options(
            selectinload(MoldTool.customer_links).selectinload(
                MoldToolCustomer.customer
            ),
            selectinload(MoldTool.products).selectinload(Product.customer),
            selectinload(MoldTool.products).selectinload(Product.material),
        )
        .where(MoldTool.id == mold_id)
    )
    if row is None:
        raise HTTPException(status_code=404, detail="模具不存在")
    if row.archive_status == "archived":
        raise HTTPException(status_code=409, detail="封存模具不能直接编辑，请先按现场搬回后恢复启用")
    if payload.mold_code and payload.mold_code != row.mold_code:
        raise HTTPException(status_code=409, detail="模具编号生成后不可在档案编辑中修改")
    formal_identity = payload.customers is not None
    if row.identity_status == "frozen" and not formal_identity:
        raise HTTPException(
            status_code=409,
            detail="该模具已使用正式客户与标签资料，请刷新后按新表单保存",
        )
    before_identity = {
        "display_name": _mold_display_name(row, None),
        "label_name": row.label_name,
        "chinese_short_name": row.chinese_short_name,
        "label_overrides": parse_label_overrides(row.label_overrides_json),
        "customers": [
            {
                "customer_id": link.customer_id,
                "display_order": link.display_order,
            }
            for link in _visible_mold_customer_links(row, None)
        ],
        "version": row.version,
    }
    request_hash: str | None = None
    formal_customers: list[Customer] = []
    formal_display_name: str | None = None
    formal_chinese_short_name: str | None = None
    if formal_identity:
        if payload.expected_version is None:
            raise HTTPException(status_code=409, detail="模具资料版本缺失，请刷新后重试")
        request_hash = _mold_master_request_hash(
            action="update",
            mold_id=row.id,
            payload=payload,
        )
        replay = _mold_master_replay(
            db,
            action="update",
            idempotency_key=payload.idempotency_key or "",
            request_hash=request_hash,
            actor_id=user.id,
        )
        if replay is not None:
            result_payload = dict(replay)
            result_payload["idempotent_replay"] = True
            return result_payload
        (
            formal_display_name,
            formal_chinese_short_name,
            formal_customers,
        ) = _formal_mold_identity(payload, db)
        associated_customer_ids = {customer.id for customer in formal_customers}
        bound_customer_ids = {
            product.customer_id
            for product in row.products
        }
        missing_bound_customers = bound_customer_ids - associated_customer_ids
        if missing_bound_customers:
            raise HTTPException(
                status_code=409,
                detail="当前仍有常用箱绑定到被移除的客户，请先解除产品绑定后再调整客户关联",
            )
        claimed = db.execute(
            update(MoldTool)
            .where(
                MoldTool.id == row.id,
                MoldTool.version == payload.expected_version,
            )
            .values(version=MoldTool.version + 1)
            .execution_options(synchronize_session=False)
        )
        if claimed.rowcount != 1:
            db.rollback()
            raise HTTPException(status_code=409, detail="模具资料已变化，请刷新后重试")
        db.refresh(row)
    location_changed = payload.rack_location.strip() != row.rack_location.strip()
    result: MoldLocationMoveResult | None = None
    if location_changed:
        _assert_asset_location_operational(
            db,
            asset_kind="mold",
            location_text=payload.rack_location,
            claim_floor=True,
        )
        if not payload.physical_move_confirmed:
            raise HTTPException(status_code=409, detail="请先确认模具实物已经搬到新位置")
        if payload.expected_location_version is None:
            raise HTTPException(status_code=409, detail="模具位置版本缺失，请重新预览后再保存")
        if payload.location_idempotency_key is None:
            raise HTTPException(status_code=409, detail="模具移位凭证缺失，请重新预览后再保存")
        try:
            # SQLite releases a SAVEPOINT as a durable transaction when no
            # outer write has begun. Claim the mold row first so the movement,
            # metadata and audit remain one rollback-safe transaction. The
            # version predicate also makes this the concurrency gate.
            claimed = db.execute(
                update(MoldTool)
                .where(
                    MoldTool.id == row.id,
                    MoldTool.is_active.is_(True),
                    MoldTool.location_version == payload.expected_location_version,
                )
                .values(location_version=MoldTool.location_version)
                .execution_options(synchronize_session=False)
            )
            if claimed.rowcount != 1:
                raise MoldLocationError("模具位置版本已变化，请重新预览", status_code=409)
            db.refresh(row)
            preview_mold_location_move(
                db,
                mold_code=row.mold_code,
                target_location=payload.rack_location,
            )
            result = confirm_mold_location_move(
                db,
                mold_code=row.mold_code,
                target_location=payload.rack_location,
                expected_version=payload.expected_location_version,
                idempotency_key=payload.location_idempotency_key,
                actor_id=user.id,
                source="manual_input",
                note=payload.location_note,
            )
            row = result.mold
        except MoldLocationError as error:
            db.rollback()
            raise HTTPException(status_code=error.status_code, detail=str(error)) from error
    values = payload.model_dump(
        exclude={
            "customer_initials",
            "mold_code",
            "rack_location",
            "label_name",
            "chinese_short_name",
            "label_overrides",
            "customers",
            "expected_version",
            "idempotency_key",
            "expected_location_version",
            "location_idempotency_key",
            "physical_move_confirmed",
            "location_note",
        }
    )
    for key, value in values.items():
        setattr(row, key, value)
    if formal_identity:
        row.label_name = normalize_mold_label_name(payload.label_name)
        row.chinese_short_name = formal_chinese_short_name
        if payload.label_overrides is not None:
            row.label_overrides_json = canonical_label_overrides(
                payload.label_overrides.as_dict()
            )
        row.mold_name = formal_display_name or row.mold_name
        row.identity_status = "frozen"
        _replace_mold_customer_links(
            db,
            row=row,
            payload=payload,
            customers=formal_customers,
            actor_id=user.id,
        )
        append_audit_event(
            db,
            request=request,
            actor=user,
            event_category="business",
            result="success",
            source="web",
            module_code="warehouse",
            action_code="mold.master.update",
            legacy_action="UPDATE",
            resource=f"warehouse/molds/{row.id}",
            entity_type="mold_tool",
            entity_id=row.id,
            object_ref=row.mold_code,
            description="更新模具正式客户与标签资料",
            details={
                "identity_status": "frozen",
                "expected_version": payload.expected_version,
                "resulting_version": row.version,
                "before": before_identity,
                "after": {
                    "display_name": row.mold_name,
                    "label_name": row.label_name,
                    "chinese_short_name": row.chinese_short_name,
                    "label_overrides": parse_label_overrides(
                        row.label_overrides_json
                    ),
                    "customers": [
                        {
                            "customer_id": item.customer_id,
                            "display_order": item.display_order,
                        }
                        for item in (payload.customers or [])
                    ],
                    "version": row.version,
                },
                "idempotency_key": payload.idempotency_key,
            },
        )
    row.updated_by = user.id
    if result is not None:
        _append_mold_location_move_log(
            db,
            request=request,
            user=user,
            result=result,
            description="模具编辑确认位置移动",
        )
    if formal_identity:
        db.flush()
        db.add(
            MoldMasterMutation(
                mold_tool=row,
                action="update",
                idempotency_key=payload.idempotency_key or "",
                request_hash=request_hash or "",
                actor_id=user.id,
                result_version=row.version,
                result_snapshot_json=json.dumps(
                    _mold_tool_dict(row),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            )
        )
    try:
        db.commit()
    except IntegrityError as error:
        db.rollback()
        if formal_identity and request_hash is not None:
            replay = _mold_master_replay(
                db,
                action="update",
                idempotency_key=payload.idempotency_key or "",
                request_hash=request_hash,
                actor_id=user.id,
            )
            if replay is not None:
                result_payload = dict(replay)
                result_payload["idempotent_replay"] = True
                return result_payload
        raise HTTPException(status_code=409, detail="模具位置已变化或保存冲突，请重新预览") from error
    row = db.scalar(
        select(MoldTool)
        .options(
            selectinload(MoldTool.customer_links).selectinload(
                MoldToolCustomer.customer
            ),
            selectinload(MoldTool.products).selectinload(Product.customer),
            selectinload(MoldTool.products).selectinload(Product.material),
        )
        .where(MoldTool.id == row.id)
    )
    result_payload = _mold_tool_dict(row)
    if formal_identity:
        result_payload["idempotent_replay"] = False
    return result_payload


def _production_process_with_die_cut(value: str | None) -> str:
    tokens = [
        token.strip()
        for token in re.split(r"[,，、]+", str(value or ""))
        if token.strip()
    ]
    if "模切" not in tokens:
        tokens.append("模切")
    return "、".join(dict.fromkeys(tokens))


def _production_process_without_die_cut(value: str | None) -> str:
    tokens = [
        token.strip()
        for token in re.split(r"[,，、]+", str(value or ""))
        if token.strip() and token.strip() != "模切"
    ]
    return "、".join(dict.fromkeys(tokens))


@router.post("/molds/{mold_id}/product-bindings")
def bind_mold_products(
    mold_id: int,
    payload: MoldProductBindingPayload,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    mold = db.scalar(
        select(MoldTool)
        .options(
            selectinload(MoldTool.customer_links).selectinload(
                MoldToolCustomer.customer
            )
        )
        .where(MoldTool.id == mold_id)
    )
    if mold is None:
        raise HTTPException(status_code=404, detail="模具不存在")
    if not mold.is_active:
        raise HTTPException(status_code=409, detail="模具已停用，不能绑定常用箱")

    expected_versions = {
        item.product_id: item.expected_version for item in payload.items
    }
    products = db.scalars(
        select(Product)
        .where(
            Product.id.in_(sorted(expected_versions)),
            Product.is_active.is_(True),
            Product.deleted_at.is_(None),
        )
        .order_by(Product.id)
    ).all()
    products_by_id = {product.id: product for product in products}
    missing_ids = sorted(set(expected_versions) - set(products_by_id))
    if missing_ids:
        raise HTTPException(status_code=404, detail="所选常用箱不存在或已停用")
    if mold.identity_status == "frozen":
        associated_customer_ids = {
            link.customer_id for link in mold.customer_links
        }
        unexpected_customers = {
            product.customer_id for product in products
        } - associated_customer_ids
        if unexpected_customers:
            raise HTTPException(
                status_code=409,
                detail="所选常用箱客户尚未关联到该模具，请先保存模具适用客户",
            )

    conflicts = [
        product
        for product in products
        if product.mold_tool_id is not None and product.mold_tool_id != mold.id
    ]
    if conflicts:
        conflict = conflicts[0]
        current_mold = db.get(MoldTool, conflict.mold_tool_id)
        raise HTTPException(
            status_code=409,
            detail=(
                f"存货编码 {conflict.customer_material_code or conflict.product_code} "
                f"已绑定模具 {current_mold.mold_code if current_mold else conflict.mold_tool_id}；"
                "当前规则一个存货编码只能绑定一块模具"
            ),
        )

    bound_ids: list[int] = []
    try:
        for product in products:
            if product.mold_tool_id == mold.id:
                continue
            apply_versioned_update(
                db,
                object_type="product",
                entity=product,
                updates={
                    "mold_tool_id": mold.id,
                    "production_process": _production_process_with_die_cut(
                        product.production_process
                    ),
                },
                expected_version=expected_versions[product.id],
                user=user,
                reason="从模具档案反向绑定常用箱并启用模切工艺",
                source="api.warehouse.mold_product_bindings",
                action="mold_binding",
            )
            bound_ids.append(product.id)
        audit_master_change(
            db,
            user=user,
            action="BIND_PRODUCTS",
            resource="MOLD_TOOL",
            resource_id=mold.id,
            details={
                "mold_code": mold.mold_code,
                "product_ids": [product.id for product in products],
                "newly_bound_product_ids": bound_ids,
            },
        )
        db.commit()
    except Exception:
        db.rollback()
        raise

    mold = db.scalar(
        select(MoldTool)
        .options(
            selectinload(MoldTool.customer_links).selectinload(
                MoldToolCustomer.customer
            ),
            selectinload(MoldTool.products).selectinload(Product.customer),
        )
        .where(MoldTool.id == mold_id)
    )
    return {
        "message": "常用箱绑定成功",
        "bound_count": len(bound_ids),
        "mold": _mold_tool_dict(mold),
    }


@router.delete("/molds/{mold_id}/product-bindings/{product_id}")
def unbind_mold_product(
    mold_id: int,
    product_id: int,
    expected_version: int = Query(gt=0),
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    mold = db.get(MoldTool, mold_id)
    if mold is None:
        raise HTTPException(status_code=404, detail="模具不存在")
    product = db.scalar(
        select(Product).where(
            Product.id == product_id,
            Product.is_active.is_(True),
            Product.deleted_at.is_(None),
        )
    )
    if product is None:
        raise HTTPException(status_code=404, detail="常用箱不存在或已停用")
    if product.mold_tool_id != mold.id:
        raise HTTPException(status_code=409, detail="绑定关系已变化，请刷新后重新核对")

    try:
        apply_versioned_update(
            db,
            object_type="product",
            entity=product,
            updates={
                "mold_tool_id": None,
                "production_process": _production_process_without_die_cut(
                    product.production_process
                ),
            },
            expected_version=expected_version,
            user=user,
            reason="从模具档案明确解除常用箱绑定并移除模切工艺",
            source="api.warehouse.mold_product_unbindings",
            action="mold_unbinding",
        )
        audit_master_change(
            db,
            user=user,
            action="UNBIND_PRODUCT",
            resource="MOLD_TOOL",
            resource_id=mold.id,
            details={
                "mold_code": mold.mold_code,
                "product_id": product.id,
            },
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(product)
    mold = db.scalar(
        select(MoldTool)
        .options(selectinload(MoldTool.products).selectinload(Product.customer))
        .where(MoldTool.id == mold_id)
    )
    return {
        "message": "常用箱已解除模具绑定",
        "unbound_product": {
            "id": product.id,
            "version": product.version,
            "product_code": product.product_code,
            "customer_material_code": product.customer_material_code,
            "product_name": product.product_name,
            "production_process": product.production_process,
            "mold_tool_id": product.mold_tool_id,
        },
        "mold": _mold_tool_dict(mold),
    }


@router.put("/molds/{mold_id}/enable")
def enable_mold_tool(
    mold_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    row = db.get(MoldTool, mold_id)
    if row is None:
        raise HTTPException(status_code=404, detail="模具不存在")
    if row.archive_status == "archived":
        raise HTTPException(status_code=409, detail="封存模具不能普通启用，请先搬回一楼正式模具位并恢复")
    if row.is_active:
        return _mold_tool_dict(row)
    row.is_active = True
    row.updated_by = user.id
    append_audit_event(
        db,
        request=request,
        actor=user,
        event_category="business",
        result="success",
        source="web",
        module_code="warehouse",
        action_code="mold.legacy_disabled.restore",
        legacy_action="ENABLE",
        resource=f"warehouse/molds/{row.id}/enable",
        entity_type="mold_tool",
        entity_id=row.id,
        object_ref=row.mold_code,
        description="历史普通停用模具恢复使用",
        details={
            "mold_code": row.mold_code,
            "location_changed": False,
            "note": "兼容恢复历史普通停用事实，不生成位置搬运事实",
        },
    )
    db.commit()
    return _mold_tool_dict(row)


@router.put("/molds/{mold_id}/disable")
def disable_mold_tool(
    mold_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    row = db.get(MoldTool, mold_id)
    if row is None:
        raise HTTPException(status_code=404, detail="模具不存在")
    raise HTTPException(
        status_code=409,
        detail="普通停用已合并，请完成现场搬运后使用封存待复用",
    )


@router.get("/references/template-locations")
def search_template_locations(
    q: str | None = None,
    limit: int = Query(default=100, ge=1, le=200),
    db: Session = Depends(get_db),
    _user: User = Depends(can_read),
) -> dict:
    keyword = (q or "").strip()
    if not keyword:
        return {"items": [], "message": "请输入存货编码、产品名称或模板位置"}
    pattern = f"%{keyword}%"
    rows = db.execute(
        select(Product, Customer, MoldTool)
        .join(Customer, Customer.id == Product.customer_id)
        .outerjoin(MoldTool, MoldTool.id == Product.mold_tool_id)
        .where(
            Product.is_active.is_(True),
            Product.deleted_at.is_(None),
            or_(
                MoldTool.id.is_not(None),
                and_(
                    Product.die_cut_path.is_not(None),
                    func.trim(Product.die_cut_path) != "",
                ),
            ),
            or_(
                Product.product_code.like(pattern),
                Product.customer_material_code.like(pattern),
                Product.product_name.like(pattern),
                Product.die_cut_path.like(pattern),
                MoldTool.mold_code.like(pattern),
                MoldTool.mold_name.like(pattern),
                MoldTool.rack_location.like(pattern),
                MoldTool.label_name.like(pattern),
                MoldTool.chinese_short_name.like(pattern),
                Customer.name.like(pattern),
                Customer.chinese_short_name.like(pattern),
                Customer.customer_code.like(pattern),
            ),
        )
        .order_by(Customer.name, Product.product_code, Product.id)
        .limit(limit)
    ).all()
    return {
        "items": [
            {
                "product_id": product.id,
                "customer_id": customer.id,
                "customer_name": customer.name,
                "product_code": product.product_code,
                "customer_material_code": product.customer_material_code,
                "product_name": product.product_name,
                "mold_tool_id": mold_tool.id if mold_tool else None,
                "mold_code": mold_tool.mold_code if mold_tool else None,
                "mold_name": mold_tool.mold_name if mold_tool else None,
                "specification": product_dimension_specification(product),
                "template_location": (
                    mold_tool.rack_location if mold_tool else product.die_cut_path
                ),
            }
            for product, customer, mold_tool in rows
        ]
    }


@router.post("/locations")
def create_location(
    payload: LocationPayload,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    _claim_floor_numbers_for_layout_write(db, payload.warehouse_floor)
    if location_alias_conflict(db, payload.location_code):
        raise HTTPException(
            status_code=409,
            detail="该库位编码是其他稳定位置的永久旧码，禁止复用。",
        )
    _require_registered_area(
        db,
        floor_number=payload.warehouse_floor,
        area_code=payload.area_code,
    )
    try:
        values = payload.model_dump()
        values["placement_status"] = "unplaced"
        values["is_temporary"] = payload.storage_type == "temporary_aisle"
        values["source_version"] = None
        row = WarehouseLocation(**values)
        db.add(row)
        db.commit()
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(status_code=409, detail="库位编码已存在") from error
    return _location_dict_for_db(db, row)


@router.put("/locations/{location_id}")
def update_location(
    location_id: int,
    payload: LocationPayload,
    db: Session = Depends(get_db),
    _user: User = Depends(admin_only),
) -> dict:
    row = db.get(WarehouseLocation, location_id)
    if row is None:
        raise HTTPException(status_code=404, detail="库位不存在")
    _claim_floor_numbers_for_layout_write(
        db,
        row.warehouse_floor,
        payload.warehouse_floor,
    )
    db.refresh(row)
    _require_location_source_not_archived(db, row)
    if row.address_kind != "legacy" and any(
        (
            row.location_code != payload.location_code,
            row.warehouse_floor != payload.warehouse_floor,
            (row.area_code or None) != payload.area_code,
            (row.storage_type or None) != payload.storage_type,
        )
    ):
        raise HTTPException(
            status_code=409,
            detail="该位置已启用结构化地址，请通过地址治理预览和一次确认修改。",
        )
    if location_alias_conflict(
        db, payload.location_code, location_id=location_id
    ):
        raise HTTPException(
            status_code=409,
            detail="该库位编码是其他稳定位置的永久旧码，禁止复用。",
        )
    _reject_v11_location_configuration(row)
    _require_registered_area(
        db,
        floor_number=payload.warehouse_floor,
        area_code=payload.area_code,
    )
    values = payload.model_dump()
    spatial_changed = any(
        (
            row.warehouse_floor != payload.warehouse_floor,
            (row.area_code or None) != payload.area_code,
            (row.storage_type or None) != payload.storage_type,
        )
    )
    values["placement_status"] = (
        "unplaced" if spatial_changed else (row.placement_status or "placed")
    )
    values["is_temporary"] = payload.storage_type == "temporary_aisle"
    if values["placement_status"] == "unplaced":
        has_lot = db.scalar(
            select(InventoryLot.id)
            .where(
                InventoryLot.warehouse_location_id == row.id,
                InventoryLot.status.in_(("active", "frozen")),
            )
            .limit(1)
        )
        has_pallet = db.scalar(
            select(InventoryPallet.id)
            .where(
                InventoryPallet.location_id == row.id,
                InventoryPallet.is_current.is_(True),
            )
            .limit(1)
        )
        if has_lot is not None or has_pallet is not None:
            raise HTTPException(
                status_code=409,
                detail="该库位仍有库存或当前栈板，不能改为未放置。",
            )
    for key, value in values.items():
        setattr(row, key, value)
    try:
        db.commit()
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(status_code=409, detail="库位编码已存在") from error
    return _location_dict_for_db(db, row)


@router.put("/locations/{location_id}/enable")
def enable_location(
    location_id: int,
    db: Session = Depends(get_db),
    _user: User = Depends(admin_only),
) -> dict:
    row = db.get(WarehouseLocation, location_id)
    if row is None:
        raise HTTPException(status_code=404, detail="库位不存在")
    _claim_floor_numbers_for_layout_write(db, row.warehouse_floor)
    db.refresh(row)
    _require_location_source_not_archived(db, row)
    _reject_v11_location_configuration(row)
    row.is_active = True
    db.commit()
    return _location_dict_for_db(db, row)


@router.put("/locations/{location_id}/disable")
def disable_location(
    location_id: int,
    db: Session = Depends(get_db),
    _user: User = Depends(admin_only),
) -> dict:
    row = db.get(WarehouseLocation, location_id)
    if row is None:
        raise HTTPException(status_code=404, detail="库位不存在")
    _claim_floor_numbers_for_layout_write(db, row.warehouse_floor)
    db.refresh(row)
    _require_location_source_not_archived(db, row)
    _reject_v11_location_configuration(row)
    row.is_active = False
    db.commit()
    return _location_dict_for_db(db, row)


@router.get("/lots")
def list_lots(
    inventory_type: str | None = None,
    status: str | None = None,
    statuses: list[str] | None = Query(default=None),
    customer_ids: list[int] | None = Query(default=None),
    finished_product_code: str | None = None,
    finished_product_name: str | None = None,
    finished_spec: str | None = None,
    semi_supplier: str | None = None,
    semi_material_code: str | None = None,
    semi_flute_type: str | None = None,
    semi_board_length_mm: int | None = Query(default=None, gt=0),
    semi_board_width_mm: int | None = Query(default=None, gt=0),
    semi_allowed_product: str | None = None,
    location_keyword: str | None = None,
    pallet_keyword: str | None = None,
    location_id: int | None = None,
    warehouse_floor: int | None = Query(default=None, ge=1, le=99),
    area_code: str | None = None,
    keyword: str | None = None,
    stale_level: str | None = None,
    sort: str = "last_movement_at_desc",
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    query = _lot_query(require_formal_location=False)
    visible_customer_ids = _visible_customer_ids(user, db)
    if visible_customer_ids is not None:
        query = query.where(_visible_lot_condition(visible_customer_ids))

    requested_customer_ids = {int(value) for value in (customer_ids or [])}
    if requested_customer_ids:
        for requested_customer_id in requested_customer_ids:
            require_customer_access(requested_customer_id, user, db)
        finished_customer_lot_ids = select(
            FinishedGoodsInventoryDetail.inventory_lot_id
        ).where(
            FinishedGoodsInventoryDetail.owner_customer_id.in_(requested_customer_ids)
        )
        semi_customer_lot_ids = select(
            SemiFinishedInventoryDetail.inventory_lot_id
        ).where(
            SemiFinishedInventoryDetail.owner_customer_id.in_(requested_customer_ids)
        )
        query = query.where(
            or_(
                InventoryLot.id.in_(finished_customer_lot_ids),
                InventoryLot.id.in_(semi_customer_lot_ids),
            )
        )
    if inventory_type:
        query = query.where(InventoryLot.inventory_type == inventory_type)
    normalized_statuses = [
        value.strip() for value in (statuses or []) if value and value.strip()
    ]
    if normalized_statuses:
        query = query.where(InventoryLot.status.in_(normalized_statuses))
    elif status:
        query = query.where(InventoryLot.status == status)
    if location_id:
        query = query.where(InventoryLot.warehouse_location_id == location_id)
    elif warehouse_floor is not None or area_code:
        location_ids = select(WarehouseLocation.id)
        if warehouse_floor is not None:
            location_ids = location_ids.where(
                WarehouseLocation.warehouse_floor == warehouse_floor
            )
        if area_code:
            location_ids = location_ids.where(
                func.upper(WarehouseLocation.area_code)
                == area_code.strip().upper()
            )
        query = query.where(InventoryLot.warehouse_location_id.in_(location_ids))

    def _contains_ci(column, value: str | None):
        normalized = (value or "").strip().lower()
        return func.lower(column).like(f"%{normalized}%") if normalized else None

    finished_conditions = [
        condition
        for condition in (
            _contains_ci(
                FinishedGoodsInventoryDetail.inventory_code_snapshot,
                finished_product_code,
            ),
            _contains_ci(
                FinishedGoodsInventoryDetail.product_name_snapshot,
                finished_product_name,
            ),
        )
        if condition is not None
    ]
    if finished_spec and finished_spec.strip():
        normalized_spec = (
            finished_spec.strip().lower().replace("x", "×").replace(" ", "")
        )
        spec_parts = normalized_spec.split("×")
        try:
            dimensions = [int(part) for part in spec_parts]
        except ValueError as error:
            raise HTTPException(
                status_code=422,
                detail="成品规格请按 长×宽 或 长×宽×高（毫米）填写",
            ) from error
        if len(dimensions) not in {2, 3} or min(dimensions) <= 0:
            raise HTTPException(
                status_code=422,
                detail="成品规格请按 长×宽 或 长×宽×高（毫米）填写",
            )
        finished_conditions.extend(
            (
                FinishedGoodsInventoryDetail.length_mm == dimensions[0],
                FinishedGoodsInventoryDetail.width_mm == dimensions[1],
            )
        )
        if len(dimensions) == 3:
            finished_conditions.append(
                FinishedGoodsInventoryDetail.height_mm == dimensions[2]
            )
    if finished_conditions:
        query = query.where(
            InventoryLot.id.in_(
                select(FinishedGoodsInventoryDetail.inventory_lot_id).where(
                    *finished_conditions
                )
            )
        )

    semi_conditions = [
        condition
        for condition in (
            _contains_ci(SemiFinishedInventoryDetail.supplier_name, semi_supplier),
            _contains_ci(
                SemiFinishedInventoryDetail.material_code_snapshot,
                semi_material_code,
            ),
            _contains_ci(SemiFinishedInventoryDetail.flute_type, semi_flute_type),
            (
                SemiFinishedInventoryDetail.board_length_mm == semi_board_length_mm
                if semi_board_length_mm is not None
                else None
            ),
            (
                SemiFinishedInventoryDetail.board_width_mm == semi_board_width_mm
                if semi_board_width_mm is not None
                else None
            ),
        )
        if condition is not None
    ]
    if semi_conditions:
        query = query.where(
            InventoryLot.id.in_(
                select(SemiFinishedInventoryDetail.inventory_lot_id).where(
                    *semi_conditions
                )
            )
        )
    allowed_product_condition = _contains_ci(
        Product.product_code, semi_allowed_product
    )
    if allowed_product_condition is not None:
        query = query.where(
            InventoryLot.id.in_(
                select(SemiFinishedLotAllowedProduct.inventory_lot_id)
                .join(
                    Product,
                    Product.id == SemiFinishedLotAllowedProduct.product_id,
                )
                .where(allowed_product_condition)
            )
        )

    location_condition = _contains_ci(WarehouseLocation.location_code, location_keyword)
    location_name_condition = _contains_ci(
        WarehouseLocation.location_name, location_keyword
    )
    if location_condition is not None and location_name_condition is not None:
        query = query.where(
            InventoryLot.warehouse_location_id.in_(
                select(WarehouseLocation.id).where(
                    or_(location_condition, location_name_condition)
                )
            )
        )
    pallet_condition = _contains_ci(InventoryPallet.pallet_code, pallet_keyword)
    if pallet_condition is not None:
        query = query.where(
            InventoryLot.id.in_(
                select(InventoryPalletItem.inventory_lot_id)
                .join(InventoryPallet, InventoryPallet.id == InventoryPalletItem.pallet_id)
                .where(
                    InventoryPalletItem.inventory_lot_id.is_not(None),
                    InventoryPallet.is_current.is_(True),
                    pallet_condition,
                )
            )
        )
    if keyword:
        text = keyword.strip()
        pattern = f"%{text}%"
        location_ids = select(WarehouseLocation.id).where(
            or_(
                WarehouseLocation.location_code.like(pattern),
                WarehouseLocation.location_name.like(pattern),
            )
        )
        finished_lot_ids = select(FinishedGoodsInventoryDetail.inventory_lot_id).where(
            or_(
                FinishedGoodsInventoryDetail.inventory_code_snapshot.like(pattern),
                FinishedGoodsInventoryDetail.product_name_snapshot.like(pattern),
                FinishedGoodsInventoryDetail.owner_customer_name_snapshot.like(pattern),
            )
        )
        semi_lot_ids = select(SemiFinishedInventoryDetail.inventory_lot_id).where(
            or_(
                SemiFinishedInventoryDetail.material_code_snapshot.like(pattern),
                SemiFinishedInventoryDetail.owner_customer_name_snapshot.like(pattern),
                SemiFinishedInventoryDetail.supplier_name.like(pattern),
                SemiFinishedInventoryDetail.cutting_note.like(pattern),
                SemiFinishedInventoryDetail.internal_name.like(pattern),
            )
        )
        allowed_product_lot_ids = (
            select(SemiFinishedLotAllowedProduct.inventory_lot_id)
            .join(
                Product,
                Product.id == SemiFinishedLotAllowedProduct.product_id,
            )
            .where(
                or_(
                    Product.product_code.like(pattern),
                    Product.customer_material_code.like(pattern),
                    Product.product_name.like(pattern),
                )
            )
        )
        query = query.where(
            or_(
                InventoryLot.lot_number.like(pattern),
                InventoryLot.warehouse_location_id.in_(location_ids),
                InventoryLot.id.in_(finished_lot_ids),
                InventoryLot.id.in_(semi_lot_ids),
                InventoryLot.id.in_(allowed_product_lot_ids),
            )
        )
    if stale_level:
        days = {"attention": 365, "handling": 548, "cleanup": 730}.get(stale_level)
        if stale_level == "unknown":
            query = query.where(InventoryLot.stock_date_accuracy == "unknown")
        elif days:
            query = query.where(
                InventoryLot.stock_date_accuracy != "unknown",
                InventoryLot.stock_date <= beijing_today() - timedelta(days=days)
            )
    count_query = select(func.count()).select_from(query.order_by(None).subquery())
    total = db.scalar(count_query) or 0
    sort_orders = {
        "last_movement_at_desc": (
            InventoryLot.last_movement_at.desc(),
            InventoryLot.id.desc(),
        ),
        "created_at_desc": (InventoryLot.created_at.desc(), InventoryLot.id.desc()),
    }
    if sort not in sort_orders:
        raise HTTPException(status_code=422, detail="库存排序方式无效")
    rows = db.scalars(
        query.order_by(*sort_orders[sort])
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    time_archives = build_inventory_lot_time_archives(db, rows)
    projection_contexts = load_warehouse_location_projection_contexts(
        db,
        [row.location for row in rows if row.location is not None],
    )
    return {
        "items": [
            _lot_dict(
                row,
                time_archive=time_archives.get(row.id),
                location_projection_context=(
                    projection_contexts.get(int(row.warehouse_location_id))
                    if row.warehouse_location_id is not None
                    else None
                ),
            )
            for row in rows
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
        "sort": sort,
        "filters": {
            "inventory_type": inventory_type,
            "customer_ids": sorted(requested_customer_ids),
            "statuses": normalized_statuses or ([status] if status else []),
            "finished_product_code": (finished_product_code or "").strip() or None,
            "finished_product_name": (finished_product_name or "").strip() or None,
            "finished_spec": (finished_spec or "").strip() or None,
            "semi_supplier": (semi_supplier or "").strip() or None,
            "semi_material_code": (semi_material_code or "").strip() or None,
            "semi_flute_type": (semi_flute_type or "").strip() or None,
            "semi_board_length_mm": semi_board_length_mm,
            "semi_board_width_mm": semi_board_width_mm,
            "semi_allowed_product": (semi_allowed_product or "").strip() or None,
            "location_keyword": (location_keyword or "").strip() or None,
            "pallet_keyword": (pallet_keyword or "").strip() or None,
            "location_id": location_id,
            "warehouse_floor": warehouse_floor,
            "area_code": (area_code or "").strip().upper() or None,
            "keyword": (keyword or "").strip() or None,
            "stale_level": stale_level,
        },
    }


_INSIGHT_OPERATIONAL_TOP_FIELDS = frozenset(
    {"generated_at", "as_of", "scope_notice", "recommendation_notice"}
)
_INSIGHT_OPERATIONAL_SUMMARY_FIELDS = frozenset(
    {
        "recorded_lots",
        "available_lots",
        "finished_available",
        "semi_finished_available",
        "total_reserved",
        "total_damaged",
        "total_scrapped",
    }
)
_INSIGHT_OPERATIONAL_QUALITY_FIELDS = frozenset(
    {
        "active_location_lots",
        "exact_stock_date_lots",
        "estimated_stock_date_lots",
        "unknown_stock_date_lots",
    }
)
_INSIGHT_OPERATIONAL_TYPE_FIELDS = frozenset(
    {"lots", "available", "reserved", "damaged", "scrapped"}
)
_INSIGHT_OPERATIONAL_AGE_FIELDS = frozenset(
    {"key", "label", "lots", "finished_available", "semi_finished_available"}
)
_INSIGHT_OPERATIONAL_ACTION_FIELDS = frozenset(
    {
        "priority",
        "lot_id",
        "lot_number",
        "inventory_type",
        "status",
        "location_code",
        "location_name",
        "quantity_available",
        "unit",
        "age_days",
        "age_basis",
        "last_movement_at",
        "movement_stagnant_days",
        "covered_demand_quantity",
        "uncovered_demand_quantity",
        "coverage_percent",
        "coverage_basis",
    }
)
_INSIGHT_OPERATIONAL_DETAIL_FIELDS = frozenset(
    {
        "customer_name",
        "inventory_code",
        "name",
        "assigned_product_count",
        "binding_scope",
        "deduction_eligibility",
        "candidate_relationship_read_only",
    }
)
_INSIGHT_OPERATIONAL_PRODUCT_FIELDS = frozenset(
    {"product_id", "inventory_code", "name"}
)
_INSIGHT_OPERATIONAL_DEMAND_FIELDS = frozenset(
    {"demand_30", "demand_90", "demand_180", "open_demand"}
)
_INSIGHT_OPERATIONAL_REASON_CODES = frozenset(
    {
        "finished_stock_can_cover_order",
        "finished_stock_exceeds_open_demand",
        "no_demand_180",
        "semi_stock_may_cover_demand",
        "semi_product_assignment_missing",
        "age_cleanup",
        "age_handling",
        "age_attention",
        "age_slow",
        "stock_date_unknown",
        "stock_date_estimated",
        "location_unavailable",
    }
)


def _insight_allowed_fields(value: object, fields: frozenset[str]) -> dict:
    if not isinstance(value, dict):
        return {}
    return {key: value[key] for key in fields if key in value}


def _redact_inventory_insight_costs(insights: dict) -> dict:
    """Return an operational-only warehouse insight response.

    ``cost.view`` is a data boundary, not merely a display preference.  Keep
    stock age, quantities, locations and demand suggestions, while dropping
    all monetary values, cost sources and cost-completeness metadata before
    the response serializer sees them.
    """

    source = insights if isinstance(insights, dict) else {}
    result = _insight_allowed_fields(source, _INSIGHT_OPERATIONAL_TOP_FIELDS)
    result["summary"] = _insight_allowed_fields(
        source.get("summary"), _INSIGHT_OPERATIONAL_SUMMARY_FIELDS
    )
    result["data_quality"] = _insight_allowed_fields(
        source.get("data_quality"), _INSIGHT_OPERATIONAL_QUALITY_FIELDS
    )
    source_by_type = source.get("by_type")
    result["by_type"] = {
        inventory_type: _insight_allowed_fields(
            source_by_type.get(inventory_type), _INSIGHT_OPERATIONAL_TYPE_FIELDS
        )
        for inventory_type in ("finished", "semi_finished")
        if isinstance(source_by_type, dict)
        and isinstance(source_by_type.get(inventory_type), dict)
    }
    source_age_buckets = source.get("age_buckets")
    result["age_buckets"] = [
        _insight_allowed_fields(bucket, _INSIGHT_OPERATIONAL_AGE_FIELDS)
        for bucket in source_age_buckets
        if isinstance(bucket, dict)
    ] if isinstance(source_age_buckets, list) else []

    action_items: list[dict] = []
    source_action_items = source.get("action_items")
    if not isinstance(source_action_items, list):
        source_action_items = []
    for source_item in source_action_items:
        if not isinstance(source_item, dict):
            continue
        reasons: list[dict] = []
        source_reasons = source_item.get("reasons")
        if isinstance(source_reasons, list):
            for reason in source_reasons:
                if not isinstance(reason, dict):
                    continue
                code = reason.get("code")
                if code not in _INSIGHT_OPERATIONAL_REASON_CODES:
                    continue
                safe_reason = {"code": code}
                if isinstance(reason.get("text"), str):
                    safe_reason["text"] = reason["text"]
                reasons.append(safe_reason)
        # A row whose only purpose was cost completion is not an operational
        # action for users who cannot view costs.
        if not reasons:
            continue
        item = _insight_allowed_fields(
            source_item, _INSIGHT_OPERATIONAL_ACTION_FIELDS
        )
        detail = _insight_allowed_fields(
            source_item.get("detail"), _INSIGHT_OPERATIONAL_DETAIL_FIELDS
        )
        source_products = (
            source_item.get("detail", {}).get("assigned_products")
            if isinstance(source_item.get("detail"), dict)
            else None
        )
        if isinstance(source_products, list):
            detail["assigned_products"] = [
                _insight_allowed_fields(
                    product, _INSIGHT_OPERATIONAL_PRODUCT_FIELDS
                )
                for product in source_products
                if isinstance(product, dict)
            ]
        item["detail"] = detail
        item["demand"] = _insight_allowed_fields(
            source_item.get("demand"), _INSIGHT_OPERATIONAL_DEMAND_FIELDS
        )
        item["reasons"] = reasons
        action_items.append(item)

    result["action_items"] = action_items
    result["action_item_count"] = len(action_items)
    result["high_priority_action_item_count"] = sum(
        1
        for item in action_items
        if isinstance(item.get("priority"), (int, float))
        and item["priority"] <= 1
    )
    return result


@router.get("/costs")
def get_inventory_costs(response: Response, location_id: int | None = Query(default=None, ge=1),
                        page: int = Query(default=1, ge=1), page_size: int = Query(default=50, ge=1, le=200),
                        keyword: str = Query(default="", max_length=150), summary_only: bool = Query(default=False),
                        db: Session = Depends(get_db), user: User = Depends(can_read)) -> dict:
    from app.services.inventory_valuation import can_view_inventory_cost
    from app.services.inventory_cost_listing import read_inventory_cost_page
    if not can_view_inventory_cost(user):
        raise HTTPException(403, "仅管理员和老板可以查看成本")
    response.headers["Cache-Control"] = "private, no-store, max-age=0"
    query = select(InventoryLot.id).where(
        InventoryLot.quantity_available + InventoryLot.quantity_reserved + InventoryLot.quantity_damaged > 0)
    scope = _visible_customer_ids(user, db)
    if scope is not None:
        query = query.where(_visible_lot_condition(scope))
    if location_id is not None:
        query = query.where(InventoryLot.warehouse_location_id == location_id)
    return read_inventory_cost_page(db, query, page=page, page_size=page_size,
                                    keyword=keyword, summary_only=summary_only)


@router.get("/insights")
def get_inventory_insights(
    as_of: date | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    visible_customer_ids = _visible_customer_ids(user, db)
    insights = build_inventory_insights(
        db,
        as_of=as_of,
        customer_ids=visible_customer_ids,
    )
    from app.services.inventory_valuation import can_view_inventory_cost
    if can_view_inventory_cost(user):
        return insights
    return _redact_inventory_insight_costs(insights)


@router.get("/lots/{lot_id}")
def get_lot(
    lot_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    row = _require_lot_customer_access(db, lot_id, user)
    include_sensitive_details = has_unrestricted_customer_access(user, db)
    timeline = build_inventory_lot_detail_timeline(
        db,
        row,
        include_sensitive_details=include_sensitive_details,
    )
    archive = build_inventory_lot_time_archives(db, [row]).get(row.id) or {}
    location_events = [
        event
        for event in timeline
        if event.get("event_type")
        in {
            "location_transfer",
            "pallet_location_move",
            "inventory_location_transfer",
        }
        or event.get("basis") == "inferred_from_finished_lot_edit_audit"
    ]
    stocktake_events = [
        event
        for event in timeline
        if event.get("event_type") in {"stocktake_submitted", "stocktake_approved"}
    ]
    if location_events:
        archive["latest_location_transfer_at"] = location_events[0]["occurred_at"]
        current_entry_events = [
            event
            for event in location_events
            if event.get("direction") in {"in", "move"}
            or event.get("event_type") == "pallet_location_move"
            or event.get("basis") == "inferred_from_finished_lot_edit_audit"
        ]
        if current_entry_events:
            archive["entered_current_location_at"] = current_entry_events[0][
                "occurred_at"
            ]
            archive["entered_current_location_basis"] = current_entry_events[0][
                "basis"
            ]
    if stocktake_events:
        archive["latest_stocktake_at"] = stocktake_events[0]["occurred_at"]
    projection_context = load_warehouse_location_projection_contexts(
        db, [row.location]
    ).get(int(row.warehouse_location_id or 0), {})
    result = _lot_dict(
        row,
        time_archive=archive,
        location_projection_context=projection_context,
    )
    result["timeline"] = timeline
    result["movements"] = [
        _movement_dict(
            item,
            include_sensitive_details=include_sensitive_details,
        )
        for item in db.scalars(
            select(InventoryMovement)
            .options(selectinload(InventoryMovement.lot))
            .where(InventoryMovement.inventory_lot_id == lot_id)
            .order_by(InventoryMovement.id.desc())
        ).all()
    ]
    reservation_query = select(InventoryReservation).where(
        InventoryReservation.inventory_lot_id == lot_id
    )
    visible_customer_ids = _visible_customer_ids(user, db)
    if visible_customer_ids is not None:
        reservation_query = reservation_query.outerjoin(
            Order, Order.id == InventoryReservation.order_id
        ).where(
            or_(
                InventoryReservation.order_id.is_(None),
                Order.customer_id.in_(visible_customer_ids),
            )
        )
    result["reservations"] = [
        _reservation_dict(
            item,
            db,
            include_sensitive_details=include_sensitive_details,
        )
        for item in db.scalars(
            reservation_query.order_by(InventoryReservation.id.desc())
        ).all()
    ]
    from app.services.shelf_lot_history import shelf_delivery_history, shelf_related_inventory
    result["shelf_deliveries"] = shelf_delivery_history(db, lot_id, visible_customer_ids)
    result["shelf_related_inventory"] = shelf_related_inventory(db, row, visible_customer_ids)
    from app.services.inventory_valuation import can_view_inventory_cost, cost_payload
    if can_view_inventory_cost(user):
        result["cost"] = cost_payload(row, db)
    return result


@router.get("/lots/{lot_id}/label")
def get_finished_goods_label(
    lot_id: int,
    request: Request,
    expected_version: int = Query(gt=0),
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    """Return a read-only, version-bound finished-goods label projection."""

    row = _require_lot_customer_access(db, lot_id, user)
    if row.inventory_type != "finished" or row.finished_detail is None:
        raise HTTPException(status_code=409, detail="只有正式成品库存可以打印货物标签")
    if int(row.version) != expected_version:
        raise HTTPException(
            status_code=409,
            detail="旧标签已失效：库存数量或位置已经变化，请从当前库存重新打印",
        )
    physical_quantity = int(row.quantity_available or 0) + int(
        row.quantity_reserved or 0
    )
    if row.status == "closed" or physical_quantity <= 0:
        raise HTTPException(status_code=409, detail="当前批次已无在库实物，不能打印货物标签")
    location = row.location
    if location is None:
        raise HTTPException(
            status_code=409,
            detail="当前批次尚未绑定正式位置，不能打印货物标签",
        )
    if row.status == "frozen":
        label_status = "异常待确认"
    elif (
        location.storage_type == "staging"
        or location.location_code == "F1-DISPATCH-01"
    ):
        label_status = "待送"
    elif location.storage_type == "sample":
        label_status = "样品"
    else:
        label_status = "成品"
    source_labels = {
        "production_completion": "生产完工",
        "production_surplus": "生产余货",
        "manual": "手工入库",
        "stocktake": "盘点入库",
        "transfer": "移库转入",
        "delivery_return": "送货退回",
    }
    lookup_url = lot_mobile_url(row.id, origin=load_settings().browser_url)
    projection_context = load_warehouse_location_projection_contexts(
        db, [row.location]
    ).get(int(row.warehouse_location_id or 0), {})
    return {
        **_lot_dict(
            row,
            location_projection_context=projection_context,
        ),
        "label_version": row.version,
        "label_status": label_status,
        "physical_quantity": physical_quantity,
        "source_reference": {
            "type": row.source_ref_type or row.source_type,
            "id": row.source_ref_id,
            "label": source_labels.get(row.source_type, "库存来源"),
        },
        "lookup_url": lookup_url,
        "qr_data_url": qr_data_url(lookup_url),
    }


@router.get("/movements")
def list_movements(
    lot_number: str | None = None,
    inventory_type: str | None = None,
    movement_type: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    query = (
        select(InventoryMovement)
        .join(InventoryLot)
        .join(
            WarehouseLocation,
            WarehouseLocation.id == InventoryLot.warehouse_location_id,
        )
        .where(_formal_inventory_location_condition())
        .options(selectinload(InventoryMovement.lot))
    )
    visible_customer_ids = _visible_customer_ids(user, db)
    if visible_customer_ids is not None:
        query = query.where(_visible_lot_condition(visible_customer_ids))
    if lot_number:
        query = query.where(InventoryLot.lot_number.contains(lot_number.strip()))
    if inventory_type:
        query = query.where(InventoryLot.inventory_type == inventory_type)
    if movement_type:
        query = query.where(InventoryMovement.movement_type == movement_type)
    if date_from:
        start_at, _ = beijing_date_bounds_utc_naive(date_from)
        query = query.where(InventoryMovement.created_at >= start_at)
    if date_to:
        _, end_at = beijing_date_bounds_utc_naive(date_to)
        query = query.where(InventoryMovement.created_at < end_at)
    total = db.scalar(select(func.count()).select_from(query.order_by(None).subquery())) or 0
    rows = db.scalars(
        query.order_by(InventoryMovement.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return {"items": [_movement_dict(row) for row in rows], "total": total}


@router.post("/finished/manual-in")
def finished_manual_in(
    payload: FinishedManualInPayload,
    request: Request = None,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    require_customer_access(payload.customer_id, user, db)
    try:
        replayed = _inventory_operation_replayed(db, payload.idempotency_key)
        row = manual_finished_in(db, operator_id=user.id, **payload.model_dump())
        if not replayed:
            _append_inventory_lot_audit(
                db, request=request, user=user,
                action_code="warehouse.finished.manual_in", row=row,
                before=None, reason=payload.remarks,
                idempotency_key=payload.idempotency_key,
            )
        db.commit()
        return _lot_dict_for_db(db, row)
    except WarehouseInventoryError as error:
        db.rollback()
        _handle(error)
    except Exception:
        db.rollback()
        raise


@router.post("/semi-finished/manual-in")
def semi_finished_manual_in(
    payload: SemiFinishedManualInPayload,
    request: Request = None,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    if payload.customer_id is None and not has_unrestricted_customer_access(user, db):
        raise HTTPException(status_code=403, detail="受限账号不能创建无客户库存")
    if payload.customer_id is not None:
        require_customer_access(payload.customer_id, user, db)
    _reject_floor3_for_semi_finished_inventory(db, payload.location_id)
    try:
        replayed = _inventory_operation_replayed(db, payload.idempotency_key)
        row = manual_semi_finished_in(db, operator_id=user.id, **payload.model_dump())
        if not replayed:
            _append_inventory_lot_audit(
                db, request=request, user=user,
                action_code="warehouse.semi_finished.manual_in", row=row,
                before=None, reason=payload.remarks,
                idempotency_key=payload.idempotency_key,
            )
        db.commit()
        return _lot_dict_for_db(db, row)
    except WarehouseInventoryError as error:
        db.rollback()
        _handle(error)
    except Exception:
        db.rollback()
        raise


@router.post("/lots/{lot_id}/edit-semi-finished")
def edit_semi_finished_inventory_lot(
    lot_id: int, payload: SemiFinishedLotEditPayload,
    db: Session = Depends(get_db), user: User = Depends(admin_only),
) -> dict:
    _require_lot_customer_access(db, lot_id, user)
    if payload.customer_id is not None:
        require_customer_access(payload.customer_id, user, db)
    try:
        row = edit_semi_finished_lot_customer(
            db, lot_id=lot_id, customer_id=payload.customer_id,
            expected_version=payload.expected_version, operator_id=user.id,
        )
        db.commit()
        return _lot_dict_for_db(db, row)
    except WarehouseInventoryError as error:
        db.rollback()
        _handle(error)
    except IntegrityError as error:
        db.rollback()
        _handle_integrity(error)


@router.post("/lots/{lot_id}/void-semi-finished")
def void_semi_finished_inventory_lot(
    lot_id: int, payload: SemiFinishedLotVoidPayload,
    db: Session = Depends(get_db), user: User = Depends(admin_only),
) -> dict:
    _require_lot_customer_access(db, lot_id, user)
    try:
        row = void_semi_finished_lot(
            db, lot_id=lot_id, expected_version=payload.expected_version,
            reason=payload.reason, operator_id=user.id,
        )
        db.commit()
        return _lot_dict_for_db(db, row)
    except WarehouseInventoryError as error:
        db.rollback()
        _handle(error)
    except IntegrityError as error:
        db.rollback()
        _handle_integrity(error)


@router.post("/lots/{lot_id}/edit-finished")
def edit_finished_inventory_lot(
    lot_id: int,
    payload: FinishedLotEditPayload,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    _require_lot_customer_access(db, lot_id, user)
    if not payload.is_general:
        assert payload.customer_id is not None
        require_customer_access(payload.customer_id, user, db)
    try:
        row = edit_finished_lot(
            db,
            lot_id=lot_id,
            operator_id=user.id,
            **payload.model_dump(),
        )
        db.commit()
        return _lot_dict_for_db(db, row)
    except WarehouseInventoryError as error:
        db.rollback()
        _handle(error)
    except IntegrityError as error:
        db.rollback()
        _handle_integrity(error)


class FinishedIdentityConfirmation(BaseModel):
    preview_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    operation_key: str = Field(min_length=1, max_length=64)
    physical_match_confirmed: Literal[True]


@router.get("/lots/{lot_id}/physical-identity/preview")
def preview_finished_identity(lot_id: int, db: Session = Depends(get_db), user: User = Depends(admin_only)):
    _require_lot_customer_access(db, lot_id, user)
    from app.services.finished_stock_identity import identity_preview
    try:
        return identity_preview(db, lot_id)
    except WarehouseInventoryError as error:
        _handle(error)


@router.post("/lots/{lot_id}/physical-identity/confirm")
def confirm_finished_identity(lot_id: int, payload: FinishedIdentityConfirmation,
                              db: Session = Depends(get_db), user: User = Depends(admin_only)):
    _require_lot_customer_access(db, lot_id, user)
    from app.services.finished_stock_identity import confirm_identity
    try:
        result = confirm_identity(db, lot_id=lot_id, preview_hash=payload.preview_hash,
            operation_key=payload.operation_key, actor=user)
        db.commit()
        return result
    except WarehouseInventoryError as error:
        db.rollback()
        _handle(error)
    except Exception:
        db.rollback()
        raise


def _lot_location_transfer_dict(
    db: Session,
    row: InventoryLotTransfer,
    *,
    source_lot: InventoryLot,
    target_lot: InventoryLot,
    replayed: bool,
) -> dict:
    projection_contexts = load_warehouse_location_projection_contexts(
        db,
        [
            lot.location
            for lot in (source_lot, target_lot)
            if lot.location is not None
        ],
    )

    def lot_payload(lot: InventoryLot) -> dict:
        return _lot_dict(
            lot,
            location_projection_context=(
                projection_contexts.get(int(lot.warehouse_location_id))
                if lot.warehouse_location_id is not None
                else None
            ),
        )

    return {
        "id": row.id,
        "quantity": row.quantity,
        "available_quantity": row.available_quantity,
        "reserved_quantity": row.reserved_quantity,
        "source_location_id": row.source_location_id,
        "target_location_id": row.target_location_id,
        "transferred_at": utc_naive_to_api(row.transferred_at),
        "replayed": replayed,
        "source_lot": lot_payload(source_lot),
        "target_lot": lot_payload(target_lot),
    }


@router.post("/lots/{lot_id}/location-transfers")
def transfer_finished_lot_from_staging(
    lot_id: int,
    payload: FinishedLotLocationTransferPayload,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    lot = _require_lot_customer_access(db, lot_id, user)
    before = _inventory_lot_audit_state(lot)
    customer_id, customer_name = _inventory_lot_audit_customer(lot)
    try:
        result = transfer_staging_finished_lot(
            db,
            lot_id=lot_id,
            expected_version=payload.expected_version,
            quantity=payload.quantity,
            location_id=payload.location_id,
            operator_id=user.id,
            idempotency_key=payload.idempotency_key,
            expected_target_layout_version=payload.expected_target_layout_version,
        )
        if not result.replayed:
            target_location = db.get(WarehouseLocation, payload.location_id)
            append_audit_event(
                db,
                request=request,
                actor=user,
                event_category="business",
                result="success",
                source="web",
                module_code="warehouse",
                action_code="warehouse.staging_lot.location_transfer",
                legacy_action="TRANSFER_STAGING_LOT",
                resource="InventoryLotTransfer",
                entity_type="inventory_lot_transfer",
                entity_id=result.transfer.id,
                object_ref=f"inventory_lot_transfer:{result.transfer.id}",
                customer_id=customer_id,
                customer_name=customer_name,
                description="一楼待送成品转入正式库位",
                details={
                    "source_lot_id": lot_id,
                    "target_lot_id": result.target_lot.id,
                    "quantity": payload.quantity,
                    "available_quantity": result.transfer.available_quantity,
                    "reserved_quantity": result.transfer.reserved_quantity,
                    "source_location_id": result.transfer.source_location_id,
                    "target_location_id": result.transfer.target_location_id,
                    "target_location_code": (
                        target_location.location_code if target_location else None
                    ),
                    "before": before,
                    "source_after": _inventory_lot_audit_state(result.source_lot),
                    "target_after": _inventory_lot_audit_state(result.target_lot),
                    "idempotency_key": payload.idempotency_key,
                },
            )
        db.commit()
        return _lot_location_transfer_dict(
            db,
            result.transfer,
            source_lot=result.source_lot,
            target_lot=result.target_lot,
            replayed=result.replayed,
        )
    except WarehouseInventoryError as error:
        db.rollback()
        _handle(error)
    except IntegrityError as error:
        db.rollback()
        _handle_integrity(error)


def _inventory_lot_audit_state(row: InventoryLot | None) -> dict[str, object] | None:
    if row is None:
        return None
    return {
        "available": row.quantity_available,
        "reserved": row.quantity_reserved,
        "consumed": row.quantity_consumed,
        "damaged": row.quantity_damaged,
        "scrapped": row.quantity_scrapped,
        "status": row.status,
        "version": row.version,
    }


def _inventory_operation_replayed(db: Session, idempotency_key: str | None) -> bool:
    if not idempotency_key:
        return False
    return db.scalar(
        select(InventoryMovement.id).where(
            InventoryMovement.idempotency_key == idempotency_key
        ).limit(1)
    ) is not None


def _inventory_lot_audit_customer(row: InventoryLot) -> tuple[int | None, str | None]:
    detail = row.finished_detail or row.semi_finished_detail
    if detail is None:
        return None, None
    return detail.owner_customer_id, detail.owner_customer_name_snapshot


def _append_inventory_lot_audit(
    db: Session,
    *,
    request: Request | None,
    user: User,
    action_code: str,
    row: InventoryLot,
    before: dict[str, object] | None,
    reason: str | None,
    idempotency_key: str | None,
    customer_id: int | None = None,
    customer_name: str | None = None,
) -> None:
    current_customer_id, current_customer_name = _inventory_lot_audit_customer(row)
    customer_id = current_customer_id if customer_id is None else customer_id
    customer_name = current_customer_name if customer_name is None else customer_name
    location = db.get(WarehouseLocation, row.warehouse_location_id)
    append_audit_event(
        db,
        request=request,
        actor=user,
        event_category="business",
        result="success",
        source="web",
        module_code="warehouse",
        action_code=action_code,
        legacy_action=action_code.upper().replace(".", "_")[:30],
        resource="InventoryLot",
        entity_type="inventory_lot",
        entity_id=row.id,
        object_ref=row.lot_number,
        customer_id=customer_id,
        customer_name=customer_name,
        description="库存批次操作",
        details={
            "before": before,
            "after": _inventory_lot_audit_state(row),
            "inventory_type": row.inventory_type,
            "location_id": row.warehouse_location_id,
            "location_code": location.location_code if location is not None else None,
            "owner_customer_id_after": current_customer_id,
            "reason": reason,
            "idempotency_key": idempotency_key,
        },
    )


def _operate(
    db: Session,
    user: User,
    lot_id: int,
    operation: str,
    payload: VersionPayload,
    quantity: int = 0,
    request: Request | None = None,
) -> dict:
    reason = (payload.reason or "").strip() or {
        "adjust": "库存数量调整（系统记录）",
        "damage": "库存报损（系统记录）",
        "scrap": "库存报废（系统记录）",
        "transfer_to_general": "转为通用成品库存（系统记录）",
        "freeze": "冻结库存（系统记录）",
        "unfreeze": "解冻库存（系统记录）",
    }.get(operation, "库存批次操作（系统记录）")
    _require_lot_customer_access(db, lot_id, user)
    try:
        lot_before = db.get(InventoryLot, lot_id)
        before = _inventory_lot_audit_state(lot_before)
        customer_id, customer_name = _inventory_lot_audit_customer(lot_before)
        replayed = _inventory_operation_replayed(db, payload.idempotency_key)
        row = mutate_lot(
            db,
            lot_id=lot_id,
            operation=operation,
            expected_version=payload.expected_version,
            operator_id=user.id,
            quantity=quantity,
            reason=reason,
            idempotency_key=payload.idempotency_key,
        )
        if not replayed:
            _append_inventory_lot_audit(
                db,
                request=request,
                user=user,
                action_code=f"warehouse.lot.{operation}",
                row=row,
                before=before,
                reason=reason,
                idempotency_key=payload.idempotency_key,
                customer_id=customer_id,
                customer_name=customer_name,
            )
        db.commit()
        return _lot_dict_for_db(db, row)
    except WarehouseInventoryError as error:
        db.rollback()
        _handle(error)
    except Exception:
        db.rollback()
        raise


@router.post("/lots/{lot_id}/adjust")
def adjust_lot(lot_id: int, payload: AdjustPayload, request: Request = None, db: Session = Depends(get_db), user: User = Depends(can_operate)) -> dict:
    return _operate(db, user, lot_id, "adjust", payload, payload.quantity_delta, request)


@router.post("/lots/{lot_id}/freeze")
def freeze_lot(lot_id: int, payload: VersionPayload, request: Request = None, db: Session = Depends(get_db), user: User = Depends(can_operate)) -> dict:
    return _operate(db, user, lot_id, "freeze", payload, request=request)


@router.post("/lots/{lot_id}/unfreeze")
def unfreeze_lot(lot_id: int, payload: VersionPayload, request: Request = None, db: Session = Depends(get_db), user: User = Depends(can_operate)) -> dict:
    return _operate(db, user, lot_id, "unfreeze", payload, request=request)


@router.post("/lots/{lot_id}/damage")
def damage_lot(lot_id: int, payload: QuantityOperationPayload, request: Request = None, db: Session = Depends(get_db), user: User = Depends(can_operate)) -> dict:
    return _operate(db, user, lot_id, "damage", payload, payload.quantity, request)


@router.post("/lots/{lot_id}/scrap")
def scrap_lot(lot_id: int, payload: QuantityOperationPayload, request: Request = None, db: Session = Depends(get_db), user: User = Depends(can_operate)) -> dict:
    return _operate(db, user, lot_id, "scrap", payload, payload.quantity, request)


@router.post("/lots/{lot_id}/transfer-to-general")
def transfer_to_general(lot_id: int, payload: VersionPayload, request: Request = None, db: Session = Depends(get_db), user: User = Depends(admin_only)) -> dict:
    return _operate(db, user, lot_id, "transfer_to_general", payload, request=request)


from app.api.inventory_cost_rules import router as inventory_cost_rules_router
router.include_router(inventory_cost_rules_router)
