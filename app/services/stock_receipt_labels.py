"""Labels for actual replenishment output, anchored to its real purchase order.

No synthetic sales orders or ProductionTasks; the existing immutable print-job
payload holds receipt and preparation identities alongside the supplier FK.
"""
from app.services.product_label_media import bind_print_media
import json
from sqlalchemy import select
from app.models.incoming_receipt import IncomingReceiptItem
from app.models.stock_replenishment import StockReplenishmentOrderItem
from app.models.stock_preparation import StockPreparationJob, StockPreparationCommand
from app.models.supplier_requisition_order import SupplierRequisitionOrderItem, SupplierRequisitionOrder
from app.models.product import Product
from app.models.customer import Customer
from app.models.production_label_print import ProductionPackagingLabelPrintJob
from app.models.warehouse_inventory import InventoryLot
from app.services.production_packaging_label import (
    _customer_label_fields, template_dimensions, apply_delivery_packaging_label_print_counts,
)
from app.services.production_packaging_label_layout import effective_layout
from app.services.production_label_strategy import build_new_task_production_label_snapshot, CURRENT_PRODUCTION_LABEL_TEMPLATE_VERSION
from app.services.production_label_operations import (
    ProductionLabelOperationError as Error, PackagingLabelJobResult,
    _canonical_hash, _canonical_json, _load_job_package, _assert_prepared_job_is_current,
    _claim_product_version, _claim_customer_version,
)


SOURCE = 'stock_receipt'


def receipt_source(db, receipt_id):
    receipt = db.get(IncomingReceiptItem, receipt_id)
    item = db.get(StockReplenishmentOrderItem, receipt.stock_replenishment_item_id) if receipt and receipt.stock_replenishment_item_id else None
    if item is None:
        raise Error('补库实收记录不存在', 404)
    return receipt, item


def build_package(db, receipt_id):
    receipt, item = receipt_source(db, receipt_id)
    if receipt.status != 'posted' or receipt.receipt.status != 'posted':
        raise Error('已撤销实收不能生成新的产品标签')
    sources = list(db.scalars(select(SupplierRequisitionOrderItem).join(SupplierRequisitionOrder).where(
        SupplierRequisitionOrderItem.source_key == f'stock_replenishment:{item.id}',
        SupplierRequisitionOrderItem.status == 'active', SupplierRequisitionOrder.status == 'confirmed')))
    if len(sources) != 1:
        raise Error('补库实收缺少唯一有效采购来源，请核对原采购单')
    source = sources[0]
    customer = db.get(Customer, item.customer_id)
    if customer is None:
        raise Error('补库实收缺少客户归属')
    jobs = list(db.scalars(select(StockPreparationJob).where(
        StockPreparationJob.receipt_item_id == receipt.id,
        StockPreparationJob.status == 'completed').order_by(StockPreparationJob.id)))
    if not jobs:
        raise Error('本次实收尚无已完成的成品加工记录，不能按纸板张数打印成品标签')
    template = CURRENT_PRODUCTION_LABEL_TEMPLATE_VERSION
    fields = _customer_label_fields(template, customer.chinese_short_name)
    plans, labels, evidence, excluded = [], [], [], []
    finished_commands = []
    for command in db.scalars(select(StockPreparationCommand).where(StockPreparationCommand.receipt_item_id == receipt.id)):
        result = json.loads(command.result_json)
        if result.get('action') == 'finish_output':
            finished_commands.append((command.operation_key, result))
    for job in jobs:
        if job.actual_output == 0:
            continue
        product = db.get(Product, job.product_id)
        lot = db.get(InventoryLot, job.output_lot_id) if job.output_lot_id else None
        if product is None or product.customer_id != customer.id or lot is None or job.actual_output <= 0 or lot.source_ref_type != 'stock_preparation' or lot.source_ref_id != job.id:
            raise Error('补库成品身份、客户归属或实际产出依据不完整')
        finish_evidence = []
        total_quantity = job.actual_output if lot.inventory_type == 'finished' else 0
        if lot.inventory_type != 'finished':
            seen = set()
            for key, result in finished_commands:
                if result.get('job_id') != job.id:
                    continue
                finished = db.get(InventoryLot, result.get('output_lot_id'))
                if (result.get('source_lot_id') != lot.id or result.get('product_id') != product.id
                        or result.get('customer_id') != customer.id or not finished
                        or finished.inventory_type != 'finished' or finished.source_ref_type != 'stock_preparation_finish'
                        or finished.source_ref_id != lot.id or finished.id in seen
                        or type(result.get('quantity')) is not int or result['quantity'] <= 0):
                    raise Error('补库转成品记录的数量或来源不一致')
                seen.add(finished.id)
                total_quantity += result['quantity']
                finish_evidence.append(dict(operation_key=key, result_hash=_canonical_hash(result)))
        if total_quantity <= 0:
            excluded.append(dict(product_code=product.product_code, reason='本批仍是子件或半成品，尚无成品入库依据'))
            continue
        if total_quantity > job.actual_output:
            raise Error('补库成品数量超过加工产出，已停止打印')
        snapshot = json.loads(job.product_snapshot)
        basis = snapshot.get('physical_basis') or '{}'
        basis = json.loads(basis) if isinstance(basis, str) else basis
        if snapshot.get('product_id') != product.id or not basis.get('spec') or not basis.get('unit'):
            raise Error('补库成品冻结规格或单位缺失，请核对加工记录')
        evidence.append(dict(id=job.id, version=job.version, product_id=product.id,
                             product_version=product.version, output_lot_id=job.output_lot_id,
                             actual_output=job.actual_output, finished_quantity=total_quantity, finish_evidence=finish_evidence, snapshot_hash=_canonical_hash(snapshot)))
        strategy = build_new_task_production_label_snapshot(product, total_quantity=total_quantity)
        if not strategy['production_label_enabled_snapshot']:
            excluded.append(dict(product_code=snapshot.get('code'), reason='常用箱未启用打印标签'))
            continue
        units = strategy['production_label_units_per_label_snapshot']
        count = strategy['production_label_count_snapshot']
        if count > 10000 or len(labels) + count > 10000:
            raise Error('本次标签超过10000张，请先核对每张标签数量')
        quantities = [min(units, total_quantity - i * units) for i in range(count)]
        plan = dict(selection_key=f'stock-receipt:{receipt.id}:preparation:{job.id}',
                    receipt_item_id=receipt.id, stock_preparation_job_id=job.id,
                    product_id=product.id, product_version=product.version,
                    customer_id=customer.id, customer_name=customer.name, customer_code=customer.customer_code,
                    **fields, product_code=snapshot.get('code'), product_name=snapshot.get('name'),
                    specification=basis['spec'], unit_label=basis['unit'], total_quantity=total_quantity,
                    units_per_label=units, label_count=count, label_quantities=quantities,
                    template_version=template)
        plans.append(plan)
        labels.extend(dict(plan, quantity=q, label_number=i) for i, q in enumerate(quantities, 1))
    from app.services.customer_label_profile import freeze_product_label_profiles
    freeze_product_label_profiles(db, plans, labels)
    package = dict(source_type=SOURCE, receipt_item_id=receipt.id, receipt_number=receipt.receipt.receipt_number,
                   stock_replenishment_item_id=item.id, supplier_order_id=source.supplier_order_id,
                   supplier_order_item_id=source.id, supplier_item_version=source.version,
                   received_quantity=receipt.received_quantity, customer_id=customer.id,
                   customer_version=customer.version, template_version=template,
                   template_dimensions=template_dimensions(template), source_evidence=evidence,
                   plans=plans, labels=labels, label_count=len(labels), print_plan_count=len(plans),
                   excluded_items=excluded, label_layout=effective_layout(db),
                   review_required=False, review_messages=[], printable=bool(labels))
    package['plan_fingerprint'] = _canonical_hash(package)
    return package


