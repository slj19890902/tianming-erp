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
from sqlalchemy.orm import Session, aliased

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
    item_id: int
    received_quantity: int

    @field_validator("received_quantity")
    @classmethod
    def validate_received_quantity(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("入库数量必须大于0")
        return value


class BatchReceiveRequest(BaseModel):
    items: list[BatchReceiveLine] = Field(min_length=1, max_length=200)


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
            OrderItem.snapshot_supplier_name,
            OrderItem.requisition_remark,
            OrderItem.special_process,
            OrderItem.supplier_delivery_time,
            OrderItem.supplier_order_number,
            OrderItem.material_received_at,
            OrderItem.material_received_by,
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
    rows = []
    for row in db.execute(query):
        data = dict(row._mapping)
        data["incoming_quantity"] = (
            data["requisition_qty"]
            if data.get("requisition_qty") is not None
            else data["quantity"]
        )
        material_code = (data.get("material") or "").strip()
        flute_type = (data.get("flute_type") or "").strip()
        data["material_code"] = material_code
        data["flute_type"] = flute_type
        if material_code and flute_type:
            data["material_display"] = f"{material_code} / {flute_type}"
        else:
            data["material_display"] = material_code
        rows.append(data)
    rows = _decorate_rows_with_display_numbers(db, rows, registry)
    product_ids = {row["product_id"] for row in rows if row.get("product_id")}
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
        drawing = latest_drawings.get(row.get("product_id"))
        row["drawing_path"] = drawing.image_path if drawing else None
        row["drawing_is_pdf"] = bool(
            drawing and drawing.image_path.lower().endswith(".pdf")
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
        "material_received_at": item.material_received_at,
        "material_received_by": item.material_received_by,
    }


def _receive_material(
    db: Session,
    *,
    user: User,
    item_id: int,
    received_quantity: int | None,
) -> dict:
    received_at = _utc_now()
    current = db.get(OrderItem, item_id)
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
    db.execute(
        update(RequisitionItem)
        .where(
            RequisitionItem.order_item_id == item_id,
            RequisitionItem.status == "有效",
        )
        .values(requisition_qty=final_quantity)
    )
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
    return _item_response(db, item_id)


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
    item_id: int,
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
    seen: set[int] = set()
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


@router.put("/revert/{item_id}")
def revert_item(
    item_id: int,
    payload: RevertRequest,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    row = db.execute(
        select(OrderItem, Order)
        .join(Order, Order.id == OrderItem.order_id)
        .where(OrderItem.id == item_id)
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
                OrderItem.id == item_id,
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
            item_id=item_id,
            details={
                "reason": payload.reason,
                "previous_received_at": previous_received_at,
                "previous_received_by": previous_received_by,
            },
        )
        db.commit()
        return _item_response(db, item_id)
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise
