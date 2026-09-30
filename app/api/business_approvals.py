from typing import Any
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select, func, or_
from sqlalchemy.orm import Session
from app.api.deps import get_db, get_current_user, customer_scope_ids, has_unrestricted_customer_access, has_permission
from app.models.user import User
from app.models.business_approval import BusinessApproval
from app.services import business_approvals as service
from app.api.deps import require_customer_access
from app.services.business_request_fields import FIELDS
from app.models.customer import Customer
from app.models.product import Product
from app.models.order import Order, OrderItem
from app.models.delivery import Delivery
from app.models.finance import Statement
from app.models.stock_replenishment import InventoryStockPolicy
from app.models.material import Material

router=APIRouter()
class Submit(BaseModel):
    action:str
    customer_id:int=Field(gt=0)
    target_id:int|None=Field(default=None,gt=0)
    payload:dict[str,Any]
    note:str=Field(default="",max_length=1000)
    idempotency_key:str=Field(min_length=8,max_length=160)
class Review(BaseModel):
    approve:bool
    expected_version:int=Field(gt=0)
    note:str=Field(default="",max_length=1000)
    confirmation_token:str|None=Field(default=None,max_length=8000)

def require_submit(user):
    if not has_permission(user,"business_requests.submit"):
        raise HTTPException(403,"没有提交申请权限")

@router.get("/profile")
def profile(user:User=Depends(get_current_user)):
    if user.role!="admin":raise HTTPException(403,"仅管理员可配置账号模板")
    from app.services.scoped_business_profile import overrides
    return {"mode":"selected","overrides":overrides()}

@router.get("/options")
def options(kind:str,customer_id:int|None=None,q:str="",page:int=Query(1,ge=1),db:Session=Depends(get_db),user:User=Depends(get_current_user)):
    require_submit(user)
    if kind=="customers":
        query=select(Customer).where(Customer.is_active.is_(True))
        if not has_unrestricted_customer_access(user,db):query=query.where(Customer.id.in_(customer_scope_ids(user,db)))
        rows=db.scalars(query.order_by(Customer.customer_number)).all()
        return {"items":[{"id":r.id,"label":r.name} for r in rows],"more":False}
    require_customer_access(customer_id,user,db)
    if kind=="materials":
        query=select(Material).where(Material.is_active.is_(True),Material.code.contains(q)).order_by(Material.code,Material.id)
        rows=db.scalars(query.offset((page-1)*100).limit(101)).all()
        return {"items":[{"id":r.id,"label":r.code} for r in rows[:100]],"more":len(rows)>100}
    models={"products":Product,"orders":Order,"order_items":OrderItem,"deliveries":Delivery,"statements":Statement,"policies":InventoryStockPolicy}
    model=models.get(kind)
    if model is None:raise HTTPException(422,"不支持的选项")
    query=select(model)
    if model==OrderItem:
        query=query.join(Order).where(Order.customer_id==customer_id,OrderItem.is_force_closed.is_(False))
        if q:query=query.where(or_(Order.customer_po.contains(q),OrderItem.snapshot_product_code.contains(q),OrderItem.snapshot_product_name.contains(q)))
    else:
        query=query.where(model.customer_id==customer_id)
        if model==Product:
            query=query.where(Product.is_active.is_(True),Product.deleted_at.is_(None))
            if q:query=query.where(or_(Product.product_code.contains(q),Product.customer_material_code.contains(q),Product.product_name.contains(q)))
        elif model==Order and q:query=query.where(or_(Order.customer_po.contains(q),Order.order_number.contains(q)))
        elif model==Delivery and q:query=query.where(Delivery.delivery_number.contains(q))
        elif model==Statement and q:query=query.where(Statement.statement_number.contains(q))
        elif model==InventoryStockPolicy:
            query=query.where(InventoryStockPolicy.active.is_(True))
            if q:query=query.where(InventoryStockPolicy.policy_name.contains(q))
    rows=db.scalars(query.order_by(model.id.desc()).offset((page-1)*100).limit(101)).all()
    items=[]
    for row in rows[:100]:
        if model==Product:label=f"{row.customer_material_code or row.product_code} · {row.product_name}"
        elif model==Order:label=f"{row.customer_po or row.order_number} · {row.status}"
        elif model==OrderItem:
            order=db.get(Order,row.order_id)
            label=f"{order.customer_po or order.order_number} · {row.snapshot_product_code} · {row.snapshot_product_name} · 订单 {row.quantity} / 已送 {row.delivered_quantity}"
        elif model==Delivery:label=f"{row.delivery_number} · {row.delivery_date} · {row.status}"
        elif model==Statement:
            from app.api.finance import _statement_for_user
            try:_statement_for_user(db,row.id,user)
            except HTTPException:continue
            label=f"{row.statement_number} · {row.confirmation_status}"
        else:label=row.policy_name
        items.append({"id":row.id,"label":label,"version":getattr(row,"version",None),"unit_price":getattr(row,"unit_price",None)})
    return {"items":items,"more":len(rows)>100}

