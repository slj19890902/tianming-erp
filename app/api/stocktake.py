from __future__ import annotations

import logging
import hashlib
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.api.deps import (
    PermissionChecker,
    get_db,
    has_unrestricted_customer_access,
)
from app.models.user import User
from app.models.stocktake import StocktakeReview
from app.models.warehouse_inventory import InventoryMovement
from app.core.time_contract import utc_now_naive, utc_naive_to_api
from app.services import stocktake as stocktake_service
from app.services.location_candidates import (
    load_warehouse_location_projection_contexts,
)


router = APIRouter()
logger = logging.getLogger(__name__)
_NO_STORE = {"Cache-Control": "private, no-store"}
_PRESERVE = {**_NO_STORE, "X-Stocktake-Preserve": "1"}
_CONFIRM_REASON = "管理员手机确认实盘数量"

_view_permission = PermissionChecker("warehouse.stocktake.view")
_submit_permission = PermissionChecker("warehouse.stocktake.submit")
_review_permission = PermissionChecker("warehouse.stocktake.review")


def _order_payload(db: Session, order) -> dict[str, object]:
    context = load_warehouse_location_projection_contexts(
        db, [order.location]
    ).get(int(order.location_id), {})
    return stocktake_service.order_payload(order, projection_context=context)


def _require_unrestricted(user: User, db: Session) -> User:
    if not has_unrestricted_customer_access(user, db):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="盘点要求可访问全部客户，受限客户范围账号不可使用",
        )
    return user


def require_stocktake_view(
    user: User = Depends(_view_permission),
    db: Session = Depends(get_db),
) -> User:
    return _require_unrestricted(user, db)


def require_stocktake_submit(
    user: User = Depends(_submit_permission),
    db: Session = Depends(get_db),
) -> User:
    return _require_unrestricted(user, db)


def require_stocktake_review(
    user: User = Depends(_review_permission),
    db: Session = Depends(get_db),
) -> User:
    return _require_unrestricted(user, db)


class StocktakeLineRequest(BaseModel):
    inventory_lot_id: int = Field(gt=0)
    counted_quantity: int = Field(ge=0)
    expected_version: int = Field(gt=0)
    expected_available: int = Field(ge=0)
    expected_reserved: int = Field(ge=0)
    client_line_id: str | None = Field(default=None, min_length=1, max_length=120)

    @field_validator("client_line_id")
    @classmethod
    def strip_client_line_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("client_line_id 不能为空")
        return stripped


