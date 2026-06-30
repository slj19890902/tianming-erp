from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import cv2
import numpy as np
from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from app.models.customer import Customer
from app.models.delivery import Delivery, DeliveryItem
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.tianhua_pre_delivery import TianhuaPreDeliveryDraft, TianhuaPreDeliveryDraftItem, TianhuaPreDeliveryImportBatch, TianhuaPreDeliveryImportItem

STATUS_LABELS = {"ok":"可送货","duplicate_warning":"疑似重复","qty_mismatch":"数量不一致","stock_shortage":"库存不足","not_matched":"未匹配","ocr_failed":"识别失败"}
GENERATABLE = {"ok","duplicate_warning","qty_mismatch","stock_shortage"}
TEMPLATES = [
("0","1e4d147145145e"),("0","1e4f1c71c714de"),("0","1e8a28e38e289e"),("1","04314104104104"),("1","0431c104104104"),("1","0639e186186186"),
("2","1e4c104210c43f"),("2","1ecc10c210843f"),("3","1c88208c08389c"),("3","1cc8208e0c389e"),("3","1e48308e041c5e"),("3","3c98218c0828bc"),
("4","0218629a4bf082"),("4","0218e2924bf082"),("4","0410c51493f104"),("5","1e430f220c289c"),("5","1f410791041c5e"),("5","3e820f220828bc"),
("6","0e450fb1c7144e"),("6","0e4f0fb186144e"),("7","3f084108218610"),("7","3f0c2104308208"),("7","3f0c2184308208"),
("8","0e4d34ce4f145e"),("8","1c8a289c8a389e"),("9","1c8a28a67828bc"),("9","1e471c537c149e")]


@dataclass
class RecognizedRow:
    row_no: int
    raw_text: str
    stock_code: str | None
    image_qty: int | None


def _norm(a):
    h,w=a.shape; nw=max(1,min(6,round(w*9/max(h,1))))
    b=cv2.resize(a,(nw,9),interpolation=cv2.INTER_AREA)
    out=np.zeros((9,6),dtype=np.uint8); x=(6-nw)//2; out[:,x:x+nw]=b
    return out.reshape(-1)>=90


