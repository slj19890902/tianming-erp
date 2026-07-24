import base64
import socket
from datetime import date, datetime, timedelta, timezone
from io import BytesIO

import qrcode
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import PermissionChecker, get_db, require_customer_access
from app.api.deliveries import _inventory_sources_for_order_item
from app.core.time_contract import beijing_today, utc_naive_to_api, utc_now_naive
from app.core.security import create_tianhua_pick_token, decode_tianhua_pick_token
from app.models.order import OrderItem
from app.models.product import Product
from app.models.tianhua_pre_delivery import (
    TianhuaPreDeliveryDraft,
    TianhuaPreDeliveryDraftItem,
    TianhuaPreDeliveryImportBatch,
    TianhuaPreDeliveryImportItem,
)
from app.models.user import User
from app.services.tianhua_pre_delivery import STATUS_LABELS, batch_dict, create_batch, draft_dict, ensure_draft_delivery, save_draft
from app.services.delivery_numbering import DeliveryNumberingError
from app.services.secure_uploads import IMAGE_POLICY, UploadValidationError, read_validated_upload

router=APIRouter()
mobile_router=APIRouter()
can_read=PermissionChecker("deliveries.view")
can_operate=PermissionChecker("deliveries.execute")


class DraftLine(BaseModel):
    item_id:int|None=Field(default=None,ge=1)
    row_no:int=Field(ge=1)
    selected:bool=False
    final_delivery_qty:int|None=Field(default=None,ge=0)


class DraftRequest(BaseModel):
    items:list[DraftLine]
    remark:str|None=None


class MobilePickUpdate(BaseModel):
    token:str=Field(min_length=20)
    mobile_pick_status:str
    mobile_picked_qty:int|None=Field(default=None,ge=0)
    mobile_pick_note:str|None=Field(default=None,max_length=500)


def _batch(db,batch_id):
    value=db.get(TianhuaPreDeliveryImportBatch,batch_id)
    if value is None: raise HTTPException(404,"天华预送货识别批次不存在")
    return value


def _batch_for_user(db:Session,batch_id:int,user:User) -> TianhuaPreDeliveryImportBatch:
    batch=_batch(db,batch_id)
    require_customer_access(batch.customer_id,user,db)
    return batch


@router.post("/tianhua-preimport/upload",status_code=status.HTTP_201_CREATED)
async def upload(file:UploadFile=File(...),pre_delivery_date:date|None=Form(default=None),db:Session=Depends(get_db),user:User=Depends(can_operate)):
    try:
        validated=await read_validated_upload(file,IMAGE_POLICY)
    except UploadValidationError as error:
        raise HTTPException(400,str(error)) from error
    filename=validated.original_filename
    content=validated.content
    try:
        batch=create_batch(db,content,filename,user.id,pre_delivery_date or beijing_today()+timedelta(days=1))
        try:
            require_customer_access(batch.customer_id,user,db)
        except HTTPException:
            # create_batch commits internally; remove the just-created batch so
            # a scoped account cannot persist an out-of-scope import.
            db.delete(batch)
            db.commit()
            raise
        return batch_dict(db,batch)
    except ValueError as e: db.rollback(); raise HTTPException(400,str(e)) from e


@router.get("/tianhua-preimport/{batch_id}")
def get_batch(batch_id:int,db:Session=Depends(get_db),_user:User=Depends(can_read)):
    return batch_dict(db,_batch_for_user(db,batch_id,_user))


def _save(batch_id,payload,db,user,update):
    try:
        draft=save_draft(db,_batch_for_user(db,batch_id,user),[x.model_dump() for x in payload.items],payload.remark,user.id,update)
        return draft_dict(db,draft)
    except RuntimeError as e: db.rollback(); raise HTTPException(409,str(e)) from e
    except DeliveryNumberingError as e: db.rollback(); raise HTTPException(409,str(e)) from e
    except ValueError as e: db.rollback(); raise HTTPException(400,str(e)) from e


