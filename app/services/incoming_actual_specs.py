"""Actual receipts have their own physical contract; purchase/master facts stay frozen."""
import hashlib
import json
from decimal import Decimal
from types import SimpleNamespace
from sqlalchemy import select
from app.models.incoming_actual_spec import IncomingActualSpec
from app.models.material import Material
from app.services.sheet_cutting_contract import SheetCuttingContract


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), default=str)


def public(value):
    return {key:field for key,field in value.items() if key != "price_contract"}


def fail(message, status=409):
    from app.services.incoming_receipts import IncomingReceiptError
    raise IncomingReceiptError(message, status, code='INCOMING_ACTUAL_SPEC_INVALID')


def stored(db, receipt_id):
    row = db.get(IncomingActualSpec, receipt_id)
    return json.loads(row.actual_json) if row else None


def for_lot(db, lot):
    from app.models.incoming_receipt import IncomingReceiptItem
    receipt_id = db.scalar(select(IncomingReceiptItem.id).where(
        IncomingReceiptItem.received_inventory_lot_id == lot.id,
        IncomingReceiptItem.status == 'posted'))
    return stored(db, receipt_id) if receipt_id else None


def source(db, key):
    from app.services.incoming_receipts import _stock_item_id, _stock_target, _target
    from app.services.stock_replenishment_plan import replenishment_supplier_name
    if _stock_item_id(key) is not None:
        order, item = _stock_target(db, key)
        supplier = replenishment_supplier_name(item, order)
        return item, supplier, item.flute_type, item.material_id, item.material_code_snapshot
    target = _target(db, key, allow_closed=False)
    item = target.supplier_order_item or target.requisition_item
    if item is None:
        fail('本条来料缺少正式采购来源，不能修改实际规格')
    if getattr(item, 'purpose_contract_status', 'legacy_unset') != 'frozen':
        fail('该旧采购缺少冻结用途，请在原订单报料采购中核对该条用途后收料；原历史记录保留')
    supplier = target.supplier_order.supplier_name if target.supplier_order else target.requisition_item.requisition.supplier_name
    return item, supplier, (getattr(item, 'flute_type_snapshot', None) or target.order_item.flute_type), item.material_id, (getattr(item, 'material_code_snapshot', None) or getattr(item, 'material_code', None))


def catalog(db, key):
    from app.services.supplier_master import resolve_supplier
    from app.services.supplier_receipt_price_facts import _material_matches_supplier
    item, supplier_name, flute, mid, code = source(db, key)
    supplier = resolve_supplier(db, supplier_name, require_active=True)
    rows = [m for m in db.scalars(select(Material).where(Material.is_active.is_(True)).order_by(Material.code, Material.id))
            if _material_matches_supplier(m, supplier)]
    raw = getattr(item, 'sheet_cutting_snapshot', None)
    contract = SheetCuttingContract.from_snapshot(json.loads(raw) if isinstance(raw, str) else raw) if raw else None
    return {'materials': [{'id': m.id, 'code': m.code, 'version': m.version, 'layer_count': m.layer_count,
                           'flute_type': m.flute_type} for m in rows],
            'material_id': mid, 'material_code': code, 'flute_type': flute,
            'length_mm': str(contract.supplier_size_mm[0]) if contract else str(getattr(item, 'report_length_mm', None) or getattr(item, 'cardboard_len', '')),
            'width_mm': str(contract.supplier_size_mm[1]) if contract else str(getattr(item, 'report_width_mm', None) or getattr(item, 'cardboard_width', '')),
            'cutting_available': contract is not None}


