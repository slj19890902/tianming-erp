"""Immutable proof of one standalone assembly, built before final INSERT."""
import json
from datetime import datetime
from collections import defaultdict
from typing import Literal
from pydantic import Field, ValidationError
from app.models.stock_preparation import StockPreparationJob as Job, StockPreparationCommand as Command
from app.models.warehouse_inventory import InventoryLot, InventoryMovement, WarehouseLocation
from app.services.stock_preparation import fail
from app.services.stock_preparation_completion_recovery import identity
from app.services.stock_preparation_groups import encode, digest
from app.services.stock_preparation_group_recovery import (
    Strict, Recipe, frozen_recipe, normalized_payload, current_customers, movement, trace_url,
)
from app.services.product_unit_labels import basis_unit_label


class Member(Strict):
    job_id: int = Field(gt=0)
    product_id: int = Field(gt=0)
    receipt_item_id: int = Field(gt=0)
    source_lot_id: int = Field(gt=0)
    source_customer_id: int | None = Field(gt=0)
    output_lot_id: int = Field(gt=0)
    requested_job_version: int = Field(gt=0)
    requested_output_version: int = Field(ge=0)
    location_id: int | None = Field(gt=0)
    location_name: str | None
    warehouse_floor: int | None
    stock_unit: str = Field(min_length=1)
    output_unit: str | None
    before_available: int = Field(ge=0)
    consumed_quantity: int = Field(ge=0)
    remaining_stock_quantity: int = Field(ge=0)
    balance_basis: Literal['consume_movement','locked_end_balance']


class Input(Strict):
    job_id: int = Field(gt=0)
    product_id: int = Field(gt=0)
    output_lot_id: int = Field(gt=0)
    movement_id: int = Field(gt=0)
    movement_key: str
    quantity: int = Field(gt=0)
    unit: str = Field(min_length=1)
    before_available: int = Field(ge=0)
    after_available: int = Field(ge=0)


class Receipt(Strict):
    schema_version: Literal[1] = Field(alias='schema')
    operation_key: str
    group_key: str
    actor_id: int = Field(gt=0)
    parent_id: int = Field(gt=0)
    request: dict
    recipe: Recipe
    sets: int = Field(gt=0)
    sets_unit: Literal['套']
    evidence_key: str
    placed_at_utc: str
    members: list[Member] = Field(min_length=1)
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
    trace_url: str


def build_receipt(db,payload,actor,result):
    """Only called for a new, top-level assembly under its original write lock."""
    key=payload['operation_key'];members=[];inputs=[]
    by_lot={row['lot_id']:row for row in result['inputs']}
    recipe=frozen_recipe(result['recipe'])
    for row in payload['jobs']:
        job=db.get(Job,row['job_id']);lot=db.get(InventoryLot,job.output_lot_id) if job else None
        if not job or not lot or lot.source_ref_type!='stock_preparation' or lot.source_ref_id!=job.id:
            fail('本次组套来源证明不完整，已回滚，请核对')
        snapshot=json.loads(job.product_snapshot);group=snapshot['preparation_group']
        if group['key']!=payload['group_key'] or encode(frozen_recipe(group['recipe']))!=encode(recipe):
            fail('本次组套冻结配比不一致，已回滚，请核对')
        customer,source=identity(db,job.receipt_item_id)
        current=by_lot.get(lot.id);taken=None
        if current:
            taken=movement(db,current['movement_key'])
            if (not taken or taken.movement_type!='consume' or taken.inventory_lot_id!=lot.id
                    or taken.operator_id!=actor.id or taken.quantity!=current['quantity']
                    or current['product_id']!=job.product_id or taken.unit!=lot.unit
                    or taken.after_available!=lot.quantity_available):
                fail('本次组套消耗证明不完整，已回滚，请核对')
            inputs.append(dict(job_id=job.id,product_id=job.product_id,output_lot_id=lot.id,movement_id=taken.id,
                movement_key=current['movement_key'],quantity=taken.quantity,unit=taken.unit,
                before_available=taken.before_available,after_available=taken.after_available))
        place=db.get(WarehouseLocation,lot.warehouse_location_id) if lot.warehouse_location_id else None
        members.append(dict(job_id=job.id,product_id=job.product_id,receipt_item_id=job.receipt_item_id,
            source_lot_id=source,source_customer_id=customer,output_lot_id=lot.id,
            requested_job_version=row['job_version'],requested_output_version=row['output_version'],
            location_id=lot.warehouse_location_id,location_name=place.location_name if place else None,
            warehouse_floor=place.warehouse_floor if place else None,stock_unit=lot.unit,
            output_unit=basis_unit_label(snapshot.get('physical_basis') or {}) or None,
            before_available=taken.before_available if taken else lot.quantity_available,
            consumed_quantity=taken.quantity if taken else 0,remaining_stock_quantity=lot.quantity_available,
            balance_basis='consume_movement' if taken else 'locked_end_balance'))
    output=db.get(InventoryLot,result['output_lot_id']);entry=movement(db,'prep-kit:'+key)
    place=db.get(WarehouseLocation,output.warehouse_location_id) if output else None
    evidence_key='assembly-inputs:'+digest(key)[:45];evidence=db.get(Command,evidence_key)
    if (not output or not entry or not place or not evidence or evidence.actor_id!=actor.id
            or entry.movement_type!='manual_in' or entry.operator_id!=actor.id
            or entry.inventory_lot_id!=output.id or entry.quantity!=payload['sets']
            or output.source_ref_type!='preparation_assembly' or output.source_ref_id!=output.id
            or output.warehouse_location_id!=payload['location_id']):
        fail('本次组套入库证明不完整，已回滚，请核对')
    basis=output.finished_detail.physical_basis_json if output.finished_detail else None
    if isinstance(basis,str):basis=json.loads(basis)
    proof=Receipt(schema=1,operation_key=key,group_key=payload['group_key'],actor_id=actor.id,
        parent_id=payload['parent_id'],request=payload,recipe=recipe,sets=payload['sets'],sets_unit='套',
        evidence_key=evidence_key,placed_at_utc=result['placed_at_utc'],members=members,inputs=inputs,
        output_lot_id=output.id,output_movement_id=entry.id,output_stock_quantity=entry.quantity,
        output_stock_unit=entry.unit,output_unit=basis_unit_label(basis or {}) or None,
        location_id=place.id,layout_version=payload['layout_version'],location_name=place.location_name,
        warehouse_floor=place.warehouse_floor,trace_url=trace_url(payload['group_key'])).model_dump(by_alias=True)
    validate_proof(proof,payload,actor.id,result)
    frozen=dict(result,assembly_completion_receipt=proof);encode(frozen)
    return frozen


