"""One reviewed 000120 correction; caller owns commit and delivery replacement.

Only fifty pieces split from lot666 receive a new, explicitly confirmed basis.
The original lot keeps its missing historical basis and reference cost.
"""
import hashlib
import json
from decimal import Decimal

from sqlalchemy import select, update

from app.models.finance import FinanceIdempotencyRecord
from app.models.delivery import Delivery, DeliveryItem
from app.models.multilevel_bom import BomAssemblyInput
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.warehouse_inventory import InventoryLot, InventoryReservation, UnorderedFinishedDeliveryAllocation
from app.services.audit_log import append_audit_event
from app.services.bom_subkit_costs import source_cost
from app.services.bom_transactions import atomic_bom
from app.services.finished_stock_identity import product_basis, compiled_product_bases, identity_preview, confirm_identity
from app.services.multilevel_bom_inventory import assemble_order_inventory
from app.services.multilevel_bom_orders import read_compiled_order_bom
from app.services.warehouse_goods_correction import split_for_correction
from app.services.warehouse_inventory import WarehouseInventoryError

ORDER_ITEM_ID = 10552
CUSTOMER_ID = 136
PARENT_ID = 3863
LOWER_ID = 3496
UPPER_ID = 3787
LOWER_LOT = 666
UPPER_LOT = 1421
LOCATION_ID = 1918
QUANTITY = 50
ACTION = 'emergency_kit_correction_20261014'


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), default=str)


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _fail(message, code=409):
    raise WarehouseInventoryError(message, code)


def _authorize(db, actor):
    if actor is None or not actor.is_active or actor.role != 'admin':
        _fail('仅活动管理员可执行本次有限组套更正', 403)
    from app.api.deps import require_customer_access
    require_customer_access(CUSTOMER_ID, actor, db)


def _scope(order_item_id, quantity, target_location_id):
    if (type(order_item_id) is not int or order_item_id != ORDER_ITEM_ID
            or type(quantity) is not int or quantity != QUANTITY
            or type(target_location_id) is not int or target_location_id != LOCATION_ID):
        _fail('本服务仅限订单10552、上下盖各50、原位1918组成50套', 400)


def _source_document(lot):
    detail = lot.finished_detail
    return {
        'id': lot.id, 'version': lot.version, 'location_id': lot.warehouse_location_id,
        'available': lot.quantity_available, 'reserved': lot.quantity_reserved,
        'consumed': lot.quantity_consumed, 'damaged': lot.quantity_damaged,
        'scrapped': lot.quantity_scrapped, 'status': lot.status, 'unit': lot.unit,
        'source_type': lot.source_type, 'source_ref_type': lot.source_ref_type,
        'source_ref_id': lot.source_ref_id, 'stock_date': str(lot.stock_date),
        'finished_detail': {c.name: getattr(detail, c.name) for c in detail.__table__.columns
                            if c.name != 'inventory_lot_id'},
        'cost': {
            'unit_cost': str(lot.estimated_unit_cost_snapshot),
            'square_price': str(lot.estimated_square_price_snapshot),
            'area_m2': str(lot.estimated_cost_area_m2_snapshot),
            'source': lot.cost_snapshot_source, 'detail_hash': _digest(lot.cost_snapshot_detail_json),
            'captured_at': str(lot.cost_snapshot_at),
        },
    }


def _planned_references(db):
    return [{'allocation_id': a.id, 'line_id': line.id, 'lot_id': a.inventory_lot_id,
             'quantity': a.planned_quantity, 'status': a.status}
            for a, line in db.execute(select(UnorderedFinishedDeliveryAllocation, DeliveryItem)
                .join(DeliveryItem, DeliveryItem.id == UnorderedFinishedDeliveryAllocation.delivery_item_id)
                .join(Delivery, Delivery.id == DeliveryItem.delivery_id)
                .where(UnorderedFinishedDeliveryAllocation.inventory_lot_id.in_((LOWER_LOT, UPPER_LOT)),
                       UnorderedFinishedDeliveryAllocation.status == 'planned',
                       DeliveryItem.is_current.is_(True), Delivery.status == 'pending'))]


