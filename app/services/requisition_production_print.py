from __future__ import annotations
from app.core.sheet_dimensions import sheet_dimension_number

import hashlib
import json
import math
import re
from collections import OrderedDict
from copy import deepcopy
from pathlib import PurePath
from types import SimpleNamespace

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload

from app.core.time_contract import utc_naive_to_api
from app.models.customer import Customer
from app.models.incoming_receipt import IncomingReceipt, IncomingReceiptItem
from app.models.mold_tool import MoldTool
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.product_bom import (
    RequisitionItemBomSource,
    SalesOrderItemBomComponent,
)
from app.models.product_drawing import ProductDrawing
from app.models.printing_plate import PrintingPlate
from app.models.purchase_receipt import (
    IncomingReceiptPurposeAllocation,
    PurchaseReceiptFact,
)
from app.models.production import ProductionTask
from app.models.requisition import Requisition
from app.models.supplier_requisition_order import (
    SupplierRequisitionOrder,
    SupplierRequisitionOrderItem,
)
from app.models.stock_replenishment import (
    StockReplenishmentOrder,
    StockReplenishmentOrderItem,
)
from app.models.warehouse_inventory import InventoryLot, InventoryPalletItem
from app.services.box_type_rules import box_type_code, canonical_box_style
from app.services.drawing_binding import bound_task_release
from app.services.drawing_snapshots import release_paper_snapshot
from app.services.product_drawings import engineering_drawing_condition
from app.services.order_number_display import build_display_registry, display_order_number
from app.services.fulfillment_reminders import (
    matching_production_reminders,
    production_reminders_by_customer,
)
from app.services.mold_location import describe_mold_location
from app.services.production_workflow import (
    _task_printing_snapshot,
    cutting_output_factor,
)
from app.services.product_qr import product_qr_payload
from app.services.product_specification import resolved_product_specification


_REQUISITION_ITEM_SOURCE = re.compile(r"^requisition_item:(\d+)$")
_ORDER_ITEM_COMPONENT_SOURCE = re.compile(
    r"^order_item:(\d+)(?::(cover|base))?$"
)
_COMPOSITE_PRINTABLE_ITEM_STATUSES = frozenset({"有效", "已入库"})
_COMPOSITE_TRANSIENT_ID_OFFSET = 1_500_000_000


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


def production_print_card_task_versions(card: dict) -> list[dict[str, int]]:
    """Return the exact formal task/version set represented by one paper card."""

    versions = {
        (
            int(component["production_task_id"]),
            int(component["production_task_version"]),
        )
        for component in card.get("components") or []
        if component.get("production_task_id") is not None
        and component.get("production_task_version") is not None
    }
    return [
        {"task_id": task_id, "version": version}
        for task_id, version in sorted(versions)
    ]


