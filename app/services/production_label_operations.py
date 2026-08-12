from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from threading import Lock
from types import SimpleNamespace

from sqlalchemy import literal, select, union_all, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.core.time_contract import utc_naive_to_api, utc_now_naive
from app.models.order import OrderItem
from app.models.product import Product
from app.models.product_bom import SalesOrderItemBomComponent
from app.models.production import ProductionCompletion, ProductionTask
from app.models.production_label_print import (
    ProductionLabelPlanRefresh,
    ProductionPackagingLabelPrintJob,
    ProductionPackagingLabelPrintJobTask,
)
from app.models.supplier_requisition_order import SupplierRequisitionOrder
from app.services.composite_bom_workflow import (
    component_available_quantity,
    effective_component_demands,
)
from app.services.production_label_strategy import (
    CURRENT_PRODUCTION_LABEL_TEMPLATE_VERSION,
    ProductionLabelStrategyError,
    build_new_task_production_label_snapshot,
)
from app.services.production_packaging_label import (
    build_supplier_requisition_packaging_label_package,
)
from app.services.warehouse_inventory import active_finished_reserved_qty


class ProductionLabelOperationError(ValueError):
    def __init__(self, message: str, status_code: int = 409):
        super().__init__(message)
        self.status_code = status_code


_PRODUCTION_LABEL_WRITE_LOCK = Lock()


def production_label_write_guard():
    """Serialize label-plan/job mutations in the supported single worker.

    Database CAS remains authoritative for cross-feature changes; this guard
    additionally makes simultaneous exact idempotency replays wait for the
    winning transaction so they can return its persisted receipt.
    """

    _PRODUCTION_LABEL_WRITE_LOCK.acquire()
    try:
        yield
    finally:
        _PRODUCTION_LABEL_WRITE_LOCK.release()


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _canonical_hash(value: object) -> str:
    return sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _is_sqlite_busy(error: OperationalError) -> bool:
    rendered = str(error).lower()
    return any(
        marker in rendered
        for marker in (
            "database is locked",
            "database table is locked",
            "database schema is locked",
            "sqlite_busy",
            "sqlite_locked",
        )
    )


def _claim_task_versions(
    db: Session,
    expected_versions: dict[int, int],
    *,
    conflict_message: str,
    allowed_statuses: set[str] | None = None,
) -> list[ProductionTask]:
    """Acquire a real write claim on task rows, including on SQLite.

    SQLite ignores ``SELECT FOR UPDATE``.  A no-op conditional UPDATE obtains
    its database writer lock while PostgreSQL obtains row locks.  Explicitly
    preserving ``updated_at`` avoids manufacturing a business modification.
    """

    for task_id in sorted(expected_versions):
        statement = update(ProductionTask).where(
            ProductionTask.id == int(task_id),
            ProductionTask.version == int(expected_versions[task_id]),
        )
        if allowed_statuses is not None:
            statement = statement.where(ProductionTask.status.in_(allowed_statuses))
        try:
            result = db.execute(
                statement.values(
                    version=ProductionTask.version,
                    updated_at=ProductionTask.updated_at,
                ).execution_options(synchronize_session=False)
            )
        except OperationalError as error:
            if _is_sqlite_busy(error):
                raise ProductionLabelOperationError(conflict_message) from error
            raise
        if result.rowcount != 1:
            raise ProductionLabelOperationError(conflict_message)

    rows = list(
        db.scalars(
            select(ProductionTask)
            .where(ProductionTask.id.in_(sorted(expected_versions)))
            .order_by(ProductionTask.id)
            .execution_options(populate_existing=True)
        ).all()
    )
    if len(rows) != len(expected_versions) or any(
        int(row.version) != int(expected_versions[int(row.id)]) for row in rows
    ):
        raise ProductionLabelOperationError(conflict_message)
    return rows