def _groups(gray):
    _,binary=cv2.threshold(gray,180,255,cv2.THRESH_BINARY_INV)
    count,_,stats,_=cv2.connectedComponentsWithStats(binary); glyphs=[]
    for i in range(1,count):
        x,y,w,h,_=stats[i]
        if not (7<=h<=max(18,gray.shape[0]//20) and 2<=w<=h*2): continue
        parts=max(1,round(w/max(h*.67,1))) if w>h*.9 else 1
        for p in range(parts):
            x1=x+round(p*w/parts); x2=x+round((p+1)*w/parts)
            glyphs.append({"x":x1,"y":y+h/2,"h":h,"bits":_norm(binary[y:y+h,x1:x2])})
    rows=[]
    for g in sorted(glyphs,key=lambda z:(z["y"],z["x"])):
        row=next((r for r in rows if abs(r["y"]-g["y"])<=max(3,r["h"]*.4)),None)
        if row is None: row={"y":g["y"],"h":g["h"],"glyphs":[]}; rows.append(row)
        row["glyphs"].append(g)
    templates=[(d,np.array([c=="1" for c in bin(int(v,16))[2:].zfill(56)[-54:]])) for d,v in TEMPLATES]
    for row in rows:
        row["text"]="".join(min(templates,key=lambda t:np.count_nonzero(g["bits"]!=t[1]))[0] for g in sorted(row["glyphs"],key=lambda z:z["x"]))
    return rows


def recognize_tianhua_image(content: bytes) -> list[RecognizedRow]:
    image=cv2.imdecode(np.frombuffer(content,np.uint8),cv2.IMREAD_COLOR)
    if image is None or image.shape[1]<180: raise ValueError("图片无法读取或宽度过小")
    gray=cv2.cvtColor(image,cv2.COLOR_BGR2GRAY); edge=min(90,image.shape[1]//3)
    left,right=_groups(gray[:,:edge]),_groups(gray[:,image.shape[1]-edge:])
    result=[]
    for no,lrow in enumerate(left,1):
        if len(lrow["text"])<4: continue
        rrow=min(right,key=lambda r:abs(r["y"]-lrow["y"]),default=None)
        qty=rrow["text"] if rrow and abs(rrow["y"]-lrow["y"])<=max(5,lrow["h"]) else ""
        result.append(RecognizedRow(no,f'{lrow["text"]} {qty}'.strip(),lrow["text"] if re.fullmatch(r"\d{8}",lrow["text"]) else None,int(qty) if re.fullmatch(r"\d+",qty) else None))
    if not result: raise ValueError("未识别到天华表格行")
    return result


def _products(db, code):
    return list(db.scalars(select(Product).join(Customer,Customer.id==Product.customer_id).where(
        or_(Customer.name.contains("天华"),Customer.customer_code=="天华"),Product.is_active.is_(True),Product.deleted_at.is_(None),
        or_(Product.product_code==code,Product.customer_material_code==code,Product.legacy_customer_material_code==code,Product.product_code.like(f"{code}/%"),Product.legacy_customer_material_code.like(f"{code}/%"))
    ).order_by(Product.customer_id.desc(),Product.id)).all())


def preprocess_row(db: Session,row: RecognizedRow) -> dict:
    data=dict(row_no=row.row_no,raw_text=row.raw_text,stock_code=row.stock_code,image_qty=row.image_qty,product_id=None,product_name=None,order_item_id=None,order_number=None,system_pending_qty=None,available_qty=None,suggested_qty=row.image_qty,final_delivery_qty=row.image_qty,status="ocr_failed",warning="未能同时识别 8 位存货编码和最右侧整数数量。",selected=False)
    if row.stock_code is None or row.image_qty is None: return data
    products=_products(db,row.stock_code)
    if not products: data.update(status="not_matched",warning="未找到该存货编码对应的天华产品。"); return data
    pmap={p.id:p for p in products}
    candidates=[]
    for oi,o in db.execute(select(OrderItem,Order).join(Order,Order.id==OrderItem.order_id).where(OrderItem.product_id.in_(pmap),OrderItem.delivered_quantity<OrderItem.quantity,OrderItem.is_force_closed.is_(False),Order.status.not_in(("cancelled","dead","closed","archived","completed")))):
        pending=int(oi.quantity-oi.delivered_quantity); available=pending if oi.material_status=="received" else 0
        candidates.append((oi,o,pmap[oi.product_id],pending,available))
    candidates.sort(key=lambda x:(0 if x[4]>0 else 1,0 if x[3]==row.image_qty else 1,abs(x[3]-row.image_qty),-x[1].order_date.toordinal(),-x[0].id))
    if not candidates: data.update(product_id=products[0].id,product_name=products[0].product_name,status="not_matched",warning="已匹配天华产品，但没有可用的未送订单。"); return data
    oi,o,p,pending,available=candidates[0]
    data.update(product_id=p.id,product_name=p.product_name,order_item_id=oi.id,order_number=o.order_number,system_pending_qty=pending,available_qty=available)
    dup=db.execute(select(Delivery.delivery_number,Delivery.delivery_date).join(DeliveryItem,DeliveryItem.delivery_id==Delivery.id).join(OrderItem,OrderItem.id==DeliveryItem.order_item_id).where(Delivery.customer_id==o.customer_id,Delivery.status=="dispatched",Delivery.delivery_date>=date.today()-timedelta(days=7),OrderItem.product_id==p.id,DeliveryItem.delivered_quantity==row.image_qty).limit(1)).one_or_none()
    if available<row.image_qty: data.update(status="stock_shortage",warning=f"当前可送数量 {available}，小于图片数量 {row.image_qty}；订单可能尚未入库或库存不足。")
    elif dup: data.update(status="duplicate_warning",warning=f"相同编码和数量最近 7 天已送过：{dup.delivery_number}（{dup.delivery_date}）。")
    elif pending!=row.image_qty: data.update(status="qty_mismatch",warning=f"客户图片数量 {row.image_qty}，系统未送数量 {pending}"+("，差异仅 1 个，请重点确认。" if abs(pending-row.image_qty)==1 else "。"))
    else: data.update(status="ok",warning="",selected=True)
    return data


def item_dict(i):
    return {"item_id":i.id,"row_no":i.row_no,"raw_text":i.raw_text,"stock_code":i.stock_code,"image_qty":i.image_qty,"product_id":i.product_id,"product_name":i.product_name,"order_item_id":i.order_item_id,"order_no":i.order_number,"system_pending_qty":i.system_pending_qty,"available_qty":i.available_qty,"suggested_qty":i.suggested_qty,"final_delivery_qty":i.final_delivery_qty,"status":i.status,"status_label":STATUS_LABELS.get(i.status,i.status),"warning":i.warning or "","selected":i.selected}


def draft_dict(db,draft):
    items=db.scalars(select(TianhuaPreDeliveryDraftItem).where(TianhuaPreDeliveryDraftItem.draft_id==draft.id).order_by(TianhuaPreDeliveryDraftItem.row_no)).all()
    return {"draft_id":draft.id,"draft_number":draft.draft_number,"batch_id":draft.batch_id,"status":draft.status,"remark":draft.remark,"items":[{"row_no":i.row_no,"stock_code":i.stock_code,"order_item_id":i.order_item_id,"delivery_qty":i.delivery_qty,"warning":i.warning or ""} for i in items]}


def batch_dict(db,batch):
    items=db.scalars(select(TianhuaPreDeliveryImportItem).where(TianhuaPreDeliveryImportItem.batch_id==batch.id).order_by(TianhuaPreDeliveryImportItem.row_no)).all()
    draft=db.scalar(select(TianhuaPreDeliveryDraft).where(TianhuaPreDeliveryDraft.batch_id==batch.id))
    return {"batch_id":batch.id,"batch_number":batch.batch_number,"customer_id":batch.customer_id,"customer_name":batch.customer_name,"status":batch.status,"total_rows":len(items),"draft":draft_dict(db,draft) if draft else None,"items":[item_dict(i) for i in items]}


def create_batch(db,content,filename,user_id):
    processed=[preprocess_row(db,r) for r in recognize_tianhua_image(content)]
    product_id=next((x["product_id"] for x in processed if x["product_id"]),None)
    product=db.get(Product,product_id) if product_id else None
    customer=db.get(Customer,product.customer_id) if product else db.scalar(select(Customer).where(or_(Customer.name.contains("天华"),Customer.customer_code=="天华")).order_by(Customer.id.desc()))
    if customer is None: raise ValueError("系统中未找到天华客户资料")
    batch=TianhuaPreDeliveryImportBatch(batch_number=f"TH-{datetime.now():%Y%m%d%H%M%S}-{uuid.uuid4().hex[:6].upper()}",filename=filename,customer_id=customer.id,customer_name=customer.name,total_rows=len(processed),created_by=user_id)
    db.add(batch); db.flush()
    for x in processed: db.add(TianhuaPreDeliveryImportItem(batch_id=batch.id,**x))
    db.commit(); db.refresh(batch); return batch


def _selection(db,batch_id,submitted):
    stored={i.row_no:i for i in db.scalars(select(TianhuaPreDeliveryImportItem).where(TianhuaPreDeliveryImportItem.batch_id==batch_id)).all()}; result=[]; seen=set()
    for line in submitted:
        row_no=int(line["row_no"])
        if row_no in seen: raise ValueError(f"第 {row_no} 行重复提交")
        seen.add(row_no)
        item=stored.get(row_no)
        if item is None or (line.get("item_id") is not None and item.id!=int(line["item_id"])):
            raise ValueError(f"第 {row_no} 行不属于当前批次")
        item.selected=bool(line.get("selected"))
        if not item.selected: continue
        if item.status not in GENERATABLE: raise ValueError(f"第 {item.row_no} 行为{STATUS_LABELS.get(item.status,item.status)}，不允许生成")
        qty=int(line.get("final_delivery_qty") or 0)
        if qty<=0 or item.order_item_id is None or item.product_id is None: raise ValueError(f"第 {item.row_no} 行数据不完整")
        if item.system_pending_qty is not None and qty>item.system_pending_qty: raise ValueError(f"第 {item.row_no} 行数量超过系统未送数量")
        item.final_delivery_qty=qty; result.append((item,qty))
    if not result: raise ValueError("至少选择一条可生成明细")
    return result


def save_draft(db,batch,submitted,remark,user_id,update_existing=False):
    draft=db.scalar(select(TianhuaPreDeliveryDraft).where(TianhuaPreDeliveryDraft.batch_id==batch.id))
    if batch.status=="draft_created" and not update_existing:
        raise RuntimeError("该批次已生成草稿，不能重复生成")
    if draft and not update_existing: raise RuntimeError(f"该批次已生成草稿 {draft.draft_number}，不能重复生成")
    if not draft and update_existing: raise ValueError("该批次尚未生成草稿")
    selected=_selection(db,batch.id,submitted)
    if draft: db.execute(delete(TianhuaPreDeliveryDraftItem).where(TianhuaPreDeliveryDraftItem.draft_id==draft.id))
    else:
        draft=TianhuaPreDeliveryDraft(draft_number=f"THYSH-{datetime.now():%Y%m%d-%H%M%S}-{batch.id}",batch_id=batch.id,customer_id=batch.customer_id,created_by=user_id)
        db.add(draft); db.flush(); batch.status="draft_created"
    draft.remark=f"来源：天华预送货图片导入，批次 ID：{batch.id}"+(f"；{remark.strip()}" if remark and remark.strip() else "")
    draft.updated_at=datetime.utcnow()
    for item,qty in selected: db.add(TianhuaPreDeliveryDraftItem(draft_id=draft.id,import_item_id=item.id,row_no=item.row_no,stock_code=item.stock_code or "",product_id=item.product_id,order_item_id=item.order_item_id,delivery_qty=qty,warning=item.warning))
    db.commit(); db.refresh(draft); return draft
