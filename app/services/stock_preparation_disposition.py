"""Explicit stock disposition; no order automation or historical backfill."""
import json
from collections import defaultdict
from decimal import Decimal
from datetime import datetime
from sqlalchemy import select, update
from app.models.stock_preparation import StockPreparationJob as Job, StockPreparationCommand as Command
from app.models.warehouse_inventory import InventoryLot, InventoryMovement, InventoryReservation
from app.models.warehouse_goods import WarehouseGoodsProfile
from app.models.product import Product
from app.core.time_contract import utc_now_naive, utc_naive_to_api, beijing_today
from app.services import stock_preparation as prep
from app.services.stock_preparation_groups import encode, digest, destination
from app.services.warehouse_inventory import (_location, _claim_inventory_transfer_locations,
    manual_semi_finished_in, manual_finished_in, _claim_inventory_restore_destination)
from app.services.audit_log import append_audit_event


def relocate_material(db, lot, location_id, layout_version, actor, key):
    if lot.inventory_type != 'semi_finished' or lot.quantity_reserved or lot.pallet_item:
        prep.fail('有生产预占或栈板绑定，请先处理预占或从栈板移货')
    _claim_inventory_transfer_locations(db,source_location_id=lot.warehouse_location_id,
        target_location_id=location_id,expected_source_layout_version=None,
        expected_target_layout_version=layout_version)
    _location(db,location_id,'semi_finished',capacity_source_location_id=lot.warehouse_location_id)
    old=lot.warehouse_location_id
    if old!=location_id:
        lot.warehouse_location_id=location_id
        prep._movement(db,lot=lot,movement_type='location_transfer',quantity=0,before=prep._balances(lot),
            operator_id=actor.id,reason='备料存放',remarks=encode(dict(from_location_id=old,to_location_id=location_id)),idempotency_key='prep-place:'+key)


def semi_output(db, job, source, item, quantity, place, payload, actor):
    snapshot=json.loads(job.product_snapshot);detail=source.semi_finished_detail
    # Keep the measured source-board dimensions; do not invent a cutting layout.
    # Product binding and 1:1 output pieces prevent a second yield multiplication.
    lot=manual_semi_finished_in(db,location_id=place.id,quantity=quantity,stock_date=beijing_today(),source_type='transfer',
        material_code=detail.material_code_snapshot,layer_count=detail.layer_count,flute_type=detail.flute_type,
        board_length_mm=detail.board_length_mm,board_width_mm=detail.board_width_mm,sheet_type='net_sheet',
        component_type=detail.component_type,pieces_per_box=1,stock_yield_per_sheet=1,supplier_name=detail.supplier_name,
        customer_id=item.customer_id,crease_type=detail.crease_type,crease_left_mm=detail.crease_left_mm,
        crease_middle_mm=detail.crease_middle_mm,crease_right_mm=detail.crease_right_mm,cutting_note='已加工子件；尺寸为来源纸板尺寸',
        remarks='备库加工半成品',operator_id=actor.id,idempotency_key=f'prep-out:{job.id}',
        source_ref_type='stock_preparation',source_ref_id=job.id,expected_layout_version=payload['layout_version'],
        internal_name=snapshot['name'],capture_material_cost=False)
    db.add(WarehouseGoodsProfile(lot_id=lot.id,data_json=encode(dict(scope='customers',customer_ids=[item.customer_id],
        product_ids=[job.product_id],processing='cut',mold_tool_id=None,verified_material_id=None,
        material_code=detail.material_code_snapshot,face_paper='unknown',usage_confirmed=True,
        output_piece=True,physical_basis=snapshot['physical_basis'],dimension_basis='source_board'))))
    return lot


def members(db,key):
    result=[j for j in db.scalars(select(Job).order_by(Job.id)) if (json.loads(j.product_snapshot).get('preparation_group') or {}).get('key')==key]
    if not result:prep.fail('整组记录不存在')
    return result