@router.post("/tianhua-preimport/{batch_id}/create-draft",status_code=status.HTTP_201_CREATED)
def create_draft(batch_id:int,payload:DraftRequest,db:Session=Depends(get_db),user:User=Depends(can_operate)):
    return _save(batch_id,payload,db,user,False)


@router.put("/tianhua-preimport/{batch_id}/update-draft")
def update_draft(batch_id:int,payload:DraftRequest,db:Session=Depends(get_db),user:User=Depends(can_operate)):
    return _save(batch_id,payload,db,user,True)


def _lan_ip() -> str:
    connection=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
    try:
        connection.connect(("8.8.8.8",80))
        return connection.getsockname()[0]
    except OSError:
        return socket.gethostbyname(socket.gethostname())
    finally:
        connection.close()


def _token_scope(token:str,db:Session) -> tuple[TianhuaPreDeliveryImportBatch,TianhuaPreDeliveryDraft]:
    try:
        batch_id,draft_id=decode_tianhua_pick_token(token)
    except ValueError as error:
        raise HTTPException(status_code=403,detail=str(error)) from error
    batch=db.get(TianhuaPreDeliveryImportBatch,batch_id)
    draft=db.get(TianhuaPreDeliveryDraft,draft_id)
    if batch is None or draft is None or draft.batch_id!=batch.id:
        raise HTTPException(status_code=403,detail="无权限访问该拿货单")
    return batch,draft


def _mobile_specification(order_item:OrderItem|None,product:Product|None) -> str:
    if order_item is not None and (order_item.snapshot_spec or "").strip():
        return order_item.snapshot_spec.strip()
    if product is None:
        return ""
    dimensions=(product.length_mm,product.width_mm,product.height_mm)
    if any(value is None for value in dimensions):
        return ""
    formatted=[]
    for value in dimensions:
        number=float(value)
        formatted.append(str(int(number)) if number.is_integer() else f"{number:g}")
    return f"{'×'.join(formatted)}mm"


def _mobile_finished_inventory_sources(
    db:Session,draft_item:TianhuaPreDeliveryDraftItem,order_item:OrderItem|None
) -> list[dict]:
    if order_item is None:
        return []
    return [
        source
        for source in _inventory_sources_for_order_item(
            db,
            order_item=order_item,
            planned_delivery_quantity=int(draft_item.delivery_qty or 0),
            delivery_item_id=draft_item.delivery_item_id,
            dispatched=False,
        )
        if source.get("source_type")=="finished"
    ]


def _mobile_items(db:Session,draft:TianhuaPreDeliveryDraft) -> list[dict]:
    rows=db.execute(
        select(TianhuaPreDeliveryDraftItem,TianhuaPreDeliveryImportItem)
        .join(TianhuaPreDeliveryImportItem,TianhuaPreDeliveryImportItem.id==TianhuaPreDeliveryDraftItem.import_item_id)
        .where(TianhuaPreDeliveryDraftItem.draft_id==draft.id)
        .order_by(TianhuaPreDeliveryDraftItem.row_no)
    ).all()
    items=[]
    for draft_item,import_item in rows:
        order_item=db.get(OrderItem,draft_item.order_item_id)
        product=db.get(Product,draft_item.product_id)
        items.append({
            "item_id":draft_item.id,
            "delivery_item_id":draft_item.delivery_item_id,
            "stock_code":draft_item.stock_code,
            "product_name":import_item.product_name,
            "specification":_mobile_specification(order_item,product),
            "finished_inventory_sources":_mobile_finished_inventory_sources(
                db,draft_item,order_item
            ),
            "order_no":draft_item.order_number,
            "customer_order_no":draft_item.customer_order_no,
            "suggested_qty":import_item.suggested_qty,
            "final_delivery_qty":draft_item.delivery_qty,
            "mobile_picked_qty":draft_item.mobile_picked_qty,
            "mobile_pick_status":draft_item.mobile_pick_status,
            "mobile_pick_note":draft_item.mobile_pick_note or "",
            "mobile_picked_at":(
                utc_naive_to_api(draft_item.mobile_picked_at)
                if draft_item.mobile_picked_at
                else None
            ),
            "status":import_item.status,
            "status_label":STATUS_LABELS.get(import_item.status,import_item.status),
            "warning":import_item.warning or "",
        })
    return items