def validate_frozen_source(job, package, links):
    plans = package.get('plans') or []
    keys = {p.get('selection_key') for p in plans}
    receipt_id = int(package.get('receipt_item_id') or 0)
    if (links or job.delivery_id or job.material_requisition_id or not job.supplier_order_id
            or job.supplier_order_id != package.get('supplier_order_id') or receipt_id <= 0
            or package.get('plan_fingerprint') != job.plan_fingerprint
            or not plans or len(keys) != len(plans)
            or any(p.get('production_task_id') is not None or p.get('receipt_item_id') != receipt_id
                   or p.get('selection_key') != f"stock-receipt:{receipt_id}:preparation:{p.get('stock_preparation_job_id')}" for p in plans)
            or any(label.get('selection_key') not in keys for label in package.get('labels') or [])):
        raise Error('补库标签作业来源校验失败，已停止读取或补打')


def assert_current(db, job, package, *, claim=False):
    if claim:
        # Claim existing versioned master rows, then re-read all source facts.
        # SQLite's writer claim serializes receipt reversal without touching
        # immutable receipt rows or bypassing ReceiptPriceGuardSession.
        _claim_customer_version(db, customer_id=package['customer_id'], expected_version=package['customer_version'])
        for e in package['source_evidence']:
            _claim_product_version(db, product_id=e['product_id'], expected_version=e['product_version'])
        db.expire_all()
    current = build_package(db, package['receipt_item_id'])
    if current['plan_fingerprint'] != job.plan_fingerprint:
        raise Error('补库实收、加工记录、常用箱标签设置或布局已变化，请重新加载标签')


def prepare_job(db, *, receipt_id, key, fingerprint, counts, operator_id, print_media=None):
    request_hash = _canonical_hash(bind_print_media(dict(source_type=SOURCE, receipt_id=receipt_id,
                                       fingerprint=fingerprint, counts=counts), print_media))
    repeated = db.scalar(select(ProductionPackagingLabelPrintJob).where(
        ProductionPackagingLabelPrintJob.idempotency_key == key))
    if repeated is not None:
        if repeated.request_hash != request_hash or repeated.operator_id != operator_id:
            raise Error('幂等键已用于不同的补库标签请求')
        package = _load_job_package(repeated, db)
        _assert_prepared_job_is_current(db, repeated, package)
        return PackagingLabelJobResult(repeated, package, True)
    package = build_package(db, receipt_id)
    if not package['label_count']:
        raise Error('本次补库成品未启用标签，请到常用箱打开“打印标签”并保存')
    from types import SimpleNamespace
    assert_current(db, SimpleNamespace(plan_fingerprint=fingerprint), package, claim=True)
    package = apply_delivery_packaging_label_print_counts(package, counts)
    package = bind_print_media(package, print_media)
    job = ProductionPackagingLabelPrintJob(supplier_order_id=package['supplier_order_id'],
        idempotency_key=key, request_hash=request_hash, operator_id=operator_id,
        template_version=package['template_version'], plan_fingerprint=fingerprint,
        payload_json=_canonical_json(package), payload_hash=_canonical_hash(package), status='prepared')
    db.add(job)
    db.flush()
    return PackagingLabelJobResult(job, package, False)