def _claim_product_version(
    db: Session,
    *,
    product_id: int,
    expected_version: int,
) -> Product:
    try:
        result = db.execute(
            update(Product)
            .where(
                Product.id == int(product_id),
                Product.version == int(expected_version),
            )
            .values(version=Product.version, updated_at=Product.updated_at)
            .execution_options(synchronize_session=False)
        )
    except OperationalError as error:
        if _is_sqlite_busy(error):
            raise ProductionLabelOperationError(
                "常用箱产品正在被其他操作更新，请刷新后重试"
            ) from error
        raise
    if result.rowcount != 1:
        raise ProductionLabelOperationError("常用箱产品版本已变化，请刷新后重试")
    product = db.scalar(
        select(Product)
        .where(Product.id == int(product_id))
        .execution_options(populate_existing=True)
    )
    if product is None or int(product.version) != int(expected_version):
        raise ProductionLabelOperationError("常用箱产品版本已变化，请刷新后重试")
    return product


def task_label_snapshot(task: ProductionTask) -> dict[str, object]:
    return {
        "enabled": bool(task.production_label_enabled_snapshot),
        "units_per_label": task.production_label_units_per_label_snapshot,
        "total_quantity": int(task.production_label_total_quantity_snapshot or 0),
        "label_count": int(task.production_label_count_snapshot or 0),
        "template_version": task.production_label_template_version_snapshot,
        "product_version": task.production_label_product_version_snapshot,
    }


def annotate_task_label_plans(
    db: Session,
    items: list[dict],
    *,
    hydrate: bool = True,
) -> list[dict]:
    """Attach the maintenance projection without changing production math."""

    task_ids = [int(item["id"]) for item in items]
    if not task_ids:
        return items
    if hydrate:
        tasks = {
            int(task.id): task
            for task in db.scalars(
                select(ProductionTask).where(ProductionTask.id.in_(task_ids))
            ).all()
        }
    else:
        tasks = {
            int(item["id"]): SimpleNamespace(
                id=int(item["id"]),
                status=item.get("status"),
                production_label_enabled_snapshot=bool(
                    item.get("production_label_enabled_snapshot")
                ),
                production_label_units_per_label_snapshot=item.get(
                    "production_label_units_per_label_snapshot"
                ),
                production_label_total_quantity_snapshot=int(
                    item.get("production_label_total_quantity_snapshot") or 0
                ),
                production_label_count_snapshot=int(
                    item.get("production_label_count_snapshot") or 0
                ),
                production_label_template_version_snapshot=item.get(
                    "production_label_template_version_snapshot"
                ),
                production_label_product_version_snapshot=item.get(
                    "production_label_product_version_snapshot"
                ),
            )
            for item in items
        }
    facts = union_all(
        select(
            ProductionCompletion.task_id.label("task_id"),
            literal("completion").label("fact_kind"),
        ).where(ProductionCompletion.task_id.in_(task_ids)),
        select(
            ProductionPackagingLabelPrintJobTask.production_task_id.label("task_id"),
            literal("printed").label("fact_kind"),
        )
        .join(
            ProductionPackagingLabelPrintJob,
            ProductionPackagingLabelPrintJob.id
            == ProductionPackagingLabelPrintJobTask.print_job_id,
        )
        .where(
            ProductionPackagingLabelPrintJobTask.production_task_id.in_(task_ids),
            ProductionPackagingLabelPrintJob.status == "printed",
        ),
    ).subquery()
    fact_rows = db.execute(select(facts.c.task_id, facts.c.fact_kind)).all()
    completion_task_ids = {
        int(task_id) for task_id, kind in fact_rows if kind == "completion"
    }
    printed_task_ids = {
        int(task_id) for task_id, kind in fact_rows if kind == "printed"
    }
    for item in items:
        task = tasks.get(int(item["id"]))
        if task is None:
            continue
        product: Product | None = None
        total_quantity: int | None = None
        product_error: str | None = None
        if hydrate:
            try:
                product, total_quantity = _task_product_and_total(db, task)
            except ProductionLabelOperationError as error:
                product_error = str(error)
        else:
            current_version = item.get("current_product_version")
            if current_version is not None:
                product = SimpleNamespace(
                    id=item.get("product_id"),
                    version=int(current_version),
                    production_label_enabled=bool(
                        item.get("current_product_production_label_enabled")
                    ),
                    production_label_units_per_label=item.get(
                        "current_product_production_label_units_per_label"
                    ),
                )
            total_quantity = int(
                item.get("production_label_total_quantity_snapshot") or 0
            )

        reason: str | None = None
        if task.status not in {"waiting_material", "pending"}:
            reason = "任务已开始或已结束"
        elif task.id in completion_task_ids:
            reason = "任务已有生产完工事实"
        elif task.id in printed_task_ids:
            reason = "任务已有实际标签打印事实"
        elif product is None:
            reason = product_error or "常用箱产品不存在"
        elif hydrate and (total_quantity is None or total_quantity <= 0):
            reason = "任务没有待生产成品数量"
        elif (
            bool(product.production_label_enabled)
            and not bool(task.production_label_enabled_snapshot)
            and task.production_label_template_version_snapshot
            == CURRENT_PRODUCTION_LABEL_TEMPLATE_VERSION
            and task.production_label_product_version_snapshot == int(product.version)
        ):
            reason = "产品已启用标签策略，但任务快照未启用；请停止操作并核对"
        elif (
            task.production_label_template_version_snapshot
            == CURRENT_PRODUCTION_LABEL_TEMPLATE_VERSION
            and task.production_label_product_version_snapshot == int(product.version)
        ):
            reason = "标签计划已是当前产品版本"

        item["production_label_plan"] = (
            task_label_snapshot(task)
            if hydrate
            else {
                "enabled": bool(item.get("production_label_enabled_snapshot")),
                "units_per_label": item.get(
                    "production_label_units_per_label_snapshot"
                ),
                "total_quantity": int(
                    item.get("production_label_total_quantity_snapshot") or 0
                ),
                "label_count": int(
                    item.get("production_label_count_snapshot") or 0
                ),
                "template_version": item.get(
                    "production_label_template_version_snapshot"
                ),
                "product_version": item.get(
                    "production_label_product_version_snapshot"
                ),
            }
        )
        item["current_production_label_product"] = (
            {
                "product_id": int(product.id),
                "version": int(product.version),
                "enabled": bool(product.production_label_enabled),
                "units_per_label": product.production_label_units_per_label,
            }
            if product is not None
            else None
        )
        item["can_refresh_production_label_plan"] = reason is None
        item["production_label_refresh_block_reason"] = reason
    return items


