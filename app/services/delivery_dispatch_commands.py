"""One externally confirmed dispatch, with durable original results.

Inventory selection and consumption remain in the existing delivery services.
This module owns request identity, snapshots and the parent transaction only.
"""
from __future__ import annotations

import hashlib
import json
import logging
from types import SimpleNamespace
from fractions import Fraction

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from app.api.deps import require_customer_access
from app.core.time_contract import utc_naive_to_api, utc_now_naive
from app.models.delivery import Delivery, DeliveryItem
from app.models.finance import FinanceIdempotencyRecord
from app.models.order import Order, OrderItem
from app.models.product_bom import BomComponentDirectDeliveryAllocation
from app.models.warehouse_inventory import (
    DeliveryInventoryAllocation, FinishedGoodsInventoryDetail, InventoryLot,
    InventoryMovement, InventoryReservation, UnorderedFinishedDeliveryAllocation,
)
from app.models.bom_subkit import SubkitDeliveryAllocation, OrderSubkit
from app.models.production import ProductionCompletion, ProductionTask
from app.models.warehouse_inventory import OrderItemSemiRequirement
from app.services.delivery_quantities import for_item, item_physical_quantity, customer_for

ACTION = 'delivery_dispatch_command'
PRESERVE = 'X-Delivery-Dispatch-Preserve'
REJECTED = 'X-Delivery-Dispatch-Rejected'


def headers(**extra):
    return {'Cache-Control': 'no-store', PRESERVE: '1', **extra}


class DispatchRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()
        async def handle(request):
            try:
                result = await original(request)
                result.headers['Cache-Control'] = 'no-store'
                return result
            except HTTPException as error:
                error.headers = headers(**(error.headers or {}))
                if error.headers.get(REJECTED) == '1':
                    error.headers.pop(PRESERVE, None)
                raise
            except RequestValidationError as error:
                # Standard 422 details remain readable, without a claim about
                # the fate of an earlier request carrying the same key.
                from starlette.responses import JSONResponse
                return JSONResponse(status_code=422, content={'detail': jsonable_encoder(error.errors())}, headers=headers())
            except Exception:
                logging.getLogger(__name__).exception('delivery command result unavailable')
                from starlette.responses import JSONResponse
                return JSONResponse(status_code=500, content={'detail': '发货结果暂无法确认，请保留原请求并核对原结果。'}, headers=headers())
        return handle


