"""Plan a stock kit atomically while retaining every physical child lot.

Planning freezes the recipe on child jobs. Explicit disposition may later
consume those children into parent stock; existing ledgers retain the trace.
"""
import hashlib
import json
import math
from collections import defaultdict
from sqlalchemy import select
from app.models.product import Product
from app.models.product_bom import ProductBomComponent
from app.models.multilevel_bom import ProductBomProfile, ProductBomInventoryRelation
from app.models.stock_preparation import StockPreparationJob as Job, StockPreparationCommand as Command
from app.models.warehouse_inventory import InventoryLot
from app.services import stock_preparation as prep
from app.services.warehouse_inventory import _location, _validate_transfer_location_snapshot


def encode(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def recipe(db, parent_id):
    parent = db.get(Product, parent_id)
    if not parent or not parent.is_active or parent.deleted_at or not parent.is_composite:
        prep.fail('组合产品已失效，请重新选择')
    edges = list(db.scalars(select(ProductBomComponent).where(ProductBomComponent.parent_product_id == parent_id).order_by(ProductBomComponent.display_order)))
    if not edges:
        prep.fail('组合产品尚未维护子件')
    profile = db.get(ProductBomProfile, parent_id)
    # Physical assembly and multilevel manufacture retain their existing workflow.
    if (profile and profile.source != 'separate') or (not profile and not parent.is_virtual_composite_parent):
        prep.fail('该产品需要实体组装，请使用既有 BOM 组装流程')
    children = []
    for edge in edges:
        child = db.get(Product, edge.component_product_id)
        if not edge.is_required or not child or not child.is_active or child.deleted_at or child.purged_at or child.customer_id != parent.customer_id or child.is_composite or child.is_virtual_composite_parent:
            prep.fail('子件需为同客户启用的独立实物；多级或可选组件请使用 BOM 生产流程')
        relation = db.get(ProductBomInventoryRelation, edge.id)
        if profile and (not relation or relation.relation != 'accompany'):
            prep.fail('组合库存关系需明确为子件配套，不能跳过实体组装')
        count = int(edge.quantity_per_set)
        if count <= 0 or count != edge.quantity_per_set:
            prep.fail('每套子件用量必须为正整数')
        children.append(dict(product_id=child.id,code=child.product_code,name=child.product_name,unit=child.unit,per_set=count,version=child.version,edge_id=edge.id))
    return dict(parent_id=parent.id,code=parent.product_code,name=parent.product_name,customer_id=parent.customer_id,
        customer_name=parent.customer.chinese_short_name or parent.customer.name,version=parent.version,children=children,
        inventory_mode='children',unit='套')


def destination(db, location_id, layout_version, inventory_type="finished"):
    if not location_id or layout_version is None:
        prep.fail('请选择存放位置并刷新地图版本')
    place = _location(db, location_id, inventory_type)
    _validate_transfer_location_snapshot(db,location=place,role='存放位置',expected_address_version=None,
        expected_layout_version=layout_version,expected_map_revision=None)
    return dict(id=place.id,layout_version=layout_version,name=f'{place.warehouse_floor}楼 · {place.location_name}')


def preview(db, parent_id, sets, rows=None):
    frozen = recipe(db,parent_id)
    rows = rows if rows is not None else prep.list_rows(db,scope=[frozen['customer_id']])
    by_product = defaultdict(list)
    for row in rows:
        if row['can_plan'] and row['available'] > 0:
            _,item,lot=prep.source(db,row['receipt_item_id'])
            try:
                prep.plan_product(db,item,lot)
            except prep.WarehouseInventoryError:
                continue
            by_product[row['product_id']].append(row)
    inputs, available_sets, shortages = [], [], []
    for child in frozen['children']:
        sources = sorted(by_product[child['product_id']],key=lambda r:r['receipt_item_id'])
        total = sum(r['available']*r['factor']//r['pieces_per_box'] for r in sources)
        available_sets.append(total//child['per_set'])
        remaining = sets*child['per_set']
        child['available_output'] = total
        if total < remaining:
            shortages.append(dict(product_id=child['product_id'],name=child['name'],missing=remaining-total))
        for row in sources:
            if remaining <= 0:
                break
            quantity = min(row['available'],math.ceil(remaining*row['pieces_per_box']/row['factor']))
            expected = quantity*row['factor']//row['pieces_per_box']
            if not expected:
                continue
            inputs.append(dict(receipt_id=row['receipt_item_id'],lot_version=row['lot_version'],product_id=child['product_id'],
                quantity=quantity,expected_output=expected,source_location=row['location'],lot_number=row['lot_number']))
            remaining -= expected
    result = dict(recipe=frozen,sets=sets,available_sets=min(available_sets,default=0),inputs=inputs,shortages=shortages)
    result['basis_hash'] = digest(result)
    return result


def groups(db, rows):
    product_ids = {r['product_id'] for r in rows if r['can_plan'] and r['available'] > 0}
    parents = set(db.scalars(select(ProductBomComponent.parent_product_id).where(ProductBomComponent.component_product_id.in_(product_ids)))) if product_ids else set()
    candidates = []
    for parent_id in sorted(parents):
        try:
            value = preview(db,parent_id,1,rows)
        except prep.WarehouseInventoryError:
            continue
        candidates.append(value)
    pending = {}
    for row in rows:
        for job in row['jobs']:
            group = job['product'].get('preparation_group')
            if not group:
                continue
            target = pending.setdefault(group['key'],dict(group=group,jobs=[],statuses=set()))
            target['jobs'].append(dict(job,receipt_id=row['receipt_item_id'],lot_version=row['lot_version'],source_location=row['location']))
            target['statuses'].add(job['status'])
    for target in pending.values():
        target['status'] = 'pending' if 'pending' in target['statuses'] else 'completed' if 'completed' in target['statuses'] else 'cancelled'
        del target['statuses']
        totals = defaultdict(int)
        for job in target['jobs']:
            totals[job['product']['product_id']] += job['actual_output']
        remaining = defaultdict(int)
        for job in target['jobs']:
            remaining[job['product']['product_id']] += job['output_remaining']
        target['remaining_sets'] = min((remaining[c['product_id']]//c['per_set'] for c in target['group']['recipe']['children']),default=0)
        target['completed_sets'] = min((totals[c['product_id']]//c['per_set'] for c in target['group']['recipe']['children']),default=0)
    return dict(candidates=candidates,groups=list(pending.values()))


def workspace_rows(db, rows, state):
    """One pending row per task/group, one arrangement row per unambiguous kit."""
    grouped = groups(db,rows)
    result = []
    if state == 'waiting':
        return [dict(row, entry_type='receipt') for row in rows if row['status'] == 'waiting']
    if state == 'materials':
        # Keep unassembled components and raw/semi remnants accessible. Finished
        # lots have their own all-source live inventory view.
        return [r for r in workspace_rows(db, rows, 'stock') if r['entry_type'] != 'assembled_stock'
                and (r['entry_type'] == 'group_stock' or r.get('physical', 0)
                     or any(j.get('output_kind') == 'semi' and j.get('output_remaining', 0) for j in r.get('jobs', [])))]
    if state == 'pending':
        for group in grouped['groups']:
            if group['status']=='pending':
                result.append(dict(key='group:'+group['group']['key'],entry_type='group_job',task=group))
        for row in rows:
            for job in row['jobs']:
                if job['status']=='pending' and not job['product'].get('preparation_group'):
                    result.append(dict(row,key='job:'+str(job['id']),entry_type='single_job',job=job))
            if row['status']=='arrange' and row['can_plan'] and row['available']>0:
                from app.services.stock_preparation_processing import processing_block
                _, item, lot = prep.source(db,row['receipt_item_id'])
                result.append(dict(row,entry_type='legacy_material',
                    processing_expected_output=row['available']*row['factor']//row['pieces_per_box'],
                    processing_yield_per_sheet=row['factor'],processing_pieces_per_product=row['pieces_per_box'],
                    processing_block=processing_block(db,item,lot)))
        return result
    if state == 'stock':
        stocked_groups = [dict(key='stock-group:'+g['group']['key'],entry_type='group_stock',task=g)
                          for g in grouped['groups'] if any(j['output_remaining'] for j in g['jobs'])]
        from app.services.stock_preparation_disposition import assembly_rows
        customer_ids={r['customer_id'] for r in rows} if rows and 'customer_id' in rows[0] else {prep.source(db,r['receipt_item_id'])[1].customer_id for r in rows if r.get('receipt_item_id')}
        assembled=[dict(key='assembled:'+a['key'],entry_type='assembled_stock',assembly=a) for a in assembly_rows(db,customer_ids) if not a['reversed'] and a['physical']>0]
        return assembled + stocked_groups + [dict(row,entry_type='receipt',grouped_output_hidden=True) for row in rows
            if row['physical'] or any(j['output_remaining'] and not j['product'].get('preparation_group') for j in row['jobs'])]
    if state == 'history':
        return [dict(row,entry_type='receipt') for row in rows if row['jobs'] or row['history'] or row['status']=='history']
    candidates=grouped['candidates']
    memberships=defaultdict(list)
    for group in candidates:
        for child in group['recipe']['children']:
            memberships[child['product_id']].append(group['recipe']['parent_id'])
    hidden=set()
    for group in candidates:
        if group['available_sets'] <= 0:
            continue
        ids={c['product_id'] for c in group['recipe']['children']}
        # Ambiguous components remain individual and allow explicit kit selection.
        if not all(len(memberships[pid])==1 for pid in ids):
            continue
        children=[r for r in rows if r['product_id'] in ids and r['available']>0 and r['can_plan'] and r['status']=='arrange']
        if not children:
            continue
        result.append(dict(key='kit:'+str(group['recipe']['parent_id']),entry_type='kit',plan=group,children=children))
        hidden.update(r['key'] for r in children)
    result.extend(dict(row,entry_type='receipt',kit_options=[g for g in candidates if any(c['product_id']==row['product_id'] for c in g['recipe']['children'])]) for row in rows if row['key'] not in hidden and row['status'] in ({'arrange'} if state == 'arrange' else {'arrange','waiting'}) and (row['available']>0 or row['status']=='waiting'))
    return result


def mutate_group(db,payload,actor,*,result_builder=None):
    if payload['action'] in {'dispose','assemble','unassemble','store_outputs'}:
        from app.services.stock_preparation_disposition import dispose,assemble,unassemble,store_outputs
        if payload['action']=='dispose':
            return dispose(db,payload,actor,result_builder=result_builder)
        return {'assemble':assemble,'unassemble':unassemble,'store_outputs':store_outputs}[payload['action']](db,payload,actor)
    key = payload['operation_key']
    request = encode(payload)
    old = db.get(Command,key)
    if old:
        if old.request_json != request or old.actor_id != actor.id:
            prep.fail('操作标识已用于其他内容')
        return json.loads(old.result_json)
    jobs = []
    if payload['action'] == 'plan':
        plan = preview(db,payload['parent_id'],payload['sets'])
        if plan['basis_hash'] != payload['basis_hash']:
            prep.fail('组合用量或库存已变化，请重新预览')
        if plan['shortages'] or not plan['inputs']:
            prep.fail('子件不足，不能安排本次套数')
        # Planning only reserves existing materials; their current locations remain
        # authoritative until the operator confirms the physical output.
        place = destination(db,payload['location_id'],payload['layout_version']) if payload.get('location_id') else None
        group = dict(key=key,recipe=plan['recipe'],sets=payload['sets'],planned_location=place)
        from app.services.finished_stock_identity import product_basis
        group['parent_basis']=product_basis(db.get(Product,plan['recipe']['parent_id']))
        for index,source in enumerate(plan['inputs']):
            result = prep.mutate(db,receipt_id=source['receipt_id'],payload=dict(action='plan',operation_key=digest([key,index])[:60],
                lot_version=source['lot_version'],quantity=source['quantity']),actor=actor,group_snapshot=group)
            jobs.append(result['job_id'])
    else:
        # Select immutable group membership from the ledger, never trust client-supplied membership.
        members = [job for job in db.scalars(select(Job)) if (json.loads(job.product_snapshot).get('preparation_group') or {}).get('key') == payload['group_key']]
        if not members or any(job.status != 'pending' for job in members):
            prep.fail('整组生产已变化，请刷新')
        expected = {job.id for job in members}
        submitted = {row['job_id']:row for row in payload['jobs']}
        if len(submitted) != len(payload['jobs']) or set(submitted) != expected:
            prep.fail('必须核对整组所有子件，不能漏项或重复')
        if payload['action'] == 'complete':
            destination(db,payload['location_id'],payload['layout_version'])
        for index,job in enumerate(members):
            row = submitted[job.id]
            group = json.loads(job.product_snapshot)['preparation_group']
            prep.mutate(db,receipt_id=job.receipt_item_id,payload=dict(action=payload['action'],operation_key=digest([key,index])[:60],
                lot_version=row['lot_version'],job_id=job.id,job_version=row['job_version'],actual_output=row['actual_output'],
                location_id=payload['location_id'],layout_version=payload['layout_version'],confirm_overproduction=payload['confirm_overproduction']),actor=actor,group_snapshot=group)
            jobs.append(job.id)
    anchor = db.get(Job,jobs[0])
    result = dict(action=payload['action'],job_ids=jobs,group_key=key if payload['action']=='plan' else payload['group_key'])
    db.add(Command(operation_key=key,receipt_item_id=anchor.receipt_item_id,request_json=request,result_json=encode(result),actor_id=actor.id))
    db.flush()
    return result