def _task_product_and_total(
    db: Session,
    task: ProductionTask,
    *,
    lock_product: bool = False,
) -> tuple[Product, int]:
    item = db.get(OrderItem, task.order_item_id)
    if item is None:
        raise ProductionLabelOperationError("订单明细不存在，不能刷新标签计划", 404)

    if task.sales_order_item_bom_component_id is None:
        product_id = int(item.product_id)
        ordered = int(item.quantity or 0)
        coverage = min(max(active_finished_reserved_qty(db, item.id), 0), ordered)
        total_quantity = max(ordered - coverage, 0)
    else:
        snapshot = db.get(
            SalesOrderItemBomComponent,
            task.sales_order_item_bom_component_id,
        )
        if snapshot is None:
            raise ProductionLabelOperationError("订单组件快照不存在，不能刷新标签计划")
        product_id = int(snapshot.component_product_id)
        demand = next(
            (
                row
                for row in effective_component_demands(db, item.id)
                if int(row.snapshot_id) == int(snapshot.id)
            ),
            None,
        )
        if demand is None:
            raise ProductionLabelOperationError("订单组件需求快照不完整，不能刷新标签计划")
        total_quantity = max(
            int(demand.required_piece_quantity)
            - int(component_available_quantity(db, snapshot.id)),
            0,
        )

    product_query = select(Product).where(Product.id == product_id)
    if lock_product:
        product_query = product_query.with_for_update()
    product = db.scalar(product_query)
    if product is None:
        raise ProductionLabelOperationError("常用箱产品不存在，不能刷新标签计划", 404)
    if total_quantity <= 0:
        raise ProductionLabelOperationError("当前任务没有待生产成品数量，不能刷新标签计划")
    return product, total_quantity


