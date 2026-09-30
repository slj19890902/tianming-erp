"""Typed customer-scoped requests. No business writes occur before review."""
from datetime import datetime
import hashlib
import json
from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from sqlalchemy import select, update, inspect, event
from sqlalchemy.exc import IntegrityError, OperationalError
from app.api.deps import require_customer_access, has_permission
from app.models.business_approval import BusinessApproval
from app.models.user import User
from app.models.customer import Customer
from app.models.product import Product
from app.models.order import Order, OrderItem
from app.models.delivery import Delivery
from app.models.finance import Statement
from app.services.audit_log import append_audit_event
from app.services.business_request_fields import FIELDS, hydrate

ACTIONS = {"customer_update":"客户资料修改", "product_update":"常用箱修改", "order_update":"订单修改",
           "stock_replenishment":"库存预警报料", "delivery_create":"送货单申请", "statement_create":"对账申请", "statement_confirm":"确认对账申请", "invoice_task_create":"开票申请"}

def encoded(value):
    return json.dumps(jsonable_encoder(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))

def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()

def specs(action):
    from app.api import customers, products, orders, requisition, deliveries, finance, invoice_tasks
    return {
      "customer_update":(customers.CustomerUpdatePayload, Customer, customers.update_customer, "customer_id"),
      "product_update":(products.ProductUpdatePayload, Product, products.update_product, "product_id"),
      "order_update":(orders.OrderUpdate, Order, orders.update_order, "order_id"),
      "stock_replenishment":(requisition.StockReplenishmentCreatePayload,None,requisition.create_stock_replenishment_order,None),
      "delivery_create":(deliveries.DeliveryCreate,None,deliveries.create_delivery,None),
      "statement_create":(finance.StatementCreate,None,finance.create_statement,None),
      "statement_confirm":(invoice_tasks.VersionPayload,Statement,invoice_tasks.confirm_statement_for_invoice,"statement_id"),
      "invoice_task_create":(invoice_tasks.TaskCreatePayload,Statement,invoice_tasks._create_invoice_task,"statement_id"),
    }[action]

def object_customer(obj):
    return obj.id if isinstance(obj,Customer) else obj.customer_id

def basis(db, action, target_id, customer_id, payload, user):
    require_customer_access(customer_id,user,db)
    schema, model, _, _ = specs(action)
    facts=[]
    def check(obj):
        if obj is None: raise HTTPException(404,"申请关联资料不存在")
        owner=object_customer(obj)
        require_customer_access(owner,user,db)
        if owner != customer_id: raise HTTPException(409,"申请明细与客户不一致")
        facts.append({"table":obj.__tablename__,"values":{c.key:getattr(obj,c.key) for c in inspect(obj).mapper.column_attrs}})
    if model:
        check(db.get(model,target_id))
    elif target_id is not None:
        raise HTTPException(422,"新增申请不得指定已有对象")
    if action in {"invoice_task_create", "statement_confirm"}:
        from app.api.finance import _statement_for_user
        _statement_for_user(db,target_id,user)
    if payload.get("customer_id") not in (None,customer_id): raise HTTPException(409,"不得变更客户归属")
    def refs(value):
        if isinstance(value,list):
            for item in value: refs(item)
        elif isinstance(value,dict):
            for key,item in value.items():
                if key in ("product_id","reference_product_id") and item: check(db.get(Product,item))
                if key=="order_item_id" and item:
                    oi=db.get(OrderItem,item)
                    if oi is None: raise HTTPException(404,"订单明细不存在")
                    check(db.get(Order,oi.order_id))
                    facts.append({"order_item":{c.key:getattr(oi,c.key) for c in inspect(oi).mapper.column_attrs}})
                if key=="delivery_ids":
                    for value_id in item: check(db.get(Delivery,value_id))
                if key=="stock_policy_id" and item:
                    from app.models.stock_replenishment import InventoryStockPolicy
                    check(db.get(InventoryStockPolicy,item))
                if key=="customer_id" and item and item != customer_id: raise HTTPException(403,"申请只能包含本客户明细")
                if isinstance(item,(list,dict)): refs(item)
    refs(payload)
    if action=="stock_replenishment":
        if payload.get("source_type") != "stock_warning" or payload.get("stock_now"):
            raise HTTPException(422,"此入口仅支持库存预警报料申请")
        if any(not item.get("stock_policy_id") for item in payload.get("items",[])):
            raise HTTPException(422,"请从库存预警选择需要补库的产品")
    if action=="delivery_create":
        if payload.get("historical_backfill") or any(i.get("source_type","order")!="order" for i in payload.get("items",[])):
            raise HTTPException(422,"业务送货申请仅支持本客户正式订单，不支持历史补录")
    if action=="statement_create":
        from app.api.finance import _settlement_context_for_customer
        _, group_ids = _settlement_context_for_customer(db,customer_id)
        for group_id in group_ids: require_customer_access(group_id,user,db)
        facts.append({"settlement_customers":sorted(group_ids)})
    if action=="statement_create" and (payload.get("return_receipt_item_ids") or payload.get("customer_charge_ids")):
        raise HTTPException(422,"请按完整送货单提交对账申请")
    if action=="statement_create" and not payload.get("delivery_ids"):
        raise HTTPException(422,"请明确选择需要对账的送货单")
    if action=="invoice_task_create" and (payload.get("seller_change_type") or payload.get("confirm_permanent_change")):
        raise HTTPException(422,"开票申请不能变更销方主档")
    # JSON storage sorts keys; reference visitation order must not change a snapshot.
    return digest(sorted({encoded(fact) for fact in facts}))

