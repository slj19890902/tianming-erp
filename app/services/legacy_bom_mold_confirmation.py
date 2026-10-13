"""Supplement a proven legacy omission after explicit physical confirmation.

Original stock identity, quantities, production records and costs are immutable.
This narrow adapter accepts ONLY missing child mold IDs on a fully evidenced
stock assembly; specification/process/recipe changes remain incompatible.
"""
import json
import hashlib
from collections import defaultdict
from sqlalchemy import select,update
from sqlalchemy.orm import object_session
from app.models.order import Order,OrderItem
from app.models.user import User
from app.models.stock_preparation import StockPreparationCommand,StockPreparationJob
from app.models.warehouse_inventory import InventoryLot,InventoryMovement
from app.models.audit import OperationLog
from app.services.warehouse_inventory import WarehouseInventoryError,_movement,_balances
from app.services.finished_stock_identity import compiled_product_bases,matches_stock_identity
from app.services.multilevel_bom_orders import read_compiled_order_bom
from app.services.bom_transactions import atomic_bom
from app.services.audit_log import append_audit_event


def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def fail():
    raise WarehouseInventoryError('该批次不属于已证实的历史模具号漏记，不能补充匹配；请核对原规格、工艺和配比',409)


def preview(db,*,lot_id,order_item_id):
    lot=db.get(InventoryLot,lot_id);item=db.get(OrderItem,order_item_id)
    if not lot or not item or not lot.finished_detail:fail()
    detail=lot.finished_detail;order=db.get(Order,item.order_id)
    if (lot.source_ref_type!='preparation_assembly' or lot.source_ref_id!=lot.id
            or lot.status!='active' or lot.quantity_available<=0 or lot.quantity_reserved or lot.quantity_consumed
            or detail.is_general or detail.owner_customer_id!=order.customer_id):fail()
    compiled=read_compiled_order_bom(db,item.id)
    if not compiled or detail.product_id!=compiled.graph.root_id:fail()
    bases=compiled_product_bases(compiled);wanted=json.loads(bases[detail.product_id])
    try:
        actual=json.loads(detail.physical_basis_json)
        if {k:v for k,v in actual.items() if k!='assembly'}!={k:v for k,v in wanted.items() if k!='assembly'}:fail()
        old_children={c['product_id']:c for c in actual['assembly']};new_children={c['product_id']:c for c in wanted['assembly']}
        if not old_children or set(old_children)!=set(new_children) or len(old_children)!=len(actual['assembly']):fail()
        entry=db.scalar(select(InventoryMovement).where(InventoryMovement.inventory_lot_id==lot.id,
            InventoryMovement.movement_type=='manual_in',InventoryMovement.idempotency_key.like('prep-kit:%')))
        if not entry:fail()
        key=entry.idempotency_key[len('prep-kit:'):];command=db.get(StockPreparationCommand,key)
        if not command:fail()
        result=json.loads(command.result_json)
        if (result['action']!='assemble' or result['output_lot_id']!=lot.id or result['sets']!=entry.quantity
                or result['recipe']['parent_id']!=detail.product_id or result['recipe']['customer_id']!=order.customer_id
                or not matches_stock_identity(result['recipe']['parent_basis'],detail.physical_basis_json)
                or command.actor_id!=entry.operator_id):fail()
        quantities=defaultdict(int);proof=[];molds={}
        for source in result['inputs']:
            pid=source['product_id'];child=db.get(InventoryLot,source['lot_id'])
            if not child or child.source_ref_type!='stock_preparation' or pid not in old_children:fail()
            job=db.get(StockPreparationJob,child.source_ref_id)
            if not job or job.status!='completed' or job.product_id!=pid or job.output_lot_id!=child.id:fail()
            snapshot=json.loads(job.product_snapshot);basis=snapshot['physical_basis'];value=json.loads(basis);target=json.loads(bases[pid])
            if hashlib.sha256(basis.encode()).hexdigest()!=old_children[pid]['basis_hash']:fail()
            if old_children[pid]['quantity']!=new_children[pid]['quantity']:fail()
            if (value.get('mold_tool_id') is not None or not str(target.get('mold_tool_id') or '').isdigit()
                    or int(target['mold_tool_id'])<=0 or not snapshot.get('sheet_cutting_snapshot',{}).get('is_die_cut')
                    or '模切' not in str(value.get('production_process') or '')):fail()
            if {k:v for k,v in value.items() if k!='mold_tool_id'}!={k:v for k,v in target.items() if k!='mold_tool_id'}:fail()
            movement=db.scalar(select(InventoryMovement).where(InventoryMovement.idempotency_key==source['movement_key']))
            if (not movement or movement.inventory_lot_id!=child.id or movement.movement_type!='consume'
                    or movement.quantity!=source['quantity'] or movement.operator_id!=command.actor_id):fail()
            quantities[pid]+=source['quantity'];molds[pid]=int(target['mold_tool_id'])
            proof.append(dict(job_id=job.id,job_version=job.version,snapshot_hash=digest(snapshot),movement_id=movement.id,
                movement_quantity=movement.quantity,source_lot_id=child.id))
        if dict(quantities)!={pid:entry.quantity*c['quantity'] for pid,c in old_children.items()}:fail()
        from app.models.mold_tool import MoldTool
        from app.services.mold_location import describe_mold_location
        facts=[dict(product_name=next(n.name for n in compiled.graph.nodes if n.product_id==pid),
            mold_name=db.get(MoldTool,mid).mold_name,mold_code=db.get(MoldTool,mid).mold_code,
            mold_location=describe_mold_location(db.get(MoldTool,mid).rack_location)['prompt']) for pid,mid in sorted(molds.items())]
        response=dict(lot_id=lot.id,lot_number=lot.lot_number,order_item_id=item.id,quantity_available=lot.quantity_available,
            lot_version=lot.version,actual_hash=digest(actual),confirmed_basis=bases[detail.product_id],mold_facts=facts,proof=proof)
        response['preview_hash']=digest(response)
        return response
    except (ValueError,KeyError,TypeError,AttributeError):
        fail()


