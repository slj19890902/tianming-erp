from __future__ import annotations

import json
from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import (
    PermissionChecker,
    RoleChecker,
    customer_scope_ids,
    get_db,
    has_permission,
    has_unrestricted_customer_access,
    require_customer_access,
)
from app.models.customer import Customer
from app.models.order import Order, OrderItem
from app.models.production import ProductionCompletion, ProductionTask
from app.models.user import User
from app.models.warehouse_inventory import (
    InventoryLot,
    InventoryPallet,
    InventoryPalletItem,
)
from app.services.audit_log import append_audit_event
from app.services.production_workflow import (
    COMPLETED,
    NOT_REQUIRED,
    PENDING,
    WAITING_MATERIAL,
    CompletionCommand,
    ProductionWorkflowError,
    StockTransferCommand,
    adjust_production_completion_actual_quantity,
    batch_customer_ids,
    complete_production_batch,
    completion_customer_id,
    count_production_tasks,
    list_production_completions_page,
    list_production_completions,
    list_production_tasks,
    list_temporary_locations,
    reverse_production_completion,
    transfer_direct_completion_to_stock,
)
from app.services.production_label_operations import (
    ProductionLabelOperationError,
    annotate_task_label_plans,
    production_label_write_guard,
    refresh_task_label_plan,
)
from app.services.fulfillment_reminders import annotate_production_reminders
from app.services.warehouse_inventory import WarehouseInventoryError


router = APIRouter()
from app.api.drawing_adoption import router as drawing_adoption_router  # noqa: E402
router.include_router(drawing_adoption_router)
can_read = PermissionChecker("orders.view")
can_complete = PermissionChecker("orders.status")
admin_only = RoleChecker(["admin"])
actual_quantity_operator = RoleChecker(["admin", "boss"])


class CompletionBatchItem(BaseModel):
    task_id: int = Field(gt=0)
    expected_version: int = Field(gt=0)
    disposition: Literal["direct", "stock"]
    completion_type: Literal["primary", "supplemental"] = "primary"
    material_input_quantity: int | None = Field(default=None, gt=0)
    actual_output_quantity: int | None = Field(default=None, gt=0)
    defective_quantity: int | None = Field(default=None, ge=0)
    direct_delivery_quantity: int | None = Field(default=None, ge=0)
    location_id: int | None = Field(default=None, gt=0)
    expected_layout_version: int | None = Field(default=None, gt=0)
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
            raise ValueError("库存完工必须选择成品库位")
        if self.disposition == "direct" and self.location_id is None and any(
            value is not None for value in (self.pallet_id, self.pallet_code)
        ):
            raise ValueError("未选择库存库位时不能填写栈板")
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
    expected_layout_version: int | None = Field(default=None, gt=0)
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


