"""An explicit reminder over the original order, fulfilled only by real dispatch."""
import json
from datetime import date
from fractions import Fraction
from sqlalchemy import select,func
from fastapi import HTTPException
from app.models.delivery_backlog import DeliveryBacklog as Backlog,DeliveryBacklogSource as Source,DeliveryBacklogFulfillment as Fulfillment
from app.models.order import Order,OrderItem
from app.models.product import Product
from app.models.delivery import Delivery,DeliveryItem
from app.core.time_contract import utc_now_naive,utc_naive_to_api


def fail(message):raise HTTPException(409,message)


def fulfilled(db,backlog_id):
    return int(db.scalar(select(func.coalesce(func.sum(Fulfillment.quantity),0)).where(Fulfillment.backlog_id==backlog_id,Fulfillment.status=='posted')) or 0)


def debt(db,row):
    """A tracking subset of the original order; never added to order debt totals."""
    item=db.get(OrderItem,row.order_item_id)
    return max(min(row.target_quantity-fulfilled(db,row.id),item.quantity-item.delivered_quantity),0)


def audit(db,row,actor,action,details,source='web'):
    from app.services.audit_log import append_audit_event
    append_audit_event(db,event_category='business',result='success',source=source,module_code='deliveries',action_code='backlog.'+action,
        resource='delivery_backlog',actor=actor,entity_type='delivery_backlog',entity_id=row.id,customer_id=row.customer_id,details=details)


def defer(db,notice,*,quantity,reason,actor=None,mobile=False,excluded_delivery_id=None,excluded_quantity=0):
    from app.models.tianhua_pre_delivery import TianhuaPreDeliveryImportBatch
    from app.services.production_workflow import lock_order_rows_for_production_transition
    from app.api.deps import require_customer_access
    from app.services.order_status_policy import DELIVERY_CANDIDATE_ORDER_STATUSES
    batch=db.get(TianhuaPreDeliveryImportBatch,notice.batch_id)
    item=db.get(OrderItem,notice.order_item_id) if notice.order_item_id else None
    order=db.get(Order,item.order_id) if item else None
    if not item or not order or not item.product_id or batch.customer_id!=order.customer_id or notice.product_id!=item.product_id:
        fail('请先核实预送货通知对应的客户、产品和原订单，不能猜测欠送来源')
    if actor:require_customer_access(order.customer_id,actor,db)
    lock_order_rows_for_production_transition(db,[order.id])
    db.refresh(item)
    previous=db.scalar(select(Source).where(Source.import_item_id==notice.id))
    if previous:
        snapshot=json.loads(previous.snapshot_json)
        if quantity!=previous.requested_quantity or (reason or '').strip()!=snapshot['reason'] or bool(mobile)!=snapshot['mobile_token_scoped']:
            fail('同一通知已登记待补送，本次数量或原因不同；请刷新核对原记录')
        return db.get(Backlog,previous.backlog_id)
    if order.status not in DELIVERY_CANDIDATE_ORDER_STATUSES or item.is_force_closed:fail('原订单已关闭或不可继续送货')
    if quantity<=0 or quantity>max(item.quantity-item.delivered_quantity,0):fail('待补送数量必须在原订单实际未送范围内')
    clean=(reason or '').strip()
    if not clean:fail('请填写本次未送原因')
    row=db.scalar(select(Backlog).where(Backlog.order_item_id==item.id))
    if row and row.status=='cancelled':fail('该订单的待补送已由管理员取消，请先核对原取消决定')
    if row:
        # Multiple screenshots of the same order are overlapping reminders,
        # not new orders. Never add their requested quantities together.
        before=row.target_quantity
        row.target_quantity=max(before,fulfilled(db,row.id)+quantity)
        row.version+=1
    else:
        row=Backlog(customer_id=order.customer_id,product_id=item.product_id,order_item_id=item.id,
            stock_code=item.snapshot_product_code or db.get(Product,item.product_id).product_code,
            product_name=item.snapshot_product_name or db.get(Product,item.product_id).product_name,
            customer_order_no=notice.customer_order_no or order.customer_po,due_date=order.delivery_date or batch.pre_delivery_date,
            original_quantity=quantity,target_quantity=quantity,reason=clean,created_by=actor.id if actor else None)
        db.add(row);db.flush();before=0
    db.add(Source(backlog_id=row.id,import_item_id=notice.id,requested_quantity=quantity,snapshot_json=json.dumps(dict(
        batch_id=batch.id,batch_number=batch.batch_number,notice_quantity=notice.image_qty,customer_order_no=notice.customer_order_no or notice.image_order_no,
        order_item_id=item.id,order_quantity=item.quantity,delivered_at_deferral=item.delivered_quantity,reason=clean,mobile_token_scoped=mobile,
        excluded_delivery_id=excluded_delivery_id,excluded_delivery_number=db.get(Delivery,excluded_delivery_id).delivery_number if excluded_delivery_id else None,
        excluded_quantity=excluded_quantity),ensure_ascii=False)))
    audit(db,row,actor,'defer',dict(import_item_id=notice.id,requested_quantity=quantity,before_target=before,after_target=row.target_quantity),source='mobile' if mobile else 'web')
    db.flush();return row


