from typing import Literal
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from app.api.deps import get_db,PermissionChecker
from app.models.user import User
from app.services import replenishment_production as service
from app.services.warehouse_inventory import WarehouseInventoryError

router=APIRouter(prefix='/stock-replenishment')

class Payload(BaseModel):
    action:Literal['plan','complete','cancel']='plan'
    operation_key:str=Field(min_length=8,max_length=60)
    source_fingerprint:str=''
    inventory_lot_id:int|None=Field(default=None,gt=0)
    lot_version:int|None=Field(default=None,gt=0)
    quantity:int=Field(default=0,ge=0,le=10000000,strict=True)
    job_id:int|None=Field(default=None,gt=0)
    job_version:int|None=Field(default=None,gt=0)
    actual_output:int=Field(default=0,ge=0,le=10000000,strict=True)
    location_id:int|None=Field(default=None,gt=0)
    layout_version:int|None=Field(default=None,gt=0)
    confirm_overproduction:bool=False
    preview_hash:str=''

class ResultPayload(BaseModel):
    original_request:Payload


def public(value,user):
    from app.services.inventory_valuation import can_view_inventory_cost
    if can_view_inventory_cost(user):return value
    if isinstance(value,list):return [public(v,user) for v in value]
    if not isinstance(value,dict):return value
    return {k:public(v,user) for k,v in value.items() if k not in ('unit_cost','labour','labour_cost')}


@router.get('/production-sources/jobs')
def jobs(include_completed:bool=False,db:Session=Depends(get_db),user:User=Depends(PermissionChecker('orders.view'))):
    return public({'items':service.jobs(db,user,include_completed)},user)

@router.get('/{item_id}/production-sources')
def options(item_id:int,db:Session=Depends(get_db),user:User=Depends(PermissionChecker('orders.view'))):
    try:return public(service.options(db,item_id,user),user)
    except WarehouseInventoryError as e:raise HTTPException(e.status_code,str(e)) from e

@router.post('/{item_id}/production-sources/preview')
def preview(item_id:int,payload:Payload,db:Session=Depends(get_db),user:User=Depends(PermissionChecker('warehouse.execute'))):
    try:return public(service.preview(db,item_id,payload.model_dump(mode='json'),user),user)
    except WarehouseInventoryError as e:raise HTTPException(e.status_code,str(e)) from e

@router.post('/{item_id}/production-sources/result')
def result(item_id:int,payload:ResultPayload,response:Response,db:Session=Depends(get_db),user:User=Depends(PermissionChecker('warehouse.execute'))):
    response.headers['Cache-Control']='no-store'
    try:
        value=service.checked_command(db,item_id,payload.original_request.model_dump(mode='json'),user)
        return public(dict(status='completed' if value else 'not_found',result=value),user)
    except WarehouseInventoryError as e:raise HTTPException(e.status_code,str(e)) from e

@router.post('/{item_id}/production-sources')
def mutate(item_id:int,payload:Payload,db:Session=Depends(get_db),user:User=Depends(PermissionChecker('warehouse.execute'))):
    try:
        result=service.mutate(db,item_id,payload.model_dump(mode='json'),user)
        db.commit();return public(result,user)
    except WarehouseInventoryError as e:
        db.rollback();raise HTTPException(e.status_code,str(e)) from e
    except Exception:
        db.rollback();raise