def validate_proof(proof,payload,actor_id,result):
    parsed=Receipt.model_validate(proof).model_dump(by_alias=True)
    datetime.fromisoformat(parsed['placed_at_utc'])
    rows={r['job_id']:r for r in payload['jobs']};members={m['job_id']:m for m in parsed['members']}
    recipe=parsed['recipe'];products={c['product_id']:c for c in recipe['children']}
    if (type(proof.get('schema')) is not int or parsed['operation_key']!=payload['operation_key']
            or parsed['group_key']!=payload['group_key'] or parsed['actor_id']!=actor_id
            or parsed['parent_id']!=payload['parent_id'] or recipe['parent_id']!=payload['parent_id']
            or encode(parsed['request'])!=encode(payload) or parsed['sets']!=payload['sets']
            or parsed['output_stock_quantity']!=payload['sets'] or len(rows)!=len(payload['jobs'])
            or len(members)!=len(parsed['members']) or set(members)!=set(rows)
            or len({m['output_lot_id'] for m in members.values()})!=len(members)
            or len(products)!=len(recipe['children']) or parsed['evidence_key']!='assembly-inputs:'+digest(payload['operation_key'])[:45]
            or parsed['trace_url']!=trace_url(payload['group_key'])
            or parsed['location_id']!=payload['location_id'] or parsed['layout_version']!=payload['layout_version']
            or parsed['output_lot_id']!=result['output_lot_id'] or parsed['sets']!=result['sets']
            or parsed['placed_at_utc']!=result['placed_at_utc'] or encode(recipe)!=encode(frozen_recipe(result['recipe']))):raise ValueError()
    totals=defaultdict(int);seen=set()
    for source in parsed['inputs']:
        member=members.get(source['job_id'])
        if (not member or source['job_id'] in seen or source['product_id']!=member['product_id']
                or source['output_lot_id']!=member['output_lot_id'] or source['unit']!=member['stock_unit']
                or source['movement_key']!='prep-assembly:'+digest([payload['operation_key'],source['job_id']])[:50]
                or source['before_available']-source['quantity']!=source['after_available']
                or source['quantity']!=member['consumed_quantity'] or source['before_available']!=member['before_available']
                or source['after_available']!=member['remaining_stock_quantity']):raise ValueError()
        seen.add(source['job_id']);totals[source['product_id']]+=source['quantity']
    for job_id,member in members.items():
        row=rows[job_id]
        if (member['product_id'] not in products or member['requested_job_version']!=row['job_version']
                or member['requested_output_version']!=row['output_version']
                or member['before_available']-member['consumed_quantity']!=member['remaining_stock_quantity']
                or member['balance_basis']!=('consume_movement' if job_id in seen else 'locked_end_balance')
                or (job_id not in seen and member['consumed_quantity']!=0)):raise ValueError()
    if dict(totals)!={p:payload['sets']*c['per_set'] for p,c in products.items()}:raise ValueError()
    old=result['inputs']
    by_lot={m['output_lot_id']:m for m in members.values()}
    if any(s['lot_id'] not in by_lot or s['version']!=by_lot[s['lot_id']]['requested_output_version']+1 for s in old):raise ValueError()
    if (len(old)!=len(parsed['inputs']) or
            {encode({k:s[k] for k in ('lot_id','product_id','quantity','movement_key')}) for s in old} !=
            {encode(dict(lot_id=s['output_lot_id'],product_id=s['product_id'],quantity=s['quantity'],movement_key=s['movement_key'])) for s in parsed['inputs']}):raise ValueError()
    return parsed


