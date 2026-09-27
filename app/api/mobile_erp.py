from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
import json
import mimetypes
from pathlib import PurePath
import re
from typing import Literal
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi import Body, APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import String, and_, case, cast, exists, func, or_, select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, aliased, selectinload

from app.api.deps import (
    PermissionChecker,
    customer_scope_ids,
    effective_permissions,
    get_db,
    get_current_user,
    has_permission,
    has_unrestricted_customer_access,
)
from app.api.incoming import (
    _incoming_row_response,
    _pending_incoming_route_rows,
    _received_rows,
    _rows as _incoming_rows,
)
from app.core.time_contract import (
    beijing_date_bounds_utc_naive,
    beijing_today,
    utc_now_naive,
    utc_naive_to_api,
)
from app.models.customer import Customer
from app.models.mold_tool import MoldTool, MoldToolCustomer
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.product_drawing import ProductDrawing
from app.models.production import ProductionTask
from app.models.product_bom import (
    RequisitionItemBomSource,
    SalesOrderItemBomComponent,
)
from app.models.user import User
from app.services.production_workflow import (
    find_pending_production_task_lookup_rows,
    list_production_tasks,
    list_production_task_dashboard_rows,
    list_production_station_task_ids,
)
from app.services.box_type_rules import box_type_code
from app.services.product_drawings import default_product_drawing, engineering_drawing_condition
from app.services.order_status_policy import ORDER_ITEM_ACTIVE_ORDER_STATUSES
from app.services.printing_colors import parse_printing_colors
from app.services.product_specification import (
    dimension_specification,
    product_dimension_specification,
)
from app.services.secure_uploads import resolve_stored_reference, stored_file_metadata
from app.services.ui_layout_settings import LAYOUT_ROLES, effective_layout
from app.models.warehouse_inventory import (
    FinishedGoodsInventoryDetail,
    InventoryLot,
    InventoryPalletItem,
    InventoryReservation,
    SemiFinishedInventoryDetail,
    SemiFinishedLotAllowedProduct,
    Floor3LocationLayout,
    InventoryLotTransfer,
    WarehouseArea,
    WarehouseLocation,
    WarehouseLocationDiscrepancy,
    WarehouseUnmatchedInventoryObservation,
)
from app.services.warehouse_location_address import (
    employee_area_name,
    location_address_payload,
    rack_cell_identity_payload,
)
from app.services.audit_log import append_audit_event
from app.services.location_candidates import (
    claim_warehouse_floor_projection,
    current_same_location_pallet,
    load_warehouse_location_projection_contexts,
    list_operational_locations,
    operational_location_payload,
    warehouse_location_projection,
)
from app.services.mold_location import describe_mold_location
from app.services.warehouse_floor1_candidate_planner import overlay_formal_area_bindings
from app.services.warehouse_inventory import (
    WarehouseInventoryError,
    transfer_finished_lot_between_locations,
)
from app.services.warehouse_twin_layout import (
    WarehouseTwinLayoutNotFoundError,
    load_warehouse_twin_floor,
)


router = APIRouter()
from app.api.mobile_stock_use import router as mobile_stock_use_router
router.include_router(mobile_stock_use_router)
can_read_inventory = PermissionChecker("warehouse.view")
can_execute_inventory = PermissionChecker("warehouse.execute")
can_correct_inventory = PermissionChecker("warehouse.correct")
can_read_orders = PermissionChecker("orders.view")
can_read_incoming = PermissionChecker("incoming.view")
_BEIJING = ZoneInfo("Asia/Shanghai")
_MOBILE_SEARCH_CATEGORIES = (
    "orders",
    "materials",
    "molds",
    "production",
    "inventory",
)
_MOBILE_SEARCH_LABELS = {
    "orders": "订单",
    "materials": "待收材料",
    "molds": "模具",
    "production": "生产任务",
    "inventory": "产品与库存",
}


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "private, no-store"


def _mobile_search_categories(user: User) -> list[str]:
    permissions = effective_permissions(user)
    categories: list[str] = []
    if "orders.view" in permissions:
        categories.append("orders")
    if "incoming.view" in permissions:
        categories.append("materials")
    if (
        "warehouse.view" in permissions
        or "production.die_cut.view" in permissions
    ):
        categories.append("molds")
    if (
        "orders.view" in permissions
        and (
            "production.printing.view" in permissions
            or "production.die_cut.view" in permissions
        )
    ):
        categories.append("production")
    if "warehouse.view" in permissions:
        categories.append("inventory")
    return [category for category in _MOBILE_SEARCH_CATEGORIES if category in categories]


