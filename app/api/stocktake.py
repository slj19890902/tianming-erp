from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.api.deps import (
    PermissionChecker,
    get_db,
    has_unrestricted_customer_access,
)
from app.models.user import User
from app.services import stocktake as stocktake_service
from app.services.location_candidates import (
    load_warehouse_location_projection_contexts,
)


router = APIRouter()
logger = logging.getLogger(__name__)

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
    ) from error


def _raise_conflict(code: str, message: str, error: Exception) -> None:
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={"code": code, "message": message},
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


@router.post("/stocktakes", status_code=status.HTTP_201_CREATED)
def submit_stocktake(
    payload: StocktakeCreateRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_stocktake_submit),
) -> dict[str, object]:
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
                "盘点记录未保存，请刷新当前库位后重试；如仍出现请联系管理员",
                error,
            )
    order = stocktake_service.get_order(db, order.id)
    return _order_payload(db, order)


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
