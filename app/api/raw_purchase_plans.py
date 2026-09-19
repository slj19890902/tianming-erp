from typing import Literal
from fastapi import APIRouter,Depends,Query
from pydantic import BaseModel,ConfigDict,Field
from sqlalchemy.orm import Session
from sqlalchemy import select
from app.api.deps import get_db,PermissionChecker,RoleChecker,require_customer_access
from app.models.user import User
from app.models.raw_purchase_plan import RawPurchasePlan
from app.services import raw_purchase_plans as service

router=APIRouter(prefix='/raw-plans',dependencies=[Depends(RoleChecker(['admin'])),Depends(PermissionChecker('requisition.execute'))])


class Choices(BaseModel):
    model_config=ConfigDict(extra='forbid')
    order_item_ids:list[int]=Field(min_length=1,max_length=100)


class Allocation(BaseModel):
    model_config=ConfigDict(extra='forbid')
    source_key:str=Field(min_length=1,max_length=100)
    yield_factor:int=Field(gt=0,le=100,strict=True)
    rotate:bool=False
    piece_flute_direction:Literal['length','width']


class Plan(Choices):
    length_mm:int=Field(gt=0,le=20000,strict=True)
    width_mm:int=Field(gt=0,le=20000,strict=True)
    quantity:int=Field(gt=0,le=10000000,strict=True)
    sheet_type:Literal['raw_board','net_sheet']='raw_board'
    flute_direction:Literal['length','width']
    trim_mm:int=Field(default=0,ge=0,le=1000,strict=True)
    kerf_mm:int=Field(default=0,ge=0,le=100,strict=True)
    allocations:list[Allocation]=Field(min_length=1,max_length=300)


class Confirm(BaseModel):
    model_config=ConfigDict(extra='forbid')
    plan:Plan
    reviewed_hash:str=Field(pattern='^[0-9a-f]{64}$')
    operation_key:str=Field(min_length=1,max_length=64)


@router.post('/choices')
def choices(payload:Choices,db:Session=Depends(get_db),user:User=Depends(RoleChecker(['admin']))):
    return service.choices(db,payload.order_item_ids,user)


@router.post('/preview')
def preview(payload:Plan,db:Session=Depends(get_db),user:User=Depends(RoleChecker(['admin']))):
    return service.preview(db,payload.model_dump(),user)


@router.post('')
def create(payload:Confirm,db:Session=Depends(get_db),user:User=Depends(RoleChecker(['admin']))):
    try:
        result=service.create(db,payload.plan.model_dump(),payload.reviewed_hash,payload.operation_key,user)
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise


@router.get('')
def listing(page:int=Query(1,ge=1,le=100000),db:Session=Depends(get_db),user:User=Depends(RoleChecker(['admin']))):
    rows=db.scalars(select(RawPurchasePlan).order_by(RawPurchasePlan.id.desc()).offset(max(page-1,0)*20).limit(20)).all()
    for row in rows: require_customer_access(row.customer_id,user,db)
    return {'items':[service.describe(db,row) for row in rows]}
