from fastapi import APIRouter,Depends,HTTPException,Query
from pydantic import BaseModel,ConfigDict,Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.api.deps import get_db,PermissionChecker,RoleChecker,require_customer_access
from app.models.user import User
from app.models.delivery_backlog import DeliveryBacklog,DeliveryBacklogSource
from app.models.tianhua_pre_delivery import TianhuaPreDeliveryImportItem,TianhuaPreDeliveryImportBatch,TianhuaPreDeliveryDraft,TianhuaPreDeliveryDraftItem
from app.services import delivery_backlogs as service

router=APIRouter(prefix='/tianhua-backlogs')


class Defer(BaseModel):
    model_config=ConfigDict(extra='forbid')
    quantity:int=Field(gt=0,le=10000000,strict=True)
    reason:str=Field(min_length=1,max_length=500)


class Suggest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    customer_id:int=Field(gt=0)
    product_id:int=Field(gt=0)
    quantity:int=Field(gt=0,le=10000000,strict=True)
    customer_order_no:str|None=Field(default=None,max_length=150)


class Cancel(BaseModel):
    model_config=ConfigDict(extra='forbid')
    expected_version:int=Field(gt=0)
    reason:str=Field(min_length=1,max_length=500)


@router.get('/list')
def listing(page:int=Query(1,ge=1,le=100000),customer_id:int|None=None,product_id:int|None=None,history:bool=False,
            db:Session=Depends(get_db),user:User=Depends(PermissionChecker('deliveries.view'))):
    return service.listing(db,user,page=page,customer_id=customer_id,product_id=product_id,history=history)


@router.post('/suggest')
def suggest(payload:Suggest,db:Session=Depends(get_db),user:User=Depends(PermissionChecker('deliveries.view'))):
    return service.suggestions(db,user,**payload.model_dump())


@router.post('/defer/{import_item_id}')
def defer(import_item_id:int,payload:Defer,db:Session=Depends(get_db),user:User=Depends(PermissionChecker('deliveries.execute'))):
    from app.services.tianhua_pre_delivery import ensure_draft_delivery
    notice=db.get(TianhuaPreDeliveryImportItem,import_item_id)
    if not notice:raise HTTPException(404,'预送货通知明细不存在')
    batch=db.get(TianhuaPreDeliveryImportBatch,notice.batch_id);require_customer_access(batch.customer_id,user,db)
    try:
        previous=db.scalar(select(DeliveryBacklogSource).where(DeliveryBacklogSource.import_item_id==notice.id))
        if previous:
            row=service.defer(db,notice,quantity=payload.quantity,reason=payload.reason,actor=user)
            result=service.describe(db,row);db.commit();return result
        row=service.defer(db,notice,quantity=payload.quantity,reason=payload.reason,actor=user)
        draft=db.scalar(select(TianhuaPreDeliveryDraft).where(TianhuaPreDeliveryDraft.batch_id==batch.id))
        if draft:
            line=db.scalar(select(TianhuaPreDeliveryDraftItem).where(TianhuaPreDeliveryDraftItem.draft_id==draft.id,TianhuaPreDeliveryDraftItem.import_item_id==notice.id))
            if line:line.delivery_qty=0;line.mobile_pick_status='no_stock';line.mobile_picked_qty=0
            ensure_draft_delivery(db,batch,draft,user.id)
        notice.selected=False;notice.final_delivery_qty=0
        result=service.describe(db,row);db.commit();return result
    except Exception:db.rollback();raise


@router.post('/{backlog_id}/cancel',dependencies=[Depends(PermissionChecker('deliveries.execute'))])
def cancel(backlog_id:int,payload:Cancel,db:Session=Depends(get_db),user:User=Depends(RoleChecker(['admin']))):
    row=db.get(DeliveryBacklog,backlog_id)
    if not row:raise HTTPException(404,'待补送记录不存在')
    try:
        result=service.cancel(db,row,user,**payload.model_dump());db.commit();return result
    except Exception:db.rollback();raise
