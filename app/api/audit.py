from __future__ import annotations

import json
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import String, cast, func, or_, select
from sqlalchemy.orm import Session

from app.api.deps import PermissionChecker, get_db
from app.core.time_contract import (
    beijing_date_bounds_utc_naive,
    utc_naive_to_api,
)
from app.models.audit import OperationLog
from app.models.user import User
from app.services.audit_log import append_audit_event, sanitize_audit_value


router = APIRouter()
can_view_audit = PermissionChecker("audit.view")

_SECURITY_ACTIONS = frozenset(
    {
        "LOGIN",
        "LOGIN_FAILED",
        "LOGIN_THROTTLED",
        "LOGOUT",
        "CHANGE_PASSWORD",
        "RESET_PASSWORD",
        "CHANGE_UI_MODE",
        "CREATE_USER",
        "UPDATE_USER",
        "UPDATE_PERMISSION_OVERRIDES",
        "UPDATE_CUSTOMER_SCOPES",
        "UPDATE_USER_ACCESS",
        "PERMISSION.DENIED",
        "ROLE.DENIED",
    }
)


def _like(value: str) -> str:
    return f"%{value.strip()}%"


def _legacy_category(row: OperationLog) -> str:
    return row.event_category or "legacy"


def _legacy_result(row: OperationLog) -> str:
    return row.result or "legacy"


def _parse_json(value: str | None) -> Any:
    if value is None:
        return None
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        parsed = value
    return sanitize_audit_value(parsed)


def _serialize_log(row: OperationLog) -> dict[str, Any]:
    operator_user_id = row.actor_user_id_snapshot
    if operator_user_id is None:
        operator_user_id = row.user_id
    operator_name = row.operator_name_snapshot
    if not operator_name:
        operator_name = row.username
    return {
        "id": row.id,
        "event_category": _legacy_category(row),
        "result": _legacy_result(row),
        "source": row.source or "legacy",
        "module_code": row.module_code or row.resource,
        "action_code": row.action_code or row.action,
        "resource": row.resource,
        "entity_type": row.entity_type,
        "entity_id": row.entity_id,
        "object_ref": row.object_ref,
        "customer_id": row.customer_id_snapshot,
        "customer_name": row.customer_name_snapshot,
        "request_id": row.request_id,
        "batch_id": row.batch_id,
        "operator": {
            "user_id": operator_user_id,
            "username": row.username,
            "name": operator_name,
            "role": row.role,
        },
        "description": row.description,
        "details": _parse_json(row.details),
        "extra": _parse_json(row.extra_json),
        "ip_address": row.ip_address,
        "user_agent": row.user_agent,
        "created_at": utc_naive_to_api(row.created_at),
        "schema_version": row.schema_version,
    }


def _category_condition(category: str):
    normalized = category.strip().lower()
    if normalized == "security":
        return or_(
            OperationLog.event_category.in_(("security", "audit_access")),
            (
                OperationLog.event_category.is_(None)
                & func.upper(OperationLog.action).in_(_SECURITY_ACTIONS)
            ),
        )
    if normalized == "business":
        return or_(
            OperationLog.event_category == "business",
            (
                OperationLog.event_category.is_(None)
                & ~func.upper(OperationLog.action).in_(_SECURITY_ACTIONS)
            ),
        )
    return OperationLog.event_category == normalized


