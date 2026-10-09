from typing import Literal
from fastapi import APIRouter, Depends, HTTPException, Query, Response, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.api.deps import get_db, get_current_user, PermissionChecker, RoleChecker, has_unrestricted_customer_access, customer_scope_ids, require_customer_access
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
    action: Literal['keep_raw','keep_semi','plan','complete','process','cancel','store_output']
    operation_key: str = Field(min_length=8,max_length=70)
    lot_version: int = Field(gt=0,strict=True)
    quantity: int = Field(default=0,ge=0,strict=True)
    job_id: int | None = None
    job_version: int | None = None
    actual_output: int = Field(default=0,ge=0,strict=True)
    actual_input_quantity: int | None = Field(default=None,gt=0,strict=True)
    location_id: int | None = None
    layout_version: int | None = None
    confirm_overproduction: bool = False
    output_kind: Literal['finished','semi'] = 'finished'
    output_version: int = Field(default=0,ge=0,strict=True)
    expected_actor_id: int | None = Field(default=None,gt=0,strict=True,exclude=True)


class CompletionResultRequest(BaseModel):
    operation_key: str = Field(min_length=8,max_length=70)
    original_request: Action
    expected_actor_id: int | None = Field(default=None,gt=0,strict=True)


def _completion_headers():
    return {'Cache-Control':'no-store','X-Production-Completion-Preserve':'1'}


def _completion_actor(expected, user):
    if expected is not None and expected != user.id:
        raise HTTPException(409,'当前登录账号与原请求账号不一致，请保留原请求核对',
            headers=dict(_completion_headers(), **{'X-Production-Completion-Actor-Mismatch':'1'}))


def _completion_user(request:Request,db:Session=Depends(get_db)):
    # Delegate unchanged authentication and denial auditing, adding cache metadata.
    try:
        user=get_current_user(request,db)
        return RoleChecker(['admin','boss'])(request,user,db)
    except HTTPException as exc:
        exc.headers=dict(exc.headers or {},**_completion_headers())
        raise


@router.post('/stock-preparation/{receipt_id}/completion-result')
def completion_result(receipt_id:int,body:CompletionResultRequest,response:Response,
                      db:Session=Depends(get_db),user:User=Depends(_completion_user)):
    from app.services import stock_preparation_completion_recovery as recovery
    from app.models.stock_preparation import StockPreparationCommand
    response.headers['Cache-Control']='no-store'
    try:
        _completion_actor(body.expected_actor_id,user)
        _completion_actor(body.original_request.expected_actor_id,user)
        if (body.original_request.action != 'complete' or body.original_request.actual_input_quantity is None
                or body.operation_key != body.original_request.operation_key):
            service.fail('请使用完整原加工请求核对，操作标识必须一致')
        with db.no_autoflush:
            customer_id,_=recovery.identity(db,receipt_id)
            require_customer_access(customer_id,user,db)
            command=db.get(StockPreparationCommand,body.operation_key)
            if command is None:
                return recovery.envelope(None,None,body.operation_key,user.id)
            payload=recovery.normalized_payload(db,body.original_request,command)
            result,proof=recovery.checked_result(command,payload,user.id,receipt_id)
            if proof:
                require_customer_access(proof['customer_id'],user,db)
            return recovery.envelope(result,proof,body.operation_key,user.id)
    except WarehouseInventoryError as exc:
        raise HTTPException(exc.status_code,str(exc),headers=_completion_headers()) from exc
    except HTTPException as exc:
        exc.headers=dict(exc.headers or {},**_completion_headers())
        raise


class GroupJobAction(BaseModel):
    job_id: int = Field(gt=0,strict=True)
    job_version: int = Field(gt=0,strict=True)
    lot_version: int = Field(gt=0,strict=True)
    actual_output: int = Field(default=0,ge=0,strict=True)
    output_version: int = Field(default=0,ge=0,strict=True)
    location_id: int | None = None
    lot_id: int | None = Field(default=None,gt=0,strict=True)
    layout_version: int | None = None


class AssemblySource(BaseModel):
    lot_id: int = Field(gt=0,strict=True)
    version: int = Field(gt=0,strict=True)


class GroupAction(BaseModel):
    action: Literal['plan','complete','cancel','dispose','assemble','assemble_stock','unassemble','store_outputs']
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
        if body.action not in {'plan','assemble_stock'}:
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
            if body.action == 'assemble_stock':
                from app.services.stock_preparation_assembly import assemble_stock
                result=assemble_stock(db,body.model_dump(),user)
            else:
                legacy_payload=body.model_dump()
                for job in legacy_payload['jobs']:
                    if job.get('lot_id') is None:
                        job.pop('lot_id',None)
                result=group_service.mutate_group(db,legacy_payload,user)
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
    from app.services.stock_preparation_read import projection
    with projection(db,scope) as cache:
        rows=service.list_rows(db,scope=scope,query='' if workspace else q,include_movements=bool(q.strip()))
        counts={key:sum(row['status']==key for row in rows) for key in ['arrange','pending','waiting','keep','stock','history']}
        filtered=group_service.workspace_rows(db,rows,state) if workspace else ([row for row in rows if row['status']==state] if state else [row for row in rows if row['status']!='history'])
        if workspace and q.strip():
            filtered=[row for row in filtered if q.strip().casefold() in group_service.encode(row).casefold()]
        pending_keys=set()
        for row in rows:
            if row['status']=='arrange' and row['can_plan'] and row['available']>0:
                pending_keys.add('legacy:'+str(row['receipt_item_id']))
            for job in row['jobs']:
                if job['status']=='pending':
                    group=job['product'].get('preparation_group')
                    pending_keys.add('group:'+group['key'] if group else 'job:'+str(job['id']))
        selected=filtered[(page-1)*page_size:page*page_size]
        if not q.strip():cache.attach_movements(selected)
        return dict(items=selected,total=len(filtered),counts=counts,page=page,page_size=page_size,workspace_pending_count=len(pending_keys))


@router.post('/stock-preparation/{receipt_id}/actions')
def post_action(receipt_id:int,body:Action,response:Response,db:Session=Depends(get_db),user:User=Depends(_completion_user)):
    from app.services import stock_preparation_completion_recovery as recovery
    from app.models.stock_preparation import StockPreparationCommand
    single=body.action=='complete' and body.actual_input_quantity is not None
    entered=False
    existed=True
    commit_started=False
    if single:
        response.headers['Cache-Control']='no-store'
    try:
        _completion_actor(body.expected_actor_id,user)
        _,item,_=service.source(db,receipt_id)
        require_customer_access(item.customer_id,user,db)
        output_kind=body.output_kind
        if 'output_kind' not in body.model_fields_set:
            from app.models.stock_preparation import StockPreparationJob
            import json
            job=db.get(StockPreparationJob,body.job_id) if body.job_id else None
            if body.action=='process' or (job and json.loads(job.product_snapshot).get('auto_planned')):
                output_kind='semi'
        with atomic_bom(db):
            if single:
                entered=True
                existed=db.get(StockPreparationCommand,body.operation_key) is not None
            if body.action == 'process' or (body.action == 'complete' and body.actual_input_quantity is not None):
                from app.services.stock_preparation_processing import process
                payload=dict(body.model_dump(),output_kind=output_kind)
                result=process(db,receipt_id,payload,user,**({'result_builder':recovery.build_receipt} if single else {}))
                if single:
                    command=db.get(StockPreparationCommand,body.operation_key)
                    result,proof=recovery.checked_result(command,payload,user.id,receipt_id)
                    if proof:
                        require_customer_access(proof['customer_id'],user,db)
                    result=dict(result,completion_receipt=proof,current_actor_id=user.id,
                        proof_status='complete' if proof else 'legacy_trace')
            else:
                legacy_payload=body.model_dump()
                legacy_payload.pop('actual_input_quantity',None)
                legacy_payload['output_kind']=output_kind
                result=service.mutate(db,receipt_id=receipt_id,payload=legacy_payload,actor=user,output_kind=output_kind)
                if body.action=='complete':
                    result.update(completed_job_id=result['job_id'],continuation_job_id=None,remaining_input_quantity=0)
        commit_started=True
        db.commit()
        return result
    except WarehouseInventoryError as exc:
        db.rollback()
        headers=_completion_headers() if single else None
        if single and entered and not existed and not commit_started:
            headers={'Cache-Control':'no-store','X-Production-Completion-Rejected':'1'}
        raise HTTPException(exc.status_code,str(exc),headers=headers) from exc
    except HTTPException as exc:
        db.rollback()
        if single:
            exc.headers=dict(exc.headers or {},**_completion_headers())
        raise
    except Exception:
        db.rollback()
        if single:
            raise HTTPException(500,'加工结果尚未确认，请保留原请求并核对',headers=_completion_headers())
        raise


@router.get('/stock-preparation/assembly/{parent_id}/preview')
def preview_stock_assembly(parent_id:int,sets:int=Query(1,gt=0,le=10000000),db:Session=Depends(get_db),user:User=Depends(PermissionChecker('orders.view'))):
    from app.models.product import Product
    from app.services.stock_preparation_assembly import preview
    parent=db.get(Product,parent_id)
    if not parent:
        raise HTTPException(404,'组合产品不存在')
    require_customer_access(parent.customer_id,user,db)
    try:
        return preview(db,parent_id,sets)
    except WarehouseInventoryError as exc:
        raise HTTPException(exc.status_code,str(exc)) from exc
