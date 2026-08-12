from __future__ import annotations

import base64
from datetime import date, datetime, timedelta
from decimal import Decimal
import hashlib
from io import BytesIO
import json
import re
import socket
from threading import Lock
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request
import qrcode
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

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
from app.models.delivery import Delivery, DeliveryPickTask
from app.models.mold_tool import MoldLocationMovement, MoldTool
from app.models.printing_plate import (
    PrintingPlate,
    PrintingPlateLocationMovement,
    PrintingPlateResinReuse,
)
from app.models.product import Product
from app.models.product_bom import SalesOrderItemBomComponent
from app.models.order import Order, OrderItem
from app.models.production import ProductionTask
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
    OrderItemSemiRequirement,
    SemiFinishedInventoryDetail,
    SemiFinishedLotAllowedProduct,
    WAREHOUSE_CAPACITY_REVIEW_STATUSES,
    WarehouseArea,
    WarehouseAreaStoragePolicy,
    WarehouseFloor,
    WarehouseLocation,
)
from app.services.floor3_locations import (
    Floor3LocationError,
    add_pallet_item,
    adjust_area_location_count,
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
    policy_location_transition_blockers,
    policy_inventory_types,
    publish_floor_area_policies,
    set_area_location_active,
    update_area_location_layout,
    warehouse_floor_for_code,
)
from app.services.factory_maps import FactoryMapNotFoundError, load_factory_map
from app.services.warehouse_twin_layout import (
    WarehouseTwinLayoutNotFoundError,
    load_warehouse_twin_floor,
)
from app.services.warehouse_floor1_candidate_planner import (
    Floor1CandidatePlanningError,
    build_floor1_formal_candidate_plan,
    confirm_floor1_formal_candidate_plan,
    inspect_floor1_formal_candidate_state,
    overlay_formal_area_bindings,
)
from app.services.warehouse_twin_layout_editor import (
    WarehouseTwinLayoutEditConflictError,
    WarehouseTwinLayoutEditError,
    WarehouseTwinLayoutEditNotFoundError,
    create_warehouse_twin_rack,
    delete_warehouse_twin_rack,
    discard_warehouse_twin_layout_draft,
    load_warehouse_twin_layout_draft,
    publish_warehouse_twin_layout_draft,
    update_warehouse_twin_rack,
    update_warehouse_twin_zone_policy,
    validate_warehouse_twin_layout_draft,
)
from app.services.warehouse_twin_production import (
    WarehouseTwinProductionError,
    build_production_projection,
    delete_production_projection_mapping,
    save_production_projection_mapping,
)
from app.services.asset_time_archive import (
    build_inventory_lot_detail_timeline,
    build_inventory_lot_time_archives,
    build_mold_detail_timeline,
    build_printing_plate_time_archives,
)
from app.services.production_workflow import PENDING, list_production_tasks
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
    void_semi_finished_lot,
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
from app.services.location_candidates import (
    list_operational_locations,
    operational_location_issue,
    operational_location_payload,
)
from app.services.requisition_quantities import cutting_factor
from app.services.master_data_versioning import (
    apply_versioned_update,
    record_versioned_create,
)
from app.services.mold_identity import (
    MoldIdentityError,
    next_available_mold_code,
)
from app.services.mold_location import (
    MOLD_ARCHIVE_AREA_CODE,
    MoldLocationError,
    MoldLocationMoveResult,
    MoldLocationPreview,
    confirm_mold_location_move,
    describe_mold_location,
    mold_location_feature_codes,
    one_floor_mold_location_options,
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
_MOLD_CODE_WRITE_LOCK = Lock()
# Configuration/master-data operations have no N028 permission equivalent and
# intentionally retain their legacy admin-only boundary.
admin_only = RoleChecker(["admin"])
can_read = PermissionChecker("warehouse.view")
can_operate = PermissionChecker("warehouse.execute")
can_archive = PermissionChecker("warehouse.archive")
can_read_orders = PermissionChecker("orders.view")
can_reserve = PermissionChecker("warehouse.reserve")
can_view_reservations = PermissionChecker("warehouse.view")
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
    if has_permission(current_user, "warehouse.view") or has_permission(
        current_user, "deliveries.pick"
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


class TwinFinishedInboundPayload(BaseModel):
    """Admin-confirmed map entry into the existing finished-goods ledger."""

    location_id: int = Field(gt=0)
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


class TwinTemporaryFinishedInboundPayload(BaseModel):
    """Explicit temporary product creation and first stock placement by an admin."""

    location_id: int = Field(gt=0)
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


class Floor3AreaLocationCountPayload(BaseModel):
    target_count: int = Field(ge=0, le=500)
    confirmed: Literal[True]


class Floor3LayoutSlotStatePayload(BaseModel):
    expected_version: int = Field(gt=0)


class Floor3PalletClearPayload(BaseModel):
    expected_version: int = Field(gt=0)
    remarks: str | None = Field(default=None, max_length=500)


class Floor3PalletRelocationPayload(BaseModel):
    expected_version: int = Field(gt=0)
    needs_relocation: bool
    placement_confirmed: bool = False
    remarks: str | None = Field(default=None, max_length=500)


class MoldToolPayload(BaseModel):
    # mold_code remains optional for compatibility with older API clients.
    # The warehouse UI sends customer_initials and lets the server allocate it.
    mold_code: str | None = Field(default=None, max_length=100)
    customer_initials: str | None = Field(default=None, max_length=20)
    mold_name: str = Field(min_length=1, max_length=200)
    rack_location: str = Field(min_length=1, max_length=250)
    remarks: str | None = None

    @field_validator("mold_name", "rack_location")
    @classmethod
    def strip_mold_fields(cls, value: str) -> str:
        return value.strip()

    @field_validator("mold_code", "customer_initials")
    @classmethod
    def strip_optional_mold_fields(cls, value: str | None) -> str | None:
        text = (value or "").strip()
        return text or None


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
    quantity: int = Field(gt=0)
    stock_date: date
    stock_date_accuracy: Literal["exact", "estimated", "unknown"] = "exact"
    stock_date_original_text: str | None = Field(default=None, max_length=100)
    source_type: str = "manual"
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


def _location_map_status(row: WarehouseLocation) -> str:
    if (
        row.source_version not in {"V11", AREA_LOCATION_SOURCE_VERSION}
        or (row.placement_status or "placed") != "placed"
        or not row.is_active
        or row.floor3_layout is None
    ):
        return "unplaced" if row.source_version in {"V11", AREA_LOCATION_SOURCE_VERSION} else "ledger_only"
    if row.source_version == "V11" and int(row.warehouse_floor or 0) != 3:
        return "unplaced"
    return "floor3_mapped" if row.source_version == "V11" else "twin_mapped"


def _location_dict(row: WarehouseLocation) -> dict:
    return {
        "id": row.id,
        "location_code": row.location_code,
        "location_name": row.location_name,
        "warehouse_type": row.warehouse_type,
        "warehouse_floor": getattr(row, "warehouse_floor", None),
        "area_code": getattr(row, "area_code", None),
        "storage_type": getattr(row, "storage_type", None),
        "level_no": getattr(row, "level_no", None),
        "side_code": getattr(row, "side_code", None),
        "sort_order": getattr(row, "sort_order", 0),
        "is_temporary": getattr(row, "is_temporary", False),
        "source_version": getattr(row, "source_version", None),
        "placement_status": getattr(row, "placement_status", None) or "placed",
        "is_active": row.is_active,
        "map_status": _location_map_status(row),
        "remarks": row.remarks,
    }


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
) -> dict:
    payload = _location_dict(row)
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


def _lot_query():
    return (
        select(InventoryLot)
        .where(
            InventoryLot.warehouse_location_id.in_(
                select(WarehouseLocation.id).where(
                    _formal_inventory_location_condition()
                )
            )
        )
        .options(
            selectinload(InventoryLot.location),
            selectinload(InventoryLot.finished_detail),
            selectinload(InventoryLot.semi_finished_detail),
            selectinload(InventoryLot.allowed_products).selectinload(
                SemiFinishedLotAllowedProduct.product
            ),
            selectinload(InventoryLot.pallet_item).selectinload(
                InventoryPalletItem.pallet
            ),
        )
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
    lot = db.scalar(_lot_query().where(InventoryLot.id == lot_id))
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


def _lot_dict(row: InventoryLot, time_archive: dict | None = None) -> dict:
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
                "客户专用纸板备料"
                if item.owner_customer_id is not None
                else "通用半成品片料"
            ),
            "allowed_products": allowed_products,
        }
    return {
        "id": row.id,
        "lot_number": row.lot_number,
        "inventory_type": row.inventory_type,
        "location": _location_dict(row.location),
        "quantity_available": row.quantity_available,
        "quantity_reserved": row.quantity_reserved,
        "quantity_consumed": row.quantity_consumed,
        "quantity_damaged": row.quantity_damaged,
        "quantity_scrapped": row.quantity_scrapped,
        "unit": row.unit,
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
        "customer_name": customer.name if customer else None,
        "product_name": item.snapshot_product_name if item else None,
        "reserved_stock_quantity": row.reserved_stock_quantity,
        "credited_requirement_quantity": row.credited_requirement_quantity,
        "consumed_stock_quantity": row.consumed_stock_quantity,
        "released_stock_quantity": row.released_stock_quantity,
        "consumed_requirement_quantity": row.consumed_requirement_quantity,
        "released_requirement_quantity": row.released_requirement_quantity,
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


def _semi_candidate_dict(row: SemiFinishedCandidate) -> dict:
    lot = row.lot
    detail = lot.semi_finished_detail
    return {
        "lot_id": lot.id,
        "lot_number": lot.lot_number,
        "version": lot.version,
        "source": row.source,
        "match_rule_id": row.match_rule_id,
        "available_stock_quantity": row.available_stock_quantity,
        "deductible_requirement_quantity": row.deductible_requirement_quantity,
        "warehouse_location": _location_dict(lot.location),
        "customer_id": detail.owner_customer_id,
        "customer_name": detail.owner_customer_name_snapshot,
        "board_length_mm": detail.board_length_mm,
        "board_width_mm": detail.board_width_mm,
        "material_code": detail.material_code_snapshot,
        "normalized_material_code": detail.normalized_material_code,
        "flute_type": detail.flute_type,
        "component_type": detail.component_type,
        "pieces_per_box": detail.pieces_per_box,
        "stock_yield_per_sheet": detail.stock_yield_per_sheet,
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
        item = db.get(OrderItem, order_item_id)
        if item is None:
            raise WarehouseInventoryError("订单明细不存在", 404)
        reserved = active_finished_reserved_qty(db, order_item_id)
        rows = finished_inventory_candidates(db, order_item_id)
        rows = _visible_finished_candidate_lots(rows, user, db)
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
                        f"{lot.location.location_code} {lot.location.location_name}"
                    ),
                    "quantity_available": lot.quantity_available,
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
            yield_per_sheet = cutting_factor(
                snapshot.snapshot_component_default_cutting_mode
            )
            if requirement is None:
                preview_candidates = semi_finished_candidates_for_product(
                    db,
                    product_id=snapshot.component_product_id,
                    customer_id=order.customer_id,
                    board_length_mm=int(physical_facts["board_length_mm"]),
                    board_width_mm=int(physical_facts["board_width_mm"]),
                    material_code=str(snapshot.snapshot_component_material),
                    flute_type=str(snapshot.snapshot_component_flute_type),
                    component_type=component_type,
                    pieces_per_box=physical_pieces_per_component,
                    stock_yield_per_sheet=yield_per_sheet,
                )
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
                    "warehouse_location": _location_dict(lot.location),
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
            **payload.model_dump(),
        )
        rows = _visible_semi_candidates(rows, user, db)
        return {
            "product_id": product_id,
            "items": [_semi_candidate_dict(row) for row in rows],
        }
    except WarehouseInventoryError as error:
        _handle(error)