@router.get("/shell")
def mobile_shell(
    response: Response,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """Return the small, permission-derived mobile entry contract.

    This endpoint deliberately returns no business list.  Each operational
    entry loads its own data only after the employee opens it.
    """

    _no_store(response)
    permissions = effective_permissions(user)
    if user.role in LAYOUT_ROLES:
        layout = effective_layout(
            db,
            role_code=user.role,
            display_mode="mobile",
            permissions=permissions,
        )
        visible_layout_ids = {
            item["id"] for item in layout["layout"].get("menus", [])
        }
        layout_version = layout["version"]
    else:
        visible_layout_ids = {"mobile_home", "mobile_search", "mobile_production"}
        layout_version = 0
    printing_allowed = "production.printing.view" in permissions
    die_cut_allowed = "production.die_cut.view" in permissions
    delivery_margin_allowed = user.role in {"admin", "boss"} and all(
        permission in permissions
        for permission in ("dashboard.view", "finance.view", "cost.view")
    )
    entries: list[dict] = []
    search_categories = _mobile_search_categories(user)

    if search_categories:
        entries.append(
            {
                "id": "lookup",
                "label": "现场查询",
                "summary": "订单、材料、模具、生产和库存按权限统一查找",
                "categories": search_categories,
                "can_execute": False,
            }
        )

    if "incoming.view" in permissions:
        entries.append(
            {
                "id": "incoming",
                "label": "收料",
                "summary": "待收明细进入后按需读取",
                "can_execute": "incoming.execute" in permissions,
            }
        )
    if (
        "warehouse.view" in permissions
        and "mobile_search" in visible_layout_ids
    ):
        entries.append(
            {
                "id": "warehouse",
                "label": "仓库",
                "summary": "产品、库存与真实位置按需读取",
                "can_execute": "warehouse.execute" in permissions,
                "can_correct": "warehouse.correct" in permissions,
            }
        )
    if (
        (printing_allowed or die_cut_allowed)
        and "mobile_production" in visible_layout_ids
    ):
        stations = [
            station
            for station, allowed in (
                ("printing", printing_allowed),
                ("die_cut", die_cut_allowed),
            )
            if allowed
        ]
        entries.append(
            {
                "id": "production",
                "label": "生产",
                "summary": "、".join(
                    "印刷工位" if station == "printing" else "模切工位"
                    for station in stations
                ),
                "stations": stations,
                "can_execute": False,
            }
        )
    if "deliveries.pick" in permissions or "deliveries.execute" in permissions:
        entries.append(
            {
                "id": "pre_delivery",
                "label": "预送货",
                "summary": "本人拿货任务进入后按需读取",
                "can_execute": "deliveries.pick" in permissions,
                "can_manage": "deliveries.execute" in permissions,
            }
        )

    return {
        "user": {
            "id": user.id,
            "username": user.username,
            "display_name": user.display_name or user.real_name or user.username,
            "role": user.role,
        },
        "entries": entries,
        "search_categories": search_categories,
        "management_summary_allowed": "dashboard.view" in permissions,
        "delivery_margin_allowed": delivery_margin_allowed,
        "layout_version": layout_version,
        "as_of": datetime.now(_BEIJING).isoformat(timespec="seconds"),
        "read_only": True,
    }


def _visible_customer_ids(user: User, db: Session) -> set[int] | None:
    if has_unrestricted_customer_access(user, db):
        return None
    return customer_scope_ids(user, db)


def _require_mobile_production_station(user: User) -> None:
    if not (
        has_permission(user, "production.printing.view")
        or has_permission(user, "production.die_cut.view")
    ):
        raise HTTPException(status_code=403, detail="当前账号没有手机生产工位查看权限")


def _require_exact_mobile_production_station(
    user: User,
    station: Literal["printing", "die_cut"],
) -> None:
    permission = {
        "printing": "production.printing.view",
        "die_cut": "production.die_cut.view",
    }[station]
    if not has_permission(user, permission):
        label = "印刷" if station == "printing" else "模切"
        raise HTTPException(status_code=403, detail=f"当前账号没有手机{label}工位查看权限")


def _production_period_bounds(
    period: Literal["today", "3d", "7d", "custom"],
    *,
    date_from: date | None,
    date_to: date | None,
) -> tuple[date, date, datetime, datetime]:
    today = beijing_today()
    if period == "custom":
        if date_from is None or date_to is None:
            raise HTTPException(status_code=422, detail="自定义日期必须同时填写开始和结束日期")
        if date_from > date_to:
            raise HTTPException(status_code=422, detail="开始日期不能晚于结束日期")
        start_date, end_date = date_from, date_to
    else:
        days = {"today": 1, "3d": 3, "7d": 7}[period]
        start_date, end_date = today - timedelta(days=days - 1), today
    start_utc, _ = beijing_date_bounds_utc_naive(start_date)
    _, end_utc = beijing_date_bounds_utc_naive(end_date)
    return start_date, end_date, start_utc, end_utc


def _task_status_text(status: str) -> str:
    return {
        "waiting_material": "材料未齐",
        "pending": "可以生产",
        "completed": "已经完工",
        "not_required": "无需生产",
    }.get(status, status or "状态未知")


def _safe_production_material_sources(task: dict) -> list[dict]:
    return [{key: source.get(key) for key in (
        'display_name', 'material_kind', 'board_length_mm', 'board_width_mm', 'flute_type',
        'location_name', 'remaining_sheet_quantity', 'remaining_product_quantity',
        'consumed_sheet_quantity', 'unit', 'recorded_processing', 'has_cut_plan',
    )} for source in task.get('customer_board_preparation_sources', [])]


def _safe_production_task(db: Session, task: dict, *, drawing_path: str | None, user: User) -> dict:
    """Expose workshop facts only; supplier material codes and prices stay private."""

    from app.services.drawing_binding import bound_task_release
    release = bound_task_release(db, int(task["id"]))
    if release is not None:
        drawing_path = (
            f"/api/mobile/erp/production/tasks/{task['id']}/drawing"
            if task.get("status") == "pending" and (
                has_permission(user, "production.printing.view")
                or has_permission(user, "production.die_cut.view")
            ) else None
        )
        mold = db.get(MoldTool, release.mold_tool_id) if release.mold_tool_id else None
        released_mold = json.loads(release.manifest_json).get("mold_snapshot") or {}
        task = {**task, "mold_name": released_mold.get("name"),
                "mold_location": mold.rack_location if mold else None}
    planned_output = max(int(task.get("planned_output_quantity") or 0), 0)
    receipt_purpose_managed = task.get("receipt_purpose_managed") is True
    parent_order_quantity = max(
        int(task.get("parent_order_quantity") or task.get("order_quantity") or 0),
        0,
    )
    delivered_quantity = max(int(task.get("delivered_quantity") or 0), 0)
    fully_delivered = (
        parent_order_quantity > 0 and delivered_quantity >= parent_order_quantity
    )
    if task.get("status") == "completed":
        status_text = "已送完" if fully_delivered else "已完工待送"
    elif receipt_purpose_managed:
        status_text = "收料自动推进"
    else:
        status_text = _task_status_text(task["status"])
    return {
        "task_id": task["id"],
        "status": task["status"],
        "status_text": status_text,
        "is_fully_delivered": fully_delivered,
        "order_number": task.get("order_number"),
        "customer_po": task.get("customer_po") or task.get("customer_order_number"),
        "item_order_number": task.get("item_order_number"),
        "product_code": task.get("product_code"),
        "product_name": task.get("product_name"),
        "carton_specification": task.get("specification"),
        "flute_type": task.get("flute"),
        "order_quantity": task.get("ordered_quantity"),
        "received_material_quantity": task.get("material_received_quantity"),
        "current_producible_quantity": planned_output,
        "output_unit": task.get('output_unit') or ('片' if task.get('is_component_task') else '只'),
        "material_sources": _safe_production_material_sources(task),
        "planned_output_quantity": planned_output,
        "actual_output_quantity": max(
            int(task.get("actual_output_quantity") or 0),
            0,
        ),
        "receipt_purpose_managed": receipt_purpose_managed,
        "receipt_purpose_summary": task.get("receipt_purpose_summary") or {},
        "completion_actionable": task.get("completion_actionable") is not False,
        "completion_block_code": task.get("completion_block_code"),
        "completion_block_message": task.get("completion_block_message"),
        "cutting_mode": task.get("special_process"),
        "production_process": task.get("production_process"),
        "production_notes": task.get("production_notes"),
        "mold_name": task.get("mold_name"),
        "mold_location": task.get("mold_location"),
        "mold_location_display": (
            describe_mold_location(task["mold_location"])["prompt"]
            if task.get("mold_location")
            else None
        ),
        "printing_plate_mode": task.get("printing_plate_mode"),
        "print_content": task.get("print_content"),
        "printing_situation": task.get("printing_situation") or task.get("print_content"),
        "printing_colors": task.get("printing_colors") or [],
        "printing_colors_frozen": task.get("printing_colors_frozen") is True,
        "printing_plate_codes": task.get("printing_plate_codes") or [],
        "printing_plates": task.get("printing_plates") or [],
        "plate_alignment_value_mm": task.get("plate_alignment_value_mm"),
        "plate_mount_value_mm": task.get("plate_mount_value_mm"),
        "machine_set_length_mm": task.get("machine_set_length_mm"),
        "machine_set_width_mm": task.get("machine_set_width_mm"),
        "machine_set_height_mm": task.get("machine_set_height_mm"),
        "printing_instruction": task.get("printing_instruction"),
        "drawing_path": drawing_path,
        "is_component_task": task.get("is_component_task") is True,
    }


def _drawing_suffix(value: str | None) -> str:
    suffix = PurePath(str(value or "").replace("\\", "/")).suffix.lower()
    return suffix if suffix in {".jpg", ".jpeg", ".png", ".webp", ".pdf"} else ".bin"


def _production_station_process_tags(*values: object) -> list[str]:
    text = " / ".join(str(value or "") for value in values)
    tags: list[str] = []
    for label, tokens in (
        ("印刷", ("印刷", "水墨")),
        ("粘贴", ("粘贴", "粘箱", "糊盒", "粘合")),
        ("打钉", ("打钉", "钉箱", "钉合")),
        ("模切", ("模切", "啤")),
    ):
        if any(token in text for token in tokens):
            tags.append(label)
    return tags


def _production_station_task_payloads(
    db: Session,
    *,
    tasks: list[dict],
    station: Literal["printing", "die_cut"],
    mold_map_allowed: bool,
) -> list[dict]:
    """Project one current page into station-safe, read-only task cards."""

    if not tasks:
        return []
    order_item_ids = {int(task["order_item_id"]) for task in tasks}
    product_ids = {
        int(task["product_id"])
        for task in tasks
        if task.get("product_id") is not None
    }
    component_ids = {
        int(task["bom_component_snapshot_id"])
        for task in tasks
        if task.get("bom_component_snapshot_id") is not None
    }
    order_items = {
        int(row.id): row
        for row in db.scalars(
            select(OrderItem).where(OrderItem.id.in_(order_item_ids))
        ).all()
    }
    products = {
        int(row.id): row
        for row in db.scalars(select(Product).where(Product.id.in_(product_ids))).all()
    }
    components = (
        {
            int(row.id): row
            for row in db.scalars(
                select(SalesOrderItemBomComponent).where(
                    SalesOrderItemBomComponent.id.in_(component_ids)
                )
            ).all()
        }
        if component_ids
        else {}
    )
    drawings: dict[int, ProductDrawing] = {}
    if product_ids:
        for drawing in db.scalars(
            select(ProductDrawing)
            .where(ProductDrawing.product_id.in_(product_ids), engineering_drawing_condition())
            .order_by(
                ProductDrawing.product_id,
                ProductDrawing.uploaded_at.desc(),
                ProductDrawing.id.desc(),
            )
        ).all():
            drawings.setdefault(int(drawing.product_id), drawing)

    from app.services.drawing_binding import bound_task_release
    releases = {int(task["id"]): bound_task_release(db, int(task["id"])) for task in tasks}
    mold_ids: set[int] = set()
    for task in tasks:
        product = products.get(int(task.get("product_id") or 0))
        component = components.get(int(task.get("bom_component_snapshot_id") or 0))
        mold_id = (
            component.snapshot_mold_tool_id
            if component is not None
            else product.mold_tool_id
            if product is not None
            else None
        )
        release = releases[int(task["id"])]
        if release is not None:
            mold_id = release.mold_tool_id
        if mold_id is not None:
            mold_ids.add(int(mold_id))
    molds = (
        {
            int(row.id): row
            for row in db.scalars(select(MoldTool).where(MoldTool.id.in_(mold_ids))).all()
        }
        if mold_ids
        else {}
    )

    payloads: list[dict] = []
    for task in tasks:
        item = order_items.get(int(task["order_item_id"]))
        product = products.get(int(task.get("product_id") or 0))
        component = components.get(int(task.get("bom_component_snapshot_id") or 0))
        mold_id = (
            component.snapshot_mold_tool_id
            if component is not None
            else product.mold_tool_id
            if product is not None
            else None
        )
        release = releases[int(task["id"])]
        if release is not None:
            mold_id = release.mold_tool_id
        mold = molds.get(int(mold_id or 0))
        released_mold = (json.loads(release.manifest_json).get("mold_snapshot") or {}) if release else {}
        mold_code = (
            released_mold.get("code") if release else
            component.snapshot_mold_tool_code
            if component is not None
            else mold.mold_code
            if mold is not None
            else None
        )
        mold_name = (
            released_mold.get("name") if release else
            mold.mold_name
            if mold is not None
            else component.snapshot_mold_tool_name
            if component is not None
            else task.get("mold_name")
        )
        drawing_url = None
        drawing_kind = None
        drawing_reference = None
        if component is None and item is not None and item.drawing_file:
            drawing_reference = item.drawing_file
            suffix = _drawing_suffix(drawing_reference)
            drawing_kind = "pdf" if suffix == ".pdf" else "image"
        else:
            drawing = drawings.get(int(task.get("product_id") or 0))
            if drawing is not None:
                drawing_reference = drawing.image_path
                suffix = _drawing_suffix(drawing_reference)
                drawing_kind = "pdf" if suffix == ".pdf" else "image"
            elif component is not None and component.snapshot_die_cut_path:
                drawing_reference = component.snapshot_die_cut_path
                suffix = _drawing_suffix(drawing_reference)
                drawing_kind = "pdf" if suffix == ".pdf" else "image"
        if release is not None:
            drawing_kind = "pdf"
        if drawing_reference or release is not None:
            drawing_url = f"/api/mobile/erp/production/tasks/{task['id']}/drawing"

        if component is not None:
            report_length = component.snapshot_component_report_length_mm
            report_width = component.snapshot_component_report_width_mm
            crease_type = component.snapshot_component_crease_type
            crease_values = [
                component.snapshot_component_crease_left_mm,
                component.snapshot_component_crease_middle_mm,
                component.snapshot_component_crease_right_mm,
            ]
        else:
            report_length = (
                item.snapshot_report_length_mm or item.cardboard_len
                if item is not None
                else None
            )
            report_width = (
                item.snapshot_report_width_mm or item.cardboard_width
                if item is not None
                else None
            )
            crease_type = item.snapshot_crease_type if item is not None else None
            crease_values = [
                item.snapshot_crease_left_mm if item is not None else None,
                item.snapshot_crease_middle_mm if item is not None else None,
                item.snapshot_crease_right_mm if item is not None else None,
            ]

        common = {
            "task_id": int(task["id"]),
            "material_sources": _safe_production_material_sources(task),
            "output_unit": task.get('output_unit') or ('片' if task.get('is_component_task') else '只'),
            "task_version": int(task.get("version") or 1),
            "customer_name": task.get("customer_name"),
            "order_number": task.get("order_number"),
            "customer_po": task.get("customer_po") or task.get("customer_order_number"),
            "item_order_number": task.get("item_order_number"),
            "product_code": task.get("product_code"),
            "product_name": task.get("product_name"),
            "carton_specification": task.get("specification"),
            "order_quantity": int(task.get("ordered_quantity") or 0),
            "planned_output_quantity": max(
                int(task.get("planned_output_quantity") or 0),
                0,
            ),
            "actual_output_quantity": max(
                int(task.get("actual_output_quantity") or 0),
                0,
            ),
            "receipt_purpose_managed": task.get("receipt_purpose_managed") is True,
            "receipt_purpose_summary": task.get("receipt_purpose_summary") or {},
            "completion_actionable": task.get("completion_actionable") is not False,
            "completion_block_code": task.get("completion_block_code"),
            "completion_block_message": task.get("completion_block_message"),
            "drawing_path": drawing_url,
            "drawing_kind": drawing_kind,
            "is_component_task": task.get("is_component_task") is True,
            "process_tags": _production_station_process_tags(
                task.get("production_process"),
                task.get("production_notes"),
                task.get("special_process"),
            ),
        }
        if station == "printing":
            common.update(
                {
                    "carton_length_mm": _number_text(product.length_mm) if product else None,
                    "carton_width_mm": _number_text(product.width_mm) if product else None,
                    "carton_height_mm": _number_text(product.height_mm) if product else None,
                    "report_length_mm": _number_text(report_length),
                    "report_width_mm": _number_text(report_width),
                    "crease_type": crease_type,
                    "crease_values_mm": [
                        _number_text(value) for value in crease_values if value is not None
                    ],
                    "print_content": task.get("print_content"),
                    "printing_situation": task.get("printing_situation")
                    or task.get("print_content"),
                    "printing_colors": task.get("printing_colors") or [],
                    "printing_colors_frozen": (
                        task.get("printing_colors_frozen") is True
                    ),
                    "printing_method": task.get("printing_plate_mode"),
                    "printing_instruction": task.get("printing_instruction"),
                    "printing_plate_codes": task.get("printing_plate_codes") or [],
                    "printing_plates": task.get("printing_plates") or [],
                    "plate_alignment_value_mm": task.get(
                        "plate_alignment_value_mm"
                    ),
                    "plate_mount_value_mm": task.get("plate_mount_value_mm"),
                    "machine_set_length_mm": task.get("machine_set_length_mm"),
                    "machine_set_width_mm": task.get("machine_set_width_mm"),
                    "machine_set_height_mm": task.get("machine_set_height_mm"),
                    "cutting_mode": task.get("special_process"),
                }
            )
        else:
            mold_active = mold.is_active if mold is not None else None
            common.update(
                {
                    "material": task.get("material"),
                    "flute_type": task.get("flute"),
                    "mold_display_name": mold_name,
                    "mold_name": mold_name,
                    "mold_location": mold.rack_location if mold is not None else None,
                    "mold_location_display": (
                        describe_mold_location(mold.rack_location)["prompt"]
                        if mold is not None
                        else None
                    ),
                    "mold_is_active": mold_active,
                    "mold_archive_status": mold.archive_status if mold is not None else None,
                    "mold_warning": (
                        "模具已封存待复用；必须先搬回一楼正式模具位并由管理员或老板恢复启用"
                        if mold is not None and mold.archive_status == "archived"
                        else "模具已停用，禁止直接生产；请联系管理员受控启用"
                        if mold_active is False
                        else "模具主档未找到，请先核对模具"
                        if mold_code and mold is None
                        else None
                    ),
                    "mold_map_url": (
                        f"/mobile/mold-lookup?mold_id={int(mold_id)}&readonly=1"
                        if mold_map_allowed and mold_id
                        else None
                    ),
                    "cutting_mode": task.get("special_process"),
                }
            )
        payloads.append(common)
    return payloads


def _escaped_like(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _number_text(value) -> str | None:
    if value is None:
        return None
    number = float(value)
    return str(int(number)) if number.is_integer() else f"{number:g}"


def _dimension_decimal(value) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _incoming_dimension_pair(value: str) -> tuple[str, str] | None:
    match = re.fullmatch(
        r"\s*([0-9]+(?:\.[0-9]+)?)\s*[xX×*]\s*([0-9]+(?:\.[0-9]+)?)\s*",
        value,
    )
    if match is None:
        return None
    length = _dimension_decimal(match.group(1))
    width = _dimension_decimal(match.group(2))
    if length is None or width is None:
        return None
    return _number_text(length), _number_text(width)


def _incoming_search_match(
    row: dict,
    *,
    keyword: str,
    dimension_mode: Literal["any", "length", "width"],
) -> dict | None:
    normalized = keyword.strip().casefold()
    requested_dimension = _dimension_decimal(keyword.strip())
    requested_pair = _incoming_dimension_pair(keyword)
    dimension_fragment = (
        _number_text(requested_dimension) if requested_dimension is not None else None
    )
    length_text = _number_text(row.get("cardboard_len"))
    width_text = _number_text(row.get("cardboard_width"))
    dimension_hits: list[str] = []
    if requested_pair is not None and dimension_mode == "any":
        requested_length, requested_width = requested_pair
        if (
            length_text is not None
            and width_text is not None
            and requested_length in length_text.casefold()
            and requested_width in width_text.casefold()
        ):
            dimension_hits.extend(("length", "width"))
    elif requested_dimension is not None:
        if (
            length_text is not None
            and dimension_fragment in length_text.casefold()
            and dimension_mode in {"any", "length"}
        ):
            dimension_hits.append("length")
        if (
            width_text is not None
            and dimension_fragment in width_text.casefold()
            and dimension_mode in {"any", "width"}
        ):
            dimension_hits.append("width")

    if dimension_mode in {"length", "width"}:
        if requested_dimension is None:
            return None
        text_hits: list[str] = []
        matched = bool(dimension_hits)
    else:
        text_hits = []
        if requested_dimension is None and requested_pair is None:
            customer_fields = (
                ("customer_name", row.get("customer_name")),
                ("customer_code", row.get("customer_code")),
            )
            text_hits.extend(
                field
                for field, value in customer_fields
                if normalized in str(value or "").casefold()
            )
        matched = bool(text_hits or dimension_hits)
    if not matched:
        return None

    side_labels = {
        "length": "报料长",
        "width": "报料宽",
    }
    summaries = [
        f"{side_labels[side]} {_number_text(row.get('cardboard_len') if side == 'length' else row.get('cardboard_width'))}mm"
        for side in dimension_hits
    ]
    if not summaries:
        summaries.append("客户筛选命中")
    return {
        "query": keyword,
        "dimension_mode": dimension_mode,
        "dimension_sides": dimension_hits,
        "text_fields": text_hits,
        "summary": "；".join(summaries),
    }


def _product_specification(product: Product) -> str:
    return product_dimension_specification(product) or ""


def _product_payload(product: Product, *, inventory_summary: dict | None = None) -> dict:
    payload = {
        "id": product.id,
        "customer_id": product.customer_id,
        "customer_name": product.customer.name,
        "customer_code": product.customer.customer_code,
        "product_code": product.product_code,
        "customer_material_code": product.customer_material_code,
        "product_name": product.product_name,
        "specification": _product_specification(product),
        "box_style": product.box_style,
    }
    if inventory_summary is not None:
        payload["inventory_summary"] = inventory_summary
    return payload


def _dimension_search_condition(keyword: str):
    normalized = re.sub(r"\s+", "", keyword).removesuffix("mm").removesuffix("MM")
    parts = re.split(r"[xX×*]", normalized)
    if len(parts) not in {2, 3} or any(not part for part in parts):
        return None
    try:
        dimensions = [Decimal(part) for part in parts]
    except InvalidOperation:
        return None
    fields = [Product.length_mm, Product.width_mm, Product.height_mm]
    return and_(*(fields[index] == value for index, value in enumerate(dimensions)))


def _product_search_condition(keyword: str):
    pattern = _escaped_like(keyword)
    conditions = [
        Product.product_code.ilike(pattern, escape="\\"),
        Product.customer_material_code.ilike(pattern, escape="\\"),
        Product.product_name.ilike(pattern, escape="\\"),
        Product.box_style.ilike(pattern, escape="\\"),
        Customer.name.ilike(pattern, escape="\\"),
        Customer.customer_code.ilike(pattern, escape="\\"),
        cast(Product.length_mm, String).ilike(pattern, escape="\\"),
        cast(Product.width_mm, String).ilike(pattern, escape="\\"),
        cast(Product.height_mm, String).ilike(pattern, escape="\\"),
    ]
    dimension_condition = _dimension_search_condition(keyword)
    if dimension_condition is not None:
        conditions.append(dimension_condition)
    return or_(*conditions)


def _product_has_stock_condition():
    finished = exists(
        select(1)
        .select_from(InventoryLot)
        .join(
            FinishedGoodsInventoryDetail,
            FinishedGoodsInventoryDetail.inventory_lot_id == InventoryLot.id,
        )
        .where(
            InventoryLot.inventory_type == "finished",
            InventoryLot.status == "active",
            or_(
                InventoryLot.quantity_available > 0,
                InventoryLot.quantity_reserved > 0,
            ),
            FinishedGoodsInventoryDetail.product_id == Product.id,
            FinishedGoodsInventoryDetail.owner_customer_id == Product.customer_id,
            FinishedGoodsInventoryDetail.is_general.is_(False),
            FinishedGoodsInventoryDetail.inventory_code_snapshot
            == Product.product_code,
        )
        .correlate(Product)
    )
    semi_finished = exists(
        select(1)
        .select_from(InventoryLot)
        .join(
            SemiFinishedInventoryDetail,
            SemiFinishedInventoryDetail.inventory_lot_id == InventoryLot.id,
        )
        .join(
            SemiFinishedLotAllowedProduct,
            SemiFinishedLotAllowedProduct.inventory_lot_id == InventoryLot.id,
        )
        .where(
            InventoryLot.inventory_type == "semi_finished",
            InventoryLot.status == "active",
            or_(
                InventoryLot.quantity_available > 0,
                InventoryLot.quantity_reserved > 0,
            ),
            SemiFinishedLotAllowedProduct.product_id == Product.id,
            SemiFinishedInventoryDetail.owner_customer_id == Product.customer_id,
        )
        .correlate(Product)
    )
    return or_(finished, semi_finished)


def _empty_product_inventory_summary() -> dict:
    return {
        "has_stock": False,
        "position_count": 0,
        "finished": {
            "unit": "只",
            "quantity_total": 0,
            "quantity_available": 0,
            "quantity_reserved": 0,
            "quantity_pending_pick": 0,
        },
        "semi_finished": {
            "unit": "张",
            "quantity_total": 0,
            "quantity_available": 0,
            "quantity_reserved": 0,
            "quantity_pending_pick": 0,
        },
    }


def _product_inventory_summaries(
    db: Session,
    products: list[Product],
) -> dict[int, dict]:
    product_ids = [product.id for product in products]
    summaries = {
        product.id: _empty_product_inventory_summary() for product in products
    }
    if not product_ids:
        return summaries

    finished_rows = db.execute(
        select(
            FinishedGoodsInventoryDetail.product_id,
            InventoryLot.id,
            InventoryLot.warehouse_location_id,
            InventoryLot.quantity_available,
            InventoryLot.quantity_reserved,
        )
        .select_from(InventoryLot)
        .join(
            FinishedGoodsInventoryDetail,
            FinishedGoodsInventoryDetail.inventory_lot_id == InventoryLot.id,
        )
        .join(Product, Product.id == FinishedGoodsInventoryDetail.product_id)
        .where(
            FinishedGoodsInventoryDetail.product_id.in_(product_ids),
            FinishedGoodsInventoryDetail.owner_customer_id == Product.customer_id,
            FinishedGoodsInventoryDetail.is_general.is_(False),
            FinishedGoodsInventoryDetail.inventory_code_snapshot == Product.product_code,
            InventoryLot.inventory_type == "finished",
            InventoryLot.status == "active",
            or_(
                InventoryLot.quantity_available > 0,
                InventoryLot.quantity_reserved > 0,
            ),
        )
    ).all()
    pending_pick_by_lot = _pending_pick_by_lot(
        db, [int(row.id) for row in finished_rows]
    )
    position_ids: dict[int, set[int]] = {product.id: set() for product in products}
    for row in finished_rows:
        summary = summaries[int(row.product_id)]
        group = summary["finished"]
        available = int(row.quantity_available or 0)
        reserved = int(row.quantity_reserved or 0)
        group["quantity_available"] += available
        group["quantity_reserved"] += reserved
        group["quantity_total"] += available + reserved
        group["quantity_pending_pick"] += pending_pick_by_lot.get(int(row.id), 0)
        position_ids[int(row.product_id)].add(int(row.warehouse_location_id))

    semi_rows = db.execute(
        select(
            SemiFinishedLotAllowedProduct.product_id,
            InventoryLot.warehouse_location_id,
            InventoryLot.quantity_available,
            InventoryLot.quantity_reserved,
        )
        .select_from(InventoryLot)
        .join(
            SemiFinishedInventoryDetail,
            SemiFinishedInventoryDetail.inventory_lot_id == InventoryLot.id,
        )
        .join(
            SemiFinishedLotAllowedProduct,
            SemiFinishedLotAllowedProduct.inventory_lot_id == InventoryLot.id,
        )
        .join(Product, Product.id == SemiFinishedLotAllowedProduct.product_id)
        .where(
            SemiFinishedLotAllowedProduct.product_id.in_(product_ids),
            SemiFinishedInventoryDetail.owner_customer_id == Product.customer_id,
            InventoryLot.inventory_type == "semi_finished",
            InventoryLot.status == "active",
            or_(
                InventoryLot.quantity_available > 0,
                InventoryLot.quantity_reserved > 0,
            ),
        )
    ).all()
    for row in semi_rows:
        summary = summaries[int(row.product_id)]
        group = summary["semi_finished"]
        available = int(row.quantity_available or 0)
        reserved = int(row.quantity_reserved or 0)
        group["quantity_available"] += available
        group["quantity_reserved"] += reserved
        group["quantity_total"] += available + reserved
        position_ids[int(row.product_id)].add(int(row.warehouse_location_id))

    for product_id, summary in summaries.items():
        summary["position_count"] = len(position_ids[product_id])
        summary["has_stock"] = any(
            summary[group]["quantity_total"] > 0
            for group in ("finished", "semi_finished")
        )
    return summaries


def _product_query(db: Session, *, product_id: int | None = None):
    statement = (
        select(Product)
        .join(Customer, Customer.id == Product.customer_id)
        .options(selectinload(Product.customer))
        .where(
            Product.is_active.is_(True),
            Product.deleted_at.is_(None),
            Customer.is_active.is_(True),
        )
    )
    if product_id is not None:
        statement = statement.where(Product.id == product_id)
    return statement


def _require_visible_product(
    db: Session,
    *,
    product_id: int,
    visible_customer_ids: set[int] | None,
) -> Product:
    statement = _product_query(db, product_id=product_id)
    if visible_customer_ids is not None:
        statement = statement.where(Product.customer_id.in_(visible_customer_ids))
    product = db.scalar(statement)
    if product is None:
        # A direct ID must not reveal whether an inaccessible product exists.
        raise HTTPException(status_code=404, detail="产品不存在或当前账号无权查看")
    return product


def _lot_load_options():
    return (
        selectinload(InventoryLot.location).selectinload(
            WarehouseLocation.floor3_layout
        ),
        selectinload(InventoryLot.location)
        .selectinload(WarehouseLocation.address_area)
        .selectinload(WarehouseArea.floor),
        selectinload(InventoryLot.pallet_item).selectinload(
            InventoryPalletItem.pallet
        ),
    )


def _position_payload(
    lot: InventoryLot,
    projection_context: Mapping[str, object] | None = None,
) -> dict:
    location = lot.location
    pallet = current_same_location_pallet(lot)
    if location is None:
        return {
            "lot_id": lot.id,
            "lot_version": lot.version,
            "lot_number": lot.lot_number,
            "location_id": None,
            "location_code": None,
            "location_name": "尚未绑定正式位置",
            "location_master_name": None,
            "current_address_name": "尚未绑定正式位置",
            "employee_location_name": "尚未绑定正式位置",
            "floor": None,
            "area_code": None,
            "map_rack_id": None,
            "rack_display_name": None,
            "level_no": None,
            "slot_no": None,
            "address_version": None,
            "layout_version": None,
            "published_map_revision": None,
            "pallet_code": None,
            "pallet_projection_status": "missing_current_pallet",
            "quantity_available": lot.quantity_available,
            "quantity_reserved": lot.quantity_reserved,
            "quantity_total": lot.quantity_available + lot.quantity_reserved,
            "unit": "只" if lot.inventory_type == "finished" else "张",
            "position_status": "unlocated",
            "map_status": "unplaced",
            "map_status_text": "尚未绑定正式位置",
            "map_issue": "库存批次尚未绑定正式位置",
            "map_url": None,
            "last_updated_at": utc_naive_to_api(lot.last_movement_at),
        }
    context = dict(projection_context or {})
    projection = warehouse_location_projection(location, **context)
    position_status = str(projection["position_status"])
    if pallet is not None and pallet.needs_relocation:
        position_status = "unplaced"
        map_issue = "当前栈板待重新归位"
    else:
        map_issue = projection.get("map_issue")
    if position_status == "mapped":
        map_status = "mapped"
        map_status_text = "实测地图已定位"
    elif position_status in {"unplaced", "unlocated", "disabled"}:
        map_status = "unplaced"
        map_status_text = str(map_issue or "待归位，暂不能在地图定位")
    else:
        map_status = "ledger_only"
        map_status_text = str(map_issue or "已登记库位，尚未接入当前发布地图")
    map_url = None
    if map_status == "mapped":
        map_url = (
            "/warehouse.html?embedded=1&readonly=1&tab=locations"
            f"&location_view=floor3&location_id={location.id}"
            f"&lot_id={lot.id}&source=mobile-product"
        )
    unit_label = "只" if lot.inventory_type == "finished" else "张"
    address = location_address_payload(
        location,
        area=context.get("area"),
        floor=context.get("floor"),
        position_status=position_status,
        area_sequence=(int(context["area_sequence"]) if context.get("area_sequence") else None),
    )
    layout = context.get("layout")
    rack_identity = {
        **rack_cell_identity_payload(location),
        "layout_version": (
            int(layout.version) if isinstance(layout, Floor3LocationLayout) else None
        ),
        "published_map_revision": projection.get("published_map_revision"),
    }
    return {
        "lot_id": lot.id,
        "lot_version": lot.version,
        "lot_number": lot.lot_number,
        "location_id": location.id,
        "location_code": location.location_code,
        "location_name": address["employee_location_name"],
        "location_master_name": location.location_name,
        "current_address_name": address["current_address_name"],
        "employee_location_name": address["employee_location_name"],
        "floor": location.warehouse_floor,
        "area_code": location.area_code,
        **rack_identity,
        "pallet_code": (
            pallet.pallet_code if pallet is not None else None
        ),
        "pallet_projection_status": (
            "current_same_location"
            if pallet is not None
            else "missing_current_pallet"
        ),
        "quantity_available": lot.quantity_available,
        "quantity_reserved": lot.quantity_reserved,
        "quantity_total": lot.quantity_available + lot.quantity_reserved,
        "unit": unit_label,
        "position_status": position_status,
        "map_status": map_status,
        "map_status_text": map_status_text,
        "map_issue": map_issue,
        "map_url": map_url,
        "last_updated_at": utc_naive_to_api(lot.last_movement_at),
    }


def _pending_pick_by_lot(db: Session, lot_ids: list[int]) -> dict[int, int]:
    if not lot_ids:
        return {}
    rows = db.scalars(
        select(InventoryReservation).where(
            InventoryReservation.inventory_lot_id.in_(lot_ids),
            InventoryReservation.reservation_type.in_(
                ["finished_order", "finished_surplus_delivery"]
            ),
            InventoryReservation.status.in_(["active", "partial"]),
        )
    ).all()
    totals: dict[int, int] = {}
    for row in rows:
        pending = max(
            row.reserved_stock_quantity
            - row.consumed_stock_quantity
            - row.released_stock_quantity,
            0,
        )
        totals[row.inventory_lot_id] = totals.get(row.inventory_lot_id, 0) + pending
    return totals


def _inventory_group(
    lots: list[InventoryLot],
    *,
    unit: str,
    pending_pick_by_lot: dict[int, int] | None = None,
    projection_contexts: Mapping[int, Mapping[str, object]] | None = None,
) -> dict:
    pending_pick_by_lot = pending_pick_by_lot or {}
    projection_contexts = projection_contexts or {}
    positions = [
        _position_payload(
            lot,
            (
                projection_contexts.get(int(lot.warehouse_location_id))
                if lot.warehouse_location_id is not None
                else None
            ),
        )
        for lot in lots
    ]
    positions.sort(
        key=lambda row: (
            row["floor"] is None,
            row["floor"] or 0,
            row["area_code"] or "",
            row["location_code"] or "",
            row["lot_id"],
        )
    )
    return {
        "unit": unit,
        "quantity_total": sum(lot.quantity_available + lot.quantity_reserved for lot in lots),
        "quantity_available": sum(lot.quantity_available for lot in lots),
        "quantity_reserved": sum(lot.quantity_reserved for lot in lots),
        "quantity_pending_pick": sum(
            pending_pick_by_lot.get(lot.id, 0) for lot in lots
        ),
        "position_count": len(positions),
        "positions": positions,
    }


@router.get("/incoming/search")
def search_pending_incoming(
    response: Response,
    q: str = Query(min_length=1, max_length=100),
    dimension_mode: Literal["any", "length", "width"] = Query(default="any"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=20),
    db: Session = Depends(get_db),
    user: User = Depends(can_read_incoming),
) -> dict:
    """Search final pending incoming routes without changing receipt facts."""

    _no_store(response)
    response.headers["X-ERP-Session-Identity"] = f"{user.id}:{user.auth_version}"
    keyword = q.strip()
    if not keyword:
        raise HTTPException(status_code=422, detail="请输入客户名称或报料尺寸")
    if dimension_mode in {"length", "width"} and _dimension_decimal(keyword) is None:
        raise HTTPException(status_code=422, detail="按报料长或报料宽查询时，请输入尺寸数字")

    matches: list[tuple[dict, dict]] = []
    for route in _pending_incoming_route_rows(db, user):
        match = _incoming_search_match(
            route,
            keyword=keyword,
            dimension_mode=dimension_mode,
        )
        if match is not None:
            matches.append((route, match))

    total = len(matches)
    last_page = max(1, (total + page_size - 1) // page_size)
    resolved_page = min(page, last_page)
    start = (resolved_page - 1) * page_size
    selected = matches[start : start + page_size]
    selected_routes = [route for route, _match in selected]
    detailed_rows = _incoming_rows(
        db,
        user=user,
        selected_pending_routes=selected_routes,
    )
    detailed_by_id = {str(row.get("item_id")): row for row in detailed_rows}
    if set(detailed_by_id) != {str(route.get("item_id")) for route in selected_routes}:
        raise HTTPException(status_code=409, detail="待收料状态已变化，请刷新后重新查询")

    items: list[dict] = []
    for route, match in selected:
        route_key = str(route["item_id"])
        item = _incoming_row_response(detailed_by_id[route_key])
        item["search_match"] = match
        item["production_detail_url"] = (
            f"/api/mobile/erp/incoming/{route_key}/production-detail"
        )
        items.append(item)
    return {
        "query": keyword,
        "dimension_mode": dimension_mode,
        "items": items,
        "total": total,
        "page": resolved_page,
        "page_size": page_size,
        "as_of": datetime.now(_BEIJING).isoformat(timespec="seconds"),
        "read_only": True,
    }


@router.get("/incoming/{route_id}/production-detail")
def pending_incoming_production_detail(
    route_id: str,
    response: Response,
    db: Session = Depends(get_db),
    user: User = Depends(can_read_orders),
) -> dict:
    """Return production facts for one exact still-pending incoming route."""

    _no_store(response)
    if not has_permission(user, "incoming.view"):
        raise HTTPException(status_code=403, detail="当前账号没有查看待收料明细的权限")
    normalized_route_id = route_id.strip()
    if not re.fullmatch(r"(?:r|sr)?\d+", normalized_route_id):
        raise HTTPException(status_code=422, detail="待收料明细标识无效")
    route = next(
        (
            row
            for row in _pending_incoming_route_rows(db, user)
            if str(row.get("item_id")) == normalized_route_id
        ),
        None,
    )
    if route is None:
        raise HTTPException(status_code=404, detail="待收料明细不存在、已收齐或无权查看")
    incoming_rows = _incoming_rows(
        db,
        user=user,
        selected_pending_routes=[route],
    )
    if len(incoming_rows) != 1 or str(incoming_rows[0].get("item_id")) != normalized_route_id:
        raise HTTPException(status_code=409, detail="待收料状态已变化，请刷新后重新打开生产明细")

    order_item_id = route.get("order_item_id")
    task_rows: list[dict] = []
    if order_item_id is not None:
        task_query = select(ProductionTask.id).where(
            ProductionTask.order_item_id == int(order_item_id)
        )
        if incoming_rows[0].get("purpose_status") != "legacy_unset":
            task_query = task_query.where(ProductionTask.task_role == "order_main")
        else:
            requisition_item_id = route.get("requisition_item_id")
            if requisition_item_id is not None:
                source = db.scalar(
                    select(RequisitionItemBomSource)
                    .where(
                        RequisitionItemBomSource.requisition_item_id
                        == int(requisition_item_id),
                        RequisitionItemBomSource.active_guard == 1,
                    )
                    .order_by(RequisitionItemBomSource.id.asc())
                )
                component_id = (
                    source.sales_order_item_bom_component_id
                    if source is not None
                    else None
                )
                task_query = task_query.where(
                    ProductionTask.sales_order_item_bom_component_id
                    == component_id
                    if component_id is not None
                    else ProductionTask.sales_order_item_bom_component_id.is_(None)
                )
            else:
                task_query = task_query.where(
                    ProductionTask.sales_order_item_bom_component_id.is_(None)
                )
        task_ids = list(db.scalars(task_query.order_by(ProductionTask.id.asc())).all())
        if task_ids:
            task_rows = list_production_tasks(
                db,
                allowed_customer_ids=_visible_customer_ids(user, db),
                task_ids=task_ids,
            )

    incoming_item = _incoming_row_response(incoming_rows[0])
    return {
        "route_id": normalized_route_id,
        "order_item_id": order_item_id,
        "requisition_item_id": route.get("requisition_item_id"),
        "stock_replenishment_item_id": route.get("stock_replenishment_item_id"),
        "incoming_item": incoming_item,
        "production_tasks": [
            _safe_production_task(db, task, drawing_path=incoming_item.get("drawing_path"), user=user)
            for task in task_rows
        ],
        "message": (
            "补库待收料没有订单生产任务"
            if order_item_id is None
            else ("未生成生产任务" if not task_rows else None)
        ),
        "as_of": datetime.now(_BEIJING).isoformat(timespec="seconds"),
        "read_only": True,
    }


@router.get("/production/tasks")
def mobile_production_station_tasks(
    response: Response,
    station: Literal["printing", "die_cut"] = Query(...),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=20),
    db: Session = Depends(get_db),
    user: User = Depends(can_read_orders),
) -> dict:
    """Return one authorized station's current pending task page, read only."""

    _no_store(response)
    _require_exact_mobile_production_station(user, station)
    visible_customer_ids = _visible_customer_ids(user, db)
    task_ids, total, resolved_page = list_production_station_task_ids(
        db,
        allowed_customer_ids=visible_customer_ids,
        station=station,
        page=page,
        page_size=page_size,
    )
    task_rows = list_production_tasks(
        db,
        allowed_customer_ids=visible_customer_ids,
        status="pending",
        task_ids=task_ids,
    )
    if {int(row["id"]) for row in task_rows} != set(task_ids):
        raise HTTPException(status_code=409, detail="生产任务状态已变化，请重新读取")
    return {
        "station": station,
        "items": _production_station_task_payloads(
            db,
            tasks=task_rows,
            station=station,
            mold_map_allowed=has_permission(user, "warehouse.view"),
        ),
        "total": total,
        "page": resolved_page,
        "page_size": page_size,
        "as_of": datetime.now(_BEIJING).isoformat(timespec="seconds"),
        "read_only": True,
    }


@router.get("/production/tasks/{task_id}/drawing")
def mobile_production_task_drawing(
    task_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(can_read_orders),
) -> FileResponse:
    """Serve one still-pending task drawing after station and scope checks."""

    _require_mobile_production_station(user)
    visible_rows = list_production_tasks(
        db,
        allowed_customer_ids=_visible_customer_ids(user, db),
        status="pending",
        task_ids=[task_id],
    )
    if len(visible_rows) != 1:
        raise HTTPException(status_code=404, detail="生产任务不存在、已完成或无权查看")
    from app.services.drawing_binding import bound_task_release
    import hashlib
    release = bound_task_release(db, task_id)
    if release is not None:
        import os
        if not os.getenv("ERP_FILE_STORAGE_DIR"):
            raise HTTPException(status_code=503, detail="图纸存储根未显式配置")
        path = resolve_stored_reference(release.pdf_reference)
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != release.pdf_sha256:
            raise HTTPException(status_code=503, detail="发布图纸缺失或校验失败")
        return FileResponse(path, media_type="application/pdf",
                            headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})
    task_row = visible_rows[0]
    task = db.get(ProductionTask, task_id)
    item = db.get(OrderItem, int(task_row["order_item_id"]))
    component = (
        db.get(
            SalesOrderItemBomComponent,
            int(task_row["bom_component_snapshot_id"]),
        )
        if task_row.get("bom_component_snapshot_id") is not None
        else None
    )
    reference = None
    if component is None and item is not None and item.drawing_file:
        reference = item.drawing_file
    else:
        drawing = db.scalar(
            select(ProductDrawing)
            .where(ProductDrawing.product_id == int(task_row["product_id"]), engineering_drawing_condition())
            .order_by(ProductDrawing.uploaded_at.desc(), ProductDrawing.id.desc())
            .limit(1)
        ) if task_row.get("product_id") is not None else None
        reference = (
            drawing.image_path
            if drawing is not None
            else component.snapshot_die_cut_path
            if component is not None
            else None
        )
    if task is None or not reference:
        raise HTTPException(status_code=404, detail="当前生产任务没有可查看的图纸")
    try:
        path = resolve_stored_reference(reference)
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail="生产图纸文件不存在") from error
    if not path.is_file():
        raise HTTPException(status_code=404, detail="生产图纸文件不存在")
    metadata = stored_file_metadata(path)
    content_type = str(
        metadata.get("content_type")
        or mimetypes.guess_type(path.name)[0]
        or "application/octet-stream"
    )
    return FileResponse(
        path,
        media_type=content_type,
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
    )


