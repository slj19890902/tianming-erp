"""Authenticated shelf scans acknowledge picking, never physical staging."""
import hashlib
import json
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, update, or_
from sqlalchemy.orm import Session
from app.api.deps import get_db, require_customer_access, has_permission
from app.api.deliveries import can_pick, _pick_task_for_user, _pick_task_response, _write_audit
from app.models.delivery import Delivery, DeliveryItem, DeliveryPickTask, DeliveryPickTaskItem
from app.models.fixed_shelf import ShelfBinding, ShelfProfile, ShelfMutation
from app.models.order import OrderItem
from app.models.product import Product
from app.models.user import User
from app.models.warehouse_inventory import WarehouseLocation
from app.services.fixed_shelf import location_issue, profile_info
from app.services.fixed_shelf_staging import item_product

router = APIRouter()


def label_product(db, user, l, p, v, a):
    binding = db.get(ShelfBinding, l)
    product = db.get(Product, p)
    if not binding or binding.product_id != p or not product or product.deleted_at:
        raise HTTPException(409, '标签料号绑定已变化，请核对实物并重印标签')
    require_customer_access(product.customer_id, user, db)
    profile = db.get(ShelfProfile, p)
    location = db.get(WarehouseLocation, l)
    if profile.version != v or location.address_version != a:
        raise HTTPException(409, '标签配置或地址已变化，请按最新货位重新打印')
    issue = location_issue(db, location)
    if issue:
        raise HTTPException(409, issue)
    return product


def matching_items(db, task, product_id):
    return [item for item in task.items if (product := item_product(db, item)) and product.id == product_id]


def scan_problem(task_response, items, location_id):
    by_id = {row['id']: row for row in task_response['items']}
    for item in items:
        if item.status == 'picked' and item.picked_quantity == item.original_quantity:
            continue
        row = by_id[item.id]
        if not row['location_plan_complete'] or row.get('is_composite_bom'):
            return '该款来源尚未完整分配或含组合子件，请在拿货明细中核对'
        lines = [line for line in row['location_lines'] if line.get('pick_quantity', 0) > 0]
        if not lines or any(line.get('source_type') != 'finished_inventory' or not line.get('lot_id')
                or line.get('location_id') != location_id for line in lines):
            return '该款实际拿货来源不全在此货位，请按拿货明细逐项核对，不能扫一格确认其他位置'
    return None


@router.get('/context')
def scan_context(l: int, p: int, v: int, a: int, db: Session = Depends(get_db), user: User = Depends(can_pick)):
    product = label_product(db, user, l, p, v, a)
    query = select(DeliveryPickTask.id).join(Delivery, Delivery.id == DeliveryPickTask.delivery_id)
    query = query.join(DeliveryPickTaskItem, DeliveryPickTaskItem.task_id == DeliveryPickTask.id)
    query = query.join(DeliveryItem, DeliveryItem.id == DeliveryPickTaskItem.delivery_item_id)
    query = query.outerjoin(OrderItem, OrderItem.id == DeliveryPickTaskItem.order_item_id).where(
        Delivery.customer_id == product.customer_id, Delivery.status == 'pending',
        DeliveryPickTask.status.in_(['pushed', 'driver_confirmed', 'exception']),
        or_(OrderItem.product_id == p, DeliveryItem.product_id == p))
    if not has_permission(user, 'deliveries.execute'):
        query = query.where(DeliveryPickTask.assigned_to == user.id)
    ids = db.scalars(query.distinct().order_by(DeliveryPickTask.id.desc()).limit(51)).all()
    if len(ids) > 50:
        raise HTTPException(409, '匹配任务过多，请先分配任务给实际拿货员工')
    tasks = []
    for task_id in ids:
        task = _pick_task_for_user(db, task_id, user)
        items = matching_items(db, task, p)
        response = _pick_task_response(db, task)
        tasks.append({'id': task.id, 'delivery_number': response['delivery_number'],
            'quantity': sum(item.original_quantity for item in items),
            'already_picked': all(item.status == 'picked' and item.picked_quantity == item.original_quantity for item in items),
            'print_version': response['print_version'], 'problem': scan_problem(response, items, l)})
    info = profile_info(db, product)
    return {'user_id': user.id, 'customer': info['customer_short_name'], 'inventory_code': info['inventory_code'],
        'product_name': product.product_name, 'tasks': tasks}


class ScanPayload(BaseModel):
    l: int = Field(gt=0)
    p: int = Field(gt=0)
    v: int = Field(gt=0)
    a: int = Field(gt=0)
    task_id: int = Field(gt=0)
    print_version: str = Field(min_length=1, max_length=100)
    idempotency_key: str = Field(min_length=1, max_length=100)


@router.post('/confirm')
def confirm_scan(payload: ScanPayload, db: Session = Depends(get_db), user: User = Depends(can_pick)):
    try:
        task = _pick_task_for_user(db, payload.task_id, user)
        claimed = db.execute(update(Delivery).where(Delivery.id == task.delivery_id,
            Delivery.status == 'pending').values(version=Delivery.version))
        if claimed.rowcount != 1:
            raise HTTPException(409, '送货单已发货或作废，扫码不能修改')
        db.expire_all()
        task = _pick_task_for_user(db, payload.task_id, user)
        label_product(db, user, payload.l, payload.p, payload.v, payload.a)
        if task.status not in {'pushed', 'driver_confirmed', 'exception'}:
            raise HTTPException(409, '拿货任务状态已变化，请重新选择任务')
        digest = hashlib.sha256(json.dumps({'actor': user.id, **payload.model_dump()}, sort_keys=True).encode()).hexdigest()
        previous = db.get(ShelfMutation, payload.idempotency_key)
        if previous:
            if previous.request_hash != digest:
                raise HTTPException(409, '扫码请求编号已被用于其他操作')
            return json.loads(previous.result_json)
        items = matching_items(db, task, payload.p)
        if not items:
            raise HTTPException(409, '该料号不在所选拿货任务中')
        response = _pick_task_response(db, task)
        already = all(item.status == 'picked' and item.picked_quantity == item.original_quantity for item in items)
        if not already and payload.print_version != response['print_version']:
            raise HTTPException(409, '拿货来源或数量已变化，请重新扫码核对')
        problem = scan_problem(response, items, payload.l)
        if problem:
            raise HTTPException(409, problem)
        if not already:
            for item in items:
                item.status = 'picked'
                item.picked_quantity = item.original_quantity
            task.status = 'pushed'
            task.submitted_at = None
            task.submitted_by = None
            _write_audit(db, user=user, action='SCAN_FIXED_SHELF_PICK', resource='DeliveryPickTask', entity_id=task.id,
                details={'product_id': payload.p, 'location_id': payload.l, 'item_ids': [item.id for item in items],
                    'quantity': sum(item.original_quantity for item in items), 'inventory_moved': False},
                description='货架扫码标记本款拿齐，实际集货另行确认')
        result = {'task_id': task.id, 'delivery_number': response['delivery_number'],
            'quantity': sum(item.original_quantity for item in items), 'already_picked': already,
            'message': '已记录本款拿齐，集货另行确认', 'inventory_moved': False}
        db.add(ShelfMutation(idempotency_key=payload.idempotency_key, request_hash=digest,
            result_json=json.dumps(result, ensure_ascii=False)))
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise
