from typing import Literal
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.api.deps import get_db, PermissionChecker, RoleChecker, has_unrestricted_customer_access, customer_scope_ids, require_customer_access
from app.models.user import User
from app.services import stock_preparation as service
from app.services.bom_transactions import atomic_bom
from app.services.warehouse_inventory import WarehouseInventoryError
from app.services import stock_preparation_groups as group_service

router=APIRouter()


class ReversalJob(BaseModel):
    job_id: int = Field(gt=0,strict=True)
    job_version: int = Field(gt=0,strict=True)
    source_version: int = Field(gt=0,strict=True)
    output_version: int = Field(gt=0,strict=True)


class Reversal(BaseModel):
    operation_key: str = Field(min_length=8,max_length=60)
    confirm_unused: bool = False
    jobs: list[ReversalJob] = Field(min_length=1,max_length=1000)


@router.get('/stock-preparation/history/{key}')
def get_preparation_trace(key:str,db:Session=Depends(get_db),user:User=Depends(PermissionChecker('orders.view'))):
    from app.services.stock_preparation_history import completed_groups
    jobs=completed_groups(db).get(key)
    if not jobs:
        raise HTTPException(404,'备库完工不存在')
    for job in jobs:
        _,item,_=service.source(db,job.receipt_item_id)
        require_customer_access(item.customer_id,user,db)
    ids={j.receipt_item_id for j in jobs}
    scope=None if has_unrestricted_customer_access(user,db) else customer_scope_ids(user,db)
    return {'items':[r for r in service.list_rows(db,scope=scope) if r['receipt_item_id'] in ids]}


@router.post('/stock-preparation/completions/{key}/revert')
def reverse_preparation(key:str,body:Reversal,db:Session=Depends(get_db),user:User=Depends(RoleChecker(['admin']))):
    from app.services.stock_preparation_history import completed_groups,reverse
    try:
        jobs=completed_groups(db).get(key)
        if not jobs:
            raise HTTPException(404,'备库完工不存在')
        for job in jobs:
            _,item,_=service.source(db,job.receipt_item_id)
            require_customer_access(item.customer_id,user,db)
        with atomic_bom(db):
            result=reverse(db,key=key,payload=body.model_dump(),actor=user)
        db.commit()
        return result
    except WarehouseInventoryError as exc:
        db.rollback()
        raise HTTPException(exc.status_code,str(exc)) from exc
    except Exception:
        db.rollback()
        raise


class Action(BaseModel):
    action: Literal['keep_raw','keep_semi','plan','complete','cancel','store_output']
    operation_key: str = Field(min_length=8,max_length=70)
    lot_version: int = Field(gt=0,strict=True)
    quantity: int = Field(default=0,ge=0,strict=True)
    job_id: int | None = None
    job_version: int | None = None
    actual_output: int = Field(default=0,ge=0,strict=True)
    location_id: int | None = None
    layout_version: int | None = None
    confirm_overproduction: bool = False
    output_kind: Literal['finished','semi'] = 'finished'
    output_version: int = Field(default=0,ge=0,strict=True)


class GroupJobAction(BaseModel):
    job_id: int = Field(gt=0,strict=True)
    job_version: int = Field(gt=0,strict=True)
    lot_version: int = Field(gt=0,strict=True)
    actual_output: int = Field(default=0,ge=0,strict=True)
    output_version: int = Field(default=0,ge=0,strict=True)
    location_id: int | None = None
    layout_version: int | None = None


class AssemblySource(BaseModel):
    lot_id: int = Field(gt=0,strict=True)
    version: int = Field(gt=0,strict=True)


class GroupAction(BaseModel):
    action: Literal['plan','complete','cancel','dispose','assemble','unassemble','store_outputs']
    disposition: Literal['finished','semi'] = 'finished'
    operation_key: str = Field(min_length=8,max_length=70)
    parent_id: int = Field(gt=0,strict=True)
    sets: int = Field(default=1,gt=0,le=10000000,strict=True)
    basis_hash: str = ''
    group_key: str = ''
    jobs: list[GroupJobAction] = Field(default_factory=list,max_length=1000)
    location_id: int | None = None
    layout_version: int | None = None
    confirm_overproduction: bool = False
    assembly_key: str = ''
    output_version: int = Field(default=0,ge=0,strict=True)
    sources: list[AssemblySource] = Field(default_factory=list,max_length=1000)
    confirm_unused: bool = False


