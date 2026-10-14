"""Bounded unordered assembly of existing finished pieces; caller owns commit."""
import hashlib
import json
from decimal import Decimal
from sqlalchemy import select, update
from app.core.time_contract import utc_now_naive, beijing_today
from app.models.finance import FinanceIdempotencyRecord
from app.models.delivery import Delivery, DeliveryItem
from app.models.product import Product
from app.models.warehouse_inventory import (InventoryLot, InventoryMovement, InventoryLotTransfer,
    InventoryReservation, UnorderedFinishedDeliveryAllocation)
from app.services import warehouse_inventory as inv
from app.services.audit_log import append_audit_event
from app.services.bom_subkit_costs import source_cost
from app.services.bom_transactions import atomic_bom
from app.services.finished_stock_identity import product_basis, identity_preview, confirm_identity
from app.services.warehouse_goods_correction import split_for_correction
from app.services.warehouse_inventory import WarehouseInventoryError

CUSTOMER_ID=136
PARENT_ID=3863
LOWER_ID=3496
UPPER_ID=3787
LOWER_LOT=666
UPPER_LOT=1421
LOCATION_ID=1918
QUANTITY=50
SOURCE='physical_kit_correction_20261014'
ACTION=SOURCE
TOTAL_COST=Decimal('641.3350')
UNIT_COST=Decimal('12.8267')


def _json(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),default=str)


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _fail(message,code=409):
    raise WarehouseInventoryError(message,code)


def _authorize(db,actor):
    if actor is None or not actor.is_active or actor.role!='admin':
        _fail('仅活动管理员可执行本次既有成品实物组套更正',403)
    from app.api.deps import require_customer_access
    require_customer_access(CUSTOMER_ID,actor,db)


def _scope(product_id,quantity,target_location_id,lower_lot_id,upper_lot_id):
    if any(type(v) is not int for v in (product_id,quantity,target_location_id,lower_lot_id,upper_lot_id)) or (
        product_id,quantity,target_location_id,lower_lot_id,upper_lot_id)!=(PARENT_ID,QUANTITY,LOCATION_ID,LOWER_LOT,UPPER_LOT):
        _fail('仅限既有666/1421上下盖各50、原位1918组成3863父件50套',400)


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