def audit(db,row,user,action):
    append_audit_event(db,actor=user,event_category="business",result="success",source="web",module_code="approval",
        action_code="business_approval."+action,resource="BusinessApproval",entity_id=row.id,customer_id=row.customer_id,
        description=ACTIONS[row.action]+"："+action,details={"request_id":row.id,"applicant_id":row.applicant_id,"version":row.version})

def view(row,db):
    return {"id":row.id,"action":row.action,"action_name":ACTIONS[row.action],"customer_id":row.customer_id,
        "customer_name":db.get(Customer,row.customer_id).name,"applicant":db.get(User,row.applicant_id).display_name or db.get(User,row.applicant_id).real_name,
        "target_id":row.target_id,"payload":json.loads(row.payload_json),"before":json.loads(row.before_json),"note":row.note,"status":row.status,"version":row.version,
        "decision_note":row.decision_note,"result":json.loads(row.result_json) if row.result_json else None,
        "created_at":row.created_at,"reviewed_at":row.reviewed_at}

def submit(db,user,body):
    if not has_permission(user,"business_requests.submit"): raise HTTPException(403,"没有提交申请权限")
    if body.action not in ACTIONS: raise HTTPException(422,"不支持的申请类型")
    require_customer_access(body.customer_id,user,db)
    payload=dict(body.payload)
    # Hidden cost fields are not writable through an approval either.
    if body.action=="product_update":
        from app.api.products import _COST_SENSITIVE_PRODUCT_FIELDS
        for key in _COST_SENSITIVE_PRODUCT_FIELDS: payload.pop(key,None)
        for key in ("external_supply","external_packaging_candidate_snapshot_json"): payload.pop(key,None)
    if body.action in {"customer_update","product_update"}:
        if payload.get("is_active") is False or payload.get("status") in {"inactive","deleted","disabled"}:
            raise HTTPException(403,"此账号不能申请删除或停用资料")
    schema,_,_,_=specs(body.action)
    if body.action not in FIELDS:
        validated=schema.model_validate(payload)
        payload=validated.model_dump(mode="json",exclude_unset=True)
    request_hash=digest({"action":body.action,"target_id":body.target_id,"customer_id":body.customer_id,"payload":payload,"note":body.note})
    existing=db.scalar(select(BusinessApproval).where(BusinessApproval.applicant_id==user.id,BusinessApproval.idempotency_key==body.idempotency_key))
    if existing:
        require_customer_access(existing.customer_id,user,db)
        if existing.request_hash!=request_hash: raise HTTPException(409,"同一提交标识的申请内容已变化")
        return view(existing,db)
    if body.action in FIELDS:
        # Replays above remain valid even after the approved edit increments the master version.
        basis(db,body.action,body.target_id,body.customer_id,{},user)
        hydrate(db,body.action,body.target_id,payload)
    snapshot=basis(db,body.action,body.target_id,body.customer_id,payload,user)
    before={}
    if body.action in FIELDS:
        obj=db.get(specs(body.action)[1],body.target_id)
        before={key:getattr(obj,key) for key in payload if key in FIELDS[body.action]}
    references={}
    for item in payload.get("items",[]):
        if item.get("order_item_id"):
            oi=db.get(OrderItem,item["order_item_id"])
            order=db.get(Order,oi.order_id)
            references[f"order_item_id:{oi.id}"]=f"{order.customer_po or order.order_number} / {oi.snapshot_product_code} / {oi.snapshot_product_name}"
    for value_id in payload.get("delivery_ids",[]):
        references[f"delivery_ids:{value_id}"]=db.get(Delivery,value_id).delivery_number
    if references:before["_references"]=references
    row=BusinessApproval(applicant_id=user.id,customer_id=body.customer_id,action=body.action,target_id=body.target_id,
        payload_json=encoded(payload),before_json=encoded(before),basis_hash=snapshot,request_hash=request_hash,idempotency_key=body.idempotency_key,note=body.note)
    try:
        db.add(row);db.flush();audit(db,row,user,"submitted");db.commit();return view(row,db)
    except IntegrityError:
        db.rollback()
        existing=db.scalar(select(BusinessApproval).where(BusinessApproval.applicant_id==user.id,BusinessApproval.idempotency_key==body.idempotency_key))
        if existing and existing.request_hash==request_hash:return view(existing,db)
        raise HTTPException(409,"同一提交标识已有不同申请")