def production_print_card_fingerprint(card: dict) -> str:
    """Hash one displayed card without coupling it to unrelated order cards."""

    payload = deepcopy(card)
    for key in (
        "paper_version_key",
        "selection_fingerprint",
        "selection_eligible",
        "selection_block_reasons",
    ):
        payload.pop(key, None)
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


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
    if methods:
        return " / ".join(methods)
    if any(token in text for token in ("无需结合", "无需", "不需结合", "其他")):
        return "无需结合"
    return None


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

    The production facts use immutable supplier-order rows, order-item snapshots,
    BOM snapshots and production-task printing snapshots.  The label action and
    per-bundle display deliberately read the current common-box label policy; the
    projection never advances receiving, production or inventory.
    """

    items = sorted(
        (row for row in order.items if row.status == "active"),
        key=lambda row: row.id,
    )
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
            .where(ProductDrawing.product_id.in_(product_ids), engineering_drawing_condition())
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
            IncomingReceipt.receipt_number,
            IncomingReceipt.received_at,
            IncomingReceiptItem.planned_quantity,
            IncomingReceiptItem.received_quantity,
            IncomingReceiptItem.cumulative_received_quantity,
            IncomingReceiptItem.variance_quantity,
            IncomingReceiptItem.variance_type,
            IncomingReceiptItem.resolution_status,
            IncomingReceiptItem.resolution_action,
            IncomingReceiptItem.received_inventory_lot_id,
            IncomingReceiptItem.surplus_inventory_lot_id,
        ).where(
            IncomingReceipt.id == IncomingReceiptItem.receipt_id,
            IncomingReceiptItem.status == "posted",
            or_(
                IncomingReceiptItem.supplier_order_id == order.id,
                IncomingReceiptItem.supplier_order_item_id.in_(supplier_item_ids),
            ),
        ).order_by(IncomingReceiptItem.id.asc())
    ).all() if supplier_item_ids else []
    receipts_by_supplier_item: dict[int, list[dict]] = {}
    for row in receipt_rows:
        if row.supplier_order_item_id is None:
            continue
        receipts_by_supplier_item.setdefault(
            int(row.supplier_order_item_id), []
        ).append(
            {
                "receipt_item_id": int(row.id),
                "receipt_number": row.receipt_number,
                "received_at": utc_naive_to_api(row.received_at),
                "planned_sheet_quantity": int(row.planned_quantity),
                "received_sheet_quantity": int(row.received_quantity),
                "cumulative_received_sheet_quantity": int(
                    row.cumulative_received_quantity
                ),
                "variance_sheet_quantity": int(row.variance_quantity),
                "variance_type": row.variance_type,
                "resolution_status": row.resolution_status,
                "resolution_action": row.resolution_action,
                "received_inventory_lot_id": row.received_inventory_lot_id,
                "surplus_inventory_lot_id": row.surplus_inventory_lot_id,
            }
        )
    receipt_lot_ids = {
        int(lot_id)
        for versions in receipts_by_supplier_item.values()
        for version in versions
        for lot_id in (
            version.get("received_inventory_lot_id"),
            version.get("surplus_inventory_lot_id"),
        )
        if lot_id is not None
    }
    receipt_lots = (
        {
            int(row.id): row
            for row in db.scalars(
                select(InventoryLot)
                .options(
                    selectinload(InventoryLot.semi_finished_detail),
                    selectinload(InventoryLot.pallet_item).selectinload(
                        InventoryPalletItem.pallet
                    ),
                    selectinload(InventoryLot.location),
                )
                .where(InventoryLot.id.in_(receipt_lot_ids))
            ).all()
        }
        if receipt_lot_ids
        else {}
    )
    receipt_without_line = any(row[0] is None for row in receipt_rows)

    registry = build_display_registry(db)
    # A physical pre-receipt task card is identified by the immutable formal
    # supplier requisition detail, not by customer/product code.  Cover/base,
    # BOM sources, or repeated rows may share the same product code and even
    # the same dimensions while still being distinct material tasks.
    grouped: OrderedDict[int, dict] = OrderedDict()
    task_label_products: dict[int, Product] = {}
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
        if task is not None and product is not None:
            task_label_products[int(task.id)] = product
        product_drawing = latest_drawings.get(int(item.product_id or 0))
        managed_release = bound_task_release(db, int(task.id)) if task is not None else None
        managed_manifest, managed_drawing = (release_paper_snapshot(managed_release)
                                               if managed_release else (None, None))
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
        if managed_release is not None:
            mold_id = managed_release.mold_tool_id
        current_mold = molds.get(int(mold_id or 0)) or (db.get(MoldTool, mold_id) if mold_id else None)
        managed_mold = (managed_manifest or {}).get("mold_snapshot") or {}
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
                "customer_name": (
                    customer.chinese_short_name
                    if customer is not None and customer.chinese_short_name
                    else item.customer_name
                    or (customer.name if customer else None)
                ),
                "product_code": product_code or None,
                "product_name": header_product_name,
                "specifications": [],
                "order_numbers": [],
                "item_order_numbers": [],
                "customer_pos": [],
                "delivery_dates": [],
                "planned_finished_quantity": 0,
                "customer_order_quantity": (
                    int(order_item.quantity or 0)
                    if order_item is not None
                    else int(item.quantity or 0) + int(item.stock_deduction_qty or 0)
                ),
                "stock_deduction_quantity": int(item.stock_deduction_qty or 0),
                "requisition_quantity": 0,
                "paper_phase": "planned",
                "paper_phase_label": "待来料计划版",
                "paper_version_key": None,
                "receipt_versions": [],
                "receipt_match_status": "not_received",
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

        specification = resolved_product_specification(
            component_snapshot.snapshot_component_spec
            if component_snapshot is not None
            else (order_item.snapshot_spec if order_item is not None else None),
            product,
        )
        production_notes = _unique_text(
            [
                component_snapshot.snapshot_component_production_notes
                if component_snapshot is not None
                else None,
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
        if joining_method is None:
            joining_method = "无需结合"
            joining_method_source = "default_no_joining"
        component_printing_colors = printing_snapshot["printing_colors"]
        from app.services.sheet_cutting_contract import cutting_work_instruction
        from app.services.requisition_quantities import normalize_cutting_mode
        cutting_instruction = cutting_work_instruction(item.sheet_cutting_snapshot)
        component = {
            "supplier_order_item_id": item.id,
            "sheet_cutting_snapshot": item.sheet_cutting_snapshot,
            "cutting_work_instruction": cutting_instruction,
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
            "cutting_mode": (normalize_cutting_mode(item.sheet_cutting_snapshot["cutting_factor"]) if item.sheet_cutting_snapshot else item.cutting_mode or order.cutting_mode),
            "pieces_per_box": int(item.pieces_per_box or 1),
            "required_piece_quantity": int(item.required_piece_qty or 0),
            "material_code": item.material_code_snapshot,
            "layer_count": item.layer_count_snapshot or order.layer_count,
            "flute_type": item.flute_type_snapshot or order.flute_type,
            "crease_type": crease_values[0],
            "crease_display": _crease_display(crease_values),
            "production_notes": [*production_notes, cutting_instruction] if cutting_instruction else production_notes,
            "box_style": canonical_box_style(box_style),
            "box_type_code": box_type_code(box_style),
            "layout_kind": layout_kind,
            "drawing_reference": _file_name(drawing_reference),
            "drawing_url": drawing_url,
            "drawing_kind": drawing_kind,
            "drawing_source": drawing_source,
            "managed_drawing": managed_drawing,
            "mold_tool_id": int(mold_id) if mold_id is not None else None,
            "mold_code": (
                managed_mold.get("code") if managed_release else
                component_snapshot.snapshot_mold_tool_code
                if component_snapshot is not None
                else current_mold.mold_code
                if current_mold is not None
                else None
            ),
            "mold_name": (
                managed_mold.get("name") if managed_release else
                component_snapshot.snapshot_mold_tool_name
                if component_snapshot is not None
                else current_mold.mold_name
                if current_mold is not None
                else None
            ),
            "mold_location": (
                current_mold.rack_location if current_mold is not None else None
            ),
            "mold_location_display": (
                describe_mold_location(current_mold.rack_location)["prompt"]
                if current_mold is not None
                else None
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
                "drawing_release" if managed_release else
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
            "output_factor": max(
                int(item.sheet_cutting_snapshot["yield_per_supplier_sheet"] if item.sheet_cutting_snapshot else task.output_factor if task is not None else 0)
                or cutting_output_factor(item.cutting_mode or order.cutting_mode),
                1,
            ),
            "production_label_units_per_bundle": (
                int(product.production_label_units_per_label)
                if product is not None
                and bool(product.production_label_enabled)
                and product.production_label_units_per_label is not None
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
        receipt_versions = receipts_by_supplier_item.get(int(item.id), [])
        if receipt_versions:
            is_split = len(receipt_versions) > 1
            for version_index, version in enumerate(receipt_versions, start=1):
                lot_id = version.get("received_inventory_lot_id") or version.get(
                    "surplus_inventory_lot_id"
                )
                lot = receipt_lots.get(int(lot_id or 0))
                semi_detail = lot.semi_finished_detail if lot is not None else None
                actual_length = (
                    sheet_dimension_number(semi_detail.board_length_mm)
                    if semi_detail is not None
                    else None
                )
                actual_width = (
                    sheet_dimension_number(semi_detail.board_width_mm)
                    if semi_detail is not None
                    else None
                )
                planned_length = sheet_dimension_number(component["report_length_mm"] or 0) or None
                planned_width = sheet_dimension_number(component["report_width_mm"] or 0) or None
                specification_changed = bool(
                    actual_length
                    and actual_width
                    and planned_length
                    and planned_width
                    and (actual_length, actual_width)
                    != (planned_length, planned_width)
                )
                pallet = (
                    lot.pallet_item.pallet
                    if lot is not None and lot.pallet_item is not None
                    else None
                )
                version["version_number"] = version_index
                version["version_count"] = len(receipt_versions)
                version["inventory_lot_number"] = (
                    lot.lot_number if lot is not None else None
                )
                version["pallet_code"] = (
                    pallet.pallet_code if pallet is not None else None
                )
                version["warehouse_location"] = (
                    lot.location.location_code
                    if lot is not None and lot.location is not None
                    else None
                )
                version["actual_board_length_mm"] = actual_length
                version["actual_board_width_mm"] = actual_width
                version["specification_changed"] = specification_changed
                version["requires_actual_card"] = bool(
                    is_split
                    or version["variance_type"] != "matched"
                    or version["received_sheet_quantity"]
                    != version["planned_sheet_quantity"]
                    or specification_changed
                )
                version["paper_version_key"] = (
                    f"actual:receipt:{version['receipt_item_id']}"
                    if version["requires_actual_card"]
                    else None
                )
            card["receipt_versions"] = receipt_versions
            if any(row["requires_actual_card"] for row in receipt_versions):
                card["receipt_match_status"] = "actual_required"
            else:
                card["receipt_match_status"] = "matched_reuse_plan"
        received = receipt_without_line or bool(receipt_versions)
        if incomplete:
            card["review_required"] = True
            card["review_messages"] = _unique_text(
                [*card["review_messages"], "历史报料快照不完整，请人工核对"]
            )
        if received and card["receipt_match_status"] != "matched_reuse_plan":
            card["review_required"] = True
            card["review_messages"] = _unique_text(
                [
                    *card["review_messages"],
                    "已有实收差异或分批版本，计划版不可冒充实收版",
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
        card["managed_drawings"] = [component["managed_drawing"] for component in card["components"]
                                     if component.get("managed_drawing")]
        per_bundle = card.get("production_label_units_per_bundle")
        card["estimated_bundle_count"] = (
            math.ceil(card["planned_finished_quantity"] / per_bundle)
            if per_bundle
            else None
        )
        if len(card["components"]) > 6:
            card["review_required"] = True
            card["review_messages"] = _unique_text(
                [*card["review_messages"], "同码物理组件较多，纸面已使用紧凑摘要，请扫码核对完整任务"]
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
        qr_candidates = [
            products[product_id]
            for product_id in sorted(product_ids)
            if product_id in products
            and products[product_id].is_active
            and products[product_id].deleted_at is None
        ]
        if len(product_ids) == 1 and len(qr_candidates) == 1:
            card["product_id"] = int(qr_candidates[0].id)
            card["product_qr"] = product_qr_payload(qr_candidates[0].id)
            card["product_qr_unavailable_reason"] = None
        else:
            card["product_id"] = None
            card["product_qr"] = None
            card["product_qr_unavailable_reason"] = (
                "临时任务没有正式产品二维码"
                if not product_ids
                else "当前产品已停用，不生成二维码"
                if len(product_ids) == 1
                else "多产品任务不生成单一产品二维码"
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
                    "receipt_versions",
                    "receipt_match_status",
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
    for card in cards:
        card["paper_version_key"] = (
            f"planned:supplier-order:{order.id}:"
            f"{card['supplier_order_item_id']}:{plan_fingerprint}"
        )
        if card.get("receipt_match_status") == "matched_reuse_plan":
            card["paper_phase_label"] = "计划版（实收一致，沿用本卡）"
            if not card.get("review_required"):
                card["status_label"] = "实收一致"
        card["production_task_versions"] = production_print_card_task_versions(card)
        selection_block_reasons: list[str] = []
        if not card["production_task_versions"] or len(
            card["production_task_versions"]
        ) != len(card.get("components") or []):
            selection_block_reasons.append("生产任务版本缺失，请先核对任务")
        card["selection_block_reasons"] = selection_block_reasons
        card["selection_eligible"] = not selection_block_reasons
        card["selection_fingerprint"] = production_print_card_fingerprint(card)
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
    # The paper renderer has an adaptive, bounded half-A4 summary.  Component
    # count is still surfaced as a review notice above, but must never make a
    # valid frozen task unprintable.
    layout_overflow = False
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
        and int(task.production_label_total_quantity_snapshot or 0) > 0
    }
    # This maintenance projection is deliberately kept outside the immutable
    # paper fingerprint above.  It does not change historical task cards; it
    # only gives the operator enough current version information to invoke the
    # existing, audited per-task refresh command when an older task froze the
    # label plan before the common-box policy was enabled.
    label_plan_refresh_options: list[dict] = []
    current_label_counts: dict[int, int] = {}
    if used_task_ids:
        # Local import avoids the intentional production-label module cycle:
        # job creation imports this read projection, while task maintenance
        # imports the packaging-label job service.
        from app.services.production_label_operations import (
            annotate_task_label_plans,
        )

        task_by_id = {int(task.id): task for task in tasks}
        maintenance_rows = annotate_task_label_plans(
            db,
            [{"id": task_id} for task_id in sorted(used_task_ids)],
        )
        for row in maintenance_rows:
            task_id = int(row["id"])
            task = task_by_id[task_id]
            current = row.get("current_production_label_product") or {}
            product = task_label_products.get(task_id)
            product_id = (
                int(product.id)
                if product is not None
                else current.get("product_id")
            )
            if product is None and product_id:
                product = db.get(Product, int(product_id))
            current_enabled = bool(
                product.production_label_enabled
                if product is not None
                else current.get("enabled")
            )
            current_units_per_label = int(
                product.production_label_units_per_label
                if product is not None
                and product.production_label_units_per_label is not None
                else current.get("units_per_label") or 0
            )
            frozen_total_quantity = int(
                task.production_label_total_quantity_snapshot or 0
            )
            if (
                task_id in label_tasks
                and current_enabled
                and current_units_per_label > 0
                and frozen_total_quantity > 0
            ):
                current_label_counts[task_id] = math.ceil(
                    frozen_total_quantity / current_units_per_label
                )
            refresh_allowed = bool(
                row.get("can_refresh_production_label_plan") and current_enabled
            )
            block_reason = row.get("production_label_refresh_block_reason")
            if not current_enabled:
                block_reason = "当前常用箱未启用生产包装标签"
            label_plan_refresh_options.append(
                {
                    "task_id": task_id,
                    "expected_task_version": int(task.version),
                    "product_id": int(product_id) if product_id else None,
                    "expected_product_version": (
                        int(product.version)
                        if product is not None
                        else current.get("version")
                    ),
                    "product_code": product.product_code if product is not None else None,
                    "product_name": product.product_name if product is not None else None,
                    "frozen_template_version": (
                        task.production_label_template_version_snapshot
                    ),
                    "current_enabled": current_enabled,
                    "current_units_per_label": (
                        current_units_per_label or None
                    ),
                    "can_refresh": refresh_allowed,
                    "block_reason": block_reason,
                }
            )
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
        "printable": True,
        "production_label_task_count": len(current_label_counts),
        "production_label_count": sum(current_label_counts.values()),
        "production_label_refresh_options": label_plan_refresh_options,
        "cards": cards,
        "pages": pages,
    }


def build_stock_replenishment_production_package(
    db: Session,
    order: StockReplenishmentOrder,
    *,
    selected_item_ids: set[int] | None = None,
) -> dict:
    """Project stable replenishment lines onto the shared half-A4 paper card.

    Stock replenishment deliberately has no sales-order ``ProductionTask``.
    The paper card therefore keeps the immutable replenishment item as its
    source identity and uses the stable product QR when one exists.  It never
    invents an order task or advances stock/receipt state.
    """

    if order.status not in {"confirmed", "partially_stocked", "stocked"}:
        raise ValueError("库存补库单已撤销、作废或尚未正式确认")
    available = {int(item.id): item for item in order.items}
    selected = (
        {int(value) for value in selected_item_ids}
        if selected_item_ids is not None
        else set(available)
    )
    if not selected or not selected.issubset(available):
        raise ValueError("所选库存补库明细不存在或已变化，请刷新后重试")

    cards: list[dict] = []
    for item_id in sorted(selected):
        item: StockReplenishmentOrderItem = available[item_id]
        product = item.product or item.reference_product
        customer = item.customer or order.customer
        product_is_current = bool(
            product is not None
            and product.deleted_at is None
            and bool(product.is_active)
        )
        layout_kind = _layout_kind(
            product.box_style if product is not None else None,
            product.box_category if product is not None else None,
        )
        planned_quantity = int(item.quantity or 0)
        from app.services.replenishment_receipt_progress import receipt_progress
        progress = receipt_progress(db, item)
        stocked_quantity = progress['received_quantity']
        remaining_quantity = progress['remaining_quantity']
        output_factor = max(int(item.stock_yield_per_sheet or 0), 1)
        is_semi_finished = item.target_inventory_type == "semi_finished"
        output_unit = "张" if is_semi_finished else "只"
        # The replenishment line is already stored in its target inventory
        # unit: board sheets for semi-finished stock and finished pieces for a
        # direct-finished line.  Never multiply that frozen plan by yield.
        planned_output = planned_quantity
        requisition_quantity = (
            planned_quantity
            if is_semi_finished
            else (planned_quantity + output_factor - 1) // output_factor
        )
        specification = (
            resolved_product_specification(None, product)
            if product is not None
            else f"{item.report_length_mm or '-'}×{item.report_width_mm or '-'}"
        )
        production_steps = _unique_text(
            [
                product.production_process if product is not None else None,
                product.production_notes if product is not None else None,
                item.remark,
            ]
        )
        joining_method = _explicit_joining_method(production_steps) or "无需结合"
        component = {
            "stock_replenishment_item_id": int(item.id),
            "source_identity": f"stock_replenishment_item:{int(item.id)}",
            "component_label": {
                "cover": "盖",
                "base": "底",
                "whole": "整片",
            }.get(str(item.component_type or "whole"), "整片"),
            "display_order": 1,
            "product_code": item.product_code_snapshot,
            "product_name": item.product_name_snapshot,
            "specification": specification,
            "planned_finished_quantity": planned_output,
            "finished_unit": output_unit,
            "requisition_quantity": requisition_quantity,
            "requisition_unit": "张",
            "report_length_mm": item.report_length_mm,
            "report_width_mm": item.report_width_mm,
            "cutting_mode": None,
            "pieces_per_box": max(int(item.pieces_per_box or 0), 1),
            "required_piece_quantity": planned_quantity,
            "material_code": item.material_code_snapshot,
            "layer_count": item.layer_count,
            "flute_type": item.flute_type,
            "crease_type": item.crease_type,
            "crease_display": _crease_display(
                (
                    item.crease_type,
                    item.crease_left_mm,
                    item.crease_middle_mm,
                    item.crease_right_mm,
                )
            ),
            "production_notes": production_steps,
            "box_style": canonical_box_style(
                product.box_style if product is not None else None
            ),
            "box_type_code": box_type_code(
                product.box_style if product is not None else None
            ),
            "layout_kind": layout_kind,
            "drawing_reference": None,
            "drawing_url": None,
            "drawing_kind": None,
            "drawing_source": None,
            "mold_tool_id": None,
            "mold_code": None,
            "mold_name": None,
            "mold_location": None,
            "mold_location_display": None,
            "joining_method": joining_method,
            "joining_method_source": (
                "current_common_box_fallback"
                if joining_method != "无需结合"
                else "default_no_joining"
            ),
            "production_task_id": None,
            "production_task_version": None,
            "output_factor": output_factor,
            "production_label_units_per_bundle": (
                int(product.production_label_units_per_label)
                if product is not None
                and bool(product.production_label_enabled)
                and product.production_label_units_per_label is not None
                else None
            ),
            "printing_colors": _unique_text(
                [product.printing_colors if product is not None else None]
            ),
            "printing_method": None,
            "printing_content": product.print_content if product is not None else None,
            "printing_plates": [],
        }
        review_messages = []
        selection_block_reasons = []
        if customer is None:
            selection_block_reasons.append("补库明细缺少权威客户归属，请核对补库单")
        if product is None:
            selection_block_reasons.append(
                "补库明细没有可回读的当前常用箱，请先核对产品资料"
            )
        elif not product_is_current:
            selection_block_reasons.append(
                "补库明细关联的常用箱已停用或删除，请先恢复或更换当前产品"
            )
        elif customer is not None and int(product.customer_id) != int(customer.id):
            selection_block_reasons.append(
                "补库明细的客户与常用箱客户不一致，请先核对补库来源"
            )
        receipt_match_status = (
            "not_received"
            if stocked_quantity <= 0
            else "received"
            if remaining_quantity <= 0
            else "partially_received"
        )
        card = {
            "source_type": "stock_replenishment",
            "stock_replenishment_order_id": int(order.id),
            "stock_replenishment_item_id": int(item.id),
            "stock_replenishment_item_ids": [int(item.id)],
            "source_identity": f"stock_replenishment_item:{int(item.id)}",
            "component_label": component["component_label"],
            "customer_id": int(customer.id) if customer is not None else None,
            "customer_name": (
                customer.chinese_short_name or customer.name
                if customer is not None
                else None
            ),
            "product_id": int(product.id) if product is not None else None,
            "product_code": item.product_code_snapshot,
            "product_name": item.product_name_snapshot,
            "specifications": [specification],
            "order_numbers": [order.order_number],
            "item_order_numbers": [order.order_number],
            "customer_pos": [],
            "delivery_dates": [],
            "planned_finished_quantity": planned_output,
            "customer_order_quantity": 0,
            "stock_deduction_quantity": 0,
            "requisition_quantity": requisition_quantity,
            "planned_replenishment_quantity": planned_quantity,
            "received_quantity": stocked_quantity,
            "remaining_quantity": remaining_quantity,
            "output_unit": output_unit,
            "paper_phase": "planned",
            "paper_phase_label": "库存补库计划版",
            "paper_version_key": None,
            "receipt_versions": [],
            "receipt_match_status": receipt_match_status,
            "layout_kind": layout_kind,
            "box_style": component["box_style"],
            "box_type_code": component["box_type_code"],
            "finished_length_mm": product.length_mm if product is not None else None,
            "finished_width_mm": product.width_mm if product is not None else None,
            "finished_height_mm": product.height_mm if product is not None else None,
            "printing_colors": component["printing_colors"],
            "joining_methods": [joining_method],
            "joining_method_sources": [component["joining_method_source"]],
            "joining_method": joining_method,
            "joining_method_source": component["joining_method_source"],
            "production_label_units_per_bundle": component[
                "production_label_units_per_bundle"
            ],
            "estimated_bundle_count": None,
            "production_steps": production_steps,
            "structure_reference": None,
            "review_required": bool(review_messages or selection_block_reasons),
            "review_messages": review_messages,
            "status_label": {
                "not_received": "库存补库待收",
                "partially_received": "库存补库部分已收",
                "received": "库存补库已收齐",
            }[receipt_match_status],
            "components": [component],
            "production_task_versions": [],
            "selection_block_reasons": selection_block_reasons,
            "selection_eligible": not selection_block_reasons,
            "product_qr": (
                product_qr_payload(int(product.id)) if product is not None else None
            ),
            "product_qr_unavailable_reason": (
                None if product is not None else "补库明细没有正式产品二维码"
            ),
        }
        cards.append(card)

    immutable_payload = {
        "stock_replenishment_order_id": int(order.id),
        "stock_replenishment_order_number": order.order_number,
        "status": order.status,
        "cards": cards,
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
    for card in cards:
        card["paper_version_key"] = (
            f"planned:stock-replenishment:{int(order.id)}:"
            f"{int(card['stock_replenishment_item_id'])}:{plan_fingerprint}"
        )
        card["selection_fingerprint"] = production_print_card_fingerprint(card)
    pages = [
        {
            "page_number": index // 2 + 1,
            "top": cards[index],
            "bottom": cards[index + 1] if index + 1 < len(cards) else None,
        }
        for index in range(0, len(cards), 2)
    ]
    review_messages = _unique_text(
        [
            message
            for card in cards
            for message in [
                *(card.get("review_messages") or []),
                *(card.get("selection_block_reasons") or []),
            ]
        ]
    )
    printable = bool(cards) and all(
        bool(card.get("selection_eligible")) for card in cards
    )
    return {
        "source_type": "stock_replenishment",
        "stock_replenishment_order_id": int(order.id),
        "supplier_order_id": None,
        "supplier_order_number": order.order_number,
        "status": order.status,
        "status_label": "库存补库计划",
        "created_at": utc_naive_to_api(order.created_at) if order.created_at else None,
        "plan_fingerprint": plan_fingerprint,
        "card_count": len(cards),
        "page_count": len(pages),
        "review_required": bool(review_messages),
        "review_messages": review_messages,
        "layout_overflow": False,
        "printable": printable,
        "production_label_task_count": 0,
        "production_label_count": 0,
        "cards": cards,
        "pages": pages,
    }


def build_composite_requisition_production_package(
    db: Session,
    requisition: Requisition,
    *,
    selected_item_ids: set[int] | None = None,
) -> dict:
    """Build one ordinary production card per reported composite component.

    A composite parent is a fulfillment identity, not a material-production
    shortcut.  Before assembly every reported BOM component remains its own
    production task in both parent- and component-delivery modes.  The adapter
    deliberately reuses the established half-A4 projection so mold, printing,
    joining, current common-box bundle policy and task-version gates stay
    identical to supplier requisition cards.
    """

    item_by_id = {int(row.id): row for row in requisition.items}
    selected_ids = (
        {int(value) for value in selected_item_ids}
        if selected_item_ids is not None
        else {
            item_id
            for item_id, row in item_by_id.items()
            if str(row.status or "").strip() in _COMPOSITE_PRINTABLE_ITEM_STATUSES
        }
    )
    if not selected_ids or not selected_ids.issubset(item_by_id):
        raise ValueError("所选组合报料明细不存在或已发生变化")
    unavailable = sorted(
        item_id
        for item_id in selected_ids
        if str(item_by_id[item_id].status or "").strip()
        not in _COMPOSITE_PRINTABLE_ITEM_STATUSES
    )
    if unavailable:
        raise ValueError(f"所选组合报料明细已取消或失效：{unavailable}")

    sources = {
        int(row.requisition_item_id): row
        for row in db.scalars(
            select(RequisitionItemBomSource).where(
                RequisitionItemBomSource.requisition_item_id.in_(selected_ids)
            )
        ).all()
    }
    if any(item_id not in sources for item_id in selected_ids):
        raise ValueError("所选报料明细不是可追溯的组合 BOM 子件")
    snapshots = {
        int(row.id): row
        for row in db.scalars(
            select(SalesOrderItemBomComponent).where(
                SalesOrderItemBomComponent.id.in_(
                    {
                        int(source.sales_order_item_bom_component_id)
                        for source in sources.values()
                    }
                )
            )
        ).all()
    }

    transient_to_requisition_item: dict[int, int] = {}
    transient_items: list[SimpleNamespace] = []
    for item_id in sorted(selected_ids):
        row = item_by_id[item_id]
        source = sources[item_id]
        snapshot = snapshots.get(int(source.sales_order_item_bom_component_id))
        if snapshot is None:
            raise ValueError(f"组合报料明细 #{item_id} 缺少订单组件快照")
        order_item = row.order_item or db.get(OrderItem, int(row.order_item_id))
        sales_order = order_item.order if order_item is not None else None
        transient_id = _COMPOSITE_TRANSIENT_ID_OFFSET + item_id
        transient_to_requisition_item[transient_id] = item_id
        transient_items.append(
            SimpleNamespace(
                id=transient_id,
                order_item_id=int(row.order_item_id),
                source_key=f"requisition_item:{item_id}",
                product_id=int(snapshot.component_product_id),
                material_id=snapshot.snapshot_component_material_id,
                material_code_snapshot=(
                    row.material_snapshot or snapshot.snapshot_component_material
                ),
                supplier_name_snapshot=(
                    snapshot.snapshot_component_supplier_name
                    or requisition.supplier_name
                ),
                layer_count_snapshot=snapshot.snapshot_component_layer_count,
                flute_type_snapshot=snapshot.snapshot_component_flute_type,
                order_number=(sales_order.order_number if sales_order is not None else None),
                product_code=snapshot.snapshot_component_product_code,
                product_name=snapshot.snapshot_component_product_name,
                report_length_mm=(
                    sheet_dimension_number(row.cardboard_len)
                    if row.cardboard_len is not None
                    else snapshot.snapshot_component_report_length_mm
                ),
                report_width_mm=(
                    sheet_dimension_number(row.cardboard_width)
                    if row.cardboard_width is not None
                    else snapshot.snapshot_component_report_width_mm
                ),
                quantity=int(source.required_piece_quantity),
                stock_deduction_qty=0,
                requisition_qty=int(row.requisition_qty or 0),
                sheet_cutting_snapshot=row.sheet_cutting_snapshot,
                cutting_mode=row.special_process or snapshot.snapshot_component_default_cutting_mode,
                pieces_per_box=(
                    row.pieces_per_box
                    or snapshot.snapshot_component_pieces_per_box
                    or 1
                ),
                required_piece_qty=int(source.required_piece_quantity),
                customer_name=None,
                delivery_date=(sales_order.delivery_date if sales_order is not None else None),
                status="active",
            )
        )

    transient_order = SimpleNamespace(
        id=_COMPOSITE_TRANSIENT_ID_OFFSET + int(requisition.id),
        order_number=requisition.requisition_number,
        supplier_name=requisition.supplier_name,
        items=transient_items,
        status="confirmed",
        created_at=requisition.created_at,
        layer_count=None,
        flute_type=None,
        report_length_mm=None,
        report_width_mm=None,
        cutting_mode=None,
        crease_type=None,
        crease_left_mm=None,
        crease_middle_mm=None,
        crease_right_mm=None,
    )
    package = build_supplier_requisition_production_package(db, transient_order)
    for card in package.get("cards") or []:
        material_item_ids: list[int] = []
        for component in card.get("components") or []:
            material_item_id = transient_to_requisition_item.get(
                int(component.get("supplier_order_item_id") or 0)
            )
            if material_item_id is None:
                continue
            component["material_requisition_item_id"] = material_item_id
            material_item_ids.append(material_item_id)
        card["source_type"] = "composite_bom_requisition"
        card["material_requisition_id"] = int(requisition.id)
        card["material_requisition_item_ids"] = sorted(set(material_item_ids))
        card["selection_fingerprint"] = production_print_card_fingerprint(card)
    package.update(
        {
            "source_type": "composite_bom_requisition",
            "document_id": int(requisition.id),
            "material_requisition_id": int(requisition.id),
            "material_requisition_number": requisition.requisition_number,
            "supplier_order_id": None,
            "supplier_order_number": requisition.requisition_number,
            "status": requisition.status,
        }
    )
    return package


def _actual_receipt_label(version: dict) -> str:
    if version.get("specification_changed"):
        return "规格差异实收版"
    if int(version.get("version_count") or 0) > 1:
        return (
            f"分批实收版 {int(version.get('version_number') or 1)}"
            f"/{int(version.get('version_count') or 1)}"
        )
    return {
        "short": "短收实收版",
        "over": "超收实收版",
    }.get(str(version.get("variance_type") or ""), "实收版")


def build_receipt_production_print_package(
    db: Session,
    receipt_item: IncomingReceiptItem,
) -> dict | None:
    """Project one posted receipt fact onto its frozen production-task card.

    This intentionally reuses the same card projection and page layout as the
    pre-receipt package.  A single exact receipt keeps the planned paper
    version; quantity/specification differences and split receipts receive one
    immutable response version per receipt fact.  No print or workflow fact is
    written here.
    """

    if (
        receipt_item.status != "posted"
        or receipt_item.supplier_order_id is None
        or receipt_item.supplier_order_item_id is None
    ):
        return None
    order = db.get(SupplierRequisitionOrder, int(receipt_item.supplier_order_id))
    if order is None or order.status != "confirmed":
        return None
    base = build_supplier_requisition_production_package(db, order)
    source_card = next(
        (
            row
            for row in base.get("cards", [])
            if int(row.get("supplier_order_item_id") or 0)
            == int(receipt_item.supplier_order_item_id)
        ),
        None,
    )
    if source_card is None:
        return None
    version = next(
        (
            row
            for row in source_card.get("receipt_versions", [])
            if int(row.get("receipt_item_id") or 0) == int(receipt_item.id)
        ),
        None,
    )
    if version is None:
        return None

    card = deepcopy(source_card)
    allocation = db.scalar(
        select(IncomingReceiptPurposeAllocation).where(
            IncomingReceiptPurposeAllocation.incoming_receipt_item_id
            == int(receipt_item.id)
        )
    )
    receipt_fact = (
        db.get(PurchaseReceiptFact, int(allocation.purchase_receipt_fact_id))
        if allocation is not None
        and allocation.purchase_receipt_fact_id is not None
        else None
    )
    if receipt_fact is not None:
        reported_material_code = str(
            receipt_fact.expected_material_code_snapshot or ""
        ).strip()
        actual_material_code = str(
            receipt_fact.actual_material_code_snapshot or ""
        ).strip()
        actual_flute_type = str(
            receipt_fact.actual_material_flute_type_snapshot or ""
        ).strip()
        actual_layer_count = receipt_fact.actual_material_layer_count_snapshot
        for component in card.get("components") or []:
            matches_supplier_item = (
                receipt_fact.supplier_requisition_order_item_id is not None
                and int(component.get("supplier_order_item_id") or 0)
                == int(receipt_fact.supplier_requisition_order_item_id)
            )
            matches_requisition_item = (
                receipt_fact.material_requisition_item_id is not None
                and int(component.get("material_requisition_item_id") or 0)
                == int(receipt_fact.material_requisition_item_id)
            )
            if not (matches_supplier_item or matches_requisition_item):
                continue
            component["reported_material_code"] = reported_material_code or None
            component["reported_flute_type"] = component.get("flute_type")
            component["material_code"] = actual_material_code or None
            component["flute_type"] = actual_flute_type or None
            component["layer_count"] = actual_layer_count
            component["material_changed"] = bool(
                actual_material_code
                and reported_material_code
                and actual_material_code != reported_material_code
            )
        card.update(
            {
                "reported_material_code": reported_material_code or None,
                "actual_material_code": actual_material_code or None,
                "actual_material_flute_type": actual_flute_type or None,
                "material_changed": bool(
                    actual_material_code
                    and reported_material_code
                    and actual_material_code != reported_material_code
                ),
            }
        )
    receipt_warning = "已有实收差异或分批版本，计划版不可冒充实收版"
    card["review_messages"] = [
        message
        for message in card.get("review_messages", [])
        if message != receipt_warning
    ]
    card["review_required"] = bool(card["review_messages"])
    requires_actual = bool(
        version.get("requires_actual_card") or card.get("material_changed")
    )
    if requires_actual:
        output_factor = max(
            (
                int(card["components"][0].get("output_factor") or 0)
                if card.get("components")
                else 0
            )
            or 1,
            1,
        )
        card.update(
            {
                "paper_phase": "actual_receipt",
                "paper_phase_label": (
                    "材质差异实收版"
                    if card.get("material_changed")
                    else _actual_receipt_label(version)
                ),
                "paper_version_key": (
                    f"actual:receipt:{int(receipt_item.id)}"
                    if card.get("material_changed")
                    else version["paper_version_key"]
                ),
                "status_label": (
                    "材质差异实收版"
                    if card.get("material_changed")
                    else _actual_receipt_label(version)
                ),
                "planned_sheet_quantity": int(
                    version["planned_sheet_quantity"]
                ),
                "received_sheet_quantity": int(
                    version["received_sheet_quantity"]
                ),
                "cumulative_received_sheet_quantity": int(
                    version["cumulative_received_sheet_quantity"]
                ),
                "production_capacity_quantity": int(
                    version["received_sheet_quantity"]
                )
                * output_factor,
                "output_factor": output_factor,
                "receipt_number": version.get("receipt_number"),
                "receipt_item_id": int(version["receipt_item_id"]),
                "received_at": version.get("received_at"),
                "variance_sheet_quantity": int(
                    version["variance_sheet_quantity"]
                ),
                "variance_type": version.get("variance_type"),
                "resolution_status": version.get("resolution_status"),
                "resolution_action": version.get("resolution_action"),
                "inventory_lot_number": version.get("inventory_lot_number"),
                "pallet_code": version.get("pallet_code"),
                "warehouse_location": version.get("warehouse_location"),
                "actual_board_length_mm": version.get(
                    "actual_board_length_mm"
                ),
                "actual_board_width_mm": version.get("actual_board_width_mm"),
                "specification_changed": bool(
                    version.get("specification_changed")
                ),
            }
        )
    else:
        card["paper_phase"] = "planned"
        card["paper_phase_label"] = "计划版（实收一致，沿用本卡）"
        card["status_label"] = (
            "需核对" if card["review_required"] else "实收一致"
        )

    immutable_receipt = {
        "plan_fingerprint": base["plan_fingerprint"],
        "paper_version_key": card["paper_version_key"],
        "receipt_item_id": int(receipt_item.id),
        "receipt_number": version.get("receipt_number"),
        "received_sheet_quantity": int(version["received_sheet_quantity"]),
        "cumulative_received_sheet_quantity": int(
            version["cumulative_received_sheet_quantity"]
        ),
        "variance_type": version.get("variance_type"),
        "inventory_lot_number": version.get("inventory_lot_number"),
        "pallet_code": version.get("pallet_code"),
        "reported_material_code": card.get("reported_material_code"),
        "actual_material_code": card.get("actual_material_code"),
        "actual_material_flute_type": card.get("actual_material_flute_type"),
    }
    paper_fingerprint = (
        hashlib.sha256(
            json.dumps(
                immutable_receipt,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        ).hexdigest()
        if requires_actual
        else base["plan_fingerprint"]
    )
    return {
        **{
            key: value
            for key, value in base.items()
            if key not in {"cards", "pages", "review_messages"}
        },
        "paper_phase": card["paper_phase"],
        "paper_phase_label": card["paper_phase_label"],
        "paper_version_key": card["paper_version_key"],
        "paper_fingerprint": paper_fingerprint,
        "reuses_planned_card": not requires_actual,
        "reprint_required": requires_actual,
        "receipt_item_id": int(receipt_item.id),
        "reported_material_code": card.get("reported_material_code"),
        "actual_material_code": card.get("actual_material_code"),
        "actual_material_flute_type": card.get("actual_material_flute_type"),
        "material_changed": bool(card.get("material_changed")),
        "card_count": 1,
        "page_count": 1,
        "review_required": bool(card["review_required"]),
        "review_messages": list(card["review_messages"]),
        "layout_overflow": False,
        "printable": True,
        "cards": [card],
        "pages": [
            {"page_number": 1, "top": card, "bottom": None}
        ],
    }
