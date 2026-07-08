import base64
import re
import socket
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from io import BytesIO

import qrcode
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import RoleChecker, get_db
from app.core.security import create_tianhua_pick_token, decode_tianhua_pick_token
from app.models.tianhua_pre_delivery import (
    TianhuaPreDeliveryDraft,
    TianhuaPreDeliveryDraftItem,
    TianhuaPreDeliveryImportBatch,
    TianhuaPreDeliveryImportItem,
)
from app.models.order import OrderItem
from app.models.product import Product
from app.models.user import User
from app.services.tianhua_pre_delivery import STATUS_LABELS, batch_dict, create_batch, draft_dict, ensure_draft_delivery, save_draft

router=APIRouter()
mobile_router=APIRouter()
can_read=RoleChecker(["admin","finance","sales","workshop"])
can_operate=RoleChecker(["admin","sales"])


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


@router.post("/tianhua-preimport/upload",status_code=status.HTTP_201_CREATED)
async def upload(file:UploadFile=File(...),pre_delivery_date:date|None=Form(default=None),db:Session=Depends(get_db),user:User=Depends(can_operate)):
    filename=(file.filename or "").strip()
    if not any(filename.lower().endswith(s) for s in (".jpg",".jpeg",".png")): raise HTTPException(400,"仅支持 jpg、jpeg、png 图片")
    content=await file.read(12*1024*1024+1)
    if not content: raise HTTPException(400,"上传图片为空")
    if len(content)>12*1024*1024: raise HTTPException(413,"图片不能超过 12MB")
    try: return batch_dict(db,create_batch(db,content,filename,user.id,pre_delivery_date or date.today()+timedelta(days=1)))
    except ValueError as e: db.rollback(); raise HTTPException(400,str(e)) from e


@router.get("/tianhua-preimport/{batch_id}")
def get_batch(batch_id:int,db:Session=Depends(get_db),_user:User=Depends(can_read)):
    return batch_dict(db,_batch(db,batch_id))


def _save(batch_id,payload,db,user,update):
    try:
        draft=save_draft(db,_batch(db,batch_id),[x.model_dump() for x in payload.items],payload.remark,user.id,update)
        return draft_dict(db,draft)
    except RuntimeError as e: db.rollback(); raise HTTPException(409,str(e)) from e
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


def _plain_mm(value) -> str | None:
    if value is None:
        return None
    try:
        number=Decimal(str(value))
    except (InvalidOperation, ValueError):
        text=str(value).strip()
        return text or None
    if number==number.to_integral_value():
        return str(int(number))
    return format(number.normalize(),"f").rstrip("0").rstrip(".")


def _dimension_from_text(value:str|None) -> str | None:
    match=re.search(r"\d+(?:\.\d+)?\s*(?:[*xX×]\s*\d+(?:\.\d+)?){1,2}\s*(?:cm|mm)?",value or "")
    if not match:
        return None
    return re.sub(r"\s+","",match.group(0)).replace("*","×").replace("x","×").replace("X","×")


def _mobile_product_spec(order_item:OrderItem|None,product:Product|None,product_name:str|None) -> str:
    if order_item and (order_item.snapshot_spec or "").strip():
        return order_item.snapshot_spec.strip()
    if product:
        dims=[_plain_mm(product.length_mm),_plain_mm(product.width_mm),_plain_mm(product.height_mm)]
        if all(dims):
            return f"{dims[0]} × {dims[1]} × {dims[2]} mm"
        parsed=_dimension_from_text(product.product_name)
        if parsed:
            return parsed
    parsed=_dimension_from_text(product_name)
    return parsed or ""


def _mobile_items(db:Session,draft:TianhuaPreDeliveryDraft) -> list[dict]:
    rows=db.execute(
        select(TianhuaPreDeliveryDraftItem,TianhuaPreDeliveryImportItem)
        .join(TianhuaPreDeliveryImportItem,TianhuaPreDeliveryImportItem.id==TianhuaPreDeliveryDraftItem.import_item_id)
        .where(TianhuaPreDeliveryDraftItem.draft_id==draft.id)
        .order_by(TianhuaPreDeliveryDraftItem.row_no)
    ).all()
    order_item_ids=[draft_item.order_item_id for draft_item,_import_item in rows if draft_item.order_item_id]
    product_ids=[import_item.product_id for _draft_item,import_item in rows if import_item.product_id]
    order_items={item.id:item for item in db.scalars(select(OrderItem).where(OrderItem.id.in_(order_item_ids))).all()} if order_item_ids else {}
    products={product.id:product for product in db.scalars(select(Product).where(Product.id.in_(product_ids))).all()} if product_ids else {}
    return [{
        "item_id":draft_item.id,
        "delivery_item_id":draft_item.delivery_item_id,
        "stock_code":draft_item.stock_code,
        "product_name":import_item.product_name,
        "product_spec":_mobile_product_spec(order_items.get(draft_item.order_item_id),products.get(import_item.product_id),import_item.product_name),
        "order_no":draft_item.order_number,
        "customer_order_no":draft_item.customer_order_no,
        "suggested_qty":import_item.suggested_qty,
        "final_delivery_qty":draft_item.delivery_qty,
        "mobile_picked_qty":draft_item.mobile_picked_qty,
        "mobile_pick_status":draft_item.mobile_pick_status,
        "mobile_pick_note":draft_item.mobile_pick_note or "",
        "mobile_picked_at":draft_item.mobile_picked_at.isoformat() if draft_item.mobile_picked_at else None,
        "status":import_item.status,
        "status_label":STATUS_LABELS.get(import_item.status,import_item.status),
        "warning":import_item.warning or "",
    } for draft_item,import_item in rows]


@router.post("/tianhua-preimport/{batch_id}/mobile-token")
def create_mobile_token(batch_id:int,request:Request,db:Session=Depends(get_db),user:User=Depends(can_operate)):
    batch=_batch(db,batch_id)
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
    return {"token":token,"url":url,"expires_at":expires.isoformat(),"qr_data_url":f"data:image/png;base64,{base64.b64encode(buffer.getvalue()).decode('ascii')}"}


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
    item.mobile_picked_at=datetime.utcnow()
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