class StocktakeCreateRequest(BaseModel):
    location_id: int = Field(gt=0)
    location_layout_version: int | None = Field(default=None, gt=0)
    location_address_version: int = Field(gt=0)
    location_position_status: str = Field(min_length=1, max_length=30)
    published_map_revision: str | None = Field(
        default=None, min_length=1, max_length=64
    )
    # An empty list is a real "checked empty" result for an empty formal
    # location.  The service still requires exact coverage of every live lot,
    # so it cannot be used to omit inventory from a non-empty location.
    items: list[StocktakeLineRequest]
    idempotency_key: str = Field(min_length=1, max_length=120)
    expected_actor_id: int | None = Field(default=None, gt=0, strict=True)

    @field_validator(
        "idempotency_key",
        "location_position_status",
        "published_map_revision",
    )
    @classmethod
    def strip_submission_identity(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("盘点提交身份字段不能为空")
        return stripped


class StocktakeResolveRequest(BaseModel):
    action: Literal["submit", "confirm"]
    body: StocktakeCreateRequest


def _require_expected_actor(payload: StocktakeCreateRequest, user: User) -> None:
    if payload.expected_actor_id is not None and payload.expected_actor_id != user.id:
        raise HTTPException(
            409,
            {"code": "STOCKTAKE_ACTOR_MISMATCH", "message": "账号已变化，请切回原账号核对；原盘点请求已保留"},
            headers={**_PRESERVE, "X-Stocktake-Actor-Mismatch": "1"},
        )


def _command_key(action: str, original_key: str) -> str:
    return ("mobile-confirm-" + hashlib.sha256(original_key.encode()).hexdigest()
            if action == "confirm" else original_key)


def _receipt(db: Session, order, *, action: str, key: str, user: User) -> dict:
    # Materialize every lazy field before commit; after a commit, a read error
    # must not make a completed stocktake look like a rejected write.
    return {**_order_payload(db, order), "request_action": action,
            "request_idempotency_key": key, "current_actor_id": user.id}


def _require_confirm_receipt(db: Session, order, key: str) -> None:
    """A submitted row alone is not proof of an atomic mobile confirmation."""
    def conflict():
        raise stocktake_service.StocktakeError(
            "原盘点确认的审核事实不一致，请保留原请求并联系管理员核对",
            409, "IDEMPOTENCY_CONFLICT",
        )
    review = db.scalar(select(StocktakeReview).where(StocktakeReview.idempotency_key == key))
    if (review is None or order.status != "approved" or review.order_id != order.id
            or review.action != "approve" or review.from_status != "submitted"
            or review.to_status != "approved" or review.reason != _CONFIRM_REASON
            or order.review_note != _CONFIRM_REASON
            or review.reviewed_by != order.reviewed_by or review.reviewed_at != order.reviewed_at):
        conflict()
    details = review.details_json
    adjustments = details.get("adjustments") if isinstance(details, dict) else None
    if not isinstance(adjustments, list) or len(adjustments) != len(order.items):
        conflict()
    items = {item.inventory_lot_id: item for item in order.items}
    rows = {}
    for adjustment in adjustments:
        if not isinstance(adjustment, dict):
            conflict()
        if any(type(adjustment.get(field)) is not int for field in (
            "inventory_lot_id", "before_available", "reserved", "counted_quantity", "after_available", "delta"
        )):
            conflict()
        lot_id = adjustment["inventory_lot_id"]
        item = items.get(lot_id)
        movement_id = adjustment.get("movement_id")
        if (item is None or lot_id in rows
                or min(adjustment["before_available"], adjustment["reserved"], adjustment["after_available"]) < 0
                or (movement_id is not None and type(movement_id) is not int)
                or adjustment["counted_quantity"] != item.counted_quantity
                or adjustment["after_available"] + adjustment["reserved"] != item.counted_quantity
                or adjustment["after_available"] - adjustment["before_available"] != adjustment["delta"]
                or adjustment.get("movement_id") != item.adjustment_movement_id
                or (adjustment["delta"] == 0) != (item.adjustment_movement_id is None)):
            conflict()
        rows[lot_id] = adjustment
    movement_ids = [item.adjustment_movement_id for item in order.items if item.adjustment_movement_id is not None]
    movements = {movement.id: movement for movement in db.scalars(
        select(InventoryMovement).where(InventoryMovement.id.in_(movement_ids))
    )} if movement_ids else {}
    for item in order.items:
        if item.adjustment_movement_id is None:
            continue
        movement = movements.get(item.adjustment_movement_id)
        row = rows[item.inventory_lot_id]
        if (movement is None or movement.movement_type != "adjust"
                or movement.inventory_lot_id != item.inventory_lot_id
                or movement.operator_id != review.reviewed_by or movement.unit != item.unit_snapshot
                or movement.idempotency_key != stocktake_service._movement_idempotency_key(key, item.inventory_lot_id)
                or movement.quantity != abs(row["delta"])
                or movement.before_available != row["before_available"]
                or movement.after_available != row["after_available"]
                or movement.before_reserved != row["reserved"] or movement.after_reserved != row["reserved"]):
            conflict()


class StocktakeApproveRequest(BaseModel):
    idempotency_key: str = Field(min_length=1, max_length=120)
    reason: str | None = Field(default=None, max_length=1000)

    @field_validator("idempotency_key")
    @classmethod
    def strip_idempotency_key(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("幂等键不能为空")
        return stripped

    @field_validator("reason")
    @classmethod
    def strip_optional_reason(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None


class StocktakeRejectRequest(BaseModel):
    idempotency_key: str = Field(min_length=1, max_length=120)
    reason: str | None = Field(default=None, max_length=1000)

    @field_validator("idempotency_key")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("不能为空")
        return stripped

    @field_validator("reason")
    @classmethod
    def strip_optional_reason(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None


def _raise_service_error(error: stocktake_service.StocktakeError) -> None:
    raise HTTPException(
        status_code=error.status_code,
        detail={"code": error.code, "message": str(error)},
        headers=_PRESERVE,
    ) from error


def _raise_conflict(code: str, message: str, error: Exception) -> None:
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={"code": code, "message": message},
        headers=_PRESERVE,
    ) from error


def _raise_sqlite_concurrency_error(error: OperationalError) -> None:
    rendered = str(error).lower()
    if any(
        marker in rendered
        for marker in (
            "database is locked",
            "database table is locked",
            "database schema is locked",
            "sqlite_busy",
            "sqlite_locked",
        )
    ):
        _raise_conflict(
            "STOCKTAKE_DRIFT",
            "库存已变化或正在处理，请稍后刷新后重试",
            error,
        )
    raise error


def _request_metadata(request: Request) -> tuple[str | None, str | None]:
    return (
        request.client.host if request.client else None,
        request.headers.get("user-agent"),
    )


@router.get("/stocktake/locations")
def get_stocktake_locations(
    db: Session = Depends(get_db),
    _user: User = Depends(require_stocktake_view),
) -> dict[str, object]:
    try:
        return {"items": stocktake_service.list_locations(db)}
    except OperationalError as error:
        _raise_sqlite_concurrency_error(error)


@router.get("/stocktake/locations/{location_id}")
def get_stocktake_location(
    location_id: int,
    db: Session = Depends(get_db),
    _user: User = Depends(require_stocktake_view),
) -> dict[str, object]:
    try:
        return stocktake_service.get_location_detail(db, location_id)
    except stocktake_service.StocktakeError as error:
        _raise_service_error(error)
    except OperationalError as error:
        _raise_sqlite_concurrency_error(error)


@router.post("/stocktakes/confirm", status_code=status.HTTP_201_CREATED)
def confirm_mobile_stocktake(
    payload: StocktakeCreateRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    user: User = Depends(require_stocktake_review),
    submitter: User = Depends(require_stocktake_submit),
) -> dict[str, object]:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可在手机确认盘点并即时更新库存")
    _require_expected_actor(payload, user)
    response.headers.update(_NO_STORE)
    # Separate command identity from submit-for-review; both writes share one transaction.
    command_key = _command_key("confirm", payload.idempotency_key)
    ip_address, user_agent = _request_metadata(request)
    try:
        order = stocktake_service.create_stocktake(
            db, location_id=payload.location_id, location_layout_version=payload.location_layout_version,
            location_address_version=payload.location_address_version, location_position_status=payload.location_position_status,
            published_map_revision=payload.published_map_revision, items=[item.model_dump() for item in payload.items],
            idempotency_key=command_key, submitter=user, ip_address=ip_address, user_agent=user_agent)
        order = stocktake_service.approve_stocktake(db, order_id=order.id,
            idempotency_key=command_key, reason=_CONFIRM_REASON, reviewer=user,
            ip_address=ip_address, user_agent=user_agent)
        db.flush()
        # Approval reads reviews to allocate its sequence, so that collection
        # can still cache the pre-approval empty list after the review is flushed.
        # Reload only the changed relationships before materializing the receipt.
        db.expire(order, ["reviews", "reviewer"])
        result = _receipt(db, stocktake_service.get_order(db, order.id),
                          action="confirm", key=payload.idempotency_key, user=user)
        db.commit()
        return result
    except stocktake_service.StocktakeError as error:
        db.rollback()
        _raise_service_error(error)
    except OperationalError as error:
        db.rollback()
        _raise_sqlite_concurrency_error(error)
    except IntegrityError as error:
        db.rollback()
        _raise_conflict("STOCKTAKE_SAVE_CONFLICT", "盘点确认冲突，请刷新核对后重试", error)
    except Exception:
        db.rollback()
        raise


@router.post("/stocktakes", status_code=status.HTTP_201_CREATED)
def submit_stocktake(
    payload: StocktakeCreateRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    user: User = Depends(require_stocktake_submit),
) -> dict[str, object]:
    _require_expected_actor(payload, user)
    response.headers.update(_NO_STORE)
    items = [item.model_dump() for item in payload.items]
    ip_address, user_agent = _request_metadata(request)
    try:
        order = stocktake_service.create_stocktake(
            db,
            location_id=payload.location_id,
            location_layout_version=payload.location_layout_version,
            location_address_version=payload.location_address_version,
            location_position_status=payload.location_position_status,
            published_map_revision=payload.published_map_revision,
            items=items,
            idempotency_key=payload.idempotency_key,
            submitter=user,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        db.flush()
        result = _receipt(db, stocktake_service.get_order(db, order.id),
                          action="submit", key=payload.idempotency_key, user=user)
        db.commit()
    except stocktake_service.StocktakeError as error:
        db.rollback()
        _raise_service_error(error)
    except OperationalError as error:
        db.rollback()
        _raise_sqlite_concurrency_error(error)
    except IntegrityError as error:
        db.rollback()
        try:
            order = stocktake_service.resolve_submission_replay(
                db,
                location_id=payload.location_id,
                location_layout_version=payload.location_layout_version,
                location_address_version=payload.location_address_version,
                location_position_status=payload.location_position_status,
                published_map_revision=payload.published_map_revision,
                items=items,
                idempotency_key=payload.idempotency_key,
            )
        except stocktake_service.StocktakeError as replay_error:
            _raise_service_error(replay_error)
        except OperationalError as replay_error:
            _raise_sqlite_concurrency_error(replay_error)
        if order is None:
            logger.exception(
                "stocktake submit integrity error without an idempotent replay",
                exc_info=error,
            )
            _raise_conflict(
                "STOCKTAKE_SAVE_CONFLICT",
                "盘点提交发生冲突，请保留原请求并核对；如仍出现请联系管理员",
                error,
            )
        with db.no_autoflush:
            result = _receipt(db, stocktake_service.get_order(db, order.id),
                              action="submit", key=payload.idempotency_key, user=user)
    except Exception:
        db.rollback()
        raise
    return result


@router.post("/stocktakes/resolve")
def resolve_mobile_stocktake(
    payload: StocktakeResolveRequest, request: Request, response: Response,
    db: Session = Depends(get_db), user: User = Depends(require_stocktake_submit),
) -> dict[str, object]:
    if payload.action == "confirm":
        _review_permission(request, current_user=user, db=db)
        if user.role != "admin":
            raise HTTPException(403, "仅管理员可在手机确认盘点并即时更新库存", headers=_PRESERVE)
    body = payload.body
    _require_expected_actor(body, user)
    response.headers.update(_NO_STORE)
    try:
        with db.no_autoflush:
            # Duplicate lines cannot be collapsed into a matching persisted
            # lot dictionary. Client line IDs and line order remain metadata.
            lot_ids = [item.inventory_lot_id for item in body.items]
            if len(lot_ids) != len(set(lot_ids)):
                raise stocktake_service.StocktakeError("原请求包含重复批次，请保留并核对", 409, "IDEMPOTENCY_CONFLICT")
            command_key = _command_key(payload.action, body.idempotency_key)
            order = stocktake_service.resolve_submission_replay(
                db, location_id=body.location_id, location_layout_version=body.location_layout_version,
                location_address_version=body.location_address_version,
                location_position_status=body.location_position_status,
                published_map_revision=body.published_map_revision,
                items=[item.model_dump() for item in body.items], idempotency_key=command_key,
            )
            if order is not None and payload.action == "confirm":
                _require_confirm_receipt(db, order, command_key)
            return {
                "status": "found" if order is not None else "not_found",
                "request_action": payload.action, "request_idempotency_key": body.idempotency_key,
                "current_actor_id": user.id, "observed_at": utc_naive_to_api(utc_now_naive()),
                "order": (_receipt(db, order, action=payload.action, key=body.idempotency_key, user=user)
                          if order is not None else None),
            }
    except stocktake_service.StocktakeError as error:
        _raise_service_error(error)
    except OperationalError as error:
        _raise_sqlite_concurrency_error(error)


@router.get("/stocktakes")
def get_stocktakes(
    stocktake_status: Literal["submitted", "approved", "rejected"] | None = Query(
        default=None,
        alias="status",
    ),
    db: Session = Depends(get_db),
    _user: User = Depends(require_stocktake_view),
) -> dict[str, object]:
    try:
        rows = stocktake_service.list_orders(db, status=stocktake_status)
        contexts = load_warehouse_location_projection_contexts(
            db, [row.location for row in rows]
        )
        return {
            "items": [
                stocktake_service.order_payload(
                    row,
                    projection_context=contexts.get(int(row.location_id)),
                )
                for row in rows
            ]
        }
    except OperationalError as error:
        _raise_sqlite_concurrency_error(error)


@router.get("/stocktakes/{order_id}")
def get_stocktake(
    order_id: int,
    db: Session = Depends(get_db),
    _user: User = Depends(require_stocktake_view),
) -> dict[str, object]:
    try:
        return _order_payload(db, stocktake_service.get_order(db, order_id))
    except stocktake_service.StocktakeError as error:
        _raise_service_error(error)
    except OperationalError as error:
        _raise_sqlite_concurrency_error(error)


def _review_response(
    *,
    request: Request,
    db: Session,
    user: User,
    order_id: int,
    action: Literal["approve", "reject"],
    idempotency_key: str,
    reason: str | None,
) -> dict[str, object]:
    ip_address, user_agent = _request_metadata(request)
    effective_reason = reason
    if action == "reject":
        effective_reason = (reason or "").strip() or "驳回库存盘点单（系统记录）"
    try:
        if action == "approve":
            order = stocktake_service.approve_stocktake(
                db,
                order_id=order_id,
                idempotency_key=idempotency_key,
                reason=effective_reason,
                reviewer=user,
                ip_address=ip_address,
                user_agent=user_agent,
            )
        else:
            order = stocktake_service.reject_stocktake(
                db,
                order_id=order_id,
                idempotency_key=idempotency_key,
                reason=effective_reason,
                reviewer=user,
                ip_address=ip_address,
                user_agent=user_agent,
            )
        db.commit()
    except stocktake_service.StocktakeError as error:
        db.rollback()
        _raise_service_error(error)
    except OperationalError as error:
        db.rollback()
        _raise_sqlite_concurrency_error(error)
    except IntegrityError as error:
        db.rollback()
        try:
            order = stocktake_service.resolve_review_replay(
                db,
                order_id=order_id,
                action=action,
                idempotency_key=idempotency_key,
                reason=effective_reason,
            )
        except stocktake_service.StocktakeError as replay_error:
            _raise_service_error(replay_error)
        except OperationalError as replay_error:
            _raise_sqlite_concurrency_error(replay_error)
        if order is None:
            _raise_conflict(
                "IDEMPOTENCY_CONFLICT",
                "盘点审核冲突，请刷新后重试",
                error,
            )
    order = stocktake_service.get_order(db, order.id)
    return _order_payload(db, order)


@router.post("/stocktakes/{order_id}/approve")
def approve_stocktake(
    order_id: int,
    payload: StocktakeApproveRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_stocktake_review),
) -> dict[str, object]:
    return _review_response(
        request=request,
        db=db,
        user=user,
        order_id=order_id,
        action="approve",
        idempotency_key=payload.idempotency_key,
        reason=payload.reason,
    )


@router.post("/stocktakes/{order_id}/reject")
def reject_stocktake(
    order_id: int,
    payload: StocktakeRejectRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_stocktake_review),
) -> dict[str, object]:
    return _review_response(
        request=request,
        db=db,
        user=user,
        order_id=order_id,
        action="reject",
        idempotency_key=payload.idempotency_key,
        reason=payload.reason,
    )