@router.post("/semi-finished/products/{product_id}/inventory")
def semi_product_inventory_browser(
    product_id: int,
    payload: SemiProductCandidatePayload,
    db: Session = Depends(get_db),
    user: User = Depends(can_view_reservations),
) -> dict:
    require_customer_access(payload.customer_id, user, db)
    try:
        rows = browse_semi_finished_inventory_for_product(
            db,
            product_id=product_id,
            **payload.model_dump(),
        )
        rows = _visible_semi_candidates(rows, user, db)
        return {
            "product_id": product_id,
            "items": [_semi_candidate_dict(row) for row in rows],
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
            "items": [_semi_candidate_dict(row) for row in rows],
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
        return {"items": [_semi_candidate_dict(row) for row in rows]}
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
                "specification": " × ".join(
                    str(round(value))
                    for value in (product.length_mm, product.width_mm, product.height_mm)
                    if value is not None
                ),
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
            or location.source_version != "V11"
        ):
            raise HTTPException(status_code=404, detail="三楼 V11 栈板不存在")
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
    try:
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
            "location": _location_dict(location),
            "layout": _floor3_layout_dict(location.floor3_layout),
        }
    except Floor3LocationError as error:
        db.rollback()
        raise HTTPException(status_code=error.status_code, detail=str(error)) from error
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(status_code=409, detail="货位编码或布局已存在") from error


@router.post("/floor3/layout/areas/{area_code}/location-count")
def set_floor3_area_location_count(
    area_code: str,
    payload: Floor3AreaLocationCountPayload,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    try:
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
                        "location": _location_dict(location),
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


@router.patch("/floor3/layout/areas/{area_code}")
def patch_floor3_layout_area(
    area_code: str,
    payload: Floor3LayoutAreaPatchPayload,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    try:
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
                    "layout_version": layout.version,
                },
            )
        db.commit()
        return {"items": [_floor3_layout_dict(layout) for layout in layouts]}
    except Floor3LocationError as error:
        db.rollback()
        raise HTTPException(status_code=error.status_code, detail=str(error)) from error