def _quantity_context(db,item):
    from app.services.direct_external_finished import eligible
    from app.services.delivery_quantities import order_basis,QuantityContractError
    order=db.get(Order,item.order_id)
    product=db.get(Product,item.product_id)
    customer_unit=str(item.sales_unit_snapshot or (product.unit if product else '') or '只').strip()
    try:
        basis=order_basis(item,order.customer_id) if eligible(db,item) else None
    except QuantityContractError as error:
        fail(str(error))
    return basis,customer_unit,(basis['physical_unit'] if basis else customer_unit)


def physical_ready(db,item):
    from app.models.warehouse_inventory import InventoryReservation,InventoryLot,FinishedGoodsInventoryDetail
    from app.api.deliveries import _delivery_remaining_quantity
    from app.services.warehouse_inventory import finished_inventory_candidates,WarehouseInventoryError
    from app.services.fixed_shelf_staging import held_for_staging_expression
    from app.services.bom_inventory_contract import is_body_lot
    from app.services.delivery_quantities import (available_customer_quantity,
        requirement_amount,reservation_customer_quantity)
    basis,_,_=_quantity_context(db,item)
    rows=db.execute(select(InventoryReservation,InventoryLot).join(InventoryLot,InventoryLot.id==InventoryReservation.inventory_lot_id)
        .join(FinishedGoodsInventoryDetail,FinishedGoodsInventoryDetail.inventory_lot_id==InventoryLot.id)
        .where(InventoryReservation.order_item_id==item.id,InventoryReservation.reservation_type=='finished_order',InventoryReservation.status!='cancelled',
            InventoryReservation.sales_order_item_bom_component_id.is_(None),
            InventoryLot.inventory_type=='finished',InventoryLot.status=='active',~held_for_staging_expression(),FinishedGoodsInventoryDetail.product_id==item.product_id,
            FinishedGoodsInventoryDetail.owner_customer_id==db.get(Order,item.order_id).customer_id)).all()
    reserved_credit=Fraction(0)
    reserved_physical=0
    for reservation,lot in rows:
        if is_body_lot(lot):continue
        stock=max(min(int(reservation.reserved_stock_quantity or 0)
            -int(reservation.consumed_stock_quantity or 0)
            -int(reservation.released_stock_quantity or 0),int(lot.quantity_reserved or 0)),0)
        if stock<=0:continue
        credit=max(requirement_amount(reservation,'credited_requirement_quantity')
            -requirement_amount(reservation,'consumed_requirement_quantity')
            -requirement_amount(reservation,'released_requirement_quantity'),0)
        reserved_credit+=min(credit,reservation_customer_quantity(reservation,stock))
        reserved_physical+=stock
    if basis:
        reserved=min(int(reserved_credit),available_customer_quantity(basis,reserved_physical))
        customer_step=int(basis['customer_basis'])
    else:
        reserved=int(reserved_credit)
        customer_step=1
    ready=min(reserved,max(_delivery_remaining_quantity(db,item),0))
    ready-=ready%customer_step
    try:free=sum(l.quantity_available for l in finished_inventory_candidates(db,item.id) if l.finished_detail.owner_customer_id==db.get(Order,item.order_id).customer_id)
    except WarehouseInventoryError:free=0
    free_customer=available_customer_quantity(basis,free) if basis else free
    return ready,free_customer


