from __future__ import annotations

import hashlib
import json
import math
import re
from collections import OrderedDict
from pathlib import PurePath

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.time_contract import utc_naive_to_api
from app.models.customer import Customer
from app.models.incoming_receipt import IncomingReceiptItem
from app.models.mold_tool import MoldTool
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.product_bom import (
    RequisitionItemBomSource,
    SalesOrderItemBomComponent,
)
from app.models.product_drawing import ProductDrawing
from app.models.printing_plate import PrintingPlate
from app.models.production import ProductionTask
from app.models.supplier_requisition_order import (
    SupplierRequisitionOrder,
    SupplierRequisitionOrderItem,
)
from app.services.box_type_rules import box_type_code, canonical_box_style
from app.services.history_orders import build_display_registry, display_order_number
from app.services.fulfillment_reminders import (
    matching_production_reminders,
    production_reminders_by_customer,
)
from app.services.production_workflow import _task_printing_snapshot


_REQUISITION_ITEM_SOURCE = re.compile(r"^requisition_item:(\d+)$")
_ORDER_ITEM_COMPONENT_SOURCE = re.compile(
    r"^order_item:(\d+)(?::(cover|base))?$"
)


def _unique_text(values: list[object]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _file_name(value: str | None) -> str | None:
    text = str(value or "").strip().replace("\\", "/")
    if not text:
        return None
    return PurePath(text).name or None


def _drawing_suffix(value: str | None) -> str:
    suffix = PurePath(str(value or "").replace("\\", "/")).suffix.lower()
    return (
        suffix
        if suffix in {".jpg", ".jpeg", ".png", ".webp", ".pdf"}
        else ".bin"
    )


def _drawing_kind(value: str | None) -> str | None:
    suffix = _drawing_suffix(value)
    if suffix == ".pdf":
        return "pdf"
    if suffix in {".jpg", ".jpeg", ".png", ".webp"}:
        return "image"
    return None


def _order_drawing_url(order_item: OrderItem | None) -> str | None:
    if order_item is None or not order_item.drawing_file:
        return None
    return (
        f"/api/orders/items/{order_item.id}/drawing/content/"
        f"file{_drawing_suffix(order_item.drawing_file)}"
    )


def _product_drawing_url(drawing: ProductDrawing | None) -> str | None:
    if drawing is None:
        return None
    reference = drawing.thumbnail_path or drawing.image_path
    return (
        f"/api/master/products/drawings/{drawing.id}/content/"
        f"thumbnail{_drawing_suffix(reference)}"
    )


def _layout_kind(box_style: str | None, box_category: str | None) -> str:
    """Map explicit common-box metadata to the three approved task-sheet layouts."""

    code = box_type_code(box_style)
    if code == "liner":
        return "liner"
    if code in {"die_cut_inner_box", "die_cut_partition"} or str(
        box_category or ""
    ).strip().casefold() == "die_cut":
        return "die_cut"
    return "carton"


def _explicit_joining_method(values: list[object]) -> str | None:
    """Extract only explicitly frozen joining instructions; never infer from names."""

    text = " / ".join(str(value or "") for value in values)
    methods: list[str] = []
    if any(token in text for token in ("打钉", "钉箱", "钉合")):
        methods.append("打钉")
    if any(token in text for token in ("粘贴", "粘箱", "粘合", "糊盒")):
        methods.append("粘贴")
    return " / ".join(methods) or None


def _json_list(value: str | None) -> list[object]:
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return parsed if isinstance(parsed, list) else []


def _printing_snapshot(task: ProductionTask | None) -> dict:
    if task is None:
        return {
            "print_content": None,
            "printing_situation": "无印刷",
            "printing_plate_mode": None,
            "printing_plate_codes": [],
            "printing_plates": [],
            "printing_colors": [],
            "printing_colors_frozen": False,
            "printing_instruction": None,
            "plate_alignment_value_mm": None,
            "plate_mount_value_mm": None,
            "machine_set_length_mm": None,
            "machine_set_width_mm": None,
            "machine_set_height_mm": None,
        }
    return _task_printing_snapshot(task)


def _crease_values(
    order: SupplierRequisitionOrder,
    order_item: OrderItem | None,
    component_snapshot: SalesOrderItemBomComponent | None,
    component_type: str,
) -> tuple[str | None, int | None, int | None, int | None]:
    if component_snapshot is not None:
        if component_type == "base":
            values = (
                component_snapshot.snapshot_component_base_crease_type,
                component_snapshot.snapshot_component_base_crease_left_mm,
                component_snapshot.snapshot_component_base_crease_middle_mm,
                component_snapshot.snapshot_component_base_crease_right_mm,
            )
            if any(value is not None for value in values):
                return values
        return (
            component_snapshot.snapshot_component_crease_type,
            component_snapshot.snapshot_component_crease_left_mm,
            component_snapshot.snapshot_component_crease_middle_mm,
            component_snapshot.snapshot_component_crease_right_mm,
        )
    if order_item is not None:
        if component_type == "base":
            values = (
                order_item.snapshot_base_crease_type,
                order_item.snapshot_base_crease_left_mm,
                order_item.snapshot_base_crease_middle_mm,
                order_item.snapshot_base_crease_right_mm,
            )
            if any(value is not None for value in values):
                return values
        return (
            order_item.snapshot_crease_type,
            order_item.snapshot_crease_left_mm,
            order_item.snapshot_crease_middle_mm,
            order_item.snapshot_crease_right_mm,
        )
    return (
        order.crease_type,
        order.crease_left_mm,
        order.crease_middle_mm,
        order.crease_right_mm,
    )


def _crease_display(values: tuple[str | None, int | None, int | None, int | None]) -> str:
    crease_type, left, middle, right = values
    if crease_type == "压线" and middle is not None:
        return f"{left or 0}+{middle}+{right or 0}"
    return str(crease_type or "-")


def _component_context(
    item: SupplierRequisitionOrderItem,
    *,
    bom_sources: dict[int, RequisitionItemBomSource],
    bom_snapshots: dict[int, SalesOrderItemBomComponent],
) -> tuple[str, str, int, SalesOrderItemBomComponent | None]:
    source_key = str(item.source_key or "").strip()
    match = _REQUISITION_ITEM_SOURCE.fullmatch(source_key)
    if match:
        source = bom_sources.get(int(match.group(1)))
        if source is not None:
            snapshot = bom_snapshots.get(source.sales_order_item_bom_component_id)
            component_type = str(source.component_type or "whole")
            display_order = int(snapshot.display_order) if snapshot is not None else item.id
            label = {"cover": "组件·盖", "base": "组件·底"}.get(
                component_type, "组件"
            )
            return (
                f"bom:{source.sales_order_item_bom_component_id}:{component_type}",
                label,
                display_order,
                snapshot,
            )
        frozen_name = str(item.product_name or "")
        component_type = (
            "base"
            if frozen_name.endswith("-底")
            else "cover" if frozen_name.endswith("-盖") else "whole"
        )
        label = {"cover": "盖", "base": "底"}.get(component_type, "整片")
        return (source_key, label, item.id, None)
    match = _ORDER_ITEM_COMPONENT_SOURCE.fullmatch(source_key)
    if match:
        component_type = str(match.group(2) or "whole")
        label = {"cover": "盖", "base": "底"}.get(component_type, "整片")
        return (source_key, label, item.id, None)
    return (f"supplier_item:{item.id}", "整片", item.id, None)


def build_supplier_requisition_production_package(
    db: Session,
    order: SupplierRequisitionOrder,
) -> dict:
    """Build a deterministic, read-only pre-receipt production print package.

    The projection uses the immutable supplier-order rows, order-item snapshots,
    BOM snapshots and production-task printing snapshots. It never reads current
    product master fields and never advances receiving, production or inventory.
    """

    items = sorted(order.items, key=lambda row: row.id)
    order_item_ids = {
        int(item.order_item_id)
        for item in items
        if item.order_item_id is not None
    }
    order_context: dict[int, tuple[OrderItem, Order, Customer]] = {}
    if order_item_ids:
        for order_item, sales_order, customer in db.execute(
            select(OrderItem, Order, Customer)
            .join(Order, Order.id == OrderItem.order_id)
            .join(Customer, Customer.id == Order.customer_id)
            .where(OrderItem.id.in_(order_item_ids))
        ).all():
            order_context[int(order_item.id)] = (order_item, sales_order, customer)

    requisition_item_ids: set[int] = set()
    for item in items:
        match = _REQUISITION_ITEM_SOURCE.fullmatch(str(item.source_key or "").strip())
        if match:
            requisition_item_ids.add(int(match.group(1)))
    bom_sources = (
        {
            int(row.requisition_item_id): row
            for row in db.scalars(
                select(RequisitionItemBomSource).where(
                    RequisitionItemBomSource.requisition_item_id.in_(
                        requisition_item_ids
                    )
                )
            ).all()
        }
        if requisition_item_ids
        else {}
    )
    bom_snapshot_ids = {
        int(source.sales_order_item_bom_component_id)
        for source in bom_sources.values()
    }
    bom_snapshots = (
        {
            int(row.id): row
            for row in db.scalars(
                select(SalesOrderItemBomComponent).where(
                    SalesOrderItemBomComponent.id.in_(bom_snapshot_ids)
                )
            ).all()
        }
        if bom_snapshot_ids
        else {}
    )

    product_ids = {
        int(item.product_id) for item in items if item.product_id is not None
    }
    products = (
        {
            int(row.id): row
            for row in db.scalars(
                select(Product).where(Product.id.in_(product_ids))
            ).all()
        }
        if product_ids
        else {}
    )
    latest_drawings: dict[int, ProductDrawing] = {}
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
            latest_drawings.setdefault(int(drawing.product_id), drawing)

    mold_ids = {
        int(product.mold_tool_id)
        for product in products.values()
        if product.mold_tool_id is not None
    }
    mold_ids.update(
        int(snapshot.snapshot_mold_tool_id)
        for snapshot in bom_snapshots.values()
        if snapshot.snapshot_mold_tool_id is not None
    )
    molds = (
        {
            int(row.id): row
            for row in db.scalars(
                select(MoldTool).where(MoldTool.id.in_(mold_ids))
            ).all()
        }
        if mold_ids
        else {}
    )

    tasks = (
        db.scalars(
            select(ProductionTask).where(
                or_(
                    ProductionTask.order_item_id.in_(order_item_ids),
                    ProductionTask.sales_order_item_bom_component_id.in_(
                        bom_snapshot_ids
                    ),
                )
            )
        ).all()
        if order_item_ids or bom_snapshot_ids
        else []
    )
    ordinary_tasks = {
        int(task.order_item_id): task
        for task in tasks
        if task.sales_order_item_bom_component_id is None
    }
    component_tasks = {
        int(task.sales_order_item_bom_component_id): task
        for task in tasks
        if task.sales_order_item_bom_component_id is not None
    }
    printing_plate_codes = {
        str(value).strip()
        for task in tasks
        for value in _json_list(task.printing_plate_codes_snapshot)
        if str(value).strip()
    }
    printing_plate_locations = (
        {
            str(code): location
            for code, location in db.execute(
                select(PrintingPlate.plate_code, PrintingPlate.rack_location).where(
                    PrintingPlate.plate_code.in_(printing_plate_codes)
                )
            ).all()
        }
        if printing_plate_codes
        else {}
    )

    supplier_item_ids = [int(item.id) for item in items]
    receipt_rows = db.execute(
        select(
            IncomingReceiptItem.supplier_order_item_id,
            IncomingReceiptItem.id,
        ).where(
            IncomingReceiptItem.status == "posted",
            or_(
                IncomingReceiptItem.supplier_order_id == order.id,
                IncomingReceiptItem.supplier_order_item_id.in_(supplier_item_ids),
            ),
        )
    ).all() if supplier_item_ids else []
    receipt_item_ids = {
        int(row[0]) for row in receipt_rows if row[0] is not None
    }
    receipt_without_line = any(row[0] is None for row in receipt_rows)

    registry = build_display_registry(db)
    # A physical pre-receipt task card is identified by the immutable formal
    # supplier requisition detail, not by customer/product code.  Cover/base,
    # BOM sources, or repeated rows may share the same product code and even
    # the same dimensions while still being distinct material tasks.
    grouped: OrderedDict[int, dict] = OrderedDict()
    for item in items:
        context = order_context.get(int(item.order_item_id or 0))
        order_item, sales_order, customer = (
            context if context is not None else (None, None, None)
        )
        source_identity, component_label, display_order, component_snapshot = (
            _component_context(
                item,
                bom_sources=bom_sources,
                bom_snapshots=bom_snapshots,
            )
        )
        component_type = (
            source_identity.rsplit(":", 1)[-1]
            if source_identity.startswith("bom:")
            else (
                str(item.source_key).rsplit(":", 1)[-1]
                if str(item.source_key or "").endswith((":cover", ":base"))
                else "whole"
            )
        )
        crease_values = _crease_values(
            order,
            order_item,
            component_snapshot,
            component_type,
        )
        task = (
            component_tasks.get(int(component_snapshot.id))
            if component_snapshot is not None
            else ordinary_tasks.get(int(item.order_item_id or 0))
        )
        product = products.get(int(item.product_id or 0))
        product_drawing = latest_drawings.get(int(item.product_id or 0))
        box_style = (
            component_snapshot.snapshot_component_box_style
            if component_snapshot is not None
            else product.box_style
            if product is not None
            else None
        )
        box_category = (
            component_snapshot.snapshot_component_box_category
            if component_snapshot is not None
            else product.box_category
            if product is not None
            else None
        )
        layout_kind = _layout_kind(box_style, box_category)
        mold_id = (
            component_snapshot.snapshot_mold_tool_id
            if component_snapshot is not None
            else product.mold_tool_id
            if product is not None
            else None
        )
        current_mold = molds.get(int(mold_id or 0))
        drawing_reference = (
            component_snapshot.snapshot_die_cut_path
            if component_snapshot is not None
            else order_item.drawing_file
            if order_item is not None
            else product_drawing.image_path
            if product_drawing is not None
            else None
        )
        drawing_url = _order_drawing_url(order_item)
        drawing_source = "订单图纸" if drawing_url else None
        drawing_kind = (
            _drawing_kind(order_item.drawing_file)
            if drawing_url and order_item is not None
            else None
        )
        if drawing_url is None and product_drawing is not None:
            drawing_url = _product_drawing_url(product_drawing)
            drawing_source = "常用箱图纸"
            drawing_kind = _drawing_kind(
                product_drawing.thumbnail_path or product_drawing.image_path
            )
        printing_snapshot = _printing_snapshot(task)
        for plate in printing_snapshot["printing_plates"]:
            plate["current_location"] = printing_plate_locations.get(
                str(plate.get("plate_code") or "").strip()
            )
        product_code = str(item.product_code or "").strip()
        group_key = int(item.id)
        header_product_name = (
            item.product_name
            or (
                order_item.snapshot_product_name
                if order_item is not None and component_snapshot is None
                else None
            )
        )
        card = grouped.get(group_key)
        if card is None:
            card = {
                "supplier_order_item_id": int(item.id),
                "source_identity": source_identity,
                "component_label": component_label,
                "customer_id": customer.id if customer is not None else None,
                "customer_name": item.customer_name or (customer.name if customer else None),
                "product_code": product_code or None,
                "product_name": header_product_name,
                "specifications": [],
                "order_numbers": [],
                "item_order_numbers": [],
                "customer_pos": [],
                "delivery_dates": [],
                "planned_finished_quantity": 0,
                "requisition_quantity": 0,
                "layout_kind": layout_kind,
                "box_style": canonical_box_style(box_style),
                "box_type_code": box_type_code(box_style),
                "finished_length_mm": (
                    product.length_mm if product is not None else None
                ),
                "finished_width_mm": (
                    product.width_mm if product is not None else None
                ),
                "finished_height_mm": (
                    product.height_mm if product is not None else None
                ),
                "printing_colors": [],
                "joining_methods": [],
                "joining_method_sources": [],
                "production_label_units_per_bundle": None,
                "review_required": False,
                "review_messages": [],
                "components": [],
                "_planned_quantity_keys": set(),
                "_layout_kinds": set(),
                "_box_styles": set(),
            }
            grouped[group_key] = card

        specification = (
            component_snapshot.snapshot_component_spec
            if component_snapshot is not None
            else (order_item.snapshot_spec if order_item is not None else None)
        )
        production_notes = _unique_text(
            [
                component_snapshot.snapshot_component_production_process
                if component_snapshot is not None
                else None,
                component_snapshot.snapshot_component_report_notes
                if component_snapshot is not None and component_type != "base"
                else None,
                component_snapshot.snapshot_component_base_report_notes
                if component_snapshot is not None and component_type == "base"
                else None,
                order_item.snapshot_production_notes if order_item is not None else None,
                (
                    order_item.snapshot_base_report_notes
                    if order_item is not None and component_type == "base"
                    else order_item.snapshot_report_notes
                    if order_item is not None
                    else None
                ),
            ]
        )
        joining_method = _explicit_joining_method(production_notes)
        joining_method_source = "frozen_snapshot" if joining_method else None
        if (
            joining_method is None
            and component_snapshot is None
            and product is not None
        ):
            joining_method = _explicit_joining_method([product.production_process])
            if joining_method:
                joining_method_source = "current_common_box_fallback"
        component_printing_colors = printing_snapshot["printing_colors"]
        component = {
            "supplier_order_item_id": item.id,
            "source_identity": source_identity,
            "component_label": component_label,
            "display_order": display_order,
            "product_code": product_code or None,
            "_product_id": int(item.product_id) if item.product_id is not None else None,
            "product_name": item.product_name,
            "specification": specification,
            "planned_finished_quantity": int(item.quantity or 0),
            "finished_unit": "个" if component_snapshot is not None else "只",
            "requisition_quantity": int(item.requisition_qty or 0),
            "requisition_unit": "张",
            "report_length_mm": item.report_length_mm or order.report_length_mm,
            "report_width_mm": item.report_width_mm or order.report_width_mm,
            "cutting_mode": item.cutting_mode or order.cutting_mode,
            "pieces_per_box": int(item.pieces_per_box or 1),
            "required_piece_quantity": int(item.required_piece_qty or 0),
            "material_code": item.material_code_snapshot,
            "layer_count": item.layer_count_snapshot or order.layer_count,
            "flute_type": item.flute_type_snapshot or order.flute_type,
            "crease_type": crease_values[0],
            "crease_display": _crease_display(crease_values),
            "production_notes": production_notes,
            "box_style": canonical_box_style(box_style),
            "box_type_code": box_type_code(box_style),
            "layout_kind": layout_kind,
            "drawing_reference": _file_name(drawing_reference),
            "drawing_url": drawing_url,
            "drawing_kind": drawing_kind,
            "drawing_source": drawing_source,
            "mold_tool_id": int(mold_id) if mold_id is not None else None,
            "mold_code": (
                component_snapshot.snapshot_mold_tool_code
                if component_snapshot is not None
                else current_mold.mold_code
                if current_mold is not None
                else None
            ),
            "mold_name": (
                component_snapshot.snapshot_mold_tool_name
                if component_snapshot is not None
                else current_mold.mold_name
                if current_mold is not None
                else None
            ),
            "mold_location": (
                current_mold.rack_location if current_mold is not None else None
            ),
            "mold_location_version": (
                int(current_mold.location_version)
                if current_mold is not None
                else None
            ),
            "mold_is_active": (
                bool(current_mold.is_active) if current_mold is not None else None
            ),
            "mold_binding_basis": (
                "bom_order_snapshot"
                if component_snapshot is not None
                and component_snapshot.snapshot_mold_tool_id is not None
                else "current_product_binding"
                if current_mold is not None
                else None
            ),
            "joining_method": joining_method,
            "joining_method_source": joining_method_source,
            "production_task_id": task.id if task is not None else None,
            "production_task_version": task.version if task is not None else None,
            "production_label_units_per_bundle": (
                int(task.production_label_units_per_label_snapshot)
                if task is not None
                and task.production_label_units_per_label_snapshot is not None
                else None
            ),
            **printing_snapshot,
        }
        card["components"].append(component)
        card["_layout_kinds"].add(layout_kind)
        if card["box_style"]:
            card["_box_styles"].add(card["box_style"])
        if component["box_style"]:
            card["_box_styles"].add(component["box_style"])
        card["printing_colors"] = _unique_text(
            [*card["printing_colors"], *component_printing_colors]
        )
        card["joining_methods"] = _unique_text(
            [*card["joining_methods"], joining_method]
        )
        card["joining_method_sources"] = _unique_text(
            [*card["joining_method_sources"], joining_method_source]
        )
        bundle_quantity = component["production_label_units_per_bundle"]
        if (
            bundle_quantity
            and card["production_label_units_per_bundle"] is None
        ):
            card["production_label_units_per_bundle"] = bundle_quantity
        planned_quantity_key = (
            f"bom:{component_snapshot.id}"
            if component_snapshot is not None
            else f"order_item:{item.order_item_id}"
            if item.order_item_id is not None
            else f"supplier_item:{item.id}"
        )
        if planned_quantity_key not in card["_planned_quantity_keys"]:
            card["_planned_quantity_keys"].add(planned_quantity_key)
            card["planned_finished_quantity"] += component[
                "planned_finished_quantity"
            ]
        card["requisition_quantity"] += component["requisition_quantity"]
        card["specifications"] = _unique_text(
            [*card["specifications"], specification]
        )
        card["item_order_numbers"] = _unique_text(
            [*card["item_order_numbers"], item.order_number]
        )
        card["order_numbers"] = _unique_text(
            [
                *card["order_numbers"],
                display_order_number(sales_order, registry)
                if sales_order is not None
                else None,
            ]
        )
        card["customer_pos"] = _unique_text(
            [*card["customer_pos"], sales_order.customer_po if sales_order else None]
        )
        card["delivery_dates"] = _unique_text(
            [*card["delivery_dates"], item.delivery_date]
        )
        incomplete = (
            not card["customer_name"]
            or not product_code
            or not card["product_name"]
            or not component["report_length_mm"]
            or not component["report_width_mm"]
            or not component["material_code"]
            or not component["flute_type"]
            or not str(item.source_key or "").strip()
        )
        received = receipt_without_line or int(item.id) in receipt_item_ids
        if incomplete:
            card["review_required"] = True
            card["review_messages"] = _unique_text(
                [*card["review_messages"], "历史报料快照不完整，请人工核对"]
            )
        if received:
            card["review_required"] = True
            card["review_messages"] = _unique_text(
                [
                    *card["review_messages"],
                    "该报料单已有实收，请改用实收随料卡并核对重打",
                ]
            )
        if task is None:
            card["review_required"] = True
            card["review_messages"] = _unique_text(
                [*card["review_messages"], "生产任务版本缺失，请人工核对"]
            )
        elif (
            order.created_at is not None
            and task.updated_at is not None
            and task.updated_at > order.created_at
        ):
            card["review_required"] = True
            card["review_messages"] = _unique_text(
                [*card["review_messages"], "生产任务版本已变化，请核对并重打"]
            )

    cards = list(grouped.values())
    reminders_by_customer = production_reminders_by_customer(
        db,
        {int(card.get("customer_id") or 0) for card in cards},
    )
    for card in cards:
        card.pop("_planned_quantity_keys", None)
        layout_kinds = card.pop("_layout_kinds", set())
        card.pop("_box_styles", None)
        card["box_styles"] = _unique_text(
            [component.get("box_style") for component in card["components"]]
        )
        card["box_type_codes"] = _unique_text(
            [component.get("box_type_code") for component in card["components"]]
        )
        if len(layout_kinds) > 1:
            card["review_required"] = True
            card["review_messages"] = _unique_text(
                [*card["review_messages"], "同码组件箱型版式不一致，请人工核对"]
            )
        card["joining_method"] = " / ".join(card["joining_methods"]) or None
        card["joining_method_source"] = (
            " / ".join(card["joining_method_sources"]) or None
        )
        card["production_steps"] = _unique_text(
            [
                note
                for component in card["components"]
                for note in component.get("production_notes", [])
            ]
        )
        card["structure_reference"] = next(
            (
                {
                    "name": component.get("drawing_reference"),
                    "url": component.get("drawing_url"),
                    "kind": component.get("drawing_kind"),
                    "source": component.get("drawing_source"),
                }
                for component in card["components"]
                if component.get("drawing_url") or component.get("drawing_reference")
            ),
            None,
        )
        per_bundle = card.get("production_label_units_per_bundle")
        card["estimated_bundle_count"] = (
            math.ceil(card["planned_finished_quantity"] / per_bundle)
            if per_bundle
            else None
        )
        if len(card["components"]) > 6:
            card["review_required"] = True
            card["review_messages"] = _unique_text(
                [*card["review_messages"], "同码物理组件较多，请核对打印版面"]
            )
        card["components"].sort(
            key=lambda row: (
                int(row["display_order"]),
                int(row["supplier_order_item_id"]),
            )
        )
        card["status_label"] = "需核对" if card["review_required"] else "待来料"
        customer_id = int(card.get("customer_id") or 0)
        product_ids = {
            int(component.get("_product_id") or 0)
            for component in card["components"]
            if component.get("_product_id")
        }
        card["fulfillment_reminders"] = matching_production_reminders(
            reminders_by_customer.get(customer_id, []),
            product_ids=product_ids,
        )
        for component in card["components"]:
            component.pop("_product_id", None)
    immutable_payload = {
        "supplier_order_id": order.id,
        "supplier_order_number": order.order_number,
        "created_at": utc_naive_to_api(order.created_at) if order.created_at else None,
        "cards": [
            {
                key: value
                for key, value in card.items()
                if key
                not in {
                    "review_required",
                    "review_messages",
                    "status_label",
                    "fulfillment_reminders",
                }
            }
            for card in cards
        ],
    }
    plan_fingerprint = hashlib.sha256(
        json.dumps(
            immutable_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    pages = [
        {
            "page_number": index // 2 + 1,
            "top": cards[index],
            "bottom": cards[index + 1] if index + 1 < len(cards) else None,
        }
        for index in range(0, len(cards), 2)
    ]
    review_messages = _unique_text(
        [message for card in cards for message in card["review_messages"]]
    )
    layout_overflow = any(len(card["components"]) > 6 for card in cards)
    used_task_ids = {
        int(component["production_task_id"])
        for card in cards
        for component in card["components"]
        if component.get("production_task_id") is not None
    }
    label_tasks = {
        int(task.id): task
        for task in tasks
        if int(task.id) in used_task_ids
        and bool(task.production_label_enabled_snapshot)
        and int(task.production_label_count_snapshot or 0) > 0
    }
    return {
        "supplier_order_id": order.id,
        "supplier_order_number": order.order_number,
        "supplier_name": order.supplier_name,
        "status": order.status,
        "status_label": "待来料",
        "created_at": utc_naive_to_api(order.created_at) if order.created_at else None,
        "plan_fingerprint": plan_fingerprint,
        "card_count": len(cards),
        "page_count": len(pages),
        "review_required": bool(review_messages),
        "review_messages": review_messages,
        "layout_overflow": layout_overflow,
        "printable": not layout_overflow,
        "production_label_task_count": len(label_tasks),
        "production_label_count": sum(
            int(task.production_label_count_snapshot or 0)
            for task in label_tasks.values()
        ),
        "cards": cards,
        "pages": pages,
    }