@router.post("/spatial-layout/floors/{floor_code}/areas/{area_code}/location-count")
def set_activated_area_location_count(
    floor_code: str,
    area_code: str,
    payload: Floor3AreaLocationCountPayload,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    if floor_code.strip().upper() == "3F":
        raise HTTPException(
            status_code=409,
            detail="三楼既有正式库位请继续使用三楼库位数量入口",
        )
    try:
        result = adjust_activated_area_location_count(
            db,
            floor_code=floor_code,
            area_code=area_code,
            target_count=payload.target_count,
            operator_id=user.id,
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
                    description="按正式区域目标数量维护一楼库存库位",
                    details={
                        "floor_code": result.floor_code,
                        "area_code": result.area_code,
                        "target_count": result.target_count,
                        "location_code": location.location_code,
                        "location_count_action": action,
                    },
                )
                actions.append(
                    {
                        "action": action,
                        "location": _location_dict(location),
                        "layout": _floor3_layout_dict(location.floor3_layout),
                    }
                )
        db.commit()
        return {
            "floor_code": result.floor_code,
            "area_code": result.area_code,
            "target_count": result.target_count,
            "active_count": result.active_count,
            "created_count": len(result.created),
            "enabled_count": len(result.enabled),
            "disabled_count": len(result.disabled),
            "items": actions,
            "message": (
                "新增库位已进入待布局草稿；拖到实际位置、保存并发布后才会进入生产入库候选。"
                if result.created
                else "区域库位数量已更新；重新发布前不会改变员工入库候选。"
            ),
        }
    except WarehouseAreaActivationError as error:
        db.rollback()
        raise HTTPException(status_code=error.status_code, detail=str(error)) from error
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(
            status_code=409, detail="区域库位编号或布局发生冲突，请刷新后重试"
        ) from error


@router.patch("/spatial-layout/floors/{floor_code}/areas/{area_code}")
def patch_activated_area_location_layout(
    floor_code: str,
    area_code: str,
    payload: Floor3LayoutAreaPatchPayload,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    if floor_code.strip().upper() == "3F":
        raise HTTPException(
            status_code=409,
            detail="三楼既有正式库位请继续使用三楼布局入口",
        )
    try:
        layouts = update_area_location_layout(
            db,
            floor_code=floor_code,
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
                description="保存正式区域库位二维位置",
                details={
                    "floor_code": floor_code.strip().upper(),
                    "area_code": area_code.strip().upper(),
                    "layout_version": layout.version,
                },
            )
        db.commit()
        return {"items": [_floor3_layout_dict(layout) for layout in layouts]}
    except WarehouseAreaActivationError as error:
        db.rollback()
        raise HTTPException(status_code=error.status_code, detail=str(error)) from error
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(
            status_code=409, detail="区域布局发生冲突，请刷新后重试"
        ) from error


@router.post("/spatial-layout/locations/{location_id}/disable")
def disable_activated_area_location(
    location_id: int,
    payload: Floor3LayoutSlotStatePayload,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    try:
        location = set_area_location_active(
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
            description="逻辑停用正式区域空库位",
            details={"is_active": False, "source_version": AREA_LOCATION_SOURCE_VERSION},
        )
        db.commit()
        return {
            "location": _location_dict(location),
            "layout": _floor3_layout_dict(location.floor3_layout),
        }
    except WarehouseAreaActivationError as error:
        db.rollback()
        raise HTTPException(status_code=error.status_code, detail=str(error)) from error


@router.post("/floor3/layout/slots/{location_id}/disable")
def disable_floor3_layout_slot(
    location_id: int,
    payload: Floor3LayoutSlotStatePayload,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    try:
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
        return {"location": _location_dict(location), "layout": _floor3_layout_dict(location.floor3_layout)}
    except Floor3LocationError as error:
        db.rollback()
        raise HTTPException(status_code=error.status_code, detail=str(error)) from error


@router.post("/floor3/layout/slots/{location_id}/enable")
def enable_floor3_layout_slot(
    location_id: int,
    payload: Floor3LayoutSlotStatePayload,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    try:
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
        return {"location": _location_dict(location), "layout": _floor3_layout_dict(location.floor3_layout)}
    except Floor3LocationError as error:
        db.rollback()
        raise HTTPException(status_code=error.status_code, detail=str(error)) from error


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
        selectinload(WarehouseLocation.floor3_layout)
    ).where(
        WarehouseLocation.warehouse_floor == 3,
        WarehouseLocation.source_version == "V11",
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
    return {
        "items": [
            _floor3_location_dict(
                row,
                pallet=pallets_by_location.get(row.id),
                visible_customer_ids=visible_customer_ids,
                customer_names=customer_names,
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
            selectinload(WarehouseLocation.floor3_layout)
        ).where(
            WarehouseLocation.id == location_id,
            WarehouseLocation.warehouse_floor == 3,
            WarehouseLocation.source_version == "V11",
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
    location_codes = (
        dict(
            db.execute(
                select(WarehouseLocation.id, WarehouseLocation.location_code).where(
                    WarehouseLocation.id.in_(location_ids)
                )
            ).all()
        )
        if location_ids
        else {}
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
                "from_location_code": location_codes.get(movement.from_location_id),
                "to_location_id": movement.to_location_id,
                "to_location_code": location_codes.get(movement.to_location_id),
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
        ),
        "movement_history": history,
    }


@router.get("/floor3/product-candidates")
def floor3_product_candidates(
    q: str = Query(default="", max_length=150),
    customer_id: int | None = Query(default=None, gt=0),
    limit: int = Query(default=30, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    keyword = q.strip()
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
                Customer.name.like(pattern),
                Product.id.in_(order_product_ids) if order_product_ids else False,
            ),
        )
    )
    if customer_id is not None:
        product_query = product_query.where(Product.customer_id == customer_id)
    elif visible_customer_ids is not None:
        product_query = product_query.where(Product.customer_id.in_(visible_customer_ids))
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
        candidates.append(
            {
                "priority": priority,
                "match_type": match_type,
                "is_exact": priority < 3,
                "product_id": product.id,
                "customer_id": customer.id,
                "customer_name": customer.name,
                "product_code": product.product_code,
                "customer_material_code": product.customer_material_code,
                "product_name": product.product_name,
                "specification": " × ".join(
                    str(round(value))
                    for value in (product.length_mm, product.width_mm, product.height_mm)
                    if value is not None
                ),
                "matched_order_numbers": sorted(
                    set(orders_by_product.get(product.id, []))
                )[:10],
                "is_tianhua": "天华" in customer.name,
            }
        )
    candidates.sort(
        key=lambda row: (
            row["priority"],
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
    if location.warehouse_floor not in {1, 3}:
        raise HTTPException(status_code=409, detail="只能选择一楼或三楼地图中的正式货位")
    issue = operational_location_issue(
        db,
        location,
        warehouse_types={"finished", "shared"},
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
                "source_location_name": source_location.location_name,
                "stock_date": lot.stock_date,
            }
        )
    return {
        "target_location": {
            "location_id": location.id,
            "location_code": location.location_code,
            "location_name": location.location_name,
            "area_code": location.area_code,
        },
        "items": items,
        "total": len(items),
        "message": "从当前空货位选择已完工未送货产品；确认后只移动原库存，不增加数量。",
    }


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
    if location.warehouse_floor != 1:
        raise HTTPException(status_code=409, detail="半成品请在一楼地图选择已启用的半成品库位")
    issue = operational_location_issue(
        db,
        location,
        warehouse_types={"semi_finished", "shared"},
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
            "lot": _lot_dict(lot),
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
        or location.source_version != "V11"
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
            "lot": _lot_dict(row),
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
    try:
        row = create_pallet(
            db,
            location_id=payload.location_id,
            pallet_code=payload.pallet_code,
            items=[item.model_dump() for item in payload.items],
            remarks=payload.remarks,
            operator_id=user.id,
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
            "lot": _lot_dict(lot),
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
    return {
        "id": row.id,
        "floor_id": row.floor_id,
        "floor_code": row.floor.floor_code,
        "floor_name": row.floor.floor_name,
        "floor_number": row.floor.floor_number,
        "area_code": row.area_code,
        "area_name": row.area_name,
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


def _warehouse_floor_dict(db: Session, row: WarehouseFloor) -> dict:
    areas = sorted(row.areas, key=lambda item: (item.area_code, item.id))
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
        return overlay_formal_area_bindings(
            db,
            floor_code=floor_code,
            floor_layout=load_warehouse_twin_floor(floor_code),
        )
    except WarehouseTwinLayoutNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.get("/twin-layout/floors/{floor_code}/draft")
def get_warehouse_twin_floor_layout_draft(
    floor_code: str,
    db: Session = Depends(get_db),
    _user: User = Depends(admin_only),
) -> dict:
    try:
        return overlay_formal_area_bindings(
            db,
            floor_code=floor_code,
            floor_layout=load_warehouse_twin_layout_draft(floor_code),
        )
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
    storage_layout: Literal["rack", "pallet_ground", "mixed"]
    erp_area_code: str | None = Field(default=None, max_length=30)
    area_name: str | None = Field(default=None, max_length=100)

    @field_validator("erp_area_code")
    @classmethod
    def normalize_erp_area_code(cls, value: str | None) -> str | None:
        normalized = (value or "").strip().upper()
        return normalized or None

    @field_validator("area_name")
    @classmethod
    def normalize_area_name(cls, value: str | None) -> str | None:
        normalized = (value or "").strip()
        return normalized or None


class TwinLayoutDraftValidatePayload(BaseModel):
    expected_revision: str = Field(min_length=1, max_length=64)


class TwinLayoutDraftPublishPayload(BaseModel):
    expected_published_revision: str = Field(min_length=1, max_length=64)
    expected_draft_revision: str = Field(min_length=1, max_length=64)
    operation_key: str = Field(min_length=8, max_length=120)


class TwinLayoutDraftDiscardPayload(BaseModel):
    expected_revision: str = Field(min_length=1, max_length=64)


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
        plan = build_floor1_formal_candidate_plan(load_warehouse_twin_floor("1F"))
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
        result = confirm_floor1_formal_candidate_plan(
            db,
            floor_layout=load_warehouse_twin_floor("1F"),
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


@router.post("/twin-layout/floors/{floor_code}/draft/validate")
def validate_twin_layout_draft(
    floor_code: str,
    payload: TwinLayoutDraftValidatePayload,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    try:
        result = validate_warehouse_twin_layout_draft(
            floor_code,
            expected_revision=payload.expected_revision,
        )
    except WarehouseTwinLayoutEditError as error:
        _handle_twin_layout_edit_error(error)
    _twin_layout_asset_log(
        db,
        request=request,
        user=user,
        action="TWIN_LAYOUT_DRAFT_VALIDATE",
        entity_type="twin_layout_draft",
        entity_id=floor_code.upper(),
        description="管理员校验仓库地图草稿",
        details=result.value,
    )
    db.commit()
    return {**result.value, "applied": result.applied}


def _formal_area_publish_blockers(db: Session, floor_code: str) -> list[str]:
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
    policies = list(
        db.scalars(
            select(WarehouseAreaStoragePolicy)
            .join(WarehouseArea)
            .where(WarehouseArea.floor_id == floor.id)
            .options(selectinload(WarehouseAreaStoragePolicy.area))
        ).all()
    )
    blockers: list[str] = []
    if floor.floor_number == 1:
        policy_features = {policy.map_feature_id for policy in policies}
        for feature_id, feature in features.items():
            area_code = str(feature.get("erp_area_code") or "").strip().upper()
            if area_code and feature_id not in policy_features:
                blockers.append(f"{area_code} 地图区域尚未写入正式区域台账")
    for policy in policies:
        area = policy.area
        feature = features.get(policy.map_feature_id)
        if feature is None:
            blockers.append(f"{area.area_code} 区绑定的地图区域已不存在")
            continue
        feature_area_code = str(feature.get("erp_area_code") or "").strip().upper()
        feature_inventory_types = list(feature.get("allowed_inventory_types") or [])
        feature_storage_layout = str(feature.get("storage_layout") or "")
        if feature_area_code and feature_area_code != area.area_code:
            blockers.append(f"{area.area_code} 区的地图正式区域编号不一致")
        if feature_inventory_types and feature_inventory_types != policy_inventory_types(policy):
            blockers.append(f"{area.area_code} 区存放类型尚未同步到正式台账")
        if feature_storage_layout and feature_storage_layout != policy.storage_layout:
            blockers.append(f"{area.area_code} 区存储形式尚未同步到正式台账")
        for message in policy_location_transition_blockers(
            db,
            floor=floor,
            area=area,
            policy=policy,
        ):
            blockers.append(f"{area.area_code} 区{message}")
        if area.planned_location_count:
            source_version = (
                "V11" if floor.floor_number == 3 else AREA_LOCATION_SOURCE_VERSION
            )
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


@router.post("/twin-layout/floors/{floor_code}/draft/publish")
def publish_twin_layout_draft(
    floor_code: str,
    payload: TwinLayoutDraftPublishPayload,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    blockers = _formal_area_publish_blockers(db, floor_code)
    if blockers:
        raise HTTPException(
            status_code=409,
            detail="正式区域尚未完成：" + "；".join(blockers[:5]),
        )
    try:
        result = publish_warehouse_twin_layout_draft(
            floor_code,
            expected_published_revision=payload.expected_published_revision,
            expected_draft_revision=payload.expected_draft_revision,
            operation_key=payload.operation_key,
        )
    except WarehouseTwinLayoutEditError as error:
        _handle_twin_layout_edit_error(error)
    try:
        published_policies = publish_floor_area_policies(
            db,
            floor_code=floor_code,
            published_revision=str(result.value.get("published_revision") or ""),
            operator_id=user.id,
            published_features=list(
                load_warehouse_twin_floor(floor_code).get("features") or []
            ),
        )
    except WarehouseAreaActivationError as error:
        db.rollback()
        raise HTTPException(status_code=error.status_code, detail=str(error)) from error
    if result.applied or published_policies:
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
                "formal_area_count": len(published_policies),
                "formal_areas": [
                    {
                        "area_code": policy.area.area_code,
                        "allowed_inventory_types": policy_inventory_types(policy),
                        "planned_location_count": policy.area.planned_location_count,
                    }
                    for policy in published_policies
                ],
                "location_master_changed": bool(published_policies),
                "inventory_changed": False,
            },
        )
        db.commit()
    return {
        **result.value,
        "applied": result.applied,
        "formal_area_count": len(published_policies),
    }


@router.post("/twin-layout/floors/{floor_code}/draft/discard")
def discard_twin_layout_draft(
    floor_code: str,
    payload: TwinLayoutDraftDiscardPayload,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    try:
        result = discard_warehouse_twin_layout_draft(
            floor_code,
            expected_revision=payload.expected_revision,
        )
    except WarehouseTwinLayoutEditError as error:
        _handle_twin_layout_edit_error(error)
    if result.applied:
        _twin_layout_asset_log(
            db,
            request=request,
            user=user,
            action="TWIN_LAYOUT_DRAFT_DISCARD",
            entity_type="twin_layout_draft",
            entity_id=floor_code.upper(),
            description="管理员放弃仓库地图草稿",
            details=result.value,
        )
        db.commit()
    return {**result.value, "applied": result.applied}


@router.post("/twin-layout/floors/{floor_code}/racks", status_code=201)
def create_twin_layout_rack(
    floor_code: str,
    payload: TwinRackLayoutCreatePayload,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    try:
        mutation = create_warehouse_twin_rack(
            floor_code,
            expected_revision=payload.expected_revision,
            operation_key=payload.operation_key,
            area_feature_id=payload.area_feature_id,
            values=_rack_layout_values(payload),
        )
    except WarehouseTwinLayoutEditError as error:
        _handle_twin_layout_edit_error(error)
    if mutation.applied:
        _twin_layout_asset_log(
            db,
            request=request,
            user=user,
            action="TWIN_RACK_CREATE",
            entity_type="twin_rack_layout",
            entity_id=str(mutation.value.get("id") or ""),
            description="二维库位布局新增货架",
            details={"floor_code": floor_code, "rack": mutation.value, "inventory_changed": False},
        )
        db.commit()
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
    try:
        mutation = update_warehouse_twin_rack(
            floor_code,
            rack_id,
            expected_revision=payload.expected_revision,
            expected_version=payload.expected_version,
            operation_key=payload.operation_key,
            values=_rack_layout_values(payload),
        )
    except WarehouseTwinLayoutEditError as error:
        _handle_twin_layout_edit_error(error)
    if mutation.applied:
        _twin_layout_asset_log(
            db,
            request=request,
            user=user,
            action="TWIN_RACK_UPDATE",
            entity_type="twin_rack_layout",
            entity_id=rack_id,
            description="二维库位布局修改货架参数",
            details={"floor_code": floor_code, "rack": mutation.value, "inventory_changed": False},
        )
        db.commit()
    return {"item": mutation.value, "revision": mutation.floor_revision, "applied": mutation.applied}


@router.delete("/twin-layout/floors/{floor_code}/racks/{rack_id}")
def delete_twin_layout_rack(
    floor_code: str,
    rack_id: str,
    expected_revision: str = Query(min_length=1, max_length=64),
    expected_version: int = Query(ge=1),
    operation_key: str = Query(min_length=8, max_length=120),
    request: Request = None,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    try:
        mutation = delete_warehouse_twin_rack(
            floor_code,
            rack_id,
            expected_revision=expected_revision,
            expected_version=expected_version,
            operation_key=operation_key,
        )
    except WarehouseTwinLayoutEditError as error:
        _handle_twin_layout_edit_error(error)
    if mutation.applied:
        _twin_layout_asset_log(
            db,
            request=request,
            user=user,
            action="TWIN_RACK_DELETE",
            entity_type="twin_rack_layout",
            entity_id=rack_id,
            description="二维库位布局删除货架并释放为空地",
            details={"floor_code": floor_code, **mutation.value},
        )
        db.commit()
    return {"item": mutation.value, "revision": mutation.floor_revision, "applied": mutation.applied}


@router.patch("/twin-layout/floors/{floor_code}/zones/{feature_id}/storage-policy")
def update_twin_zone_storage_policy(
    floor_code: str,
    feature_id: str,
    payload: TwinZoneStoragePolicyPayload,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    requested_area_code = str(payload.erp_area_code or "").strip().upper()
    formal_area: WarehouseArea | None = None
    policy: WarehouseAreaStoragePolicy | None = None
    if requested_area_code:
        floor = warehouse_floor_for_code(db, floor_code)
        if floor is None:
            raise HTTPException(status_code=409, detail="请先建立正式仓库楼层台账")
        formal_area = db.scalar(
            select(WarehouseArea)
            .options(selectinload(WarehouseArea.storage_policy))
            .where(
                WarehouseArea.floor_id == floor.id,
                WarehouseArea.area_code == requested_area_code,
            )
        )
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
        )
    except WarehouseTwinLayoutEditError as error:
        _handle_twin_layout_edit_error(error)
    mapped_area_code = str(mutation.value.get("erp_area_code") or "").strip().upper()
    if mapped_area_code:
        assert mapped_area_code == requested_area_code
        floor = warehouse_floor_for_code(db, floor_code)
        assert floor is not None
        if formal_area is None:
            formal_area = WarehouseArea(
                floor_id=floor.id,
                area_code=mapped_area_code,
                area_name=(
                    payload.area_name
                    or str(mutation.value.get("name") or "").strip()
                    or mapped_area_code
                ),
                planned_location_count=0,
                planned_pallet_capacity=0,
                construction_status="layout_building",
                capacity_review_status="pending",
                capacity_eligible=False,
            )
            db.add(formal_area)
            db.flush()
        policy = formal_area.storage_policy
        if policy is None:
            policy = WarehouseAreaStoragePolicy(
                area_id=formal_area.id,
                map_feature_id=feature_id,
                allowed_inventory_types_json="[]",
                storage_layout=payload.storage_layout,
                status="draft",
                version=1,
                updated_by=user.id,
            )
            db.add(policy)
        desired_types_json = json.dumps(
            list(dict.fromkeys(payload.allowed_inventory_types)),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        policy_changed = bool(
            policy.id is None
            or policy.allowed_inventory_types_json != desired_types_json
            or policy.storage_layout != payload.storage_layout
            or policy.status != "draft"
            or policy.draft_map_revision != mutation.floor_revision
            or policy.published_map_revision is not None
        )
        policy.allowed_inventory_types_json = desired_types_json
        policy.storage_layout = payload.storage_layout
        policy.status = "draft"
        policy.draft_map_revision = mutation.floor_revision
        policy.published_map_revision = None
        policy.updated_by = user.id
        if policy.id is not None and policy_changed:
            policy.version += 1
        if formal_area.construction_status != "layout_building":
            formal_area.construction_status = "layout_building"
            policy_changed = True
    else:
        policy_changed = False
    if mutation.applied or policy_changed:
        _twin_layout_asset_log(
            db,
            request=request,
            user=user,
            action="TWIN_ZONE_POLICY_UPDATE",
            entity_type="twin_zone_policy",
            entity_id=feature_id,
            description="二维库位布局修改区域存放策略",
            details={
                "floor_code": floor_code,
                "zone": mutation.value,
                "formal_area_code": mapped_area_code or None,
                "formal_binding_status": policy.status if policy else None,
                "inventory_changed": False,
            },
        )
        try:
            db.commit()
        except IntegrityError as error:
            db.rollback()
            raise HTTPException(
                status_code=409, detail="正式区域编号或地图区域绑定已存在"
            ) from error
    return {
        "item": mutation.value,
        "revision": mutation.floor_revision,
        "applied": mutation.applied,
        "formal_area": (
            _warehouse_area_dict(db, formal_area) if formal_area is not None else None
        ),
    }


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
        floor = load_warehouse_twin_floor("1F")
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
    try:
        floor = load_warehouse_twin_floor("1F")
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
        return save_production_projection_mapping(
            floor=floor,
            source_task_id=source_task_id,
            target_kind=body.target_kind,
            target_id=body.target_id,
            expected_version=body.version,
        )
    except WarehouseTwinLayoutNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except WarehouseTwinProductionError as error:
        _raise_twin_production_error(error)


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
    lot_query = _lot_query()
    if visible_customer_ids is not None:
        lot_query = lot_query.where(_visible_lot_condition(visible_customer_ids))
    lots = list(db.scalars(lot_query.order_by(InventoryLot.id)).unique().all())
    locations = list(
        db.scalars(
            select(WarehouseLocation)
            .where(_formal_inventory_location_condition())
            .options(selectinload(WarehouseLocation.floor3_layout))
            .order_by(WarehouseLocation.sort_order, WarehouseLocation.location_code)
        ).all()
    )
    pallets = list(
        db.scalars(
            select(InventoryPallet)
            .join(WarehouseLocation, WarehouseLocation.id == InventoryPallet.location_id)
            .where(
                InventoryPallet.is_current.is_(True),
                _formal_inventory_location_condition(),
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
) -> dict:
    if days not in {7, 30, 90}:
        raise HTTPException(status_code=422, detail="时间范围仅支持7、30或90天")
    lots, locations, pallets, floors, visible_customer_ids = (
        _twin_dashboard_source_rows(db, user)
    )
    return build_warehouse_twin_dashboard(
        db,
        lots=lots,
        locations=locations,
        pallets=pallets,
        floors=floors,
        visible_customer_ids=visible_customer_ids,
        days=days,
        as_of=beijing_today(),
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
            .where(WarehouseFloor.floor_number.in_((1, 3)))
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
                _formal_inventory_location_condition(),
                WarehouseLocation.warehouse_floor.in_((1, 3)),
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
            .where(WarehouseFloor.floor_number.in_((1, 3)))
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
        if floor is None or floor.floor_number not in {1, 3}:
            raise HTTPException(status_code=422, detail="容量预测只允许选择已建档的一楼或三楼")
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
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    effective_keyword = str(keyword or inventory_code or "").strip()
    if len(effective_keyword) < 2:
        raise HTTPException(status_code=422, detail="全仓查找至少输入2个字符")
    query = _lot_query().where(InventoryLot.status.in_(("active", "frozen")))
    visible_customer_ids = _visible_customer_ids(user, db)
    if visible_customer_ids is not None:
        query = query.where(_visible_lot_condition(visible_customer_ids))
    candidates = list(db.scalars(query.order_by(InventoryLot.id).limit(2500)).unique().all())
    today = beijing_today()
    lots = [
        row for row in candidates
        if inventory_search_matches(row, effective_keyword, today)
    ][:500]
    return build_inventory_code_search_results(
        lots=lots,
        keyword=effective_keyword,
        as_of=today,
    )


def _twin_reference_feature_codes(kind: str, location_text: str | None) -> list[str]:
    normalized = str(location_text or "").strip().upper()
    candidates = (
        ("ZONE-1F-MOLD-001", "ZONE-1F-MOLD-002")
        if kind == "mold"
        else ("ZONE-1F-PLATE-001", "ZONE-1F-PLATE-002")
    )
    return [code for code in candidates if code in normalized]


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


def _twin_reference_area_resources(keyword: str) -> list[dict]:
    needle = keyword.casefold()
    subtype_labels = {"mold": "模具", "printing_plate": "印刷版 模板"}
    resources: list[dict] = []
    for floor_code in ("1F", "3F"):
        try:
            floor = load_warehouse_twin_floor(floor_code)
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
        floor1_layout = load_warehouse_twin_floor("1F")
    except WarehouseTwinLayoutNotFoundError:
        floor1_layout = None
    response = list_mold_tools(
        q=keyword,
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
                "title": mold.get("mold_name") or "模具",
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


@router.get("/twin-operations/locate")
def locate_warehouse_twin_objects(
    keyword: str = Query(default="", max_length=150),
    search_type: Literal["all", "finished", "mold", "printing_plate"] = Query(default="all"),
    customer_id: int | None = Query(default=None, gt=0),
    db: Session = Depends(get_db),
    user: User = Depends(_can_locate_twin),
) -> dict:
    """Unified read-only locator for inventory, pick tasks, molds and plates."""

    effective_keyword = keyword.strip()
    if search_type == "finished" and customer_id is not None:
        require_customer_access(customer_id, user, db)
    if len(effective_keyword) < 2 and not (
        search_type == "finished" and customer_id is not None
    ):
        raise HTTPException(status_code=422, detail="全仓查找至少输入2个字符")
    query = _lot_query().where(InventoryLot.status.in_(("active", "frozen")))
    visible_customer_ids = _twin_locator_visible_customer_ids(db, user)
    if visible_customer_ids is not None:
        query = query.where(_visible_lot_condition(visible_customer_ids))
    if search_type == "finished":
        query = query.where(InventoryLot.inventory_type == "finished")
    today = beijing_today()
    lots = [
        row
        for row in db.scalars(query.order_by(InventoryLot.id).limit(2500)).unique().all()
        if (
            (customer_id is None or (
                row.finished_detail is not None
                and row.finished_detail.owner_customer_id == customer_id
            ))
            and (
                not effective_keyword
                or inventory_search_matches(row, effective_keyword, today)
            )
        )
    ][:500]
    if search_type in {"mold", "printing_plate"}:
        lots = []
    inventory = build_inventory_code_search_results(
        lots=lots,
        keyword=effective_keyword,
        as_of=today,
    )
    pick_resources: list[dict] = []
    pick_tasks: list[dict] = []
    if search_type == "all":
        pick_resources, pick_tasks = _twin_pick_task_resources(
            db, user, effective_keyword, visible_customer_ids
        )
    area_resources = _twin_reference_area_resources(effective_keyword)
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
    return {
        **inventory,
        "search_type": search_type,
        "customer_id": customer_id,
        "result_count": len(inventory["items"]) + len(resources),
        "inventory_result_count": len(inventory["items"]),
        "resource_result_count": len(resources),
        "resources": resources,
        "pick_tasks": pick_tasks,
        "notice": (
            "只显示当前账号有权查看的库存、拿货任务、模具和印刷版位置；"
            "没有已确认坐标的结果只提供文字指引。"
        ),
    }


@router.get("/space/floors")
def list_warehouse_floors(
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
    items = [_warehouse_floor_dict(db, row) for row in rows]
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
    row = db.get(WarehouseArea, area_id)
    if row is None:
        raise HTTPException(status_code=404, detail="区域不存在")
    floor = db.get(WarehouseFloor, payload.floor_id)
    if floor is None:
        raise HTTPException(status_code=404, detail="楼层不存在")
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


@router.get("/locations")
def list_locations(
    include_inactive: bool = False,
    db: Session = Depends(get_db),
    _user: User = Depends(can_read),
) -> dict:
    query = (
        select(WarehouseLocation)
        .options(selectinload(WarehouseLocation.floor3_layout))
        .where(_formal_inventory_location_condition())
    )
    if not include_inactive:
        query = query.where(WarehouseLocation.is_active.is_(True))
    rows = db.scalars(query.order_by(WarehouseLocation.location_code)).all()
    return {"items": [_location_dict(row) for row in rows]}


def _require_printable_location_label(row: WarehouseLocation) -> None:
    if (
        not row.is_active
        or (row.placement_status or "placed") != "placed"
        or row.is_temporary
        or row.storage_type == "temporary_aisle"
        or _location_map_status(row) not in {"floor3_mapped", "twin_mapped"}
    ):
        raise HTTPException(
            status_code=409,
            detail="仅已发布到三楼平面图或一楼仓库地图的正式位置可以打印位置标签",
        )


def _location_label_dict(
    row: WarehouseLocation,
    request: Request,
    lan_ip: str | None = None,
) -> dict:
    _require_printable_location_label(row)
    floor_number = int(row.warehouse_floor or 0)
    floor_text = {1: "一楼", 2: "二楼", 3: "三楼", 4: "四楼"}.get(
        floor_number,
        f"{floor_number}楼" if floor_number else "楼层待确认",
    )
    area_text = f"{row.area_code}区" if row.area_code else "区域待确认"
    port = request.url.port or 8000
    lookup_url = (
        f"http://{lan_ip or _lan_ip()}:{port}/warehouse.html"
        f"?tab=locations&location_id={row.id}"
    )
    image = qrcode.make(lookup_url)
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return {
        **_location_dict(row),
        "floor_text": floor_text,
        "area_text": area_text,
        "display_path": f"{floor_text} · {area_text} · {row.location_name}",
        "layout_version": row.floor3_layout.version if row.floor3_layout else None,
        "lookup_url": lookup_url,
        "qr_data_url": (
            "data:image/png;base64,"
            + base64.b64encode(buffer.getvalue()).decode("ascii")
        ),
    }


@router.get("/locations/labels")
def get_location_labels(
    request: Request,
    location_ids: str = Query(min_length=1, max_length=1200),
    db: Session = Depends(get_db),
    _user: User = Depends(can_read),
) -> dict:
    raw_ids = [part.strip() for part in location_ids.split(",") if part.strip()]
    if not raw_ids or any(not part.isdigit() or int(part) <= 0 for part in raw_ids):
        raise HTTPException(status_code=422, detail="位置批量标签参数无效")
    ordered_ids = list(dict.fromkeys(int(part) for part in raw_ids))
    if len(ordered_ids) > 100:
        raise HTTPException(status_code=422, detail="一次最多打印 100 个位置")
    rows = db.scalars(
        select(WarehouseLocation)
        .options(selectinload(WarehouseLocation.floor3_layout))
        .where(WarehouseLocation.id.in_(ordered_ids))
    ).all()
    rows_by_id = {row.id: row for row in rows}
    if any(location_id not in rows_by_id for location_id in ordered_ids):
        raise HTTPException(status_code=404, detail="所选位置已变化，请返回台账重新选择")
    ordered_rows = [rows_by_id[location_id] for location_id in ordered_ids]
    for row in ordered_rows:
        _require_printable_location_label(row)
    lan_ip = _lan_ip()
    return {
        "items": [_location_label_dict(row, request, lan_ip) for row in ordered_rows],
        "count": len(ordered_rows),
    }


@router.get("/locations/{location_id}/label")
def get_location_label(
    location_id: int,
    request: Request,
    db: Session = Depends(get_db),
    _user: User = Depends(can_read),
) -> dict:
    row = db.scalar(
        select(WarehouseLocation)
        .options(selectinload(WarehouseLocation.floor3_layout))
        .where(WarehouseLocation.id == location_id)
    )
    if row is None:
        raise HTTPException(status_code=404, detail="位置不存在")
    return _location_label_dict(row, request)


@router.get("/location-candidates")
def list_location_candidates(
    inventory_type: Literal["finished", "semi_finished"] = "finished",
    empty_only: bool = False,
    pallet_storage_only: bool = False,
    include_hierarchy: bool = True,
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
                "specification": " × ".join(
                    str(round(value))
                    for value in (row.length_mm, row.width_mm, row.height_mm)
                    if value is not None
                ),
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
    if allowed_customer_ids is not None and not visible_products:
        raise HTTPException(status_code=403, detail="无客户访问权限")


def _mold_tool_dict(
    row: MoldTool,
    allowed_customer_ids: set[int] | None = None,
    *,
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
    return {
        "id": row.id,
        "mold_code": row.mold_code,
        "mold_name": row.mold_name,
        "rack_location": row.rack_location,
        "location_guide": describe_mold_location(row.rack_location),
        "location_version": row.location_version,
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
        "products": [
            {
                "id": product.id,
                "customer_id": product.customer_id,
                "customer_code": product.customer.customer_code if product.customer else None,
                "customer_name": product.customer.name if product.customer else None,
                "product_code": product.product_code,
                "product_name": product.product_name,
                "customer_material_code": product.customer_material_code,
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
                "production_process": product.production_process,
                "direction_note": product.report_notes,
            }
            for product in products
        ],
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
    }


def _mold_tools_query(
    *,
    db: Session,
    user: User,
    q: str | None,
    include_inactive: bool,
):
    allowed_customer_ids = _mold_customer_scope(user, db)
    query = select(MoldTool).options(
        selectinload(MoldTool.products).selectinload(Product.customer)
    )
    if allowed_customer_ids is not None:
        query = query.where(
            MoldTool.id.in_(
                select(Product.mold_tool_id).where(
                    Product.mold_tool_id.is_not(None),
                    Product.deleted_at.is_(None),
                    Product.is_active.is_(True),
                    Product.customer_id.in_(allowed_customer_ids),
                )
            )
        )
    if not include_inactive:
        query = query.where(
            or_(
                MoldTool.is_active.is_(True),
                MoldTool.archive_status == "archived",
            )
        )
    keyword = (q or "").strip()
    if keyword:
        pattern = f"%{keyword}%"
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
                ),
            )
        )
        query = query.where(
            or_(
                MoldTool.mold_code.like(pattern),
                MoldTool.mold_name.like(pattern),
                MoldTool.rack_location.like(pattern),
                MoldTool.remarks.like(pattern),
                MoldTool.id.in_(linked_molds),
            )
        )
    return query, allowed_customer_ids


@router.get("/molds")
def list_mold_tools(
    q: str | None = None,
    include_inactive: bool = False,
    limit: int = Query(default=200, ge=1, le=500),
    page: int | None = Query(default=None, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    query, allowed_customer_ids = _mold_tools_query(
        db=db,
        user=user,
        q=q,
        include_inactive=include_inactive,
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
    return {
        "items": [
            _mold_tool_dict(row, allowed_customer_ids)
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
        .options(selectinload(MoldTool.products).selectinload(Product.customer))
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


@router.get("/molds/by-map-area")
def list_mold_tools_by_map_area(
    floor_code: Literal["1F", "3F"] = Query(default="1F"),
    feature_code: str = Query(min_length=1, max_length=100),
    q: str | None = Query(default=None, max_length=150),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(_can_locate_twin),
) -> dict:
    """List real mold masters assigned to one measured-map mold area."""

    try:
        floor = load_warehouse_twin_floor(floor_code)
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
    _user: User = Depends(can_read),
) -> dict:
    return {
        "floor_code": "1F",
        "position_order": None,
        "position_numbers_are_dynamic": False,
        "storage_rule": "rack_level_grid",
        "racks": one_floor_mold_location_options(),
    }


@router.post("/molds/location-movement/preview")
def preview_mold_location_movement(
    payload: MoldLocationPreviewPayload,
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    try:
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
        if not result.replayed and not result.no_change and result.movement is not None:
            db.add(
                OperationLog(
                    user_id=user.id,
                    username=user.username,
                    role=user.role,
                    action="UPDATE",
                    resource=f"warehouse/molds/{result.mold.id}/location",
                    entity_type="mold_tool",
                    entity_id=result.mold.id,
                    description="双码确认模具位置移动",
                    details=json.dumps(
                        {
                            "movement_id": result.movement.id,
                            "mold_code": result.movement.mold_code_snapshot,
                            "from_location": result.movement.from_location,
                            "to_location": result.movement.to_location,
                            "expected_version": result.movement.expected_version,
                            "resulting_version": result.movement.resulting_version,
                            "idempotency_key": result.movement.idempotency_key,
                            "source": result.movement.source,
                            "note": result.movement.note,
                        },
                        ensure_ascii=False,
                    ),
                    ip_address=request.client.host if request.client else None,
                    user_agent=request.headers.get("user-agent"),
                )
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


def _lan_ip() -> str:
    connection = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        connection.connect(("8.8.8.8", 80))
        return connection.getsockname()[0]
    except OSError:
        return socket.gethostbyname(socket.gethostname())
    finally:
        connection.close()


def _mold_label_dict(
    row: MoldTool,
    request: Request,
    allowed_customer_ids: set[int] | None,
    lan_ip: str | None = None,
) -> dict:
    port = request.url.port or 8000
    lookup_url = (
        f"http://{lan_ip or _lan_ip()}:{port}/mobile/mold-lookup"
        f"?mold={quote(row.mold_code, safe='')}"
    )
    image = qrcode.make(lookup_url)
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return {
        **_mold_tool_dict(row, allowed_customer_ids),
        "lookup_url": lookup_url,
        "qr_data_url": (
            "data:image/png;base64,"
            + base64.b64encode(buffer.getvalue()).decode("ascii")
        ),
    }


@router.get("/molds/labels")
def get_mold_labels(
    request: Request,
    mold_ids: str = Query(min_length=1, max_length=1200),
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    raw_ids = [part.strip() for part in mold_ids.split(",") if part.strip()]
    if not raw_ids or any(not part.isdigit() or int(part) <= 0 for part in raw_ids):
        raise HTTPException(status_code=422, detail="模具批量标签参数无效")
    ordered_ids = list(dict.fromkeys(int(part) for part in raw_ids))
    if len(ordered_ids) > 100:
        raise HTTPException(status_code=422, detail="一次最多打印 100 件模具")
    rows = db.scalars(
        select(MoldTool)
        .options(selectinload(MoldTool.products).selectinload(Product.customer))
        .where(MoldTool.id.in_(ordered_ids))
    ).unique().all()
    rows_by_id = {row.id: row for row in rows}
    missing_ids = [mold_id for mold_id in ordered_ids if mold_id not in rows_by_id]
    if missing_ids:
        raise HTTPException(status_code=404, detail="所选模具已变化，请返回列表重新选择")
    allowed_customer_ids = _mold_customer_scope(user, db)
    ordered_rows = [rows_by_id[mold_id] for mold_id in ordered_ids]
    for row in ordered_rows:
        _require_mold_customer_scope(row, allowed_customer_ids)
    lan_ip = _lan_ip()
    return {
        "items": [
            _mold_label_dict(row, request, allowed_customer_ids, lan_ip)
            for row in ordered_rows
        ],
        "count": len(ordered_rows),
    }


@router.get("/molds/{mold_id}/label")
def get_mold_label(
    mold_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    row = db.scalar(
        select(MoldTool)
        .options(selectinload(MoldTool.products).selectinload(Product.customer))
        .where(MoldTool.id == mold_id)
    )
    if row is None:
        raise HTTPException(status_code=404, detail="模具不存在")
    allowed_customer_ids = _mold_customer_scope(user, db)
    _require_mold_customer_scope(row, allowed_customer_ids)
    return _mold_label_dict(row, request, allowed_customer_ids)


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
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    with _MOLD_CODE_WRITE_LOCK:
        values = payload.model_dump(exclude={"customer_initials"})
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
        row = MoldTool(**values, created_by=user.id, updated_by=user.id)
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
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    row = db.get(MoldTool, mold_id)
    if row is None:
        raise HTTPException(status_code=404, detail="模具不存在")
    if row.archive_status == "archived":
        raise HTTPException(status_code=409, detail="封存模具不能直接编辑，请先按现场搬回后恢复启用")
    if payload.rack_location.strip() != row.rack_location.strip():
        raise HTTPException(
            status_code=409,
            detail="模具位置不能在档案编辑中直接修改，请使用模具码 + 位置码双码移动确认",
        )
    if payload.mold_code and payload.mold_code != row.mold_code:
        raise HTTPException(status_code=409, detail="模具编号生成后不可在档案编辑中修改")
    values = payload.model_dump(exclude={"customer_initials", "mold_code"})
    for key, value in values.items():
        setattr(row, key, value)
    row.updated_by = user.id
    try:
        db.commit()
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(status_code=409, detail="模具编号已存在") from error
    db.refresh(row)
    return _mold_tool_dict(row)


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
    mold = db.get(MoldTool, mold_id)
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
        .options(selectinload(MoldTool.products).selectinload(Product.customer))
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
                Customer.name.like(pattern),
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
                "specification": " × ".join(
                    str(round(value))
                    for value in (
                        product.length_mm,
                        product.width_mm,
                        product.height_mm,
                    )
                    if value is not None
                ),
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
    return _location_dict(row)


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
    return _location_dict(row)


@router.put("/locations/{location_id}/enable")
def enable_location(
    location_id: int,
    db: Session = Depends(get_db),
    _user: User = Depends(admin_only),
) -> dict:
    row = db.get(WarehouseLocation, location_id)
    if row is None:
        raise HTTPException(status_code=404, detail="库位不存在")
    _reject_v11_location_configuration(row)
    row.is_active = True
    db.commit()
    return _location_dict(row)


@router.put("/locations/{location_id}/disable")
def disable_location(
    location_id: int,
    db: Session = Depends(get_db),
    _user: User = Depends(admin_only),
) -> dict:
    row = db.get(WarehouseLocation, location_id)
    if row is None:
        raise HTTPException(status_code=404, detail="库位不存在")
    _reject_v11_location_configuration(row)
    row.is_active = False
    db.commit()
    return _location_dict(row)


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
    query = _lot_query()
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
            length_mm, width_mm, height_mm = (int(part) for part in spec_parts)
        except ValueError as error:
            raise HTTPException(
                status_code=422,
                detail="成品规格请按 长×宽×高（毫米）填写",
            ) from error
        if len(spec_parts) != 3 or min(length_mm, width_mm, height_mm) <= 0:
            raise HTTPException(status_code=422, detail="成品规格请按 长×宽×高（毫米）填写")
        finished_conditions.extend(
            (
                FinishedGoodsInventoryDetail.length_mm == length_mm,
                FinishedGoodsInventoryDetail.width_mm == width_mm,
                FinishedGoodsInventoryDetail.height_mm == height_mm,
            )
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
    return {
        "items": [
            _lot_dict(row, time_archive=time_archives.get(row.id)) for row in rows
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
    if has_permission(user, "cost.view"):
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
    result = _lot_dict(
        row,
        time_archive=archive,
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
    port = request.url.port or 8000
    lookup_url = (
        f"http://{_lan_ip()}:{port}/static/finished-goods-label.html"
        f"?lot_id={row.id}&version={row.version}&view=validate"
    )
    image = qrcode.make(lookup_url)
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return {
        **_lot_dict(row),
        "label_version": row.version,
        "label_status": label_status,
        "physical_quantity": physical_quantity,
        "source_reference": {
            "type": row.source_ref_type or row.source_type,
            "id": row.source_ref_id,
            "label": source_labels.get(row.source_type, "库存来源"),
        },
        "lookup_url": lookup_url,
        "qr_data_url": (
            "data:image/png;base64,"
            + base64.b64encode(buffer.getvalue()).decode("ascii")
        ),
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
        return _lot_dict(row)
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
        return _lot_dict(row)
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
        return _lot_dict(row)
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
        return _lot_dict(row)
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
        return _lot_dict(row)
    except WarehouseInventoryError as error:
        db.rollback()
        _handle(error)
    except IntegrityError as error:
        db.rollback()
        _handle_integrity(error)


def _lot_location_transfer_dict(
    row: InventoryLotTransfer,
    *,
    source_lot: InventoryLot,
    target_lot: InventoryLot,
    replayed: bool,
) -> dict:
    return {
        "id": row.id,
        "quantity": row.quantity,
        "available_quantity": row.available_quantity,
        "reserved_quantity": row.reserved_quantity,
        "source_location_id": row.source_location_id,
        "target_location_id": row.target_location_id,
        "transferred_at": utc_naive_to_api(row.transferred_at),
        "replayed": replayed,
        "source_lot": _lot_dict(source_lot),
        "target_lot": _lot_dict(target_lot),
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
        return _lot_dict(row)
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