def preview(db, *, actor, order_item_id=ORDER_ITEM_ID, quantity=QUANTITY, target_location_id=LOCATION_ID):
    """Read a versioned proposal; no stock or history is changed."""
    _authorize(db, actor)
    _scope(order_item_id, quantity, target_location_id)
    item = db.get(OrderItem, ORDER_ITEM_ID)
    order = db.get(Order, item.order_id) if item else None
    if (not item or not order or item.product_id != PARENT_ID or order.customer_id != CUSTOMER_ID
            or item.quantity != QUANTITY or item.is_force_closed or item.delivered_quantity
            or order.status in {'completed', 'archived', 'closed', 'dead', 'cancelled', 'delivered'}):
        _fail('本次订单身份、数量、送货或结束状态已变化')
    compiled = read_compiled_order_bom(db, item.id)
    if (compiled is None or compiled.graph.root_id != PARENT_ID
            or {(e.parent_id, e.child_id, e.quantity, e.relation) for e in compiled.graph.edges}
            != {(PARENT_ID, LOWER_ID, 1, 'assembly'), (PARENT_ID, UPPER_ID, 1, 'assembly')}):
        _fail('订单冻结配方不再是上下盖各一件')
    bases = compiled_product_bases(compiled)
    products = [db.get(Product, pid) for pid in (PARENT_ID, LOWER_ID, UPPER_ID)]
    for product in products:
        if (product is None or product.deleted_at or not product.is_active
                or product.customer_id != CUSTOMER_ID or product_basis(product) != bases[product.id]):
            _fail('当前产品与本订单冻结依据不完全相等，不能确认拆分批次')
    lots = [db.get(InventoryLot, lid) for lid in (LOWER_LOT, UPPER_LOT)]
    for lot, pid in zip(lots, (LOWER_ID, UPPER_ID)):
        if (lot is None or lot.inventory_type != 'finished' or not lot.finished_detail
                or lot.finished_detail.product_id != pid or lot.finished_detail.owner_customer_id != CUSTOMER_ID
                or lot.finished_detail.is_general or lot.status != 'active' or lot.unit != 'boxes'
                or lot.warehouse_location_id != LOCATION_ID or lot.quantity_available != 210
                or any((lot.quantity_reserved, lot.quantity_consumed, lot.quantity_damaged, lot.quantity_scrapped))):
            _fail('来源库存身份、原位210件余额、预占或消费状态已变化')
    if lots[0].version != 1 or lots[0].finished_detail.physical_basis_json is not None:
        _fail('下盖原批不再是version1且缺历史basis的批准范围')
    if lots[1].finished_detail.physical_basis_json != bases[UPPER_ID]:
        _fail('上盖冻结依据与本订单不完全一致')
    outstanding = db.scalar(select(InventoryReservation.id).where(
        InventoryReservation.inventory_lot_id.in_((LOWER_LOT, UPPER_LOT)),
        InventoryReservation.status != 'cancelled',
        InventoryReservation.reserved_stock_quantity >
        InventoryReservation.consumed_stock_quantity + InventoryReservation.released_stock_quantity))
    if outstanding:
        _fail('来源批次仍有有效预占，不能用于本次有限更正')
    planned = _planned_references(db)
    if any(r['line_id'] != 1027 or r['lot_id'] != LOWER_LOT or r['quantity'] != QUANTITY for r in planned):
        _fail('来源批次有本次目标之外的有效待送引用')
    from app.services.fixed_shelf_staging import staging_owner
    if any(staging_owner(db, lot.id) for lot in lots):
        _fail('来源库存已实际集货，必须沿原集货流程处理，不能伪造恢复')
    amounts = [source_cost(db, lot, QUANTITY)[0] for lot in lots]
    plan = assemble_order_inventory(db, order_item_id=item.id,
        source_lot_versions={lot.id: lot.version for lot in lots}, target_locations={PARENT_ID: LOCATION_ID},
        operation_key='emergency-kit-preview', operator_id=actor.id,
        available_lot_ids=[LOWER_LOT, UPPER_LOT], preview_only=True, expected_outputs={PARENT_ID: QUANTITY})
    if plan != {PARENT_ID: QUANTITY}:
        _fail('本订单已满足或可组套数量不再是50')
    result = {
        'schema': 1, 'order_item_id': item.id, 'order_id': order.id, 'customer_id': CUSTOMER_ID,
        'quantity': QUANTITY, 'target_location_id': LOCATION_ID, 'actor_id': actor.id,
        'order_state_hash': _digest({c.name: getattr(item, c.name) for c in item.__table__.columns}),
        'order_status': order.status,
        'products': [{'id': p.id, 'version': p.version, 'current_basis': product_basis(p)} for p in products],
        'expected_bases': bases, 'sources': [_source_document(lot) for lot in lots],
        'source_reference_costs': [str(a) for a in amounts], 'total_reference_cost': str(sum(amounts, Decimal(0))),
        'reference_unit_cost': str(sum(amounts, Decimal(0)) / QUANTITY),
        'target_planned_references': planned, 'expected_outputs': {PARENT_ID: QUANTITY},
        'uncertainties': ['下盖原批历史basis缺失；仅用户确认的本次50件拆分后记录依据，剩余160不确认',
                          '继承已有参考成本，不提升为实际采购现金成本；本服务不改送货',
                          '旧1027 planned记录必须保留；执行前由外层退休目标待送行'],
    }
    result['preview_hash'] = _digest(result)
    return result


