from datetime import date
from typing import Literal
from fastapi import APIRouter,Depends,HTTPException,Query
from pydantic import BaseModel,Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.api.deps import get_db,PermissionChecker,customer_scope_ids,has_unrestricted_customer_access
from app.models.user import User
from app.models.scrap_production import ScrapProductionJob
from app.services import scrap_production as service
from app.services.warehouse_inventory import WarehouseInventoryError

router=APIRouter(prefix='/scrap-production')


class ScrapPayload(BaseModel):
    action:Literal['plan','finish_now','complete','cancel']='plan'
    product_id:int=Field(gt=0)
    order_item_id:int|None=Field(default=None,gt=0)
    quantity:int=Field(default=1,gt=0,le=10000000,strict=True)
    location_id:int|None=Field(default=None,gt=0)
    stock_date:date
    variant_id:str|None=Field(default=None,max_length=60)
    remarks:str=Field(default='',max_length=1000)
    preview_hash:str=Field(default='',max_length=64)
    operation_key:str=Field(min_length=8,max_length=60)
    job_id:int|None=Field(default=None,gt=0)
    job_version:int|None=Field(default=None,gt=0)


def public(value,user):
    from app.services.inventory_valuation import can_view_inventory_cost
    value=dict(value)
    if not can_view_inventory_cost(user):
        value.pop('labour',None);value.pop('labour_cost',None)
    return value


@router.get('')
def jobs(db:Session=Depends(get_db),user:User=Depends(PermissionChecker('orders.view'))):
    q=select(ScrapProductionJob).where(ScrapProductionJob.status=='pending')
    if not has_unrestricted_customer_access(user,db):
        q=q.where(ScrapProductionJob.customer_id.in_(customer_scope_ids(user,db)))
    return {'items':[public(service.row(db,j),user) for j in db.scalars(q.order_by(ScrapProductionJob.id).limit(200))]}


@router.get('/options')
def options(product_id:int=Query(gt=0),order_item_id:int|None=Query(default=None,gt=0),
            db:Session=Depends(get_db),user:User=Depends(PermissionChecker('orders.view'))):
    value=service.options(db,product_id,order_item_id,user)
    value['jobs']=[public(j,user) for j in value['jobs']]
    return value


@router.post('/preview')
def preview(payload:ScrapPayload,db:Session=Depends(get_db),user:User=Depends(PermissionChecker('warehouse.execute'))):
    try:return public(service.preview(db,payload.model_dump(mode='json'),user),user)
    except WarehouseInventoryError as e:raise HTTPException(e.status_code,str(e)) from e


@router.post('')
def mutate(payload:ScrapPayload,db:Session=Depends(get_db),user:User=Depends(PermissionChecker('warehouse.execute'))):
    try:
        result=service.mutate(db,payload.model_dump(mode='json'),user)
        db.commit();return public(result,user)
    except WarehouseInventoryError as e:
        db.rollback();raise HTTPException(e.status_code,str(e)) from e
    except Exception:
        db.rollback();raise