def confirm(db,*,lot_id,order_item_id,preview_hash,operation_key,physical_confirmed,actor):
    if not actor or not actor.is_active or actor.role!='admin':raise WarehouseInventoryError('仅活动管理员可确认实物模具',403)
    if not physical_confirmed:raise WarehouseInventoryError('请现场核对本批实物使用的子件模具后明确确认',409)
    key='legacy-bom-mold:'+operation_key
    request=dict(lot_id=lot_id,order_item_id=order_item_id,preview_hash=preview_hash,actor_id=actor.id)
    with atomic_bom(db):
        old=db.scalar(select(InventoryMovement).where(InventoryMovement.idempotency_key==key))
        if old:
            if json.loads(old.remarks or '{}').get('request')!=request:raise WarehouseInventoryError('该操作标识已用于不同确认',409)
            return dict(lot_id=lot_id,movement_id=old.id,replayed=True,message='原实物确认已完成')
        facts=preview(db,lot_id=lot_id,order_item_id=order_item_id)
        if preview_hash!=facts['preview_hash']:raise WarehouseInventoryError('批次或订单依据已变化，请重新核对',409)
        changed=db.execute(update(InventoryLot).where(InventoryLot.id==lot_id,InventoryLot.version==facts['lot_version']).values(version=InventoryLot.version+1))
        if changed.rowcount!=1:raise WarehouseInventoryError('批次已变化，请重新核对',409)
        lot=db.get(InventoryLot,lot_id);db.refresh(lot);before=_balances(lot)
        evidence=dict(request=request,actual_hash=facts['actual_hash'],confirmed_basis=facts['confirmed_basis'])
        movement=_movement(db,lot=lot,movement_type='adjust',quantity=0,before=before,operator_id=actor.id,
            reason='确认历史BOM漏记模具',remarks=json.dumps(evidence,ensure_ascii=False,sort_keys=True),idempotency_key=key)
        db.flush()
        append_audit_event(db,event_category='business',result='success',source='web',module_code='warehouse',
            action_code='BOM_LEGACY_MOLD_CONFIRM',resource='inventory_lot',actor=actor,entity_type='inventory_lot',entity_id=lot_id,
            customer_id=lot.finished_detail.owner_customer_id,details=dict(movement_id=movement.id,operation_key=key,preview_hash=preview_hash))
        return dict(lot_id=lot_id,movement_id=movement.id,replayed=False,message='已记录实物模具核对，原冻结身份和数量不变；可确认采用本批成品')


def confirmed_match(lot,expected):
    if (getattr(lot,'source_ref_type',None)!='preparation_assembly'
            or not hasattr(lot,'_sa_instance_state')):return False
    db=object_session(lot)
    if not db or not expected or not lot.finished_detail:return False
    try:
        actual_hash=digest(json.loads(lot.finished_detail.physical_basis_json))
        rows=db.scalars(select(InventoryMovement).where(InventoryMovement.inventory_lot_id==lot.id,
            InventoryMovement.movement_type=='adjust',InventoryMovement.quantity==0,
            InventoryMovement.reason=='确认历史BOM漏记模具'))
        for row in rows:
            evidence=json.loads(row.remarks or '{}')
            request=evidence.get('request',{})
            if (request.get('lot_id')!=lot.id or request.get('actor_id')!=row.operator_id
                    or row.before_available!=row.after_available or row.before_reserved!=row.after_reserved
                    or row.before_consumed!=row.after_consumed or row.before_damaged!=row.after_damaged
                    or row.before_scrapped!=row.after_scrapped):continue
            if evidence.get('actual_hash')!=actual_hash or not matches_stock_identity(evidence.get('confirmed_basis'),expected):continue
            logs=db.scalars(select(OperationLog).where(OperationLog.action_code=='BOM_LEGACY_MOLD_CONFIRM',
                OperationLog.entity_id==lot.id,OperationLog.user_id==row.operator_id,OperationLog.role=='admin',OperationLog.result=='success',
                OperationLog.customer_id_snapshot==lot.finished_detail.owner_customer_id))
            if any(json.loads(log.details or '{}').get('movement_id')==row.id for log in logs):return True
    except (ValueError,TypeError,KeyError):return False
    return False
