from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import (
    PermissionChecker,
    customer_scope_ids,
    get_db,
    has_permission,
    has_unrestricted_customer_access,
    require_customer_access,
)
from app.models.order import Order, OrderItem
from app.models.production import ProductionCompletion, ProductionTask
from app.models.user import User
from app.services.production_workflow import (
    COMPLETED,
    NOT_REQUIRED,
    PENDING,
    WAITING_MATERIAL,
    CompletionCommand,
    ProductionWorkflowError,
    StockTransferCommand,
    batch_customer_ids,
    complete_production_batch,
    completion_customer_id,
    list_production_completions,
    list_production_tasks,
    list_temporary_locations,
    transfer_direct_completion_to_stock,
)
from app.services.warehouse_inventory import WarehouseInventoryError


router = APIRouter()
can_read = PermissionChecker("orders.view")
can_complete = PermissionChecker("orders.status")


class CompletionBatchItem(BaseModel):
    task_id: int = Field(gt=0)
    expected_version: int = Field(gt=0)
    disposition: Literal["direct", "stock"]
    location_id: int | None = Field(default=None, gt=0)
    pallet_id: int | None = Field(default=None, gt=0)
    pallet_code: str | None = Field(default=None, max_length=100)
    remarks: str | None = Field(default=None, max_length=1000)

    @field_validator("pallet_code", "remarks")
    @classmethod
    def trim_optional_text(cls, value: str | None) -> str | None:
        normalized = (value or "").strip()
        return normalized or None

    @model_validator(mode="after")
    def validate_disposition_target(self):
        if self.disposition == "stock" and self.location_id is None:
            raise ValueError("库存完工必须选择三楼临放位")
        if self.disposition == "direct" and any(
            value is not None
            for value in (self.location_id, self.pallet_id, self.pallet_code)
        ):
            raise ValueError("直接送货完工不能填写库存货位或栈板")
        return self


