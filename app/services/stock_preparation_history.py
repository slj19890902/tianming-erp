"""Receipt-backed completions share the history page, never fake order records."""
from app.services.product_unit_labels import basis_unit_label
import json
from datetime import datetime
from collections import defaultdict
from sqlalchemy import select, update, func
from app.models.stock_preparation import StockPreparationJob as Job, StockPreparationCommand as Command
from app.models.warehouse_inventory import InventoryLot, InventoryReservation, InventoryMovement, InventoryLocationMovement, InventoryPalletItem
from app.services import stock_preparation as prep
from app.services.stock_preparation_groups import encode
from app.core.time_contract import utc_naive_to_api, utc_now_naive, utc_naive_to_beijing_date, BEIJING_UTC_OFFSET
from app.services.audit_log import append_audit_event
from app.models.user import User


def completed_groups(db, scope=None):
    groups = defaultdict(list)
    for job in db.scalars(select(Job).where(Job.actual_output > 0).order_by(Job.id)):
        _, item, _ = prep.source(db, job.receipt_item_id)
        if scope is not None and item.customer_id not in scope:
            continue
        snapshot = json.loads(job.product_snapshot)
        group = snapshot.get('preparation_group')
        groups[group['key'] if group else f'job:{job.id}'].append(job)
    return groups


def reverse_block(db, jobs):
    for job in jobs:
        receipt, item, source = prep.source(db, job.receipt_item_id)
        output = db.get(InventoryLot, job.output_lot_id)
        reservation = db.get(InventoryReservation, job.reservation_id)
        if job.status != 'completed':
            return '该完工已撤销或状态已变化'
        if receipt.status != 'posted' or item.order.status == 'voided' or not source or source.status != 'active':
            return '原收料或原材料库存不可用'
        if (not reservation or reservation.inventory_lot_id != source.id or reservation.status != 'consumed'
                or not reservation.consumed_at or reservation.consumed_stock_quantity != job.input_quantity or source.quantity_consumed < job.input_quantity
                or source.inventory_type != 'semi_finished'):
            return '原生产消耗记录已变化'
        if (not output or output.source_ref_type != 'stock_preparation' or output.source_ref_id != job.id
                or output.status != 'active' or output.inventory_type not in {'finished','semi_finished'}
                or output.quantity_available != job.actual_output
                or any((output.quantity_reserved, output.quantity_consumed, output.quantity_damaged, output.quantity_scrapped))):
            return '产出已被使用、占用或调整，请先处理后续业务'
        movements = list(db.scalars(select(InventoryMovement).where(InventoryMovement.inventory_lot_id == output.id)))
        if not movements or any(m.movement_type != 'manual_in' for m in movements):
            return '产出已有移库或其他后续流水，不能直接撤销'
        if db.scalar(select(InventoryReservation.id).where(InventoryReservation.inventory_lot_id == output.id).limit(1)):
            return '产出已有预占记录，不能直接撤销'
        pallet = output.pallet_item.pallet if output.pallet_item else None
        # The consume timestamp is recorded after initial output placement. Pallet
        # movement times are Beijing-naive, while reservation times are UTC-naive.
        if pallet and db.scalar(select(InventoryLocationMovement.id).where(
                InventoryLocationMovement.pallet_id == pallet.id,
                InventoryLocationMovement.movement_type == 'move',
                InventoryLocationMovement.moved_at > reservation.consumed_at + BEIJING_UTC_OFFSET).limit(1)):
            return '产出栈板已经移库，不能直接撤销'
    return None


