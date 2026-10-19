"""Labels follow the completed lot identity, never the requisition's BOM expansion."""
from datetime import datetime, timezone
import json
from sqlalchemy import select, update
from app.models.finished_product_label import FinishedProductLabelPrintJob as Job
from app.models.warehouse_inventory import InventoryLot
from app.models.customer import Customer
from app.models.product import Product
from app.services.production_label_operations import ProductionLabelOperationError as Error, _canonical_hash, _canonical_json
from app.services.production_packaging_label import _customer_label_fields, template_dimensions, apply_delivery_packaging_label_print_counts
from app.services.production_label_strategy import CURRENT_PRODUCTION_LABEL_TEMPLATE_VERSION, build_new_task_production_label_snapshot
from app.services.production_packaging_label_layout import effective_layout
from app.services.customer_label_profile import freeze_product_label_profiles
from app.services.product_stock_origins import production_lot_ids
from app.services.warehouse_display_units import lot_display_unit
from app.services.warehouse_location_address import employee_location_name


SOURCE = 'finished_stock'


def read_job(db, job_id):
    job = db.get(Job, job_id)
    if job is None:
        raise Error('成品标签作业不存在', 404)
    try:
        package = json.loads(job.payload_json)
        sources = package['lot_versions']
        plans = package['plans']
        keys = {p['selection_key'] for p in plans}
        valid = (package['source_type'] == SOURCE and _canonical_hash(package) == job.payload_hash
                 and package['plan_fingerprint'] == job.plan_fingerprint
                 and sources and sources[0]['lot_id'] == job.inventory_lot_id
                 and len(keys) == len(plans) and len(package['labels']) == package['label_count']
                 and all(p['selection_key'] == f"finished-lot:{p['inventory_lot_id']}" for p in plans)
                 and all(l['selection_key'] in keys for l in package['labels']))
    except (ValueError, TypeError, KeyError):
        valid = False
    if not valid:
        raise Error('成品标签冻结内容校验失败，已停止打印')
    return job, package


def build_package(db, selections):
    ids = [int(s['lot_id']) for s in selections]
    if not ids or len(ids) > 200 or len(set(ids)) != len(ids):
        raise Error('请选择本页不重复的成品批次')
    selections = sorted(selections, key=lambda s: int(s['lot_id']))
    lots = {lot.id: lot for lot in db.scalars(select(InventoryLot).where(
        InventoryLot.id.in_(ids), InventoryLot.id.in_(production_lot_ids())).with_for_update())}
    if set(lots) != set(ids):
        raise Error('所选记录已变化或不属于生产成品，请刷新后重新选择')
    plans, labels, evidence = [], [], []
    template = CURRENT_PRODUCTION_LABEL_TEMPLATE_VERSION
    for selection in selections:
        lot = lots[int(selection['lot_id'])]
        detail = lot.finished_detail
        if lot.version != int(selection['expected_version']):
            raise Error('所选成品库存已变化，请刷新后重新勾选')
        if (lot.inventory_type != 'finished' or lot.status != 'active' or not detail
                or lot.quantity_damaged or lot.quantity_scrapped):
            raise Error('所选记录包含非成品、冻结或报损批次，请先核对库存')
        total = lot.quantity_available + lot.quantity_reserved
        if total <= 0:
            raise Error('所选批次已无现存成品，请刷新')
        product = db.get(Product, detail.product_id)
        customer = db.get(Customer, detail.owner_customer_id)
        if not product or not customer or product.customer_id != customer.id:
            raise Error('所选成品产品身份或客户归属不完整，请核对')
        prefix = detail.inventory_code_snapshot or product.product_code
        if not product.production_label_enabled:
            raise Error(f'{prefix} 未启用打印标签，请先到常用箱启用并保存；本批未打印')
        try:
            basis = json.loads(detail.physical_basis_json or '{}')
        except (ValueError, TypeError):
            basis = {}
        if not isinstance(basis, dict) or basis.get('product_id') != product.id or not basis.get('unit'):
            raise Error(f'{prefix} 缺少已核实的入库实物身份/单位，请先在库存详情核对')
        spec = basis.get('spec') or ('成套产品（按入库配方）' if basis.get('assembly') else '')
        if not spec or not detail.inventory_code_snapshot or not detail.product_name_snapshot:
            raise Error(f'{prefix} 缺少入库冻结规格、编码或名称，请先核对')
        strategy = build_new_task_production_label_snapshot(product, total_quantity=total)
        units, count = strategy['production_label_units_per_label_snapshot'], strategy['production_label_count_snapshot']
        if count > 10000 or len(labels) + count > 10000:
            raise Error('本次标签超过10000张，请核对每张数量或减少选择')
        quantities = [min(units, total - i * units) for i in range(count)]
        plan = dict(selection_key=f'finished-lot:{lot.id}', inventory_lot_id=lot.id,
            inventory_version=lot.version, product_id=product.id, product_version=product.version,
            customer_id=customer.id, customer_name=customer.name, customer_code=customer.customer_code,
            **_customer_label_fields(template, customer.chinese_short_name),
            product_code=detail.inventory_code_snapshot, product_name=detail.product_name_snapshot,
            specification=spec, unit_label=lot_display_unit(lot), total_quantity=total,
            units_per_label=units, label_count=count, label_quantities=quantities, template_version=template,
            warehouse_location_name=employee_location_name(lot.location),
            fulfillment_mode='assembled' if basis.get('assembly') else 'physical_product')
        plans.append(plan)
        labels.extend(dict(plan, quantity=q, label_number=i) for i, q in enumerate(quantities, 1))
        evidence.append(dict(lot_id=lot.id, expected_version=lot.version, product_id=product.id,
            product_version=product.version, customer_id=customer.id, customer_version=customer.version,
            total_quantity=total, warehouse_location_id=lot.warehouse_location_id,
            physical_basis_hash=_canonical_hash(basis)))
    freeze_product_label_profiles(db, plans, labels)
    package = dict(source_type=SOURCE, status_label='已入库成品标签', lot_versions=evidence,
        customer_ids=sorted({p['customer_id'] for p in plans}),
        template_version=template, template_dimensions=template_dimensions(template),
        label_layout=effective_layout(db), plans=plans, labels=labels, label_count=len(labels),
        print_plan_count=len(plans), review_required=False, review_messages=[], printable=bool(labels))
    package['plan_fingerprint'] = _canonical_hash(package)
    return package