def preview(db, key, request, user):
    from app.api.deps import has_permission
    from app.services.supplier_master import resolve_supplier
    from app.services.supplier_receipt_price_facts import _material_matches_supplier
    from app.services.material_purchase_contract import purchase_price_contract_issues
    from app.services.supplier_material_display import clean_supplier_flute_type
    item, supplier_name, flute, original_mid, code = source(db, key)
    material = db.get(Material, request['material_id'])
    supplier = resolve_supplier(db, supplier_name, require_active=True)
    if not material or not material.is_active or not _material_matches_supplier(material, supplier):
        fail('请从本供应商材质字典选择有效材质')
    if int(material.version) != int(request['material_version']):
        fail('实际材质资料已更新，请重新选择材质')
    if material.id != original_mid and not has_permission(user, 'incoming.material_variance.confirm'):
        fail('修改实际材质需要来料材质差异确认权限', 403)
    issues = purchase_price_contract_issues(quote_price=material.quote_price, price_unit=material.price_unit,
        purchase_currency=material.purchase_currency, purchase_tax_included=material.purchase_tax_included,
        purchase_tax_rate=material.purchase_tax_rate)
    if issues:
        fail('实际材质采购价格需完善：' + '；'.join(issues))
    actual_flute = clean_supplier_flute_type(None, fallback=flute)
    supported = str(material.flute_type or '').replace('／', '/').upper().split('/')
    if material.flute_type and actual_flute and actual_flute not in [s.strip() for s in supported]:
        fail('所选材质不适用本条楞型，请选择对应楞型的字典材质')
    raw = getattr(item, 'sheet_cutting_snapshot', None)
    if not raw:
        fail('原采购缺少理论净片与开料合同，请先完善该条加工依据；不能由到货数量猜开料')
    original = SheetCuttingContract.from_snapshot(json.loads(raw) if isinstance(raw, str) else raw)
    length, width = Decimal(str(request['length_mm'])), Decimal(str(request['width_mm']))
    # Keep the frozen orientation. No rotation or area-only fit is allowed.
    a, b = int(length // original.theoretical_length_mm), int(width // original.theoretical_width_mm)
    if a < 1 or b < 1:
        fail(f'实际纸板放不下理论净片 {original.theoretical_length_mm}×{original.theoretical_width_mm}mm，请核对长宽和楞向')
    if (length,width)==original.supplier_size_mm:a,b=original.length_parts,original.width_parts
    actual = SheetCuttingContract(original.theoretical_length_mm, original.theoretical_width_mm,
        length_parts=a, width_parts=b, is_die_cut=original.is_die_cut, mold_count=original.mold_count,
        actual_supplier_length_mm=length, actual_supplier_width_mm=width)
    # Source version and all cutting/price inputs are bound into a server hash.
    value = {'schema': 1, 'source_key': str(key), 'source_id': item.id,
        'source_version': getattr(item, 'version', None), 'original_cutting': original.to_snapshot(),
        'original_material_id': original_mid, 'original_material_code': code,
        'material_id': material.id, 'material_version': material.version, 'material_code': material.code,
        'layer_count': material.layer_count, 'flute_type': actual_flute,
        'length_mm': str(length), 'width_mm': str(width), 'cutting': actual.to_snapshot(),
        'original_yield': original.yield_per_supplier_sheet, 'yield': actual.yield_per_supplier_sheet,
        'price_contract': {'unit_price': str(material.quote_price), 'price_unit': material.price_unit,
            'currency': material.purchase_currency, 'tax_included': material.purchase_tax_included,
            'tax_rate': str(material.purchase_tax_rate)},
        'trim_length_mm': str(length-actual.required_supplier_size_mm[0]),
        'trim_width_mm': str(width-actual.required_supplier_size_mm[1])}
    value['fingerprint'] = hashlib.sha256(encode(value).encode()).hexdigest()
    return value


def prepare(db, key, request, user):
    if request is None:
        return None
    value = preview(db, key, request, user)
    if request.get('fingerprint') != value['fingerprint']:
        fail('实际收料预览已变化，请重新核对该行后收料')
    return value


def freeze(db, receipt, value, request, user):
    if value:
        db.add(IncomingActualSpec(receipt_item_id=receipt.id, material_id=value['material_id'],
            actor_id=user.id, request_json=encode(request), actual_json=encode(value)))
        db.flush()
        from app.services.audit_log import append_audit_event
        append_audit_event(db,event_category='business',result='success',source='web',module_code='incoming',
            action_code='incoming.actual_spec.confirmed',resource='IncomingReceiptItem',actor=user,
            entity_type='incoming_receipt_item',entity_id=receipt.id,
            details={'fingerprint':value['fingerprint'],'material_id':value['material_id'],
                'length_mm':value['length_mm'],'width_mm':value['width_mm'],'cutting':value['cutting'],
                'original_material_id':value['original_material_id'],'original_cutting':value['original_cutting']})


def replay(db, receipt, request):
    row = db.get(IncomingActualSpec, receipt.id)
    if (row.request_json if row else None) != (encode(request) if request else None):
        fail('幂等键已用于不同的实际收料规格')


def effective_item(item, value):
    if not value:
        return item
    from sqlalchemy import inspect
    fields = {c.key: getattr(item, c.key) for c in inspect(type(item)).columns}
    fields.update(material_id=value['material_id'], material_code_snapshot=value['material_code'],
        layer_count=value['layer_count'], flute_type=value['flute_type'],
        report_length_mm=Decimal(value['length_mm']), report_width_mm=Decimal(value['width_mm']),
            cardboard_len=Decimal(value['length_mm']),cardboard_width=Decimal(value['width_mm']),
        stock_yield_per_sheet=value['yield'], sheet_cutting_snapshot=encode(value['cutting']))
    return SimpleNamespace(**fields)


def decorate(db, rows):
    ids={row.get('receipt_item_id') for row in rows if row.get('receipt_item_id')}
    values={r.receipt_item_id:json.loads(r.actual_json) for r in db.scalars(select(IncomingActualSpec).where(IncomingActualSpec.receipt_item_id.in_(ids)))} if ids else {}
    for row in rows:
        rid = row.get('receipt_item_id')
        value = values.get(rid)
        if not value:
            continue
        row['original_receipt_spec'] = {'length_mm': value['original_cutting']['supplier_length_mm'],
            'width_mm': value['original_cutting']['supplier_width_mm'], 'material_code': value['original_material_code']}
        row.update(actual_spec=public(value), report_length_mm=Decimal(value['length_mm']), report_width_mm=Decimal(value['width_mm']),
            cardboard_len=Decimal(value['length_mm']),cardboard_width=Decimal(value['width_mm']),
            actual_material_code=value['material_code'], material_code=value['material_code'], material=value['material_code'],
            flute_type=value['flute_type'], material_display=value['material_code']+' / '+value['flute_type'],
            actual_material_display=value['material_code']+' / '+value['flute_type'], cutting_mode=str(value['cutting']['cutting_factor']))


def receipt_yield(db, receipt_id, default):
    value = stored(db, receipt_id)
    return int(value['yield']) if value else int(default)


def source_received_pieces(db, target, current):
    from app.models.incoming_receipt import IncomingReceiptItem
    from app.services.incoming_receipts import _source_filter
    default = int(current['original_yield'])
    return sum(r.received_quantity*receipt_yield(db,r.id,default) for r in db.scalars(
        select(IncomingReceiptItem).where(_source_filter(target),IncomingReceiptItem.status=='posted')))


def effective_context(context, value):
    if not value:
        return context
    from dataclasses import replace
    from sqlalchemy import inspect
    fact = context.receipt_fact
    fields = {c.key: getattr(fact,c.key) for c in inspect(type(fact)).columns}
    price = value['price_contract']
    fields.update(actual_material_id=value['material_id'],actual_material_version=value['material_version'],
        actual_material_code_snapshot=value['material_code'],actual_material_layer_count_snapshot=value['layer_count'],
        actual_material_flute_type_snapshot=value['flute_type'],unit_price=Decimal(price['unit_price']),
        price_unit=price['price_unit'],currency=price['currency'],tax_included=price['tax_included'],tax_rate=Decimal(price['tax_rate']))
    return replace(context,receipt_fact=SimpleNamespace(**fields))


def piece_corrections(db, sheet_counts, current=None):
    from app.services.receipt_purpose_distribution import _active_allocation_statement
    from app.models.purchase_receipt import IncomingReceiptPurposeAllocation
    result = {}; remaining = dict(sheet_counts)
    allocations=list(db.scalars(_active_allocation_statement().where(IncomingReceiptPurposeAllocation.purchase_purpose_source_snapshot_id.in_(sheet_counts)).order_by(IncomingReceiptPurposeAllocation.id)))
    ids=[a.incoming_receipt_item_id for a in allocations]
    values={r.receipt_item_id:json.loads(r.actual_json) for r in db.scalars(select(IncomingActualSpec).where(IncomingActualSpec.receipt_item_id.in_(ids)))} if ids else {}
    for allocation in allocations:
        sid=allocation.purchase_purpose_source_snapshot_id
        if sid not in remaining:continue
        take=min(max(int(remaining[sid]),0),allocation.receipt_order_purpose_sheet_qty)
        remaining[sid]-=take
        value=values.get(allocation.incoming_receipt_item_id)
        if value:result[sid]=result.get(sid,0)+take*(value['yield']-value['original_yield'])
    if current:
        sid,receipt_id,quantity=current;value=stored(db,receipt_id)
        if value:result[sid]=result.get(sid,0)+quantity*(value['yield']-value['original_yield'])
    return result
