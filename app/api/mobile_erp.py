from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
import mimetypes
from pathlib import PurePath
import re
from typing import Literal
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import FileResponse
from sqlalchemy import String, and_, case, cast, exists, func, or_, select
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
    WarehouseLocation,
)


router = APIRouter()
can_read_inventory = PermissionChecker("warehouse.view")
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
    is_mapped = (
        location.warehouse_floor == 3 and location.floor3_layout is not None
    )
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
