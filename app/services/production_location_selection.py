from __future__ import annotations

from datetime import timedelta
from hashlib import sha256
import json
import secrets
from urllib.parse import urlencode

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.time_contract import utc_naive_to_api, utc_now_naive
from app.models.customer import Customer
from app.models.order import Order, OrderItem
from app.models.production import (
    ProductionCompletion,
    ProductionLocationSelectionSession,
    ProductionStockTransfer,
)
from app.models.warehouse_inventory import WarehouseLocation
from app.services.production_workflow import (
    MUTABLE_ORDER_STATUSES,
    ProductionWorkflowError,
    StockTransferCommand,
    StockTransferResult,
    list_temporary_locations,
    transfer_direct_completion_to_stock,
    validate_production_stock_destination,
)
from app.services.warehouse_location_address import employee_location_name


SELECTION_SESSION_MINUTES = 20


def _canonical_hash(payload: dict) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _completion_context(
    db: Session,
    completion_id: int,
) -> tuple[ProductionCompletion, OrderItem, Order, Customer]:
    row = db.execute(
        select(ProductionCompletion, OrderItem, Order, Customer)
        .join(OrderItem, OrderItem.id == ProductionCompletion.order_item_id)
        .join(Order, Order.id == OrderItem.order_id)
        .join(Customer, Customer.id == Order.customer_id)
        .where(ProductionCompletion.id == completion_id)
    ).one_or_none()
    if row is None:
        raise ProductionWorkflowError("生产完工记录不存在", 404)
    return row


def _assert_completion_can_open_selection(
    db: Session,
    *,
    completion: ProductionCompletion,
    item: OrderItem,
    order: Order,
) -> None:
    if completion.status != "posted":
        raise ProductionWorkflowError("生产完工记录已经撤销，不能再转入库存", 409)
    if completion.initial_disposition != "direct":
        raise ProductionWorkflowError("只有直接送货完工记录可以选择转库存位置", 409)
    if order.status not in MUTABLE_ORDER_STATUSES or item.is_force_closed:
        raise ProductionWorkflowError("订单或明细已结案，不能再转入库存", 409)
    if int(item.delivered_quantity or 0) != 0:
        raise ProductionWorkflowError("订单明细已产生送货数量，不能转入库存", 409)
    transfer_id = db.scalar(
        select(ProductionStockTransfer.id).where(
            ProductionStockTransfer.completion_id == completion.id,
            ProductionStockTransfer.status == "posted",
        )
    )
    if transfer_id is not None:
        raise ProductionWorkflowError("该完工记录已转入库存，不能重复操作", 409)


def create_location_selection_session(
    db: Session,
    *,
    completion_id: int,
    user_id: int,
    idempotency_key: str,
) -> tuple[ProductionLocationSelectionSession, bool]:
    key = idempotency_key.strip()
    if not key or len(key) > 120:
        raise ProductionWorkflowError("幂等键长度必须为1到120个字符")
    completion, item, order, customer = _completion_context(db, completion_id)
    request_hash = _canonical_hash(
        {
            "completion_id": completion_id,
            "customer_id": customer.id,
            "user_id": user_id,
        }
    )
    repeated = db.scalar(
        select(ProductionLocationSelectionSession).where(
            ProductionLocationSelectionSession.create_idempotency_key == key
        )
    )
    if repeated is not None:
        if repeated.create_request_hash != request_hash:
            raise ProductionWorkflowError("同一幂等键对应的地图选位任务不一致", 409)
        return repeated, True
    _assert_completion_can_open_selection(
        db,
        completion=completion,
        item=item,
        order=order,
    )
    now = utc_now_naive()
    session = ProductionLocationSelectionSession(
        token=secrets.token_urlsafe(36),
        completion_id=completion.id,
        user_id=user_id,
        customer_id=customer.id,
        create_idempotency_key=key,
        create_request_hash=request_hash,
        expires_at=now + timedelta(minutes=SELECTION_SESSION_MINUTES),
    )
    db.add(session)
    db.flush()
    return session, False


def load_location_selection_session(
    db: Session,
    *,
    token: str,
    user_id: int,
    allow_consumed: bool = True,
) -> ProductionLocationSelectionSession:
    session = db.scalar(
        select(ProductionLocationSelectionSession).where(
            ProductionLocationSelectionSession.token == token
        )
    )
    if session is None:
        raise ProductionWorkflowError("地图选位任务不存在或已失效", 404)
    if session.user_id != user_id:
        raise ProductionWorkflowError("该地图选位任务不属于当前账号", 403)
    if session.status == "consumed" and not allow_consumed:
        raise ProductionWorkflowError("该地图选位任务已经使用", 409)
    if session.status != "consumed" and session.expires_at <= utc_now_naive():
        raise ProductionWorkflowError("地图选位已超时，请返回生产页面重新打开", 410)
    return session


