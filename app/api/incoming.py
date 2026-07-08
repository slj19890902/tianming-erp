from __future__ import annotations

import base64
import json
import socket
from datetime import datetime, timedelta, timezone
from io import BytesIO

import qrcode
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, aliased, selectinload

from app.api.deps import RoleChecker, get_db
from app.models.audit import OperationLog
from app.models.customer import Customer
from app.models.order import Order, OrderItem
from app.models.product_drawing import ProductDrawing
from app.models.product import Product
from app.models.requisition import RequisitionItem
from app.models.user import User
from app.services.history_orders import build_display_registry, display_order_number


router = APIRouter()
can_read = RoleChecker(["admin", "workshop"])
can_operate = RoleChecker(["admin", "workshop"])


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


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

    @field_validator("received_quantity")
    @classmethod
    def validate_received_quantity(cls, value: int | None) -> int | None:
        if value is not None and value <= 0:
            raise ValueError("入库数量必须大于0")
        return value


class BatchReceiveLine(BaseModel):
    item_id: int | str
    received_quantity: int

    @field_validator("received_quantity")
    @classmethod
    def validate_received_quantity(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("入库数量必须大于0")
        return value


class BatchReceiveRequest(BaseModel):
    items: list[BatchReceiveLine] = Field(min_length=1, max_length=200)


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


def _rows(db: Session, *, received_since: datetime | None = None) -> list[dict]:
    receiver = aliased(User)
    query = (
        select(
            OrderItem.id.label("item_id"),
            OrderItem.product_id,
            Order.id.label("order_id"),
            Order.order_number,
            Order.customer_po,
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
    if received_since is None:
        query = query.where(
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
    component_status = "有效" if received_since is None else "已入库"
    order_item_ids = [row["item_id"] for row in base_rows if row.get("item_id")]
    if order_item_ids:
        req_rows = db.scalars(
            select(RequisitionItem)
            .where(RequisitionItem.order_item_id.in_(order_item_ids))
            .order_by(RequisitionItem.order_item_id, RequisitionItem.id)
        ).all()
        for req in req_rows:
            order_items_with_requisitions.add(req.order_item_id)
            if req.status == component_status:
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
                component_data["item_id"] = (
                    f"r{req.id}" if component else data["item_id"]
                )
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
        "material_received_at": item.material_received_at,
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
        "material_received_at": order_item.material_received_at,
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
    if requisition_item.status != "有效":
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

    remaining_components = db.scalar(
        select(func.count(RequisitionItem.id)).where(
            RequisitionItem.order_item_id == order_item.id,
            RequisitionItem.status == "有效",
        )
    ) or 0
    if remaining_components == 0:
        total_received = db.scalar(
            select(func.coalesce(func.sum(RequisitionItem.requisition_qty), 0)).where(
                RequisitionItem.order_item_id == order_item.id,
                RequisitionItem.status == "已入库",
            )
        ) or 0
        order_item.material_status = "received"
        order_item.requisition_status = "已入库"
        order_item.material_received_at = received_at
        order_item.material_received_by = user.id
        order_item.requisition_qty = int(total_received)
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


@router.get("/pending")
def pending_items(
    db: Session = Depends(get_db),
    _user: User = Depends(can_read),
) -> dict:
    return {"items": _rows(db)}


@router.get("/received")
def recently_received_items(
    db: Session = Depends(get_db),
    _user: User = Depends(can_read),
) -> dict:
    return {"items": _rows(db, received_since=_utc_now() - timedelta(hours=24))}


@router.get("/history")
def history_received_items(
    db: Session = Depends(get_db),
    _user: User = Depends(can_read),
) -> dict:
    """返回全部历史入库记录（不限时间）。"""
    # received_since=epoch_start 表示"从最早时间起"即不过滤
    epoch_start = datetime(2000, 1, 1)
    return {"items": _rows(db, received_since=epoch_start)}


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


@router.put("/receive/{item_id}")
def receive_item(
    item_id: str,
    payload: ReceiveRequest | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    try:
        response = _receive_material(
            db,
            user=user,
            item_id=item_id,
            received_quantity=(
                payload.received_quantity if payload is not None else None
            ),
        )
        db.commit()
        return response
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
                response = _receive_material(
                    db,
                    user=user,
                    item_id=line.item_id,
                    received_quantity=line.received_quantity,
                )
            results.append(
                {
                    "item_id": line.item_id,
                    "success": True,
                    "message": "入库成功",
                    "item": response,
                }
            )
            succeeded += 1
        except HTTPException as error:
            results.append(
                {
                    "item_id": line.item_id,
                    "success": False,
                    "message": str(error.detail),
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
    if order.status in {"partially_delivered", "delivered"}:
        raise HTTPException(status_code=409, detail="订单已发货，禁止撤回来料")
    if requisition_item.status != "已入库":
        raise HTTPException(status_code=409, detail="该报料明细当前不是已入库状态")

    previous_received_at = order_item.material_received_at
    previous_received_by = order_item.material_received_by
    try:
        result = db.execute(
            update(RequisitionItem)
            .where(
                RequisitionItem.id == requisition_item_id,
                RequisitionItem.status == "已入库",
            )
            .values(status="有效")
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
    if order.status in {"partially_delivered", "delivered"}:
        raise HTTPException(status_code=409, detail="订单已发货，禁止撤回来料")
    if item.material_status != "received":
        raise HTTPException(status_code=409, detail="该明细当前不是已入库状态")

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