def preview(db, *, actor, product_id=PARENT_ID, quantity=QUANTITY, target_location_id=LOCATION_ID,
            lower_lot_id=LOWER_LOT, upper_lot_id=UPPER_LOT):
    """Read-only fixed physical contract. No order is created or reopened."""
    _authorize(db,actor);_scope(product_id,quantity,target_location_id,lower_lot_id,upper_lot_id)
    from app.services.multilevel_bom_master import load_master_structure
    from app.services.bom_subkits import active_subkit_order
    from app.services.bom_inventory_contract import is_body_lot
    structure=load_master_structure(db,PARENT_ID)
    if (structure['profiles'].get(PARENT_ID)!='assembled' or set(structure['products'])!={PARENT_ID,LOWER_ID,UPPER_ID}
            or {(e['parent_id'],e['child_id'],e['quantity'],e['relation']) for e in structure['edges']}
                !={(PARENT_ID,LOWER_ID,1,'assembly'),(PARENT_ID,UPPER_ID,1,'assembly')}):
        _fail('当前实物配方不再是明确的上下盖各一件')
    products=[db.get(Product,pid) for pid in (PARENT_ID,LOWER_ID,UPPER_ID)]
    bases={p.id:product_basis(p) for p in products}
    if any(not p.is_active or p.deleted_at or p.customer_id!=CUSTOMER_ID for p in products):
        _fail('本次组套产品或客户身份已变化')
    if json.loads(bases[PARENT_ID])['unit']!='套' or any(json.loads(bases[pid])['assembly'] for pid in (LOWER_ID,UPPER_ID)):
        _fail('本次父套和独立成品零件角色不一致')
    lots=[db.get(InventoryLot,lid) for lid in (LOWER_LOT,UPPER_LOT)]
    for lot,pid in zip(lots,(LOWER_ID,UPPER_ID)):
        if (not lot or lot.inventory_type!='finished' or not lot.finished_detail
                or lot.finished_detail.product_id!=pid or lot.finished_detail.owner_customer_id!=CUSTOMER_ID
                or lot.finished_detail.is_general or lot.status!='active' or lot.unit!='boxes'
                or lot.warehouse_location_id!=LOCATION_ID or lot.quantity_available!=210
                or any((lot.quantity_reserved,lot.quantity_consumed,lot.quantity_damaged,lot.quantity_scrapped))
                or is_body_lot(lot) or active_subkit_order(db,lot) is not None):
            _fail('来源成品身份、位置、原210件余额或有效用途已变化')
    if lots[0].version!=1 or lots[0].finished_detail.physical_basis_json is not None:
        _fail('下盖原批不再是v1且历史basis缺失的批准范围')
    if lots[1].finished_detail.physical_basis_json!=bases[UPPER_ID]:
        _fail('上盖实物basis与本次冻结配方不一致')
    outstanding=db.scalar(select(InventoryReservation.id).where(
        InventoryReservation.inventory_lot_id.in_((LOWER_LOT,UPPER_LOT)),InventoryReservation.status!='cancelled',
        InventoryReservation.reserved_stock_quantity>InventoryReservation.consumed_stock_quantity+InventoryReservation.released_stock_quantity))
    if outstanding:_fail('来源仍有活跃预占，不能用于实物组套更正')
    planned=_planned_references(db)
    if any(r['line_id']!=1027 or r['lot_id']!=LOWER_LOT or r['quantity']!=QUANTITY for r in planned):
        _fail('来源存在其他有效无订单待送引用')
    from app.services.fixed_shelf_staging import staging_owner
    if any(staging_owner(db,lot.id) for lot in lots):_fail('原批已实际集货，不能伪造恢复后组套')
    amounts=[source_cost(db,lot,QUANTITY)[0] for lot in lots]
    if amounts!=[Decimal('573.1350'),Decimal('68.2000')] or sum(amounts,Decimal(0))!=TOTAL_COST:
        _fail('原参考成本已变化，不属于已核定641.3350范围')
    result={'schema':1,'customer_id':CUSTOMER_ID,'product_id':PARENT_ID,'quantity':QUANTITY,'target_location_id':LOCATION_ID,
        'actor_id':actor.id,'products':[{'id':p.id,'version':p.version,'basis':bases[p.id]} for p in products],
        'bases':bases,'sources':[_source_document(lot) for lot in lots],
        'source_costs':[str(a) for a in amounts],'total_cost':str(TOTAL_COST),'unit_cost':str(UNIT_COST),
        'target_planned_references':planned,
        'uncertainties':['仅拆出下盖50确认；原下盖160保持NULL历史basis','继承参考成本，不认定现金采购成本',
                        '外层先退休旧1027，再重新预览；原planned历史保留','不创建或复活订单，不产生新的采购/生产事实']}
    result['preview_hash']=_digest(result);return result


def _replay(db,key,request):
    record=db.scalar(select(FinanceIdempotencyRecord).where(FinanceIdempotencyRecord.idempotency_key==key))
    if not record:return None
    if record.action!=SOURCE or record.actor_user_id!=request['actor_id'] or record.request_hash!=_digest(request):
        _fail('实物组套更正标识已用于其他确认或管理员')
    try:
        value=json.loads(record.response_json);receipt=value['receipt']
        if value['state']!='posted' or value['request']!=request or receipt['quantity']!=QUANTITY or receipt['product_id']!=PARENT_ID:
            _fail('完整实物组套回执与固定范围不一致')
        if record.resource_type!='inventory_lot' or record.resource_id!=receipt['output_lot_id']:_fail('回执来源不一致')
        output=db.get(InventoryLot,record.resource_id)
        if not output or validate_source_cost(db,output,json.loads(output.cost_snapshot_detail_json),set())[0] is None:
            _fail('完整实物组套回执损坏，不能重放')
    except (ValueError,TypeError,KeyError):_fail('完整实物组套回执损坏，不能再次执行')
    return {**receipt,'replayed':True}