@router.get('/stock-preparation/locations')
def get_stock_locations(db:Session=Depends(get_db),user:User=Depends(PermissionChecker('orders.view'))):
    from app.services.production_workflow import list_temporary_locations
    return {'items':list_temporary_locations(db,stock_materials=True)}


@router.get('/stock-preparation/groups')
def get_groups(db:Session=Depends(get_db),user:User=Depends(PermissionChecker('orders.view'))):
    scope=None if has_unrestricted_customer_access(user,db) else customer_scope_ids(user,db)
    return group_service.groups(db,service.list_rows(db,scope=scope))


@router.get('/stock-preparation/groups/{parent_id}/preview')
def preview_group(parent_id:int,sets:int=Query(1,gt=0,le=10000000),db:Session=Depends(get_db),user:User=Depends(PermissionChecker('orders.view'))):
    try:
        recipe=group_service.recipe(db,parent_id)
        require_customer_access(recipe['customer_id'],user,db)
        return group_service.preview(db,parent_id,sets)
    except WarehouseInventoryError as exc:
        raise HTTPException(exc.status_code,str(exc)) from exc


@router.post('/stock-preparation/group-actions')
def post_group_action(body:GroupAction,db:Session=Depends(get_db),user:User=Depends(RoleChecker(['admin','boss']))):
    try:
        if body.action=='unassemble' and user.role!='admin':
            raise HTTPException(403,'仅管理员可撤销组装')
        # Completion uses frozen product/customer identity even after later BOM edits.
        from app.models.product import Product
        parent=db.get(Product,body.parent_id)
        if not parent:
            raise HTTPException(404,'组合产品不存在')
        require_customer_access(parent.customer_id,user,db)
        if body.action!='plan':
            from app.models.stock_preparation import StockPreparationJob
            import json
            for job in db.scalars(select(StockPreparationJob)):
                group=json.loads(job.product_snapshot).get('preparation_group') or {}
                if group.get('key')==body.group_key:
                    if group['recipe']['parent_id']!=body.parent_id:
                        raise HTTPException(409,'组合产品与任务不一致')
                    _,item,_=service.source(db,job.receipt_item_id)
                    require_customer_access(item.customer_id,user,db)
        with atomic_bom(db):
            result=group_service.mutate_group(db,body.model_dump(),user)
        db.commit()
        return result
    except WarehouseInventoryError as exc:
        db.rollback()
        raise HTTPException(exc.status_code,str(exc)) from exc
    except Exception:
        db.rollback()
        raise


@router.get('/stock-preparation')
def get_rows(q:str=Query('',max_length=150), state:str='', workspace:bool=False, page:int=Query(1,ge=1),page_size:int=Query(20,ge=1,le=100),
             db:Session=Depends(get_db),user:User=Depends(PermissionChecker('orders.view'))):
    scope=None if has_unrestricted_customer_access(user,db) else customer_scope_ids(user,db)
    rows=service.list_rows(db,scope=scope,query='' if workspace else q)
    counts={key:sum(row['status']==key for row in rows) for key in ['arrange','pending','waiting','keep','stock','history']}
    filtered=group_service.workspace_rows(db,rows,state) if workspace else ([row for row in rows if row['status']==state] if state else [row for row in rows if row['status']!='history'])
    if workspace and q.strip():
        filtered=[row for row in filtered if q.strip().casefold() in group_service.encode(row).casefold()]
    pending_keys=set()
    for row in rows:
        for job in row['jobs']:
            if job['status']=='pending':
                group=job['product'].get('preparation_group')
                pending_keys.add('group:'+group['key'] if group else 'job:'+str(job['id']))
    return dict(items=filtered[(page-1)*page_size:page*page_size],total=len(filtered),counts=counts,page=page,page_size=page_size,workspace_pending_count=len(pending_keys))


@router.post('/stock-preparation/{receipt_id}/actions')
def post_action(receipt_id:int,body:Action,db:Session=Depends(get_db),user:User=Depends(RoleChecker(['admin','boss']))):
    try:
        _,item,_=service.source(db,receipt_id)
        require_customer_access(item.customer_id,user,db)
        with atomic_bom(db):
            result=service.mutate(db,receipt_id=receipt_id,payload=body.model_dump(),actor=user,output_kind=body.output_kind)
        db.commit()
        return result
    except WarehouseInventoryError as exc:
        db.rollback()
        raise HTTPException(exc.status_code,str(exc)) from exc
    except Exception:
        db.rollback()
        raise
