"""Product-identification labels from real reported replenishment sources.

Printing a planned bundle label is not a receipt, production or inventory fact.
Sales tasks retain their FK links; replenishment evidence is frozen with the
supplier-order FK in the existing immutable print-job payload.
"""
from app.services.product_label_media import bind_print_media
import json
import re
from math import ceil

from sqlalchemy import select

from app.models.customer import Customer
from app.models.procurement_source import ProcurementSourceLink
from app.models.product import Product
from app.models.production_label_print import ProductionPackagingLabelPrintJob, ProductionPackagingLabelPrintJobTask
from app.models.stock_replenishment import StockReplenishmentOrderItem
from app.models.supplier_requisition_order import SupplierRequisitionOrderItem, SupplierRequisitionOrder
from app.services.production_packaging_label import (
    ProductionPackagingLabelError, _customer_label_fields,
    apply_delivery_packaging_label_print_counts,
    build_supplier_requisition_packaging_label_package,
)
from app.services.production_label_strategy import CURRENT_PRODUCTION_LABEL_TEMPLATE_VERSION

SOURCE = "supplier_sources"


def available_selection_keys(db, order):
    from app.services.requisition_production_print import build_supplier_requisition_production_package
    keys = set()
    for card in build_supplier_requisition_production_package(db, order)["cards"]:
        if card.get("stock_replenishment_item_id"):
            keys.add(f"stock-item:{card['supplier_order_item_id']}")
        else:
            keys.update(f"task:{row['production_task_id']}" for row in card.get("components", [])
                if row.get("production_task_id") is not None)
    return keys


def parse_selection_keys(keys):
    tasks, items = set(), set()
    if not keys:
        raise ProductionPackagingLabelError("请选择需要打印标签的报料产品")
    for key in keys:
        match = re.fullmatch(r"(task|stock-item):([1-9][0-9]*)", key)
        if not match:
            raise ProductionPackagingLabelError("产品标签来源编号无效")
        (tasks if match[1] == "task" else items).add(int(match[2]))
    return tasks, items


