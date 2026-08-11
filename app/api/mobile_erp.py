from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
import mimetypes
from pathlib import PurePath
import re
from typing import Literal
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import String, and_, case, cast, exists, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

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
from app.models.mold_tool import MoldTool
from app.models.order import OrderItem
from app.models.product import Product
from app.models.product_drawing import ProductDrawing
from app.models.production import ProductionTask
from app.models.product_bom import (
    RequisitionItemBomSource,
    SalesOrderItemBomComponent,
)
from app.models.user import User
from app.services.production_workflow import (
    count_production_tasks,
    find_pending_production_task_lookup_rows,
    list_production_tasks,
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
    WarehouseLocation,
    WarehouseLocationDiscrepancy,
)
from app.services.audit_log import append_audit_event
from app.services.location_candidates import (
    list_operational_locations,
    operational_location_issue,
)
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
can_read_inventory = PermissionChecker("warehouse.view")
can_execute_inventory = PermissionChecker("warehouse.execute")
can_correct_inventory = PermissionChecker("warehouse.correct")
can_read_orders = PermissionChecker("orders.view")
can_read_incoming = PermissionChecker("incoming.view")
_BEIJING = ZoneInfo("Asia/Shanghai")


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "private, no-store"


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
    entries: list[dict] = []

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
        "management_summary_allowed": "dashboard.view" in permissions,
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