def _consume_input(db,lot,quantity,basis,actor,key):
    from app.services.inventory_valuation import require_inherited_entry_cost
    from app.services.delivery_quantities import require_physical_stock
    if lot.finished_detail.physical_basis_json!=basis:_fail('投入实物身份不再等于已确认零件basis')
    wanted=json.loads(basis)
    require_physical_stock(lot,dict(customer_basis=1,physical_basis=1,customer_id=CUSTOMER_ID,
        product_id=wanted['product_id'],physical_unit=wanted['unit']))
    require_inherited_entry_cost(db,lot)
    amount,lineage=source_cost(db,lot,quantity)
    before=inv._balances(lot);version=lot.version
    changed=db.execute(update(InventoryLot).where(InventoryLot.id==lot.id,InventoryLot.version==version,
        InventoryLot.status=='active',InventoryLot.inventory_type=='finished',InventoryLot.quantity_available>=quantity,
        InventoryLot.quantity_reserved==0,InventoryLot.quantity_damaged==0,InventoryLot.quantity_scrapped==0).values(
        quantity_available=InventoryLot.quantity_available-quantity,quantity_consumed=InventoryLot.quantity_consumed+quantity,
        version=InventoryLot.version+1,last_movement_at=utc_now_naive()))
    if changed.rowcount!=1:_fail('实物投入库存版本或余额已变化')
    db.refresh(lot)
    movement=inv._movement(db,lot=lot,movement_type='consume',quantity=quantity,before=before,operator_id=actor.id,
        reason='既有上下盖实物组套更正投入',idempotency_key=key)
    db.flush()
    return {'lot_id':lot.id,'product_id':wanted['product_id'],'quantity':quantity,'unit':lot.unit,'basis':basis,
        'version_before':version,'version_after':lot.version,'before':before,'after':inv._balances(lot),
        'movement_id':movement.id,'movement_key':key,'unit_cost':str(lot.estimated_unit_cost_snapshot),
        'total_cost':str(amount),'cost_source':lot.cost_snapshot_source,'cost_detail_json':lot.cost_snapshot_detail_json,
        'lineage':lineage}


