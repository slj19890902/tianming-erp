"""Actual assembly of independently processed stock, without creating GET facts."""
import json
from collections import defaultdict
from decimal import Decimal
from sqlalchemy import select, update
from app.models.product import Product
from app.models.stock_preparation import StockPreparationJob as Job, StockPreparationCommand as Command
from app.models.warehouse_inventory import InventoryLot
from app.services import stock_preparation as prep
from app.services.stock_preparation_groups import encode,digest,destination
from app.services.finished_stock_identity import product_basis,matches_stock_identity,_with_assembly,_child_basis
from app.core.time_contract import utc_now_naive,beijing_today


def _plan_identity(plan):
    """Demand quantities differ between purchases; physical recipe identity does not."""
    return digest(dict(parent_product_id=plan['parent_product_id'],parent_product_version=plan['parent_product_version'],
        customer_id=plan['customer_id'],components=sorted([
            dict(product_id=c['product_id'],product_version=c['product_version'],
                 quantity_per_set=str(Decimal(str(c['quantity_per_set'])).normalize()))
            for c in plan['components']],key=lambda c:c['product_id'])))


def _recipe(db,parent_id,jobs):
    parent=db.get(Product,parent_id)
    if not parent or not parent.is_active or parent.deleted_at or not parent.is_composite:
        prep.fail('组合产品已失效，请核对')
    from app.models.multilevel_bom import ProductBomProfile
    parent_profile=db.get(ProductBomProfile,parent_id)
    if parent_profile and parent_profile.source in {'manufactured','purchased'}:
        prep.fail('该父件还需真实本体，请使用BOM生产组装入口')
    plans=[]
    for job in jobs:
        snapshot=json.loads(job.product_snapshot)
        plan=snapshot.get('frozen_bom_plan')
        if plan and plan.get('parent_product_id')==parent_id:
            plans.append((plan,snapshot.get('bom_parent_basis')))
    if plans:
        # Select one physical recipe from live output candidates. Historical
        # versions do not block a newer batch; each preview stays on one recipe.
        selected_identity=_plan_identity(plans[-1][0])
        excluded_recipes=[dict(parent_version=p['parent_product_version'],recipe_hash=_plan_identity(p))
            for p,_ in plans if _plan_identity(p)!=selected_identity]
        plans=[(p,b) for p,b in plans if _plan_identity(p)==selected_identity]
        fingerprints={_plan_identity(p) for p,_ in plans}
        bases={b for _,b in plans}
        if len(fingerprints)!=1 or len(bases)!=1 or None in bases:
            prep.fail('子件来自不同冻结BOM版本，请先核对各批用途')
        plan,basis=plans[0]
        children=[]
        for row in plan['components']:
            child=db.get(Product,row['product_id'])
            count=Decimal(str(row['quantity_per_set']))
            if not child or child.customer_id!=parent.customer_id or int(count)!=count or count<=0:
                prep.fail('冻结BOM子件关系或数量无效')
            children.append(dict(product_id=child.id,code=child.product_code,name=child.product_name,per_set=int(count),unit=child.unit))
        return dict(parent_id=parent_id,customer_id=plan['customer_id'],code=parent.product_code,name=parent.product_name,
            version=plan['parent_product_version'],children=children,parent_basis=basis,frozen_plan_hash=_plan_identity(plan),
            excluded_recipes=excluded_recipes)
    # Explicit old independently processed stock may use a currently matching
    # physical recipe. Unknown or changed child identities are excluded below.
    from app.services.multilevel_bom_master import load_master_structure
    from app.services.multilevel_bom_plan import BomPlanError
    if db.get(ProductBomProfile,parent_id) is None:
        from app.services.stock_preparation_groups import recipe
        result=recipe(db,parent_id)
        return dict(result,parent_basis=product_basis(parent),frozen_plan_hash=None)
    try:
        structure=load_master_structure(db,parent_id)
        edges=[e for e in structure['edges'] if e['parent_id']==parent_id and e['relation']=='assembly']
    except BomPlanError as error:
        prep.fail(str(error))
    if not edges:
        from app.services.stock_preparation_groups import recipe
        result=recipe(db,parent_id)
        return dict(result,parent_basis=product_basis(parent),frozen_plan_hash=None)
    children=[]
    for edge in edges:
        child=structure['products'][edge['child_id']]
        count=edge['quantity']
        if child.is_composite or int(count)!=count or count<=0:
            prep.fail('多级或非整数配方请使用BOM生产入口')
        children.append(dict(product_id=child.id,code=child.product_code,name=child.product_name,per_set=int(count),unit=child.unit))
    return dict(parent_id=parent_id,customer_id=parent.customer_id,code=parent.product_code,name=parent.product_name,
        version=parent.version,children=children,parent_basis=product_basis(parent),frozen_plan_hash=None)


def preview(db,parent_id,sets):
    parent=db.get(Product,parent_id)
    if not parent:
        prep.fail('组合产品不存在')
    jobs=[]
    from app.services.processed_component_stock import available_outputs
    eligible={}
    from app.models.product_bom import ProductBomComponent
    child_ids=set(db.scalars(select(ProductBomComponent.component_product_id).where(ProductBomComponent.parent_product_id==parent_id)))
    excluded=[]
    candidates=db.scalars(select(Job).join(InventoryLot,
        (InventoryLot.source_ref_type=='stock_preparation') & (InventoryLot.source_ref_id==Job.id))
        .where(Job.status=='completed',InventoryLot.status=='active',InventoryLot.quantity_available>0)
        .order_by(Job.id).distinct())
    for job in candidates:
        try:
            snapshot=json.loads(job.product_snapshot)
        except (ValueError,TypeError):
            if job.product_id in child_ids:
                excluded.append(dict(job_id=job.id,reason='历史子件身份不完整，请核对'))
            continue
        contract=snapshot.get('frozen_bom_plan')
        if contract and contract.get('parent_product_id')!=parent_id:
            continue
        if not contract and job.product_id not in child_ids:
            continue
        identity_key=(job.product_id,snapshot.get('physical_basis'))
        if identity_key not in eligible:
            eligible[identity_key]=available_outputs(db,product_id=job.product_id,
                customer_id=parent.customer_id,expected_basis=snapshot.get('physical_basis'))
        if not any(l.source_ref_type=='stock_preparation' and l.source_ref_id==job.id for l in eligible[identity_key]):
            continue
        try:
            _,item,_=prep.source(db,job.receipt_item_id)
        except prep.WarehouseInventoryError as error:
            excluded.append(dict(job_id=job.id,reason=str(error)))
            continue
        if item.customer_id==parent.customer_id:jobs.append(job)
    recipe=_recipe(db,parent_id,jobs)
    by_product=defaultdict(list)
    for job in jobs:
        snapshot=json.loads(job.product_snapshot)
        plan=snapshot.get('frozen_bom_plan')
        if recipe['frozen_plan_hash'] and (not plan or _plan_identity(plan)!=recipe['frozen_plan_hash']):
            continue
        child=next((c for c in recipe['children'] if c['product_id']==job.product_id),None)
        if not child:
            continue
        if not recipe['frozen_plan_hash'] and not matches_stock_identity(snapshot.get('physical_basis'),product_basis(db.get(Product,job.product_id))):
            continue
        for lot in eligible[(job.product_id,snapshot.get('physical_basis'))]:
            if lot.source_ref_type!='stock_preparation' or lot.source_ref_id!=job.id:
                continue
            from app.services.warehouse_goods import goods_profile
            profile=goods_profile(db,lot) or {}
            detail=lot.semi_finished_detail
            if (not detail or detail.owner_customer_id!=parent.customer_id or not profile.get('output_piece')
                    or profile.get('product_ids')!=[job.product_id] or profile.get('remaining_processes')
                    or not matches_stock_identity(profile.get('physical_basis'),snapshot.get('physical_basis'))):
                continue
            by_product[job.product_id].append((job,lot))
    sources=[];capacities=[];shortages=[];component_availability={}
    for child in recipe['children']:
        lots=by_product[child['product_id']]
        available=sum(l.quantity_available for _,l in lots)
        component_availability[child['product_id']]=available
        capacities.append(available//child['per_set'])
        remaining=sets*child['per_set']
        if remaining>available:
            shortages.append(dict(product_id=child['product_id'],name=child['name'],missing=remaining-available,unit=child['unit']))
        for job,lot in lots:
            take=min(remaining,lot.quantity_available)
            if take<=0:break
            sources.append(dict(job_id=job.id,job_version=job.version,output_version=lot.version,
                lot_version=prep.source(db,job.receipt_item_id)[2].version,lot_id=lot.id,
                product_id=job.product_id,product_code=child['code'],product_name=child['name'],unit=child['unit'],
                quantity=take,available=lot.quantity_available,location=prep.location_name(db,lot)))
            remaining-=take
    result=dict(recipe=recipe,parent_product_id=parent_id,sets=sets,sources=sources,
        output_unit=json.loads(recipe['parent_basis']).get('unit'),
        available_sets=min(capacities,default=0),shortages=shortages,excluded_sources=excluded,
        component_availability=component_availability,excluded_recipes=recipe.get('excluded_recipes',[]))
    result['basis_hash']=digest(result)
    return result


def assemble_stock(db,payload,actor):
    key=payload['operation_key'];request=encode(dict(payload,action='assemble'));old=db.get(Command,key)
    if old:
        if old.actor_id!=actor.id or old.request_json!=request:prep.fail('操作标识已用于其他内容')
        return json.loads(old.result_json)
    plan=preview(db,payload['parent_id'],payload['sets'])
    if payload['basis_hash']!=plan['basis_hash']:prep.fail('子件、配比或库存已变化，请重新预览')
    if plan['shortages'] or not plan['sources']:prep.fail('子件不足，不能组套')
    submitted={(v['job_id'],v.get('lot_id')):(v['job_version'],v['output_version']) for v in payload['jobs']}
    expected={(v['job_id'],v['lot_id']):(v['job_version'],v['output_version']) for v in plan['sources']}
    if len(submitted)!=len(payload['jobs']) or submitted!=expected:prep.fail('请核对本次所有实物子件与版本')
    destination(db,payload['location_id'],payload['layout_version'])
    from app.services.inventory_valuation import require_inherited_entry_cost
    total=Decimal(0);inputs=[]
    for row in plan['sources']:
        lot=db.get(InventoryLot,row['lot_id']);require_inherited_entry_cost(db,lot);before=prep._balances(lot)
        from app.services.processed_component_stock import available_outputs,matches_output
        expected_basis=json.loads(db.get(Job,row['job_id']).product_snapshot)['physical_basis']
        if (not matches_output(db,lot,product_id=row['product_id'],customer_id=plan['recipe']['customer_id'],expected_basis=expected_basis)
                or lot.id not in {l.id for l in available_outputs(db,product_id=row['product_id'],
                    customer_id=plan['recipe']['customer_id'],expected_basis=expected_basis)}):
            prep.fail('子件身份、实际位置或集货归属已变化，请刷新')
        take=row['quantity']
        changed=db.execute(update(InventoryLot).where(InventoryLot.id==lot.id,InventoryLot.version==row['output_version'],
            InventoryLot.status=='active',InventoryLot.quantity_available>=take).values(
                quantity_available=InventoryLot.quantity_available-take,quantity_consumed=InventoryLot.quantity_consumed+take,
                version=InventoryLot.version+1,last_movement_at=utc_now_naive()))
        if changed.rowcount!=1:prep.fail('子件库存已变化')
        db.refresh(lot);movement_key='stock-assembly:'+digest([key,lot.id])[:48]
        prep._movement(db,lot=lot,movement_type='consume',quantity=take,before=before,operator_id=actor.id,
            reason='独立加工备库子件实际组套',idempotency_key=movement_key)
        total+=Decimal(str(lot.estimated_unit_cost_snapshot))*take
        inputs.append(dict(lot_id=lot.id,quantity=take,product_id=row['product_id'],movement_key=movement_key,version=lot.version))
    recipe=plan['recipe'];anchor=db.get(Job,plan['sources'][0]['job_id'])
    result=dict(action='assemble',sets=payload['sets'],recipe=recipe,inputs=inputs,output_lot_id=None,group_key='stock:'+str(payload['parent_id']))
    evidence_key='assembly-inputs:'+digest(key)[:45]
    db.add(Command(operation_key=evidence_key,receipt_item_id=anchor.receipt_item_id,
        request_json=encode(dict(action='assemble_inputs',operation_key=key)),result_json=encode(dict(result,final_operation_key=key)),actor_id=actor.id));db.flush()
    bases={r['product_id']:json.loads(db.get(Job,r['job_id']).product_snapshot)['physical_basis'] for r in plan['sources']}
    basis=_with_assembly(recipe['parent_basis'],[_child_basis(c['product_id'],c['per_set'],bases[c['product_id']]) for c in recipe['children']])
    output=prep.manual_finished_in(db,customer_id=recipe['customer_id'],product_id=recipe['parent_id'],
        location_id=payload['location_id'],quantity=payload['sets'],stock_date=beijing_today(),source_type='transfer',
        remarks='独立加工备库子件已实际组套',operator_id=actor.id,idempotency_key='prep-kit:'+key,
        source_ref_type='preparation_assembly',source_ref_id=anchor.id,expected_layout_version=payload['layout_version'],
        physical_basis_json=basis,assembly_command_key=evidence_key)
    output.source_ref_id=output.id
    output.estimated_unit_cost_snapshot=total/payload['sets']
    from app.services.bom_entry_cost import freeze_assembly_standard
    labour=freeze_assembly_standard(db,recipe['parent_id'],[(db.get(InventoryLot,r['lot_id']),r['quantity']) for r in inputs],payload['sets'])
    output.cost_snapshot_source='stock_preparation_assembly';output.cost_snapshot_detail_json=encode(dict(inputs=inputs,total_cost=str(total),**labour))
    from app.services.stock_preparation_disposition import release_empty_output_pallet
    for row in inputs:release_empty_output_pallet(db,db.get(InventoryLot,row['lot_id']),actor)
    result.update(output_lot_id=output.id,placed_at_utc=utc_now_naive().isoformat())
    # Existing assembly history/reversal recognizes the same append-only result.
    stored_request=dict(payload,action='assemble')
    db.add(Command(operation_key=key,receipt_item_id=anchor.receipt_item_id,request_json=encode(stored_request),result_json=encode(result),actor_id=actor.id))
    prep.append_audit_event(db,event_category='business',result='success',source='web',module_code='production',
        action_code='stock_preparation.assemble_stock',resource='production',actor=actor,
        entity_type='inventory_lot',entity_id=output.id,details=result)
    db.flush();return result