@router.get("/production/recent")
def recent_production_materials(
    response: Response,
    period: Literal["today", "3d", "7d", "custom"] = Query(default="3d"),
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    db: Session = Depends(get_db),
    user: User = Depends(can_read_orders),
) -> dict:
    """Return formally received material and linked production facts, read only."""

    _no_store(response)
    _require_mobile_production_station(user)
    if not has_permission(user, "incoming.view"):
        raise HTTPException(status_code=403, detail="当前账号没有查看来料资料的权限")
    start_date, end_date, start_utc, end_utc = _production_period_bounds(
        period,
        date_from=date_from,
        date_to=date_to,
    )
    received_rows = [
        row
        for row in _received_rows(
            db,
            user=user,
            received_since=start_utc,
        )
        if row.get("material_received_at") is not None
        and row["material_received_at"] < end_utc
        and row.get("receipt_status", "posted") == "posted"
    ]
    task_rows = list_production_tasks(
        db,
        allowed_customer_ids=_visible_customer_ids(user, db),
    )
    tasks_by_item: dict[int, list[dict]] = {}
    for task in task_rows:
        tasks_by_item.setdefault(int(task["order_item_id"]), []).append(task)
    received_order_item_ids = {
        int(row["order_item_id"])
        for row in received_rows
        if row.get("order_item_id") is not None
    }
    item_box_types = {
        int(item_id): box_type_code(box_style)
        for item_id, box_style in db.execute(
            select(OrderItem.id, Product.box_style)
            .join(Product, Product.id == OrderItem.product_id)
            .where(OrderItem.id.in_(received_order_item_ids))
        ).all()
    }
    direction_notes = {
        item_id: note
        for item_id, note in db.execute(
            select(OrderItem.id, OrderItem.snapshot_report_notes).where(
                OrderItem.id.in_(received_order_item_ids)
            )
        ).all()
        if (note or "").strip()
    }

    items: list[dict] = []
    seen_production_sources: set[tuple[int, str]] = set()
    for row in received_rows:
        order_item_id = row.get("order_item_id")
        linked_tasks = tasks_by_item.get(int(order_item_id), []) if order_item_id else []
        board_direction_note = row.get("requisition_remark")
        if not board_direction_note and order_item_id:
            board_direction_note = direction_notes.get(int(order_item_id))
        received_product_code = (row.get("product_code") or "").strip()
        exact_tasks = [
            task
            for task in linked_tasks
            if (task.get("product_code") or "").strip() == received_product_code
        ]
        if exact_tasks:
            linked_tasks = exact_tasks
        linked_tasks = [
            task for task in linked_tasks if task.get("box_type_code") != "liner"
        ]
        if not linked_tasks and item_box_types.get(int(order_item_id or 0)) == "liner":
            continue
        source_key = (
            int(order_item_id or 0),
            (row.get("product_code") or "").strip().casefold(),
        )
        if source_key in seen_production_sources:
            continue
        seen_production_sources.add(source_key)
        remaining = max(int(row.get("remaining_quantity") or 0), 0)
        material_state = "材料未齐" if remaining > 0 else "材料已齐"
        items.append(
            {
                "receipt_item_id": row.get("receipt_item_id"),
                "receipt_number": row.get("receipt_number"),
                "received_at": utc_naive_to_api(row["material_received_at"]),
                "received_by_name": row.get("received_by_name") or "未记录",
                "supplier_document_number": row.get("supplier_order_number"),
                "customer_name": row.get("customer_name"),
                "order_number": row.get("display_order_number") or row.get("order_number"),
                "customer_po": row.get("customer_po"),
                "product_code": row.get("product_code"),
                "product_name": row.get("product_name"),
                "carton_specification": row.get("specification"),
                "board_length_mm": _number_text(row.get("cardboard_len")),
                "board_width_mm": _number_text(row.get("cardboard_width")),
                "flute_type": row.get("flute_type"),
                "crease_type": row.get("snapshot_crease_type"),
                "crease_values_mm": [
                    value
                    for value in (
                        row.get("snapshot_crease_left_mm"),
                        row.get("snapshot_crease_middle_mm"),
                        row.get("snapshot_crease_right_mm"),
                    )
                    if value is not None
                ],
                "received_quantity": int(row.get("received_quantity_this_time") or 0),
                "cumulative_received_quantity": int(
                    row.get("cumulative_received_quantity") or 0
                ),
                "remaining_quantity": remaining,
                "difference_quantity": int(row.get("variance_quantity") or 0),
                "material_state": material_state,
                "board_direction_note": board_direction_note,
                "delivery_date": (
                    row["delivery_date"].isoformat()
                    if row.get("delivery_date")
                    else None
                ),
                "drawing_path": row.get("drawing_path"),
                "drawing_is_pdf": row.get("drawing_is_pdf") is True,
                "production_tasks": [
                    _safe_production_task(
                        db,
                        task,
                        drawing_path=row.get("drawing_path"),
                        user=user,
                    )
                    for task in linked_tasks
                ],
            }
        )
    return {
        "period": period,
        "date_from": start_date.isoformat(),
        "date_to": end_date.isoformat(),
        "count": len(items),
        "items": items,
        "as_of": datetime.now(_BEIJING).isoformat(timespec="seconds"),
        "read_only": True,
    }


@router.get("/production/pending/lookup")
def lookup_pending_production_tasks(
    response: Response,
    q: str = Query(min_length=1, max_length=100),
    limit: int = Query(default=50, ge=1, le=50),
    db: Session = Depends(get_db),
    user: User = Depends(can_read_orders),
) -> dict:
    """Reverse lookup current pending production tasks without writing facts."""

    _no_store(response)
    _require_mobile_production_station(user)
    if not has_permission(user, "incoming.view"):
        raise HTTPException(status_code=403, detail="当前账号没有查看来料资料的权限")
    keyword = q.strip()
    if not keyword:
        raise HTTPException(status_code=422, detail="请扫码或输入存货编码、订单号、任务号")
    visible_customer_ids = _visible_customer_ids(user, db)
    lookup_rows, total = find_pending_production_task_lookup_rows(
        db,
        allowed_customer_ids=visible_customer_ids,
        keyword=keyword,
        limit=limit,
    )
    task_ids = [row["task_id"] for row in lookup_rows]
    full_rows = list_production_tasks(
        db,
        allowed_customer_ids=visible_customer_ids,
        status="pending",
        task_ids=task_ids,
    )
    tasks_by_id = {int(row["id"]): row for row in full_rows}
    if set(tasks_by_id) != set(task_ids):
        raise HTTPException(status_code=409, detail="生产任务状态已变化，请重新扫码")
    items = []
    for lookup_row in lookup_rows:
        task = tasks_by_id[lookup_row["task_id"]]
        safe_task = _safe_production_task(db, task, drawing_path=None, user=user)
        safe_task.update(
            {
                "customer_name": task.get("customer_name"),
                "customer_po": lookup_row.get("customer_po"),
                "delivery_date": (
                    lookup_row["delivery_date"].isoformat()
                    if lookup_row.get("delivery_date")
                    else None
                ),
            }
        )
        items.append(safe_task)
    return {
        "query": keyword,
        "total": total,
        "returned_count": len(items),
        "limit": limit,
        "items": items,
        "as_of": datetime.now(_BEIJING).isoformat(timespec="seconds"),
        "read_only": True,
    }


_PRODUCT_TASK_STATUS_LABELS = {
    "waiting_material": "待收料",
    "pending": "待生产",
}
_PRODUCT_GLUE_TOKENS = {"粘合", "粘贴", "粘箱", "糊箱", "糊盒"}
_PRODUCT_STAPLE_TOKENS = {"打钉", "钉箱", "打钉箱", "钉合"}
_PRODUCT_NO_JOINING_TOKENS = {"无需结合", "无需", "不需结合", "不需要结合", "其他"}


def _product_joining_summary(value: str | None) -> tuple[str, str | None]:
    tokens = {
        item.strip()
        for item in re.split(r"[,，、;；]", str(value or ""))
        if item.strip()
    }
    matches = [
        label
        for label, candidates in (
            ("粘贴", _PRODUCT_GLUE_TOKENS),
            ("打钉", _PRODUCT_STAPLE_TOKENS),
            ("无需结合", _PRODUCT_NO_JOINING_TOKENS),
        )
        if tokens.intersection(candidates)
    ]
    if len(matches) > 1:
        return "结合方式冲突", "当前主档同时包含多个结合方式，请先在客户常用箱中核对"
    return (matches[0] if matches else "无需结合"), None


def _current_product_task_ids(db: Session, product_id: int) -> list[int]:
    main_task = aliased(ProductionTask)
    main_task_exists = exists().where(
        main_task.order_item_id == ProductionTask.order_item_id,
        main_task.task_role == "order_main",
    )
    component_match = exists().where(
        SalesOrderItemBomComponent.sales_order_item_id == OrderItem.id,
        SalesOrderItemBomComponent.component_product_id == product_id,
        or_(
            ProductionTask.task_role == "order_main",
            SalesOrderItemBomComponent.id
            == ProductionTask.sales_order_item_bom_component_id,
        ),
    )
    return list(
        db.scalars(
            select(ProductionTask.id)
            .join(OrderItem, OrderItem.id == ProductionTask.order_item_id)
            .join(Order, Order.id == OrderItem.order_id)
            .where(
                ProductionTask.status.in_(tuple(_PRODUCT_TASK_STATUS_LABELS)),
                Order.status.in_(ORDER_ITEM_ACTIVE_ORDER_STATUSES),
                OrderItem.is_force_closed.is_(False),
                OrderItem.delivered_quantity < OrderItem.quantity,
                or_(ProductionTask.task_role == "order_main", ~main_task_exists),
                or_(
                    OrderItem.product_id == product_id,
                    component_match,
                ),
            )
            .order_by(ProductionTask.id.desc())
            .limit(21)
        ).all()
    )