def execute(db, *, actor, preview_hash, operation_key, evidence, physical_match_confirmed,
            product_id=PARENT_ID, quantity=QUANTITY, target_location_id=LOCATION_ID,
            lower_lot_id=LOWER_LOT, upper_lot_id=UPPER_LOT):
    """Real consumption and typed output; no order/delivery write or commit."""
    _authorize(db,actor);_scope(product_id,quantity,target_location_id,lower_lot_id,upper_lot_id)
    if physical_match_confirmed is not True or not isinstance(evidence,str) or not evidence.strip() or len(evidence)>1000:
        _fail('必须明确确认这次上下盖各50及其成品状态')
    if not isinstance(operation_key,str) or not operation_key.strip() or len(operation_key)>64:_fail('操作标识无效',400)
    operation_key=operation_key.strip();key='physical-kit:'+_digest(operation_key)
    request={'preview_hash':preview_hash,'operation_key':operation_key,'evidence':evidence.strip(),
        'actor_id':actor.id,'product_id':PARENT_ID,'quantity':QUANTITY,'location_id':LOCATION_ID}
    old=_replay(db,key,request)
    if old:return old
    with atomic_bom(db):
        # Serialize all keys at the exact original source before replay again.
        db.execute(update(InventoryLot).where(InventoryLot.id==LOWER_LOT).values(updated_at=InventoryLot.updated_at))
        old=_replay(db,key,request)
        if old:return old
        db.expire_all();proposal=preview(db,actor=actor)
        if proposal['preview_hash']!=preview_hash:_fail('实物、主档、价格或引用变化，请重新预览')
        if proposal['target_planned_references']:_fail('外层须先退休旧目标行并保留planned历史')
        for product in proposal['products']:
            claimed=db.execute(update(Product).where(Product.id==product['id'],Product.version==product['version']).values(
                version=Product.version,updated_at=Product.updated_at))
            if claimed.rowcount!=1:_fail('产品版本已变化')
        for source in proposal['sources']:
            claimed=db.execute(update(InventoryLot).where(InventoryLot.id==source['id'],InventoryLot.version==source['version']).values(
                version=InventoryLot.version,updated_at=InventoryLot.updated_at))
            if claimed.rowcount!=1:_fail('来源版本已变化')
        record=FinanceIdempotencyRecord(idempotency_key=key,request_hash=_digest(request),action=SOURCE,
            actor_user_id=actor.id,resource_type='inventory_lot',resource_id=0,response_json=_json({'state':'pending','request':request}))
        db.add(record);db.flush()
        lower=db.get(InventoryLot,LOWER_LOT);upper=db.get(InventoryLot,UPPER_LOT)
        child=split_for_correction(db,lower,QUANTITY,actor,_digest(operation_key+':split'))
        check=identity_preview(db,child.id)
        if _json(check['physical_basis'])!=proposal['bases'][LOWER_ID]:_fail('拆出零件确认与冻结basis不符')
        confirm_identity(db,lot_id=child.id,preview_hash=check['preview_hash'],operation_key=_digest(operation_key+':identity'),actor=actor)
        inputs=[_consume_input(db,lot,QUANTITY,proposal['bases'][pid],actor,key+':input:'+str(lot.id))
                for lot,pid in ((child,LOWER_ID),(upper,UPPER_ID))]
        if sum((Decimal(r['total_cost']) for r in inputs),Decimal(0))!=TOTAL_COST:_fail('实际投入成本不守恒')
        location=db.get(inv.WarehouseLocation,LOCATION_ID)
        output=inv.manual_finished_in(db,customer_id=CUSTOMER_ID,product_id=PARENT_ID,location_id=LOCATION_ID,
            quantity=QUANTITY,stock_date=beijing_today(),source_type='transfer',source_ref_type=SOURCE,source_ref_id=record.id,
            remarks='已确认现有上下盖各50实际组成50套；不关联取消订单',operator_id=actor.id,
            idempotency_key=key+':output',movement_reason='既有成品实物组套更正入库',
            physical_basis_json=proposal['bases'][PARENT_ID],
            expected_layout_version=location.floor3_layout.version if location.floor3_layout else None)
        split=db.scalar(select(InventoryLotTransfer).where(InventoryLotTransfer.target_lot_id==child.id,
            InventoryLotTransfer.idempotency_key=='correction:'+_digest(operation_key+':split')))
        entry=db.scalar(select(InventoryMovement).where(InventoryMovement.idempotency_key==key+':output'))
        contract={'schema':1,'source':SOURCE,'actor_id':actor.id,'customer_id':CUSTOMER_ID,
            'product_id':PARENT_ID,'quantity':QUANTITY,'location_id':LOCATION_ID,
            'split':{'id':split.id,'source_lot_id':LOWER_LOT,'target_lot_id':child.id,'quantity':QUANTITY},
            'inputs':inputs,'output':{'lot_id':output.id,'entry_movement_id':entry.id,'entry_key':key+':output',
                'basis':proposal['bases'][PARENT_ID],'quantity':QUANTITY,'unit':'boxes','physical_unit':'套'},
            'total_cost':str(TOTAL_COST),'unit_cost':str(UNIT_COST),'currency':'CNY','actual':False}
        detail={'schema_version':1,'source_record_id':record.id,'contract_hash':_digest(contract),
            'currency':'CNY','actual':False,'quantity':QUANTITY,'total_cost':str(TOTAL_COST),
            'product_unit':'套','cost_label':'既有成品实物组套继承参考成本'}
        output.estimated_unit_cost_snapshot=UNIT_COST;output.estimated_square_price_snapshot=Decimal(0)
        output.estimated_cost_area_m2_snapshot=Decimal(0);output.cost_snapshot_source=SOURCE
        output.cost_snapshot_detail_json=_json(detail);output.cost_snapshot_at=utc_now_naive()
        receipt={'source_record_id':record.id,'output_lot_id':output.id,'split_lot_id':child.id,
            'product_id':PARENT_ID,'quantity':QUANTITY,'location_id':LOCATION_ID,
            'source_lot_ids':[LOWER_LOT,UPPER_LOT],'remaining_source_quantities':[160,160],
            'reference_total_cost':str(TOTAL_COST),'reference_unit_cost':str(UNIT_COST),'replayed':False}
        record.resource_id=output.id
        record.response_json=_json({'state':'posted','request':request,'preview':proposal,'contract':contract,'receipt':receipt})
        db.flush();db.refresh(lower);db.refresh(upper);db.refresh(child)
        if lower.quantity_available!=160 or lower.quantity_consumed or lower.finished_detail.physical_basis_json is not None:
            _fail('原下盖160余量或NULL历史身份被改变')
        if upper.quantity_available!=160 or upper.quantity_consumed!=QUANTITY or child.quantity_available or child.quantity_consumed!=QUANTITY:
            _fail('上下盖投入不守恒')
        for lot,before in ((lower,proposal['sources'][0]),(upper,proposal['sources'][1])):
            now=_source_document(lot)
            if now['finished_detail']!=before['finished_detail'] or now['cost']!=before['cost'] or now['location_id']!=LOCATION_ID:
                _fail('来源历史身份、成本或原位被改变')
        from app.services.inventory_valuation import require_inherited_entry_cost
        require_inherited_entry_cost(db,output)
        if output.quantity_available!=QUANTITY or output.quantity_reserved or output.quantity_consumed or output.finished_detail.physical_basis_json!=proposal['bases'][PARENT_ID]:
            _fail('成套入库数量或冻结身份不一致')
        append_audit_event(db,event_category='business',result='success',source='script',module_code='warehouse',
            action_code=SOURCE,resource='inventory_lot',actor=actor,entity_type='inventory_lot',entity_id=output.id,
            object_ref=key,customer_id=CUSTOMER_ID,details={'finance_receipt_id':record.id,'contract_hash':detail['contract_hash'],
                'output_lot_id':output.id,'split_lot_id':child.id,'input_movement_ids':[r['movement_id'] for r in inputs],
                'quantity':QUANTITY,'total_reference_cost':str(TOTAL_COST),'remaining_source_quantities':[160,160]})
        return receipt


