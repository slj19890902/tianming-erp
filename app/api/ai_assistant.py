from __future__ import annotations

from datetime import date
from decimal import Decimal
import json
from time import perf_counter
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import (
    PermissionChecker,
    customer_scope_ids,
    get_db,
    has_permission,
    has_unrestricted_customer_access,
)
from app.core.time_contract import (
    beijing_today,
    utc_naive_to_api,
    utc_now_naive,
)
from app.models.ai_assistant import (
    AiAnalysisFeedback,
    AiAnalysisRun,
    AiUsageLedger,
)
from app.models.audit import OperationLog
from app.models.user import User
from app.services.ai.inventory_assistant import (
    ALLOWED_FOCUS,
    PROMPT_VERSION,
    InventoryAssistantError,
    build_inventory_snapshot,
    generate_inventory_interpretation,
    inventory_snapshot_hash,
)
from app.services.ai.providers import (
    ProviderUnavailable,
    inventory_provider_status,
    resolve_inventory_provider,
)
from app.services.inventory_insights import build_inventory_insights


router = APIRouter()
can_view_ai_inventory = PermissionChecker("ai.inventory.view")
can_view_ai_usage = PermissionChecker("ai.usage.view")

InventoryFocus = Literal[
    "all",
    "aged_inventory",
    "demand_coverage",
    "cost_missing",
    "data_quality",
]
FeedbackRating = Literal["helpful", "inaccurate"]
FeedbackReason = Literal[
    "useful_actionable",
    "accurate",
    "wrong_data",
    "missing_context",
    "hard_to_understand",
    "other",
]
_RUN_STATUS_MESSAGES = {
    "running": "库存经营解读正在生成。",
    "success": "库存经营解读已生成，所有动作仍需在原业务页面人工确认。",
    "degraded": "AI 经营解读尚未启用，原库存经营看板仍可正常使用。",
    "failed": "AI 经营解读本次未完成，原库存经营看板仍可正常使用。",
}