def checked_result(command,payload,actor_id):
    if command.actor_id!=actor_id or command.request_json!=encode(payload):
        fail('操作标识与原账号或原内容不一致，请保留原请求核对')
    try:
        result=json.loads(command.result_json)
        if (not isinstance(result,dict) or set(result)-{'action','group_key','sets','recipe','inputs','output_lot_id','placed_at_utc','assembly_completion_receipt'}
                or result.get('action')!='assemble' or result.get('group_key')!=payload['group_key']
                or type(result.get('sets')) is not int or result['sets']!=payload['sets']
                or type(result.get('output_lot_id')) is not int or result['output_lot_id']<=0
                or not isinstance(result.get('placed_at_utc'),str) or not result['placed_at_utc']
                or not isinstance(result.get('inputs'),list) or not result['inputs']):raise ValueError()
        recipe=result['recipe'];Recipe.model_validate(frozen_recipe(recipe))
        if (recipe['parent_id']!=payload['parent_id']
                or set(recipe)-{'parent_id','customer_id','code','name','unit','version','children','customer_name','inventory_mode'}
                or any(set(c)-{'product_id','code','name','unit','per_set','version','edge_id','available_output'} for c in recipe['children'])):raise ValueError()
        for source in result['inputs']:
            if (not isinstance(source,dict) or set(source)!={'lot_id','quantity','product_id','movement_key','version'}
                    or any(type(source[k]) is not int or source[k]<=0 for k in ('lot_id','quantity','product_id','version'))
                    or not isinstance(source['movement_key'],str)):raise ValueError()
        if 'assembly_completion_receipt' not in result:return result,None
        return result,validate_proof(result['assembly_completion_receipt'],payload,actor_id,result)
    except (ValueError,TypeError,KeyError,ValidationError):
        fail('原组套保存证明损坏，请保留原请求并联系管理员核对')


def proof_customers(db,proof,command):
    if command.receipt_item_id not in {m['receipt_item_id'] for m in proof['members']}:
        fail('原组套证明归属不一致，请保留原请求核对')
    customers=[proof['recipe']['customer_id']]
    entry=db.get(InventoryMovement,proof['output_movement_id'])
    if (not entry or entry.movement_type!='manual_in' or entry.inventory_lot_id!=proof['output_lot_id']
            or entry.operator_id!=command.actor_id or entry.idempotency_key!='prep-kit:'+proof['operation_key']
            or entry.quantity!=proof['output_stock_quantity'] or entry.unit!=proof['output_stock_unit']):
        fail('原组套入库流水证明不一致，请保留原请求核对')
    evidence=db.get(Command,proof['evidence_key'])
    if (not evidence or evidence.actor_id!=command.actor_id or evidence.receipt_item_id!=command.receipt_item_id
            or evidence.request_json!=encode(dict(action='assemble_inputs',operation_key=proof['operation_key']))):
        fail('原组套投入证据不一致，请保留原请求核对')
    for source in proof['inputs']:
        taken=db.get(InventoryMovement,source['movement_id'])
        if (not taken or taken.movement_type!='consume' or taken.operator_id!=command.actor_id
                or taken.inventory_lot_id!=source['output_lot_id'] or taken.quantity!=source['quantity']
                or taken.unit!=source['unit'] or taken.idempotency_key!=source['movement_key']
                or taken.before_available!=source['before_available'] or taken.after_available!=source['after_available']):
            fail('原组套消耗流水证明不一致，请保留原请求核对')
    for member in proof['members']:
        job=db.get(Job,member['job_id']);customer,source=identity(db,member['receipt_item_id'])
        if (not job or job.receipt_item_id!=member['receipt_item_id'] or job.product_id!=member['product_id']
                or source!=member['source_lot_id'] or job.output_lot_id!=member['output_lot_id']):
            fail('原组套来源身份不一致，请保留原请求核对')
        customers.extend([customer,member['source_customer_id']])
    return customers


def envelope(result,proof,key,actor_id):
    plain={k:v for k,v in result.items() if k!='assembly_completion_receipt'} if result else None
    return dict(status='completed' if proof else 'legacy_trace' if result else 'not_recorded',operation_key=key,
        current_actor_id=actor_id,assembly_completion_receipt=proof,result=plain,
        trace_url=trace_url(plain['group_key']) if plain else None)