@router.get("/prepare")
def prepare(action:str,customer_id:int,target_id:int,db:Session=Depends(get_db),user:User=Depends(get_current_user)):
    require_submit(user)
    if action not in FIELDS:raise HTTPException(422,"请选择可修改的资料")
    service.basis(db,action,target_id,customer_id,{},user)
    obj=db.get(service.specs(action)[1],target_id)
    material=db.get(Material,obj.material_id) if action=="product_update" and obj.material_id else None
    return {"fields":FIELDS[action],"payload":{key:getattr(obj,key) for key in FIELDS[action]},"labels":{"material_id":material.code if material else None},"expected_version":getattr(obj,"version",None)}

@router.get("")
def list_requests(status:str="pending",page:int=Query(1,ge=1),db:Session=Depends(get_db),user:User=Depends(get_current_user)):
    if user.role not in {"admin","boss"} and not has_permission(user,"business_requests.submit"): raise HTTPException(403,"没有申请查看权限")
    q=select(BusinessApproval)
    if user.role=="boss": q=q.where(BusinessApproval.action=="stock_replenishment")
    elif user.role!="admin": q=q.where(BusinessApproval.applicant_id==user.id)
    if not has_unrestricted_customer_access(user,db): q=q.where(BusinessApproval.customer_id.in_(customer_scope_ids(user,db)))
    if status: q=q.where(BusinessApproval.status==status)
    total=db.scalar(select(func.count()).select_from(q.subquery()))
    rows=db.scalars(q.order_by(BusinessApproval.id.desc()).offset((page-1)*20).limit(20)).all()
    return {"items":[service.view(row,db) for row in rows],"total":total,"page":page,"can_review":user.role in {"admin","boss"}}

@router.post("",status_code=201)
def submit_request(body:Submit,db:Session=Depends(get_db),user:User=Depends(get_current_user)):
    try:return service.submit(db,user,body)
    except ValidationError as error: raise HTTPException(422,"申请内容不完整或格式不正确") from error

@router.post("/{request_id}/review")
def review_request(request_id:int,body:Review,db:Session=Depends(get_db),user:User=Depends(get_current_user)):
    return service.review(db,user,request_id,body)

@router.post("/{request_id}/withdraw")
def withdraw(request_id:int,db:Session=Depends(get_db),user:User=Depends(get_current_user)):
    from sqlalchemy import update
    row=db.get(BusinessApproval,request_id)
    if row is None or row.applicant_id!=user.id:raise HTTPException(404,"申请不存在")
    require_customer_access(row.customer_id,user,db)
    changed=db.execute(update(BusinessApproval).where(BusinessApproval.id==row.id,BusinessApproval.status=="pending").values(status="withdrawn",version=BusinessApproval.version+1))
    if changed.rowcount!=1:raise HTTPException(409,"申请已处理")
    db.refresh(row);service.audit(db,row,user,"withdrawn");db.commit()
    return service.view(row,db)
