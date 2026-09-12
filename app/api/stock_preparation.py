from typing import Literal
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from app.api.deps import get_db, PermissionChecker, RoleChecker, has_unrestricted_customer_access, customer_scope_ids, require_customer_access
from app.models.user import User
from app.services import stock_preparation as service
from app.services.bom_transactions import atomic_bom
from app.services.warehouse_inventory import WarehouseInventoryError

router=APIRouter()


class Action(BaseModel):
    action: Literal['keep_raw','keep_semi','plan','complete','cancel']
    operation_key: str = Field(min_length=8,max_length=70)
    lot_version: int = Field(gt=0,strict=True)
    quantity: int = Field(default=0,ge=0,strict=True)
    job_id: int | None = None
    job_version: int | None = None
    actual_output: int = Field(default=0,ge=0,strict=True)
    location_id: int | None = None
    layout_version: int | None = None
    confirm_overproduction: bool = False


@router.get('/stock-preparation')
def get_rows(q:str=Query('',max_length=150), state:str='', page:int=Query(1,ge=1),page_size:int=Query(20,ge=1,le=100),
             db:Session=Depends(get_db),user:User=Depends(PermissionChecker('orders.view'))):
    scope=None if has_unrestricted_customer_access(user,db) else customer_scope_ids(user,db)
    rows=service.list_rows(db,scope=scope,query=q)
    counts={key:sum(row['status']==key for row in rows) for key in ['arrange','pending','waiting','keep','stock','history']}
    filtered=[row for row in rows if row['status']==state] if state else [row for row in rows if row['status']!='history']
    return dict(items=filtered[(page-1)*page_size:page*page_size],total=len(filtered),counts=counts,page=page,page_size=page_size)


@router.post('/stock-preparation/{receipt_id}/actions')
def post_action(receipt_id:int,body:Action,db:Session=Depends(get_db),user:User=Depends(RoleChecker(['admin','boss']))):
    try:
        _,item,_=service.source(db,receipt_id)
        require_customer_access(item.customer_id,user,db)
        with atomic_bom(db):
            result=service.mutate(db,receipt_id=receipt_id,payload=body.model_dump(),actor=user)
        db.commit()
        return result
    except WarehouseInventoryError as exc:
        db.rollback()
        raise HTTPException(exc.status_code,str(exc)) from exc
    except Exception:
        db.rollback()
        raise