def selectable_production_locations(db: Session) -> list[dict]:
    return [
        row
        for row in list_temporary_locations(db)
        if row.get("layout_version") is not None and row.get("is_empty") is True
    ]


def select_location_for_session(
    db: Session,
    *,
    session: ProductionLocationSelectionSession,
    location_id: int,
    expected_layout_version: int,
) -> ProductionLocationSelectionSession:
    if session.status == "consumed":
        raise ProductionWorkflowError("该地图选位任务已经使用", 409)
    if session.status == "selected":
        if (
            session.selected_location_id == location_id
            and session.selected_layout_version == expected_layout_version
        ):
            return session
        raise ProductionWorkflowError("该选位任务已经确认位置，请返回生产页重新发起", 409)
    eligible = {
        int(row["id"]): row for row in selectable_production_locations(db)
    }
    candidate = eligible.get(location_id)
    if candidate is None:
        raise ProductionWorkflowError("该位置不是当前可用的成品库存位置", 409)
    validate_production_stock_destination(
        db,
        location_id=location_id,
        expected_layout_version=expected_layout_version,
    )
    if int(candidate["layout_version"]) != int(expected_layout_version):
        raise ProductionWorkflowError("目标库位地图状态已变化，请返回地图重新选择", 409)
    location = db.get(WarehouseLocation, location_id)
    if location is None:
        raise ProductionWorkflowError("成品库位不存在", 404)
    session.status = "selected"
    session.selected_location_id = location_id
    session.selected_layout_version = expected_layout_version
    session.selected_location_name_snapshot = employee_location_name(location)
    session.selected_at = utc_now_naive()
    session.version += 1
    db.flush()
    return session


def confirm_location_selection_transfer(
    db: Session,
    *,
    session: ProductionLocationSelectionSession,
    completion_id: int,
    idempotency_key: str,
    operator_id: int,
) -> StockTransferResult:
    key = idempotency_key.strip()
    if not key or len(key) > 120:
        raise ProductionWorkflowError("幂等键长度必须为1到120个字符")
    if session.completion_id != completion_id:
        raise ProductionWorkflowError("地图选位任务与生产完工记录不一致", 409)
    if session.status == "consumed":
        if session.confirm_idempotency_key != key or session.transfer_id is None:
            raise ProductionWorkflowError("该地图选位任务已经使用", 409)
        transfer = db.get(ProductionStockTransfer, session.transfer_id)
        if transfer is None:
            raise ProductionWorkflowError("已确认的转库存记录不存在", 409)
        return StockTransferResult(transfer, True)
    if session.expires_at <= utc_now_naive():
        raise ProductionWorkflowError("地图选位已超时，请返回生产页面重新打开", 410)
    if (
        session.status != "selected"
        or session.selected_location_id is None
        or session.selected_layout_version is None
    ):
        raise ProductionWorkflowError("请先在实测地图选择并确认具体位置", 409)
    validate_production_stock_destination(
        db,
        location_id=session.selected_location_id,
        expected_layout_version=session.selected_layout_version,
    )
    result = transfer_direct_completion_to_stock(
        db,
        completion_id=completion_id,
        command=StockTransferCommand(
            location_id=session.selected_location_id,
            expected_layout_version=session.selected_layout_version,
            idempotency_key=key,
        ),
        operator_id=operator_id,
    )
    session.status = "consumed"
    session.transfer_id = result.transfer.id
    session.confirm_idempotency_key = key
    session.consumed_at = utc_now_naive()
    session.version += 1
    db.flush()
    return result


def serialize_location_selection_session(
    db: Session,
    session: ProductionLocationSelectionSession,
    *,
    include_candidates: bool,
) -> dict:
    completion, item, order, customer = _completion_context(db, session.completion_id)
    selected = None
    if session.selected_location_id is not None:
        selected = {
            "id": session.selected_location_id,
            "layout_version": session.selected_layout_version,
            "location_name": session.selected_location_name_snapshot,
        }
    payload = {
        "token": session.token,
        "status": session.status,
        "completion_id": session.completion_id,
        "expires_at": utc_naive_to_api(session.expires_at),
        "map_url": "/warehouse.html?"
        + urlencode(
            {
                "view": "2d",
                "mode": "production-location-selection",
                "selection_token": session.token,
            }
        ),
        "completion": {
            "id": completion.id,
            "order_number": order.order_number,
            "customer_po": order.customer_po,
            "customer_id": customer.id,
            "customer_name": customer.name,
            "product_code": item.snapshot_product_code,
            "product_name": item.snapshot_product_name,
            "specification": item.snapshot_spec,
            "quantity": int(completion.quantity),
        },
        "selected_location": selected,
    }
    if include_candidates:
        payload["candidate_locations"] = selectable_production_locations(db)
    return payload
