from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from threading import Lock
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models.audit import OperationLog
from app.models.delivery import Delivery, DeliveryItem
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.product_bom import SalesOrderItemBomComponent
from app.models.production import ProductionCompletion, ProductionTask
from app.models.production_label_print import (
    ProductionPackagingLabelPrintJob,
    ProductionPackagingLabelPrintJobTask,
)
from app.models.production_profile_refresh import ProductionTaskProfileRefresh


PROFILE_SCHEMA_VERSION = 1
PROFILE_REFRESH_ACTION = "production.task_profile.refreshed"
PRODUCTION_PRINT_BATCH_ACTION = "requisition.production_print_batch.prepared"

_PROFILE_REFRESH_LOCK = Lock()
_PROFILE_FIELDS = (
    "box_style",
    "needs_die_cut",
    "production_process",
    "production_notes",
    "cutting_mode",
    "mold_tool_id",
    "mold_tool_code",
    "mold_tool_name",
    "drawing_reference",
    "printing_plate_mode",
    "print_content",
    "printing_colors",
    "printing_plate_codes",
    "printing_plate_details",
    "plate_alignment_value_mm",
    "plate_mount_value_mm",
    "machine_set_length_mm",
    "machine_set_width_mm",
    "machine_set_height_mm",
    "source_product_version",
    "schema_version",
)
_PROFILE_LABELS = {
    "box_style": "箱型",
    "needs_die_cut": "模切",
    "production_process": "结合/生产方式",
    "production_notes": "生产说明",
    "cutting_mode": "开料方式",
    "mold_tool_id": "模具",
    "mold_tool_code": "模具编号",
    "mold_tool_name": "模具名称",
    "drawing_reference": "图纸",
    "printing_plate_mode": "印刷方式",
    "print_content": "印刷情况",
    "printing_colors": "印刷颜色",
    "printing_plate_codes": "挂板编号",
    "printing_plate_details": "挂板资料",
    "plate_alignment_value_mm": "挂板对齐值",
    "plate_mount_value_mm": "挂板装版值",
    "machine_set_length_mm": "机器设定长",
    "machine_set_width_mm": "机器设定宽",
    "machine_set_height_mm": "机器设定高",
    "source_product_version": "常用箱版本",
}


class ProductionTaskProfileError(ValueError):
    def __init__(self, message: str, status_code: int = 409) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class ProductionTaskProfileContext:
    task: ProductionTask
    item: OrderItem
    order: Order
    product: Product
    component: SalesOrderItemBomComponent | None


@dataclass(frozen=True)
class ProductionTaskProfilePreview:
    context: ProductionTaskProfileContext
    before: dict[str, Any]
    after: dict[str, Any]
    changes: list[dict[str, Any]]
    eligible: bool
    block_reasons: list[str]
    preview_fingerprint: str


@dataclass(frozen=True)
class ProductionTaskProfileRefreshResult:
    receipt: ProductionTaskProfileRefresh
    task: ProductionTask
    product: Product
    replayed: bool


def production_profile_write_guard():
    """Serialize single-task preview/confirm writes in the supported worker."""

    _PROFILE_REFRESH_LOCK.acquire()
    try:
        yield
    finally:
        _PROFILE_REFRESH_LOCK.release()