def review(db,user,request_id,body):
    row=db.get(BusinessApproval,request_id)
    if row is None: raise HTTPException(404,"申请不存在")
    if user.role!="admin" and not(user.role=="boss" and row.action=="stock_replenishment"):
        raise HTTPException(403,"此申请需要管理员审批；报料申请也可由老板审批")
    if user.id==row.applicant_id: raise HTTPException(403,"不能审批自己的申请")
    if row.status!="pending":
        if row.status==("applied" if body.approve else "rejected"): return view(row,db)
        raise HTTPException(409,"申请已处理")
    try:
        claimed=db.execute(update(BusinessApproval).where(BusinessApproval.id==row.id,BusinessApproval.version==body.expected_version,BusinessApproval.status=="pending")
            .values(version=BusinessApproval.version+1))
    except OperationalError:
        db.rollback()
        raise HTTPException(409,"申请正在处理，请刷新核对结果")
    if claimed.rowcount!=1: raise HTTPException(409,"申请状态已变化，请刷新")
    db.refresh(row)
    try:
        if body.approve:
            applicant=db.get(User,row.applicant_id)
            if not applicant or not applicant.is_active or not has_permission(applicant,"business_requests.submit"):
                raise HTTPException(409,"申请账号已停用或取消申请权限")
            payload=json.loads(row.payload_json)
            if basis(db,row.action,row.target_id,row.customer_id,payload,applicant)!=row.basis_hash:
                raise HTTPException(409,"关联资料已变化，请重新提交核对后的申请")
            schema,_,fn,arg=specs(row.action)
            validated=hydrate(db,row.action,row.target_id,payload) if row.action in FIELDS else schema.model_validate(payload)
            if row.action in {"customer_update","product_update"} and body.confirmation_token:
                validated.confirmation_token=body.confirmation_token
            kwargs={"payload":validated,"db":db,"user":user}
            if arg: kwargs[arg]=row.target_id
            if row.action=="invoice_task_create": kwargs["commit"]=False
            db.info["business_approval_transaction"]=True
            transaction=db.get_transaction()
            def forbid_early_commit(session):
                raise RuntimeError("审批事务禁止提前提交")
            event.listen(db,"before_commit",forbid_early_commit)
            try:
                result=fn(**kwargs)
                if db.get_transaction() is not transaction:
                    raise HTTPException(409,"业务状态已变化，本次审批未生效，请刷新核对")
            finally:
                event.remove(db,"before_commit",forbid_early_commit)
            if hasattr(result,"model_dump"): result=result.model_dump(mode="json")
            # Only store the business identity, never an admin's full response.
            row.result_json=encoded({k:result[k] for k in ("id","order_number","delivery_number","statement_number","status") if isinstance(result,dict) and k in result})
            row.status="applied"
        else:
            if not body.note.strip(): raise HTTPException(422,"请填写拒绝原因")
            row.status="rejected"
        row.reviewer_id=user.id;row.reviewed_at=datetime.now();row.decision_note=body.note
        audit(db,row,user,row.status);db.flush();db.info.pop("business_approval_transaction",None);db.commit()
        return view(row,db)
    except Exception:
        db.info.pop("business_approval_transaction",None);db.rollback();raise