def describe(db,row,*,inventory=True):
    from app.models.customer import Customer
    item=db.get(OrderItem,row.order_item_id);order=db.get(Order,item.order_id);customer=db.get(Customer,row.customer_id)
    done=fulfilled(db,row.id);remaining=debt(db,row) if row.status=='active' else 0
    basis,customer_unit,physical_unit=_quantity_context(db,item)
    ready,free=(physical_ready(db,item) if inventory and remaining and not item.is_force_closed else (0,0))
    ready_customer=min(remaining,ready);free_customer=min(remaining,free)
    from app.services.delivery_quantities import physical_for
    if basis:
        customer_step=int(basis['customer_basis'])
        ready_customer-=ready_customer%customer_step
        free_customer-=free_customer%customer_step
    ready_physical=(physical_for(basis,ready_customer) if basis else ready_customer)
    free_physical=(physical_for(basis,free_customer) if basis else free_customer)
    return dict(id=row.id,version=row.version,customer_id=row.customer_id,customer_name=customer.chinese_short_name or customer.name,order_id=order.id,
        product_id=row.product_id,stock_code=row.stock_code,product_name=row.product_name,order_item_id=item.id,order_number=order.order_number,
        customer_order_no=row.customer_order_no,due_date=row.due_date,original_quantity=row.original_quantity,target_quantity=row.target_quantity,
        fulfilled_quantity=done,remaining_quantity=remaining,order_remaining_quantity=max(item.quantity-item.delivered_quantity,0),
        ready_quantity=ready_customer,available_finished_quantity=free_customer,
        ready_physical_quantity=ready_physical,available_finished_physical_quantity=free_physical,
        customer_unit=customer_unit,physical_unit=physical_unit,reason=row.reason,
        status='cancelled' if row.status=='cancelled' else 'fulfilled' if remaining==0 else 'partial' if done else 'waiting',
        cancelled_reason=row.cancelled_reason,created_at=utc_naive_to_api(row.created_at),
        sources=[dict(import_item_id=s.import_item_id,requested_quantity=s.requested_quantity,**json.loads(s.snapshot_json)) for s in db.scalars(select(Source).where(Source.backlog_id==row.id))],
        deliveries=[dict(delivery_id=db.get(DeliveryItem,f.delivery_item_id).delivery_id,delivery_item_id=f.delivery_item_id,quantity=f.quantity,status=f.status,dispatched_at=utc_naive_to_api(f.dispatched_at)) for f in db.scalars(select(Fulfillment).where(Fulfillment.backlog_id==row.id).order_by(Fulfillment.id))])


def scoped(db,user):
    from app.api.deps import has_unrestricted_customer_access,customer_scope_ids
    query=select(Backlog)
    if not has_unrestricted_customer_access(user,db):query=query.where(Backlog.customer_id.in_(customer_scope_ids(user,db)))
    return query


def listing(db,user,*,page=1,customer_id=None,product_id=None,history=False):
    query=scoped(db,user)
    if customer_id is not None:query=query.where(Backlog.customer_id==customer_id)
    if product_id is not None:query=query.where(Backlog.product_id==product_id)
    if not history:
        done=select(func.coalesce(func.sum(Fulfillment.quantity),0)).where(Fulfillment.backlog_id==Backlog.id,Fulfillment.status=='posted').correlate(Backlog).scalar_subquery()
        query=query.join(OrderItem,OrderItem.id==Backlog.order_item_id).where(Backlog.status=='active',Backlog.target_quantity>done,OrderItem.quantity>OrderItem.delivered_quantity)
    rows=db.scalars(query.order_by(Backlog.due_date.asc().nulls_last(),Backlog.created_at,Backlog.id).offset((page-1)*20).limit(21)).all()
    return dict(items=[describe(db,row) for row in rows[:20]],has_more=len(rows)>20,page=page)