def build_stock_plan(db, order, card):
    from app.services.stock_purchase_identity import resolve
    from app.services.sheet_cutting_contract import SheetCuttingContract
    from app.services.warehouse_inventory import WarehouseInventoryError
    from app.services.production_label_operations import _canonical_hash

    line = db.get(SupplierRequisitionOrderItem, card["supplier_order_item_id"])
    item = db.get(StockReplenishmentOrderItem, card["stock_replenishment_item_id"])
    links = list(db.scalars(select(ProcurementSourceLink).where(
        ProcurementSourceLink.supplier_item_id == line.id,
        ProcurementSourceLink.status == "active"))) if line else []
    if (not line or not item or len(links) != 1
            or links[0].stock_replenishment_item_id != item.id
            or links[0].material_requisition_item_id is not None
            or line.supplier_order_id != order.id or line.status != "active"
            or order.status != "confirmed"
            or item.order.status not in {"confirmed", "partially_stocked", "stocked"}):
        raise ProductionPackagingLabelError("补库标签缺少唯一有效报料来源，请核对原补库单")
    product = db.get(Product, item.reference_product_id or item.product_id) if (item.reference_product_id or item.product_id) else None
    customer = db.get(Customer, item.customer_id) if item.customer_id else None
    if (not product or not customer or product.deleted_at is not None or not product.is_active
            or product.customer_id != customer.id or line.product_id != product.id):
        raise ProductionPackagingLabelError("补库报料没有有效的同客户产品用途，通用原纸不能打印产品标签")
    if not product.production_label_enabled:
        raise ProductionPackagingLabelError(f"{item.product_code_snapshot}：常用箱未启用打印标签，请勾选并保存后刷新")
    units = int(product.production_label_units_per_label or 0)
    if units <= 0:
        raise ProductionPackagingLabelError(f"{item.product_code_snapshot}：请在常用箱填写每捆数量")
    try:
        identity = resolve(db, item, product)
        basis = json.loads(identity["physical_basis"])
        factor, pieces = int(item.stock_yield_per_sheet), int(item.pieces_per_box)
        if factor <= 0 or pieces <= 0:
            raise ValueError("invalid conversion")
        if item.sheet_cutting_snapshot:
            cutting = SheetCuttingContract.from_snapshot(item.sheet_cutting_snapshot)
            if cutting.yield_per_supplier_sheet != factor:
                raise ValueError("cutting differs")
        if (int(links[0].source_quantity) != int(item.quantity)
                or int(line.requisition_qty) != int(item.quantity)
                or line.sheet_cutting_snapshot != item.sheet_cutting_snapshot
                or not basis.get("spec") or not basis.get("unit")):
            raise ValueError("source differs")
    except (ValueError, KeyError, TypeError, WarehouseInventoryError) as error:
        raise ProductionPackagingLabelError("补库报料冻结身份或数量换算不完整，请核对原报料资料") from error
    total = int(item.quantity)
    if item.target_inventory_type == "semi_finished":
        total = total * factor // pieces
    count = ceil(total / units)
    if total <= 0 or count > 10000:
        raise ProductionPackagingLabelError("补库标签数量无效或超过10000张，请核对报料量与每捆数量")
    template = CURRENT_PRODUCTION_LABEL_TEMPLATE_VERSION
    quantities = [min(units, total - i * units) for i in range(count)]
    return dict(
        selection_key=f"stock-item:{line.id}", production_task_id=None, production_task_version=None,
        supplier_order_item_id=line.id, stock_replenishment_item_id=item.id,
        product_id=product.id, product_version=product.version,
        customer_id=customer.id, customer_version=customer.version,
        customer_name=customer.name, customer_code=customer.customer_code,
        **_customer_label_fields(template, customer.chinese_short_name),
        product_code=item.product_code_snapshot, product_name=item.product_name_snapshot,
        specification=basis["spec"], unit_label=basis["unit"],
        order_numbers=[], customer_pos=[], template_version=template,
        total_quantity=total, units_per_label=units, label_count=count, label_quantities=quantities,
        label_policy_source="product_master_current", quantity_basis="reported_product_plan",
        source_evidence=dict(
            supplier_order_id=order.id, supplier_item_version=line.version,
            stock_order_id=item.order.id, stock_order_status=item.order.status,
            source_link_id=links[0].id, source_snapshot_hash=_canonical_hash(links[0].source_snapshot_json),
            reported_quantity=item.quantity, quantity_contract_hash=_canonical_hash(item.quantity_contract_json),
            production_identity_hash=_canonical_hash(identity),
            yield_per_sheet=factor, pieces_per_product=pieces,
        ),
    )


def validate_frozen_source(job, package, links):
    from app.services.production_label_operations import ProductionLabelOperationError as Error, _canonical_json
    plans = package.get("plans") or []
    keys = [p.get("selection_key") for p in plans]
    if (job.delivery_id or job.material_requisition_id or not job.supplier_order_id
            or package.get("supplier_order_id") != job.supplier_order_id
            or package.get("plan_fingerprint") != job.plan_fingerprint
            or not plans or len(set(keys)) != len(keys)
            or not set(keys).issubset(package.get("source_selection_keys") or [])
            or any(label.get("selection_key") not in keys for label in package.get("labels") or [])):
        raise Error("报料标签作业来源校验失败，已停止读取或补打")
    tasks = {}
    for plan in plans:
        task_id = plan.get("production_task_id")
        if task_id is not None:
            if plan["selection_key"] != f"task:{task_id}":
                raise Error("报料标签任务身份不一致")
            tasks[task_id] = plan
        elif (plan["selection_key"] != f"stock-item:{plan.get('supplier_order_item_id')}"
                or not plan.get("stock_replenishment_item_id")
                or (plan.get("source_evidence") or {}).get("supplier_order_id") != job.supplier_order_id):
            raise Error("补库标签来源身份不一致")
    if set(tasks) != {link.production_task_id for link in links} or len(tasks) != len(links):
        raise Error("报料标签任务关联不一致")
    for link in links:
        plan = tasks[link.production_task_id]
        if (link.snapshot_json != _canonical_json(plan)
                or link.production_task_version != plan["production_task_version"]
                or link.product_id != plan["product_id"] or link.product_version != plan["product_version"]
                or link.template_version != job.template_version):
            raise Error("报料标签冻结任务校验失败")