def _json_list(value: object) -> list[Any]:
    try:
        parsed = json.loads(str(value or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return parsed if isinstance(parsed, list) else []


def _profile_hash(payload: object) -> str:
    return sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _mold_snapshot(product: Product | None, *, enabled: bool) -> dict[str, object]:
    mold = product.mold_tool if product is not None and enabled else None
    return {
        "mold_tool_id": int(mold.id) if mold is not None else None,
        "mold_tool_code": mold.mold_code if mold is not None else None,
        "mold_tool_name": mold.mold_name if mold is not None else None,
    }


def new_task_profile_snapshot(
    db: Session,
    product: Product | None,
    *,
    item: OrderItem,
    component_snapshot: SalesOrderItemBomComponent | None = None,
) -> dict[str, object]:
    """Return ProductionTask kwargs from the explicit order/BOM source."""

    del db  # kept in the signature for one authoritative creation seam
    if product is None:
        raise ProductionTaskProfileError("常用箱不存在，不能冻结生产任务资料")
    is_component = component_snapshot is not None
    needs_die_cut = (
        bool(component_snapshot.is_die_cut)
        if is_component
        else product.box_category == "die_cut"
    )
    if is_component:
        mold_values = {
            "mold_tool_id": component_snapshot.snapshot_mold_tool_id,
            "mold_tool_code": component_snapshot.snapshot_mold_tool_code,
            "mold_tool_name": component_snapshot.snapshot_mold_tool_name,
        }
    else:
        mold_values = _mold_snapshot(product, enabled=needs_die_cut)
    return {
        "production_box_style_snapshot": (
            component_snapshot.snapshot_component_box_style
            if is_component
            else product.box_style
        ),
        "production_needs_die_cut_snapshot": needs_die_cut,
        "production_process_snapshot": (
            component_snapshot.snapshot_component_production_process
            if is_component
            else product.production_process
        ),
        "production_notes_snapshot": (
            component_snapshot.snapshot_component_production_notes
            if is_component
            else item.snapshot_production_notes or product.production_notes
        ),
        "production_cutting_mode_snapshot": (
            component_snapshot.snapshot_component_default_cutting_mode
            if is_component
            else item.special_process
        ),
        "production_mold_tool_id_snapshot": mold_values["mold_tool_id"],
        "production_mold_tool_code_snapshot": mold_values["mold_tool_code"],
        "production_mold_tool_name_snapshot": mold_values["mold_tool_name"],
        "production_drawing_reference_snapshot": (
            component_snapshot.snapshot_die_cut_path
            if is_component
            else item.drawing_file or product.die_cut_path
        ),
        "production_profile_source_version_snapshot": max(int(product.version or 1), 1),
        "production_profile_schema_version": PROFILE_SCHEMA_VERSION,
    }


def _printing_profile(task: ProductionTask) -> dict[str, object]:
    return {
        "printing_plate_mode": task.printing_plate_mode_snapshot or "no_plate",
        "print_content": task.print_content_snapshot,
        "printing_colors": _json_list(task.printing_colors_snapshot),
        "printing_plate_codes": _json_list(task.printing_plate_codes_snapshot),
        "printing_plate_details": _json_list(task.printing_plate_details_snapshot),
        "plate_alignment_value_mm": task.plate_alignment_value_mm_snapshot,
        "plate_mount_value_mm": task.plate_mount_value_mm_snapshot,
        "machine_set_length_mm": task.machine_set_length_mm_snapshot,
        "machine_set_width_mm": task.machine_set_width_mm_snapshot,
        "machine_set_height_mm": task.machine_set_height_mm_snapshot,
    }


def task_profile_snapshot(task: ProductionTask) -> dict[str, object]:
    return {
        "box_style": task.production_box_style_snapshot,
        "needs_die_cut": task.production_needs_die_cut_snapshot,
        "production_process": task.production_process_snapshot,
        "production_notes": task.production_notes_snapshot,
        "cutting_mode": task.production_cutting_mode_snapshot,
        "mold_tool_id": task.production_mold_tool_id_snapshot,
        "mold_tool_code": task.production_mold_tool_code_snapshot,
        "mold_tool_name": task.production_mold_tool_name_snapshot,
        "drawing_reference": task.production_drawing_reference_snapshot,
        **_printing_profile(task),
        "source_product_version": task.production_profile_source_version_snapshot,
        "schema_version": task.production_profile_schema_version,
    }


def current_source_profile(
    db: Session,
    context: ProductionTaskProfileContext,
) -> dict[str, object]:
    from app.services.production_workflow import (
        ProductionWorkflowError,
        _new_task_printing_snapshot,
    )

    task_values = new_task_profile_snapshot(
        db,
        context.product,
        item=context.item,
        component_snapshot=context.component,
    )
    try:
        printing_values = _new_task_printing_snapshot(db, context.product)
    except ProductionWorkflowError as error:
        raise ProductionTaskProfileError(str(error), error.status_code) from error
    return {
        "box_style": task_values["production_box_style_snapshot"],
        "needs_die_cut": task_values["production_needs_die_cut_snapshot"],
        "production_process": task_values["production_process_snapshot"],
        "production_notes": task_values["production_notes_snapshot"],
        "cutting_mode": task_values["production_cutting_mode_snapshot"],
        "mold_tool_id": task_values["production_mold_tool_id_snapshot"],
        "mold_tool_code": task_values["production_mold_tool_code_snapshot"],
        "mold_tool_name": task_values["production_mold_tool_name_snapshot"],
        "drawing_reference": task_values["production_drawing_reference_snapshot"],
        "printing_plate_mode": printing_values["printing_plate_mode_snapshot"],
        "print_content": printing_values["print_content_snapshot"],
        "printing_colors": _json_list(printing_values["printing_colors_snapshot"]),
        "printing_plate_codes": _json_list(
            printing_values["printing_plate_codes_snapshot"]
        ),
        "printing_plate_details": _json_list(
            printing_values["printing_plate_details_snapshot"]
        ),
        "plate_alignment_value_mm": printing_values[
            "plate_alignment_value_mm_snapshot"
        ],
        "plate_mount_value_mm": printing_values["plate_mount_value_mm_snapshot"],
        "machine_set_length_mm": printing_values[
            "machine_set_length_mm_snapshot"
        ],
        "machine_set_width_mm": printing_values[
            "machine_set_width_mm_snapshot"
        ],
        "machine_set_height_mm": printing_values[
            "machine_set_height_mm_snapshot"
        ],
        "source_product_version": task_values[
            "production_profile_source_version_snapshot"
        ],
        "schema_version": PROFILE_SCHEMA_VERSION,
    }


def resolved_task_profile(
    db: Session,
    *,
    task: ProductionTask,
    item: OrderItem,
    product: Product,
    component: SalesOrderItemBomComponent | None,
) -> dict[str, object]:
    if task.production_profile_schema_version is not None:
        return task_profile_snapshot(task)
    # Legacy compatibility is read-only: no implicit database backfill.
    context = ProductionTaskProfileContext(task, item, item.order, product, component)
    current = current_source_profile(db, context)
    current["schema_version"] = None
    current["source_product_version"] = None
    return current


def _load_context(db: Session, task_id: int) -> ProductionTaskProfileContext:
    task = db.get(ProductionTask, int(task_id))
    if task is None:
        raise ProductionTaskProfileError("生产任务不存在", 404)
    item = db.get(OrderItem, int(task.order_item_id))
    if item is None:
        raise ProductionTaskProfileError("生产任务关联订单明细不存在")
    order = db.get(Order, int(item.order_id))
    product_id = int(item.product_id)
    component = None
    if task.sales_order_item_bom_component_id is not None:
        component = db.get(
            SalesOrderItemBomComponent,
            int(task.sales_order_item_bom_component_id),
        )
        if component is None:
            raise ProductionTaskProfileError("生产任务关联组件快照不存在")
        product_id = int(component.component_product_id)
    product = db.get(Product, product_id)
    if order is None or product is None or not product.is_active:
        raise ProductionTaskProfileError("生产任务关联订单或常用箱不存在/已停用")
    return ProductionTaskProfileContext(task, item, order, product, component)


def _task_in_prepared_print_batch(db: Session, task_id: int) -> bool:
    logs = db.scalars(
        select(OperationLog).where(
            OperationLog.action_code == PRODUCTION_PRINT_BATCH_ACTION,
            OperationLog.result == "success",
        )
    ).all()
    for log in logs:
        try:
            details = json.loads(log.details or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        for item in details.get("items") or []:
            for version in item.get("task_versions") or []:
                if int(version.get("task_id") or 0) == int(task_id):
                    return True
    return False


def _refresh_block_reasons(
    db: Session,
    context: ProductionTaskProfileContext,
) -> list[str]:
    task = context.task
    reasons: list[str] = []
    if task.status != "pending":
        reasons.append("只有待生产任务可以刷新资料")
    if db.scalar(
        select(ProductionCompletion.id)
        .where(ProductionCompletion.task_id == task.id)
        .limit(1)
    ) is not None:
        reasons.append("任务已有完工或撤销历史，不能改动冻结资料")
    printed_label = db.scalar(
        select(ProductionPackagingLabelPrintJobTask.id)
        .join(
            ProductionPackagingLabelPrintJob,
            ProductionPackagingLabelPrintJob.id
            == ProductionPackagingLabelPrintJobTask.print_job_id,
        )
        .where(
            ProductionPackagingLabelPrintJobTask.production_task_id == task.id,
            ProductionPackagingLabelPrintJob.status == "printed",
        )
        .limit(1)
    )
    if printed_label is not None or _task_in_prepared_print_batch(db, task.id):
        reasons.append("任务已生成/打印生产任务单或产品标签，不能刷新")
    delivery_fact = db.scalar(
        select(DeliveryItem.id)
        .join(Delivery, Delivery.id == DeliveryItem.delivery_id)
        .where(
            DeliveryItem.order_item_id == context.item.id,
            Delivery.status != "voided",
        )
        .limit(1)
    )
    if delivery_fact is not None:
        reasons.append("订单明细已有送货单事实，不能刷新生产资料")
    return reasons


def _changes(before: dict[str, object], after: dict[str, object]) -> list[dict[str, object]]:
    return [
        {
            "field": field,
            "label": _PROFILE_LABELS.get(field, field),
            "before": before.get(field),
            "after": after.get(field),
        }
        for field in _PROFILE_FIELDS
        if before.get(field) != after.get(field)
    ]


def preview_task_profile_refresh(
    db: Session,
    *,
    task_id: int,
) -> ProductionTaskProfilePreview:
    context = _load_context(db, task_id)
    before = task_profile_snapshot(context.task)
    after = current_source_profile(db, context)
    changes = _changes(before, after)
    reasons = _refresh_block_reasons(db, context)
    if not changes:
        reasons.append("任务冻结资料已与当前来源一致")
    fingerprint = _profile_hash(
        {
            "task_id": context.task.id,
            "task_version": context.task.version,
            "source_version": context.product.version,
            "before": before,
            "after": after,
            "changes": changes,
        }
    )
    return ProductionTaskProfilePreview(
        context=context,
        before=before,
        after=after,
        changes=changes,
        eligible=not reasons,
        block_reasons=reasons,
        preview_fingerprint=fingerprint,
    )


def _request_hash(
    *,
    task_id: int,
    expected_task_version: int,
    expected_source_version: int,
    preview_fingerprint: str,
    operator_id: int | None,
) -> str:
    return _profile_hash(
        {
            "task_id": int(task_id),
            "expected_task_version": int(expected_task_version),
            "expected_source_version": int(expected_source_version),
            "preview_fingerprint": str(preview_fingerprint),
            "operator_id": operator_id,
        }
    )


def _task_update_values(after: dict[str, object], next_version: int) -> dict[str, object]:
    return {
        "production_box_style_snapshot": after["box_style"],
        "production_needs_die_cut_snapshot": after["needs_die_cut"],
        "production_process_snapshot": after["production_process"],
        "production_notes_snapshot": after["production_notes"],
        "production_cutting_mode_snapshot": after["cutting_mode"],
        "production_mold_tool_id_snapshot": after["mold_tool_id"],
        "production_mold_tool_code_snapshot": after["mold_tool_code"],
        "production_mold_tool_name_snapshot": after["mold_tool_name"],
        "production_drawing_reference_snapshot": after["drawing_reference"],
        "printing_plate_mode_snapshot": after["printing_plate_mode"],
        "print_content_snapshot": after["print_content"],
        "printing_colors_snapshot": json.dumps(
            after["printing_colors"], ensure_ascii=False
        ),
        "printing_plate_codes_snapshot": json.dumps(
            after["printing_plate_codes"], ensure_ascii=False
        ),
        "printing_plate_details_snapshot": json.dumps(
            after["printing_plate_details"], ensure_ascii=False
        ),
        "plate_alignment_value_mm_snapshot": after["plate_alignment_value_mm"],
        "plate_mount_value_mm_snapshot": after["plate_mount_value_mm"],
        "machine_set_length_mm_snapshot": after["machine_set_length_mm"],
        "machine_set_width_mm_snapshot": after["machine_set_width_mm"],
        "machine_set_height_mm_snapshot": after["machine_set_height_mm"],
        "production_profile_source_version_snapshot": after[
            "source_product_version"
        ],
        "production_profile_schema_version": PROFILE_SCHEMA_VERSION,
        "version": next_version,
    }


def refresh_task_profile(
    db: Session,
    *,
    task_id: int,
    expected_task_version: int,
    expected_source_version: int,
    preview_fingerprint: str,
    idempotency_key: str,
    operator_id: int | None,
) -> ProductionTaskProfileRefreshResult:
    key = str(idempotency_key or "").strip()
    if not key:
        raise ProductionTaskProfileError("幂等键不能为空", 422)
    fingerprint = str(preview_fingerprint or "").strip().lower()
    if len(fingerprint) != 64:
        raise ProductionTaskProfileError("差异预览凭证无效，请重新预览", 422)
    request_hash = _request_hash(
        task_id=task_id,
        expected_task_version=expected_task_version,
        expected_source_version=expected_source_version,
        preview_fingerprint=fingerprint,
        operator_id=operator_id,
    )
    existing = db.scalar(
        select(ProductionTaskProfileRefresh).where(
            ProductionTaskProfileRefresh.idempotency_key == key
        )
    )
    if existing is not None:
        if existing.request_hash != request_hash or existing.operator_id != operator_id:
            raise ProductionTaskProfileError("该幂等键已用于另一项资料刷新")
        task = db.get(ProductionTask, existing.task_id)
        product = db.get(Product, existing.product_id)
        if task is None or product is None:
            raise ProductionTaskProfileError("原资料刷新回执关联对象不存在")
        return ProductionTaskProfileRefreshResult(existing, task, product, True)

    preview = preview_task_profile_refresh(db, task_id=task_id)
    task = preview.context.task
    product = preview.context.product
    if int(task.version) != int(expected_task_version):
        raise ProductionTaskProfileError("生产任务已变化，请重新预览")
    if int(product.version) != int(expected_source_version):
        raise ProductionTaskProfileError("常用箱资料已变化，请重新预览")
    if preview.preview_fingerprint != fingerprint:
        raise ProductionTaskProfileError("资料差异已变化，请重新预览")
    if not preview.eligible:
        raise ProductionTaskProfileError("；".join(preview.block_reasons))

    next_version = int(task.version) + 1
    changed = db.execute(
        update(ProductionTask)
        .where(
            ProductionTask.id == int(task_id),
            ProductionTask.version == int(expected_task_version),
            ProductionTask.status == "pending",
        )
        .values(**_task_update_values(preview.after, next_version))
    )
    if changed.rowcount != 1:
        raise ProductionTaskProfileError("生产任务已被其他操作修改，请重新预览")
    receipt = ProductionTaskProfileRefresh(
        task_id=int(task_id),
        product_id=int(product.id),
        operator_id=operator_id,
        idempotency_key=key,
        request_hash=request_hash,
        preview_fingerprint=fingerprint,
        expected_task_version=int(expected_task_version),
        expected_source_version=int(expected_source_version),
        before_task_version=int(expected_task_version),
        after_task_version=next_version,
        source_version_snapshot=int(product.version),
        before_snapshot_json=json.dumps(preview.before, ensure_ascii=False, default=str),
        after_snapshot_json=json.dumps(preview.after, ensure_ascii=False, default=str),
        changes_json=json.dumps(preview.changes, ensure_ascii=False, default=str),
    )
    db.add(receipt)
    db.flush()
    db.expire_all()
    refreshed_task = db.get(ProductionTask, int(task_id))
    assert refreshed_task is not None
    return ProductionTaskProfileRefreshResult(
        receipt, refreshed_task, product, False
    )