class CreateInventoryInsightRunRequest(BaseModel):
    as_of: date | None = None
    focus: InventoryFocus = "all"
    idempotency_key: str = Field(min_length=1, max_length=120)

    @field_validator("idempotency_key")
    @classmethod
    def normalize_idempotency_key(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("幂等键不能为空")
        return normalized


class InventoryInsightFeedbackRequest(BaseModel):
    rating: FeedbackRating
    reason_code: FeedbackReason | None = None
    remark: str | None = Field(default=None, max_length=500)

    @field_validator("remark")
    @classmethod
    def normalize_remark(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None


def _visible_customer_ids(user: User, db: Session) -> set[int] | None:
    if has_unrestricted_customer_access(user, db):
        return None
    return customer_scope_ids(user, db)


def _require_inventory_permissions(user: User) -> None:
    if not has_permission(user, "warehouse.view"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="需要库存查看权限才能使用 AI 库存解读",
        )


def _scope_snapshot(
    user: User,
    db: Session,
    *,
    focus: str,
    include_cost: bool,
) -> tuple[dict[str, object], set[int] | None]:
    customer_ids = _visible_customer_ids(user, db)
    scope = {
        "customer_access_mode": "all" if customer_ids is None else "selected",
        "customer_ids": [] if customer_ids is None else sorted(customer_ids),
        "contains_cost_data": include_cost,
        "focus": focus,
    }
    return scope, customer_ids


def _safe_json_loads(value: str | None) -> dict | list | None:
    if value is None:
        return None
    parsed = json.loads(value)
    if isinstance(parsed, (dict, list)):
        return parsed
    raise ValueError("stored AI JSON must be an object or list")


def _feedback_payload(feedback: AiAnalysisFeedback | None) -> dict | None:
    if feedback is None:
        return None
    return {
        "rating": feedback.rating,
        "reason_code": feedback.reason_code,
        "remark": feedback.remark,
        "created_at": utc_naive_to_api(feedback.created_at),
        "updated_at": (
            utc_naive_to_api(feedback.updated_at)
            if feedback.updated_at is not None
            else None
        ),
    }


def _run_payload(
    db: Session,
    run: AiAnalysisRun,
    *,
    viewer: User,
) -> dict[str, object]:
    scope = _safe_json_loads(run.scope_json)
    output = _safe_json_loads(run.output_json)
    snapshot = _safe_json_loads(run.sanitized_snapshot_json)
    feedback = db.scalar(
        select(AiAnalysisFeedback).where(
            AiAnalysisFeedback.run_id == run.id,
            AiAnalysisFeedback.user_id == run.requested_by,
        )
    )
    result: dict[str, object] = {
        "public_id": run.public_id,
        "analysis_type": run.analysis_type,
        "status": run.status,
        "message": _RUN_STATUS_MESSAGES[run.status],
        "as_of": run.as_of_date.isoformat(),
        "focus": scope.get("focus") if isinstance(scope, dict) else None,
        "provider_code": run.provider_code,
        "model_code": run.model_code,
        "prompt_version": run.prompt_version,
        "input_hash": run.input_hash,
        "snapshot": snapshot,
        "interpretation": output,
        "error_code": run.error_code,
        "latency_ms": run.latency_ms,
        "created_at": utc_naive_to_api(run.created_at),
        "completed_at": (
            utc_naive_to_api(run.completed_at)
            if run.completed_at is not None
            else None
        ),
        "feedback": _feedback_payload(feedback),
    }
    if has_permission(viewer, "ai.usage.view"):
        result["usage"] = {
            "input_tokens": run.input_tokens,
            "output_tokens": run.output_tokens,
            "provider_cost_estimate": str(run.provider_cost_estimate),
        }
    return result


def _ensure_run_visible(
    db: Session,
    run: AiAnalysisRun | None,
    *,
    user: User,
) -> AiAnalysisRun:
    if run is None:
        raise HTTPException(status_code=404, detail="AI 解读记录不存在")
    if run.requested_by != user.id:
        if not has_permission(user, "ai.usage.view"):
            raise HTTPException(status_code=404, detail="AI 解读记录不存在")
        return run

    scope = _safe_json_loads(run.scope_json)
    if not isinstance(scope, dict):
        raise HTTPException(status_code=409, detail="AI 解读权限快照无效")
    current_scope, _ = _scope_snapshot(
        user,
        db,
        focus=str(scope.get("focus") or "all"),
        include_cost=has_permission(user, "cost.view"),
    )
    if scope != current_scope:
        raise HTTPException(
            status_code=403,
            detail="当前账号的数据权限已变化，请重新生成解读",
        )
    return run


def _add_run_operation_log(
    db: Session,
    *,
    user: User,
    run: AiAnalysisRun,
) -> None:
    db.add(
        OperationLog(
            user_id=user.id,
            action="AI_INVENTORY_RUN",
            resource="AiAnalysisRun",
            details=json.dumps(
                {
                    "public_id": run.public_id,
                    "status": run.status,
                    "provider_code": run.provider_code,
                    "input_hash": run.input_hash,
                },
                ensure_ascii=False,
            ),
            username=user.username,
            role=user.role,
            entity_type="ai_analysis_run",
            entity_id=run.id,
            description="生成只读库存经营解读",
        )
    )


def _existing_idempotent_run(
    db: Session,
    *,
    user_id: int,
    idempotency_key: str,
) -> AiAnalysisRun | None:
    return db.scalar(
        select(AiAnalysisRun).where(
            AiAnalysisRun.requested_by == user_id,
            AiAnalysisRun.idempotency_key == idempotency_key,
        )
    )


@router.post("/inventory-insights/runs")
def create_inventory_insight_run(
    payload: CreateInventoryInsightRunRequest,
    db: Session = Depends(get_db),
    user: User = Depends(can_view_ai_inventory),
) -> dict[str, object]:
    _require_inventory_permissions(user)
    if payload.focus not in ALLOWED_FOCUS:
        raise HTTPException(status_code=422, detail="库存分析关注方向无效")

    as_of = payload.as_of or beijing_today()
    include_cost = has_permission(user, "cost.view")
    scope, visible_customer_ids = _scope_snapshot(
        user,
        db,
        focus=payload.focus,
        include_cost=include_cost,
    )
    insights = build_inventory_insights(
        db,
        as_of=as_of,
        customer_ids=visible_customer_ids,
    )
    try:
        snapshot = build_inventory_snapshot(
            insights,
            focus=payload.focus,
            include_cost=include_cost,
        )
    except InventoryAssistantError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    input_hash = inventory_snapshot_hash(snapshot)
    scope_json = json.dumps(
        scope,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )

    existing = _existing_idempotent_run(
        db,
        user_id=user.id,
        idempotency_key=payload.idempotency_key,
    )
    if existing is not None:
        if (
            existing.input_hash != input_hash
            or existing.scope_json != scope_json
            or existing.as_of_date != as_of
        ):
            raise HTTPException(
                status_code=409,
                detail="该幂等键已用于不同的库存快照，请刷新后重新操作",
            )
        return _run_payload(db, existing, viewer=user)

    provider_status = inventory_provider_status()
    run = AiAnalysisRun(
        public_id=str(uuid4()),
        analysis_type="inventory_insight",
        status="running",
        requested_by=user.id,
        as_of_date=as_of,
        scope_json=scope_json,
        idempotency_key=payload.idempotency_key,
        prompt_version=PROMPT_VERSION,
        provider_code=str(provider_status["provider_code"]),
        model_code=str(provider_status["model_code"]),
        input_hash=input_hash,
        sanitized_snapshot_json=json.dumps(
            snapshot,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        input_tokens=0,
        output_tokens=0,
        provider_cost_estimate=Decimal("0"),
    )
    db.add(run)
    try:
        db.flush()
    except IntegrityError as error:
        db.rollback()
        existing = _existing_idempotent_run(
            db,
            user_id=user.id,
            idempotency_key=payload.idempotency_key,
        )
        if (
            existing is not None
            and existing.input_hash == input_hash
            and existing.scope_json == scope_json
            and existing.as_of_date == as_of
        ):
            return _run_payload(db, existing, viewer=user)
        raise HTTPException(
            status_code=409,
            detail="AI 解读请求发生并发冲突，请刷新后重试",
        ) from error

    started = perf_counter()
    request_count = 0
    try:
        provider = resolve_inventory_provider()
        result = generate_inventory_interpretation(
            snapshot,
            provider=provider,
        )
        request_count = 1
        run.status = "success"
        run.provider_code = str(result["provider_code"])
        run.model_code = str(result["model_code"])
        run.output_json = json.dumps(
            result["interpretation"],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        run.input_tokens = int(result["input_tokens"])
        run.output_tokens = int(result["output_tokens"])
    except ProviderUnavailable as error:
        run.status = "degraded"
        run.error_code = error.code
    except InventoryAssistantError:
        request_count = 1
        run.status = "failed"
        run.error_code = "ai_output_validation_failed"
    except Exception:
        request_count = 1
        run.status = "failed"
        run.error_code = "ai_provider_failed"

    run.latency_ms = max(0, round((perf_counter() - started) * 1000))
    run.completed_at = utc_now_naive()
    db.add(
        AiUsageLedger(
            run_id=run.id,
            feature_code="ai.inventory.insight",
            provider_code=run.provider_code,
            model_code=run.model_code,
            request_count=request_count,
            input_tokens=run.input_tokens,
            output_tokens=run.output_tokens,
            billable_units=Decimal("0"),
            provider_cost_estimate=Decimal("0"),
        )
    )
    _add_run_operation_log(db, user=user, run=run)
    db.commit()
    db.refresh(run)
    return _run_payload(db, run, viewer=user)


@router.get("/inventory-insights/runs/{public_id}")
def get_inventory_insight_run(
    public_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(can_view_ai_inventory),
) -> dict[str, object]:
    _require_inventory_permissions(user)
    run = _ensure_run_visible(
        db,
        db.scalar(
            select(AiAnalysisRun).where(AiAnalysisRun.public_id == public_id)
        ),
        user=user,
    )
    return _run_payload(db, run, viewer=user)


@router.post("/inventory-insights/runs/{public_id}/feedback")
def save_inventory_insight_feedback(
    public_id: str,
    payload: InventoryInsightFeedbackRequest,
    db: Session = Depends(get_db),
    user: User = Depends(can_view_ai_inventory),
) -> dict[str, object]:
    _require_inventory_permissions(user)
    run = _ensure_run_visible(
        db,
        db.scalar(
            select(AiAnalysisRun).where(AiAnalysisRun.public_id == public_id)
        ),
        user=user,
    )
    if run.requested_by != user.id:
        raise HTTPException(status_code=404, detail="AI 解读记录不存在")

    feedback = db.scalar(
        select(AiAnalysisFeedback).where(
            AiAnalysisFeedback.run_id == run.id,
            AiAnalysisFeedback.user_id == user.id,
        )
    )
    action = "AI_INVENTORY_FEEDBACK_CREATE"
    if feedback is None:
        feedback = AiAnalysisFeedback(
            run_id=run.id,
            user_id=user.id,
            rating=payload.rating,
            reason_code=payload.reason_code,
            remark=payload.remark,
        )
        db.add(feedback)
    else:
        action = "AI_INVENTORY_FEEDBACK_UPDATE"
        feedback.rating = payload.rating
        feedback.reason_code = payload.reason_code
        feedback.remark = payload.remark
        feedback.updated_at = utc_now_naive()

    db.flush()
    db.add(
        OperationLog(
            user_id=user.id,
            action=action,
            resource="AiAnalysisFeedback",
            details=json.dumps(
                {
                    "run_public_id": run.public_id,
                    "rating": payload.rating,
                    "reason_code": payload.reason_code,
                },
                ensure_ascii=False,
            ),
            username=user.username,
            role=user.role,
            entity_type="ai_analysis_feedback",
            entity_id=feedback.id,
            description="记录库存经营解读反馈",
        )
    )
    db.commit()
    db.refresh(feedback)
    return {"ok": True, "feedback": _feedback_payload(feedback)}


@router.get("/usage/summary")
def get_ai_usage_summary(
    db: Session = Depends(get_db),
    user: User = Depends(can_view_ai_usage),
) -> dict[str, object]:
    del user
    totals = db.execute(
        select(
            func.count(AiUsageLedger.id),
            func.coalesce(func.sum(AiUsageLedger.request_count), 0),
            func.coalesce(func.sum(AiUsageLedger.input_tokens), 0),
            func.coalesce(func.sum(AiUsageLedger.output_tokens), 0),
            func.coalesce(func.sum(AiUsageLedger.billable_units), 0),
            func.coalesce(func.sum(AiUsageLedger.provider_cost_estimate), 0),
        )
    ).one()
    status_rows = db.execute(
        select(AiAnalysisRun.status, func.count(AiAnalysisRun.id))
        .group_by(AiAnalysisRun.status)
        .order_by(AiAnalysisRun.status)
    ).all()
    provider_rows = db.execute(
        select(
            AiUsageLedger.provider_code,
            AiUsageLedger.model_code,
            func.sum(AiUsageLedger.request_count),
        )
        .group_by(
            AiUsageLedger.provider_code,
            AiUsageLedger.model_code,
        )
        .order_by(
            AiUsageLedger.provider_code,
            AiUsageLedger.model_code,
        )
    ).all()
    return {
        "feature_code": "ai.inventory.insight",
        "ledger_rows": int(totals[0] or 0),
        "request_count": int(totals[1] or 0),
        "input_tokens": int(totals[2] or 0),
        "output_tokens": int(totals[3] or 0),
        "billable_units": str(totals[4] or 0),
        "provider_cost_estimate": str(totals[5] or 0),
        "runs_by_status": {
            run_status: int(count)
            for run_status, count in status_rows
        },
        "providers": [
            {
                "provider_code": provider_code,
                "model_code": model_code,
                "request_count": int(request_count or 0),
            }
            for provider_code, model_code, request_count in provider_rows
        ],
    }


@router.get("/status")
def get_ai_status(
    user: User = Depends(can_view_ai_inventory),
) -> dict[str, object]:
    _require_inventory_permissions(user)
    return {
        "feature_code": "ai.inventory.insight",
        **inventory_provider_status(),
    }