class ActualQuantityAdjustmentRequest(BaseModel):
    actual_output_quantity: int = Field(gt=0)
    expected_task_version: int = Field(gt=0)
    expected_lot_version: int = Field(gt=0)
    idempotency_key: str = Field(min_length=1, max_length=100)

    @field_validator("idempotency_key")
    @classmethod
    def trim_idempotency_key(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("idempotency key cannot be blank")
        return normalized


class CompletionReversalRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


class LabelPlanRefreshRequest(BaseModel):
    idempotency_key: str = Field(min_length=1, max_length=120)
    expected_task_version: int = Field(gt=0)
    expected_product_version: int = Field(gt=0)
    confirmed_not_started: Literal[True]
    confirmed_no_prior_print: Literal[True]

    @field_validator("idempotency_key")
    @classmethod
    def trim_idempotency_key(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("幂等键不能为空")
        return normalized


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


def _completion_customer_snapshots(
    db: Session,
    completions: list[ProductionCompletion] | tuple[ProductionCompletion, ...],
) -> dict[int, tuple[int, str]]:
    """Read only the customer identifiers needed by production audit events."""

    completion_ids = [completion.id for completion in completions]
    if not completion_ids:
        return {}
    rows = db.execute(
        select(ProductionCompletion.id, Customer.id, Customer.name)
        .join(OrderItem, OrderItem.id == ProductionCompletion.order_item_id)
        .join(Order, Order.id == OrderItem.order_id)
        .join(Customer, Customer.id == Order.customer_id)
        .where(ProductionCompletion.id.in_(completion_ids))
    ).all()
    return {
        completion_id: (customer_id, customer_name)
        for completion_id, customer_id, customer_name in rows
    }


def _append_production_completion_audit(
    db: Session,
    *,
    user: User,
    completion: ProductionCompletion,
    customer_snapshot: tuple[int, str] | None,
) -> None:
    customer_id, customer_name = customer_snapshot or (None, None)
    is_supplemental = completion.completion_type == "supplemental"
    pallet_rows = list(
        db.execute(
            select(InventoryPallet, InventoryPalletItem, InventoryLot)
            .join(
                InventoryPalletItem,
                InventoryPalletItem.pallet_id == InventoryPallet.id,
            )
            .join(InventoryLot, InventoryLot.id == InventoryPalletItem.inventory_lot_id)
            .where(
                InventoryLot.source_ref_type == "production_completion",
                InventoryLot.source_ref_id == completion.id,
                InventoryPallet.is_current.is_(True),
            )
            .order_by(InventoryPallet.id, InventoryLot.id)
        )
    )
    pallet_ids = sorted({int(pallet.id) for pallet, _item, _lot in pallet_rows})
    pallet_codes = sorted({pallet.pallet_code for pallet, _item, _lot in pallet_rows})
    pallet_lot_ids = [int(lot.id) for _pallet, _item, lot in pallet_rows]
    pallet_quantity = sum(
        int(lot.quantity_available or 0)
        + int(lot.quantity_reserved or 0)
        + int(lot.quantity_damaged or 0)
        for _pallet, _item, lot in pallet_rows
    )
    append_audit_event(
        db,
        event_category="business",
        result="success",
        source="web",
        module_code="production",
        action_code="production.completion.posted",
        legacy_action=(
            "SUPPLEMENT_PRODUCTION_COMPLETE"
            if is_supplemental
            else "COMPLETE_PRODUCTION"
        ),
        resource="ProductionCompletion",
        actor=user,
        entity_type="production_completion",
        entity_id=completion.id,
        object_ref=f"production_completion:{completion.id}",
        customer_id=customer_id,
        customer_name=customer_name,
        batch_id=str(completion.batch_id),
        description=("管理员补充生产确认" if is_supplemental else "确认生产完工"),
        details={
            "batch_id": completion.batch_id,
            "task_id": completion.task_id,
            "completion_type": completion.completion_type,
            "initial_disposition": completion.initial_disposition,
            "material_input_quantity": completion.material_input_quantity,
            "planned_output_quantity": completion.planned_output_quantity,
            "actual_output_quantity": completion.actual_output_quantity,
            "defective_quantity": completion.defective_quantity,
            "order_reserved_quantity": completion.order_reserved_quantity,
            "direct_delivery_quantity": completion.direct_delivery_quantity,
            "stock_quantity": completion.stock_quantity,
            "surplus_finished_quantity": completion.surplus_finished_quantity,
            "inventory_lot_id": completion.inventory_lot_id,
            "warehouse_location_id": completion.warehouse_location_id,
            "system_pallet_id": pallet_ids[0] if len(pallet_ids) == 1 else None,
            "system_pallet_code": pallet_codes[0] if len(pallet_codes) == 1 else None,
            "system_pallet_ids": pallet_ids,
            "system_pallet_inventory_lot_ids": pallet_lot_ids,
            "system_pallet_quantity": pallet_quantity,
        },
    )


@router.get("/tasks")
def get_production_tasks(
    task_status: Literal[
        "waiting_material", "pending", "completed", "not_required"
    ]
    | None = Query(default=None, alias="status"),
    page: Annotated[int | None, Query(ge=1)] = None,
    page_size: Annotated[int | None, Query(ge=1, le=200)] = None,
    user: User = Depends(can_read),
    db: Session = Depends(get_db),
) -> dict:
    allowed_customer_ids = _allowed_customer_ids(user, db)
    if page is None and page_size is None:
        items = list_production_tasks(
                db,
                allowed_customer_ids=allowed_customer_ids,
                status=task_status,
            )
        return {
            "items": annotate_production_reminders(
                db,
                annotate_task_label_plans(db, items, hydrate=False),
            )
        }

    resolved_page = page or 1
    resolved_page_size = page_size or 25
    items = list_production_tasks(
            db,
            allowed_customer_ids=allowed_customer_ids,
            status=task_status,
            page=resolved_page,
            page_size=resolved_page_size,
        )
    return {
        "items": annotate_production_reminders(
            db,
            annotate_task_label_plans(db, items, hydrate=False),
        ),
        "total": count_production_tasks(
            db,
            allowed_customer_ids=allowed_customer_ids,
            status=task_status,
        ),
        "page": resolved_page,
        "page_size": resolved_page_size,
    }


@router.post("/tasks/{task_id}/label-plan-refresh")
def post_production_task_label_plan_refresh(
    task_id: int,
    payload: LabelPlanRefreshRequest,
    user: User = Depends(can_complete),
    _write_guard: None = Depends(production_label_write_guard),
    db: Session = Depends(get_db),
) -> dict:
    """Explicitly refresh one eligible task; never mutate production facts."""

    try:
        _require_task_customer_access(db, task_ids=[task_id], user=user)
        result = refresh_task_label_plan(
            db,
            task_id=task_id,
            expected_task_version=payload.expected_task_version,
            expected_product_version=payload.expected_product_version,
            idempotency_key=payload.idempotency_key,
            operator_id=user.id,
        )
        # A replay has already persisted its audit in the original transaction.
        if not result.replayed:
            order_row = db.execute(
                select(Order.customer_id, Customer.name)
                .join(OrderItem, OrderItem.order_id == Order.id)
                .join(Customer, Customer.id == Order.customer_id)
                .where(OrderItem.id == result.task.order_item_id)
            ).first()
            before = json.loads(result.receipt.before_snapshot_json)
            after = json.loads(result.receipt.after_snapshot_json)
            append_audit_event(
                db,
                event_category="business",
                result="success",
                source="web",
                module_code="production",
                action_code="production.label_plan.refreshed",
                legacy_action="REFRESH_PRODUCTION_LABEL_PLAN",
                resource="ProductionLabelPlanRefresh",
                actor=user,
                entity_type="production_label_plan_refresh",
                entity_id=result.receipt.id,
                object_ref=f"production_label_plan_refresh:{result.receipt.id}",
                customer_id=order_row.customer_id if order_row else None,
                customer_name=order_row.name if order_row else None,
                batch_id=payload.idempotency_key,
                description="按当前常用箱刷新生产任务标签计划",
                details={
                    "task_id": result.task.id,
                    "product_id": result.product.id,
                    "operator_id": user.id,
                    "expected_task_version": payload.expected_task_version,
                    "expected_product_version": payload.expected_product_version,
                    "before": before,
                    "after": after,
                },
            )
        db.commit()
        frozen_plan = json.loads(result.receipt.after_snapshot_json)
        return {
            "refresh_id": int(result.receipt.id),
            "idempotency_key": result.receipt.idempotency_key,
            "request_hash": result.receipt.request_hash,
            # Idempotent replay returns the exact original receipt payload.
            "replayed": False,
            "task_id": int(result.receipt.task_id),
            "task_version": int(result.receipt.after_task_version),
            "product_id": int(result.receipt.product_id),
            "product_version": int(result.receipt.after_product_version),
            "production_label_plan": frozen_plan,
        }
    except HTTPException:
        db.rollback()
        raise
    except ProductionLabelOperationError as error:
        db.rollback()
        raise HTTPException(
            status_code=error.status_code,
            detail=str(error),
        ) from error
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="标签计划已被其他请求修改，请刷新后重试",
        ) from error
    except Exception:
        db.rollback()
        raise


@router.get("/completions")
def get_production_completions(
    customer_id: int | None = Query(default=None, gt=0),
    order_keyword: str | None = Query(default=None, max_length=150),
    product_code: str | None = Query(default=None, max_length=150),
    product_name: str | None = Query(default=None, max_length=250),
    completed_date_from: date | None = Query(default=None),
    completed_date_to: date | None = Query(default=None),
    completion_status: Literal["posted", "reversed"] | None = Query(
        default=None,
        alias="status",
    ),
    placement_pending: bool = Query(default=False),
    page: int | None = Query(default=None, ge=1),
    page_size: int | None = Query(default=None, ge=1, le=200),
    user: User = Depends(can_read),
    db: Session = Depends(get_db),
) -> dict:
    allowed_customer_ids = _allowed_customer_ids(user, db)
    # Keep the existing unpaged request compatible with the production page
    # until its UI is switched to the paged contract.
    if page is None and page_size is None and not any(
        (
            customer_id,
            order_keyword,
            product_code,
            product_name,
            completed_date_from,
            completed_date_to,
            completion_status,
            placement_pending,
        )
    ):
        return {
            "items": list_production_completions(
                db,
                allowed_customer_ids=allowed_customer_ids,
            )
        }

    resolved_page = page or 1
    resolved_page_size = page_size or 50
    items, total = list_production_completions_page(
        db,
        allowed_customer_ids=allowed_customer_ids,
        customer_id=customer_id,
        order_keyword=order_keyword,
        product_code=product_code,
        product_name=product_name,
        completed_date_from=completed_date_from,
        completed_date_to=completed_date_to,
        status=completion_status,
        placement_pending=placement_pending,
        page=resolved_page,
        page_size=resolved_page_size,
    )
    return {
        "items": items,
        "total": total,
        "page": resolved_page,
        "page_size": resolved_page_size,
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
        if any(item.location_id is not None for item in payload.items) and not has_permission(
            user, "warehouse.execute"
        ):
            raise HTTPException(status_code=403, detail="余货入库需要仓库执行权限")
        if any(item.completion_type == "supplemental" for item in payload.items):
            if user.role != "admin":
                raise HTTPException(status_code=403, detail="补充生产确认仅限管理员")
        result = complete_production_batch(
            db,
            idempotency_key=payload.idempotency_key,
            commands=[
                CompletionCommand(
                    task_id=item.task_id,
                    expected_version=item.expected_version,
                    disposition=item.disposition,
                    completion_type=item.completion_type,
                    material_input_quantity=item.material_input_quantity,
                    actual_output_quantity=item.actual_output_quantity,
                    defective_quantity=item.defective_quantity,
                    direct_delivery_quantity=item.direct_delivery_quantity,
                    location_id=item.location_id,
                    expected_layout_version=item.expected_layout_version,
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
        if not result.replayed:
            customer_snapshots = _completion_customer_snapshots(
                db,
                result.completions,
            )
            for completion in result.completions:
                _append_production_completion_audit(
                    db,
                    user=user,
                    completion=completion,
                    customer_snapshot=customer_snapshots.get(completion.id),
                )
            customer_ids = sorted(
                {snapshot[0] for snapshot in customer_snapshots.values()}
            )
            customer_names = sorted(
                {snapshot[1] for snapshot in customer_snapshots.values()}
            )
            append_audit_event(
                db,
                event_category="business",
                result="success",
                source="web",
                module_code="production",
                action_code="production.completion.batch_posted",
                legacy_action="COMPLETE_PRODUCTION_BATCH",
                resource="ProductionCompletionBatch",
                actor=user,
                entity_type="production_completion_batch",
                entity_id=result.batch.id,
                object_ref=f"production_completion_batch:{result.batch.id}",
                customer_id=(customer_ids[0] if len(customer_ids) == 1 else None),
                customer_name=(
                    customer_names[0] if len(customer_names) == 1 else None
                ),
                batch_id=str(result.batch.id),
                description="批量确认生产完工",
                details={
                    "batch_id": result.batch.id,
                    "completion_ids": [
                        completion.id for completion in result.completions
                    ],
                    "task_ids": [
                        completion.task_id for completion in result.completions
                    ],
                    "completion_types": [
                        completion.completion_type
                        for completion in result.completions
                    ],
                    "item_count": result.batch.item_count,
                    "customer_ids": customer_ids,
                    "customer_names": customer_names,
                },
            )
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
    except Exception:
        db.rollback()
        raise


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
                expected_layout_version=payload.expected_layout_version,
                pallet_id=payload.pallet_id,
                pallet_code=payload.pallet_code,
                remarks=payload.remarks,
            ),
            operator_id=user.id,
        )
        if not result.replayed:
            completion = db.get(ProductionCompletion, completion_id)
            customer_snapshot = _completion_customer_snapshots(
                db,
                (completion,) if completion is not None else (),
            ).get(completion_id)
            snapshot_customer_id, snapshot_customer_name = (
                customer_snapshot or (None, None)
            )
            append_audit_event(
                db,
                event_category="business",
                result="success",
                source="web",
                module_code="production",
                action_code="production.stock_transfer.posted",
                legacy_action="TRANSFER_PRODUCTION_STOCK",
                resource="ProductionStockTransfer",
                actor=user,
                entity_type="production_stock_transfer",
                entity_id=result.transfer.id,
                object_ref=f"production_stock_transfer:{result.transfer.id}",
                customer_id=snapshot_customer_id,
                customer_name=snapshot_customer_name,
                batch_id=(str(completion.batch_id) if completion is not None else None),
                description="生产完工余货转入库存",
                details={
                    "completion_id": completion_id,
                    "batch_id": completion.batch_id if completion is not None else None,
                    "transfer_id": result.transfer.id,
                    "inventory_lot_id": result.transfer.inventory_lot_id,
                    "warehouse_location_id": result.transfer.warehouse_location_id,
                },
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
    except Exception:
        db.rollback()
        raise


@router.put("/completions/{completion_id}/actual-quantity")
def put_completion_actual_quantity(
    completion_id: int,
    payload: ActualQuantityAdjustmentRequest,
    user: User = Depends(actual_quantity_operator),
    db: Session = Depends(get_db),
) -> dict:
    try:
        customer_id = completion_customer_id(db, completion_id)
        if customer_id is not None:
            require_customer_access(customer_id, current_user=user, db=db)
        result = adjust_production_completion_actual_quantity(
            db,
            completion_id=completion_id,
            actual_output_quantity=payload.actual_output_quantity,
            expected_task_version=payload.expected_task_version,
            expected_lot_version=payload.expected_lot_version,
            idempotency_key=payload.idempotency_key,
            operator_id=user.id,
        )
        if not result.replayed:
            customer_snapshot = _completion_customer_snapshots(
                db,
                (result.completion,),
            ).get(completion_id)
            snapshot_customer_id, snapshot_customer_name = (
                customer_snapshot or (None, None)
            )
            append_audit_event(
                db,
                event_category="business",
                result="success",
                source="web",
                module_code="production",
                action_code="production.actual_quantity.adjusted",
                legacy_action="ADJUST_PRODUCTION_ACTUAL",
                resource="ProductionCompletion",
                actor=user,
                entity_type="production_completion",
                entity_id=completion_id,
                object_ref=f"production_completion:{completion_id}",
                customer_id=snapshot_customer_id,
                customer_name=snapshot_customer_name,
                batch_id=payload.idempotency_key,
                description="老板或管理员修订实际完工成品数量",
                details={
                    "completion_id": completion_id,
                    "inventory_lot_id": result.lot.id,
                    "inventory_movement_id": result.movement.id,
                    "before_actual_output_quantity": (
                        result.previous_actual_output_quantity
                    ),
                    "after_actual_output_quantity": payload.actual_output_quantity,
                    "expected_task_version": payload.expected_task_version,
                    "expected_lot_version": payload.expected_lot_version,
                },
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
            "replayed": result.replayed,
            "inventory_movement_id": result.movement.id,
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
            detail="实际完工数量已被其他操作修改，请刷新后重试",
        ) from error
    except Exception:
        db.rollback()
        raise


@router.post("/completions/{completion_id}/revert")
def revert_production_completion(
    completion_id: int,
    payload: CompletionReversalRequest,
    user: User = Depends(admin_only),
    db: Session = Depends(get_db),
) -> dict:
    reason = (payload.reason or "").strip() or "撤销生产确认（系统记录）"
    try:
        customer_id = completion_customer_id(db, completion_id)
        if customer_id is not None:
            require_customer_access(customer_id, current_user=user, db=db)
        result = reverse_production_completion(
            db,
            completion_id=completion_id,
            operator_id=user.id,
            reason=reason,
        )
        customer_snapshot = _completion_customer_snapshots(
            db,
            (result.completion,),
        ).get(completion_id)
        snapshot_customer_id, snapshot_customer_name = (
            customer_snapshot or (None, None)
        )
        append_audit_event(
            db,
            event_category="business",
            result="success",
            source="web",
            module_code="production",
            action_code="production.completion.reverted",
            legacy_action="REVERT_PRODUCTION_COMPLETION",
            resource="ProductionCompletion",
            actor=user,
            entity_type="production_completion",
            entity_id=completion_id,
            object_ref=f"production_completion:{completion_id}",
            customer_id=snapshot_customer_id,
            customer_name=snapshot_customer_name,
            batch_id=str(result.completion.batch_id),
            description="管理员撤销生产确认并回到待生产确认",
            details={
                "reason": reason,
                "before_completion_status": "posted",
                "after_completion_status": result.completion.status,
                "completion_id": completion_id,
                "batch_id": result.completion.batch_id,
                "stock_transfer_id": result.transfer.id if result.transfer else None,
                "inventory_lot_id": result.inventory_lot_id,
                "warehouse_location_id": (
                    result.transfer.warehouse_location_id
                    if result.transfer is not None
                    else result.completion.warehouse_location_id
                ),
                "reversed_semi_movement_ids": list(
                    result.reversed_semi_movement_ids
                ),
            },
        )
        db.commit()
        rows = list_production_completions(
            db,
            allowed_customer_ids=_allowed_customer_ids(user, db),
            completion_ids=[completion_id],
        )
        return {
            "message": "已撤销生产确认，订单已回到待生产确认",
            "completion": rows[0] if rows else None,
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
            detail="生产或库存记录已被其他操作修改，请刷新后重试",
        ) from error
    except Exception:
        db.rollback()
        raise
