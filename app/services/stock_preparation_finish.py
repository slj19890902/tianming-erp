"""Employee-confirmed 1:1 conversion of a completed processed output to finished stock."""
import json
from decimal import Decimal

from sqlalchemy import select, update

from app.core.time_contract import beijing_today, utc_now_naive
from app.models.product import Product
from app.models.stock_preparation import StockPreparationCommand as Command, StockPreparationJob as Job
from app.models.warehouse_inventory import InventoryLot
from app.services import stock_preparation as prep
from app.services.audit_log import append_audit_event
from app.services.finished_stock_identity import matches_stock_identity, product_basis
from app.services.inventory_valuation import require_inherited_entry_cost
from app.services.processed_component_stock import eligible_output, output_identity
from app.services.stock_preparation_groups import destination, encode
from app.services.warehouse_inventory import _balances, _movement, manual_finished_in


def _request(payload):
    return encode({'action': 'finish_output', **payload})


def checked_command(db, receipt_id, payload, actor):
    """Resolve a prior request without trusting a client-supplied result or stale inventory."""
    command = db.get(Command, payload['operation_key'])
    if command is None:
        return None
    if (command.receipt_item_id != receipt_id or command.actor_id != actor.id
            or command.request_json != _request(payload)):
        prep.fail('操作标识已用于不同请求或账号，请保留原内容核对')
    result = json.loads(command.result_json)
    if result.get('action') != 'finish_output' or result.get('operation_key') != payload['operation_key']:
        prep.fail('原转换结果记录不一致，请保留原内容核对')
    return result


def finish_output(db, *, receipt_id, payload, actor):
    prior = checked_command(db, receipt_id, payload, actor)
    if prior is not None:
        return prior
    receipt, item, _ = prep.source(db, receipt_id)
    job = db.get(Job, payload['job_id'])
    source = db.get(InventoryLot, payload['output_lot_id'])
    product = db.get(Product, job.product_id) if job else None
    qty = payload['quantity']
    if (not receipt or receipt.status != 'posted' or not job or job.receipt_item_id != receipt_id
            or job.status != 'completed' or job.version != payload['job_version']
            or not source or source.id != job.output_lot_id
            or source.source_ref_type != 'stock_preparation' or source.source_ref_id != job.id
            or source.version != payload['output_version'] or source.quantity_available < qty
            or not product or not product.is_active or product.deleted_at or product.customer_id != item.customer_id):
        prep.fail('来源批次、可用数量或版本已变化，请刷新后核对')
    if not payload['confirm_finished']:
        prep.fail('请先确认本次实物已完成全部工序，可以作为成品出货')
    identity = output_identity(db, source)
    basis = product_basis(product)
    if (not identity or identity[:2] != (product.id, item.customer_id)
            or not matches_stock_identity(identity[2], basis)
            or not eligible_output(db, source, product_id=product.id,
                customer_id=item.customer_id, expected_basis=basis)):
        prep.fail('该批次的产品、工艺或实物身份与当前成品不一致，请先核对')
    place = destination(db, payload['location_id'], payload['layout_version'])
    unit_cost = require_inherited_entry_cost(db, source)
    before = _balances(source)
    now = utc_now_naive()
    changed = db.execute(update(InventoryLot).where(
        InventoryLot.id == source.id, InventoryLot.version == payload['output_version'],
        InventoryLot.status == 'active', InventoryLot.quantity_available >= qty,
    ).values(quantity_available=InventoryLot.quantity_available - qty,
             quantity_consumed=InventoryLot.quantity_consumed + qty,
             version=InventoryLot.version + 1, last_movement_at=now))
    if changed.rowcount != 1:
        prep.fail('来源批次已被其他操作使用，请刷新后核对')
    db.refresh(source)
    movement_key = 'prep-finish-consume:' + payload['operation_key']
    _movement(db, lot=source, movement_type='consume', quantity=qty, before=before,
              operator_id=actor.id, reason='已加工半成品转成品', idempotency_key=movement_key)
    finished = manual_finished_in(db, customer_id=item.customer_id, product_id=product.id,
        location_id=place['id'], quantity=qty, stock_date=beijing_today(), source_type='transfer',
        remarks=f'备库半成品批次 {source.lot_number} 转成品', operator_id=actor.id,
        idempotency_key='prep-finish-in:' + payload['operation_key'],
        source_ref_type='stock_preparation_finish', source_ref_id=source.id,
        expected_layout_version=payload['layout_version'], physical_basis_json=basis,
        movement_reason='已加工半成品转成品')
    total = unit_cost * qty
    finished.estimated_unit_cost_snapshot = unit_cost
    finished.cost_snapshot_source = 'stock_preparation_finish'
    finished.cost_snapshot_detail_json = encode({
        'source_lot_id': source.id, 'input_quantity': qty, 'output_quantity': qty,
        'unit_cost': str(unit_cost), 'total_cost': str(total), 'currency': 'CNY',
        'movement_key': movement_key, 'physical_basis': basis,
        'basis': 'inherited_entry_cost_not_new_purchase',
    })
    finished.cost_snapshot_at = now
    result = {'action': 'finish_output', 'operation_key': payload['operation_key'],
              'receipt_item_id': receipt_id, 'job_id': job.id, 'source_lot_id': source.id,
              'output_lot_id': finished.id, 'quantity': qty, 'customer_id': item.customer_id,
              'product_id': product.id, 'source_remaining': source.quantity_available,
              'output_lot_number': finished.lot_number, 'location_id': place['id'],
              'movement_key': movement_key}
    db.add(Command(operation_key=payload['operation_key'], receipt_item_id=receipt_id,
                   request_json=_request(payload), result_json=encode(result), actor_id=actor.id))
    db.flush()
    from app.services.shared_bom_stock import enroll_completed_bom
    enroll_completed_bom(db, finished, operator_id=actor.id)
    append_audit_event(db, event_category='business', result='success', source='system',
        module_code='warehouse', action_code='stock_preparation.finish_output',
        resource='inventory_lot', actor=actor, entity_type='inventory_lot', entity_id=finished.id,
        customer_id=item.customer_id, details=result)
    db.flush()
    return result
