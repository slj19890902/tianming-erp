"""Internal ledger primitive for an independently audited historical correction.

No API or CLI exposes this primitive. The caller must validate the complete
source plan, costs and reversal handling before committing a correction batch.
Existing source movements and commercial delivery quantities remain immutable.
"""
import hashlib
import json
from fractions import Fraction

from sqlalchemy import select, update

from app.core.time_contract import utc_now_naive
from app.models.user import User
from app.models.delivery import Delivery
from app.models.warehouse_inventory import InventoryLot, InventoryMovement
from app.services.warehouse_inventory import WarehouseInventoryError, _balances, _movement

PREFIX = 'historical-dual-quantity:'


def corrected_allocation_physical_quantity(db, allocation, quantity):
    """Translate immutable legacy allocation units using its audited ledger only."""
    if not allocation.consume_movement_id:
        return quantity
    key = f'{PREFIX}outbound_basis:{allocation.consume_movement_id}:{allocation.inventory_lot_id}'
    correction = db.scalar(select(InventoryMovement).where(InventoryMovement.idempotency_key == key))
    if correction is None:
        return quantity
    source = db.get(InventoryMovement, allocation.consume_movement_id)
    if (source is None or source.inventory_lot_id != allocation.inventory_lot_id
            or source.movement_type != 'consume' or source.quantity <= 0
            or source.quantity != allocation.consumed_quantity
            or correction.movement_type != 'consume'
            or correction.inventory_lot_id != source.inventory_lot_id
            or correction.related_delivery_id != source.related_delivery_id):
        raise WarehouseInventoryError('历史校正出库关联不完整，禁止按旧数量冲回', 409)
    physical = Fraction(quantity) * Fraction(source.quantity + correction.quantity, source.quantity)
    if physical.denominator != 1:
        raise WarehouseInventoryError('历史冲回不能形成完整实物片数', 409)
    return physical.numerator


def _post(db, event, *, operator_id):
    required = {'kind', 'quantity', 'source_movement_id', 'lot_id', 'customer_id',
                'product_id', 'before', 'expected_version'}
    if (not isinstance(event, dict) or not required.issubset(event)
            or any(type(event[name]) is not int or not 0 < event[name] <= 2147483647
                   for name in required - {'kind', 'before'})
            or not isinstance(event['before'], dict)
            or set(event['before']) != {'available', 'reserved', 'consumed', 'damaged', 'scrapped'}
            or any(type(value) is not int or not 0 <= value <= 2147483647
                   for value in event['before'].values())):
        raise WarehouseInventoryError('历史数量校正事件格式无效', 422)
    kind = event['kind']
    quantity = event['quantity']
    if not isinstance(kind, str) or kind not in {'inbound_basis', 'outbound_basis'}:
        raise WarehouseInventoryError('历史数量校正类型或数量无效', 422)
    physical_unit = event.get('physical_unit')
    if physical_unit is not None and (not isinstance(physical_unit, str)
            or not physical_unit.strip() or len(physical_unit) > 20):
        raise WarehouseInventoryError('历史校正实物单位无效', 422)
    encoded = json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    fingerprint = hashlib.sha256(encoded.encode()).hexdigest()
    key = f"{PREFIX}{kind}:{event['source_movement_id']}:{event['lot_id']}"
    existing = db.scalar(select(InventoryMovement).where(InventoryMovement.idempotency_key == key))
    if existing is not None:
        try:
            matches = json.loads(existing.remarks or '{}')['event_hash'] == fingerprint
        except (ValueError, KeyError, TypeError):
            matches = False
        if not matches:
            raise WarehouseInventoryError('同一历史来源已存在不同校正，禁止重复补记或补扣', 409)
        return existing, False
    source = db.get(InventoryMovement, event['source_movement_id'])
    if source is None or source.inventory_lot_id != event['lot_id'] or (source.idempotency_key or '').startswith(PREFIX):
        raise WarehouseInventoryError('历史校正原始流水不一致', 409)
    if kind == 'inbound_basis':
        if source.movement_type not in {'manual_in', 'location_transfer'} or source.after_available <= source.before_available:
            raise WarehouseInventoryError('入库口径校正必须关联原入库或移入流水', 409)
    elif source.movement_type != 'consume' or not source.related_delivery_id:
        raise WarehouseInventoryError('出库口径校正必须关联原送货出库流水', 409)
    if kind == 'outbound_basis':
        delivery = db.get(Delivery, source.related_delivery_id)
        reversed_source = db.scalar(select(InventoryMovement.id).where(
            InventoryMovement.reversal_of_movement_id == source.id).limit(1))
        if delivery is None or delivery.status != 'dispatched' or reversed_source is not None:
            raise WarehouseInventoryError('原送货已取消或存在冲回，必须重新审核净实物差额', 409)
    lot = db.get(InventoryLot, event['lot_id'], populate_existing=True)
    if lot is None or lot.finished_detail is None:
        raise WarehouseInventoryError('历史校正成品批次不存在', 409)
    detail = lot.finished_detail
    if detail.owner_customer_id != event['customer_id'] or detail.product_id != event['product_id']:
        raise WarehouseInventoryError('历史校正客户或存货身份不符', 409)
    before = _balances(lot)
    if before != event['before'] or lot.version != event['expected_version']:
        raise WarehouseInventoryError('历史库存已变化，必须重新只读审核', 409)
    if before['reserved'] or before['damaged'] or before['scrapped']:
        raise WarehouseInventoryError('历史校正批次存在预占或异常品，必须单独核对', 409)
    available_delta = quantity if kind == 'inbound_basis' else -quantity
    consumed_delta = quantity if kind == 'outbound_basis' else 0
    if before['available'] + available_delta < 0:
        raise WarehouseInventoryError('历史校正实物余额不足，禁止负库存', 409)
    if max(before['available'] + available_delta, before['consumed'] + consumed_delta) > 2147483647:
        raise WarehouseInventoryError('历史校正数量超出允许范围', 422)
    changed = db.execute(update(InventoryLot).where(
        InventoryLot.id == lot.id, InventoryLot.version == event['expected_version'],
    ).values(quantity_available=before['available'] + available_delta,
             quantity_consumed=before['consumed'] + consumed_delta,
             version=InventoryLot.version + 1, last_movement_at=utc_now_naive())
        .execution_options(synchronize_session=False))
    if changed.rowcount != 1:
        raise WarehouseInventoryError('历史校正并发冲突，请刷新', 409)
    db.expire(lot)
    movement = _movement(db, lot=lot,
        movement_type='adjust' if kind == 'inbound_basis' else 'consume',
        quantity=quantity, before=before, operator_id=operator_id,
        reason='历史双单位入库计量校正' if kind == 'inbound_basis' else '历史双单位换算补扣',
        related_delivery_id=source.related_delivery_id if kind == 'outbound_basis' else None,
        remarks=json.dumps({'schema': 1, 'correction_type': kind,
            'source_movement_id': source.id, 'event_hash': fingerprint, 'event': event}, ensure_ascii=False),
        idempotency_key=key)
    if physical_unit is not None:
        movement.unit = physical_unit
    db.flush()
    return movement, True


def post_audited_quantity_batch(db, events, *, operator_id):
    """Post all audited ledger events or none; caller retains final commit control."""
    user = db.get(User, operator_id)
    if user is None or not user.is_active or user.role != 'admin':
        raise WarehouseInventoryError('历史数量校正仅限有效管理员', 403)
    if not events:
        raise WarehouseInventoryError('历史校正清单不能为空', 422)
    connection = db.connection()
    # sqlite3 legacy mode does not BEGIN for SELECT. Without an outer BEGIN,
    # RELEASE SAVEPOINT commits and a caller's later rollback cannot undo it.
    if (connection.dialect.name == 'sqlite'
            and not connection.connection.driver_connection.in_transaction):
        connection.exec_driver_sql('BEGIN IMMEDIATE')
    # A savepoint also rolls back earlier events when a later event is stale.
    with db.begin_nested():
        results = [_post(db, event, operator_id=operator_id) for event in events]
        db.flush()
    return [{'movement_id': row.id, 'created': created} for row, created in results]