def assembly_rows(db,scope=None):
    result=[]
    reversals={json.loads(c.request_json).get('assembly_key') for c in db.scalars(select(Command)) if json.loads(c.request_json).get('action')=='unassemble'}
    for c in db.scalars(select(Command).order_by(Command.created_at,Command.operation_key)):
        req=json.loads(c.request_json)
        if req.get('action')!='assemble':continue
        data=json.loads(c.result_json)
        if not data.get('output_lot_id'):continue
        if scope is not None and data['recipe']['customer_id'] not in scope:continue
        lot=db.get(InventoryLot,data['output_lot_id'])
        family=list(db.scalars(select(InventoryLot).where(InventoryLot.source_ref_type=='preparation_assembly',InventoryLot.source_ref_id==data['output_lot_id'])))
        result.append(dict(data,key=c.operation_key,reversed=c.operation_key in reversals,version=lot.version,
            available=sum(l.quantity_available for l in family),physical=sum(l.quantity_available+l.quantity_reserved+l.quantity_damaged for l in family),location=' / '.join(dict.fromkeys(prep.location_name(db,l) for l in family if l.quantity_available+l.quantity_reserved)),
            completed_at=utc_naive_to_api(c.created_at),source_versions=[dict(lot_id=i['lot_id'],version=db.get(InventoryLot,i['lot_id']).version) for i in data['inputs']]))
    return result


def assert_assembly_evidence(db,key,product_id,customer_id,quantity):
    command=db.get(Command,key)
    if not command:prep.fail('缺少组装来源记录')
    data=json.loads(command.result_json)
    if data.get('recipe',{}).get('parent_id')!=product_id or data['recipe']['customer_id']!=customer_id or data.get('sets')!=quantity or data.get('output_lot_id'):
        prep.fail('组装身份与数量不一致')
    quantities=defaultdict(int)
    for source in data['inputs']:
        movement=db.scalar(select(InventoryMovement).where(InventoryMovement.idempotency_key==source['movement_key']))
        if not movement or movement.movement_type!='consume' or movement.inventory_lot_id!=source['lot_id'] or movement.quantity!=source['quantity']:
            prep.fail('组装子件消耗记录不完整')
        quantities[source['product_id']]+=source['quantity']
    if dict(quantities)!={c['product_id']:quantity*c['per_set'] for c in data['recipe']['children']}:
        prep.fail('组装数量与冻结配比不符')