def suggestions(db,user,*,customer_id,product_id,quantity,customer_order_no=None):
    from app.api.deps import require_customer_access
    require_customer_access(customer_id,user,db)
    rows=list(db.scalars(scoped(db,user).where(Backlog.customer_id==customer_id,Backlog.product_id==product_id,Backlog.status=='active')))
    rows.sort(key=lambda r:(0 if customer_order_no and r.customer_order_no==customer_order_no else 1,r.due_date or date.max,r.created_at,r.id))
    remaining=quantity;output=[]
    for row in rows:
        data=describe(db,row)
        if not data['remaining_quantity']:continue
        item=db.get(OrderItem,row.order_item_id)
        from app.api.deliveries import _delivery_kit_metadata,_inventory_sources_for_order_item
        data['candidate']=dict(order_item_id=item.id,order_id=item.order_id,order_number=data['order_number'],
            customer_po=data['customer_order_no'],product_id=row.product_id,product_code=row.stock_code,product_name=row.product_name,
            specification=item.snapshot_spec or '',remaining_quantity=data['ready_quantity'],deliverable_quantity=data['ready_quantity'],
            order_remaining_quantity=data['order_remaining_quantity'],
            inventory_sources=_inventory_sources_for_order_item(db,order_item=item,planned_delivery_quantity=min(quantity,data['ready_quantity'])),
            **_delivery_kit_metadata(db,item,planned_delivery_quantity=min(quantity,data['ready_quantity'])))
        take=min(remaining,data['remaining_quantity'],data['ready_quantity']);remaining-=take
        output.append(dict(data,suggested_quantity=take,same_customer_order_no=bool(customer_order_no and row.customer_order_no==customer_order_no)))
    return dict(items=output,requested_quantity=quantity,unallocated_quantity=remaining,
        notice='草稿只关联原订单；按实际发货数量核销。已有新单明细不能冒充旧单补送，请按预览选择对应原订单。')


def dispatch(db,delivery,lines,actor,dispatched_at):
    for line in lines:
        if not line.order_item_id:continue
        row=db.scalar(select(Backlog).where(Backlog.order_item_id==line.order_item_id,Backlog.status=='active'))
        if not row:continue
        if row.customer_id!=delivery.customer_id or row.product_id!=db.get(OrderItem,line.order_item_id).product_id:fail('待补送与实际发货客户或产品不一致')
        if db.scalar(select(Fulfillment.id).where(Fulfillment.backlog_id==row.id,Fulfillment.delivery_item_id==line.id,Fulfillment.dispatched_at==dispatched_at)):continue
        # Order delivered_quantity has already advanced in this same transaction.
        source_details=[json.loads(s.snapshot_json) for s in db.scalars(select(Source).where(Source.backlog_id==row.id))]
        excluded=max((s.get('excluded_quantity',0) for s in source_details if s.get('excluded_delivery_id')==delivery.id and s.get('excluded_delivery_number')==delivery.delivery_number),default=0)
        take=min(max(row.target_quantity-fulfilled(db,row.id),0),max(int(line.delivered_quantity)-excluded,0))
        if not take:continue
        db.add(Fulfillment(backlog_id=row.id,delivery_item_id=line.id,quantity=take,dispatched_at=dispatched_at,created_by=actor.id))
        row.version+=1;audit(db,row,actor,'dispatch',dict(delivery_id=delivery.id,delivery_item_id=line.id,quantity=take))
    db.flush()


def reverse(db,delivery,actor):
    rows=db.scalars(select(Fulfillment).join(DeliveryItem,DeliveryItem.id==Fulfillment.delivery_item_id)
        .where(DeliveryItem.delivery_id==delivery.id,Fulfillment.status=='posted')).all()
    for fact in rows:
        row=db.get(Backlog,fact.backlog_id)
        if row.customer_id!=delivery.customer_id:fail('待补送撤销客户不一致')
        fact.status='reversed';fact.reversed_at=utc_now_naive();row.version+=1
        audit(db,row,actor,'reverse_dispatch',dict(delivery_id=delivery.id,delivery_item_id=fact.delivery_item_id,quantity=fact.quantity))
    db.flush()


def cancel(db,row,actor,*,expected_version,reason):
    from app.services.production_workflow import lock_order_rows_for_production_transition
    from app.api.deps import require_customer_access
    if actor.role!='admin' or not actor.is_active:raise HTTPException(403,'仅管理员可确认取消待补送')
    require_customer_access(row.customer_id,actor,db)
    lock_order_rows_for_production_transition(db,[db.get(OrderItem,row.order_item_id).order_id])
    db.refresh(row)
    clean=(reason or '').strip()
    if not clean:fail('取消待补送须说明客户取消原因')
    if row.status=='cancelled' and row.cancelled_reason==clean:return describe(db,row,inventory=False)
    if row.version!=expected_version or row.status!='active':fail('待补送已变化，请刷新核对')
    row.status='cancelled';row.cancelled_reason=clean;row.cancelled_by=actor.id;row.cancelled_at=utc_now_naive();row.version+=1
    audit(db,row,actor,'cancel',dict(reason=clean,remaining_quantity=debt(db,row),order_unchanged=True))
    db.flush();return describe(db,row,inventory=False)