def _current_product_printing_plates(
    product: Product,
    *,
    show_location: bool,
) -> list[dict]:
    result: list[dict] = []
    for plate in (
        product.printing_plate_1,
        product.printing_plate_2,
        product.printing_plate_3,
    ):
        if plate is None:
            continue
        result.append(
            {
                "plate_code": plate.plate_code,
                "plate_name": plate.plate_name,
                "color_name": plate.color_name,
                "status": plate.status,
                "current_location": plate.rack_location if show_location else None,
            }
        )
    return result


def _product_overview_headers(response: Response) -> None:
    response.headers["Cache-Control"] = "private, no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Vary"] = "Cookie"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"


@router.get("/products/{product_id}/production-overview")
def product_production_overview(
    product_id: int,
    response: Response,
    db: Session = Depends(get_db),
    user: User = Depends(can_read_orders),
) -> dict:
    """Return a current, read-only and price-free product production view."""

    _product_overview_headers(response)
    _require_mobile_production_station(user)
    try:
        product = _require_visible_product(
            db,
            product_id=product_id,
            visible_customer_ids=_visible_customer_ids(user, db),
        )
    except HTTPException as error:
        error.headers = {
            **(error.headers or {}),
            "Cache-Control": "private, no-store, max-age=0",
            "X-Robots-Tag": "noindex, nofollow",
        }
        raise

    task_ids = _current_product_task_ids(db, product.id)
    task_rows = list_production_tasks(
        db,
        allowed_customer_ids=_visible_customer_ids(user, db),
        task_ids=task_ids,
    )
    task_rows = [
        row
        for row in task_rows
        if str(row.get("status") or "") in _PRODUCT_TASK_STATUS_LABELS
    ][:20]
    joining_method, joining_warning = _product_joining_summary(
        product.production_process
    )
    location_allowed = has_permission(user, "warehouse.view") or has_permission(
        user, "production.die_cut.view"
    )
    printing_location_allowed = has_permission(
        user, "warehouse.view"
    ) or has_permission(user, "production.printing.view")
    latest_drawing = default_product_drawing(product.drawings)
    material_code = (
        product.default_material_code
        or (product.material.code if product.material is not None else None)
        or product.legacy_material_text
    )
    mold = product.mold_tool
    return {
        "view": "current_product",
        "view_label": "当前资料",
        "read_only": True,
        "product": {
            "id": int(product.id),
            "version": int(product.version),
            "customer_id": int(product.customer_id),
            "customer_name": product.customer.name,
            "customer_code": product.customer.customer_code,
            "product_code": product.product_code,
            "customer_material_code": product.customer_material_code,
            "product_name": product.product_name,
            "specification": _product_specification(product),
            "report_specification": (
                f"{product.report_length_mm}×{product.report_width_mm}mm"
                if product.report_length_mm and product.report_width_mm
                else None
            ),
            "material_code": material_code,
            "flute_type": product.flute_type
            or (product.material.flute_type if product.material is not None else None),
            "layer_count": product.layer_count
            or (product.material.layer_count if product.material is not None else None),
            "box_style": product.box_style,
            "crease_type": product.crease_type,
            "crease_values_mm": [
                value
                for value in (
                    product.crease_left_mm,
                    product.crease_middle_mm,
                    product.crease_right_mm,
                )
                if value is not None
            ],
            "print_content": product.print_content or "无印刷",
            "printing_colors": parse_printing_colors(product.printing_colors),
            "printing_plate_mode": product.printing_plate_mode,
            "printing_plates": _current_product_printing_plates(
                product,
                show_location=printing_location_allowed,
            ),
            "joining_method": joining_method,
            "joining_warning": joining_warning,
            "drawing_available": bool(latest_drawing or product.die_cut_path),
            "drawing_url": (
                f"/api/mobile/erp/products/{product.id}/drawing"
                if latest_drawing or product.die_cut_path
                else None
            ),
            "mold": (
                {
                    "display_name": mold.mold_name,
                    "mold_name": mold.mold_name,
                    "current_location": mold.rack_location,
                    "current_location_display": describe_mold_location(
                        mold.rack_location
                    )["prompt"],
                    "is_active": bool(mold.is_active),
                }
                if mold is not None and location_allowed
                else {"visibility": "hidden_by_permission"}
                if mold is not None
                else None
            ),
        },
        "current_tasks": {
            "items": [
                {
                    "production_task_id": int(row["id"]),
                    "production_task_version": int(row["version"]),
                    "order_number": row.get("order_number"),
                    "customer_po": row.get("customer_po") or row.get("customer_order_number"),
                    "item_order_number": row.get("item_order_number"),
                    "status": row.get("status"),
                    "status_label": _PRODUCT_TASK_STATUS_LABELS.get(
                        str(row.get("status") or ""), str(row.get("status") or "")
                    ),
                    "planned_quantity": int(row.get("planned_quantity") or 0),
                    "actual_output_quantity": int(
                        row.get("actual_output_quantity") or 0
                    ),
                    "production_quantity_unit": row.get(
                        "production_quantity_unit"
                    ),
                }
                for row in task_rows
            ],
            "has_more": len(task_ids) > 20,
        },
        "as_of": datetime.now(_BEIJING).isoformat(timespec="seconds"),
    }


@router.get("/products/{product_id}/drawing")
def product_production_drawing(
    product_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(can_read_orders),
) -> FileResponse:
    _require_mobile_production_station(user)
    product = _require_visible_product(
        db,
        product_id=product_id,
        visible_customer_ids=_visible_customer_ids(user, db),
    )
    latest_drawing = default_product_drawing(product.drawings)
    reference = latest_drawing.image_path if latest_drawing else product.die_cut_path
    if not reference:
        raise HTTPException(status_code=404, detail="当前产品没有可查看的图纸")
    try:
        path = resolve_stored_reference(reference)
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail="当前产品图纸文件不存在") from error
    if not path.is_file():
        raise HTTPException(status_code=404, detail="当前产品图纸文件不存在")
    metadata = stored_file_metadata(path)
    return FileResponse(
        path,
        media_type=str(
            metadata.get("content_type")
            or mimetypes.guess_type(path.name)[0]
            or "application/octet-stream"
        ),
        headers={
            "Cache-Control": "private, no-store, max-age=0",
            "Pragma": "no-cache",
            "X-Content-Type-Options": "nosniff",
            "X-Robots-Tag": "noindex, nofollow",
        },
    )


def _mobile_group_payload(
    category: str,
    *,
    total: int,
    page: int,
    page_size: int,
    items: list[dict],
) -> dict:
    return {
        "id": category,
        "label": _MOBILE_SEARCH_LABELS[category],
        "total": int(total),
        "page": int(page),
        "page_size": int(page_size),
        "has_more": page * page_size < total,
        "items": items,
    }