def assert_current(db, job, package, *, claim=False):
    from app.services.production_label_operations import (
        ProductionLabelOperationError as Error, _claim_package_product_versions,
        _claim_customer_version, _claim_task_versions,
    )
    if claim:
        _claim_package_product_versions(db, package)
        _claim_task_versions(db, {p["production_task_id"]: p["production_task_version"]
            for p in package["plans"] if p.get("production_task_id") is not None},
            conflict_message="标签对应的生产任务已变化，请刷新")
        for p in package["plans"]:
            if p.get("customer_version"):
                _claim_customer_version(db, customer_id=p["customer_id"], expected_version=p["customer_version"])
        db.expire_all()
    order = db.get(SupplierRequisitionOrder, job.supplier_order_id)
    try:
        current = build_supplier_requisition_packaging_label_package(db, order,
            selected_source_keys=set(package["source_selection_keys"]))
    except (ProductionPackagingLabelError, ValueError) as error:
        raise Error(f"报料标签来源已变化，请刷新：{error}") from error
    if current["plan_fingerprint"] != job.plan_fingerprint:
        raise Error("报料来源、产品标签设置或布局已变化，请重新加载标签")


def prepare_job(db, *, order, key, fingerprint, counts, operator_id, print_media=None):
    from types import SimpleNamespace
    from app.services.production_label_operations import (
        ProductionLabelOperationError as Error, PackagingLabelJobResult,
        _canonical_hash, _canonical_json, _load_job_package, _assert_prepared_job_is_current,
    )
    request_hash = _canonical_hash(bind_print_media(dict(supplier_order_id=order.id, fingerprint=fingerprint, source_counts=counts), print_media))
    repeated = db.scalar(select(ProductionPackagingLabelPrintJob).where(ProductionPackagingLabelPrintJob.idempotency_key == key))
    if repeated:
        if repeated.request_hash != request_hash or repeated.operator_id != operator_id:
            raise Error("幂等键已用于不同的报料标签请求")
        package = _load_job_package(repeated, db)
        _assert_prepared_job_is_current(db, repeated, package)
        return PackagingLabelJobResult(repeated, package, True)
    package = build_supplier_requisition_packaging_label_package(db, order, selected_source_keys=set(counts))
    if package.get("review_required") or not package.get("label_count"):
        raise Error("所选报料产品标签资料不完整，请核对后刷新")
    assert_current(db, SimpleNamespace(supplier_order_id=order.id, plan_fingerprint=fingerprint), package, claim=True)
    package = apply_delivery_packaging_label_print_counts(package, counts)
    package = bind_print_media(package, print_media)
    job = ProductionPackagingLabelPrintJob(supplier_order_id=order.id, idempotency_key=key,
        request_hash=request_hash, operator_id=operator_id, template_version=package["template_version"],
        plan_fingerprint=fingerprint, payload_json=_canonical_json(package), payload_hash=_canonical_hash(package), status="prepared")
    db.add(job)
    db.flush()
    for plan in package["plans"]:
        if plan.get("production_task_id") is not None:
            db.add(ProductionPackagingLabelPrintJobTask(print_job_id=job.id,
                production_task_id=plan["production_task_id"], production_task_version=plan["production_task_version"],
                product_id=plan["product_id"], product_version=plan["product_version"],
                template_version=job.template_version, snapshot_json=_canonical_json(plan)))
    db.flush()
    return PackagingLabelJobResult(job, package, False)