def _build_filters(
    *,
    event_category: str | None,
    date_from: date | None,
    date_to: date | None,
    operator: str | None,
    module: str | None,
    action: str | None,
    object_ref: str | None,
    customer: str | None,
    result: str | None,
    source: str | None,
) -> tuple[list[Any], dict[str, Any]]:
    conditions: list[Any] = []
    safe_filters: dict[str, Any] = {}
    if event_category and event_category.strip():
        safe_filters["event_category"] = event_category.strip().lower()
        conditions.append(_category_condition(event_category))
    if date_from is not None:
        safe_filters["date_from"] = date_from.isoformat()
        start_at, _ = beijing_date_bounds_utc_naive(date_from)
        conditions.append(OperationLog.created_at >= start_at)
    if date_to is not None:
        if date_from is not None and date_to < date_from:
            raise HTTPException(status_code=400, detail="结束日期不能早于开始日期")
        safe_filters["date_to"] = date_to.isoformat()
        _, end_at = beijing_date_bounds_utc_naive(date_to)
        conditions.append(OperationLog.created_at < end_at)
    if operator and operator.strip():
        safe_filters["operator"] = operator.strip()
        pattern = _like(operator)
        conditions.append(
            or_(
                OperationLog.operator_name_snapshot.ilike(pattern),
                OperationLog.username.ilike(pattern),
                cast(OperationLog.actor_user_id_snapshot, String).ilike(pattern),
                cast(OperationLog.user_id, String).ilike(pattern),
            )
        )
    if module and module.strip():
        safe_filters["module"] = module.strip()
        pattern = _like(module)
        conditions.append(
            or_(
                OperationLog.module_code.ilike(pattern),
                OperationLog.resource.ilike(pattern),
            )
        )
    if action and action.strip():
        safe_filters["action"] = action.strip()
        pattern = _like(action)
        conditions.append(
            or_(
                OperationLog.action_code.ilike(pattern),
                OperationLog.action.ilike(pattern),
                OperationLog.description.ilike(pattern),
            )
        )
    if object_ref and object_ref.strip():
        safe_filters["object_ref"] = object_ref.strip()
        pattern = _like(object_ref)
        conditions.append(
            or_(
                OperationLog.object_ref.ilike(pattern),
                cast(OperationLog.entity_id, String).ilike(pattern),
                OperationLog.details.ilike(pattern),
            )
        )
    if customer and customer.strip():
        safe_filters["customer"] = customer.strip()
        pattern = _like(customer)
        conditions.append(
            or_(
                OperationLog.customer_name_snapshot.ilike(pattern),
                cast(OperationLog.customer_id_snapshot, String).ilike(pattern),
                OperationLog.details.ilike(pattern),
            )
        )
    if result and result.strip():
        normalized = result.strip().lower()
        safe_filters["result"] = normalized
        if normalized == "legacy":
            conditions.append(
                or_(
                    OperationLog.result.is_(None),
                    OperationLog.result == "legacy",
                )
            )
        else:
            conditions.append(OperationLog.result == normalized)
    if source and source.strip():
        normalized = source.strip().lower()
        safe_filters["source"] = normalized
        conditions.append(
            or_(
                OperationLog.source.is_(None),
                OperationLog.source == "legacy",
            )
            if normalized == "legacy"
            else OperationLog.source == normalized
        )
    return conditions, safe_filters


def _commit_audit_read(
    db: Session,
    *,
    request: Request,
    admin: User,
    action_code: str,
    description: str,
    details: dict[str, Any],
    object_ref: str | None = None,
) -> None:
    try:
        append_audit_event(
            db,
            request=request,
            actor=admin,
            event_category="audit_access",
            result="success",
            source="web",
            module_code="audit",
            action_code=action_code,
            resource="OperationLog",
            entity_type="operation_log" if object_ref else "audit_query",
            object_ref=object_ref,
            description=description,
            details=details,
        )
        db.commit()
    except Exception as error:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="审计留痕失败，未返回查询结果",
        ) from error


@router.get("/logs")
def list_audit_logs(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    event_category: str | None = Query(None),
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    operator: str | None = Query(None, max_length=100),
    module: str | None = Query(None, max_length=100),
    action: str | None = Query(None, max_length=100),
    object_ref: str | None = Query(None, max_length=100),
    customer: str | None = Query(None, max_length=100),
    result: str | None = Query(None, max_length=30),
    source: str | None = Query(None, max_length=30),
    admin: User = Depends(can_view_audit),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    conditions, safe_filters = _build_filters(
        event_category=event_category,
        date_from=date_from,
        date_to=date_to,
        operator=operator,
        module=module,
        action=action,
        object_ref=object_ref,
        customer=customer,
        result=result,
        source=source,
    )
    total = db.scalar(select(func.count(OperationLog.id)).where(*conditions)) or 0
    rows = db.scalars(
        select(OperationLog)
        .where(*conditions)
        .order_by(OperationLog.created_at.desc(), OperationLog.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    _commit_audit_read(
        db,
        request=request,
        admin=admin,
        action_code="audit.list.view",
        description="查看审计日志列表",
        details={
            "filters": {
                **safe_filters,
                "page": page,
                "page_size": page_size,
            },
            "result_count": total,
        },
    )
    return {
        "items": [_serialize_log(row) for row in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": (total + page_size - 1) // page_size,
    }


@router.get("/logs/{log_id}")
def get_audit_log(
    log_id: int,
    request: Request,
    admin: User = Depends(can_view_audit),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    row = db.get(OperationLog, log_id)
    if row is None:
        raise HTTPException(status_code=404, detail="审计日志不存在")
    payload = _serialize_log(row)
    _commit_audit_read(
        db,
        request=request,
        admin=admin,
        action_code="audit.detail.view",
        description="查看审计日志详情",
        object_ref=str(log_id),
        details={"target_log_id": log_id},
    )
    return payload
