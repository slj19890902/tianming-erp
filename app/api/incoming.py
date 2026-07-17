from __future__ import annotations

import base64
import json
import socket
from datetime import datetime, timedelta
from io import BytesIO
from uuid import uuid4

import qrcode
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, aliased, selectinload

from app.api.deps import (
    PermissionChecker,
    customer_scope_ids,
    get_db,
    has_unrestricted_customer_access,
    require_customer_access,
)
from app.core.time_contract import (
    beijing_naive_to_api,
    utc_naive_to_api,
    utc_now_naive,
)
from app.models.audit import OperationLog
from app.models.customer import Customer
from app.models.incoming_receipt import IncomingReceiptItem
from app.models.order import Order, OrderItem
from app.models.product_drawing import ProductDrawing
from app.models.product import Product
from app.models.requisition import Requisition, RequisitionItem
from app.models.supplier_requisition_order import (
    SupplierRequisitionOrder,
    SupplierRequisitionOrderItem,
)
from app.models.user import User
from app.models.warehouse_inventory import WarehouseLocation
from app.services.history_orders import build_display_registry, display_order_number
from app.services.incoming_receipts import (
    IncomingReceiptError,
    accept_short,
    receipt_history,
    receipt_item_dict,
    receive_one,
    revert_receipt_item,
    source_summary_for_item,
)
from app.services.production_workflow import (
    ProductionWorkflowError,
    has_production_completion_facts,
    lock_order_rows_for_production_transition,
    refresh_order_production_status,
    refresh_production_task,
)


router = APIRouter()
can_read = PermissionChecker("incoming.view")
can_operate = PermissionChecker("incoming.execute")


def _utc_now() -> datetime:
    return utc_now_naive()


def _refresh_production_after_material_change(
    db: Session,
    order_item: OrderItem,
) -> bool:
    """Refresh N029 state and report whether this is a production-managed item."""
    try:
        task = refresh_production_task(db, order_item.id)
    except ProductionWorkflowError as error:
        raise HTTPException(status_code=error.status_code, detail=str(error)) from error
    if task is None:
        return False
    refresh_order_production_status(db, order_item.order_id)
    return True


def _lock_order_for_material_revert(db: Session, order_id: int) -> Order:
    try:
        return lock_order_rows_for_production_transition(db, [order_id])[order_id]
    except ProductionWorkflowError as error:
        raise HTTPException(status_code=error.status_code, detail=str(error)) from error


class RevertRequest(BaseModel):
    reason: str

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value: str) -> str:
        reason = value.strip()
        if not reason:
            raise ValueError("撤回原因不能为空")
        return reason


class ReceiveRequest(BaseModel):
    received_quantity: int | None = None
    resolution_action: str | None = None
    resolution_reason: str | None = None
    surplus_location_id: int | None = Field(default=None, gt=0)
    idempotency_key: str | None = Field(default=None, max_length=100)

    @field_validator("received_quantity")
    @classmethod
    def validate_received_quantity(cls, value: int | None) -> int | None:
        if value is not None and value <= 0:
            raise ValueError("入库数量必须大于0")
        return value