@router.post("/tianhua-preimport/{batch_id}/mobile-token")
def create_mobile_token(batch_id:int,request:Request,db:Session=Depends(get_db),user:User=Depends(can_operate)):
    batch=_batch_for_user(db,batch_id,user)
    draft=db.scalar(select(TianhuaPreDeliveryDraft).where(TianhuaPreDeliveryDraft.batch_id==batch.id))
    if draft is None:
        raise HTTPException(status_code=409,detail="请先生成天华预送货草稿")
    try:
        ensure_draft_delivery(db,batch,draft,user.id)
        db.commit()
        db.refresh(draft)
    except ValueError as e:
        db.rollback()
        raise HTTPException(status_code=409,detail=str(e)) from e
    token,expires=create_tianhua_pick_token(batch.id,draft.id)
    port=request.url.port or 8000
    url=f"http://{_lan_ip()}:{port}/mobile/tianhua-pick?token={token}"
    image=qrcode.make(url)
    buffer=BytesIO()
    image.save(buffer,format="PNG")
    return {"token":token,"url":url,"expires_at":utc_naive_to_api(expires.astimezone(timezone.utc).replace(tzinfo=None)),"qr_data_url":f"data:image/png;base64,{base64.b64encode(buffer.getvalue()).decode('ascii')}"}


@mobile_router.get("/tianhua-pick")
def get_mobile_pick(token:str,db:Session=Depends(get_db)):
    batch,draft=_token_scope(token,db)
    return {"batch_id":batch.id,"draft_id":draft.id,"draft_number":draft.draft_number,"delivery_id":draft.delivery_id,"customer_name":batch.customer_name,"pre_delivery_date":batch.pre_delivery_date.isoformat() if batch.pre_delivery_date else None,"items":_mobile_items(db,draft)}


@mobile_router.put("/tianhua-pick/items/{item_id}")
def update_mobile_pick(item_id:int,payload:MobilePickUpdate,db:Session=Depends(get_db)):
    batch,draft=_token_scope(payload.token,db)
    item=db.get(TianhuaPreDeliveryDraftItem,item_id)
    if item is None or item.draft_id!=draft.id:
        raise HTTPException(status_code=403,detail="无权限修改该拿货明细")
    if payload.mobile_pick_status not in {"picked","no_stock","partial"}:
        raise HTTPException(status_code=400,detail="拿货状态无效")
    import_item=db.get(TianhuaPreDeliveryImportItem,item.import_item_id)
    if import_item is None:
        raise HTTPException(status_code=409,detail="拿货明细关联数据不存在")
    suggested=int(import_item.suggested_qty or item.delivery_qty or 0)
    qty=0 if payload.mobile_pick_status=="no_stock" else payload.mobile_picked_qty
    if qty is None:
        qty=int(item.delivery_qty)
    actual_status="no_stock" if payload.mobile_pick_status=="no_stock" else ("partial" if qty<suggested else "picked")
    item.mobile_pick_status=actual_status
    item.mobile_picked_qty=qty
    item.mobile_pick_note=(payload.mobile_pick_note or "").strip() or None
    item.mobile_picked_at=utc_now_naive()
    item.delivery_qty=qty
    import_item.final_delivery_qty=qty
    try:
        ensure_draft_delivery(db,batch,draft,None)
    except ValueError as e:
        db.rollback()
        raise HTTPException(status_code=409,detail=str(e)) from e
    db.commit()
    db.refresh(item)
    return {"ok":True,"item":next(value for value in _mobile_items(db,draft) if value["item_id"]==item.id)}