def rows(db, scope=None, **filters):
    events = {}
    operators = {}
    users = {u.id: u.real_name for u in db.scalars(select(User))}
    for command in db.scalars(select(Command).order_by(Command.created_at, Command.operation_key)):
        request = json.loads(command.request_json)
        if request.get('action') == 'complete' and request.get('job_id'):
            events[request['job_id']] = command.created_at
            operators[request['job_id']] = command.actor_id
    result = []
    for key, jobs in completed_groups(db, scope).items():
        snapshot = json.loads(jobs[0].product_snapshot)
        group = snapshot.get('preparation_group')
        recipe = group['recipe'] if group else snapshot
        _, item, _ = prep.source(db, jobs[0].receipt_item_id)
        completed_at = max((events.get(j.id, j.created_at) for j in jobs))
        reversed_ = all(j.status == 'cancelled' for j in jobs)
        state = 'reversed' if reversed_ else 'posted'
        details = [prep.job_dict(db, j) for j in jobs]
        amounts = defaultdict(int)
        for j in jobs:
            amounts[j.product_id] += j.actual_output
        quantity = min((amounts[c['product_id']]//c['per_set'] for c in recipe['children']), default=0) if group else jobs[0].actual_output
        if filters.get('customer_id') and filters['customer_id'] != item.customer_id:
            continue
        if filters.get('status') and filters['status'] != state:
            continue
        day = utc_naive_to_beijing_date(completed_at)
        if filters.get('completed_date_from') and day < filters['completed_date_from'] or filters.get('completed_date_to') and day > filters['completed_date_to']:
            continue
        terms = {'order_keyword':item.order.order_number, 'product_code':recipe['code'], 'product_name':recipe['name']}
        if any(str(filters.get(k) or '').strip().casefold() not in str(v or '').casefold() for k,v in terms.items()):
            continue
        block = reverse_block(db, jobs)
        versions = []
        for job in jobs:
            _,_,source = prep.source(db, job.receipt_item_id)
            output = db.get(InventoryLot, job.output_lot_id)
            versions.append(dict(job_id=job.id, job_version=job.version, source_version=source.version if source else 0, output_version=output.version if output else 0))
        locations = list(dict.fromkeys(d['output_location'] for d in details if d['output_location']))
        result.append(dict(id='prep:'+key, origin='stock_preparation', preparation_key=key, jobs=details,
            reverse_versions=versions, can_revert=not block, reversal_block=block, status=state,
            customer_id=item.customer_id, customer_name=item.customer.name if item.customer else '通用备料',
            customer_short_name=(item.customer.chinese_short_name or item.customer.name) if item.customer else '通用备料',
            customer_order_number='半成品加工' if all(d['output_kind']=='semi' for d in details) else '备库生产', order_number=item.order.order_number,
            product_code=recipe['code'], product_name=recipe['name'], completed_at=utc_naive_to_api(completed_at),
            completed_by_name=' / '.join(dict.fromkeys(users.get(operators.get(j.id)) or '未登记' for j in jobs)),
            actual_output_quantity=quantity, planned_output_quantity=group['sets'] if group else jobs[0].expected_output,
            output_unit='套' if group else basis_unit_label(snapshot.get('physical_basis') or {}), current_warehouse_location_name=' / '.join(locations),
            current_inventory_status='located' if locations else 'empty', can_adjust_actual_quantity=False,
            is_fully_delivered=False, can_transfer_to_stock=False))
    from app.services.stock_preparation_disposition import assembly_rows,unassemble_block
    for a in assembly_rows(db,scope):
        recipe=a['recipe'];state='reversed' if a['reversed'] else 'posted'
        if filters.get('customer_id') and filters['customer_id']!=recipe['customer_id']:continue
        if filters.get('status') and filters['status']!=state:continue
        day=utc_naive_to_beijing_date(datetime.fromisoformat(a['completed_at'].replace('Z','+00:00')).replace(tzinfo=None))
        if filters.get('completed_date_from') and day<filters['completed_date_from'] or filters.get('completed_date_to') and day>filters['completed_date_to']:continue
        if filters.get('order_keyword'):continue
        if any(str(filters.get(k) or '').strip().casefold() not in recipe[field].casefold() for k,field in [('product_code','code'),('product_name','name')]):continue
        block=unassemble_block(db,a)
        result.append(dict(id='assembly:'+a['key'],origin='stock_assembly',assembly=a,preparation_key=a['group_key'],status=state,
            completed_by_name=users.get(db.get(Command, a['key']).actor_id),
            customer_name=recipe['customer_name'],customer_short_name=recipe['customer_name'],customer_order_number='成套入库',
            product_code=recipe['code'],product_name=recipe['name'],completed_at=a['completed_at'],actual_output_quantity=a['sets'],planned_output_quantity=a['sets'],
            output_unit='套',current_warehouse_location_name=a['location'],current_inventory_status='located',can_revert=not block,reversal_block=block,
            can_adjust_actual_quantity=False,is_fully_delivered=False,can_transfer_to_stock=False))
    return result


def combined_page(db, *, allowed_customer_ids, page, page_size, **filters):
    from app.services.production_workflow import _completion_rows, list_production_completions
    record_type = filters.pop('record_type', None)
    operator_name = str(filters.pop('operator_name', None) or '').strip().casefold()
    stock = [] if filters.get('placement_pending') else rows(db, allowed_customer_ids, **filters)
    order_keys = _completion_rows(db, allowed_customer_ids=allowed_customer_ids, keys_only=True,
        record_type=record_type, operator_name=operator_name, **filters)
    consumed = {}
    if not filters.get('placement_pending'):
        from app.services.bom_assembly_history import history
        assembly, consumed = history(db, allowed_customer_ids, **filters)
        if record_type and record_type != 'bom_assembly' or operator_name:
            # Only fold source processing rows when their matching assembly is visible.
            assembly = [r for r in assembly if (not record_type or r['origin'] == record_type)
                        and operator_name in str(r.get('completed_by_name') or '').casefold()]
            consumed = {}
        stock += assembly
        # Fold fully consumed processing records only when their assembly is
        # included in this result. Product/date filters still expose originals.
        from app.models.production import ProductionCompletion
        folded = set(db.scalars(select(ProductionCompletion.id).join(InventoryLot,
            InventoryLot.id == ProductionCompletion.inventory_lot_id).where(
                ProductionCompletion.status == 'posted', InventoryLot.id.in_(consumed),
                InventoryLot.quantity_available + InventoryLot.quantity_reserved + InventoryLot.quantity_damaged == 0)))
        order_keys = [(id, at) for id, at in order_keys if id not in folded]
    stock = [r for r in stock if (not record_type or r['origin'] == record_type)
             and operator_name in str(r.get('completed_by_name') or '').casefold()]
    keys = [(utc_naive_to_api(at), 'order:'+str(id), id, None) for id,at in order_keys]
    keys += [(r['completed_at'], r['id'], None, r) for r in stock]
    keys.sort(key=lambda k:(datetime.fromisoformat(k[0].replace('Z','+00:00')),k[2] or 0,k[1]), reverse=True)
    selected = keys[(page-1)*page_size:page*page_size]
    ids = [k[2] for k in selected if k[2] is not None]
    orders = {r['id']:r for r in list_production_completions(db, allowed_customer_ids=allowed_customer_ids, completion_ids=ids)} if ids else {}
    for row in orders.values():
        if consumed.get(row.get('inventory_lot_id')):
            row['assembly_consumed_quantity'] = consumed[row['inventory_lot_id']]
            row['inventory_usage_label'] = '未配套余料' if row.get('current_inventory_quantity', 0) else '已用于组套'
    return [orders[k[2]] if k[2] is not None else k[3] for k in selected], len(keys)


def reverse(db, *, key, payload, actor):
    request = encode(dict(payload, preparation_key=key))
    previous = db.get(Command, payload['operation_key'])
    if previous:
        if previous.actor_id != actor.id or previous.request_json != request:
            prep.fail('操作标识已用于其他内容')
        return json.loads(previous.result_json)
    jobs = completed_groups(db).get(key)
    if not jobs:
        prep.fail('备库完工记录不存在')
    if not payload['confirm_unused']:
        prep.fail('请确认仅纠正误报，产出尚未组装或使用')
    versions = {v['job_id']:v for v in payload['jobs']}
    if len(versions) != len(payload['jobs']) or set(versions) != {j.id for j in jobs}:
        prep.fail('必须整组撤销全部子件')
    block = reverse_block(db, jobs)
    if block:
        prep.fail(block)
    pallets = {}
    for job in jobs:
        _,_,source = prep.source(db,job.receipt_item_id)
        output = db.get(InventoryLot,job.output_lot_id)
        expected = versions[job.id]
        if (job.version,source.version,output.version) != (expected['job_version'],expected['source_version'],expected['output_version']):
            prep.fail('库存或完工版本已变化，请刷新')
        if output.pallet_item:
            pallets[output.pallet_item.pallet.id] = output.pallet_item.pallet
        before = prep._balances(output)
        changed = db.execute(update(InventoryLot).where(InventoryLot.id==output.id,InventoryLot.version==expected['output_version']).values(quantity_available=0,status='closed',version=InventoryLot.version+1,last_movement_at=utc_now_naive()))
        if changed.rowcount != 1:
            prep.fail('产出库存已变化')
        db.refresh(output)
        prep._movement(db,lot=output,movement_type='adjust',quantity=job.actual_output,before=before,operator_id=actor.id,reason='撤销备库完工产出',idempotency_key=f"prep-reverse-out:{payload['operation_key']}:{job.id}")
        before = prep._balances(source)
        changed = db.execute(update(InventoryLot).where(InventoryLot.id==source.id,InventoryLot.version==expected['source_version'],InventoryLot.quantity_consumed>=job.input_quantity).values(quantity_available=InventoryLot.quantity_available+job.input_quantity,quantity_consumed=InventoryLot.quantity_consumed-job.input_quantity,version=InventoryLot.version+1,last_movement_at=utc_now_naive()))
        if changed.rowcount != 1:
            prep.fail('投入材料已变化')
        db.refresh(source)
        prep._movement(db,lot=source,movement_type='reverse_consume',quantity=job.input_quantity,before=before,operator_id=actor.id,reason='撤销备库完工恢复材料',reservation_id=job.reservation_id,idempotency_key=f"prep-reverse-in:{payload['operation_key']}:{job.id}")
        reservation = db.get(InventoryReservation,job.reservation_id)
        reservation.status='released';reservation.consumed_stock_quantity=0;reservation.released_stock_quantity=job.input_quantity
        reservation.released_by=actor.id;reservation.released_at=utc_now_naive();reservation.release_reason='撤销备库完工'
        changed=db.execute(update(Job).where(Job.id==job.id,Job.version==expected['job_version'],Job.status=='completed').values(status='cancelled',version=Job.version+1))
        if changed.rowcount != 1:
            prep.fail('完工记录已变化')
    for pallet in pallets.values():
        physical=db.scalar(select(func.coalesce(func.sum(InventoryLot.quantity_available+InventoryLot.quantity_reserved+InventoryLot.quantity_damaged),0)).join(InventoryPalletItem,InventoryPalletItem.inventory_lot_id==InventoryLot.id).where(InventoryPalletItem.pallet_id==pallet.id))
        if not physical and pallet.is_current:
            from app.services.floor3_locations import clear_pallet
            from app.services.warehouse_ground_slots import release_ground_occupancy_for_pallet
            clear_pallet(db,pallet_id=pallet.id,expected_version=pallet.version,remarks='撤销备库完工',operator_id=actor.id)
            release_ground_occupancy_for_pallet(db,pallet_id=pallet.id,operator_id=actor.id)
    result=dict(action='reverse_complete',preparation_key=key,job_ids=[j.id for j in jobs])
    db.add(Command(operation_key=payload['operation_key'],receipt_item_id=jobs[0].receipt_item_id,request_json=request,result_json=encode(result),actor_id=actor.id))
    append_audit_event(db,event_category='business',result='success',source='web',module_code='production',action_code='stock_preparation.reverse_complete',resource='production',actor=actor,entity_type='stock_preparation_job',entity_id=jobs[0].id,details=dict(result,versions=payload['jobs'],confirm_unused=True))
    db.flush()
    return result