class DispatchCommand(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_actor_id: int = Field(strict=True, gt=0)
    idempotency_key: str = Field(min_length=8, max_length=120)
    expected_version: int = Field(strict=True, gt=0)
    snapshot_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    snapshot: dict
    confirm_pick_exception: bool = Field(strict=True)

    @field_validator('idempotency_key')
    @classmethod
    def key(cls, value):
        value = value.strip()
        if len(value) < 8:
            raise ValueError('请求标识不能为空，请刷新后重新核对')
        return value


class ResolveCommand(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_actor_id: int = Field(strict=True, gt=0)
    original_request: DispatchCommand


def canonical(value):
    return json.dumps(jsonable_encoder(value), ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


def request_value(payload):
    return payload.model_dump(mode='json', exclude={'idempotency_key', 'expected_actor_id'})


def request_hash(delivery_id, payload):
    return digest({'action': ACTION, 'delivery_id': delivery_id, 'payload': request_value(payload)})


def actor_guard(user, expected):
    if user.id != expected:
        raise HTTPException(409, '当前账号已变化，请保留原请求并切回原账号核对。', headers=headers(**{'X-Delivery-Dispatch-Actor-Mismatch': '1'}))


def require_activation(db):
    try:
        from desktop_assistant.delivery_dispatch_contract import require_managed_dispatch_activation
        require_managed_dispatch_activation(db.get_bind().url.database)
    except (ValueError, ImportError) as error:
        raise HTTPException(503, '确认发货的恢复保护尚未激活，请联系管理员升级助手后再操作。', headers=headers()) from error


def consistent_read(db):
    # Python sqlite legacy transaction mode does not BEGIN for SELECT. Pin
    # one database snapshot for all delivery/pick reads without taking a write
    # claim or performing business flush/commit.
    if db.get_bind().dialect.name == 'sqlite':
        connection = db.connection()
        driver = connection.connection.driver_connection
        if not driver.in_transaction:
            connection.exec_driver_sql('BEGIN')


def _rows(db, delivery_id, *, refresh=False):
    query = select(DeliveryItem).where(DeliveryItem.delivery_id == delivery_id, DeliveryItem.is_current.is_(True)).order_by(DeliveryItem.id)
    if refresh:
        query = query.execution_options(populate_existing=True)
    return list(db.scalars(query))


def _line(db, row, api, *, customer_quantity=None, dispatched=False):
    contract = for_item(row)
    quantity = int(row.delivered_quantity if customer_quantity is None else customer_quantity)
    physical = item_physical_quantity(row, quantity)
    order_item = db.get(OrderItem, row.order_item_id) if row.order_item_id else None
    order = db.get(Order, order_item.order_id) if order_item else None
    customer_id = order.customer_id if order else db.get(Delivery, row.delivery_id).customer_id
    source_product = order_item.product_id if order_item else row.product_id
    unit = contract.get('customer_unit') if contract else row.unit_snapshot
    physical_unit = contract.get('physical_unit') if contract else row.unit_snapshot
    goods = []
    if order_item:
        from app.services.legacy_accompany import contract as accompany_contract
        components = api._delivery_component_lines(db, order_item=order_item, planned_delivery_quantity=quantity,
            delivery_item_id=row.id, dispatched=dispatched)
        if not api._uses_composite_inventory(db,order_item.id) and accompany_contract(db,order_item.id) is None:
            components=[]
        goods = [dict(component_snapshot_id=c['component_snapshot_id'], source_product_id=c['component_product_id'],
            customer_id=customer_id, physical_quantity=int(c['planned_delivery_quantity']), physical_unit=c.get('unit'),
            quantity_per_set=c['quantity_per_set'], bom_delivery_mode=c.get('bom_delivery_mode')) for c in components]
    if not goods or (order_item and not api._uses_composite_inventory(db,order_item.id)):
        goods.insert(0,dict(component_snapshot_id=None, source_product_id=source_product, customer_id=customer_id,
            physical_quantity=physical, physical_unit=physical_unit, quantity_per_set=None, bom_delivery_mode=None))
    semi_requirements=[]
    direct_sources=[]
    if order_item:
        kit=db.get(OrderSubkit,order_item.id)
        if kit:
            goods.append(dict(component_snapshot_id=None,source_product_id=kit.kit_product_id,customer_id=customer_id,
                physical_quantity=quantity*kit.kits_per_parent,physical_unit=None,quantity_per_set=kit.kits_per_parent,bom_delivery_mode=None))
        semi_requirements=[dict(requirement_id=r.id,pieces_per_box=int(r.pieces_per_box or 1)) for r in db.scalars(select(OrderItemSemiRequirement)
            .where(OrderItemSemiRequirement.order_item_id==order_item.id).order_by(OrderItemSemiRequirement.id))]
        direct_sources=[dict(completion_id=r.id,direct_quantity=int(r.direct_delivery_quantity)) for r in db.scalars(select(ProductionCompletion)
            .join(ProductionTask,ProductionTask.id==ProductionCompletion.task_id).where(ProductionTask.sales_order_item_bom_component_id.is_(None),ProductionCompletion.order_item_id==order_item.id,ProductionCompletion.status=='posted',
                ProductionCompletion.inventory_lot_id.is_(None),ProductionCompletion.initial_disposition=='direct').order_by(ProductionCompletion.id))]
    allocations = []
    if row.source_type == 'unordered_finished':
        allocations = [dict(allocation_id=a.id, inventory_lot_id=a.inventory_lot_id,
            planned_physical_quantity=int(a.planned_quantity), status=a.status) for a in db.scalars(select(UnorderedFinishedDeliveryAllocation)
                .where(UnorderedFinishedDeliveryAllocation.delivery_item_id == row.id).order_by(UnorderedFinishedDeliveryAllocation.id))]
    return dict(delivery_item_id=row.id, revision_number=int(row.revision_number or 1), source_type=row.source_type,
        order_item_id=row.order_item_id, product_id=row.product_id, source_product_id=source_product, customer_id=customer_id,
        customer_quantity=quantity, physical_quantity=physical, customer_unit=unit, physical_unit=physical_unit,
        quantity_contract=contract, goods=jsonable_encoder(goods), semi_requirements=semi_requirements,direct_completion_sources=direct_sources,
        order_delivered_quantity=int(order_item.delivered_quantity or 0) if order_item else None, allocation_mode='explicit_unordered' if row.source_type=='unordered_finished' else 'on_dispatch', allocations=allocations)


def snapshot(db, delivery_id, user, api, *, pick_action='none', refresh=False):
    delivery = api._delivery_for_user(db, delivery_id, user)
    if delivery.status != 'pending':
        raise HTTPException(409, '送货单已不在待发货状态，请查看原单和原结果。')
    rows = _rows(db, delivery_id, refresh=refresh)
    if not rows:
        raise HTTPException(409, '送货单没有有效明细，不能确认发货。')
    items = [_line(db, row, api) for row in rows]
    for item in items:
        require_customer_access(item['customer_id'], user, db)
    task = api._delivery_pick_task(db, delivery_id)
    pick = None
    picked = {}
    if task:
        if refresh:
            db.refresh(task)
            db.expire(task, ['items'])
        # The old response helper can fill route_snapshot_* while reading.
        # Project through a detached value object so preview never dirties a
        # mapped task or lets a later command flush a read-generated route.
        projected_task = SimpleNamespace(**{column.key:getattr(task,column.key) for column in task.__table__.columns},
            items=list(task.items), customer=task.customer, delivery=task.delivery)
        value = api._pick_task_response(db, projected_task)
        if task.customer_id != delivery.customer_id:
            raise HTTPException(409, '拿货任务与送货客户不一致，请保留原请求核对。')
        pick = dict(id=task.id, delivery_id=task.delivery_id, snapshot_version=task.snapshot_version,
            route_snapshot_version=value.get('route_snapshot_version'), print_version=value.get('print_version'), status=task.status,
            items=[dict(task_item_id=r['id'], delivery_item_id=r['delivery_item_id'], order_item_id=r.get('order_item_id'),
                original_physical_quantity=r['original_quantity'], picked_physical_quantity=r['picked_quantity'],
                pick_status=r['pick_status'], location_plan_complete=r['location_plan_complete'], location_lines=r['location_lines']) for r in value['items']])
        if pick_action == 'apply':
            if task.status not in {'driver_confirmed', 'exception'}:
                raise HTTPException(409, '请先由司机提交拿货结果，不能应用未确认或已应用的任务。')
            picked = {r['delivery_item_id']: int(r['picked_quantity']) for r in value['items']}
            if len(picked) != len(rows) or set(picked) != {r.id for r in rows}:
                raise HTTPException(409, '拿货任务与送货明细已变化，请重新核对。')
    elif pick_action == 'apply':
        raise HTTPException(409, '拿货任务不存在，请重新核对。')
    final = []
    for row, item in zip(rows, items):
        physical = picked.get(row.id, item['physical_quantity'])
        quantity = customer_for(item['quantity_contract'], physical) if item['quantity_contract'] else physical
        final.append(dict(delivery_item_id=row.id, customer_quantity=int(quantity), physical_quantity=int(physical),
            customer_unit=item['customer_unit'], physical_unit=item['physical_unit'],goods=_line(db,row,api,customer_quantity=int(quantity))['goods']))
    frozen = dict(schema_version=1, delivery=dict(id=delivery.id, customer_id=delivery.customer_id, status=delivery.status,
        version=int(delivery.version or 1), source_mode=delivery.source_mode, delivery_date=delivery.delivery_date.isoformat(), vehicle_number=delivery.vehicle_number),
        items=items, pick_task=jsonable_encoder(pick), pick_action=pick_action, expected_final_items=final)
    # Display is deliberately separate from the business snapshot. No future
    # FIFO allocation is promised by an order's current location hints.
    display_items=[]
    for row, original, expected in zip(rows, items, final):
        display_items.append(dict(delivery_item_id=row.id, product_code=row.product_code_snapshot,
            product_name=row.product_name_snapshot, original_customer_quantity=original['customer_quantity'],
            original_physical_quantity=original['physical_quantity'], final_customer_quantity=expected['customer_quantity'],
            final_physical_quantity=expected['physical_quantity'], customer_unit=original['customer_unit'],physical_unit=original['physical_unit'],
            allocation_mode=original['allocation_mode']))
    return dict(schema_version=1,current_actor_id=user.id,snapshot=frozen,snapshot_hash=digest(frozen),display=dict(
        delivery_number=delivery.delivery_number,customer_name=delivery.customer_name_snapshot,requires_pick_apply=bool(task and task.status=='exception'),
        pick_status=task.status if task else None,items=display_items))


def _record(db, key):
    return db.scalar(select(FinanceIdempotencyRecord).where(FinanceIdempotencyRecord.idempotency_key==key))


def consumption_proven(receipt, payload):
    """Compare original confirmed needs with frozen transaction evidence.

    Physical stock quantities and integer requirement numerators are separate;
    no FIFO selection, stock writes, or current-master reconstruction occurs.
    """
    try:
        expected={r['delivery_item_id']:r for r in payload.snapshot['expected_final_items'] if r['physical_quantity']>0}
        originals={r['delivery_item_id']:r for r in payload.snapshot['items']}
        final_rows={r['delivery_item_id']:r for r in receipt['final_items']}
        def physical_factor(key):
            row=final_rows[key[0]]
            # Ordinary order reservation credit is in customer units. Use
            # only the two quantities confirmed by the original snapshot;
            # components, subkits and unordered stock already use their own
            # frozen demand units and must not inherit this parent ratio.
            if row['source_type']=='order' and key[1] is None and key[2]==row['source_product_id']:
                return Fraction(row['physical_quantity'],row['customer_quantity'])
            return Fraction(1)
        required={};actual={};semi={}
        for row in receipt['final_items']:
            confirmed=expected[row['delivery_item_id']]['goods']
            if len(row['goods'])!=len(confirmed):return False
            for got,wanted in zip(row['goods'],confirmed):
                if any(got[name]!=wanted[name] for name in ('component_snapshot_id','source_product_id','customer_id','physical_quantity','quantity_per_set','bom_delivery_mode')):return False
                if wanted['physical_unit'] is not None and got['physical_unit']!=wanted['physical_unit']:return False
                key=(row['delivery_item_id'],got['component_snapshot_id'],got['source_product_id'])
                if key in required or type(got['physical_quantity']) is not int or got['physical_quantity']<0:return False
                required[key]=Fraction(got['physical_quantity'])
        for movement in receipt['movements']:
            if any(type(movement[name]) is not int or movement[name]<=0 for name in ('requirement_quantity','requirement_denominator','physical_quantity')):return False
            key=(movement['delivery_item_id'],movement['component_snapshot_id'],movement['requirement_product_id'])
            if key not in required:return False
            credit=Fraction(movement['requirement_quantity'],movement['requirement_denominator'])
            if movement['source_kind']=='semi':
                semi_key=(key,movement['semi_requirement_id'])
                semi[semi_key]=semi.get(semi_key,Fraction(0))+credit
            elif movement['source_kind'] in {'inventory','subkit'}:
                actual[key]=actual.get(key,Fraction(0))+credit*physical_factor(key)
            else:return False
        for key in {row[0] for row in semi}:
            definitions=originals[key[0]]['semi_requirements']
            if not definitions:return False
            # Each material component contributes the same parent coverage;
            # summing lid/body pieces would count one parent twice.
            coverage=min(semi.get((key,d['requirement_id']),Fraction(0))/d['pieces_per_box'] for d in definitions)
            actual[key]=actual.get(key,Fraction(0))+coverage*physical_factor(key)
        allocation_ids=set()
        for allocation in receipt['direct_component_allocations']:
            if allocation['allocation_id'] in allocation_ids:return False
            allocation_ids.add(allocation['allocation_id'])
            matches=[key for key in required if key[0]==allocation['delivery_item_id'] and key[1]==allocation['component_snapshot_id']]
            if len(matches)!=1:return False
            key=matches[0];actual[key]=actual.get(key,Fraction(0))+allocation['physical_quantity']
        covered_items=set()
        for direct in receipt['direct_completion_coverage']:
            if direct['delivery_item_id'] in covered_items:return False
            covered_items.add(direct['delivery_item_id'])
            sources=direct['completion_sources']
            if len({r['completion_id'] for r in sources})!=len(sources):return False
            original=originals[direct['delivery_item_id']]
            if (direct['completion_sources']!=original['direct_completion_sources'] or direct['delivered_before']!=original['order_delivered_quantity']
                    or direct['order_item_id']!=original['order_item_id'] or direct['source_product_id']!=original['source_product_id']
                    or type(direct['credited_quantity']) is not int or direct['credited_quantity']<=0
                    or direct['available_before']!=max(sum(r['direct_quantity'] for r in direct['completion_sources'])-direct['delivered_before'],0)
                    or direct['credited_quantity']>direct['available_before']):return False
            key=(direct['delivery_item_id'],None,direct['source_product_id'])
            if key not in required:return False
            actual[key]=actual.get(key,Fraction(0))+direct['credited_quantity']*physical_factor(key)
        return all(actual.get(key,Fraction(0))==amount for key,amount in required.items()) and not set(actual)-set(required)
    except (KeyError,TypeError,ValueError,ZeroDivisionError):
        return False


def _matched_record(db, delivery_id, payload, user, api):
    record = _record(db,payload.idempotency_key)
    if not record:
        return None,None
    if (record.action != ACTION or record.actor_user_id != user.id or record.resource_type != 'delivery'
            or record.resource_id != delivery_id or record.request_hash != request_hash(delivery_id,payload)):
        raise HTTPException(409,'原请求标识、账号或内容不一致，请保留原请求并核对。')
    delivery=api._delivery_for_user(db,delivery_id,user)
    for row in _rows(db, delivery_id):
        item = db.get(OrderItem, row.order_item_id) if row.order_item_id else None
        owner = db.get(Order, item.order_id) if item else None
        require_customer_access(owner.customer_id if owner else delivery.customer_id, user, db)
    require_customer_access(payload.snapshot.get('delivery',{}).get('customer_id'),user,db)
    for original in payload.snapshot.get('items',[]):
        require_customer_access(original.get('customer_id'),user,db)
    try:
        value=json.loads(record.response_json)
        if value.get('result') == 'closed':
            closure = value['closure_receipt']
            if (value['schema_version'] != 1 or value['current_actor_id'] != user.id
                    or value['dispatch_receipt'] is not None or value['current'] is not None
                    or closure['schema_version'] != 1 or closure['actor_id'] != user.id
                    or closure['delivery_id'] != delivery_id or closure['idempotency_key'] != payload.idempotency_key
                    or closure['request_hash'] != record.request_hash or not closure['closed_at']
                    or closure['customer_id'] != payload.snapshot['delivery']['customer_id']
                    or canonical(closure['request']) != canonical(request_value(payload))):
                return None, record
            require_customer_access(closure['customer_id'], user, db)
            return value, record
        receipt=value['dispatch_receipt']
        if (value['schema_version']!=1 or value['result']!='completed' or receipt['schema_version']!=1
                or receipt['actor_id']!=user.id or receipt['delivery_id']!=delivery_id
                or receipt['idempotency_key']!=payload.idempotency_key or receipt['request_hash']!=record.request_hash
                or canonical(receipt['request'])!=canonical(request_value(payload))
                or receipt['customer_id']!=payload.snapshot['delivery']['customer_id']
                or not isinstance(receipt['final_items'],list) or not receipt['final_items']
                or not isinstance(receipt['movements'],list) or not receipt['dispatched_at']
                or receipt['completed_version']<=receipt['original_version']):
            return None,record
        expected = {r['delivery_item_id']:r for r in payload.snapshot['expected_final_items']}
        final = receipt['final_items']
        expected_ids = {key for key, row in expected.items() if row['physical_quantity'] > 0}
        if (receipt['original_version'] != payload.expected_version
                or receipt['completed_version'] != payload.expected_version + 1 + int(payload.snapshot['pick_action'] == 'apply')
                or canonical(receipt['original_items']) != canonical(payload.snapshot['items'])
                or len(final) != len(expected_ids) or {r['delivery_item_id'] for r in final} != expected_ids
                or sorted(receipt['removed_delivery_item_ids']) != sorted(set(expected)-expected_ids)):
            return None, record
        originals = {r['delivery_item_id']:r for r in payload.snapshot['items']}
        for row in final:
            before = originals[row['delivery_item_id']]
            wanted = expected[row['delivery_item_id']]
            if any(row[name] != before[name] for name in ('revision_number','source_type','order_item_id','product_id','source_product_id','customer_id','allocation_mode')):
                return None, record
            if any(before[name] is not None and row[name] != before[name] for name in ('customer_unit','physical_unit')):
                return None, record
            if any(row[name] != wanted[name] for name in ('customer_quantity','physical_quantity')):
                return None, record
            if not isinstance(row['goods'],list) or not row['goods']:
                return None, record
        ids=set()
        for movement in receipt['movements']:
            if (type(movement['movement_id']) is not int or movement['movement_id'] <= 0 or movement['movement_id'] in ids
                    or movement['delivery_item_id'] not in expected_ids or movement['operator_id'] != user.id
                    or type(movement['physical_quantity']) is not int or movement['physical_quantity'] <= 0
                    or type(movement['inventory_lot_id']) is not int or movement['inventory_lot_id'] <= 0
                    or not movement['occurred_at']):
                return None, record
            ids.add(movement['movement_id'])
        if not consumption_proven(receipt, payload):
            return None, record
        for allocation in receipt['direct_component_allocations']:
            if allocation['delivery_item_id'] not in expected_ids or any(type(allocation[name]) is not int or allocation[name] <= 0 for name in ('allocation_id','production_completion_id','component_snapshot_id','physical_quantity')):
                return None, record
        require_customer_access(receipt['customer_id'],user,db)
    except (KeyError,TypeError,ValueError):
        return None,record
    return value,record


def current(db,delivery_id,api,user):
    row=api._delivery_for_user(db,delivery_id,user)
    return dict(delivery_id=row.id,customer_id=row.customer_id,delivery_number=row.delivery_number,status=row.status,
        version=int(row.version or 1),total_quantity=row.total_quantity,dispatched_at=utc_naive_to_api(row.dispatched_at) if row.dispatched_at else None)


def _trace(row):
    return dict(delivery_id=row.id,delivery_number=row.delivery_number,url=f'/api/deliveries/{row.id}')


def resolve(db,delivery_id,key,payload,user,api):
    actor_guard(user,payload.expected_actor_id);actor_guard(user,payload.original_request.expected_actor_id)
    original=payload.original_request
    if (key != original.idempotency_key or original.snapshot.get('delivery',{}).get('id')!=delivery_id
            or original.snapshot.get('delivery',{}).get('version')!=original.expected_version or digest(original.snapshot)!=original.snapshot_hash):
        raise HTTPException(409,'原请求标识或送货单不一致，请保留原内容核对。')
    require_activation(db)
    with db.no_autoflush:
        consistent_read(db)
        row=api._delivery_for_user(db,delivery_id,user)
        value,record=_matched_record(db,delivery_id,original,user,api)
        return dict(status=value['result'] if value else 'trace' if record else 'not_recorded',current_actor_id=user.id,
            idempotency_key=key,dispatch_receipt=value['dispatch_receipt'] if value else None,
            closure_receipt=value.get('closure_receipt') if value else None,
            current=current(db,delivery_id,api,user),trace=_trace(row) if record and not value else None)


def commit_command(db):
    db.commit()


def build_receipt(db,delivery_id,payload,user,api,first_movement_id):
    db.flush()
    delivery=api._delivery_for_user(db,delivery_id,user)
    final=[_line(db,r,api,dispatched=True) for r in _rows(db,delivery_id)]
    expected={r['delivery_item_id']:r for r in payload.snapshot['expected_final_items']}
    if {r['delivery_item_id'] for r in final}!={key for key,row in expected.items() if row['physical_quantity']>0}:
        raise RuntimeError('Dispatch final line identity mismatch')
    for row in final:
        wanted=expected[row['delivery_item_id']]
        if row['physical_quantity']!=wanted['physical_quantity'] or row['customer_quantity']!=wanted['customer_quantity']:
            raise RuntimeError('Dispatch final quantity mismatch')
    movements=list(db.scalars(select(InventoryMovement).where(InventoryMovement.id>first_movement_id,
        InventoryMovement.related_delivery_id==delivery_id,InventoryMovement.movement_type=='consume').order_by(InventoryMovement.id)))
    movement_ids=[r.id for r in movements]
    bindings={}
    for model in (DeliveryInventoryAllocation,UnorderedFinishedDeliveryAllocation,SubkitDeliveryAllocation):
        for allocation in db.scalars(select(model).where(model.consume_movement_id.in_(movement_ids))):
            bindings[allocation.consume_movement_id]=allocation
    facts=[]
    for movement in movements:
        lot=db.get(InventoryLot,movement.inventory_lot_id)
        detail=db.scalar(select(FinishedGoodsInventoryDetail).where(FinishedGoodsInventoryDetail.inventory_lot_id==lot.id))
        reservation=db.get(InventoryReservation,movement.reservation_id) if movement.reservation_id else None
        if movement.operator_id!=user.id or movement.quantity<=0 or movement.id not in bindings:
            raise RuntimeError('Dispatch movement identity missing')
        allocation=bindings[movement.id]
        line=next(r for r in final if r['delivery_item_id']==allocation.delivery_item_id)
        component=getattr(reservation,'sales_order_item_bom_component_id',None)
        semi_id=getattr(reservation,'semi_requirement_id',None)
        if isinstance(allocation,SubkitDeliveryAllocation):
            kit=db.get(OrderSubkit,line['order_item_id']);requirement_product=kit.kit_product_id;kind='subkit'
        else:
            matching=[g for g in line['goods'] if g['component_snapshot_id']==component]
            if component is None:
                root=[g for g in line['goods'] if g['source_product_id']==line['source_product_id']]
                matching=root or matching
                if len(matching)==1:component=matching[0]['component_snapshot_id']
            if len(matching)!=1:raise RuntimeError('Dispatch requirement identity missing')
            requirement_product=matching[0]['source_product_id'];kind='semi' if semi_id else 'inventory'
        credit=getattr(allocation,'credited_requirement_quantity',movement.quantity)
        denominator=int(reservation.requirement_quantity_denominator or 1) if reservation else 1
        facts.append(dict(movement_id=movement.id,inventory_lot_id=lot.id,source_product_id=detail.product_id if detail else None,
            owner_customer_id=detail.owner_customer_id if detail else None,delivery_item_id=allocation.delivery_item_id,
            component_snapshot_id=component,requirement_product_id=requirement_product,requirement_quantity=credit,requirement_denominator=denominator,
            semi_requirement_id=semi_id,source_kind=kind,physical_quantity=movement.quantity,
            unit=movement.unit,location_id=lot.warehouse_location_id,operator_id=movement.operator_id,occurred_at=utc_naive_to_api(movement.created_at)))
    direct=[dict(allocation_id=a.id,delivery_item_id=a.delivery_item_id,production_completion_id=a.production_completion_id,
        component_snapshot_id=a.sales_order_item_bom_component_id,physical_quantity=a.consumed_quantity) for a in db.scalars(select(BomComponentDirectDeliveryAllocation)
        .where(BomComponentDirectDeliveryAllocation.delivery_item_id.in_([r['delivery_item_id'] for r in final]),BomComponentDirectDeliveryAllocation.status=='active'))]
    coverage=[]
    originals={r['delivery_item_id']:r for r in payload.snapshot['items']}
    for row in final:
        before=originals[row['delivery_item_id']]
        if before['direct_completion_sources'] and any(g['component_snapshot_id'] is None and g['source_product_id']==row['source_product_id'] for g in row['goods']):
            parent_credit=sum((Fraction(r['requirement_quantity'],r['requirement_denominator']) for r in facts
                if r['delivery_item_id']==row['delivery_item_id'] and r['source_kind']=='inventory' and r['component_snapshot_id'] is None and r['requirement_product_id']==row['source_product_id']),Fraction(0))
            missing=Fraction(row['customer_quantity'])-parent_credit
            available=max(sum(r['direct_quantity'] for r in before['direct_completion_sources'])-before['order_delivered_quantity'],0)
            if missing>0 and missing.denominator==1 and missing<=available:
                coverage.append(dict(delivery_item_id=row['delivery_item_id'],order_item_id=row['order_item_id'],source_product_id=row['source_product_id'],
                    completion_sources=before['direct_completion_sources'],delivered_before=before['order_delivered_quantity'],available_before=available,credited_quantity=int(missing)))
        row['allocation_ids']=[a.id for a in db.scalars(select(DeliveryInventoryAllocation).where(DeliveryInventoryAllocation.delivery_item_id==row['delivery_item_id'],DeliveryInventoryAllocation.consume_movement_id.in_(movement_ids))) ]
    receipt=dict(schema_version=1,idempotency_key=payload.idempotency_key,actor_id=user.id,request_hash=request_hash(delivery_id,payload),
        request=request_value(payload),delivery_id=delivery_id,customer_id=delivery.customer_id,delivery_number=delivery.delivery_number,
        original_version=payload.expected_version,completed_version=int(delivery.version),dispatched_at=utc_naive_to_api(delivery.dispatched_at),
        stages=dict(pick_applied=payload.snapshot['pick_action']=='apply',pick_task_id=payload.snapshot['pick_task']['id'] if payload.snapshot['pick_task'] else None,prepared=True,dispatched=True),
        original_items=payload.snapshot['items'],final_items=final,removed_delivery_item_ids=[key for key,row in expected.items() if row['physical_quantity']==0],movements=facts,direct_component_allocations=direct,direct_completion_coverage=coverage)
    if not consumption_proven(receipt,payload):
        raise RuntimeError('Dispatch consumption proof does not conserve confirmed requirements')
    return receipt


def execute(db,delivery_id,payload,user,api):
    actor_guard(user,payload.expected_actor_id);require_activation(db)
    if payload.snapshot.get('delivery',{}).get('id')!=delivery_id or payload.snapshot.get('delivery',{}).get('version')!=payload.expected_version or digest(payload.snapshot)!=payload.snapshot_hash:
        raise HTTPException(409,'发货快照或版本不一致，请保留原请求核对。')
    # Exact historical evidence precedes all fresh status/stock qualifications.
    value,record=_matched_record(db,delivery_id,payload,user,api)
    if record:
        if value:return value
        raise HTTPException(409,'原请求已有记录但完整证明不可读，请查看原单并联系管理员核对，勿换标识重发。')
    try:
        order_ids=list(db.scalars(select(OrderItem.order_id).join(DeliveryItem,DeliveryItem.order_item_id==OrderItem.id)
            .where(DeliveryItem.delivery_id==delivery_id,DeliveryItem.is_current.is_(True)).distinct().order_by(OrderItem.order_id)))
        api.lock_order_rows_for_production_transition(db,order_ids)
        claimed=db.execute(update(Delivery).where(Delivery.id==delivery_id).values(version=Delivery.version).execution_options(synchronize_session=False))
        # A peer may have completed this same key while we waited for the lock.
        value,record=_matched_record(db,delivery_id,payload,user,api)
        if record:
            if value:db.rollback();return value
            raise HTTPException(409,'原请求已有记录但完整证明不可读，请保留并查看原单。')
        if claimed.rowcount!=1:raise HTTPException(404,'送货单不存在。')
        row=api._delivery_for_user(db,delivery_id,user);db.refresh(row)
        if row.version!=payload.expected_version:raise HTTPException(409,'送货单版本已变化，请核对原数量；不能直接发后来修改的数量。')
        actual=snapshot(db,delivery_id,user,api,pick_action=payload.snapshot.get('pick_action'),refresh=True)['snapshot']
        if canonical(actual)!=canonical(payload.snapshot):raise HTTPException(409,'送货明细或实际拿货已变化，请重新核对，不会自动采用新数量。')
        first_movement_id=int(db.scalar(select(func.coalesce(func.max(InventoryMovement.id),0))) or 0)
        if actual['pick_action']=='apply':
            if actual['pick_task']['status']=='exception' and not payload.confirm_pick_exception:
                raise HTTPException(409,'拿货存在异常，请先明确确认实际拿货结果。')
            api._apply_delivery_pick_task(actual['pick_task']['id'],db=db,user=user,commit=False)
        api._prepare_legacy_accompany(db,row,user)
        api._dispatch_delivery(delivery_id,db=db,user=user,commit=False,advance_version=True)
        receipt=build_receipt(db,delivery_id,payload,user,api,first_movement_id)
        value=dict(schema_version=1,result='completed',current_actor_id=user.id,dispatch_receipt=receipt,current=None)
        api._record_delivery_idempotency(db,idempotency_key=payload.idempotency_key,request_hash=request_hash(delivery_id,payload),
            action=ACTION,actor=user,delivery_id=delivery_id,response=value)
        db.flush();commit_command(db)
        return value
    except IntegrityError:
        # Another resource can race for the global key despite our delivery
        # lock. Re-read only after rollback, then apply the same exact metadata,
        # actor, scope and proof checks; never overwrite a terminal record.
        db.rollback()
        value, record = _matched_record(db, delivery_id, payload, user, api)
        if value:
            return value
        if record:
            raise HTTPException(409, '原请求记录不可完整核对，请保留原内容并查看原单。')
        raise
    except Exception as error:
        db.rollback()
        if isinstance(error,HTTPException):
            error.headers=headers(**(error.headers or {}))
        raise


def close(db, delivery_id, key, payload, user, api):
    """Explicitly end this exact request; neither cancel nor alter a delivery."""
    actor_guard(user, payload.expected_actor_id)
    original = payload.original_request
    actor_guard(user, original.expected_actor_id)
    require_activation(db)
    if (key != original.idempotency_key or original.snapshot.get('delivery', {}).get('id') != delivery_id
            or original.snapshot.get('delivery', {}).get('version') != original.expected_version
            or digest(original.snapshot) != original.snapshot_hash):
        raise HTTPException(409, '原请求标识、版本或快照不一致，请保留原内容核对。')
    value, record = _matched_record(db, delivery_id, original, user, api)
    if record:
        if value:
            return value
        raise HTTPException(409, '原请求已有记录但完整证明不可读，请保留并查看原单。')
    try:
        # Same Order -> Delivery claim as execute. An in-flight command wins
        # wholly before or after this claim; close does not test old version,
        # pending status, or stock qualifications.
        order_ids = list(db.scalars(select(OrderItem.order_id).join(DeliveryItem, DeliveryItem.order_item_id == OrderItem.id)
            .where(DeliveryItem.delivery_id == delivery_id, DeliveryItem.is_current.is_(True)).distinct().order_by(OrderItem.order_id)))
        api.lock_order_rows_for_production_transition(db, order_ids)
        claimed = db.execute(update(Delivery).where(Delivery.id == delivery_id).values(version=Delivery.version).execution_options(synchronize_session=False))
        value, record = _matched_record(db, delivery_id, original, user, api)
        if record:
            if value:
                db.rollback()
                return value
            raise HTTPException(409, '原请求已有记录但完整证明不可读，请保留并查看原单。')
        if claimed.rowcount != 1:
            raise HTTPException(404, '送货单不存在。')
        row = api._delivery_for_user(db, delivery_id, user)
        db.refresh(row)
        for current_row in _rows(db, delivery_id, refresh=True):
            item = db.get(OrderItem, current_row.order_item_id) if current_row.order_item_id else None
            owner = db.get(Order, item.order_id) if item else None
            require_customer_access(owner.customer_id if owner else row.customer_id, user, db)
        original_customer = original.snapshot['delivery']['customer_id']
        require_customer_access(original_customer, user, db)
        for original_row in original.snapshot['items']:
            require_customer_access(original_row['customer_id'], user, db)
        closure = dict(schema_version=1, idempotency_key=key, actor_id=user.id,
            request_hash=request_hash(delivery_id, original), request=request_value(original), delivery_id=delivery_id,
            customer_id=original_customer, delivery_number=row.delivery_number, closed_at=utc_naive_to_api(utc_now_naive()))
        value = dict(schema_version=1, result='closed', current_actor_id=user.id, dispatch_receipt=None,
            closure_receipt=closure, current=None)
        api._record_delivery_idempotency(db, idempotency_key=key, request_hash=closure['request_hash'],
            action=ACTION, actor=user, delivery_id=delivery_id, response=value)
        api._write_audit(db, user=user, action='CLOSE_DISPATCH_REQUEST', resource='Delivery', entity_id=delivery_id,
            details={'request_hash':closure['request_hash'],'idempotency_key_sha256':digest(key)},
            description='明确结束原发货请求；送货数量和库存不变')
        db.flush()
        commit_command(db)
        return value
    except IntegrityError:
        db.rollback()
        value, record = _matched_record(db, delivery_id, original, user, api)
        if value:
            return value
        if record:
            raise HTTPException(409, '原请求记录不可完整核对，请保留并查看原单。')
        raise
    except Exception:
        db.rollback()
        raise