def _resolved_mobile_page(total: int, page: int, page_size: int) -> int:
    last_page = max(1, (int(total) + page_size - 1) // page_size)
    return min(max(int(page), 1), last_page)


def _mobile_order_search_group(
    db: Session,
    *,
    user: User,
    keyword: str,
    page: int,
    page_size: int,
    unfulfilled_only: bool = False,
    date_field: str = "order_date",
    date_from: date | None = None,
    date_to: date | None = None,
) -> dict:
    pattern = _escaped_like(keyword)
    statement = (
        select(OrderItem, Order, Customer)
        .join(Order, Order.id == OrderItem.order_id)
        .join(Customer, Customer.id == Order.customer_id)
        .where(
            or_(
                Order.order_number.ilike(pattern, escape="\\"),
                Order.customer_po.ilike(pattern, escape="\\"),
                OrderItem.item_order_number.ilike(pattern, escape="\\"),
                OrderItem.snapshot_product_code.ilike(pattern, escape="\\"),
                OrderItem.snapshot_product_name.ilike(pattern, escape="\\"),
                OrderItem.snapshot_spec.ilike(pattern, escape="\\"),
                Customer.name.ilike(pattern, escape="\\"),
                Customer.chinese_short_name.ilike(pattern, escape="\\"),
                Customer.customer_code.ilike(pattern, escape="\\"),
            )
        )
    )
    if unfulfilled_only:
        statement = statement.where(
            Order.status.notin_(("archived", "closed", "dead", "cancelled")),
            OrderItem.is_force_closed.is_(False),
            OrderItem.quantity > func.coalesce(OrderItem.delivered_quantity, 0),
        )
    date_column = Order.delivery_date if date_field == "delivery_date" else Order.order_date
    if date_from is not None:
        statement = statement.where(date_column >= date_from)
    if date_to is not None:
        statement = statement.where(date_column <= date_to)
    visible_customer_ids = _visible_customer_ids(user, db)
    if visible_customer_ids is not None:
        statement = statement.where(Order.customer_id.in_(visible_customer_ids))
    total = int(
        db.scalar(
            select(func.count()).select_from(statement.order_by(None).subquery())
        )
        or 0
    )
    resolved_page = _resolved_mobile_page(total, page, page_size)
    normalized = keyword.casefold()
    exact_rank = case(
        (func.lower(Order.order_number) == normalized, 0),
        (func.lower(OrderItem.item_order_number) == normalized, 0),
        (func.lower(OrderItem.snapshot_product_code) == normalized, 0),
        (func.lower(Order.customer_po) == normalized, 0),
        else_=1,
    )
    rows = db.execute(
        statement.order_by(
            exact_rank,
            Order.delivery_date.is_(None),
            Order.delivery_date,
            Order.id.desc(),
            OrderItem.id,
        )
        .offset((resolved_page - 1) * page_size)
        .limit(page_size)
    ).all()
    items = [
        {
            "order_id": order.id,
            "order_item_id": item.id,
            "order_number": order.order_number,
            "item_order_number": item.item_order_number,
            "customer_po": order.customer_po,
            "customer_name": customer.chinese_short_name or customer.name,
            "product_code": item.snapshot_product_code,
            "product_name": item.snapshot_product_name,
            "specification": item.snapshot_spec,
            "quantity": int(item.quantity or 0),
            "delivered_quantity": int(item.delivered_quantity or 0),
            "order_date": order.order_date.isoformat(),
            "remaining_quantity": max(
                int(item.quantity or 0) - int(item.delivered_quantity or 0), 0
            ),
            "delivery_date": order.delivery_date.isoformat()
            if order.delivery_date
            else None,
            "status": order.status,
        }
        for item, order, customer in rows
    ]
    return _mobile_group_payload(
        "orders",
        total=total,
        page=resolved_page,
        page_size=page_size,
        items=items,
    )


def _mobile_material_search_group(
    db: Session,
    *,
    user: User,
    keyword: str,
    page: int,
    page_size: int,
) -> dict:
    normalized = keyword.casefold()
    matches: list[dict] = []
    for route in _pending_incoming_route_rows(db, user):
        fields = (
            route.get("customer_name"),
            route.get("customer_code"),
            route.get("order_number"),
            route.get("item_order_number"),
            route.get("product_code"),
            route.get("product_name"),
            route.get("cardboard_len"),
            route.get("cardboard_width"),
        )
        compact_dimensions = "×".join(
            value
            for value in (
                _number_text(route.get("cardboard_len")),
                _number_text(route.get("cardboard_width")),
            )
            if value
        ).casefold()
        compact_query = re.sub(r"\s+", "", normalized).replace("x", "×")
        if not (
            any(normalized in str(value or "").casefold() for value in fields)
            or (compact_query and compact_query in compact_dimensions)
        ):
            continue
        matches.append(route)
    total = len(matches)
    resolved_page = _resolved_mobile_page(total, page, page_size)
    selected = matches[
        (resolved_page - 1) * page_size : resolved_page * page_size
    ]
    items = [
        {
            "route_id": str(route.get("item_id")),
            "customer_name": route.get("customer_name"),
            "customer_code": route.get("customer_code"),
            "order_number": route.get("order_number"),
            "customer_po": route.get("customer_po") or route.get("customer_order_number"),
            "item_order_number": route.get("item_order_number"),
            "product_code": route.get("product_code"),
            "product_name": route.get("product_name"),
            "report_length_mm": _number_text(route.get("cardboard_len")),
            "report_width_mm": _number_text(route.get("cardboard_width")),
            "material": route.get("material"),
            "flute_type": route.get("flute_type"),
            "crease_type": route.get("snapshot_crease_type"),
            "crease_values_mm": [
                _number_text(value)
                for value in (
                    route.get("snapshot_crease_left_mm"),
                    route.get("snapshot_crease_middle_mm"),
                    route.get("snapshot_crease_right_mm"),
                )
                if value is not None
            ],
            "pending_quantity": route.get("pending_quantity")
            or route.get("remaining_quantity")
            or route.get("requisition_qty"),
            "unit": route.get("unit") or "张",
        }
        for route in selected
    ]
    return _mobile_group_payload(
        "materials",
        total=total,
        page=resolved_page,
        page_size=page_size,
        items=items,
    )


def _mobile_mold_search_group(
    db: Session,
    *,
    user: User,
    keyword: str,
    page: int,
    page_size: int,
) -> dict:
    pattern = _escaped_like(keyword)
    visible_customer_ids = _visible_customer_ids(user, db)
    visible_product = [
        Product.mold_tool_id == MoldTool.id,
        Product.is_active.is_(True),
        Product.deleted_at.is_(None),
    ]
    if visible_customer_ids is not None:
        visible_product.append(Product.customer_id.in_(visible_customer_ids))
    visible_product_exists = exists(
        select(1).select_from(Product).where(*visible_product)
    )
    visible_relation = [MoldToolCustomer.mold_tool_id == MoldTool.id]
    if visible_customer_ids is not None:
        visible_relation.append(
            MoldToolCustomer.customer_id.in_(visible_customer_ids)
        )
    visible_relation_exists = exists(
        select(1).select_from(MoldToolCustomer).where(*visible_relation)
    )
    linked_match = exists(
        select(1)
        .select_from(Product)
        .join(Customer, Customer.id == Product.customer_id)
        .where(
            Product.mold_tool_id == MoldTool.id,
            Product.is_active.is_(True),
            Product.deleted_at.is_(None),
            or_(
                Product.product_code.ilike(pattern, escape="\\"),
                Product.customer_material_code.ilike(pattern, escape="\\"),
                Product.product_name.ilike(pattern, escape="\\"),
                Customer.name.ilike(pattern, escape="\\"),
                Customer.chinese_short_name.ilike(pattern, escape="\\"),
            ),
            *(
                [Product.customer_id.in_(visible_customer_ids)]
                if visible_customer_ids is not None
                else []
            ),
        )
    )
    associated_customer_match = exists(
        select(1)
        .select_from(MoldToolCustomer)
        .join(Customer, Customer.id == MoldToolCustomer.customer_id)
        .where(
            MoldToolCustomer.mold_tool_id == MoldTool.id,
            or_(
                Customer.name.ilike(pattern, escape="\\"),
                Customer.chinese_short_name.ilike(pattern, escape="\\"),
            ),
            *(
                [MoldToolCustomer.customer_id.in_(visible_customer_ids)]
                if visible_customer_ids is not None
                else []
            ),
        )
    )
    statement = (
        select(MoldTool)
        .options(
            selectinload(MoldTool.products).selectinload(Product.customer),
            selectinload(MoldTool.customer_links).selectinload(
                MoldToolCustomer.customer
            ),
        )
        .where(
            or_(
                MoldTool.mold_code.ilike(pattern, escape="\\"),
                MoldTool.mold_name.ilike(pattern, escape="\\"),
                MoldTool.label_name.ilike(pattern, escape="\\"),
                MoldTool.chinese_short_name.ilike(pattern, escape="\\"),
                MoldTool.rack_location.ilike(pattern, escape="\\"),
                linked_match,
                associated_customer_match,
            )
        )
    )
    if visible_customer_ids is not None:
        statement = statement.where(
            or_(visible_product_exists, visible_relation_exists)
        )
    total = int(
        db.scalar(
            select(func.count()).select_from(statement.order_by(None).subquery())
        )
        or 0
    )
    resolved_page = _resolved_mobile_page(total, page, page_size)
    molds = list(
        db.scalars(
            statement.order_by(MoldTool.mold_code, MoldTool.id)
            .offset((resolved_page - 1) * page_size)
            .limit(page_size)
        ).unique().all()
    )
    items: list[dict] = []
    for mold in molds:
        products = [
            product
            for product in mold.products
            if product.is_active
            and product.deleted_at is None
            and (
                visible_customer_ids is None
                or product.customer_id in visible_customer_ids
            )
        ]
        items.append(
            {
                "mold_id": mold.id,
                "display_name": mold.mold_name,
                "mold_name": mold.mold_name,
                "identity_status": mold.identity_status,
                "rack_location": mold.rack_location,
                "location_guide": describe_mold_location(mold.rack_location),
                "is_active": bool(mold.is_active),
                "archive_status": mold.archive_status,
                "repair_status": mold.repair_status,
                "products": [
                    {
                        "product_id": product.id,
                        "customer_name": product.customer.chinese_short_name
                        or product.customer.name,
                        "product_code": product.product_code,
                        "product_name": product.product_name,
                    }
                    for product in products[:5]
                ],
                "associated_customers": [
                    {
                        "customer_id": int(link.customer_id),
                        "customer_name": (
                            link.customer.chinese_short_name
                            or link.customer.name
                        ),
                        "display_order": link.display_order,
                    }
                    for link in sorted(
                        (
                            link
                            for link in mold.customer_links
                            if visible_customer_ids is None
                            or link.customer_id in visible_customer_ids
                        ),
                        key=lambda link: (
                            link.display_order is None,
                            link.display_order or 99,
                            link.customer_id,
                        ),
                    )
                ],
                "lookup_url": f"/mobile/mold-lookup?mold_id={mold.id}&readonly=1",
            }
        )
    return _mobile_group_payload(
        "molds",
        total=total,
        page=resolved_page,
        page_size=page_size,
        items=items,
    )


def _mobile_production_search_group(
    db: Session,
    *,
    user: User,
    keyword: str,
    page: int,
    page_size: int,
) -> dict:
    normalized = keyword.casefold()
    rows = list_production_task_dashboard_rows(
        db,
        allowed_customer_ids=_visible_customer_ids(user, db),
        status="pending",
    )
    matched = [
        row
        for row in rows
        if any(
            normalized in str(row.get(field) or "").casefold()
            for field in ("id", "customer_name", "order_number", "product_code")
        )
    ]
    total = len(matched)
    resolved_page = _resolved_mobile_page(total, page, page_size)
    selected = matched[
        (resolved_page - 1) * page_size : resolved_page * page_size
    ]
    selected_ids = [int(row["id"]) for row in selected]
    full_rows = list_production_tasks(
        db,
        allowed_customer_ids=_visible_customer_ids(user, db),
        status="pending",
        task_ids=selected_ids,
    )
    if {int(row["id"]) for row in full_rows} != set(selected_ids):
        raise HTTPException(status_code=409, detail="生产任务状态已变化，请重新查询")
    station: Literal["printing", "die_cut"] = (
        "printing"
        if has_permission(user, "production.printing.view")
        else "die_cut"
    )
    projected = _production_station_task_payloads(
        db,
        tasks=full_rows,
        station=station,
        mold_map_allowed=has_permission(user, "warehouse.view"),
    )
    projected_by_id = {int(row["task_id"]): row for row in projected}
    items = [projected_by_id[task_id] for task_id in selected_ids]
    return _mobile_group_payload(
        "production",
        total=total,
        page=resolved_page,
        page_size=page_size,
        items=items,
    )


def _mobile_inventory_search_group(
    db: Session,
    *,
    user: User,
    keyword: str,
    page: int,
    page_size: int,
) -> dict:
    statement = _product_query(db).where(_product_search_condition(keyword))
    visible_customer_ids = _visible_customer_ids(user, db)
    if visible_customer_ids is not None:
        statement = statement.where(Product.customer_id.in_(visible_customer_ids))
    total = int(
        db.scalar(
            select(func.count()).select_from(statement.order_by(None).subquery())
        )
        or 0
    )
    resolved_page = _resolved_mobile_page(total, page, page_size)
    normalized = keyword.casefold()
    exact_rank = case(
        (func.lower(Product.customer_material_code) == normalized, 0),
        (func.lower(Product.product_code) == normalized, 0),
        (func.lower(Customer.customer_code) == normalized, 1),
        else_=2,
    )
    products = list(
        db.scalars(
            statement.order_by(exact_rank, Customer.name, Product.id)
            .offset((resolved_page - 1) * page_size)
            .limit(page_size)
        ).all()
    )
    summaries = _product_inventory_summaries(db, products)
    return _mobile_group_payload(
        "inventory",
        total=total,
        page=resolved_page,
        page_size=page_size,
        items=[
            _product_payload(product, inventory_summary=summaries[product.id])
            for product in products
        ],
    )


@router.get("/search")
def search_mobile_portal(
    response: Response,
    q: str = Query(default="", max_length=100),
    category: Literal[
        "all", "orders", "materials", "molds", "production", "inventory"
    ] = Query(default="all"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=20),
    unfulfilled_only: bool = Query(default=False),
    date_field: Literal["order_date", "delivery_date"] = Query(default="order_date"),
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """Search only the business groups currently authorized for this account."""

    _no_store(response)
    response.headers["X-ERP-Session-Identity"] = f"{user.id}:{user.auth_version}"
    keyword = q.strip()
    if date_from is not None and date_to is not None and date_from > date_to:
        raise HTTPException(status_code=422, detail="开始日期不能晚于结束日期")
    if not keyword and not (category == "orders" and unfulfilled_only):
        raise HTTPException(status_code=422, detail="请输入订单、客户、产品、模具或尺寸")
    allowed_categories = _mobile_search_categories(user)
    if not allowed_categories:
        raise HTTPException(status_code=403, detail="当前账号没有可用的现场查询权限")
    if category != "all" and category not in allowed_categories:
        raise HTTPException(status_code=403, detail="当前账号没有该类现场资料的查看权限")

    requested_categories = (
        allowed_categories if category == "all" else [category]
    )
    resolved_page = 1 if category == "all" else page
    resolved_page_size = min(page_size, 5) if category == "all" else page_size
    builders = {
        "orders": _mobile_order_search_group,
        "materials": _mobile_material_search_group,
        "molds": _mobile_mold_search_group,
        "production": _mobile_production_search_group,
        "inventory": _mobile_inventory_search_group,
    }
    groups = [
        builders[group](
            db,
            user=user,
            keyword=keyword,
            page=resolved_page,
            page_size=resolved_page_size,
            **({"unfulfilled_only": unfulfilled_only, "date_field": date_field,
                "date_from": date_from, "date_to": date_to} if group == "orders" else {}),
        )
        for group in requested_categories
    ]
    return {
        "query": keyword,
        "category": category,
        "allowed_categories": allowed_categories,
        "groups": groups,
        "read_only": True,
        "as_of": datetime.now(_BEIJING).isoformat(timespec="seconds"),
    }


def _dimension_order_progress(db, user, items):
    """Use the shared production projection, never a guessed order status."""
    if not items or not has_permission(user, "incoming.view") or not (has_permission(user, "production.printing.view") or has_permission(user, "production.die_cut.view")):
        return
    ids = [row["order_item_id"] for row in items]
    task_ids = list(db.scalars(select(ProductionTask.id).where(ProductionTask.order_item_id.in_(ids), ProductionTask.sales_order_item_bom_component_id.is_(None))))
    tasks = list_production_tasks(db, allowed_customer_ids=_visible_customer_ids(user, db), task_ids=task_ids)
    by_item = {row["order_item_id"]: row for row in tasks}
    reservations = dict(db.execute(select(InventoryReservation.order_item_id, func.sum(InventoryReservation.credited_requirement_quantity - InventoryReservation.consumed_requirement_quantity - InventoryReservation.released_requirement_quantity)).join(InventoryLot, InventoryLot.id == InventoryReservation.inventory_lot_id).where(InventoryReservation.order_item_id.in_(ids), InventoryReservation.status.in_(("active", "partial")), InventoryReservation.sales_order_item_bom_component_id.is_(None), InventoryLot.inventory_type == "finished").group_by(InventoryReservation.order_item_id)).all())
    for row in items:
        task = by_item.get(row["order_item_id"], {})
        received = int(task.get("material_received_quantity") or 0)
        ready = int(task.get("delivery_ready_quantity") or 0)
        covered = max(0, int(reservations.get(row["order_item_id"]) or 0))
        completed = int(task.get("actual_output_quantity") or 0)
        if row["delivered_quantity"] > 0:
            label, color = "部分已送", "#7e22ce"
        elif ready > 0 or completed > row["delivered_quantity"]:
            label, color = "已完工待送", "#15803d"
        elif covered > 0:
            label, color = "库存已预占", "#1d4ed8"
        elif received > 0:
            label, color = ("已到料", "#15803d") if task.get("material_status") == "received" else ("部分到料", "#b45309")
        else:
            label, color = "未到料", "#64748b"
        row["progress"] = {"label": label, "color": color, "received_sheets": received, "completed_quantity": completed, "delivery_ready_quantity": ready, "finished_coverage": covered}


@router.get("/dimension-settings")
def mobile_dimension_settings_get(response: Response, user: User = Depends(can_read_orders)):
    from app.services.mobile_dimension_settings import read, public
    _no_store(response)
    return {**public(read()), "can_edit": user.role == "admin"}


@router.put("/dimension-settings")
def mobile_dimension_settings_put(response: Response, payload: dict = Body(...), user: User = Depends(can_read_orders)):
    from app.services.mobile_dimension_settings import save
    _no_store(response)
    if user.role != "admin": raise HTTPException(403, "仅管理员可修改查询范围")
    try:
        return save(near_mm=payload.get("near_mm"), expanded_mm=payload.get("expanded_mm"), expected_version=payload.get("expected_version"), operation_key=payload.get("operation_key"), user_id=user.id)
    except LookupError as exc: raise HTTPException(409, str(exc)) from exc
    except ValueError as exc: raise HTTPException(422, str(exc)) from exc


@router.get("/orders/by-dimensions")
def mobile_orders_by_dimensions(
    response: Response,
    dimensions: str = Query(min_length=1, max_length=100),
    domain: Literal["board", "box"] = Query(default="board"),
    customer: str = Query(default="", max_length=100),
    tolerance: int | None = Query(default=None, ge=0, le=50),
    axis: Literal["any", "length", "width"] = Query(default="any"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=20),
    db: Session = Depends(get_db), user: User = Depends(can_read_orders),
) -> dict:
    from app.services.mobile_order_dimensions import find_order_dimensions
    _no_store(response)
    response.headers["X-ERP-Session-Identity"] = f"{user.id}:{user.auth_version}"
    if domain == "board" and not has_permission(user, "incoming.view"):
        raise HTTPException(403, "当前账号没有纸板资料查看权限")
    try:
        from app.services.mobile_dimension_settings import read
        settings = read()
        tolerance = settings["near_mm"] if tolerance is None else tolerance
        if tolerance not in (0, settings["near_mm"], settings["expanded_mm"]):
            raise ValueError("查询范围已变化，请刷新范围设置后重试")
        group = find_order_dimensions(db, visible_ids=_visible_customer_ids(user, db), domain=domain, text=dimensions, customer=customer.strip(), tolerance=tolerance, axis=axis, page=page, page_size=page_size, near_tolerance=settings["near_mm"])
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    _dimension_order_progress(db, user, group["items"])
    return {"category": "orders", "groups": [group], "read_only": True, "tolerance_mm": tolerance}


@router.get("/orders/{order_id}/unfulfilled")
def mobile_order_unfulfilled(
    order_id: int, response: Response,
    db: Session = Depends(get_db), user: User = Depends(can_read_orders),
) -> dict:
    from app.services.mobile_order_dimensions import outstanding_query, line_payload
    _no_store(response)
    scope = _visible_customer_ids(user, db)
    order = db.get(Order, order_id)
    if order is None or (scope is not None and order.customer_id not in scope):
        raise HTTPException(404, "订单不存在或不在当前客户范围")
    items = [line_payload(*row) for row in db.execute(outstanding_query(scope).where(Order.id == order_id).order_by(OrderItem.id))]
    _dimension_order_progress(db, user, items)
    totals = {}
    for row in items:
        totals[row["unit"]] = totals.get(row["unit"], 0) + row["remaining_quantity"]
    return {"order_id": order_id, "items": items, "remaining_by_unit": totals, "read_only": True}


@router.get("/products")
def search_products(
    response: Response,
    q: str = Query(min_length=1, max_length=100),
    limit: int = Query(default=12, ge=1, le=30),
    include_zero: bool = Query(default=False),
    db: Session = Depends(get_db),
    user: User = Depends(can_read_inventory),
) -> dict:
    _no_store(response)
    keyword = q.strip()
    if not keyword:
        raise HTTPException(status_code=422, detail="请输入存货编码、客户或产品名称")
    if include_zero and user.role != "admin":
        raise HTTPException(status_code=403, detail="只有管理员可以查询零库存产品")
    statement = _product_query(db).where(_product_search_condition(keyword))
    visible_customer_ids = _visible_customer_ids(user, db)
    if visible_customer_ids is not None:
        statement = statement.where(Product.customer_id.in_(visible_customer_ids))
    matched_product_count = int(
        db.scalar(
            select(func.count()).select_from(statement.order_by(None).subquery())
        )
        or 0
    )
    stocked_statement = statement.where(_product_has_stock_condition())
    stocked_product_count = int(
        db.scalar(
            select(func.count()).select_from(
                stocked_statement.order_by(None).subquery()
            )
        )
        or 0
    )
    result_statement = statement if include_zero else stocked_statement
    normalized_keyword = keyword.casefold()
    exact_rank = case(
        (
            or_(
                func.lower(Product.customer_material_code) == normalized_keyword,
                func.lower(Product.product_code) == normalized_keyword,
            ),
            0,
        ),
        (func.lower(Customer.customer_code) == normalized_keyword, 1),
        (func.lower(Product.product_name) == normalized_keyword, 2),
        else_=3,
    )
    products = list(
        db.scalars(
            result_statement.order_by(
                exact_rank,
                Customer.name,
                Product.customer_material_code,
                Product.id,
            ).limit(limit)
        ).all()
    )
    summaries = _product_inventory_summaries(db, products)
    return {
        "query": keyword,
        "count": len(products),
        "matched_product_count": matched_product_count,
        "zero_stock_match_count": max(
            matched_product_count - stocked_product_count, 0
        ),
        "include_zero": include_zero,
        "include_zero_allowed": user.role == "admin",
        "requires_selection": len(products) > 1,
        "auto_selected": False,
        "items": [
            _product_payload(
                product,
                inventory_summary=summaries[product.id],
            )
            for product in products
        ],
        "as_of": datetime.now(_BEIJING).isoformat(timespec="seconds"),
    }


@router.get("/products/{product_id}/inventory")
def product_inventory(
    product_id: int,
    response: Response,
    db: Session = Depends(get_db),
    user: User = Depends(can_read_inventory),
) -> dict:
    _no_store(response)
    visible_customer_ids = _visible_customer_ids(user, db)
    product = _require_visible_product(
        db,
        product_id=product_id,
        visible_customer_ids=visible_customer_ids,
    )
    finished_lots = list(
        db.scalars(
            select(InventoryLot)
            .join(
                FinishedGoodsInventoryDetail,
                FinishedGoodsInventoryDetail.inventory_lot_id == InventoryLot.id,
            )
            .options(*_lot_load_options())
            .where(
                InventoryLot.inventory_type == "finished",
                InventoryLot.status == "active",
                or_(
                    InventoryLot.quantity_available > 0,
                    InventoryLot.quantity_reserved > 0,
                ),
                FinishedGoodsInventoryDetail.product_id == product.id,
                FinishedGoodsInventoryDetail.owner_customer_id == product.customer_id,
                FinishedGoodsInventoryDetail.is_general.is_(False),
                FinishedGoodsInventoryDetail.inventory_code_snapshot
                == product.product_code,
            )
            .order_by(InventoryLot.stock_date, InventoryLot.id)
        ).all()
    )
    semi_finished_lots = list(
        db.scalars(
            select(InventoryLot)
            .join(
                SemiFinishedInventoryDetail,
                SemiFinishedInventoryDetail.inventory_lot_id == InventoryLot.id,
            )
            .join(
                SemiFinishedLotAllowedProduct,
                SemiFinishedLotAllowedProduct.inventory_lot_id == InventoryLot.id,
            )
            .options(*_lot_load_options())
            .where(
                InventoryLot.inventory_type == "semi_finished",
                InventoryLot.status == "active",
                or_(
                    InventoryLot.quantity_available > 0,
                    InventoryLot.quantity_reserved > 0,
                ),
                SemiFinishedLotAllowedProduct.product_id == product.id,
                SemiFinishedInventoryDetail.owner_customer_id == product.customer_id,
            )
            .order_by(InventoryLot.stock_date, InventoryLot.id)
        ).all()
    )
    pending_pick = _pending_pick_by_lot(
        db, [lot.id for lot in finished_lots]
    )
    projection_contexts = load_warehouse_location_projection_contexts(
        db,
        [
            lot.location
            for lot in [*finished_lots, *semi_finished_lots]
            if lot.location is not None
        ],
    )
    timestamps = [
        lot.last_movement_at for lot in [*finished_lots, *semi_finished_lots]
    ]
    last_updated_at = (
        utc_naive_to_api(max(timestamps)) if timestamps else None
    )
    finished_group = _inventory_group(
        finished_lots,
        unit="只",
        pending_pick_by_lot=pending_pick,
        projection_contexts=projection_contexts,
    )
    semi_finished_group = _inventory_group(
        semi_finished_lots,
        unit="张",
        projection_contexts=projection_contexts,
    )
    mapped_positions = [
        position
        for position in [
            *finished_group["positions"],
            *semi_finished_group["positions"],
        ]
        if position["map_status"] == "mapped"
    ]
    map_url = None
    if mapped_positions:
        map_url = "/warehouse.html?" + urlencode(
            {
                "embedded": 1,
                "readonly": 1,
                "tab": "locations",
                "location_view": "floor3",
                "customer_id": product.customer_id,
                "keyword": product.customer_material_code or product.product_code,
                "source": "mobile-product-all",
            }
        )
    return {
        "product": _product_payload(product),
        "inventory": {
            "finished": finished_group,
            "semi_finished": semi_finished_group,
            "raw_material": {
                "state": "not_configured",
                "label": "原料仓尚未建立",
                "message": "当前不显示原料数量，避免把未知数据当成零库存。",
            },
        },
        "map_url": map_url,
        "mapped_position_count": len(mapped_positions),
        "last_updated_at": last_updated_at,
        "as_of": datetime.now(_BEIJING).isoformat(timespec="seconds"),
        "read_only": True,
    }


class MobileWarehouseMovePayload(BaseModel):
    expected_version: int = Field(gt=0)
    quantity: int = Field(gt=0)
    expected_source_location_id: int = Field(gt=0)
    expected_source_address_version: int = Field(gt=0)
    expected_source_layout_version: int = Field(gt=0)
    expected_source_map_revision: str = Field(min_length=1, max_length=64)
    target_location_id: int = Field(gt=0)
    expected_target_layout_version: int = Field(gt=0)
    expected_target_address_version: int = Field(gt=0)
    expected_target_map_revision: str = Field(min_length=1, max_length=64)
    idempotency_key: str = Field(min_length=1, max_length=100)
    physical_move_confirmed: bool
    location_discrepancy_id: int | None = Field(default=None, gt=0)
    expected_discrepancy_version: int | None = Field(default=None, gt=0)

    @field_validator(
        "idempotency_key",
        "expected_source_map_revision",
        "expected_target_map_revision",
    )
    @classmethod
    def strip_move_key(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("请求标识和地图版本不能为空")
        return text


class MobileWarehouseDiscrepancyPayload(BaseModel):
    inventory_lot_id: int = Field(gt=0)
    expected_lot_version: int = Field(gt=0)
    reported_quantity: int = Field(gt=0)
    observed_location_id: int = Field(gt=0)
    observed_location_layout_version: int = Field(gt=0)
    reason: str = Field(min_length=1, max_length=500)
    idempotency_key: str = Field(min_length=1, max_length=120)

    @field_validator("reason", "idempotency_key")
    @classmethod
    def strip_discrepancy_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("位置不符说明和请求标识不能为空")
        return text


class MobileWarehouseDiscrepancyResolvePayload(BaseModel):
    expected_version: int = Field(gt=0)
    expected_lot_version: int = Field(gt=0)
    idempotency_key: str = Field(min_length=1, max_length=100)
    resolution_note: str = Field(min_length=1, max_length=500)
    resolution_action: Literal["correct_ledger", "physical_returned"] = (
        "correct_ledger"
    )

    @field_validator("idempotency_key", "resolution_note")
    @classmethod
    def strip_resolution_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("纠正说明和请求标识不能为空")
        return text


class MobileWarehouseUnmatchedObservationPayload(BaseModel):
    product_id: int | None = Field(default=None, gt=0)
    observed_location_id: int = Field(gt=0)
    observed_location_layout_version: int = Field(gt=0)
    customer_keyword: str | None = Field(default=None, max_length=120)
    inventory_keyword: str = Field(min_length=1, max_length=200)
    reported_quantity: int | None = Field(default=None, gt=0)
    reported_unit: str | None = Field(default=None, max_length=20)
    reason: str = Field(min_length=1, max_length=500)
    idempotency_key: str = Field(min_length=1, max_length=120)

    @field_validator("customer_keyword", "reported_unit")
    @classmethod
    def strip_optional_unmatched_observation_text(
        cls, value: str | None
    ) -> str | None:
        text = str(value or "").strip()
        return text or None

    @field_validator("inventory_keyword", "reason", "idempotency_key")
    @classmethod
    def strip_required_unmatched_observation_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("存货关键词、现场说明和请求标识不能为空")
        return text


class MobileWarehouseUnmatchedResolvePayload(BaseModel):
    expected_version: int = Field(gt=0)
    resolution_note: str = Field(min_length=1, max_length=500)
    resolved_inventory_lot_id: int | None = Field(default=None, gt=0)
    idempotency_key: str = Field(min_length=1, max_length=120)

    @field_validator("resolution_note", "idempotency_key")
    @classmethod
    def strip_unmatched_resolution_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("处理说明和请求标识不能为空")
        return text


def _mobile_layout_payload(layout: Floor3LocationLayout) -> dict:
    return {
        "left_pct": float(layout.left_pct),
        "top_pct": float(layout.top_pct),
        "width_pct": float(layout.width_pct),
        "height_pct": float(layout.height_pct),
        "z_index": int(layout.z_index or 0),
        "version": int(layout.version),
    }


def _mobile_short_location_label(
    location: WarehouseLocation,
    *,
    canonical: Mapping,
    area_code: str,
) -> str:
    """Return one concise but unique physical address for the phone map."""

    compact_area = re.sub(r"^([A-Z]+)0+(\d+)$", r"\1\2", area_code.upper())
    if not canonical.get("map_rack_id") and canonical.get("employee_location_name"):
        return str(canonical["employee_location_name"])
    level_no = canonical.get("level_no")
    slot_no = canonical.get("slot_no")
    rack_name = str(canonical.get("rack_display_name") or "").strip()
    if rack_name and level_no and slot_no:
        return f"{rack_name}·{int(level_no)}层·{int(slot_no)}格"
    employee_name = str(canonical.get("employee_location_name") or "").strip()
    return employee_name or location.location_code


def _mobile_area_display_bounds(
    features: list[dict], *, area_code: str
) -> dict[str, float] | None:
    """Fit the phone canvas to one formal zone, not the entire warehouse floor."""

    normalized_area = str(area_code or "").strip().upper()
    zone = next(
        (
            feature
            for feature in features
            if str(feature.get("feature_kind") or "") == "zone"
            and str(feature.get("erp_area_code") or "").strip().upper()
            == normalized_area
        ),
        None,
    )
    points = zone.get("points") if isinstance(zone, dict) else None
    if not isinstance(points, list) or len(points) < 3:
        return None
    try:
        xs = [float(point[0]) for point in points]
        ys = [float(point[1]) for point in points]
    except (IndexError, TypeError, ValueError):
        return None
    if not xs or not ys or max(xs) <= min(xs) or max(ys) <= min(ys):
        return None
    return {
        "min_x": min(xs),
        "min_y": min(ys),
        "max_x": max(xs),
        "max_y": max(ys),
    }


def _mobile_lot_customer_id(lot: InventoryLot) -> int | None:
    if lot.finished_detail is not None:
        return lot.finished_detail.owner_customer_id
    if lot.semi_finished_detail is not None:
        return lot.semi_finished_detail.owner_customer_id
    return None


def _mobile_lot_options():
    return (
        selectinload(InventoryLot.location).selectinload(
            WarehouseLocation.floor3_layout
        ),
        selectinload(InventoryLot.finished_detail),
        selectinload(InventoryLot.semi_finished_detail),
    )


def _mobile_lot_is_visible(
    lot: InventoryLot, visible_customer_ids: set[int] | None
) -> bool:
    if visible_customer_ids is None:
        return True
    customer_id = _mobile_lot_customer_id(lot)
    return customer_id is not None and customer_id in visible_customer_ids


def _require_mobile_lot(
    db: Session,
    *,
    lot_id: int,
    visible_customer_ids: set[int] | None,
) -> InventoryLot:
    lot = db.scalar(
        select(InventoryLot)
        .options(*_mobile_lot_options())
        .where(InventoryLot.id == lot_id)
    )
    if lot is None or not _mobile_lot_is_visible(lot, visible_customer_ids):
        raise HTTPException(status_code=404, detail="库存不存在或当前账号无权查看")
    return lot


def _mobile_location_is_published(db: Session, location: WarehouseLocation) -> bool:
    context = load_warehouse_location_projection_contexts(db, [location]).get(
        int(location.id),
        {},
    )
    return (
        warehouse_location_projection(location, **context)["position_status"]
        == "mapped"
    )


def _claim_mobile_warehouse_floors(
    db: Session,
    *locations: WarehouseLocation,
) -> None:
    floor_numbers = sorted(
        {
            int(location.warehouse_floor)
            for location in locations
            if location.warehouse_floor is not None
        }
    )
    try:
        for floor_number in floor_numbers:
            if not claim_warehouse_floor_projection(
                db,
                floor_number=floor_number,
            ):
                raise HTTPException(
                    status_code=409,
                    detail="仓库楼层台账已变化，请刷新后重试",
                )
    except OperationalError as error:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="仓库区域正在调整，请稍后刷新重试",
        ) from error


def _mobile_goods_payload(lot: InventoryLot) -> dict:
    from app.services.warehouse_reading_identity import shelf_merge_identity
    from app.services.warehouse_display_units import lot_display_unit
    if lot.finished_detail is not None:
        detail = lot.finished_detail
        product_id = detail.product_id
        specification = dimension_specification(
            detail.length_mm,
            detail.width_mm,
            detail.height_mm,
        ) or ""
        customer_name = detail.owner_customer_name_snapshot
        product_code = detail.inventory_code_snapshot
        product_name = detail.product_name_snapshot
    else:
        detail = lot.semi_finished_detail
        product_id = None
        specification = (
            f"{detail.board_length_mm}×{detail.board_width_mm}mm"
            if detail is not None
            else ""
        )
        customer_name = detail.owner_customer_name_snapshot if detail else None
        product_code = None  # Material identity is not a customer product code.
        product_name = (detail.internal_name or ("原材料纸板" if detail.sheet_type == "raw_board" else "半成品纸板")) if detail else "半成品纸板"
    movable_quantity = int(lot.quantity_available or 0) + int(
        lot.quantity_reserved or 0
    )
    damaged_quantity = int(lot.quantity_damaged or 0)
    return {
        **shelf_merge_identity(lot),
        "location_id": lot.warehouse_location_id,
        "ledger_unit": lot.unit,
        "lot_id": int(lot.id),
        "lot_version": int(lot.version),
        "inventory_type": lot.inventory_type,
        "customer_id": _mobile_lot_customer_id(lot),
        "product_id": int(product_id) if product_id is not None else None,
        "customer_name": customer_name,
        "product_code": product_code,
        "product_name": product_name,
        "specification": specification,
        "flute_type": (getattr(detail, "flute_type_snapshot", None) or getattr(detail, "flute_type", None)) if detail else None,
        "material_code": getattr(detail, "material_code_snapshot", None) if detail else None,
        "quantity_available": int(lot.quantity_available or 0),
        "quantity_reserved": int(lot.quantity_reserved or 0),
        "quantity_damaged": damaged_quantity,
        "quantity_movable": movable_quantity,
        "quantity_total": movable_quantity + damaged_quantity,
        "status": lot.status,
        "unit": {"boxes": "只", "sheets": "张", "sets": "套", "pieces": "片"}.get(lot_display_unit(lot), lot_display_unit(lot)),
        "stock_date": lot.stock_date,
        "last_movement_at": utc_naive_to_api(lot.last_movement_at),
        "can_move": (
            lot.inventory_type in {"finished", "semi_finished"}
            and lot.status == "active"
            and damaged_quantity == 0
            and movable_quantity > 0
        ),
    }


def _mobile_location_summary(
    location: WarehouseLocation,
    context: Mapping[str, object],
) -> dict:
    projection = warehouse_location_projection(location, **context)
    address = location_address_payload(
        location,
        area=context.get("area"),
        floor=context.get("floor"),
        position_status=str(projection["position_status"]),
        area_sequence=(
            int(context["area_sequence"])
            if context.get("area_sequence")
            else None
        ),
    )
    layout = context.get("layout")
    return {
        "location_id": int(location.id),
        "location_code": location.location_code,
        "employee_location_name": address["employee_location_name"],
        "floor": location.warehouse_floor,
        "area_code": location.area_code,
        **rack_cell_identity_payload(location),
        "layout_version": (
            int(layout.version)
            if isinstance(layout, Floor3LocationLayout)
            else None
        ),
        "published_map_revision": projection.get("published_map_revision"),
        "position_status": projection["position_status"],
        "map_issue": projection["map_issue"],
    }


def _mobile_floor_code(row) -> str:
    if row.floor is not None:
        return str(row.floor.floor_code).upper()
    return f"{int(row.location.warehouse_floor or 0)}F"


def _mobile_area_name(row) -> str:
    return employee_area_name(
        row.area,
        area_code=row.location.area_code,
        floor_number=(
            row.floor.floor_number
            if row.floor is not None
            else row.location.warehouse_floor
        ),
    )


@router.get("/warehouse/map/floors")
def mobile_warehouse_map_floors(
    response: Response,
    db: Session = Depends(get_db),
    user: User = Depends(can_read_inventory),
) -> dict:
    _no_store(response)
    rows = list_operational_locations(db)
    grouped: dict[str, dict] = {}
    for row in rows:
        location_payload = operational_location_payload(row)
        floor_code = _mobile_floor_code(row)
        floor = grouped.setdefault(
            floor_code,
            {
                "floor_code": floor_code,
                "floor_name": row.floor.floor_name if row.floor else f"{floor_code} 仓库",
                "floor_number": row.location.warehouse_floor,
                "areas": {},
            },
        )
        area_code = str(row.location.area_code or "").strip().upper()
        area = floor["areas"].setdefault(
            area_code,
            {
                "area_code": area_code,
                "area_name": _mobile_area_name(row),
                "area_master_name": row.area.area_name if row.area else None,
                "published_location_count": 0,
            },
        )
        if location_payload["position_status"] == "mapped":
            area["published_location_count"] += 1
    floors: list[dict] = []
    for floor in sorted(
        grouped.values(), key=lambda item: (item["floor_number"] or 0, item["floor_code"])
    ):
        areas = []
        for area in sorted(floor.pop("areas").values(), key=lambda item: item["area_code"]):
            area["map_status"] = (
                "ready" if area["published_location_count"] else "unmeasured"
            )
            area["map_status_text"] = (
                "实测地图已建立"
                if area["published_location_count"]
                else "未建立实测地图"
            )
            areas.append(area)
        floor["areas"] = areas
        floors.append(floor)
    return {
        "floors": floors,
        "can_execute": has_permission(user, "warehouse.execute"),
        "can_correct": has_permission(user, "warehouse.correct"),
        "as_of": datetime.now(_BEIJING).isoformat(timespec="seconds"),
    }


def _mobile_map_compass(layout: dict | None) -> dict:
    """Match EditorCanvas's published-layout east calibration, never phone heading."""
    layout = layout or {}
    floor_code = str(layout.get("floor_code") or "").upper()
    calibration = (layout.get("metadata") or {}).get("calibration") or layout.get("calibration") or {}
    aligned = (calibration.get("status") == "aligned" and calibration.get("applied") is True) or (
        layout.get("alignment_status") == "aligned" and layout.get("alignment_applied") is True)
    real_east = floor_code in {"1F", "3F"} or (floor_code == "4F" and aligned)
    return {"code": "E" if real_east else "N", "label": "现实东向 E" if real_east else "图纸北向 N（未校准）"}


@router.get("/warehouse/map/overview/{floor_code}")
def mobile_warehouse_floor_overview(
    floor_code: str,
    response: Response,
    db: Session = Depends(get_db),
    user: User = Depends(can_read_inventory),
) -> dict:
    """Only area geometry/identity; no selectable locations or stock details."""
    _no_store(response)
    normalized = floor_code.strip().upper()
    floors = mobile_warehouse_map_floors(response=response, db=db, user=user)["floors"]
    floor = next((item for item in floors if item["floor_code"] == normalized), None)
    if floor is None:
        raise HTTPException(status_code=404, detail="楼层不存在或尚未启用")
    try:
        layout = overlay_formal_area_bindings(db, floor_code=normalized,
            floor_layout=load_warehouse_twin_floor(normalized))
    except (WarehouseTwinLayoutNotFoundError, ValueError):
        return {"floor_code": normalized, "floor_name": floor["floor_name"], "areas": [], "map_status": "unmeasured"}
    by_code = {area["area_code"]: area for area in floor["areas"]}
    areas = [{"feature_id": feature["id"], "area_code": feature["erp_area_code"],
              "area_name": by_code[feature["erp_area_code"]]["area_name"], "points": feature["points"]}
             for feature in layout.get("features", [])
             if feature.get("feature_kind") == "zone" and feature.get("erp_area_code") in by_code
             and len(feature.get("points") or []) >= 3]
    return {"floor_code": normalized, "floor_name": floor["floor_name"], "areas": areas,
            "compass": _mobile_map_compass(layout),
            "map_status": "ready" if areas else "unmeasured"}


@router.get("/warehouse/map/locations/{location_id}")
def mobile_warehouse_map_location_identity(
    location_id: int,
    response: Response,
    db: Session = Depends(get_db),
    _user: User = Depends(can_read_inventory),
) -> dict:
    """Resolve a stable location deep link before choosing floor and area."""

    _no_store(response)
    row = next(
        (
            item
            for item in list_operational_locations(db)
            if int(item.location.id) == int(location_id)
        ),
        None,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="仓库货位不存在或尚未启用")
    payload = operational_location_payload(row)
    return {
        **payload,
        "location_id": int(row.location.id),
        "floor_code": _mobile_floor_code(row),
        "area_code": str(row.location.area_code or "").strip().upper(),
    }


@router.get("/warehouse/map/racks/{floor_code}/{rack_id}")
def mobile_warehouse_map_rack_identity(
    floor_code: str,
    rack_id: str,
    response: Response,
    db: Session = Depends(get_db),
    _user: User = Depends(can_read_inventory),
) -> dict:
    """Resolve one persistent map-rack identity without trusting a display name."""

    _no_store(response)
    normalized_floor = str(floor_code or "").strip().upper()
    normalized_rack = str(rack_id or "").strip()
    if not re.fullmatch(r"\d{1,2}F", normalized_floor) or not re.fullmatch(
        r"[A-Za-z0-9._:-]{1,80}", normalized_rack
    ):
        raise HTTPException(status_code=422, detail="货架二维码身份无效")
    matching = [
        row
        for row in list_operational_locations(db)
        if _mobile_floor_code(row) == normalized_floor
        and str(row.location.map_rack_id or "") == normalized_rack
    ]
    if not matching:
        raise HTTPException(status_code=404, detail="仓库货架不存在或尚未启用")
    area_codes = {
        str(row.location.area_code or "").strip().upper() for row in matching
    }
    if len(area_codes) != 1:
        raise HTTPException(status_code=409, detail="货架空间身份冲突，请管理员核对地图")
    first = matching[0]
    return {
        "floor_code": normalized_floor,
        "area_code": next(iter(area_codes)),
        "rack_id": normalized_rack,
        "rack_display_name": first.location.rack_display_name or first.location.rack_code,
        "location_id": int(first.location.id),
        "location_count": len(matching),
    }


@router.get("/warehouse/map/lots/{lot_id}")
def mobile_warehouse_map_lot_identity(
    lot_id: int,
    response: Response,
    db: Session = Depends(get_db),
    user: User = Depends(can_read_inventory),
) -> dict:
    """Resolve a visible inventory lot to its current physical location."""

    _no_store(response)
    lot = _require_mobile_lot(
        db,
        lot_id=lot_id,
        visible_customer_ids=_visible_customer_ids(user, db),
    )
    location = lot.location
    if location is None or not _mobile_location_is_published(db, location):
        raise HTTPException(status_code=409, detail="当前库存批次尚未绑定已发布货位")
    row = next(
        (
            item
            for item in list_operational_locations(db)
            if int(item.location.id) == int(location.id)
        ),
        None,
    )
    if row is None:
        raise HTTPException(status_code=409, detail="当前库存批次货位尚未启用")
    return {
        "lot_id": int(lot.id),
        "location_id": int(location.id),
        "floor_code": _mobile_floor_code(row),
        "area_code": str(location.area_code or "").strip().upper(),
        "rack_id": location.map_rack_id,
    }


@router.get("/warehouse/map/floors/{floor_code}")
def mobile_warehouse_map_area(
    floor_code: str,
    response: Response,
    area_code: str = Query(min_length=1, max_length=30),
    db: Session = Depends(get_db),
    user: User = Depends(can_read_inventory),
) -> dict:
    _no_store(response)
    normalized_floor = floor_code.strip().upper()
    normalized_area = area_code.strip().upper()
    rows = [
        row
        for row in list_operational_locations(db)
        if _mobile_floor_code(row) == normalized_floor
        and str(row.location.area_code or "").strip().upper() == normalized_area
    ]
    if not rows:
        raise HTTPException(status_code=404, detail="仓库楼层或区域不存在或尚未启用")
    area_name = _mobile_area_name(rows[0])
    locations = [row.location for row in rows]
    location_ids = [int(location.id) for location in locations]
    lots = list(
        db.scalars(
            select(InventoryLot)
            .options(*_mobile_lot_options())
            .where(
                InventoryLot.warehouse_location_id.in_(location_ids),
                InventoryLot.status.in_(("active", "frozen")),
                (
                    InventoryLot.quantity_available
                    + InventoryLot.quantity_reserved
                    + InventoryLot.quantity_damaged
                )
                > 0,
            )
            .order_by(InventoryLot.stock_date, InventoryLot.id)
        ).all()
    )
    visible_customer_ids = _visible_customer_ids(user, db)
    unmatched_counts = {
        int(location_id): int(count or 0)
        for location_id, count in db.execute(
            select(
                WarehouseUnmatchedInventoryObservation.observed_location_id,
                func.count(WarehouseUnmatchedInventoryObservation.id),
            )
            .where(
                WarehouseUnmatchedInventoryObservation.status == "open",
                WarehouseUnmatchedInventoryObservation.observed_location_id.in_(
                    location_ids
                ),
            )
            .group_by(
                WarehouseUnmatchedInventoryObservation.observed_location_id
            )
        ).all()
    }
    discrepancy_rows = list(
        db.scalars(
            select(WarehouseLocationDiscrepancy)
            .where(
                WarehouseLocationDiscrepancy.status == "open",
                WarehouseLocationDiscrepancy.observed_location_id.in_(location_ids),
            )
            .order_by(
                WarehouseLocationDiscrepancy.reported_at,
                WarehouseLocationDiscrepancy.id,
            )
        ).all()
    )
    discrepancy_lot_ids = {
        int(report.inventory_lot_id) for report in discrepancy_rows
    }
    discrepancy_lots_by_id = {
        int(lot.id): lot
        for lot in db.scalars(
            select(InventoryLot)
            .options(*_mobile_lot_options())
            .where(
                InventoryLot.id.in_(discrepancy_lot_ids),
                InventoryLot.status.in_(("active", "frozen")),
                (
                    InventoryLot.quantity_available
                    + InventoryLot.quantity_reserved
                    + InventoryLot.quantity_damaged
                )
                > 0,
            )
        ).all()
    }
    discrepancy_registered_locations = [
        lot.location
        for lot in discrepancy_lots_by_id.values()
        if lot.location is not None
    ]
    discrepancy_location_contexts = load_warehouse_location_projection_contexts(
        db,
        discrepancy_registered_locations,
    )
    discrepancies_by_location: dict[int, list[dict]] = {}
    for report in discrepancy_rows:
        lot = discrepancy_lots_by_id.get(int(report.inventory_lot_id))
        if lot is None or not _mobile_lot_is_visible(lot, visible_customer_ids):
            continue
        registered = lot.location
        if registered is None:
            continue
        discrepancies_by_location.setdefault(
            int(report.observed_location_id), []
        ).append(
            {
                "report_id": int(report.id),
                "report_version": int(report.version),
                "reported_quantity": int(report.reported_quantity),
                "reason": report.reason,
                "reported_at": utc_naive_to_api(report.reported_at),
                "lot": _mobile_goods_payload(lot),
                "registered_location": _mobile_location_summary(
                    registered,
                    discrepancy_location_contexts.get(int(registered.id), {}),
                ),
            }
        )
    goods_by_location: dict[int, list[dict]] = {}
    visible_lots = [lot for lot in lots if _mobile_lot_is_visible(lot, visible_customer_ids)]
    customer_ids = {_mobile_lot_customer_id(lot) for lot in visible_lots} - {None}
    customer_short_names = dict(db.execute(select(Customer.id, Customer.chinese_short_name).where(Customer.id.in_(customer_ids))).all()) if customer_ids else {}
    for lot in visible_lots:
        goods_by_location.setdefault(int(lot.warehouse_location_id), []).append({
            **_mobile_goods_payload(lot),
            "customer_short_name": customer_short_names.get(_mobile_lot_customer_id(lot)),
        })
    map_floor: dict | None = None
    try:
        map_floor = overlay_formal_area_bindings(
            db,
            floor_code=normalized_floor,
            floor_layout=load_warehouse_twin_floor(normalized_floor),
        )
    except (WarehouseTwinLayoutNotFoundError, ValueError):
        map_floor = None
    features: list[dict] = []
    area_display_bounds: dict[str, float] | None = None
    if map_floor is not None:
        raw_features = list(map_floor.get("features") or [])
        area_display_bounds = _mobile_area_display_bounds(
            raw_features,
            area_code=normalized_area,
        )
        for raw in raw_features:
            kind = str(raw.get("feature_kind") or "")
            bound_area = str(raw.get("erp_area_code") or "").strip().upper()
            if kind == "aisle" or (kind == "zone" and bound_area == normalized_area):
                features.append(
                    {
                        "id": raw.get("id"),
                        "feature_kind": kind,
                        "name": (
                            area_name
                            if kind == "zone"
                            else raw.get("name")
                        ),
                        "points": raw.get("points") or [],
                    }
                )
    unrestricted = visible_customer_ids is None
    location_payloads = []
    from app.services.warehouse_location_sequence import applied_ground_geometry
    area_feature = next((feature for feature in (map_floor or {}).get("features", [])
                         if feature.get("erp_area_code") == normalized_area), None)
    for row in rows:
        location = row.location
        canonical = operational_location_payload(row)
        is_mapped = canonical["position_status"] == "mapped"
        layout = (row.projection_context or {}).get("layout") if is_mapped else None
        goods = goods_by_location.get(int(location.id), [])
        discrepant_goods = discrepancies_by_location.get(int(location.id), [])
        location_payloads.append(
            {
                "location_id": int(location.id),
                "location_code": location.location_code,
                "location_name": canonical["employee_location_name"],
                "location_master_name": location.location_name,
                "current_address_name": canonical["current_address_name"],
                "employee_location_name": canonical["employee_location_name"],
                "short_location_label": _mobile_short_location_label(
                    location,
                    canonical=canonical,
                    area_code=normalized_area,
                ),
                "storage_type": location.storage_type,
                "area_code": normalized_area,
                "map_rack_id": canonical["map_rack_id"],
                "rack_display_name": canonical["rack_display_name"],
                "level_no": canonical["level_no"],
                "slot_no": canonical["slot_no"],
                "address_version": canonical["address_version"],
                "layout_version": canonical["layout_version"],
                "position_status": canonical["position_status"],
                "map_issue": canonical["map_issue"],
                "published_map_revision": canonical["published_map_revision"],
                "map_feature_id": canonical["map_feature_id"],
                "geometry": (
                    {**_mobile_layout_payload(layout), **applied_ground_geometry(location.id, layout, area_feature)}
                    if isinstance(layout, Floor3LocationLayout)
                    else None
                ),
                "map_status": "ready" if is_mapped else "unmeasured",
                "map_status_text": (
                    "实测地图已建立"
                    if is_mapped
                    else str(canonical["map_issue"] or "未建立实测地图")
                ),
                "occupancy_state": (
                    "occupied" if goods else "empty"
                )
                if unrestricted
                else ("visible_goods" if goods else "not_disclosed"),
                "goods": goods,
                "has_unmatched_inventory_observation": bool(
                    unmatched_counts.get(int(location.id), 0)
                ),
                "unmatched_inventory_observation_count": unmatched_counts.get(
                    int(location.id), 0
                ),
                "has_location_discrepancy": bool(discrepant_goods),
                "location_discrepancy_count": len(discrepant_goods),
                "discrepant_goods": discrepant_goods,
                "can_select_target": is_mapped,
            }
        )
    has_geometry = any(item["geometry"] is not None for item in location_payloads)
    floor_name = rows[0].floor.floor_name if rows[0].floor else "楼层名称待完善"
    return {
        "floor_code": normalized_floor,
        "floor_name": floor_name,
        "area_code": normalized_area,
        "area_name": area_name,
        "area_master_name": rows[0].area.area_name if rows[0].area else None,
        "map_status": "ready" if has_geometry else "unmeasured",
        "map_status_text": "实测地图已建立" if has_geometry else "未建立实测地图",
        "guidance": (
            f"{floor_name} {area_name}，以地图高亮位置为准；到现场后核对相邻位置。"
            if has_geometry
            else "未建立实测地图，只能查看文字区域和库位；系统不会生成假坐标或编号格子。"
        ),
        "bounds_mm": area_display_bounds,
        "compass": _mobile_map_compass(map_floor),
        "features": features,
        "locations": location_payloads,
        "can_execute": has_permission(user, "warehouse.execute"),
        "can_correct": has_permission(user, "warehouse.correct"),
        "as_of": datetime.now(_BEIJING).isoformat(timespec="seconds"),
    }


@router.get("/warehouse/physical-inventory/search")
def search_mobile_warehouse_physical_inventory(
    response: Response,
    customer_keyword: str | None = Query(default=None, max_length=120),
    inventory_keyword: str | None = Query(default=None, max_length=200),
    customer_id: int | None = Query(default=None, gt=0),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=200),
    db: Session = Depends(get_db),
    user: User = Depends(can_read_inventory),
) -> dict:
    """Search every positive physical finished lot visible to the employee."""

    _no_store(response)
    customer_text = str(customer_keyword or "").strip()
    inventory_text = str(inventory_keyword or "").strip()
    if not customer_text and not inventory_text and customer_id is None:
        raise HTTPException(status_code=422, detail="请输入客户简称或存货编码等关键词")

    filters = [
        InventoryLot.inventory_type == "finished",
        InventoryLot.status.in_(("active", "frozen")),
        (
            InventoryLot.quantity_available
            + InventoryLot.quantity_reserved
            + InventoryLot.quantity_damaged
        )
        > 0,
    ]
    visible_customer_ids = _visible_customer_ids(user, db)
    if visible_customer_ids is not None:
        filters.append(
            FinishedGoodsInventoryDetail.owner_customer_id.in_(visible_customer_ids)
        )
    if isinstance(customer_id, int):
        filters.append(FinishedGoodsInventoryDetail.owner_customer_id == customer_id)
    if customer_text:
        pattern = f"%{customer_text}%"
        filters.append(
            or_(
                FinishedGoodsInventoryDetail.owner_customer_name_snapshot.ilike(pattern),
                Customer.name.ilike(pattern),
                Customer.chinese_short_name.ilike(pattern),
                Customer.customer_code.ilike(pattern),
            )
        )
    if inventory_text:
        pattern = f"%{inventory_text}%"
        filters.append(
            or_(
                InventoryLot.lot_number.ilike(pattern),
                FinishedGoodsInventoryDetail.inventory_code_snapshot.ilike(pattern),
                FinishedGoodsInventoryDetail.product_name_snapshot.ilike(pattern),
                Product.product_code.ilike(pattern),
                Product.customer_material_code.ilike(pattern),
                Product.product_name.ilike(pattern),
                (cast(FinishedGoodsInventoryDetail.length_mm, String) + "×" + cast(FinishedGoodsInventoryDetail.width_mm, String) + "×" + func.coalesce(cast(FinishedGoodsInventoryDetail.height_mm, String), "")).ilike("%" + "".join(inventory_text.split()).replace("*", "×").replace("x", "×").replace("X", "×") + "%"),
            )
        )

    def joined_statement():
        return (
            select(InventoryLot)
            .join(
                FinishedGoodsInventoryDetail,
                FinishedGoodsInventoryDetail.inventory_lot_id == InventoryLot.id,
            )
            .outerjoin(
                Customer,
                Customer.id == FinishedGoodsInventoryDetail.owner_customer_id,
            )
            .outerjoin(Product, Product.id == FinishedGoodsInventoryDetail.product_id)
            .join(WarehouseLocation, WarehouseLocation.id == InventoryLot.warehouse_location_id)
            .where(*filters)
        )

    total = int(
        db.scalar(
            joined_statement()
            .with_only_columns(func.count(InventoryLot.id))
            .order_by(None)
        )
        or 0
    )
    lots = list(
        db.scalars(
            joined_statement()
            .options(*_mobile_lot_options())
            .order_by(
                case((WarehouseLocation.location_code == "RECOUNT-PENDING", 0), else_=1),
                FinishedGoodsInventoryDetail.owner_customer_name_snapshot,
                FinishedGoodsInventoryDetail.inventory_code_snapshot,
                InventoryLot.stock_date,
                InventoryLot.id,
            )
            .offset(offset)
            .limit(limit)
        ).all()
    )
    contexts = load_warehouse_location_projection_contexts(
        db, [lot.location for lot in lots if lot.location is not None]
    )
    items = []
    for lot in lots:
        location = lot.location
        from app.services.warehouse_relocation_pending import is_pending_relocation_location
        context = contexts.get(int(location.id), {})
        projection = warehouse_location_projection(location, **context)
        address = location_address_payload(
            location,
            area=context.get("area"),
            floor=context.get("floor"),
            position_status=str(projection["position_status"]),
            area_sequence=(
                int(context["area_sequence"])
                if context.get("area_sequence")
                else None
            ),
        )
        items.append(
            {
                **_mobile_goods_payload(lot),
                "registered_location": {
                    "is_pending_relocation": is_pending_relocation_location(location),
                    "location_id": int(location.id),
                    "location_code": location.location_code,
                    "address_version": location.address_version,
                    "employee_location_name": address["employee_location_name"],
                    "floor": location.warehouse_floor,
                    "area_code": location.area_code,
                    **rack_cell_identity_payload(location),
                    "layout_version": (
                        int(context["layout"].version)
                        if isinstance(
                            context.get("layout"), Floor3LocationLayout
                        )
                        else None
                    ),
                    "published_map_revision": projection.get(
                        "published_map_revision"
                    ),
                    "position_status": projection["position_status"],
                },
            }
        )
    return {
        "items": items,
        "total": total,
        "offset": offset,
        "limit": limit,
        "has_more": offset + len(items) < total,
        "search_scope": "all_positive_physical_finished_inventory",
    }


@router.post("/warehouse/lots/{lot_id}/moves")
def mobile_move_warehouse_lot(
    lot_id: int,
    payload: MobileWarehouseMovePayload,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    user: User = Depends(can_execute_inventory),
) -> dict:
    _no_store(response)
    if not payload.physical_move_confirmed:
        raise HTTPException(status_code=409, detail="请先完成现场搬运并最终确认")
    if (payload.location_discrepancy_id is None) != (
        payload.expected_discrepancy_version is None
    ):
        raise HTTPException(
            status_code=422,
            detail="位置不符报告编号和版本必须同时提交",
        )
    visible_customer_ids = _visible_customer_ids(user, db)
    source_lot = _require_mobile_lot(
        db, lot_id=lot_id, visible_customer_ids=visible_customer_ids
    )
    customer_id = _mobile_lot_customer_id(source_lot)
    discrepancy: WarehouseLocationDiscrepancy | None = None
    discrepancy_already_resolved = False
    if payload.location_discrepancy_id is not None:
        if not has_permission(user, "warehouse.correct"):
            raise HTTPException(status_code=403, detail="当前账号没有仓库位置纠正权限")
        discrepancy = db.get(
            WarehouseLocationDiscrepancy,
            payload.location_discrepancy_id,
        )
        if discrepancy is None or discrepancy.inventory_lot_id != source_lot.id:
            raise HTTPException(status_code=404, detail="位置不符报告不存在或不属于该批货物")
        if discrepancy.status == "resolved":
            transfer = (
                db.get(InventoryLotTransfer, discrepancy.resolution_transfer_id)
                if discrepancy.resolution_transfer_id is not None
                else None
            )
            if transfer is None or transfer.idempotency_key != payload.idempotency_key:
                raise HTTPException(status_code=409, detail="该位置不符报告已经处理")
            discrepancy_already_resolved = True
        elif (
            discrepancy.status != "open"
            or int(discrepancy.version)
            != int(payload.expected_discrepancy_version or 0)
        ):
            raise HTTPException(status_code=409, detail="位置不符报告状态已变化，请刷新后重试")
        elif source_lot.warehouse_location_id != discrepancy.registered_location_id:
            raise HTTPException(status_code=409, detail="库存登记位置已变化，请刷新后重新核对")
        elif int(discrepancy.reported_quantity) != int(payload.quantity):
            raise HTTPException(status_code=409, detail="请一次搬完该红色标记记录的现场数量")
    try:
        from app.services.warehouse_sheet_transfer import transfer_sheet_lot_between_locations
        transfer_lot = (transfer_sheet_lot_between_locations
                        if source_lot.inventory_type == "semi_finished"
                        else transfer_finished_lot_between_locations)
        result = transfer_lot(
            db,
            lot_id=lot_id,
            expected_version=payload.expected_version,
            quantity=payload.quantity,
            location_id=payload.target_location_id,
            expected_source_location_id=payload.expected_source_location_id,
            expected_source_address_version=payload.expected_source_address_version,
            expected_source_layout_version=payload.expected_source_layout_version,
            expected_source_map_revision=payload.expected_source_map_revision,
            expected_target_layout_version=payload.expected_target_layout_version,
            expected_target_address_version=payload.expected_target_address_version,
            expected_target_map_revision=payload.expected_target_map_revision,
            operator_id=user.id,
            idempotency_key=payload.idempotency_key,
        )
        if discrepancy_already_resolved:
            if (
                discrepancy is None
                or discrepancy.resolution_transfer_id != result.transfer.id
                or not result.replayed
            ):
                raise WarehouseInventoryError("该位置不符报告已经由其他请求处理", 409)
        elif discrepancy is not None:
            discrepancy.status = "resolved"
            discrepancy.version = int(discrepancy.version) + 1
            discrepancy.resolved_by = user.id
            discrepancy.resolved_at = utc_now_naive()
            discrepancy.resolution_transfer_id = result.transfer.id
            discrepancy.resolution_note = "现场错位货物已搬到另一正式货位"
        if not result.replayed:
            append_audit_event(
                db,
                request=request,
                actor=user,
                event_category="business",
                result="success",
                source="mobile",
                module_code="warehouse",
                action_code="warehouse.mobile_lot.location_move",
                resource="InventoryLotTransfer",
                entity_type="inventory_lot_transfer",
                entity_id=result.transfer.id,
                object_ref=f"inventory_lot_transfer:{result.transfer.id}",
                customer_id=customer_id,
                description="手机实测仓库地图确认实际搬运",
                details={
                    "source_lot_id": lot_id,
                    "target_lot_id": result.target_lot.id,
                    "source_location_id": result.transfer.source_location_id,
                    "target_location_id": result.transfer.target_location_id,
                    "expected_source_address_version": (
                        payload.expected_source_address_version
                    ),
                    "expected_source_layout_version": (
                        payload.expected_source_layout_version
                    ),
                    "expected_source_map_revision": (
                        payload.expected_source_map_revision
                    ),
                    "expected_target_layout_version": (
                        payload.expected_target_layout_version
                    ),
                    "expected_target_address_version": (
                        payload.expected_target_address_version
                    ),
                    "expected_target_map_revision": (
                        payload.expected_target_map_revision
                    ),
                    "quantity": payload.quantity,
                    "idempotency_key": payload.idempotency_key,
                    "resolved_location_discrepancy_id": (
                        int(discrepancy.id) if discrepancy is not None else None
                    ),
                },
            )
        db.commit()
        return {
            "message": (
                "现场错位货物已搬到正确货位，红色标记已关闭"
                if discrepancy is not None
                else "实际搬运已登记，库存总数未改变"
            ),
            "idempotent_replay": result.replayed,
            "transfer_id": result.transfer.id,
            "resolved_location_discrepancy_id": (
                int(discrepancy.id) if discrepancy is not None else None
            ),
            "source_lot": _mobile_goods_payload(result.source_lot),
            "target_lot": _mobile_goods_payload(result.target_lot),
        }
    except WarehouseInventoryError as error:
        db.rollback()
        raise HTTPException(status_code=error.status_code, detail=str(error)) from error
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(status_code=409, detail="库存位置或请求标识已变化，请刷新重试") from error


def _mobile_discrepancy_payload(
    db: Session,
    row: WarehouseLocationDiscrepancy,
    *,
    lot: InventoryLot,
    registered: WarehouseLocation,
    observed: WarehouseLocation,
    projection_contexts: Mapping[int, Mapping[str, object]] | None = None,
) -> dict:
    contexts = (
        projection_contexts
        if projection_contexts is not None
        else load_warehouse_location_projection_contexts(
            db,
            [registered, observed],
        )
    )

    def location_payload(location: WarehouseLocation) -> dict:
        context = contexts.get(int(location.id), {})
        projection = warehouse_location_projection(location, **context)
        address = location_address_payload(
            location,
            area=context.get("area"),
            floor=context.get("floor"),
            position_status=str(projection["position_status"]),
            area_sequence=(int(context["area_sequence"]) if context.get("area_sequence") else None),
        )
        return {
            "location_id": location.id,
            "location_code": location.location_code,
            "location_name": address["employee_location_name"],
            "location_master_name": location.location_name,
            "employee_location_name": address["employee_location_name"],
            "position_status": projection["position_status"],
            "map_issue": projection["map_issue"],
        }

    return {
        "id": row.id,
        "version": row.version,
        "status": row.status,
        "lot": _mobile_goods_payload(lot),
        "registered_location": location_payload(registered),
        "observed_location": location_payload(observed),
        "observed_location_layout_version": row.observed_location_layout_version,
        "reported_quantity": row.reported_quantity,
        "reason": row.reason,
        "reported_at": utc_naive_to_api(row.reported_at),
        "resolution_note": row.resolution_note,
    }


@router.post("/warehouse/location-discrepancies", status_code=201)
def report_mobile_warehouse_location_discrepancy(
    payload: MobileWarehouseDiscrepancyPayload,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    user: User = Depends(can_read_inventory),
) -> dict:
    _no_store(response)
    visible_customer_ids = _visible_customer_ids(user, db)
    lot = _require_mobile_lot(
        db, lot_id=payload.inventory_lot_id, visible_customer_ids=visible_customer_ids
    )
    if lot.inventory_type != "finished" or lot.status != "active":
        raise HTTPException(status_code=409, detail="当前只允许上报有效成品库存的位置不符")
    if int(lot.version) != payload.expected_lot_version:
        raise HTTPException(status_code=409, detail="库存已变化，请刷新后重试")
    live_quantity = int(lot.quantity_available or 0) + int(lot.quantity_reserved or 0)
    if payload.reported_quantity > live_quantity:
        raise HTTPException(status_code=409, detail=f"当前位置仅登记 {live_quantity} 只")
    observed = db.scalar(
        select(WarehouseLocation)
        .options(selectinload(WarehouseLocation.floor3_layout))
        .where(WarehouseLocation.id == payload.observed_location_id)
    )
    registered = lot.location
    if observed is not None and registered is not None:
        _claim_mobile_warehouse_floors(db, registered, observed)
        db.expire(lot)
        lot = _require_mobile_lot(
            db,
            lot_id=payload.inventory_lot_id,
            visible_customer_ids=visible_customer_ids,
        )
        registered = lot.location
        observed = db.scalar(
            select(WarehouseLocation)
            .options(selectinload(WarehouseLocation.floor3_layout))
            .where(WarehouseLocation.id == payload.observed_location_id)
        )
    if observed is None or not _mobile_location_is_published(db, observed):
        raise HTTPException(status_code=409, detail="现场观察位置尚未正式发布")
    if (
        observed.floor3_layout is None
        or int(observed.floor3_layout.version)
        != payload.observed_location_layout_version
    ):
        raise HTTPException(
            status_code=409,
            detail="现场观察位置布局已变化，请刷新地图后重新上报",
        )
    if observed.id == registered.id:
        raise HTTPException(status_code=409, detail="观察位置与系统登记位置相同")
    existing = db.scalar(
        select(WarehouseLocationDiscrepancy).where(
            WarehouseLocationDiscrepancy.idempotency_key == payload.idempotency_key
        )
    )
    if existing is not None:
        if (
            existing.inventory_lot_id != lot.id
            or existing.observed_location_id != observed.id
            or existing.observed_location_layout_version
            != payload.observed_location_layout_version
            or existing.reported_quantity != payload.reported_quantity
        ):
            raise HTTPException(status_code=409, detail="同一请求标识已用于其他位置不符上报")
        existing_registered = db.get(
            WarehouseLocation, existing.registered_location_id
        )
        existing_observed = db.get(WarehouseLocation, existing.observed_location_id)
        if existing_registered is None or existing_observed is None:
            raise HTTPException(status_code=409, detail="位置不符报告关联的位置已失效")
        return {
            "message": "位置不符已上报，尚未改变库存位置",
            "idempotent_replay": True,
            "report": _mobile_discrepancy_payload(
                db,
                existing,
                lot=lot,
                registered=existing_registered,
                observed=existing_observed,
            ),
        }
    row = WarehouseLocationDiscrepancy(
        inventory_lot_id=lot.id,
        registered_location_id=registered.id,
        observed_location_id=observed.id,
        observed_location_layout_version=payload.observed_location_layout_version,
        reported_lot_version=lot.version,
        reported_quantity=payload.reported_quantity,
        reason=payload.reason,
        idempotency_key=payload.idempotency_key,
        reported_by=user.id,
    )
    db.add(row)
    try:
        db.flush()
        append_audit_event(
            db,
            request=request,
            actor=user,
            event_category="business",
            result="success",
            source="mobile",
            module_code="warehouse",
            action_code="warehouse.location_discrepancy.report",
            resource="WarehouseLocationDiscrepancy",
            entity_type="warehouse_location_discrepancy",
            entity_id=row.id,
            object_ref=f"warehouse_location_discrepancy:{row.id}",
            customer_id=_mobile_lot_customer_id(lot),
            description="员工上报仓库现场位置与系统登记不符",
            details={
                "inventory_lot_id": lot.id,
                "registered_location_id": registered.id,
                "observed_location_id": observed.id,
                "observed_location_layout_version": (
                    payload.observed_location_layout_version
                ),
                "reported_quantity": payload.reported_quantity,
            },
        )
        db.commit()
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(status_code=409, detail="位置不符已被提交，请刷新重试") from error
    return {
        "message": "位置不符已上报，尚未改变库存位置",
        "idempotent_replay": False,
        "report": _mobile_discrepancy_payload(
            db,
            row, lot=lot, registered=registered, observed=observed
        ),
    }


@router.get("/warehouse/location-discrepancies")
def list_mobile_warehouse_location_discrepancies(
    response: Response,
    status: Literal["open", "resolved", "cancelled"] = "open",
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(can_correct_inventory),
) -> dict:
    _no_store(response)
    visible_customer_ids = _visible_customer_ids(user, db)
    rows = list(
        db.scalars(
            select(WarehouseLocationDiscrepancy)
            .where(WarehouseLocationDiscrepancy.status == status)
            .order_by(
                WarehouseLocationDiscrepancy.reported_at.desc(),
                WarehouseLocationDiscrepancy.id.desc(),
            )
            .limit(limit * 3)
        ).all()
    )
    lot_ids = {int(row.inventory_lot_id) for row in rows}
    lots_by_id = {
        int(lot.id): lot
        for lot in db.scalars(
            select(InventoryLot)
            .options(*_mobile_lot_options())
            .where(InventoryLot.id.in_(lot_ids))
        ).all()
    }
    location_ids = {
        int(location_id)
        for row in rows
        for location_id in (
            row.registered_location_id,
            row.observed_location_id,
        )
    }
    locations_by_id = {
        int(location.id): location
        for location in db.scalars(
            select(WarehouseLocation).where(WarehouseLocation.id.in_(location_ids))
        ).all()
    }
    projection_contexts = load_warehouse_location_projection_contexts(
        db,
        locations_by_id.values(),
    )
    items = []
    for row in rows:
        lot = lots_by_id.get(int(row.inventory_lot_id))
        if lot is None or not _mobile_lot_is_visible(lot, visible_customer_ids):
            continue
        registered = locations_by_id.get(int(row.registered_location_id))
        observed = locations_by_id.get(int(row.observed_location_id))
        if registered is None or observed is None:
            continue
        items.append(
            _mobile_discrepancy_payload(
                db,
                row,
                lot=lot,
                registered=registered,
                observed=observed,
                projection_contexts=projection_contexts,
            )
        )
        if len(items) >= limit:
            break
    return {"items": items, "count": len(items), "status": status}


@router.post("/warehouse/location-discrepancies/{report_id}/resolve")
def resolve_mobile_warehouse_location_discrepancy(
    report_id: int,
    payload: MobileWarehouseDiscrepancyResolvePayload,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    user: User = Depends(can_correct_inventory),
) -> dict:
    _no_store(response)
    row = db.get(WarehouseLocationDiscrepancy, report_id)
    if row is None:
        raise HTTPException(status_code=404, detail="位置不符报告不存在")
    visible_customer_ids = _visible_customer_ids(user, db)
    lot = _require_mobile_lot(
        db, lot_id=row.inventory_lot_id, visible_customer_ids=visible_customer_ids
    )
    correction_key = f"correction:{payload.idempotency_key}"
    physical_return_note = f"实物已搬回系统登记位置：{payload.resolution_note}"
    if (
        row.status == "resolved"
        and row.resolution_transfer_id is None
        and payload.resolution_action == "physical_returned"
    ):
        if row.resolution_note == physical_return_note:
            return {
                "message": "实物已搬回系统登记货位，红色标记已关闭",
                "idempotent_replay": True,
                "report_id": row.id,
                "report_version": row.version,
                "transfer_id": None,
            }
        raise HTTPException(status_code=409, detail="该位置不符报告已经处理")
    if row.status == "resolved" and row.resolution_transfer_id is not None:
        transfer = db.get(InventoryLotTransfer, row.resolution_transfer_id)
        if transfer is not None and transfer.idempotency_key == correction_key:
            return {
                "message": "位置登记已纠正，库存总数未改变",
                "idempotent_replay": True,
                "report_id": row.id,
                "transfer_id": transfer.id,
            }
        raise HTTPException(status_code=409, detail="该位置不符报告已经处理")
    if row.status != "open" or int(row.version) != payload.expected_version:
        raise HTTPException(status_code=409, detail="位置不符报告状态已变化，请刷新重试")
    if lot.warehouse_location_id != row.registered_location_id:
        raise HTTPException(status_code=409, detail="库存登记位置已变化，请刷新后重新核对")
    if int(lot.version) != payload.expected_lot_version:
        raise HTTPException(status_code=409, detail="库存已变化，请刷新后重新核对")
    if payload.resolution_action == "physical_returned":
        row.status = "resolved"
        row.version = int(row.version) + 1
        row.resolved_by = user.id
        row.resolved_at = utc_now_naive()
        row.resolution_transfer_id = None
        row.resolution_note = physical_return_note
        append_audit_event(
            db,
            request=request,
            actor=user,
            event_category="business",
            result="success",
            source="mobile",
            module_code="warehouse",
            action_code="warehouse.location_discrepancy.physical_returned",
            resource="WarehouseLocationDiscrepancy",
            entity_type="warehouse_location_discrepancy",
            entity_id=row.id,
            object_ref=f"warehouse_location_discrepancy:{row.id}",
            customer_id=_mobile_lot_customer_id(lot),
            description="授权人员确认现场实物已搬回系统登记货位",
            details={
                "inventory_lot_id": lot.id,
                "registered_location_id": row.registered_location_id,
                "observed_location_id": row.observed_location_id,
                "reported_quantity": row.reported_quantity,
                "inventory_quantity_changed": False,
                "idempotency_key": payload.idempotency_key,
            },
        )
        db.commit()
        return {
            "message": "实物已搬回系统登记货位，红色标记已关闭",
            "idempotent_replay": False,
            "report_id": row.id,
            "report_version": row.version,
            "transfer_id": None,
        }
    observed = db.scalar(
        select(WarehouseLocation)
        .options(selectinload(WarehouseLocation.floor3_layout))
        .where(WarehouseLocation.id == row.observed_location_id)
    )
    if observed is None or not _mobile_location_is_published(db, observed):
        raise HTTPException(status_code=409, detail="现场观察位置已失效或尚未正式发布")
    try:
        result = transfer_finished_lot_between_locations(
            db,
            lot_id=lot.id,
            expected_version=payload.expected_lot_version,
            quantity=row.reported_quantity,
            location_id=observed.id,
            expected_target_layout_version=(
                int(row.observed_location_layout_version)
                if row.observed_location_layout_version is not None
                else None
            ),
            operator_id=user.id,
            idempotency_key=correction_key,
        )
        row.status = "resolved"
        row.version = int(row.version) + 1
        row.resolved_by = user.id
        row.resolved_at = utc_now_naive()
        row.resolution_transfer_id = result.transfer.id
        row.resolution_note = payload.resolution_note
        append_audit_event(
            db,
            request=request,
            actor=user,
            event_category="business",
            result="success",
            source="mobile",
            module_code="warehouse",
            action_code="warehouse.location_discrepancy.resolve",
            resource="WarehouseLocationDiscrepancy",
            entity_type="warehouse_location_discrepancy",
            entity_id=row.id,
            object_ref=f"warehouse_location_discrepancy:{row.id}",
            customer_id=_mobile_lot_customer_id(lot),
            description="授权人员确认仓库位置登记纠正",
            details={
                "inventory_lot_id": lot.id,
                "registered_location_id": row.registered_location_id,
                "observed_location_id": row.observed_location_id,
                "observed_location_layout_version": (
                    row.observed_location_layout_version
                ),
                "quantity": row.reported_quantity,
                "transfer_id": result.transfer.id,
            },
        )
        db.commit()
        return {
            "message": "位置登记已纠正，库存总数未改变",
            "idempotent_replay": result.replayed,
            "report_id": row.id,
            "report_version": row.version,
            "transfer_id": result.transfer.id,
            "source_lot": _mobile_goods_payload(result.source_lot),
            "target_lot": _mobile_goods_payload(result.target_lot),
        }
    except WarehouseInventoryError as error:
        db.rollback()
        raise HTTPException(status_code=error.status_code, detail=str(error)) from error
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(status_code=409, detail="库存位置或纠正请求已变化，请刷新重试") from error


def _mobile_unmatched_observation_payload(
    row: WarehouseUnmatchedInventoryObservation,
    *,
    location: WarehouseLocation,
    projection_context: Mapping[str, object] | None = None,
) -> dict:
    context = dict(projection_context or {})
    projection = warehouse_location_projection(location, **context)
    address = location_address_payload(
        location,
        area=context.get("area"),
        floor=context.get("floor"),
        position_status=str(projection["position_status"]),
        area_sequence=(
            int(context["area_sequence"])
            if context.get("area_sequence")
            else None
        ),
    )
    return {
        "id": int(row.id),
        "version": int(row.version),
        "status": row.status,
        "observed_location": {
            "location_id": int(location.id),
            "location_code": location.location_code,
            "employee_location_name": address["employee_location_name"],
            "floor": location.warehouse_floor,
            "area_code": location.area_code,
            "position_status": projection["position_status"],
        },
        "observed_location_layout_version": int(
            row.observed_location_layout_version
        ),
        "customer_keyword": row.customer_keyword,
        "inventory_keyword": row.inventory_keyword,
        "reported_quantity": row.reported_quantity,
        "reported_unit": row.reported_unit,
        "reason": row.reason,
        "reported_at": utc_naive_to_api(row.reported_at),
        "resolution_note": row.resolution_note,
        "resolved_at": utc_naive_to_api(row.resolved_at) if row.resolved_at else None,
        "resolved_inventory_lot_id": row.resolved_inventory_lot_id,
    }


@router.get("/warehouse/observation-products")
def search_mobile_observation_products(
    response: Response, q: str = Query(min_length=1, max_length=200),
    db: Session = Depends(get_db), user: User = Depends(can_read_inventory),
) -> dict:
    _no_store(response)
    pattern = f"%{q.strip()}%"
    statement = select(Product, Customer).join(Customer, Customer.id == Product.customer_id).where(Product.deleted_at.is_(None), Product.is_active.is_(True), or_(
        Product.product_code.ilike(pattern), Product.customer_material_code.ilike(pattern),
        Product.product_name.ilike(pattern), Customer.name.ilike(pattern),
        Customer.chinese_short_name.ilike(pattern), Customer.customer_code.ilike(pattern)))
    allowed = _visible_customer_ids(user, db)
    if allowed is not None:
        statement = statement.where(Product.customer_id.in_(allowed))
    return {"items": [{"product_id": product.id, "customer_id": customer.id,
                       "customer_name": customer.chinese_short_name or customer.name,
                       "product_code": product.product_code, "product_name": product.product_name}
                      for product, customer in db.execute(statement.order_by(Product.product_code, Product.id).limit(50))]}


@router.post("/warehouse/unmatched-inventory-observations", status_code=201)
def report_mobile_unmatched_inventory_observation(
    payload: MobileWarehouseUnmatchedObservationPayload,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    user: User = Depends(can_read_inventory),
) -> dict:
    _no_store(response)
    if payload.product_id is not None:
        product = db.get(Product, payload.product_id)
        allowed = _visible_customer_ids(user, db)
        if product is None or product.deleted_at is not None or not product.is_active or (allowed is not None and product.customer_id not in allowed):
            raise HTTPException(status_code=404, detail="产品不存在或不在可访问客户范围内")
        customer = db.get(Customer, product.customer_id)
        payload = payload.model_copy(update={
            "customer_keyword": (customer.chinese_short_name or customer.name)[:120],
            "inventory_keyword": str(product.product_code or product.customer_material_code or product.id)[:200],
            "reason": f"现场盘点选定产品 #{product.id}：{product.product_name}；员工上报，待管理员确认入账",
        })
    location = db.scalar(
        select(WarehouseLocation)
        .options(selectinload(WarehouseLocation.floor3_layout))
        .where(WarehouseLocation.id == payload.observed_location_id)
    )
    if location is not None:
        _claim_mobile_warehouse_floors(db, location)
        db.refresh(location)
    if location is None or not _mobile_location_is_published(db, location):
        raise HTTPException(status_code=409, detail="现场观察货位已失效或尚未正式发布")
    if (
        location.floor3_layout is None
        or int(location.floor3_layout.version)
        != payload.observed_location_layout_version
    ):
        raise HTTPException(status_code=409, detail="现场货位布局已变化，请刷新地图后重新标记")

    existing = db.scalar(
        select(WarehouseUnmatchedInventoryObservation).where(
            WarehouseUnmatchedInventoryObservation.idempotency_key
            == payload.idempotency_key
        )
    )
    if existing is not None:
        if (
            int(existing.observed_location_id) != int(location.id)
            or int(existing.observed_location_layout_version)
            != payload.observed_location_layout_version
            or existing.customer_keyword != payload.customer_keyword
            or existing.inventory_keyword != payload.inventory_keyword
            or existing.reported_quantity != payload.reported_quantity
            or existing.reported_unit != payload.reported_unit
            or existing.reason != payload.reason
        ):
            raise HTTPException(status_code=409, detail="同一请求标识已用于其他现场未匹配货物")
        context = load_warehouse_location_projection_contexts(db, [location]).get(
            int(location.id), {}
        )
        return {
            "message": "现场未匹配货物已标记，等待管理员核对",
            "idempotent_replay": True,
            "observation": _mobile_unmatched_observation_payload(
                existing, location=location, projection_context=context
            ),
        }

    row = WarehouseUnmatchedInventoryObservation(
        observed_location_id=location.id,
        observed_location_layout_version=payload.observed_location_layout_version,
        customer_keyword=payload.customer_keyword,
        inventory_keyword=payload.inventory_keyword,
        reported_quantity=payload.reported_quantity,
        reported_unit=payload.reported_unit,
        reason=payload.reason,
        idempotency_key=payload.idempotency_key,
        reported_by=user.id,
    )
    db.add(row)
    try:
        db.flush()
        append_audit_event(
            db,
            request=request,
            actor=user,
            event_category="business",
            result="success",
            source="mobile",
            module_code="warehouse",
            action_code="warehouse.unmatched_inventory_observation.report",
            resource="WarehouseUnmatchedInventoryObservation",
            entity_type="warehouse_unmatched_inventory_observation",
            entity_id=row.id,
            object_ref=f"warehouse_unmatched_inventory_observation:{row.id}",
            description="员工标记现场有货但全仓实物库存搜索未匹配",
            details={
                "observed_location_id": int(location.id),
                "observed_location_layout_version": (
                    payload.observed_location_layout_version
                ),
                "customer_keyword": payload.customer_keyword,
                "inventory_keyword": payload.inventory_keyword,
                "reported_quantity": payload.reported_quantity,
                "inventory_changed": False,
            },
        )
        db.commit()
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(status_code=409, detail="现场未匹配货物已被提交，请刷新重试") from error
    context = load_warehouse_location_projection_contexts(db, [location]).get(
        int(location.id), {}
    )
    return {
        "message": "现场未匹配货物已标红，等待管理员核对；库存数量尚未改变",
        "idempotent_replay": False,
        "observation": _mobile_unmatched_observation_payload(
            row, location=location, projection_context=context
        ),
    }


@router.get("/warehouse/unmatched-inventory-observations")
def list_mobile_unmatched_inventory_observations(
    response: Response,
    status: Literal["open", "resolved", "cancelled"] = Query(default="open"),
    location_id: int | None = Query(default=None, gt=0),
    limit: int = Query(default=100, ge=1, le=200),
    db: Session = Depends(get_db),
    _user: User = Depends(can_correct_inventory),
) -> dict:
    _no_store(response)
    rows = list(
        db.scalars(
            select(WarehouseUnmatchedInventoryObservation)
            .where(WarehouseUnmatchedInventoryObservation.status == status)
            .where(WarehouseUnmatchedInventoryObservation.observed_location_id == location_id if location_id is not None else True)
            .order_by(
                WarehouseUnmatchedInventoryObservation.reported_at.desc(),
                WarehouseUnmatchedInventoryObservation.id.desc(),
            )
            .limit(limit)
        ).all()
    )
    locations = {
        int(row.id): row
        for row in db.scalars(
            select(WarehouseLocation)
            .options(selectinload(WarehouseLocation.floor3_layout))
            .where(
                WarehouseLocation.id.in_(
                    [int(row.observed_location_id) for row in rows]
                )
            )
        ).all()
    } if rows else {}
    contexts = load_warehouse_location_projection_contexts(db, locations.values())
    items = [
        _mobile_unmatched_observation_payload(
            row,
            location=locations[int(row.observed_location_id)],
            projection_context=contexts.get(int(row.observed_location_id), {}),
        )
        for row in rows
        if int(row.observed_location_id) in locations
    ]
    return {"items": items, "count": len(items), "status": status}


@router.post("/warehouse/unmatched-inventory-observations/{observation_id}/resolve")
def resolve_mobile_unmatched_inventory_observation(
    observation_id: int,
    payload: MobileWarehouseUnmatchedResolvePayload,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    user: User = Depends(can_correct_inventory),
) -> dict:
    _no_store(response)
    row = db.get(WarehouseUnmatchedInventoryObservation, observation_id)
    if row is None:
        raise HTTPException(status_code=404, detail="现场未匹配货物标记不存在")
    if row.status == "resolved":
        if row.resolution_idempotency_key == payload.idempotency_key:
            if (
                row.resolution_note != payload.resolution_note
                or row.resolved_inventory_lot_id
                != payload.resolved_inventory_lot_id
            ):
                raise HTTPException(
                    status_code=409,
                    detail="同一处理请求标识已用于其他核对结果",
                )
            return {
                "message": "现场未匹配货物已处理",
                "idempotent_replay": True,
                "observation_id": int(row.id),
            }
        raise HTTPException(status_code=409, detail="该现场未匹配货物已经处理")
    if row.status != "open" or int(row.version) != payload.expected_version:
        raise HTTPException(status_code=409, detail="现场未匹配货物状态已变化，请刷新重试")

    resolved_lot = None
    if payload.resolved_inventory_lot_id is not None:
        resolved_lot = _require_mobile_lot(
            db,
            lot_id=payload.resolved_inventory_lot_id,
            visible_customer_ids=_visible_customer_ids(user, db),
        )
        if int(resolved_lot.warehouse_location_id) != int(row.observed_location_id):
            raise HTTPException(status_code=409, detail="关联库存不在该现场货位，不能关闭标记")
    updated = db.execute(
        update(WarehouseUnmatchedInventoryObservation)
        .where(
            WarehouseUnmatchedInventoryObservation.id == row.id,
            WarehouseUnmatchedInventoryObservation.status == "open",
            WarehouseUnmatchedInventoryObservation.version
            == payload.expected_version,
        )
        .values(
            status="resolved",
            version=WarehouseUnmatchedInventoryObservation.version + 1,
            resolved_by=user.id,
            resolved_at=utc_now_naive(),
            resolution_note=payload.resolution_note,
            resolution_idempotency_key=payload.idempotency_key,
            resolved_inventory_lot_id=(resolved_lot.id if resolved_lot else None),
        )
    )
    if updated.rowcount != 1:
        db.rollback()
        raise HTTPException(status_code=409, detail="现场未匹配货物状态已变化，请刷新重试")
    append_audit_event(
        db,
        request=request,
        actor=user,
        event_category="business",
        result="success",
        source="mobile",
        module_code="warehouse",
        action_code="warehouse.unmatched_inventory_observation.resolve",
        resource="WarehouseUnmatchedInventoryObservation",
        entity_type="warehouse_unmatched_inventory_observation",
        entity_id=row.id,
        object_ref=f"warehouse_unmatched_inventory_observation:{row.id}",
        description="管理员核对并关闭现场未匹配货物标记",
        details={
            "observed_location_id": int(row.observed_location_id),
            "resolved_inventory_lot_id": (
                int(resolved_lot.id) if resolved_lot is not None else None
            ),
            "inventory_changed_by_this_action": False,
        },
    )
    try:
        db.commit()
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(status_code=409, detail="处理请求已被使用，请刷新重试") from error
    return {
        "message": "现场未匹配货物标记已关闭",
        "idempotent_replay": False,
        "observation_id": int(row.id),
        "resolved_inventory_lot_id": (
            int(resolved_lot.id) if resolved_lot is not None else None
        ),
    }