class CompletionBatchRequest(BaseModel):
    idempotency_key: str = Field(min_length=1, max_length=120)
    items: list[CompletionBatchItem] = Field(min_length=1)

    @field_validator("idempotency_key")
    @classmethod
    def trim_key(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("幂等键不能为空")
        return normalized

    @model_validator(mode="after")
    def validate_unique_tasks(self):
        task_ids = [item.task_id for item in self.items]
        if len(set(task_ids)) != len(task_ids):
            raise ValueError("同一生产任务不能在批次中重复提交")
        return self


class StockTransferRequest(BaseModel):
    idempotency_key: str = Field(min_length=1, max_length=120)
    location_id: int = Field(gt=0)
    pallet_id: int | None = Field(default=None, gt=0)
    pallet_code: str | None = Field(default=None, max_length=100)
    remarks: str | None = Field(default=None, max_length=1000)

    @field_validator("idempotency_key")
    @classmethod
    def trim_key(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("幂等键不能为空")
        return normalized

    @field_validator("pallet_code", "remarks")
    @classmethod
    def trim_optional_text(cls, value: str | None) -> str | None:
        normalized = (value or "").strip()
        return normalized or None


def _allowed_customer_ids(user: User, db: Session) -> set[int] | None:
    if has_unrestricted_customer_access(user, db):
        return None
    return customer_scope_ids(user, db)


def _raise_workflow_error(error: Exception) -> None:
    status_code = getattr(error, "status_code", 400)
    raise HTTPException(status_code=status_code, detail=str(error)) from error


def _require_task_customer_access(
    db: Session,
    *,
    task_ids: list[int],
    user: User,
) -> None:
    rows = db.execute(
        select(ProductionTask.id, Order.customer_id)
        .join(OrderItem, OrderItem.id == ProductionTask.order_item_id)
        .join(Order, Order.id == OrderItem.order_id)
        .where(ProductionTask.id.in_(task_ids))
    ).all()
    for _task_id, customer_id in rows:
        require_customer_access(customer_id, current_user=user, db=db)


@router.get("/tasks")
def get_production_tasks(
    task_status: Literal[
        "waiting_material", "pending", "completed", "not_required"
    ]
    | None = Query(default=None, alias="status"),
    user: User = Depends(can_read),
    db: Session = Depends(get_db),
) -> dict:
    return {
        "items": list_production_tasks(
            db,
            allowed_customer_ids=_allowed_customer_ids(user, db),
            status=task_status,
        )
    }


@router.get("/completions")
def get_production_completions(
    user: User = Depends(can_read),
    db: Session = Depends(get_db),
) -> dict:
    return {
        "items": list_production_completions(
            db,
            allowed_customer_ids=_allowed_customer_ids(user, db),
        )
    }


@router.get("/temporary-locations")
def get_temporary_locations(
    _user: User = Depends(can_read),
    db: Session = Depends(get_db),
) -> dict:
    return {"items": list_temporary_locations(db)}


@router.post("/completion-batches")
def post_completion_batch(
    payload: CompletionBatchRequest,
    user: User = Depends(can_complete),
    db: Session = Depends(get_db),
) -> dict:
    task_ids = [item.task_id for item in payload.items]
    try:
        _require_task_customer_access(db, task_ids=task_ids, user=user)
        if any(item.disposition == "stock" for item in payload.items) and not has_permission(
            user, "warehouse.execute"
        ):
            raise HTTPException(status_code=403, detail="库存完工需要仓库执行权限")
        result = complete_production_batch(
            db,
            idempotency_key=payload.idempotency_key,
            commands=[
                CompletionCommand(
                    task_id=item.task_id,
                    expected_version=item.expected_version,
                    disposition=item.disposition,
                    location_id=item.location_id,
                    pallet_id=item.pallet_id,
                    pallet_code=item.pallet_code,
                    remarks=item.remarks,
                )
                for item in payload.items
            ],
            operator_id=user.id,
        )
        # Replays are still scoped by the actual persisted completion facts.
        for customer_id in batch_customer_ids(db, result.completions):
            require_customer_access(customer_id, current_user=user, db=db)
        completion_ids = [row.id for row in result.completions]
        db.commit()
        items = list_production_completions(
            db,
            allowed_customer_ids=_allowed_customer_ids(user, db),
            completion_ids=completion_ids,
        )
        by_id = {row["id"]: row for row in items}
        return {
            "batch_id": result.batch.id,
            "idempotency_key": result.batch.idempotency_key,
            "request_hash": result.batch.request_hash,
            "item_count": result.batch.item_count,
            "replayed": result.replayed,
            "items": [by_id[row.id] for row in result.completions],
        }
    except HTTPException:
        db.rollback()
        raise
    except (ProductionWorkflowError, WarehouseInventoryError) as error:
        db.rollback()
        _raise_workflow_error(error)
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="完工批次已被其他请求修改，请刷新后重试",
        ) from error


@router.post("/completions/{completion_id}/stock-transfers")
def post_completion_stock_transfer(
    completion_id: int,
    payload: StockTransferRequest,
    user: User = Depends(can_complete),
    db: Session = Depends(get_db),
) -> dict:
    try:
        customer_id = completion_customer_id(db, completion_id)
        if customer_id is not None:
            require_customer_access(customer_id, current_user=user, db=db)
        if not has_permission(user, "warehouse.execute"):
            raise HTTPException(status_code=403, detail="转库存需要仓库执行权限")
        result = transfer_direct_completion_to_stock(
            db,
            completion_id=completion_id,
            command=StockTransferCommand(
                location_id=payload.location_id,
                idempotency_key=payload.idempotency_key,
                pallet_id=payload.pallet_id,
                pallet_code=payload.pallet_code,
                remarks=payload.remarks,
            ),
            operator_id=user.id,
        )
        db.commit()
        rows = list_production_completions(
            db,
            allowed_customer_ids=_allowed_customer_ids(user, db),
            completion_ids=[completion_id],
        )
        if not rows:
            raise HTTPException(status_code=404, detail="生产完工记录不存在")
        return {
            "transfer_id": result.transfer.id,
            "replayed": result.replayed,
            "completion": rows[0],
        }
    except HTTPException:
        db.rollback()
        raise
    except (ProductionWorkflowError, WarehouseInventoryError) as error:
        db.rollback()
        _raise_workflow_error(error)
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="转库存记录已被其他请求修改，请刷新后重试",
        ) from error