@dataclass(frozen=True)
class LabelPlanRefreshResult:
    receipt: ProductionLabelPlanRefresh
    task: ProductionTask
    product: Product
    replayed: bool


def refresh_task_label_plan(
    db: Session,
    *,
    task_id: int,
    expected_task_version: int,
    expected_product_version: int,
    idempotency_key: str,
    operator_id: int,
) -> LabelPlanRefreshResult:
    key = idempotency_key.strip()
    request_hash = _canonical_hash(
        {
            "task_id": int(task_id),
            "expected_task_version": int(expected_task_version),
            "expected_product_version": int(expected_product_version),
            "idempotency_key": key,
        }
    )
    repeated = db.scalar(
        select(ProductionLabelPlanRefresh).where(
            ProductionLabelPlanRefresh.idempotency_key == key
        )
    )
    if repeated is not None:
        if repeated.request_hash != request_hash or repeated.operator_id != operator_id:
            raise ProductionLabelOperationError("幂等键已用于不同的标签计划刷新请求")
        task = db.get(ProductionTask, repeated.task_id)
        product = db.get(Product, repeated.product_id)
        if task is None or product is None:
            raise ProductionLabelOperationError("标签计划刷新回执关联的数据已缺失")
        return LabelPlanRefreshResult(repeated, task, product, True)

    task = db.get(ProductionTask, task_id)
    if task is None:
        raise ProductionLabelOperationError("生产任务不存在", 404)
    if task.status not in {"waiting_material", "pending"}:
        raise ProductionLabelOperationError("只有尚未开始的待料或待生产任务可刷新标签计划")
    if int(task.version or 0) != int(expected_task_version):
        raise ProductionLabelOperationError("生产任务版本已变化，请刷新后重试")
    task = _claim_task_versions(
        db,
        {int(task.id): int(expected_task_version)},
        conflict_message="生产任务版本已变化或正在被其他操作更新，请刷新后重试",
        allowed_statuses={"waiting_material", "pending"},
    )[0]
    completion_id = db.scalar(
        select(ProductionCompletion.id)
        .where(ProductionCompletion.task_id == task.id)
        .limit(1)
    )
    if completion_id is not None:
        raise ProductionLabelOperationError("该任务已有生产完工事实，不能刷新标签计划")

    printed_job_id = db.scalar(
        select(ProductionPackagingLabelPrintJobTask.print_job_id)
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
    if printed_job_id is not None:
        raise ProductionLabelOperationError("该任务已有实际打印事实，不能刷新标签计划")

    product, total_quantity = _task_product_and_total(db, task)
    if int(product.version or 0) != int(expected_product_version):
        raise ProductionLabelOperationError("常用箱产品版本已变化，请刷新后重试")
    product = _claim_product_version(
        db,
        product_id=int(product.id),
        expected_version=int(expected_product_version),
    )
    # Recompute after the task/product write claims.  This prevents a stale
    # projection from being frozen when inventory coverage changed before the
    # transaction acquired its write lock.
    product, total_quantity = _task_product_and_total(db, task)
    if int(product.version or 0) != int(expected_product_version):
        raise ProductionLabelOperationError("常用箱产品版本已变化，请刷新后重试")
    try:
        snapshot = build_new_task_production_label_snapshot(
            product,
            total_quantity=total_quantity,
        )
    except ProductionLabelStrategyError as error:
        raise ProductionLabelOperationError(str(error)) from error
    if bool(product.production_label_enabled) and not bool(
        snapshot["production_label_enabled_snapshot"]
    ):
        raise ProductionLabelOperationError("产品已启用标签策略，但新标签计划未启用，请核对产品资料")

    before = task_label_snapshot(task)
    before_version = int(task.version)
    task.production_label_enabled_snapshot = bool(
        snapshot["production_label_enabled_snapshot"]
    )
    task.production_label_units_per_label_snapshot = snapshot[
        "production_label_units_per_label_snapshot"
    ]
    task.production_label_total_quantity_snapshot = int(
        snapshot["production_label_total_quantity_snapshot"]
    )
    task.production_label_count_snapshot = int(
        snapshot["production_label_count_snapshot"]
    )
    task.production_label_template_version_snapshot = (
        CURRENT_PRODUCTION_LABEL_TEMPLATE_VERSION
    )
    task.production_label_product_version_snapshot = int(product.version)
    task.version = before_version + 1
    after = task_label_snapshot(task)

    receipt = ProductionLabelPlanRefresh(
        task_id=task.id,
        product_id=product.id,
        idempotency_key=key,
        request_hash=request_hash,
        operator_id=operator_id,
        expected_task_version=expected_task_version,
        expected_product_version=expected_product_version,
        before_task_version=before_version,
        after_task_version=int(task.version),
        before_product_version=int(product.version),
        after_product_version=int(product.version),
        before_snapshot_json=_canonical_json(before),
        after_snapshot_json=_canonical_json(after),
    )
    db.add(receipt)
    db.flush()
    return LabelPlanRefreshResult(receipt, task, product, False)


@dataclass(frozen=True)
class PackagingLabelJobResult:
    job: ProductionPackagingLabelPrintJob
    package: dict
    replayed: bool


def _validate_job_task_links(
    db: Session,
    job: ProductionPackagingLabelPrintJob,
    package: dict,
) -> list[ProductionPackagingLabelPrintJobTask]:
    links = list(
        db.scalars(
            select(ProductionPackagingLabelPrintJobTask).where(
                ProductionPackagingLabelPrintJobTask.print_job_id == job.id
            )
        ).all()
    )
    frozen_plans = {
        int(plan["production_task_id"]): plan
        for plan in (package.get("plans") or [])
        if isinstance(plan, dict) and plan.get("production_task_id") is not None
    }
    link_ids = [int(link.production_task_id) for link in links]
    if not frozen_plans or set(frozen_plans) != set(link_ids) or len(links) != len(
        frozen_plans
    ):
        raise ProductionLabelOperationError(
            "打印作业任务关联校验失败，已停止读取或补打"
        )
    for link in links:
        plan = frozen_plans[int(link.production_task_id)]
        if (
            int(link.production_task_version)
            != int(plan["production_task_version"])
            or link.template_version != job.template_version
            or link.template_version != plan.get("template_version")
            or link.product_id != plan.get("product_id")
            or link.product_version != plan.get("product_version")
            or link.snapshot_json != _canonical_json(plan)
        ):
            raise ProductionLabelOperationError(
                "打印作业冻结任务校验失败，已停止读取或补打"
            )
    return links


def _load_job_package(
    job: ProductionPackagingLabelPrintJob,
    db: Session | None = None,
) -> dict:
    try:
        package = json.loads(job.payload_json)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise ProductionLabelOperationError("打印作业冻结内容损坏，已停止补打") from error
    if not isinstance(package, dict) or _canonical_hash(package) != job.payload_hash:
        raise ProductionLabelOperationError("打印作业冻结内容校验失败，已停止补打")
    if package.get("template_version") != job.template_version:
        raise ProductionLabelOperationError("打印作业模板版本校验失败，已停止补打")
    if db is not None:
        _validate_job_task_links(db, job, package)
    return package


def packaging_label_job_response(
    job: ProductionPackagingLabelPrintJob,
    package: dict,
    *,
    replayed: bool = False,
) -> dict:
    return {
        "job_id": int(job.id),
        "supplier_order_id": int(job.supplier_order_id),
        "status": job.status,
        "template_version": job.template_version,
        "plan_fingerprint": job.plan_fingerprint,
        "payload_hash": job.payload_hash,
        "created_at": utc_naive_to_api(job.created_at) if job.created_at else None,
        "printed_at": utc_naive_to_api(job.printed_at) if job.printed_at else None,
        "package": package,
    }


def prepare_packaging_label_job(
    db: Session,
    *,
    order: SupplierRequisitionOrder,
    idempotency_key: str,
    expected_plan_fingerprint: str,
    operator_id: int,
) -> PackagingLabelJobResult:
    key = idempotency_key.strip()
    request_hash = _canonical_hash(
        {
            "supplier_order_id": int(order.id),
            "idempotency_key": key,
            "plan_fingerprint": expected_plan_fingerprint,
        }
    )
    repeated = db.scalar(
        select(ProductionPackagingLabelPrintJob).where(
            ProductionPackagingLabelPrintJob.idempotency_key == key
        )
    )
    if repeated is not None:
        if repeated.request_hash != request_hash or repeated.operator_id != operator_id:
            raise ProductionLabelOperationError("幂等键已用于不同的标签打印作业")
        return PackagingLabelJobResult(repeated, _load_job_package(repeated, db), True)

    package = build_supplier_requisition_packaging_label_package(db, order)
    if package.get("review_required"):
        raise ProductionLabelOperationError("标签计划需要人工核对，不能创建打印作业")
    if not package.get("label_count"):
        raise ProductionLabelOperationError("该报料单没有启用生产包装标签的任务")
    if package.get("plan_fingerprint") != expected_plan_fingerprint:
        raise ProductionLabelOperationError("标签计划已变化，请刷新预览后重试")
    plan_versions = {
        int(plan["production_task_id"]): int(plan["production_task_version"])
        for plan in package.get("plans") or []
    }
    _claim_task_versions(
        db,
        plan_versions,
        conflict_message="标签计划已被刷新或正在被其他操作更新，请重新加载预览",
    )
    # Rebuild while the exact task rows are locked so a job can never combine
    # snapshots observed on opposite sides of a concurrent refresh.
    package = build_supplier_requisition_packaging_label_package(db, order)
    if package.get("plan_fingerprint") != expected_plan_fingerprint:
        raise ProductionLabelOperationError("标签计划已变化，请刷新预览后重试")
    template_version = str(package.get("template_version") or "")
    if template_version not in {"legacy_65x45_v1", "current_40x30_v1"}:
        raise ProductionLabelOperationError("标签模板版本不受支持")

    frozen_json = _canonical_json(package)
    payload_hash = sha256(frozen_json.encode("utf-8")).hexdigest()
    job = ProductionPackagingLabelPrintJob(
        supplier_order_id=order.id,
        idempotency_key=key,
        request_hash=request_hash,
        operator_id=operator_id,
        template_version=template_version,
        plan_fingerprint=expected_plan_fingerprint,
        payload_json=frozen_json,
        payload_hash=payload_hash,
        status="prepared",
    )
    db.add(job)
    db.flush()
    for plan in package.get("plans") or []:
        db.add(
            ProductionPackagingLabelPrintJobTask(
                print_job_id=job.id,
                production_task_id=int(plan["production_task_id"]),
                production_task_version=int(plan["production_task_version"]),
                product_id=plan.get("product_id"),
                product_version=plan.get("product_version"),
                template_version=template_version,
                snapshot_json=_canonical_json(plan),
            )
        )
    db.flush()
    return PackagingLabelJobResult(job, package, False)


def get_packaging_label_job(
    db: Session,
    job_id: int,
) -> PackagingLabelJobResult:
    job = db.get(ProductionPackagingLabelPrintJob, job_id)
    if job is None:
        raise ProductionLabelOperationError("标签打印作业不存在", 404)
    return PackagingLabelJobResult(job, _load_job_package(job, db), False)


def confirm_packaging_label_job_printed(
    db: Session,
    *,
    job_id: int,
    confirmation_key: str,
    operator_id: int,
) -> PackagingLabelJobResult:
    key = confirmation_key.strip()
    job = db.get(ProductionPackagingLabelPrintJob, job_id)
    if job is None:
        raise ProductionLabelOperationError("标签打印作业不存在", 404)
    package = _load_job_package(job, db)
    if job.status == "printed":
        if (
            job.printed_confirmation_key != key
            or job.printed_by_user_id != operator_id
        ):
            raise ProductionLabelOperationError("该打印作业已经由另一确认请求登记")
        return PackagingLabelJobResult(job, package, True)
    if job.status != "prepared":
        raise ProductionLabelOperationError("只有待确认的标签打印作业可以登记实际打印")

    printed_at = utc_now_naive()
    try:
        claim = db.execute(
            update(ProductionPackagingLabelPrintJob)
            .where(
                ProductionPackagingLabelPrintJob.id == int(job_id),
                ProductionPackagingLabelPrintJob.status == "prepared",
                ProductionPackagingLabelPrintJob.printed_confirmation_key.is_(None),
                ProductionPackagingLabelPrintJob.printed_at.is_(None),
            )
            .values(
                status="printed",
                printed_confirmation_key=key,
                printed_by_user_id=int(operator_id),
                printed_at=printed_at,
            )
            .execution_options(synchronize_session=False)
        )
    except OperationalError as error:
        if _is_sqlite_busy(error):
            raise ProductionLabelOperationError(
                "标签打印作业正在被其他操作确认，请刷新后重试"
            ) from error
        raise
    if claim.rowcount != 1:
        current = db.scalar(
            select(ProductionPackagingLabelPrintJob)
            .where(ProductionPackagingLabelPrintJob.id == int(job_id))
            .execution_options(populate_existing=True)
        )
        if (
            current is not None
            and current.status == "printed"
            and current.printed_confirmation_key == key
            and current.printed_by_user_id == operator_id
        ):
            return PackagingLabelJobResult(current, _load_job_package(current, db), True)
        raise ProductionLabelOperationError("该打印作业已被另一确认请求登记")
    job = db.scalar(
        select(ProductionPackagingLabelPrintJob)
        .where(ProductionPackagingLabelPrintJob.id == int(job_id))
        .execution_options(populate_existing=True)
    )
    if job is None:
        raise ProductionLabelOperationError("标签打印作业不存在", 404)

    links = _validate_job_task_links(db, job, package)
    task_ids = [int(link.production_task_id) for link in links]
    if not task_ids:
        raise ProductionLabelOperationError("打印作业没有关联生产任务，不能登记实际打印")
    frozen_plans = {
        int(plan["production_task_id"]): plan
        for plan in (package.get("plans") or [])
    }
    # Serialize against manual plan refreshes.  The job payload itself remains
    # immutable even if a later business read changes.
    _claim_task_versions(
        db,
        {
            task_id: int(frozen_plans[task_id]["production_task_version"])
            for task_id in task_ids
        },
        conflict_message="打印作业准备后标签计划已变化，不能登记旧预览为实际打印",
    )
    db.flush()
    return PackagingLabelJobResult(job, package, False)


def latest_printed_job_metadata(db: Session, supplier_order_id: int) -> dict | None:
    job = db.scalar(
        select(ProductionPackagingLabelPrintJob)
        .where(
            ProductionPackagingLabelPrintJob.supplier_order_id == supplier_order_id,
            ProductionPackagingLabelPrintJob.status == "printed",
        )
        .order_by(
            ProductionPackagingLabelPrintJob.printed_at.desc(),
            ProductionPackagingLabelPrintJob.id.desc(),
        )
        .limit(1)
    )
    if job is None:
        return None
    return {
        "job_id": int(job.id),
        "template_version": job.template_version,
        "printed_at": utc_naive_to_api(job.printed_at) if job.printed_at else None,
    }