def assemble(db,payload,actor):
    key=payload['operation_key'];request=encode(payload);old=db.get(Command,key)
    if old:
        if old.actor_id!=actor.id or old.request_json!=request:prep.fail('操作标识已用于其他内容')
        return json.loads(old.result_json)
    jobs=members(db,payload['group_key']);group=json.loads(jobs[0].product_snapshot)['preparation_group'];recipe=group['recipe']
    if any(j.status!='completed' for j in jobs):prep.fail('子件尚未完成，不能组装入库')
    submitted={v['job_id']:v for v in payload['jobs']}
    if len(submitted)!=len(payload['jobs']) or set(submitted)!={j.id for j in jobs}:prep.fail('请核对整组所有子件')
    sets=payload['sets'];destination(db,payload['location_id'],payload['layout_version'])
    remaining={c['product_id']:sets*c['per_set'] for c in recipe['children']};inputs=[];total_cost=Decimal(0);cost_known=True
    for job in jobs:
        v=submitted[job.id];lot=db.get(InventoryLot,job.output_lot_id)
        if not lot or lot.version!=v['output_version'] or job.version!=v['job_version'] or lot.source_ref_type!='stock_preparation' or lot.source_ref_id!=job.id or lot.status!='active':prep.fail('子件库存已变化，请刷新')
        take=min(remaining[job.product_id],lot.quantity_available)
        if not take:continue
        before=prep._balances(lot)
        changed=db.execute(update(InventoryLot).where(InventoryLot.id==lot.id,InventoryLot.version==v['output_version'],InventoryLot.quantity_available>=take).values(quantity_available=InventoryLot.quantity_available-take,quantity_consumed=InventoryLot.quantity_consumed+take,version=InventoryLot.version+1,last_movement_at=utc_now_naive()))
        if changed.rowcount!=1:prep.fail('子件数量已变化')
        db.refresh(lot);movement_key='prep-assembly:'+digest([key,job.id])[:50]
        prep._movement(db,lot=lot,movement_type='consume',quantity=take,before=before,operator_id=actor.id,reason='备库子件组装成套',idempotency_key=movement_key)
        inputs.append(dict(lot_id=lot.id,quantity=take,product_id=job.product_id,movement_key=movement_key,version=lot.version))
        remaining[job.product_id]-=take
        if lot.estimated_unit_cost_snapshot is None:cost_known=False
        else:total_cost+=Decimal(str(lot.estimated_unit_cost_snapshot))*take
    if any(remaining.values()):prep.fail('可用子件不足，不能形成所填套数')
    result=dict(action='assemble',group_key=payload['group_key'],sets=sets,recipe=recipe,inputs=inputs,output_lot_id=None)
    command=Command(operation_key=key,receipt_item_id=jobs[0].receipt_item_id,request_json=request,result_json=encode(result),actor_id=actor.id)
    db.add(command);db.flush()
    from app.services.finished_stock_identity import product_basis,_with_assembly,_child_basis
    parent=db.get(Product,recipe['parent_id'])
    if (not group.get('parent_basis') and parent.version!=recipe['version']) or parent.customer_id!=recipe['customer_id']:prep.fail('组合资料已变化，请先核对原配方')
    basis=_with_assembly(group.get('parent_basis') or product_basis(parent),[
        _child_basis(c['product_id'],c['per_set'],json.loads(next(j for j in jobs if j.product_id==c['product_id']).product_snapshot)['physical_basis']) for c in recipe['children']])
    output=manual_finished_in(db,customer_id=recipe['customer_id'],product_id=recipe['parent_id'],location_id=payload['location_id'],quantity=sets,
        stock_date=beijing_today(),source_type='transfer',remarks='备库子件已组装成套',operator_id=actor.id,idempotency_key='prep-kit:'+key,
        source_ref_type='preparation_assembly',source_ref_id=jobs[0].id,expected_layout_version=payload['layout_version'],
        physical_basis_json=basis,assembly_command_key=key)
    output.source_ref_id=output.id
    output.finished_detail.inventory_code_snapshot=recipe['code'];output.finished_detail.product_name_snapshot=recipe['name']+'（成套）'
    output.estimated_unit_cost_snapshot=total_cost/sets if cost_known else None
    output.cost_snapshot_source='stock_preparation_assembly';output.cost_snapshot_detail_json=encode(dict(inputs=inputs,total_cost=str(total_cost) if cost_known else None))
    for source in inputs:release_empty_output_pallet(db,db.get(InventoryLot,source['lot_id']),actor)
    result['output_lot_id']=output.id;result['placed_at_utc']=utc_now_naive().isoformat();command.result_json=encode(result)
    append_audit_event(db,event_category='business',result='success',source='web',module_code='production',action_code='stock_preparation.assemble',resource='production',actor=actor,entity_type='inventory_lot',entity_id=output.id,details=result)
    db.flush();return result


def dispose(db,payload,actor):
    key=payload['operation_key'];request=encode(payload);old=db.get(Command,key)
    if old:
        if old.actor_id!=actor.id or old.request_json!=request:prep.fail('操作标识已用于其他内容')
        return json.loads(old.result_json)
    jobs=members(db,payload['group_key']);submitted={v['job_id']:v for v in payload['jobs']}
    if len(submitted)!=len(payload['jobs']) or set(submitted)!={j.id for j in jobs}:prep.fail('必须核对整组全部子件')
    for job in jobs:
        v=submitted[job.id];group=json.loads(job.product_snapshot)['preparation_group']
        place_id=payload['location_id'] if payload['disposition']=='finished' else v['location_id']
        version=payload['layout_version'] if payload['disposition']=='finished' else v['layout_version']
        destination(db,place_id,version,"semi_finished")
        prep.mutate(db,receipt_id=job.receipt_item_id,payload=dict(action='complete',operation_key=digest([key,job.id])[:60],lot_version=v['lot_version'],job_id=job.id,job_version=v['job_version'],actual_output=v['actual_output'],location_id=place_id,layout_version=version,confirm_overproduction=payload['confirm_overproduction']),actor=actor,group_snapshot=group,output_kind='semi')
    result=dict(action='dispose',group_key=payload['group_key'],disposition=payload['disposition'],job_ids=[j.id for j in jobs])
    if payload['disposition']=='finished':
        result['assembly']=assemble(db,dict(action='assemble',operation_key=digest([key,'assemble'])[:60],group_key=payload['group_key'],sets=payload['sets'],location_id=payload['location_id'],layout_version=payload['layout_version'],jobs=[dict(job_id=j.id,job_version=j.version,output_version=db.get(InventoryLot,j.output_lot_id).version) for j in jobs]),actor)
    db.add(Command(operation_key=key,receipt_item_id=jobs[0].receipt_item_id,request_json=request,result_json=encode(result),actor_id=actor.id));db.flush()
    return result