def selections_from(package):
    return [dict(lot_id=e['lot_id'], expected_version=e['expected_version']) for e in package['lot_versions']]


def assert_current(db, package):
    db.expire_all()
    current = build_package(db, selections_from(package))
    if current['plan_fingerprint'] != package['plan_fingerprint']:
        raise Error('库存、产品标签设置、客户资料或布局已变化，请重新加载标签')


def response(job, package, *, replayed=False):
    return dict(job_id=job.id, job=dict(id=job.id, status=job.status,
        printed_at=job.printed_at.isoformat() if job.printed_at else None,
        template_version=package['template_version']), package=package, replayed=replayed)


def prepare(db, *, selections, key, fingerprint, counts, operator_id, reprint_confirmed=False):
    request_hash = _canonical_hash(dict(source_type=SOURCE, selections=sorted(selections, key=lambda s:s['lot_id']),
        fingerprint=fingerprint, counts=counts, reprint_confirmed=reprint_confirmed))
    repeated = db.scalar(select(Job).where(Job.idempotency_key == key))
    if repeated is not None:
        if repeated.request_hash != request_hash or repeated.operator_id != operator_id:
            raise Error('幂等键已用于不同的成品标签请求')
        job, package = read_job(db, repeated.id)
        if job.status == 'prepared':
            assert_current(db, package)
        return job, package, True
    package = build_package(db, selections)
    if package['plan_fingerprint'] != fingerprint:
        raise Error('标签计划已变化，请重新加载')
    frozen = apply_delivery_packaging_label_print_counts(package, counts)
    frozen['reprint_confirmed'] = reprint_confirmed
    job = Job(inventory_lot_id=package['lot_versions'][0]['lot_id'], idempotency_key=key,
        request_hash=request_hash, plan_fingerprint=fingerprint, payload_json=_canonical_json(frozen),
        payload_hash=_canonical_hash(frozen), operator_id=operator_id, status='prepared')
    db.add(job)
    db.flush()  # SQLite writer claim: serialize stock/master edits before final re-read.
    assert_current(db, package)
    prior = latest_printed(db, selections)
    if prior and not reprint_confirmed:
        raise Error('所选成品已有实际打印记录，请确认补打后继续')
    return job, frozen, False


def latest_printed(db, selections):
    ids = {int(s['lot_id']) for s in selections}
    for job in db.scalars(select(Job).where(Job.status == 'printed').order_by(Job.id.desc())):
        _, package = read_job(db, job.id)
        if ids.intersection(p['inventory_lot_id'] for p in package['plans']):
            return dict(id=job.id, printed_at=job.printed_at.isoformat(), template_version=package['template_version'])
    return None


def confirm(db, *, job_id, key, operator_id):
    job, package = read_job(db, job_id)
    if job.status == 'printed':
        if job.printed_confirmation_key == key and job.printed_by_user_id == operator_id:
            return job, package, True
        raise Error('该作业已登记实际打印，请刷新查看原记录')
    if db.scalar(select(Job.id).where(Job.printed_confirmation_key == key)) is not None:
        raise Error('该确认凭证已用于其他成品标签作业')
    result = db.execute(update(Job).where(Job.id == job_id, Job.status == 'prepared').values(
        status='printed', printed_confirmation_key=key, printed_by_user_id=operator_id,
        printed_at=datetime.now(timezone.utc).replace(tzinfo=None)))
    if result.rowcount != 1:
        raise Error('打印状态已变化，请刷新')
    assert_current(db, package)
    if latest_printed_except(db, package, job_id) and not package.get('reprint_confirmed'):
        raise Error('所选成品已被其他作业打印，请重新核对并确认补打')
    return db.get(Job, job_id), package, False


def latest_printed_except(db, package, job_id):
    ids = {p['inventory_lot_id'] for p in package['plans']}
    for job in db.scalars(select(Job).where(Job.status == 'printed', Job.id != job_id)):
        _, frozen = read_job(db, job.id)
        if ids.intersection(p['inventory_lot_id'] for p in frozen['plans']):
            return True
    return False