def _safe_production_task(task: dict, *, drawing_path: str | None) -> dict:
    """Expose workshop facts only; supplier material codes and prices stay private."""

    available_input = max(int(task.get("available_material_input_quantity") or 0), 0)
    output_factor = max(int(task.get("output_factor") or 1), 1)
    pieces_per_box = max(int(task.get("pieces_per_box") or 1), 1)
    return {
        "task_id": task["id"],
        "status": task["status"],
        "status_text": _task_status_text(task["status"]),
        "order_number": task.get("order_number"),
        "item_order_number": task.get("item_order_number"),
        "product_code": task.get("product_code"),
        "product_name": task.get("product_name"),
        "carton_specification": task.get("specification"),
        "flute_type": task.get("flute"),
        "order_quantity": task.get("ordered_quantity"),
        "received_material_quantity": task.get("material_received_quantity"),
        "current_producible_quantity": available_input * output_factor // pieces_per_box,
        "planned_output_quantity": task.get("planned_output_quantity"),
        "cutting_mode": task.get("special_process"),
        "production_process": task.get("production_process"),
        "production_notes": task.get("production_notes"),
        "mold_name": task.get("mold_name"),
        "mold_location": task.get("mold_location"),
        "printing_plate_mode": task.get("printing_plate_mode"),
        "print_content": task.get("print_content"),
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
            .where(ProductDrawing.product_id.in_(product_ids))
            .order_by(
                ProductDrawing.product_id,
                ProductDrawing.uploaded_at.desc(),
                ProductDrawing.id.desc(),
            )
        ).all():
            drawings.setdefault(int(drawing.product_id), drawing)

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
        mold = molds.get(int(mold_id or 0))
        mold_code = (
            component.snapshot_mold_tool_code
            if component is not None
            else mold.mold_code
            if mold is not None
            else None
        )
        mold_name = (
            component.snapshot_mold_tool_name
            if component is not None
            else mold.mold_name
            if mold is not None
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
        if drawing_reference:
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
            "task_version": int(task.get("version") or 1),
            "customer_name": task.get("customer_name"),
            "order_number": task.get("order_number"),
            "item_order_number": task.get("item_order_number"),
            "product_code": task.get("product_code"),
            "product_name": task.get("product_name"),
            "carton_specification": task.get("specification"),
            "order_quantity": int(task.get("ordered_quantity") or 0),
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
            plate_colors = [
                str(row.get("color_name") or "").strip()
                for row in task.get("printing_plates") or []
                if str(row.get("color_name") or "").strip()
            ]
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
                    "printing_colors": plate_colors
                    or [
                        value.strip()
                        for value in str(
                            product.printing_colors if product else ""
                        ).replace("，", ",").split(",")
                        if value.strip()
                    ],
                    "printing_method": task.get("printing_plate_mode"),
                    "printing_instruction": task.get("printing_instruction"),
                    "printing_plate_codes": task.get("printing_plate_codes") or [],
                    "printing_plates": task.get("printing_plates") or [],
                    "cutting_mode": task.get("special_process"),
                }
            )
        else:
            mold_active = mold.is_active if mold is not None else None
            common.update(
                {
                    "material": task.get("material"),
                    "flute_type": task.get("flute"),
                    "mold_code": mold_code,
                    "mold_name": mold_name,
                    "mold_location": mold.rack_location if mold is not None else None,
                    "mold_is_active": mold_active,
                    "mold_warning": (
                        "模具已停用，禁止直接生产；请联系管理员受控启用"
                        if mold_active is False
                        else "模具主档未找到，请先核对模具"
                        if mold_code and mold is None
                        else None
                    ),
                    "mold_map_url": (
                        f"/mobile/mold-lookup?q={mold_code}&readonly=1"
                        if mold_map_allowed and mold_code
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


def _incoming_search_match(
    row: dict,
    *,
    keyword: str,
    dimension_mode: Literal["any", "length", "width"],
) -> dict | None:
    normalized = keyword.strip().casefold()
    requested_dimension = _dimension_decimal(keyword.strip())
    length = _dimension_decimal(row.get("cardboard_len"))
    width = _dimension_decimal(row.get("cardboard_width"))
    dimension_hits: list[str] = []
    if requested_dimension is not None:
        if length == requested_dimension and dimension_mode in {"any", "length"}:
            dimension_hits.append("length")
        if width == requested_dimension and dimension_mode in {"any", "width"}:
            dimension_hits.append("width")

    if dimension_mode in {"length", "width"}:
        if requested_dimension is None:
            return None
        text_hits: list[str] = []
        matched = bool(dimension_hits)
    else:
        searchable = (
            ("customer_name", row.get("customer_name")),
            ("product_code", row.get("product_code")),
            ("product_name", row.get("product_name")),
            ("order_number", row.get("order_number")),
            ("customer_po", row.get("customer_po")),
        )
        text_hits = [
            field
            for field, value in searchable
            if normalized and normalized in str(value or "").casefold()
        ]
        compact_spec = "x".join(
            value
            for value in (_number_text(length), _number_text(width))
            if value is not None
        )
        if compact_spec and normalized.replace("×", "x") in {
            compact_spec.casefold(),
            compact_spec.replace("x", "*").casefold(),
        }:
            text_hits.append("reported_dimensions")
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
        summaries.append("客户、款号、名称或订单号命中")
    return {
        "query": keyword,
        "dimension_mode": dimension_mode,
        "dimension_sides": dimension_hits,
        "text_fields": text_hits,
        "summary": "；".join(summaries),
    }


def _product_specification(product: Product) -> str:
    dimensions = [
        _number_text(product.length_mm),
        _number_text(product.width_mm),
        _number_text(product.height_mm),
    ]
    present = [value for value in dimensions if value is not None]
    return "×".join(present) + ("mm" if present else "")


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
        selectinload(InventoryLot.pallet_item).selectinload(
            InventoryPalletItem.pallet
        ),
    )


def _position_payload(lot: InventoryLot) -> dict:
    location = lot.location
    pallet_item = lot.pallet_item
    pallet = pallet_item.pallet if pallet_item is not None else None
    is_mapped = location.floor3_layout is not None
    is_unplaced = (
        location.placement_status == "unplaced"
        or location.is_temporary
        or (pallet is not None and pallet.needs_relocation)
    )
    if is_unplaced:
        map_status = "unplaced"
        map_status_text = "待归位，暂不能在地图定位"
    elif is_mapped:
        map_status = "mapped"
        map_status_text = "三楼地图已定位"
    else:
        map_status = "ledger_only"
        map_status_text = "已登记库位，尚未接入平面图"
    map_url = None
    if map_status == "mapped":
        map_url = (
            "/warehouse.html?embedded=1&readonly=1&tab=locations"
            f"&location_view=floor3&location_id={location.id}"
            f"&lot_id={lot.id}&source=mobile-product"
        )
    unit_label = "只" if lot.inventory_type == "finished" else "张"
    return {
        "lot_id": lot.id,
        "lot_version": lot.version,
        "lot_number": lot.lot_number,
        "location_id": location.id,
        "location_code": location.location_code,
        "location_name": location.location_name,
        "floor": location.warehouse_floor,
        "area_code": location.area_code,
        "pallet_code": (
            pallet.pallet_code
            if pallet is not None and pallet.is_current
            else None
        ),
        "quantity_available": lot.quantity_available,
        "quantity_reserved": lot.quantity_reserved,
        "quantity_total": lot.quantity_available + lot.quantity_reserved,
        "unit": unit_label,
        "map_status": map_status,
        "map_status_text": map_status_text,
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
) -> dict:
    pending_pick_by_lot = pending_pick_by_lot or {}
    positions = [_position_payload(lot) for lot in lots]
    positions.sort(
        key=lambda row: (
            row["floor"] is None,
            row["floor"] or 0,
            row["area_code"] or "",
            row["location_code"],
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
    keyword = q.strip()
    if not keyword:
        raise HTTPException(status_code=422, detail="请输入客户、款号、名称、订单号或报料尺寸")
    if dimension_mode in {"length", "width"} and _dimension_decimal(keyword) is None:
        raise HTTPException(status_code=422, detail="按报料长或报料宽查询时，请输入一个毫米数值")

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
                source.sales_order_item_bom_component_id if source is not None else None
            )
            if component_id is None:
                task_query = task_query.where(
                    ProductionTask.sales_order_item_bom_component_id.is_(None)
                )
            else:
                task_query = task_query.where(
                    ProductionTask.sales_order_item_bom_component_id == component_id
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
            _safe_production_task(task, drawing_path=incoming_item.get("drawing_path"))
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
    total = count_production_tasks(
        db,
        allowed_customer_ids=visible_customer_ids,
        status="pending",
    )
    last_page = max(1, (total + page_size - 1) // page_size)
    resolved_page = min(page, last_page)
    task_rows = list_production_tasks(
        db,
        allowed_customer_ids=visible_customer_ids,
        status="pending",
        page=resolved_page,
        page_size=page_size,
    )
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
            .where(ProductDrawing.product_id == int(task_row["product_id"]))
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
                        task,
                        drawing_path=row.get("drawing_path"),
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
        safe_task = _safe_production_task(task, drawing_path=None)
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
    )
    semi_finished_group = _inventory_group(
        semi_finished_lots,
        unit="张",
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
    target_location_id: int = Field(gt=0)
    idempotency_key: str = Field(min_length=1, max_length=100)
    physical_move_confirmed: bool

    @field_validator("idempotency_key")
    @classmethod
    def strip_move_key(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("请求标识不能为空")
        return text


class MobileWarehouseDiscrepancyPayload(BaseModel):
    inventory_lot_id: int = Field(gt=0)
    expected_lot_version: int = Field(gt=0)
    reported_quantity: int = Field(gt=0)
    observed_location_id: int = Field(gt=0)
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

    @field_validator("idempotency_key", "resolution_note")
    @classmethod
    def strip_resolution_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("纠正说明和请求标识不能为空")
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
    return bool(
        location.floor3_layout is not None
        and operational_location_issue(
            db,
            location,
            warehouse_types={"finished", "semi_finished", "shared"},
        )
        is None
    )


def _mobile_goods_payload(lot: InventoryLot) -> dict:
    if lot.finished_detail is not None:
        detail = lot.finished_detail
        specification = "×".join(
            str(value)
            for value in (detail.length_mm, detail.width_mm, detail.height_mm)
            if value is not None
        )
        customer_name = detail.owner_customer_name_snapshot
        product_code = detail.inventory_code_snapshot
        product_name = detail.product_name_snapshot
    else:
        detail = lot.semi_finished_detail
        specification = (
            f"{detail.board_length_mm}×{detail.board_width_mm}mm"
            if detail is not None
            else ""
        )
        customer_name = detail.owner_customer_name_snapshot if detail else None
        product_code = detail.material_code if detail else None
        product_name = "半成品纸板"
    return {
        "lot_id": int(lot.id),
        "lot_version": int(lot.version),
        "inventory_type": lot.inventory_type,
        "customer_id": _mobile_lot_customer_id(lot),
        "customer_name": customer_name,
        "product_code": product_code,
        "product_name": product_name,
        "specification": specification,
        "quantity_available": int(lot.quantity_available or 0),
        "quantity_reserved": int(lot.quantity_reserved or 0),
        "quantity_total": int(lot.quantity_available or 0)
        + int(lot.quantity_reserved or 0),
        "unit": "只" if lot.inventory_type == "finished" else "张",
        "stock_date": lot.stock_date,
        "last_movement_at": utc_naive_to_api(lot.last_movement_at),
        "can_move": lot.inventory_type == "finished" and lot.status == "active",
    }


def _mobile_floor_code(row) -> str:
    if row.floor is not None:
        return str(row.floor.floor_code).upper()
    return f"{int(row.location.warehouse_floor or 0)}F"


@router.get("/warehouse/map/floors")
def mobile_warehouse_map_floors(
    response: Response,
    db: Session = Depends(get_db),
    user: User = Depends(can_read_inventory),
) -> dict:
    _no_store(response)
    rows = list_operational_locations(db)
    location_ids = [int(row.location.id) for row in rows]
    placed_ids = set(
        db.scalars(
            select(Floor3LocationLayout.location_id).where(
                Floor3LocationLayout.location_id.in_(location_ids or [-1])
            )
        ).all()
    )
    grouped: dict[str, dict] = {}
    for row in rows:
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
                "area_name": row.area.area_name if row.area else area_code,
                "published_location_count": 0,
            },
        )
        if row.location.id in placed_ids:
            area["published_location_count"] += 1
    floors: list[dict] = []
    for floor in sorted(
        grouped.values(), key=lambda item: (item["floor_number"] or 0, item["floor_code"])
    ):
        areas = []
        for area in sorted(floor.pop("areas").values(), key=lambda item: item["area_code"]):
            area["map_status"] = (
                "ready" if area["published_location_count"] else "area_pending_location"
            )
            area["map_status_text"] = (
                "位置已发布" if area["published_location_count"] else "区域待定位"
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
    locations = [row.location for row in rows]
    location_ids = [int(location.id) for location in locations]
    layouts = {
        int(layout.location_id): layout
        for layout in db.scalars(
            select(Floor3LocationLayout).where(
                Floor3LocationLayout.location_id.in_(location_ids)
            )
        ).all()
    }
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
    goods_by_location: dict[int, list[dict]] = {}
    for lot in lots:
        if _mobile_lot_is_visible(lot, visible_customer_ids):
            goods_by_location.setdefault(int(lot.warehouse_location_id), []).append(
                _mobile_goods_payload(lot)
            )
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
    if map_floor is not None:
        for raw in map_floor.get("features") or []:
            kind = str(raw.get("feature_kind") or "")
            bound_area = str(raw.get("erp_area_code") or "").strip().upper()
            if kind == "aisle" or (kind == "zone" and bound_area == normalized_area):
                features.append(
                    {
                        "id": raw.get("id"),
                        "feature_kind": kind,
                        "name": raw.get("name"),
                        "points": raw.get("points") or [],
                    }
                )
    unrestricted = visible_customer_ids is None
    location_payloads = []
    for row in rows:
        location = row.location
        layout = layouts.get(int(location.id))
        goods = goods_by_location.get(int(location.id), [])
        location_payloads.append(
            {
                "location_id": int(location.id),
                "location_code": location.location_code,
                "location_name": location.location_name,
                "area_code": normalized_area,
                "geometry": _mobile_layout_payload(layout) if layout else None,
                "map_status": "ready" if layout else "area_pending_location",
                "map_status_text": "位置已发布" if layout else "区域待定位",
                "occupancy_state": (
                    "occupied" if goods else "empty"
                )
                if unrestricted
                else ("visible_goods" if goods else "not_disclosed"),
                "goods": goods,
                "can_select_target": layout is not None,
            }
        )
    has_geometry = any(item["geometry"] is not None for item in location_payloads)
    return {
        "floor_code": normalized_floor,
        "floor_name": rows[0].floor.floor_name if rows[0].floor else normalized_floor,
        "area_code": normalized_area,
        "area_name": rows[0].area.area_name if rows[0].area else normalized_area,
        "map_status": "ready" if has_geometry else "area_pending_location",
        "map_status_text": "位置已发布" if has_geometry else "区域待定位",
        "guidance": (
            f"{normalized_floor} {normalized_area}区，以地图高亮位置为准；到现场后核对相邻位置。"
            if has_geometry
            else "该区域尚未发布位置几何，只能查看区域，不能猜测具体货位。"
        ),
        "bounds_mm": map_floor.get("bounds_mm") if map_floor else None,
        "features": features,
        "locations": location_payloads,
        "can_execute": has_permission(user, "warehouse.execute"),
        "can_correct": has_permission(user, "warehouse.correct"),
        "as_of": datetime.now(_BEIJING).isoformat(timespec="seconds"),
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
    visible_customer_ids = _visible_customer_ids(user, db)
    source_lot = _require_mobile_lot(
        db, lot_id=lot_id, visible_customer_ids=visible_customer_ids
    )
    customer_id = _mobile_lot_customer_id(source_lot)
    target = db.scalar(
        select(WarehouseLocation)
        .options(selectinload(WarehouseLocation.floor3_layout))
        .where(WarehouseLocation.id == payload.target_location_id)
    )
    if target is None or not _mobile_location_is_published(db, target):
        raise HTTPException(status_code=409, detail="目标位置尚未正式发布，不能执行搬运")
    try:
        result = transfer_finished_lot_between_locations(
            db,
            lot_id=lot_id,
            expected_version=payload.expected_version,
            quantity=payload.quantity,
            location_id=payload.target_location_id,
            operator_id=user.id,
            idempotency_key=payload.idempotency_key,
        )
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
                    "quantity": payload.quantity,
                    "idempotency_key": payload.idempotency_key,
                },
            )
        db.commit()
        return {
            "message": "实际搬运已登记，库存总数未改变",
            "idempotent_replay": result.replayed,
            "transfer_id": result.transfer.id,
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
    row: WarehouseLocationDiscrepancy,
    *,
    lot: InventoryLot,
    registered: WarehouseLocation,
    observed: WarehouseLocation,
) -> dict:
    return {
        "id": row.id,
        "version": row.version,
        "status": row.status,
        "lot": _mobile_goods_payload(lot),
        "registered_location": {
            "location_id": registered.id,
            "location_code": registered.location_code,
            "location_name": registered.location_name,
        },
        "observed_location": {
            "location_id": observed.id,
            "location_code": observed.location_code,
            "location_name": observed.location_name,
        },
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
    if observed is None or not _mobile_location_is_published(db, observed):
        raise HTTPException(status_code=409, detail="现场观察位置尚未正式发布")
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
    items = []
    for row in rows:
        lot = db.scalar(
            select(InventoryLot)
            .options(*_mobile_lot_options())
            .where(InventoryLot.id == row.inventory_lot_id)
        )
        if lot is None or not _mobile_lot_is_visible(lot, visible_customer_ids):
            continue
        registered = db.get(WarehouseLocation, row.registered_location_id)
        observed = db.get(WarehouseLocation, row.observed_location_id)
        if registered is None or observed is None:
            continue
        items.append(
            _mobile_discrepancy_payload(
                row, lot=lot, registered=registered, observed=observed
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