def unassemble_block(db,row):
    if row['reversed']:return '已撤销组装'
    lot=db.get(InventoryLot,row['output_lot_id'])
    if lot.status!='active' or lot.quantity_available!=row['sets'] or any((lot.quantity_reserved,lot.quantity_consumed,lot.quantity_damaged,lot.quantity_scrapped)):
        return '成套库存已使用、预占或调整'
    movements=list(db.scalars(select(InventoryMovement).where(InventoryMovement.inventory_lot_id==lot.id)))
    if not movements or any(m.movement_type!='manual_in' for m in movements):return '成套库存已有后续操作'
    if db.scalar(select(InventoryReservation.id).where(InventoryReservation.inventory_lot_id==lot.id).limit(1)):return '成套库存已有预占记录'
    for source in row['inputs']:
        child=db.get(InventoryLot,source['lot_id'])
        if child.pallet_item and not child.pallet_item.pallet.is_current:
            return '原子件栈板已清空或变更，请先核对实际存放位置'
    if lot.pallet_item:
        from app.models.warehouse_inventory import InventoryLocationMovement
        from app.core.time_contract import BEIJING_UTC_OFFSET
        # This is an additional conservative pallet guard; lot transfers are above.
        if db.scalar(select(InventoryLocationMovement.id).where(InventoryLocationMovement.pallet_id==lot.pallet_item.pallet_id,InventoryLocationMovement.movement_type=='move',InventoryLocationMovement.moved_at>datetime.fromisoformat(row['placed_at_utc'])+BEIJING_UTC_OFFSET).limit(1)):
            return '成套库存所在栈板已有移动'
    return None


def unassemble(db,payload,actor):
    key=payload['operation_key'];request=encode(payload);old=db.get(Command,key)
    if old:
        if old.actor_id!=actor.id or old.request_json!=request:prep.fail('操作标识已用于其他内容')
        return json.loads(old.result_json)
    if not payload['confirm_unused']:prep.fail('请确认已核对实物，撤销组装恢复子件半成品')
    row=next((r for r in assembly_rows(db) if r['key']==payload['assembly_key']),None)
    if not row or row['group_key']!=payload['group_key']:prep.fail('组装记录不一致')
    block=unassemble_block(db,row)
    if block:prep.fail(block)
    lot=db.get(InventoryLot,row['output_lot_id'])
    versions={v['lot_id']:v['version'] for v in payload['sources']}
    if len(versions)!=len(payload['sources']) or set(versions)!={s['lot_id'] for s in row['inputs']}:prep.fail('必须核对全部来源版本')
    if lot.version!=payload['output_version']:prep.fail('成套库存已变化')
    before=prep._balances(lot)
    changed=db.execute(update(InventoryLot).where(InventoryLot.id==lot.id,InventoryLot.version==payload['output_version']).values(quantity_available=0,status='closed',version=InventoryLot.version+1,last_movement_at=utc_now_naive()))
    if changed.rowcount!=1:prep.fail('成套库存版本冲突')
    db.refresh(lot);prep._movement(db,lot=lot,movement_type='adjust',quantity=row['sets'],before=before,operator_id=actor.id,reason='撤销组装产出',idempotency_key='unassemble-out:'+key)
    for source in row['inputs']:
        child=db.get(InventoryLot,source['lot_id']);before=prep._balances(child)
        _claim_inventory_restore_destination(db,child.warehouse_location_id)
        _location(db,child.warehouse_location_id,child.inventory_type)
        changed=db.execute(update(InventoryLot).where(InventoryLot.id==child.id,InventoryLot.version==versions[child.id],InventoryLot.quantity_consumed>=source['quantity'],InventoryLot.status=='active').values(quantity_available=InventoryLot.quantity_available+source['quantity'],quantity_consumed=InventoryLot.quantity_consumed-source['quantity'],version=InventoryLot.version+1,last_movement_at=utc_now_naive()))
        if changed.rowcount!=1:prep.fail('子件来源已变化，不能撤销')
        db.refresh(child);prep._movement(db,lot=child,movement_type='reverse_consume',quantity=source['quantity'],before=before,operator_id=actor.id,reason='撤销组装恢复子件',idempotency_key='unassemble-in:'+digest([key,child.id])[:50])
    release_empty_output_pallet(db,lot,actor)
    result=dict(action='unassemble',assembly_key=row['key'],group_key=row['group_key'])
    original=db.get(Command,row['key'])
    db.add(Command(operation_key=key,receipt_item_id=original.receipt_item_id,request_json=request,result_json=encode(result),actor_id=actor.id))
    append_audit_event(db,event_category='business',result='success',source='web',module_code='production',action_code='stock_preparation.unassemble',resource='production',actor=actor,entity_type='inventory_lot',entity_id=lot.id,details=result)
    db.flush();return result



def release_empty_output_pallet(db,lot,actor):
    if not lot.pallet_item:return
    pallet=lot.pallet_item.pallet
    from app.services.warehouse_inventory import _pallet_has_physical_goods
    if not pallet.is_current or _pallet_has_physical_goods(db,pallet.id):return
    from app.services.floor3_locations import clear_pallet
    from app.services.warehouse_ground_slots import release_ground_occupancy_for_pallet
    clear_pallet(db,pallet_id=pallet.id,expected_version=pallet.version,remarks='组装库存流转后空位',operator_id=actor.id)
    release_ground_occupancy_for_pallet(db,pallet_id=pallet.id,operator_id=actor.id)



def store_output(db,receipt_id,payload,actor):
    key=payload['operation_key'];request=encode(dict(payload,receipt_id=receipt_id));old=db.get(Command,key)
    if old:
        if old.actor_id!=actor.id or old.request_json!=request:prep.fail('操作标识已用于其他内容')
        return json.loads(old.result_json)
    job=db.get(Job,payload['job_id'])
    if not job or job.receipt_item_id!=receipt_id or job.status!='completed' or job.version!=payload['job_version']:prep.fail('半成品来源已变化')
    lot=db.get(InventoryLot,job.output_lot_id)
    if not lot or lot.version!=payload['output_version'] or lot.status!='active' or lot.quantity_available<=0:prep.fail('半成品库存已变化')
    relocate_material(db,lot,payload['location_id'],payload['layout_version'],actor,key)
    changed=db.execute(update(InventoryLot).where(InventoryLot.id==lot.id,InventoryLot.version==payload['output_version']).values(version=InventoryLot.version+1,last_movement_at=utc_now_naive()))
    if changed.rowcount!=1:prep.fail('半成品库存版本冲突')
    result=dict(action='store_output',job_id=job.id,lot_id=lot.id,location_id=payload['location_id'])
    db.add(Command(operation_key=key,receipt_item_id=receipt_id,request_json=request,result_json=encode(result),actor_id=actor.id))
    append_audit_event(db,event_category='business',result='success',source='web',module_code='production',action_code='stock_preparation.store_output',resource='production',actor=actor,entity_type='inventory_lot',entity_id=lot.id,details=result)
    db.flush();return result


def store_outputs(db,payload,actor):
    key=payload['operation_key'];request=encode(payload);old=db.get(Command,key)
    if old:
        if old.actor_id!=actor.id or old.request_json!=request:prep.fail('操作标识已用于其他内容')
        return json.loads(old.result_json)
    jobs=members(db,payload['group_key']);submitted={v['job_id']:v for v in payload['jobs']}
    if len(submitted)!=len(payload['jobs']) or set(submitted)!={j.id for j in jobs}:prep.fail('必须核对整组子件')
    result=dict(action='store_outputs',group_key=payload['group_key'],items=[])
    for job in jobs:
        v=submitted[job.id]
        lot=db.get(InventoryLot,job.output_lot_id)
        if job.status!='completed' or job.version!=v['job_version'] or not lot or lot.version!=v['output_version']:prep.fail('半成品库存已变化')
        if not lot.quantity_available and not lot.quantity_reserved:
            continue
        result['items'].append(store_output(db,job.receipt_item_id,dict(action='store_output',operation_key=digest([key,job.id])[:60],job_id=job.id,job_version=v['job_version'],output_version=v['output_version'],location_id=v['location_id'],layout_version=v['layout_version']),actor))
    db.add(Command(operation_key=key,receipt_item_id=jobs[0].receipt_item_id,request_json=request,result_json=encode(result),actor_id=actor.id));db.flush();return result