def _validate_transfer_movements(db, transfer):
    """Bind a typed descendant edge to its actual paired ledger operation.

    Movements do not store historical positions or lot versions. Preserve the
    typed version contract; only corrections have a fully reproducible hash.
    """
    key=transfer.idempotency_key
    operation=key[len('correction:'):] if key.startswith('correction:') else key
    source=db.scalar(select(InventoryMovement).where(
        InventoryMovement.idempotency_key==inv._transfer_key('goods-correction',operation,'source'))) if key.startswith('correction:') else None
    correction=source is not None
    if not correction:
        operation=key
        source=db.scalar(select(InventoryMovement).where(
            InventoryMovement.idempotency_key==inv._transfer_key('location-transfer',operation,'source')))
    kind='goods-correction' if correction else 'location-transfer'
    same_lot=transfer.source_lot_id==transfer.target_lot_id
    target=db.scalar(select(InventoryMovement).where(
        InventoryMovement.idempotency_key==inv._transfer_key(kind,operation,'target')))
    if correction:
        expected=hashlib.sha256(f'{transfer.source_lot_id}:{transfer.source_version_before}:{transfer.quantity}:{operation}'.encode()).hexdigest()
        if (same_lot or transfer.source_location_id!=transfer.target_location_id
                or transfer.reserved_quantity or transfer.request_hash!=expected):
            raise ValueError('原位拆分位置、数量或已存请求hash不一致')
    names=('available','reserved','consumed','damaged','scrapped')
    for movement,lid in ((source,transfer.source_lot_id),) if same_lot else (
            (source,transfer.source_lot_id),(target,transfer.target_lot_id)):
        if (not movement or movement.inventory_lot_id!=lid or movement.movement_type!='location_transfer'
                or movement.quantity!=transfer.quantity or movement.unit!='boxes'
                or movement.operator_id!=transfer.transferred_by or movement.reversal_of_movement_id is not None
                or any(getattr(movement,prefix+n)<0 for prefix in ('before_','after_') for n in names)
                or any(getattr(movement,field) is not None for field in (
                    'reservation_id','related_order_id','related_order_item_id','related_requisition_id',
                    'related_supplier_order_id','related_delivery_id'))):
            raise ValueError('typed转移缺少精确键绑定的真实库存流水')
    if same_lot:
        if target is not None or any(getattr(source,'before_'+n)!=getattr(source,'after_'+n) for n in names):
            raise ValueError('整批移动的流水余额不守恒')
        if source.before_available+source.before_reserved!=transfer.quantity:
            raise ValueError('整批移动数量与真实可用/预占不一致')
        return
    for name in names:
        amount={'available':transfer.available_quantity,'reserved':transfer.reserved_quantity}.get(name,0)
        if (getattr(source,'after_'+name)!=getattr(source,'before_'+name)-amount
                or getattr(target,'before_'+name)!=0 or getattr(target,'after_'+name)!=amount):
            raise ValueError('typed转移数量与真实移出/移入五余额不守恒')