class BatchReceiveLine(BaseModel):
    item_id: int | str
    received_quantity: int
    resolution_action: str | None = None
    resolution_reason: str | None = None
    surplus_location_id: int | None = Field(default=None, gt=0)
    idempotency_key: str | None = Field(default=None, max_length=100)

    @field_validator("received_quantity")
    @classmethod
    def validate_received_quantity(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("入库数量必须大于0")
        return value


class BatchReceiveRequest(BaseModel):
    items: list[BatchReceiveLine] = Field(min_length=1, max_length=200)
    idempotency_key: str | None = Field(default=None, max_length=80)


class AcceptShortRequest(BaseModel):
    reason: str | None = None


def _component_kind(name: str | None) -> str:
    value = str(name or "")
    if value.endswith("-底"):
        return "base"
    if value.endswith("-盖"):
        return "cover"
    return ""


def _is_component_key(value: int | str) -> bool:
    return isinstance(value, str) and value.startswith("r") and value[1:].isdigit()


def _component_id(value: int | str) -> int:
    if _is_component_key(value):
        return int(str(value)[1:])
    return int(value)


def _apply_component_crease(data: dict, component: str) -> None:
    if component == "base":
        data["snapshot_crease_type"] = data.get("snapshot_base_crease_type")
        data["snapshot_crease_left_mm"] = data.get("snapshot_base_crease_left_mm")
        data["snapshot_crease_middle_mm"] = data.get("snapshot_base_crease_middle_mm")
        data["snapshot_crease_right_mm"] = data.get("snapshot_base_crease_right_mm")


def _component_receive_times(
    db: Session,
    *,
    received_since: datetime,
) -> dict[int, datetime]:
    query = select(OperationLog).where(
        OperationLog.action == "RECEIVE_MATERIAL",
        OperationLog.details.like("%requisition_item_id%"),
    )
    if received_since.year > 2000:
        query = query.where(OperationLog.created_at >= received_since)
    times: dict[int, datetime] = {}
    for log in db.scalars(query.order_by(OperationLog.created_at.desc())).all():
        try:
            details = json.loads(log.details or "{}")
        except json.JSONDecodeError:
            continue
        requisition_item_id = details.get("requisition_item_id")
        if not requisition_item_id:
            continue
        times.setdefault(int(requisition_item_id), log.created_at)
    return times


def _visible_customer_ids(user: User, db: Session) -> set[int] | None:
    if has_unrestricted_customer_access(user, db):
        return None
    return customer_scope_ids(user, db)


def _require_order_item_customer_access(
    db: Session,
    *,
    order_item_id: int,
    user: User,
) -> None:
    customer_id = db.scalar(
        select(Order.customer_id)
        .join(OrderItem, OrderItem.order_id == Order.id)
        .where(OrderItem.id == order_item_id)
    )
    if customer_id is not None:
        require_customer_access(customer_id, user, db)


def _preflight_item_customer_access(
    db: Session,
    *,
    item_id: int | str,
    user: User,
) -> None:
    if _is_component_key(item_id):
        order_item_id = db.scalar(
            select(RequisitionItem.order_item_id).where(
                RequisitionItem.id == _component_id(item_id)
            )
        )
    else:
        try:
            order_item_id = int(item_id)
        except (TypeError, ValueError):
            return
    if order_item_id is not None:
        _require_order_item_customer_access(
            db,
            order_item_id=order_item_id,
            user=user,
        )


def _confirmed_supplier_order_item_ids(
    db: Session,
    *,
    order_item_ids: list[int],
) -> set[int]:
    if not order_item_ids:
        return set()
    return set(
        db.scalars(
            select(OrderItem.id)
            .join(
                SupplierRequisitionOrder,
                (SupplierRequisitionOrder.order_number == OrderItem.supplier_order_number)
                & (SupplierRequisitionOrder.status == "confirmed"),
            )
            .join(
                SupplierRequisitionOrderItem,
                (SupplierRequisitionOrderItem.supplier_order_id == SupplierRequisitionOrder.id)
                & (SupplierRequisitionOrderItem.order_item_id == OrderItem.id),
            )
            .where(OrderItem.id.in_(order_item_ids))
            .distinct()
        ).all()
    )


def _active_requisition_components(
    db: Session,
    *,
    order_item_ids: list[int],
    include_received: bool = False,
) -> list[RequisitionItem]:
    """Return only the requisition lines that the incoming workflow may use.

    Supplier-created requisition rows do not point directly at a supplier order.
    The order item's current supplier-order number is therefore the authority for
    selecting the latest matching requisition group.  Legacy effective rows keep
    their original behavior for order items without such a current supplier order.
    """
    if not order_item_ids:
        return []

    supplier_order_item_ids = _confirmed_supplier_order_item_ids(
        db,
        order_item_ids=order_item_ids,
    )
    legacy_order_item_ids = set(order_item_ids) - supplier_order_item_ids
    components: list[RequisitionItem] = []

    legacy_statuses = ["有效"]
    if include_received:
        legacy_statuses.append("已入库")
    if legacy_order_item_ids:
        components.extend(
            db.scalars(
                select(RequisitionItem)
                .where(
                    RequisitionItem.order_item_id.in_(legacy_order_item_ids),
                    RequisitionItem.status.in_(legacy_statuses),
                )
                .order_by(RequisitionItem.order_item_id, RequisitionItem.id)
            ).all()
        )

    supplier_statuses = ["supplier_requisition_created"]
    if include_received:
        supplier_statuses.append("已入库")
    if supplier_order_item_ids:
        latest_groups = (
            select(
                RequisitionItem.order_item_id.label("order_item_id"),
                func.max(RequisitionItem.requisition_id).label("requisition_id"),
            )
            .join(Requisition, Requisition.id == RequisitionItem.requisition_id)
            .where(
                RequisitionItem.order_item_id.in_(supplier_order_item_ids),
                Requisition.status == "supplier_requisition_created",
            )
            .group_by(RequisitionItem.order_item_id)
            .subquery()
        )
        components.extend(
            db.scalars(
                select(RequisitionItem)
                .join(
                    latest_groups,
                    (latest_groups.c.order_item_id == RequisitionItem.order_item_id)
                    & (latest_groups.c.requisition_id == RequisitionItem.requisition_id),
                )
                .where(RequisitionItem.status.in_(supplier_statuses))
                .order_by(RequisitionItem.order_item_id, RequisitionItem.id)
            ).all()
        )
    return components


def _rows(
    db: Session,
    *,
    user: User,
    received_since: datetime | None = None,
) -> list[dict]:
    receiver = aliased(User)
    query = (
        select(
            OrderItem.id.label("item_id"),
            OrderItem.product_id,
            Order.id.label("order_id"),
            Customer.id.label("customer_id"),
            Order.order_number,
            Order.customer_po,
            Order.created_at,
            Customer.name.label("customer_name"),
            OrderItem.snapshot_product_name.label("product_name"),
            func.coalesce(
                OrderItem.snapshot_product_code,
                Product.product_code,
            ).label("product_code"),
            OrderItem.snapshot_spec.label("specification"),
            OrderItem.snapshot_material.label("material"),
            OrderItem.flute_type,
            OrderItem.quantity,
            Order.delivery_date,
            Order.status.label("order_status"),
            OrderItem.material_status,
            OrderItem.requisition_status,
            OrderItem.requisition_qty,
            OrderItem.requisition_date,
            OrderItem.requisition_spec,
            OrderItem.cardboard_len,
            OrderItem.cardboard_width,
            OrderItem.snapshot_crease_type,
            OrderItem.snapshot_crease_left_mm,
            OrderItem.snapshot_crease_middle_mm,
            OrderItem.snapshot_crease_right_mm,
            OrderItem.snapshot_base_crease_type,
            OrderItem.snapshot_base_crease_left_mm,
            OrderItem.snapshot_base_crease_middle_mm,
            OrderItem.snapshot_base_crease_right_mm,
            OrderItem.snapshot_supplier_name,
            OrderItem.requisition_remark,
            OrderItem.special_process,
            OrderItem.supplier_delivery_time,
            OrderItem.supplier_order_number,
            OrderItem.material_received_at,
            OrderItem.material_received_by,
            OrderItem.drawing_file.label("order_item_drawing_file"),
            receiver.real_name.label("received_by_name"),
        )
        .join(Order, Order.id == OrderItem.order_id)
        .join(Product, Product.id == OrderItem.product_id)
        .join(Customer, Customer.id == Order.customer_id)
        .outerjoin(receiver, receiver.id == OrderItem.material_received_by)
    )
    visible_customer_ids = _visible_customer_ids(user, db)
    if visible_customer_ids is not None:
        query = query.where(Order.customer_id.in_(visible_customer_ids))
    if received_since is None:
        query = query.where(
            Order.status.notin_(["cancelled", "dead"]),
            OrderItem.material_status == "pending",
            OrderItem.requisition_status.in_(["已报料", "供应商已排单"]),
        ).order_by(
            OrderItem.requisition_date.desc(),
            OrderItem.created_at.desc(),
            OrderItem.id.desc(),
        )
    else:
        query = query.where(
            OrderItem.material_status == "received",
            OrderItem.material_received_at >= received_since,
        ).order_by(
            OrderItem.material_received_at.desc(),
            OrderItem.id.desc(),
        )
    registry = build_display_registry(db)
    base_rows = []
    for row in db.execute(query):
        data = dict(row._mapping)
        data["incoming_quantity"] = (
            data["requisition_qty"]
            if data.get("requisition_qty") is not None
            else data["quantity"]
        )
        base_rows.append(data)

    rows = []
    received_component_order_item_ids: set[int] = set()
    if received_since is not None:
        receive_times = _component_receive_times(db, received_since=received_since)
        component_query = (
            select(
                RequisitionItem,
                OrderItem,
                Order,
                Product,
                Customer,
                receiver.real_name.label("received_by_name"),
            )
            .join(OrderItem, OrderItem.id == RequisitionItem.order_item_id)
            .join(Order, Order.id == OrderItem.order_id)
            .join(Product, Product.id == OrderItem.product_id)
            .join(Customer, Customer.id == Order.customer_id)
            .outerjoin(receiver, receiver.id == OrderItem.material_received_by)
            .where(RequisitionItem.status == "已入库")
            .order_by(RequisitionItem.id.desc())
        )
        if visible_customer_ids is not None:
            component_query = component_query.where(
                Order.customer_id.in_(visible_customer_ids)
            )
        for req, item, order, product, customer, received_by_name in db.execute(
            component_query
        ):
            component = _component_kind(req.product_name_snapshot)
            if not component:
                continue
            received_at = (
                receive_times.get(req.id)
                or item.material_received_at
                or req.created_at
            )
            if received_since.year > 2000 and received_at < received_since:
                continue
            received_component_order_item_ids.add(item.id)
            component_data = {
                "item_id": f"r{req.id}",
                "order_item_id": item.id,
                "requisition_item_id": req.id,
                "component_type": component,
                "product_id": item.product_id,
                "order_id": order.id,
                "order_number": order.order_number,
                "customer_po": order.customer_po,
                "customer_name": customer.name,
                "product_name": req.product_name_snapshot or item.snapshot_product_name,
                "product_code": req.product_code_snapshot
                or item.snapshot_product_code
                or product.product_code,
                "specification": req.specification_snapshot or item.snapshot_spec,
                "material": req.material_snapshot or item.snapshot_material,
                "flute_type": item.flute_type,
                "quantity": item.quantity,
                "delivery_date": order.delivery_date,
                "order_status": order.status,
                "material_status": item.material_status,
                "requisition_status": item.requisition_status,
                "requisition_qty": req.requisition_qty,
                "incoming_quantity": req.requisition_qty,
                "requisition_date": item.requisition_date,
                "requisition_spec": item.requisition_spec,
                "cardboard_len": req.cardboard_len,
                "cardboard_width": req.cardboard_width,
                "snapshot_crease_type": item.snapshot_crease_type,
                "snapshot_crease_left_mm": item.snapshot_crease_left_mm,
                "snapshot_crease_middle_mm": item.snapshot_crease_middle_mm,
                "snapshot_crease_right_mm": item.snapshot_crease_right_mm,
                "snapshot_base_crease_type": item.snapshot_base_crease_type,
                "snapshot_base_crease_left_mm": item.snapshot_base_crease_left_mm,
                "snapshot_base_crease_middle_mm": item.snapshot_base_crease_middle_mm,
                "snapshot_base_crease_right_mm": item.snapshot_base_crease_right_mm,
                "snapshot_supplier_name": item.snapshot_supplier_name,
                "requisition_remark": req.remark or item.requisition_remark,
                "special_process": req.special_process,
                "supplier_delivery_time": item.supplier_delivery_time,
                "supplier_order_number": item.supplier_order_number,
                "material_received_at": received_at,
                "material_received_by": item.material_received_by,
                "order_item_drawing_file": item.drawing_file,
                "received_by_name": received_by_name,
            }
            _apply_component_crease(component_data, component)
            rows.append(component_data)

    component_requisition_items: dict[int, list[RequisitionItem]] = {}
    order_items_with_requisitions: set[int] = set()
    order_item_ids = [row["item_id"] for row in base_rows if row.get("item_id")]
    if order_item_ids:
        order_items_with_requisitions = set(
            db.scalars(
                select(RequisitionItem.order_item_id).where(
                    RequisitionItem.order_item_id.in_(order_item_ids)
                )
            ).all()
        )
        if received_since is None:
            req_rows = _active_requisition_components(
                db,
                order_item_ids=order_item_ids,
            )
        else:
            req_rows = db.scalars(
                select(RequisitionItem)
                .where(
                    RequisitionItem.order_item_id.in_(order_item_ids),
                    RequisitionItem.status == "已入库",
                )
                .order_by(RequisitionItem.order_item_id, RequisitionItem.id)
            ).all()
        for req in req_rows:
            if received_since is None or req.status == "已入库":
                component_requisition_items.setdefault(req.order_item_id, []).append(req)

    for data in base_rows:
        if data["item_id"] in received_component_order_item_ids:
            continue
        req_rows = component_requisition_items.get(data["item_id"], [])
        if req_rows:
            for req in req_rows:
                component = _component_kind(req.product_name_snapshot)
                component_data = dict(data)
                component_data["order_item_id"] = data["item_id"]
                component_data["requisition_item_id"] = req.id
                # Any row backed by a concrete requisition item must use its
                # own route key.  Otherwise a normal single-piece row falls
                # through to the order-item receive/revert path and leaves the
                # selected supplier requisition row in the wrong status.
                component_data["item_id"] = f"r{req.id}"
                component_data["component_type"] = component or "single"
                component_data["product_code"] = req.product_code_snapshot or data.get("product_code")
                component_data["product_name"] = req.product_name_snapshot or data.get("product_name")
                component_data["material"] = req.material_snapshot or data.get("material")
                component_data["incoming_quantity"] = req.requisition_qty
                component_data["requisition_qty"] = req.requisition_qty
                component_data["cardboard_len"] = req.cardboard_len
                component_data["cardboard_width"] = req.cardboard_width
                component_data["special_process"] = req.special_process
                component_data["requisition_remark"] = req.remark or data.get("requisition_remark")
                _apply_component_crease(component_data, component)
                rows.append(component_data)
        elif data["item_id"] in order_items_with_requisitions:
            continue
        else:
            rows.append(data)
    rows = _decorate_rows_with_display_numbers(db, rows, registry)
    product_ids = {row["product_id"] for row in rows if row.get("product_id")}

    # v0.23.0 P0-4/P0-5：明细快照（订单/报料时写入）优先；快照缺失时才回退到
    # 常用箱当前值——绝不从材质字典反查楞型，只读常用箱自身的 flute_type /
    # crease_*_mm 字段。历史数据不做任何回写，只在展示时按需回退。
    products_by_id: dict[int, Product] = {}
    if product_ids:
        products_by_id = {
            product.id: product
            for product in db.scalars(
                select(Product)
                .options(selectinload(Product.material))
                .where(Product.id.in_(product_ids))
            ).all()
        }
    for data in rows:
        product = products_by_id.get(data.get("product_id"))

        material_code = (data.get("material") or "").strip()
        if not material_code and product is not None:
            fallback_code = (
                product.material.code if product.material is not None else None
            ) or product.legacy_material_text
            material_code = (fallback_code or "").strip()

        flute_type = (data.get("flute_type") or "").strip()
        if not flute_type and product is not None:
            flute_type = (product.flute_type or "").strip()

        data["material_code"] = material_code
        data["flute_type"] = flute_type
        if material_code and flute_type:
            data["material_display"] = f"{material_code} / {flute_type}"
        else:
            data["material_display"] = material_code

        if product is not None:
            if not (data.get("snapshot_crease_type") or "").strip():
                data["snapshot_crease_type"] = product.crease_type
            if data.get("snapshot_crease_left_mm") is None:
                data["snapshot_crease_left_mm"] = product.crease_left_mm
            if data.get("snapshot_crease_middle_mm") is None:
                data["snapshot_crease_middle_mm"] = product.crease_middle_mm
            if data.get("snapshot_crease_right_mm") is None:
                data["snapshot_crease_right_mm"] = product.crease_right_mm

    latest_drawings: dict[int, ProductDrawing] = {}
    if product_ids:
        drawings = db.scalars(
            select(ProductDrawing)
            .where(ProductDrawing.product_id.in_(product_ids))
            .order_by(
                ProductDrawing.product_id,
                ProductDrawing.uploaded_at.desc(),
                ProductDrawing.id.desc(),
            )
        ).all()
        for drawing in drawings:
            latest_drawings.setdefault(drawing.product_id, drawing)
    for row in rows:
        # v0.23.0 P0-3：订单/明细上传的图纸优先于常用箱图纸——车间来料页面
        # 需要能看到"这一单"实际上传的图纸，而不仅仅是常用箱历史图纸。
        product_drawing = latest_drawings.get(row.get("product_id"))
        product_drawing_path = product_drawing.image_path if product_drawing else None
        order_drawing_path = (row.get("order_item_drawing_file") or "").strip() or None
        final_path = order_drawing_path or product_drawing_path
        row["order_drawing_path"] = order_drawing_path
        row["product_drawing_path"] = product_drawing_path
        row["drawing_path"] = final_path
        row["drawing_is_pdf"] = bool(final_path and final_path.lower().endswith(".pdf"))
        summary = source_summary_for_item(db, row["item_id"])
        if summary is not None:
            row.update(summary)
            if received_since is None:
                row["incoming_quantity"] = summary["remaining_quantity"]
        else:
            planned = int(row.get("requisition_qty") or row.get("quantity") or 0)
            row.update(
                {
                    "planned_quantity": planned,
                    "cumulative_received_quantity": 0,
                    "remaining_quantity": planned,
                    "variance_quantity": 0,
                    "variance_type": None,
                    "resolution_status": "not_required",
                    "resolution_action": None,
                    "pending_receipt_item_id": None,
                    "latest_receipt_item_id": None,
                    "latest_receipt_id": None,
                    "surplus_inventory_lot_id": None,
                }
            )
    return rows


def _decorate_rows_with_display_numbers(
    db: Session, rows: list[dict], registry
) -> list[dict]:
    if not rows:
        return rows
    order_ids = {row["order_id"] for row in rows if row.get("order_id")}
    history_orders = {
        order.id: order
        for order in db.scalars(select(Order).where(Order.id.in_(order_ids))).all()
    }
    for row in rows:
        order = history_orders.get(row.get("order_id"))
        display = display_order_number(order, registry) if order is not None else row.get("order_number")
        row["order_number"] = display
        row["display_order_number"] = display
    return rows


def _audit(
    db: Session,
    *,
    user: User,
    action: str,
    item_id: int,
    details: dict,
) -> None:
    db.add(
        OperationLog(
            user_id=user.id,
            action=action,
            resource="OrderItem",
            details=json.dumps(details, ensure_ascii=False, default=str),
            username=user.username,
            role=user.role,
            entity_type="order_item",
            entity_id=item_id,
            description=(
                "车间来料入库"
                if action == "RECEIVE_MATERIAL"
                else "撤回来料入库"
            ),
        )
    )


def _item_response(db: Session, item_id: int) -> dict:
    item = db.get(OrderItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="订单明细不存在")
    return {
        "item_id": item.id,
        "material_status": item.material_status,
        "requisition_status": item.requisition_status,
        "requisition_qty": item.requisition_qty,
        "incoming_quantity": item.requisition_qty or item.quantity,
        "material_received_at": (
            utc_naive_to_api(item.material_received_at)
            if item.material_received_at
            else None
        ),
        "material_received_by": item.material_received_by,
    }


def _component_response(db: Session, requisition_item_id: int) -> dict:
    row = db.execute(
        select(RequisitionItem, OrderItem)
        .join(OrderItem, OrderItem.id == RequisitionItem.order_item_id)
        .where(RequisitionItem.id == requisition_item_id)
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="报料明细不存在")
    requisition_item, order_item = row
    return {
        "item_id": f"r{requisition_item.id}",
        "order_item_id": order_item.id,
        "requisition_item_id": requisition_item.id,
        "material_status": order_item.material_status,
        "requisition_status": order_item.requisition_status,
        "requisition_qty": requisition_item.requisition_qty,
        "incoming_quantity": requisition_item.requisition_qty,
        "material_received_at": (
            utc_naive_to_api(order_item.material_received_at)
            if order_item.material_received_at
            else None
        ),
        "material_received_by": order_item.material_received_by,
        "component_status": requisition_item.status,
    }


def _receive_requisition_component(
    db: Session,
    *,
    user: User,
    requisition_item_id: int,
    received_quantity: int | None,
) -> dict:
    received_at = _utc_now()
    row = db.execute(
        select(RequisitionItem, OrderItem)
        .join(OrderItem, OrderItem.id == RequisitionItem.order_item_id)
        .where(RequisitionItem.id == requisition_item_id)
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="报料明细不存在")
    requisition_item, order_item = row
    _require_order_item_customer_access(
        db,
        order_item_id=order_item.id,
        user=user,
    )
    active_component_ids = {
        item.id
        for item in _active_requisition_components(
            db,
            order_item_ids=[order_item.id],
        )
    }
    if requisition_item.id not in active_component_ids:
        raise HTTPException(status_code=409, detail="该报料明细当前不可入库")
    if order_item.material_status != "pending" or order_item.requisition_status not in {
        "已报料",
        "供应商已排单",
    }:
        raise HTTPException(status_code=409, detail="该明细当前不可入库，可能已入库、已作废或状态已变化")

    final_quantity = (
        received_quantity
        if received_quantity is not None
        else int(requisition_item.requisition_qty or 0)
    )
    if final_quantity <= 0:
        raise HTTPException(status_code=400, detail="入库数量必须大于0")

    previous_requisition_qty = requisition_item.requisition_qty
    requisition_item.requisition_qty = final_quantity
    requisition_item.status = "已入库"

    remaining_components = len(
        _active_requisition_components(
            db,
            order_item_ids=[order_item.id],
        )
    )
    if remaining_components == 0:
        total_received = sum(
            int(item.requisition_qty or 0)
            for item in _active_requisition_components(
                db,
                order_item_ids=[order_item.id],
                include_received=True,
            )
            if item.status == "已入库"
        )
        order_item.material_status = "received"
        order_item.requisition_status = "已入库"
        order_item.material_received_at = received_at
        order_item.material_received_by = user.id
        order_item.requisition_qty = int(total_received)
        db.flush()
        if not _refresh_production_after_material_change(db, order_item):
            remaining_pending = db.scalar(
                select(func.count(OrderItem.id)).where(
                    OrderItem.order_id == order_item.order_id,
                    OrderItem.material_status != "received",
                )
            ) or 0
            if remaining_pending == 0:
                db.execute(
                    update(Order)
                    .where(
                        Order.id == order_item.order_id,
                        Order.status.in_(["pending_production", "production"]),
                    )
                    .values(status="pending_delivery")
                )

    _audit(
        db,
        user=user,
        action="RECEIVE_MATERIAL",
        item_id=order_item.id,
        details={
            "received_at": received_at,
            "requisition_item_id": requisition_item.id,
            "component_type": _component_kind(requisition_item.product_name_snapshot),
            "previous_requisition_qty": previous_requisition_qty,
            "received_quantity": final_quantity,
        },
    )
    db.flush()
    return _component_response(db, requisition_item.id)


def _receive_material(
    db: Session,
    *,
    user: User,
    item_id: int | str,
    received_quantity: int | None,
) -> dict:
    if _is_component_key(item_id):
        return _receive_requisition_component(
            db,
            user=user,
            requisition_item_id=_component_id(item_id),
            received_quantity=received_quantity,
        )
    received_at = _utc_now()
    current = db.get(OrderItem, _component_id(item_id))
    if current is None:
        raise HTTPException(status_code=404, detail="订单明细不存在")
    _require_order_item_customer_access(
        db,
        order_item_id=current.id,
        user=user,
    )
    final_quantity = (
        received_quantity
        if received_quantity is not None
        else (current.requisition_qty or current.quantity)
    )
    if final_quantity <= 0:
        raise HTTPException(status_code=400, detail="入库数量必须大于0")
    previous_requisition_qty = current.requisition_qty
    result = db.execute(
        update(OrderItem)
        .where(
            OrderItem.id == item_id,
            OrderItem.material_status == "pending",
            OrderItem.requisition_status.in_(["已报料", "供应商已排单"]),
        )
        .values(
            material_status="received",
            requisition_status="已入库",
            material_received_at=received_at,
            material_received_by=user.id,
            requisition_qty=final_quantity,
        )
    )
    current.material_status = "received"
    current.requisition_status = "已入库"
    current.material_received_at = received_at
    current.material_received_by = user.id
    current.requisition_qty = final_quantity
    if result.rowcount != 1:
        exists = db.scalar(select(OrderItem.id).where(OrderItem.id == item_id))
        if exists is None:
            raise HTTPException(status_code=404, detail="订单明细不存在")
        raise HTTPException(
            status_code=409,
            detail="该明细当前不可入库，可能已入库、已作废或状态已变化",
        )
    db.flush()
    if not _refresh_production_after_material_change(db, current):
        remaining_pending = db.scalar(
            select(func.count(OrderItem.id)).where(
                OrderItem.order_id == current.order_id,
                OrderItem.material_status != "received",
            )
        ) or 0
        if remaining_pending == 0:
            db.execute(
                update(Order)
                .where(
                    Order.id == current.order_id,
                    Order.status.in_(["pending_production", "production"]),
                )
                .values(status="pending_delivery")
            )
    active_requisition_items = db.scalars(
        select(RequisitionItem)
        .where(
            RequisitionItem.order_item_id == item_id,
            RequisitionItem.status == "有效",
        )
        .order_by(RequisitionItem.id)
    ).all()
    if len(active_requisition_items) <= 1:
        db.execute(
            update(RequisitionItem)
            .where(
                RequisitionItem.order_item_id == item_id,
                RequisitionItem.status == "有效",
            )
            .values(requisition_qty=final_quantity)
        )
    else:
        remaining = int(final_quantity)
        for index, requisition_item in enumerate(active_requisition_items):
            if index == len(active_requisition_items) - 1:
                assigned = max(remaining, 0)
            else:
                assigned = min(
                    int(requisition_item.requisition_qty or 0),
                    max(remaining, 0),
                )
            requisition_item.requisition_qty = assigned
            remaining -= assigned
    _audit(
        db,
        user=user,
        action="RECEIVE_MATERIAL",
        item_id=item_id,
        details={
            "received_at": received_at,
            "previous_requisition_qty": previous_requisition_qty,
            "received_quantity": final_quantity,
        },
    )
    db.flush()
    return _item_response(db, current.id)


def _receipt_fact_rows(
    db: Session,
    *,
    user: User,
    received_since: datetime | None,
    include_reversed: bool = False,
) -> list[dict]:
    registry = build_display_registry(db)
    visible_customer_ids = _visible_customer_ids(user, db)
    rows: list[dict] = []
    for fact in receipt_history(
        db,
        received_since=received_since,
        include_reversed=include_reversed,
    ):
        item = db.get(OrderItem, fact.order_item_id)
        order = db.get(Order, fact.order_id)
        if item is None or order is None:
            continue
        if (
            visible_customer_ids is not None
            and order.customer_id not in visible_customer_ids
        ):
            continue
        product = db.get(Product, item.product_id)
        customer = db.get(Customer, order.customer_id)
        requisition_item = (
            db.get(RequisitionItem, fact.requisition_item_id)
            if fact.requisition_item_id
            else None
        )
        component = _component_kind(
            requisition_item.product_name_snapshot if requisition_item else None
        )
        product_code = (
            requisition_item.product_code_snapshot if requisition_item else None
        ) or item.snapshot_product_code or (product.product_code if product else None)
        product_name = (
            requisition_item.product_name_snapshot if requisition_item else None
        ) or item.snapshot_product_name
        material_code = (
            requisition_item.material_snapshot if requisition_item else None
        ) or item.snapshot_material or ""
        display_number = display_order_number(order, registry)
        drawing_path = (item.drawing_file or "").strip() or None
        if drawing_path is None and product is not None:
            drawing = db.scalar(
                select(ProductDrawing)
                .where(ProductDrawing.product_id == product.id)
                .order_by(ProductDrawing.uploaded_at.desc(), ProductDrawing.id.desc())
            )
            drawing_path = drawing.image_path if drawing else None
        receiver = db.get(User, fact.receipt.received_by) if fact.receipt.received_by else None
        row = {
            "history_key": f"receipt-{fact.id}",
            "item_id": f"r{fact.requisition_item_id}" if fact.requisition_item_id else fact.order_item_id,
            "order_item_id": fact.order_item_id,
            "requisition_item_id": fact.requisition_item_id,
            "receipt_id": fact.receipt_id,
            "receipt_item_id": fact.id,
            "receipt_number": fact.receipt.receipt_number,
            "receipt_status": fact.status,
            "product_id": item.product_id,
            "order_id": order.id,
            "order_number": display_number,
            "display_order_number": display_number,
            "customer_po": order.customer_po,
            "customer_name": customer.name if customer else "",
            "product_name": product_name,
            "product_code": product_code,
            "specification": (
                requisition_item.specification_snapshot if requisition_item else None
            )
            or item.snapshot_spec,
            "material": material_code,
            "material_code": material_code,
            "flute_type": item.flute_type or (product.flute_type if product else None),
            "material_display": (
                f"{material_code} / {item.flute_type}" if material_code and item.flute_type else material_code
            ),
            "quantity": item.quantity,
            "delivery_date": order.delivery_date,
            "order_status": order.status,
            "material_status": item.material_status,
            "requisition_status": item.requisition_status,
            "requisition_qty": fact.planned_quantity,
            "incoming_quantity": fact.received_quantity,
            "planned_quantity": fact.planned_quantity,
            "received_quantity_this_time": fact.received_quantity,
            "cumulative_received_quantity": fact.cumulative_received_quantity,
            "remaining_quantity": max(
                fact.planned_quantity - fact.cumulative_received_quantity, 0
            ),
            "variance_quantity": fact.variance_quantity,
            "variance_type": fact.variance_type,
            "resolution_status": fact.resolution_status,
            "resolution_action": fact.resolution_action,
            "resolution_reason": fact.resolution_reason,
            "surplus_inventory_lot_id": fact.surplus_inventory_lot_id,
            "requisition_date": item.requisition_date,
            "requisition_spec": item.requisition_spec,
            "cardboard_len": (
                requisition_item.cardboard_len if requisition_item else item.cardboard_len
            ),
            "cardboard_width": (
                requisition_item.cardboard_width if requisition_item else item.cardboard_width
            ),
            "snapshot_crease_type": item.snapshot_crease_type,
            "snapshot_crease_left_mm": item.snapshot_crease_left_mm,
            "snapshot_crease_middle_mm": item.snapshot_crease_middle_mm,
            "snapshot_crease_right_mm": item.snapshot_crease_right_mm,
            "snapshot_base_crease_type": item.snapshot_base_crease_type,
            "snapshot_base_crease_left_mm": item.snapshot_base_crease_left_mm,
            "snapshot_base_crease_middle_mm": item.snapshot_base_crease_middle_mm,
            "snapshot_base_crease_right_mm": item.snapshot_base_crease_right_mm,
            "snapshot_supplier_name": item.snapshot_supplier_name,
            "requisition_remark": (
                requisition_item.remark if requisition_item else item.requisition_remark
            ),
            "special_process": (
                requisition_item.special_process if requisition_item else item.special_process
            ),
            "supplier_delivery_time": item.supplier_delivery_time,
            "supplier_order_number": item.supplier_order_number,
            "material_received_at": fact.receipt.received_at,
            "material_received_by": fact.receipt.received_by,
            "received_by_name": receiver.real_name if receiver else None,
            "component_type": component or "single",
            "drawing_path": drawing_path,
            "drawing_is_pdf": bool(drawing_path and drawing_path.lower().endswith(".pdf")),
        }
        _apply_component_crease(row, component)
        rows.append(row)
    return rows


def _received_rows(
    db: Session,
    *,
    user: User,
    received_since: datetime,
    include_reversed: bool = False,
) -> list[dict]:
    facts = _receipt_fact_rows(
        db,
        user=user,
        received_since=received_since,
        include_reversed=include_reversed,
    )
    fact_keys = {str(row["item_id"]) for row in facts}
    legacy = [
        row for row in _rows(db, user=user, received_since=received_since)
        if str(row["item_id"]) not in fact_keys
    ]
    combined = [*facts, *legacy]
    combined.sort(
        key=lambda row: row.get("material_received_at") or datetime.min,
        reverse=True,
    )
    return combined



def _incoming_row_response(row: dict) -> dict:
    """Serialize incoming-list datetime fields using their storage contracts."""

    response = dict(row)
    if response.get("created_at"):
        response["created_at"] = utc_naive_to_api(response["created_at"])
    if response.get("supplier_delivery_time"):
        response["supplier_delivery_time"] = beijing_naive_to_api(
            response["supplier_delivery_time"]
        )
    if response.get("material_received_at"):
        response["material_received_at"] = utc_naive_to_api(
            response["material_received_at"]
        )
    return response


@router.get("/surplus-locations")
def surplus_inventory_locations(
    db: Session = Depends(get_db),
    _user: User = Depends(can_operate),
) -> dict:
    """Return only locations that can receive an incoming surplus transfer."""
    rows = db.scalars(
        select(WarehouseLocation)
        .where(
            WarehouseLocation.is_active.is_(True),
            WarehouseLocation.warehouse_type.in_(("semi_finished", "shared")),
        )
        .order_by(WarehouseLocation.location_code, WarehouseLocation.id)
    ).all()
    return {
        "items": [
            {
                "id": row.id,
                "location_code": row.location_code,
                "location_name": row.location_name,
                "warehouse_type": row.warehouse_type,
            }
            for row in rows
        ]
    }


@router.get("/pending")
def pending_items(
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    return {
        "items": [
            _incoming_row_response(row)
            for row in _rows(db, user=user)
        ]
    }


@router.get("/received")
def recently_received_items(
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    return {
        "items": [
            _incoming_row_response(row)
            for row in _received_rows(
                db,
                user=user,
                received_since=_utc_now() - timedelta(hours=24),
            )
        ]
    }


@router.get("/history")
def history_received_items(
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    """返回全部历史入库记录（不限时间）。"""
    # received_since=epoch_start 表示"从最早时间起"即不过滤
    epoch_start = datetime(2000, 1, 1)
    return {
        "items": [
            _incoming_row_response(row)
            for row in _received_rows(
                db,
                user=user,
                received_since=epoch_start,
                include_reversed=True,
            )
        ]
    }


def _lan_ip() -> str:
    connection = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        connection.connect(("8.8.8.8", 80))
        return connection.getsockname()[0]
    except OSError:
        return socket.gethostbyname(socket.gethostname())
    finally:
        connection.close()


@router.get("/mobile-entry")
def mobile_entry(
    request: Request,
    _user: User = Depends(can_read),
) -> dict:
    port = request.url.port or 8000
    url = f"http://{_lan_ip()}:{port}/incoming.html"
    image = qrcode.make(url)
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return {"url": url, "qr_data_url": f"data:image/png;base64,{encoded}"}


def _new_receipt_response(db: Session, fact: IncomingReceiptItem) -> dict:
    item_key: int | str = (
        f"r{fact.requisition_item_id}"
        if fact.requisition_item_id
        else fact.order_item_id
    )
    response = (
        _component_response(db, fact.requisition_item_id)
        if fact.requisition_item_id
        else _item_response(db, fact.order_item_id)
    )
    response.update(receipt_item_dict(fact))
    summary = source_summary_for_item(db, item_key)
    if summary is not None:
        response.update(summary)
        response["incoming_quantity"] = (
            summary["remaining_quantity"]
            if summary["resolution_action"] == "await_supplier"
            and summary["remaining_quantity"] > 0
            else fact.received_quantity
        )
    return response


def _raise_receipt_error(error: IncomingReceiptError) -> None:
    raise HTTPException(status_code=error.status_code, detail=str(error)) from error


def _preflight_receipt_item_customer_access(
    db: Session,
    *,
    receipt_item_id: int,
    user: User,
) -> None:
    fact = db.get(IncomingReceiptItem, receipt_item_id)
    if fact is not None:
        _require_order_item_customer_access(
            db,
            order_item_id=fact.order_item_id,
            user=user,
        )


@router.put("/receive/{item_id}")
def receive_item(
    item_id: str,
    payload: ReceiveRequest | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    _preflight_item_customer_access(db, item_id=item_id, user=user)
    try:
        fact = receive_one(
            db,
            user=user,
            item_key=item_id,
            received_quantity=(
                payload.received_quantity if payload is not None else None
            ),
            resolution_action=(payload.resolution_action if payload else None),
            resolution_reason=(payload.resolution_reason if payload else None),
            surplus_location_id=(payload.surplus_location_id if payload else None),
            idempotency_key=(payload.idempotency_key if payload else None),
        )
        db.commit()
        return _new_receipt_response(db, fact)
    except IncomingReceiptError as error:
        db.rollback()
        _raise_receipt_error(error)
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise


@router.put("/batch-receive")
def batch_receive_items(
    payload: BatchReceiveRequest,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    # Check all targets before the first write.  A cross-customer item must not
    # turn a batch into a partial write that happens before the 403 response.
    for line in payload.items:
        _preflight_item_customer_access(db, item_id=line.item_id, user=user)
    seen: set[int | str] = set()
    results: list[dict] = []
    succeeded = 0
    for line in payload.items:
        if line.item_id in seen:
            results.append(
                {
                    "item_id": line.item_id,
                    "success": False,
                    "message": "同一明细不能重复提交",
                }
            )
            continue
        seen.add(line.item_id)
        try:
            with db.begin_nested():
                fact = receive_one(
                    db,
                    user=user,
                    item_key=line.item_id,
                    received_quantity=line.received_quantity,
                    resolution_action=line.resolution_action,
                    resolution_reason=line.resolution_reason,
                    surplus_location_id=line.surplus_location_id,
                    idempotency_key=(
                        line.idempotency_key
                        or (
                            f"{payload.idempotency_key}:{line.item_id}"
                            if payload.idempotency_key
                            else uuid4().hex
                        )
                    ),
                )
                response = _new_receipt_response(db, fact)
            results.append(
                {
                    "item_id": line.item_id,
                    "success": True,
                    "message": "入库成功",
                    "item": response,
                }
            )
            succeeded += 1
        except (HTTPException, IncomingReceiptError) as error:
            results.append(
                {
                    "item_id": line.item_id,
                    "success": False,
                    "message": str(
                        error.detail if isinstance(error, HTTPException) else error
                    ),
                }
            )
        except Exception:
            results.append(
                {
                    "item_id": line.item_id,
                    "success": False,
                    "message": "系统处理失败，请刷新后重试",
                }
            )
    db.commit()
    return {
        "total": len(payload.items),
        "succeeded": succeeded,
        "failed": len(payload.items) - succeeded,
        "results": results,
    }


@router.put("/receipt-items/{receipt_item_id}/accept-short")
def accept_short_receipt_item(
    receipt_item_id: int,
    payload: AcceptShortRequest,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    _preflight_receipt_item_customer_access(
        db,
        receipt_item_id=receipt_item_id,
        user=user,
    )
    try:
        fact = accept_short(
            db,
            user=user,
            receipt_item_id=receipt_item_id,
            reason=payload.reason,
        )
        db.commit()
        return _new_receipt_response(db, fact)
    except IncomingReceiptError as error:
        db.rollback()
        _raise_receipt_error(error)


@router.put("/receipt-items/{receipt_item_id}/revert")
def revert_new_receipt_item(
    receipt_item_id: int,
    payload: RevertRequest,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    _preflight_receipt_item_customer_access(
        db,
        receipt_item_id=receipt_item_id,
        user=user,
    )
    try:
        fact = revert_receipt_item(
            db,
            user=user,
            receipt_item_id=receipt_item_id,
            reason=payload.reason,
        )
        db.commit()
        return receipt_item_dict(fact)
    except IncomingReceiptError as error:
        db.rollback()
        _raise_receipt_error(error)


def _revert_requisition_component(
    db: Session,
    *,
    requisition_item_id: int,
    payload: RevertRequest,
    user: User,
) -> dict:
    row = db.execute(
        select(RequisitionItem, OrderItem, Order)
        .join(OrderItem, OrderItem.id == RequisitionItem.order_item_id)
        .join(Order, Order.id == OrderItem.order_id)
        .where(RequisitionItem.id == requisition_item_id)
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="报料明细不存在")
    requisition_item, order_item, order = row
    _require_order_item_customer_access(
        db,
        order_item_id=order_item.id,
        user=user,
    )
    _lock_order_for_material_revert(db, order.id)
    row = db.execute(
        select(RequisitionItem, OrderItem, Order)
        .join(OrderItem, OrderItem.id == RequisitionItem.order_item_id)
        .join(Order, Order.id == OrderItem.order_id)
        .where(RequisitionItem.id == requisition_item_id)
        .execution_options(populate_existing=True)
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=409, detail="报料明细或关联订单已被删除，请刷新后重试")
    requisition_item, order_item, order = row
    if order.status in {"partially_delivered", "delivered"}:
        raise HTTPException(status_code=409, detail="订单已发货，禁止撤回来料")
    if requisition_item.status != "已入库":
        raise HTTPException(status_code=409, detail="该报料明细当前不是已入库状态")
    if has_production_completion_facts(db, [order_item.id]):
        raise HTTPException(status_code=409, detail="订单明细已有生产完工事实，不能撤销来料实收")

    active_components = _active_requisition_components(
        db,
        order_item_ids=[order_item.id],
        include_received=True,
    )
    if requisition_item.id not in {item.id for item in active_components}:
        raise HTTPException(status_code=409, detail="该报料明细当前不可撤回")
    restore_status = (
        "supplier_requisition_created"
        if order_item.id
        in _confirmed_supplier_order_item_ids(
            db,
            order_item_ids=[order_item.id],
        )
        else "有效"
    )

    previous_received_at = order_item.material_received_at
    previous_received_by = order_item.material_received_by
    try:
        result = db.execute(
            update(RequisitionItem)
            .where(
                RequisitionItem.id == requisition_item_id,
                RequisitionItem.status == "已入库",
            )
            .values(status=restore_status)
        )
        if result.rowcount != 1:
            raise HTTPException(status_code=409, detail="状态已变化，请刷新后重试")
        db.execute(
            update(OrderItem)
            .where(OrderItem.id == order_item.id)
            .values(
                material_status="pending",
                requisition_status="已报料",
                material_received_at=None,
                material_received_by=None,
            )
        )
        db.flush()
        db.refresh(order_item)
        _refresh_production_after_material_change(db, order_item)
        _audit(
            db,
            user=user,
            action="REVERT_MATERIAL",
            item_id=order_item.id,
            details={
                "reason": payload.reason,
                "requisition_item_id": requisition_item_id,
                "component_type": _component_kind(
                    requisition_item.product_name_snapshot
                ),
                "previous_received_at": previous_received_at,
                "previous_received_by": previous_received_by,
            },
        )
        db.commit()
        return _component_response(db, requisition_item_id)
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise


@router.put("/revert/{item_id}")
def revert_item(
    item_id: str,
    payload: RevertRequest,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    if _is_component_key(item_id):
        return _revert_requisition_component(
            db,
            requisition_item_id=_component_id(item_id),
            payload=payload,
            user=user,
        )
    try:
        item_id_int = int(item_id)
    except ValueError as error:
        raise HTTPException(status_code=400, detail="入库明细ID无效") from error
    row = db.execute(
        select(OrderItem, Order)
        .join(Order, Order.id == OrderItem.order_id)
        .where(OrderItem.id == item_id_int)
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="订单明细不存在")
    item, order = row
    _require_order_item_customer_access(
        db,
        order_item_id=item.id,
        user=user,
    )
    _lock_order_for_material_revert(db, order.id)
    row = db.execute(
        select(OrderItem, Order)
        .join(Order, Order.id == OrderItem.order_id)
        .where(OrderItem.id == item_id_int)
        .execution_options(populate_existing=True)
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=409, detail="订单明细或关联订单已被删除，请刷新后重试")
    item, order = row
    if order.status in {"partially_delivered", "delivered"}:
        raise HTTPException(status_code=409, detail="订单已发货，禁止撤回来料")
    if item.material_status != "received":
        raise HTTPException(status_code=409, detail="该明细当前不是已入库状态")
    if has_production_completion_facts(db, [item.id]):
        raise HTTPException(status_code=409, detail="订单明细已有生产完工事实，不能撤销来料实收")

    previous_received_at = item.material_received_at
    previous_received_by = item.material_received_by
    try:
        result = db.execute(
            update(OrderItem)
            .where(
                OrderItem.id == item_id_int,
                OrderItem.material_status == "received",
            )
            .values(
                material_status="pending",
                requisition_status="已报料",
                material_received_at=None,
                material_received_by=None,
            )
        )
        if result.rowcount != 1:
            raise HTTPException(status_code=409, detail="状态已变化，请刷新后重试")
        db.flush()
        db.refresh(item)
        _refresh_production_after_material_change(db, item)
        _audit(
            db,
            user=user,
            action="REVERT_MATERIAL",
            item_id=item_id_int,
            details={
                "reason": payload.reason,
                "previous_received_at": previous_received_at,
                "previous_received_by": previous_received_by,
            },
        )
        db.execute(
            update(RequisitionItem)
            .where(
                RequisitionItem.order_item_id == item_id_int,
                RequisitionItem.status == "已入库",
            )
            .values(status="有效")
        )
        db.commit()
        return _item_response(db, item_id_int)
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise
