"""Freeze one group disposition's ledger facts before the parent INSERT."""
import json
from collections import defaultdict
from typing import Literal
from urllib.parse import quote
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from app.models.product import Product
from app.models.stock_preparation import StockPreparationJob as Job
from app.models.warehouse_inventory import InventoryLot, InventoryMovement, InventoryReservation, WarehouseLocation
from app.services.stock_preparation import fail
from app.services.stock_preparation_completion_recovery import identity
from app.services.stock_preparation_groups import encode, digest
from app.services.product_unit_labels import basis_unit_label


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class Component(Strict):
    product_id: int = Field(gt=0)
    code: str | None
    name: str | None
    unit: str | None
    per_set: int = Field(gt=0)
    version: int = Field(gt=0)


class Recipe(Strict):
    parent_id: int = Field(gt=0)
    customer_id: int = Field(gt=0)
    code: str | None
    name: str | None
    unit: str | None
    version: int = Field(gt=0)
    children: list[Component] = Field(min_length=1)


class Child(Strict):
    job_id: int = Field(gt=0)
    product_id: int = Field(gt=0)
    receipt_item_id: int = Field(gt=0)
    source_lot_id: int = Field(gt=0)
    source_customer_id: int | None
    requested_job_version: int = Field(gt=0)
    requested_lot_version: int = Field(gt=0)
    input_quantity: int = Field(gt=0)
    input_unit: Literal['张']
    input_stock_unit: str = Field(min_length=1)
    actual_output: int = Field(gt=0)
    output_kind: Literal['semi']
    output_lot_id: int = Field(gt=0)
    output_movement_id: int = Field(gt=0)
    output_stock_quantity: int = Field(gt=0)
    output_stock_unit: str = Field(min_length=1)
    output_unit: str | None
    location_id: int = Field(gt=0)
    layout_version: int | None
    location_name: str
    warehouse_floor: int | None
    reservation_id: int = Field(gt=0)
    consume_movement_id: int = Field(gt=0)
    assembly_consumed_quantity: int = Field(ge=0)
    remaining_stock_quantity: int = Field(ge=0)


class Input(Strict):
    job_id: int = Field(gt=0)
    product_id: int = Field(gt=0)
    output_lot_id: int = Field(gt=0)
    movement_id: int = Field(gt=0)
    movement_key: str
    quantity: int = Field(gt=0)
    unit: str = Field(min_length=1)


class Assembly(Strict):
    operation_key: str
    evidence_key: str
    sets: int = Field(gt=0)
    sets_unit: Literal['套']
    recipe: Recipe
    inputs: list[Input] = Field(min_length=1)
    output_lot_id: int = Field(gt=0)
    output_movement_id: int = Field(gt=0)
    output_stock_quantity: int = Field(gt=0)
    output_stock_unit: str = Field(min_length=1)
    output_unit: str | None
    location_id: int = Field(gt=0)
    layout_version: int | None
    location_name: str
    warehouse_floor: int | None


class Receipt(Strict):
    schema_version: Literal[1] = Field(alias='schema')
    operation_key: str
    group_key: str
    actor_id: int = Field(gt=0)
    parent_id: int = Field(gt=0)
    request: dict
    disposition: Literal['semi', 'finished']
    recipe: Recipe
    children: list[Child] = Field(min_length=1)
    assembly: Assembly | None
    trace_url: str


def normalized_payload(body):
    payload = body.model_dump()
    for row in payload['jobs']:
        if row.get('lot_id') is None:
            row.pop('lot_id', None)
    return payload


def trace_url(key):
    return '/api/production/stock-preparation/history/' + quote(key, safe='')


def current_customers(db, payload):
    """Ownership only: completed jobs remain readable after qualification changes."""
    parent = db.get(Product, payload['parent_id'])
    ids = [r['job_id'] for r in payload['jobs']]
    if not parent or not ids or len(ids) != len(set(ids)):
        fail('原组合产品或任务身份不完整，请保留原请求核对')
    customers = [parent.customer_id]
    for job_id in ids:
        job = db.get(Job, job_id)
        try:
            group = json.loads(job.product_snapshot)['preparation_group'] if job else {}
            if group.get('key') != payload['group_key'] or group['recipe']['parent_id'] != parent.id:
                raise ValueError()
            customer, _ = identity(db, job.receipt_item_id)
            customers.extend([customer, group['recipe']['customer_id']])
        except (ValueError, TypeError, KeyError):
            fail('原组任务归属不完整，请保留原请求核对')
    return customers


def frozen_recipe(value):
    return {**{k: value.get(k) for k in ('parent_id','customer_id','code','name','unit','version')},
            'children':[{k:c.get(k) for k in ('product_id','code','name','unit','per_set','version')} for c in value['children']]}


def movement(db, key):
    return db.scalar(select(InventoryMovement).where(InventoryMovement.idempotency_key == key))


def build_receipt(db, payload, actor, result):
    db.flush()
    children = []
    recipe = None
    jobs = {j: db.get(Job,j) for j in result['job_ids']}
    for row in payload['jobs']:
        job = jobs.get(row['job_id'])
        customer, source_id = identity(db, job.receipt_item_id) if job else (None,None)
        reservation = db.get(InventoryReservation,job.reservation_id) if job else None
        output = db.get(InventoryLot,job.output_lot_id) if job else None
        place = db.get(WarehouseLocation,output.warehouse_location_id) if output else None
        consume = db.scalar(select(InventoryMovement).where(InventoryMovement.reservation_id==job.reservation_id,
            InventoryMovement.movement_type=='consume')) if job else None
        entry = movement(db,f'prep-out:{job.id}') if job else None
        place_id = payload['location_id'] if payload['disposition']=='finished' else row['location_id']
        layout = payload['layout_version'] if payload['disposition']=='finished' else row['layout_version']
        if (not job or job.status!='completed' or not reservation or not output or not place or not consume or not entry
                or job.actual_output!=row['actual_output'] or reservation.status!='consumed'
                or reservation.inventory_lot_id!=source_id or reservation.consumed_stock_quantity!=job.input_quantity
                or consume.inventory_lot_id!=source_id or consume.quantity!=job.input_quantity
                or consume.operator_id!=actor.id or entry.operator_id!=actor.id or entry.movement_type!='manual_in'
                or entry.inventory_lot_id!=output.id or output.source_ref_type!='stock_preparation'
                or output.source_ref_id!=job.id or output.warehouse_location_id!=place_id):
            fail('本次组加工保存证明不完整，已回滚，请核对')
        snapshot=json.loads(job.product_snapshot)
        value=frozen_recipe(snapshot['preparation_group']['recipe'])
        if recipe is not None and encode(recipe)!=encode(value):
            fail('本次冻结配比不一致，已回滚，请核对')
        recipe=value
        children.append(dict(job_id=job.id,product_id=job.product_id,receipt_item_id=job.receipt_item_id,
            source_lot_id=source_id,source_customer_id=customer,requested_job_version=row['job_version'],
            requested_lot_version=row['lot_version'],input_quantity=job.input_quantity,input_unit='张',
            input_stock_unit=consume.unit,actual_output=job.actual_output,output_kind='semi',output_lot_id=output.id,
            output_movement_id=entry.id,output_stock_quantity=entry.quantity,output_stock_unit=entry.unit,
            output_unit=basis_unit_label(snapshot.get('physical_basis') or {}) or None,location_id=place.id,
            layout_version=layout,location_name=place.location_name,warehouse_floor=place.warehouse_floor,
            reservation_id=reservation.id,consume_movement_id=consume.id,assembly_consumed_quantity=0,
            remaining_stock_quantity=entry.quantity))
    assembly=None
    if payload['disposition']=='finished':
        data=result['assembly'];key=digest([payload['operation_key'],'assemble'])[:60]
        by_lot={c['output_lot_id']:c for c in children};inputs=[]
        for source in data['inputs']:
            child=by_lot.get(source['lot_id']);taken=movement(db,source['movement_key'])
            if (not child or not taken or taken.movement_type!='consume' or taken.inventory_lot_id!=source['lot_id']
                    or taken.quantity!=source['quantity'] or taken.operator_id!=actor.id
                    or child['product_id']!=source['product_id']):
                fail('本次组装投入证明不完整，已回滚，请核对')
            inputs.append(dict(job_id=child['job_id'],product_id=source['product_id'],output_lot_id=source['lot_id'],
                movement_id=taken.id,movement_key=source['movement_key'],quantity=taken.quantity,unit=taken.unit))
            child['assembly_consumed_quantity']+=taken.quantity
            child['remaining_stock_quantity']-=taken.quantity
        output=db.get(InventoryLot,data['output_lot_id']);entry=movement(db,'prep-kit:'+key)
        place=db.get(WarehouseLocation,output.warehouse_location_id) if output else None
        if (not output or not entry or not place or entry.movement_type!='manual_in' or entry.inventory_lot_id!=output.id
                or entry.operator_id!=actor.id or output.warehouse_location_id!=payload['location_id']
                or data['sets']!=payload['sets'] or encode(frozen_recipe(data['recipe']))!=encode(recipe)):
            fail('本次成套入库证明不完整，已回滚，请核对')
        basis=output.finished_detail.physical_basis_json if output.finished_detail else None
        if isinstance(basis,str):basis=json.loads(basis)
        assembly=dict(operation_key=key,evidence_key='assembly-inputs:'+digest(key)[:45],sets=data['sets'],
            sets_unit='套',recipe=recipe,inputs=inputs,output_lot_id=output.id,output_movement_id=entry.id,
            output_stock_quantity=entry.quantity,output_stock_unit=entry.unit,
            output_unit=basis_unit_label(basis or {}) or None,location_id=place.id,
            layout_version=payload['layout_version'],location_name=place.location_name,warehouse_floor=place.warehouse_floor)
    proof=Receipt(schema=1,operation_key=payload['operation_key'],group_key=payload['group_key'],actor_id=actor.id,
        parent_id=payload['parent_id'],request=payload,disposition=payload['disposition'],recipe=recipe,
        children=children,assembly=assembly,trace_url=trace_url(payload['group_key'])).model_dump(by_alias=True)
    frozen=dict(result,group_completion_receipt=proof)
    # Same verification for first write and all later reads; before parent INSERT.
    validate_proof(proof,payload,actor.id,result)
    encode(frozen)
    return frozen


def validate_proof(proof, payload, actor_id, result):
    parsed=Receipt.model_validate(proof).model_dump(by_alias=True)
    rows={r['job_id']:r for r in payload['jobs']}
    children={c['job_id']:c for c in parsed['children']}
    if (type(proof.get('schema')) is not int or parsed['operation_key']!=payload['operation_key']
            or parsed['group_key']!=payload['group_key'] or parsed['parent_id']!=payload['parent_id']
            or parsed['actor_id']!=actor_id or parsed['disposition']!=payload['disposition']
            or encode(parsed['request'])!=encode(payload) or parsed['recipe']['parent_id']!=payload['parent_id']
            or len(rows)!=len(payload['jobs']) or len(children)!=len(parsed['children'])
            or set(children)!=set(rows) or set(children)!=set(result['job_ids'])
            or parsed['trace_url']!=trace_url(payload['group_key'])):
        raise ValueError()
    products={c['product_id']:c for c in parsed['recipe']['children']}
    if len(products)!=len(parsed['recipe']['children']):raise ValueError()
    for job_id,child in children.items():
        row=rows[job_id];finished=payload['disposition']=='finished'
        if (child['product_id'] not in products or child['requested_job_version']!=row['job_version']
                or child['requested_lot_version']!=row['lot_version'] or child['actual_output']!=row['actual_output']
                or child['location_id']!=(payload['location_id'] if finished else row['location_id'])
                or child['layout_version']!=(payload['layout_version'] if finished else row['layout_version'])
                or child['remaining_stock_quantity']+child['assembly_consumed_quantity']!=child['output_stock_quantity']):
            raise ValueError()
    assembly=parsed['assembly']
    if payload['disposition']=='semi':
        if assembly is not None or 'assembly' in result or any(c['assembly_consumed_quantity'] for c in children.values()):raise ValueError()
    else:
        key=digest([payload['operation_key'],'assemble'])[:60]
        if (not assembly or assembly['operation_key']!=key or assembly['evidence_key']!='assembly-inputs:'+digest(key)[:45]
                or assembly['sets']!=payload['sets'] or encode(assembly['recipe'])!=encode(parsed['recipe'])
                or assembly['location_id']!=payload['location_id'] or assembly['layout_version']!=payload['layout_version']
                or result['assembly'].get('output_lot_id')!=assembly['output_lot_id']
                or result['assembly'].get('sets')!=assembly['sets']
                or encode(frozen_recipe(result['assembly']['recipe']))!=encode(parsed['recipe'])):raise ValueError()
        totals=defaultdict(int);taken=defaultdict(int);seen=set()
        for source in assembly['inputs']:
            child=children.get(source['job_id'])
            if (not child or source['job_id'] in seen or source['product_id']!=child['product_id']
                    or source['output_lot_id']!=child['output_lot_id'] or source['unit']!=child['output_stock_unit']
                    or source['movement_key']!='prep-assembly:'+digest([key,child['job_id']])[:50]):raise ValueError()
            seen.add(source['job_id']);totals[source['product_id']]+=source['quantity'];taken[source['job_id']]+=source['quantity']
        if dict(totals)!={p:payload['sets']*c['per_set'] for p,c in products.items()}:raise ValueError()
        if any(c['assembly_consumed_quantity']!=taken[j] for j,c in children.items()):raise ValueError()
        old_inputs=result['assembly']['inputs']
        if (len(old_inputs)!=len(assembly['inputs']) or
                {encode({k:s[k] for k in ('lot_id','product_id','quantity','movement_key')}) for s in old_inputs} !=
                {encode(dict(lot_id=s['output_lot_id'],product_id=s['product_id'],quantity=s['quantity'],movement_key=s['movement_key'])) for s in assembly['inputs']}):raise ValueError()
    return parsed


def checked_result(command, payload, actor_id):
    if command.actor_id!=actor_id or command.request_json!=encode(payload):
        fail('操作标识与原账号或原内容不一致，请保留原请求核对')
    try:
        result=json.loads(command.result_json)
        if (not isinstance(result,dict) or result.get('action')!='dispose' or result.get('group_key')!=payload['group_key']
                or result.get('disposition')!=payload['disposition'] or set(result)-{'action','group_key','disposition','job_ids','assembly','group_completion_receipt'}
                or not isinstance(result.get('job_ids'),list) or not result['job_ids']
                or any(type(j) is not int or j<=0 for j in result['job_ids'])
                or len(result['job_ids'])!=len(set(result['job_ids']))
                or set(result['job_ids'])!={r['job_id'] for r in payload['jobs']}):raise ValueError()
        if payload['disposition']=='finished':
            assembly=result.get('assembly')
            if (not isinstance(assembly,dict) or set(assembly)-{'action','group_key','sets','recipe','inputs','output_lot_id','placed_at_utc'}
                    or assembly.get('action')!='assemble' or assembly.get('group_key')!=payload['group_key']
                    or type(assembly.get('sets')) is not int or assembly['sets']!=payload['sets']
                    or type(assembly.get('output_lot_id')) is not int or assembly['output_lot_id']<=0
                    or not isinstance(assembly.get('recipe'),dict) or not isinstance(assembly.get('inputs'),list)
                    or not assembly['inputs']):raise ValueError()
            recipe=assembly['recipe']
            if (set(recipe)-{'parent_id','customer_id','code','name','unit','version','children','customer_name','inventory_mode'}
                    or Recipe.model_validate(frozen_recipe(recipe)).parent_id!=payload['parent_id']
                    or any(set(c)-{'product_id','code','name','unit','per_set','version','edge_id','available_output'} for c in recipe['children'])):raise ValueError()
            for source in assembly['inputs']:
                if (not isinstance(source,dict) or set(source)-{'lot_id','quantity','product_id','movement_key','version'}
                        or any(type(source.get(k)) is not int or source[k]<=0 for k in ('lot_id','quantity','product_id','version'))
                        or not isinstance(source.get('movement_key'),str)):raise ValueError()
        elif 'assembly' in result:raise ValueError()
        if 'group_completion_receipt' not in result:
            return result,None
        return result,validate_proof(result['group_completion_receipt'],payload,actor_id,result)
    except (ValueError, TypeError, KeyError, ValidationError):
        fail('原组加工保存证明损坏，请保留原请求并联系管理员核对')


def proof_customers(db, proof, command):
    if command.receipt_item_id not in {c['receipt_item_id'] for c in proof['children']}:
        fail('原组加工证明归属不一致，请保留原请求核对')
    customers=[proof['recipe']['customer_id']]
    for child in proof['children']:
        job=db.get(Job,child['job_id'])
        customer,source=identity(db,child['receipt_item_id'])
        if not job or job.receipt_item_id!=child['receipt_item_id'] or source!=child['source_lot_id']:
            fail('原组加工来源身份不一致，请保留原请求核对')
        customers.extend([customer,child['source_customer_id']])
    return customers


def envelope(result, proof, key, actor_id):
    plain={k:v for k,v in result.items() if k!='group_completion_receipt'} if result else None
    return dict(status='completed' if proof else 'legacy_trace' if result else 'not_recorded',operation_key=key,
        current_actor_id=actor_id,group_completion_receipt=proof,result=plain,
        trace_url=trace_url(plain['group_key']) if plain else None)