def validate_source_cost(db,lot,detail,visited):
    """Validate frozen evidence, including typed moved/split descendants.

    Current SKU prices and the current master graph never reinterpret this
    receipt. Quantity/version/location changes are validated by ledger lineage.
    """
    try:
        if db is None or lot.id in visited or len(visited)>30:raise ValueError('成本来源缺库或循环')
        if lot.source_ref_type!=SOURCE or lot.cost_snapshot_source!=SOURCE:raise ValueError('实物组套来源类型不符')
        record=db.get(FinanceIdempotencyRecord,lot.source_ref_id)
        if not record or record.action!=SOURCE or record.resource_type!='inventory_lot':raise ValueError('缺少完整Finance实物来源')
        value=json.loads(record.response_json);request=value['request'];proposal=value['preview'];contract=value['contract']
        if (value['state']!='posted' or record.actor_user_id!=request['actor_id'] or record.request_hash!=_digest(request)
                or proposal['preview_hash']!=request['preview_hash']
                or _digest({k:v for k,v in proposal.items() if k!='preview_hash'})!=proposal['preview_hash']
                or contract['schema']!=1 or contract['source']!=SOURCE or contract['actor_id']!=record.actor_user_id
                or contract['customer_id']!=CUSTOMER_ID or contract['product_id']!=PARENT_ID
                or type(contract['quantity']) is not int or contract['quantity']!=QUANTITY
                or contract['location_id']!=LOCATION_ID or contract['currency']!='CNY' or contract['actual'] is not False
                or Decimal(contract['total_cost'])!=TOTAL_COST or Decimal(contract['unit_cost'])!=UNIT_COST
                or detail.get('source_record_id')!=record.id or detail.get('contract_hash')!=_digest(contract)
                or detail.get('currency')!='CNY' or detail.get('actual') is not False
                or detail.get('quantity')!=QUANTITY or Decimal(detail['total_cost'])!=TOTAL_COST):
            raise ValueError('实物组套来源hash、固定范围或成本合同不一致')
        output=contract['output'];origin=db.get(InventoryLot,output['lot_id'])
        if not origin or record.resource_id!=origin.id or value['receipt']['output_lot_id']!=origin.id:
            raise ValueError('实物组套原输出绑定不一致')
        wanted=json.loads(output['basis'])
        if (output['basis']!=proposal['bases'][str(PARENT_ID)] or wanted['product_id']!=PARENT_ID
                or wanted['unit']!='套' or output['quantity']!=QUANTITY or output['unit']!='boxes'
                or output['physical_unit']!='套' or wanted['schema']!=1):raise ValueError('冻结父套basis不完整')
        parts=contract['inputs']
        if len(parts)!=2 or {r['product_id'] for r in parts}!={LOWER_ID,UPPER_ID}:
            raise ValueError('真实投入不再是两个不同零件')
        if sum((Decimal(r['total_cost']) for r in parts),Decimal(0))!=TOTAL_COST:raise ValueError('投入总成本不守恒')
        expected_children=[]
        for part in parts:
            physical=json.loads(part['basis']);source=db.get(InventoryLot,part['lot_id'])
            movement=db.get(InventoryMovement,part['movement_id'])
            if (part['quantity']!=QUANTITY or part['unit']!='boxes' or physical['product_id']!=part['product_id']
                    or physical['assembly'] or physical['unit'] not in ('只','片') or physical['schema']!=1
                    or not physical.get('spec') or physical.get('material')!='VSNIV' or physical.get('flute')!='AB'
                    or str(physical.get('layer_count'))!='5'
                    or part['basis']!=proposal['bases'][str(part['product_id'])]
                    or Decimal(part['unit_cost'])*QUANTITY!=Decimal(part['total_cost'])
                    or not source or not source.finished_detail or source.finished_detail.product_id!=part['product_id']
                    or source.finished_detail.owner_customer_id!=CUSTOMER_ID or source.unit!='boxes'
                    or source.finished_detail.physical_basis_json!=part['basis']
                    or Decimal(part['unit_cost'])!={LOWER_ID:Decimal('11.4627'),UPPER_ID:Decimal('1.3640')}[part['product_id']]
                    or not movement or movement.inventory_lot_id!=source.id or movement.movement_type!='consume'
                    or movement.quantity!=QUANTITY or movement.unit!='boxes' or movement.operator_id!=record.actor_user_id
                    or movement.reversal_of_movement_id is not None or movement.idempotency_key!=part['movement_key']
                    or movement.related_order_id is not None or movement.related_order_item_id is not None
                    or movement.related_delivery_id is not None or part['version_after']!=part['version_before']+1):
                raise ValueError('投入身份、数量、版本或真实consume来源不一致')
            for name in ('available','reserved','consumed','damaged','scrapped'):
                if (getattr(movement,'before_'+name)!=part['before'][name]
                        or getattr(movement,'after_'+name)!=part['after'][name]):raise ValueError('投入流水before/after不一致')
            if (part['after']['available']!=part['before']['available']-QUANTITY
                    or part['after']['consumed']!=part['before']['consumed']+QUANTITY
                    or any(part['before'][n]!=part['after'][n] for n in ('reserved','damaged','scrapped'))
                    or any(part['before'][n] for n in ('reserved','damaged','scrapped'))):
                raise ValueError('投入流水不守恒')
            expected_children.append({'product_id':part['product_id'],'quantity':1,
                'basis_hash':hashlib.sha256(part['basis'].encode()).hexdigest()})
        if wanted['assembly']!=sorted(expected_children,key=lambda r:r['product_id']):raise ValueError('冻结1+1组套basis不守恒')
        split=contract['split'];edge=db.get(InventoryLotTransfer,split['id'])
        bottom=next(r for r in parts if r['product_id']==LOWER_ID);top=next(r for r in parts if r['product_id']==UPPER_ID)
        if (not edge or edge.source_lot_id!=LOWER_LOT or edge.target_lot_id!=bottom['lot_id']
                or split['target_lot_id']!=bottom['lot_id'] or edge.quantity!=QUANTITY or edge.available_quantity!=QUANTITY
                or edge.reserved_quantity or edge.source_location_id!=LOCATION_ID or edge.target_location_id!=LOCATION_ID
                or edge.source_version_before!=1 or edge.source_version_after!=2 or top['lot_id']!=UPPER_LOT):
            raise ValueError('下盖原位拆50的typed来源不一致')
        entry=db.get(InventoryMovement,output['entry_movement_id'])
        if (not entry or entry.inventory_lot_id!=origin.id or entry.movement_type!='manual_in'
                or entry.quantity!=QUANTITY or entry.unit!='boxes' or entry.operator_id!=record.actor_user_id
                or entry.idempotency_key!=output['entry_key'] or entry.before_available or entry.after_available!=QUANTITY
                or any(getattr(entry,'before_'+n) or getattr(entry,'after_'+n) for n in ('reserved','consumed','damaged','scrapped'))):
            raise ValueError('原成套入库与真实来源不一致')
        siblings=list(db.scalars(select(InventoryLot).where(InventoryLot.source_ref_type==SOURCE,InventoryLot.source_ref_id==record.id)))
        by_id={r.id:r for r in siblings}
        def node_ok(node):
            return (node.inventory_type=='finished' and node.unit=='boxes' and node.finished_detail
                and node.finished_detail.product_id==PARENT_ID and node.finished_detail.owner_customer_id==CUSTOMER_ID
                and not node.finished_detail.is_general and node.finished_detail.physical_basis_json==output['basis']
                and node.cost_snapshot_source==SOURCE and json.loads(node.cost_snapshot_detail_json)==detail
                and Decimal(node.estimated_unit_cost_snapshot)==UNIT_COST)
        if origin.id not in by_id or not all(node_ok(n) for n in siblings):raise ValueError('原输出或派生实物/成本身份不一致')
        if lot.id not in by_id:raise ValueError('派生来源未绑定本次组套')
        if db.scalar(select(InventoryLotTransfer.id).where(InventoryLotTransfer.target_lot_id==origin.id,
                InventoryLotTransfer.source_lot_id!=origin.id).limit(1)) is not None:
            raise ValueError('原输出不能具有其他批次输入或循环')
        def trace(lid):
            seen=set()
            while lid!=origin.id:
                if lid in seen:raise ValueError('成套typed移库来源循环')
                seen.add(lid)
                if len(seen)>100:raise ValueError('成套移库来源过长')
                links=list(db.scalars(select(InventoryLotTransfer).where(InventoryLotTransfer.target_lot_id==lid,
                    InventoryLotTransfer.source_lot_id!=InventoryLotTransfer.target_lot_id)))
                if len(links)!=1:raise ValueError('成套派生缺少唯一typed原输出链')
                transfer=links[0]
                if (transfer.source_lot_id not in by_id or transfer.quantity<=0 or transfer.quantity>QUANTITY
                        or transfer.available_quantity+transfer.reserved_quantity!=transfer.quantity
                        or transfer.reserved_quantity<0 or transfer.source_version_before<1
                        or transfer.source_version_after!=transfer.source_version_before+1):
                    raise ValueError('成套派生跨伪源或typed数量版本不一致')
                _validate_transfer_movements(db,transfer)
                lid=transfer.source_lot_id
        for node in siblings:trace(node.id)
        for moved in db.scalars(select(InventoryLotTransfer).where(
                InventoryLotTransfer.source_lot_id.in_(by_id),
                InventoryLotTransfer.source_lot_id==InventoryLotTransfer.target_lot_id)):
            if (moved.quantity<=0 or moved.quantity>QUANTITY
                    or moved.available_quantity+moved.reserved_quantity!=moved.quantity
                    or moved.source_version_before<1 or moved.source_version_after!=moved.source_version_before+1):
                raise ValueError('整批移动typed数量或版本不一致')
            _validate_transfer_movements(db,moved)
        total=sum(n.quantity_available+n.quantity_reserved+n.quantity_consumed+n.quantity_damaged+n.quantity_scrapped for n in siblings)
        if total!=QUANTITY:raise ValueError('原输出及合法后裔数量不守恒')
        return UNIT_COST,dict(detail,temporary=True,actual=False,product_unit='套',
            cost_label='既有成品实物组套继承参考成本',source_receipt_id=record.id,
            frozen_assembly_spec=[{'product_id':p['product_id'],'quantity_per_set':1,
                'basis_json':p['basis'],'physical_basis':json.loads(p['basis'])} for p in parts])
    except (ValueError,TypeError,KeyError,ArithmeticError,AttributeError) as error:
        return None,dict(detail,validation_issue=str(error))