def _replay(db, object_ref, request):
    record = db.scalar(select(FinanceIdempotencyRecord).where(FinanceIdempotencyRecord.idempotency_key == object_ref))
    if record is None:
        return None
    if (record.request_hash != _digest(request) or record.action != ACTION
            or record.actor_user_id != request['actor_id'] or record.resource_type != 'order_item'
            or record.resource_id != ORDER_ITEM_ID):
        _fail('本次更正标识已用于不同确认或管理员')
    try:
        value = json.loads(record.response_json)
        receipt = value['receipt']
        if (not isinstance(receipt, dict) or receipt.get('order_item_id') != ORDER_ITEM_ID
                or receipt.get('quantity') != QUANTITY or receipt.get('location_id') != LOCATION_ID):
            _fail('本次更正完整回执的范围不一致')
    except (ValueError, KeyError, TypeError):
        _fail('本次更正完整回执损坏，不能再次执行')
    if value.get('request') != request:
        _fail('本次更正回执的完整确认资料不一致')
    return {**receipt, 'replayed': True}


def execute(db, *, actor, preview_hash, operation_key, evidence, physical_match_confirmed,
            order_item_id=ORDER_ITEM_ID, quantity=QUANTITY, target_location_id=LOCATION_ID):
    """All changes and audit are atomic; never commit the caller's transaction."""
    _authorize(db, actor)
    _scope(order_item_id, quantity, target_location_id)
    if (physical_match_confirmed is not True or not isinstance(evidence, str)
            or not evidence.strip() or len(evidence) > 1000):
        _fail('本次50套须有明确实物用途确认和依据')
    if not isinstance(operation_key, str) or not operation_key.strip() or len(operation_key) > 64:
        _fail('有限更正操作标识须为1到64字符', 400)
    operation_key = operation_key.strip()
    request = {'preview_hash': preview_hash, 'operation_key': operation_key, 'evidence': evidence.strip(),
               'actor_id': actor.id, 'order_item_id': ORDER_ITEM_ID, 'quantity': QUANTITY, 'location_id': LOCATION_ID}
    object_ref = 'emergency-kit:' + _digest(operation_key)
    replayed = _replay(db, object_ref, request)
    if replayed:
        return replayed
    with atomic_bom(db):
        # Serialize identical and different keys on the real order, then replay
        # again after the lock. No version or quantity is changed by this claim.
        order_id = db.scalar(select(OrderItem.order_id).where(OrderItem.id == ORDER_ITEM_ID))
        db.execute(update(Order).where(Order.id == order_id).values(updated_at=Order.updated_at))
        replayed = _replay(db, object_ref, request)
        if replayed:
            return replayed
        db.expire_all()
        value = preview(db, actor=actor)
        if value['preview_hash'] != preview_hash:
            _fail('批次、产品、订单或来源成本已变化，请重新预览')
        if value['target_planned_references']:
            _fail('请先在同一外层事务退休旧目标待送行，保留原planned记录')
        for product in value['products']:
            claimed = db.execute(update(Product).where(Product.id == product['id'], Product.version == product['version'])
                                 .values(version=Product.version, updated_at=Product.updated_at))
            if claimed.rowcount != 1:
                _fail('产品版本已变化，请重新预览')
        for source in value['sources']:
            claimed = db.execute(update(InventoryLot).where(InventoryLot.id == source['id'], InventoryLot.version == source['version'])
                                 .values(version=InventoryLot.version, updated_at=InventoryLot.updated_at))
            if claimed.rowcount != 1:
                _fail('来源库存版本已变化，请重新预览')
        lower, upper = db.get(InventoryLot, LOWER_LOT), db.get(InventoryLot, UPPER_LOT)
        child = split_for_correction(db, lower, QUANTITY, actor, _digest(operation_key + ':split'))
        child_preview = identity_preview(db, child.id)
        if _json(child_preview['physical_basis']) != value['expected_bases'][LOWER_ID]:
            _fail('拆分确认依据不再等于原订单冻结下盖')
        confirm_identity(db, lot_id=child.id, preview_hash=child_preview['preview_hash'],
                         operation_key=_digest(operation_key + ':identity'), actor=actor)
        rows = assemble_order_inventory(db, order_item_id=ORDER_ITEM_ID,
            source_lot_versions={child.id: child.version, upper.id: upper.version},
            target_locations={PARENT_ID: LOCATION_ID}, operation_key='emergency-kit:' + _digest(operation_key),
            operator_id=actor.id, available_lot_ids=[child.id, upper.id], expected_outputs={PARENT_ID: QUANTITY})
        if len(rows) != 1 or rows[0].quantity != QUANTITY or rows[0].output_product_id != PARENT_ID:
            _fail('本次组套输出不完整')
        assembly = rows[0]
        inputs = list(db.scalars(select(BomAssemblyInput).where(BomAssemblyInput.conversion_id == assembly.id)))
        if {(r.lot_id, r.product_id, r.quantity) for r in inputs} != {(child.id, LOWER_ID, QUANTITY), (UPPER_LOT, UPPER_ID, QUANTITY)}:
            _fail('组套输入来源或数量不守恒')
        db.refresh(lower); db.refresh(upper); db.refresh(child)
        output = db.get(InventoryLot, assembly.output_lot_id)
        if (lower.quantity_available != 160 or upper.quantity_available != 160
                or lower.quantity_consumed or upper.quantity_consumed != QUANTITY
                or child.quantity_available or child.quantity_reserved or child.quantity_consumed != QUANTITY
                or lower.finished_detail.physical_basis_json is not None
                or upper.finished_detail.physical_basis_json != value['expected_bases'][UPPER_ID]
                or child.finished_detail.physical_basis_json != value['expected_bases'][LOWER_ID]):
            _fail('原批余量、消费或身份未守恒')
        for lot, original in ((lower, value['sources'][0]), (upper, value['sources'][1])):
            actual = _source_document(lot)
            if (actual['finished_detail'] != original['finished_detail'] or actual['cost'] != original['cost']
                    or actual['location_id'] != LOCATION_ID or actual['reserved'] or actual['damaged'] or actual['scrapped']):
                _fail('原批次历史身份、成本或位置被改变')
        if (not output or output.quantity_available != QUANTITY or output.quantity_reserved or output.quantity_consumed
                or output.inventory_type != 'finished' or output.warehouse_location_id != LOCATION_ID
                or output.source_ref_type != 'bom_assembly' or output.source_ref_id != assembly.id
                or output.finished_detail.product_id != PARENT_ID or output.finished_detail.owner_customer_id != CUSTOMER_ID
                or output.finished_detail.physical_basis_json != value['expected_bases'][PARENT_ID]
                or assembly.total_cost != Decimal(value['total_reference_cost'])
                or sum((r.total_cost for r in inputs), Decimal(0)) != assembly.total_cost
                or output.estimated_unit_cost_snapshot * QUANTITY != assembly.total_cost):
            _fail('新成套入库数量、冻结身份或继承成本不守恒')
        from app.services.inventory_valuation import require_inherited_entry_cost
        require_inherited_entry_cost(db, output)
        receipt = {'order_item_id': ORDER_ITEM_ID, 'assembly_id': assembly.id, 'output_lot_id': output.id,
                   'split_lot_id': child.id, 'source_lot_ids': [LOWER_LOT, UPPER_LOT], 'quantity': QUANTITY,
                   'remaining_source_quantities': [160, 160], 'location_id': LOCATION_ID,
                   'reference_total_cost': str(assembly.total_cost), 'reference_unit_cost': str(output.estimated_unit_cost_snapshot),
                   'preview_hash': preview_hash, 'replayed': False}
        record = FinanceIdempotencyRecord(idempotency_key=object_ref, request_hash=_digest(request), action=ACTION,
                                         actor_user_id=actor.id, resource_type='order_item', resource_id=ORDER_ITEM_ID,
                                         response_json=_json({'request': request, 'receipt': receipt, 'preview': value}))
        db.add(record)
        db.flush()
        append_audit_event(db, event_category='business', result='success', source='script',
                          module_code='warehouse', action_code=ACTION, resource='inventory_lot', actor=actor,
                          entity_type='order_item', entity_id=ORDER_ITEM_ID, object_ref=object_ref, customer_id=CUSTOMER_ID,
                          details={'finance_receipt_id': record.id, 'request_hash': record.request_hash,
                                   'preview_hash': preview_hash, 'quantity': QUANTITY, 'assembly_id': assembly.id,
                                   'output_lot_id': output.id, 'split_lot_id': child.id,
                                   'source_lot_ids': [LOWER_LOT, UPPER_LOT], 'remaining_source_quantities': [160, 160]})
        return receipt
