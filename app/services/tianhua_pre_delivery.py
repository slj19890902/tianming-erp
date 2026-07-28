from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import date, timedelta

import cv2
import numpy as np
from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from app.core.time_contract import (
    beijing_now_naive,
    beijing_today,
    utc_naive_to_api,
    utc_now_naive,
)
from app.models.customer import Customer
from app.models.delivery import Delivery, DeliveryItem
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.production import ProductionTask
from app.models.tianhua_pre_delivery import TianhuaPreDeliveryDraft, TianhuaPreDeliveryDraftItem, TianhuaPreDeliveryImportBatch, TianhuaPreDeliveryImportItem
from app.services.delivery_numbering import next_delivery_number
from app.services.production_workflow import production_ready_quantity

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
    image_order_no: str | None = None


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


VALID_ORDER_STATUSES = {
    "pending_production",
    "production",
    "pending_delivery",
    "partially_delivered",
}


def _available_delivery_quantity(db: Session, order_item: OrderItem) -> int:
    task = db.scalar(
        select(ProductionTask).where(
            ProductionTask.order_item_id == order_item.id,
        )
    )
    if task is None:
        pending = max(
            int(order_item.quantity or 0) - int(order_item.delivered_quantity or 0),
            0,
        )
        return pending if order_item.material_status == "received" else 0
    if task.status not in {"completed", "not_required"}:
        return 0
    return max(
        min(int(order_item.quantity or 0), production_ready_quantity(db, order_item))
        - int(order_item.delivered_quantity or 0),
        0,
    )


def _image_order_no(row: RecognizedRow) -> str | None:
    if row.image_order_no:
        return row.image_order_no.strip()
    for token in re.findall(r"[A-Za-z0-9_-]{5,40}", row.raw_text):
        if token != row.stock_code and token != str(row.image_qty):
            return token
    return None


def _candidate_score(
    order_item: OrderItem,
    order: Order,
    *,
    image_qty: int,
    image_order_no: str | None,
    pre_delivery_date: date,
) -> tuple[int, int, bool, bool]:
    order_match = bool(
        image_order_no
        and image_order_no.strip().upper()
        in {
            order.order_number.strip().upper(),
            (order.customer_po or "").strip().upper(),
        }
    )
    pending = int(order_item.quantity - order_item.delivered_quantity)
    quantity_match = image_qty in {pending, int(order_item.quantity)}
    target_date = order.delivery_date or order.order_date
    date_distance = abs((target_date - pre_delivery_date).days)
    score = (
        (100_000 if order_match else 0)
        + (10_000 if pending == image_qty else 5_000 if order_item.quantity == image_qty else 0)
        + max(0, 1_000 - date_distance)
        + max(0, 500 - abs(pending - image_qty))
    )
    return score, date_distance, order_match, quantity_match


def preprocess_row(
    db: Session,
    row: RecognizedRow,
    pre_delivery_date: date | None = None,
) -> dict:
    target_date = pre_delivery_date or (beijing_today() + timedelta(days=1))
    image_order_no = _image_order_no(row)
    data=dict(row_no=row.row_no,raw_text=row.raw_text,stock_code=row.stock_code,image_qty=row.image_qty,image_order_no=image_order_no,product_id=None,product_name=None,order_item_id=None,order_id=None,order_number=None,customer_order_no=None,match_reason=None,match_score=None,candidate_count=0,system_pending_qty=None,available_qty=None,suggested_qty=row.image_qty,final_delivery_qty=row.image_qty,status="ocr_failed",warning="未能同时识别 8 位存货编码和最右侧整数数量。",selected=False)
    if row.stock_code is None or row.image_qty is None: return data
    products=_products(db,row.stock_code)
    if not products: data.update(status="not_matched",warning="未找到该存货编码对应的天华产品。"); return data
    pmap={p.id:p for p in products}
    candidates=[]
    for oi,o in db.execute(select(OrderItem,Order).join(Order,Order.id==OrderItem.order_id).where(OrderItem.product_id.in_(pmap),OrderItem.delivered_quantity<OrderItem.quantity,OrderItem.is_force_closed.is_(False),Order.status.not_in(("cancelled","dead","closed","archived","completed")))):
        p=pmap[oi.product_id]
        if (
            o.customer_id != p.customer_id
            or o.status not in VALID_ORDER_STATUSES
            or "RUIDA" in o.order_number.upper()
        ):
            continue
        pending=int(oi.quantity-oi.delivered_quantity); available=_available_delivery_quantity(db,oi)
        score,distance,order_match,quantity_match=_candidate_score(oi,o,image_qty=row.image_qty,image_order_no=image_order_no,pre_delivery_date=target_date)
        candidates.append((score,distance,oi,o,p,pending,available,order_match,quantity_match))
    candidates.sort(key=lambda x:(-x[0],x[1],x[3].delivery_date or x[3].order_date,-x[2].id))
    if not candidates: data.update(product_id=products[0].id,product_name=products[0].product_name,status="not_matched",warning="已匹配天华产品，但没有可用的未送订单。"); return data
    score,distance,oi,o,p,pending,available,order_match,quantity_match=candidates[0]
    candidate_count=len(candidates)
    ambiguous=candidate_count>1 and score-candidates[1][0]<=1
    ruida_excluded=db.scalar(select(OrderItem.id).join(Order,Order.id==OrderItem.order_id).where(OrderItem.product_id.in_(pmap),Order.order_number.ilike("%RUIDA%")).limit(1)) is not None
    if order_match and quantity_match:
        reason="按订单号+数量完全匹配"
    elif quantity_match and candidate_count>1:
        reason=f"同编码同数量 {candidate_count} 个候选，按预送货日期最近匹配（相差 {distance} 天）"
    elif quantity_match:
        reason=f"数量完全匹配，订单日期距预送货日期 {distance} 天"
    else:
        reason=f"数量差 {abs(pending-row.image_qty)}，按综合评分匹配"
    if ruida_excluded:
        reason += "；已排除 RUIDA 历史订单"
    data.update(product_id=p.id,product_name=p.product_name,order_item_id=oi.id,order_id=o.id,order_number=o.order_number,customer_order_no=o.customer_po,match_reason=reason,match_score=score,candidate_count=candidate_count,system_pending_qty=available,available_qty=available)
    dup=db.execute(select(Delivery.delivery_number,Delivery.delivery_date).join(DeliveryItem,DeliveryItem.delivery_id==Delivery.id).join(OrderItem,OrderItem.id==DeliveryItem.order_item_id).where(Delivery.customer_id==o.customer_id,Delivery.status=="dispatched",Delivery.delivery_date>=beijing_today()-timedelta(days=7),OrderItem.product_id==p.id,DeliveryItem.delivered_quantity==row.image_qty).limit(1)).one_or_none()
    multi_warning=f"该存货编码存在 {candidate_count} 个未送订单，请核对匹配订单号。" if candidate_count>1 else ""
    if available<row.image_qty: data.update(status="stock_shortage",warning=f"当前可送数量 {available}，小于图片数量 {row.image_qty}；订单可能尚未入库或库存不足。")
    elif dup: data.update(status="duplicate_warning",warning=f"相同编码和数量最近 7 天已送过：{dup.delivery_number}（{dup.delivery_date}）。")
    elif ambiguous: data.update(status="duplicate_warning",warning=multi_warning+" 候选评分接近，需要人工确认。")
    elif pending!=row.image_qty: data.update(status="qty_mismatch",warning=f"客户图片数量 {row.image_qty}，系统未送数量 {pending}"+("，差异仅 1 个，请重点确认。" if abs(pending-row.image_qty)==1 else "。"))
    else: data.update(status="ok",warning=multi_warning,selected=True)
    return data


def item_dict(i, draft_item=None):
    return {"item_id":i.id,"row_no":i.row_no,"raw_text":i.raw_text,"stock_code":i.stock_code,"image_qty":i.image_qty,"image_order_no":i.image_order_no,"product_id":i.product_id,"product_name":i.product_name,"order_item_id":i.order_item_id,"order_id":i.order_id,"order_no":i.order_number,"customer_order_no":i.customer_order_no,"match_reason":i.match_reason,"match_score":i.match_score,"candidate_count":i.candidate_count,"system_pending_qty":i.system_pending_qty,"available_qty":i.available_qty,"suggested_qty":i.suggested_qty,"final_delivery_qty":i.final_delivery_qty,"status":i.status,"status_label":STATUS_LABELS.get(i.status,i.status),"warning":i.warning or "","selected":i.selected,"delivery_item_id":draft_item.delivery_item_id if draft_item else None,"mobile_pick_status":draft_item.mobile_pick_status if draft_item else "pending","mobile_picked_qty":draft_item.mobile_picked_qty if draft_item else None,"mobile_pick_note":draft_item.mobile_pick_note if draft_item else "","mobile_picked_at":utc_naive_to_api(draft_item.mobile_picked_at) if draft_item and draft_item.mobile_picked_at else None}


def draft_dict(db,draft):
    items=db.scalars(select(TianhuaPreDeliveryDraftItem).where(TianhuaPreDeliveryDraftItem.draft_id==draft.id).order_by(TianhuaPreDeliveryDraftItem.row_no)).all()
    delivery=db.get(Delivery,draft.delivery_id) if draft.delivery_id else None
    return {"draft_id":draft.id,"draft_number":draft.draft_number,"batch_id":draft.batch_id,"status":draft.status,"remark":draft.remark,"delivery_id":delivery.id if delivery else None,"delivery_number":delivery.delivery_number if delivery else None,"delivery_status":delivery.status if delivery else None,"delivery_total_quantity":delivery.total_quantity if delivery else 0,"items":[{"item_id":i.id,"import_item_id":i.import_item_id,"row_no":i.row_no,"stock_code":i.stock_code,"order_item_id":i.order_item_id,"order_id":i.order_id,"order_no":i.order_number,"customer_order_no":i.customer_order_no,"delivery_item_id":i.delivery_item_id,"delivery_qty":i.delivery_qty,"warning":i.warning or "","mobile_pick_status":i.mobile_pick_status,"mobile_picked_qty":i.mobile_picked_qty,"mobile_pick_note":i.mobile_pick_note or "","mobile_picked_at":utc_naive_to_api(i.mobile_picked_at) if i.mobile_picked_at else None} for i in items]}


def batch_dict(db,batch):
    items=db.scalars(select(TianhuaPreDeliveryImportItem).where(TianhuaPreDeliveryImportItem.batch_id==batch.id).order_by(TianhuaPreDeliveryImportItem.row_no)).all()
    draft=db.scalar(select(TianhuaPreDeliveryDraft).where(TianhuaPreDeliveryDraft.batch_id==batch.id))
    draft_items = {
        value.import_item_id: value
        for value in db.scalars(
            select(TianhuaPreDeliveryDraftItem).where(
                TianhuaPreDeliveryDraftItem.draft_id == draft.id
            )
        ).all()
    } if draft else {}
    return {"batch_id":batch.id,"batch_number":batch.batch_number,"customer_id":batch.customer_id,"customer_name":batch.customer_name,"pre_delivery_date":batch.pre_delivery_date.isoformat() if batch.pre_delivery_date else None,"status":batch.status,"total_rows":len(items),"draft":draft_dict(db,draft) if draft else None,"items":[item_dict(i,draft_items.get(i.id)) for i in items]}


def create_batch(db,content,filename,user_id,pre_delivery_date=None):
    target_date=pre_delivery_date or (beijing_today()+timedelta(days=1))
    processed=[preprocess_row(db,r,target_date) for r in recognize_tianhua_image(content)]
    product_id=next((x["product_id"] for x in processed if x["product_id"]),None)
    product=db.get(Product,product_id) if product_id else None
    customer=db.get(Customer,product.customer_id) if product else db.scalar(select(Customer).where(or_(Customer.name.contains("天华"),Customer.customer_code=="天华")).order_by(Customer.id.desc()))
    if customer is None: raise ValueError("系统中未找到天华客户资料")
    batch=TianhuaPreDeliveryImportBatch(batch_number=f"TH-{beijing_now_naive():%Y%m%d%H%M%S}-{uuid.uuid4().hex[:6].upper()}",filename=filename,customer_id=customer.id,customer_name=customer.name,pre_delivery_date=target_date,total_rows=len(processed),created_by=user_id)
    db.add(batch); db.flush()
    for x in processed: db.add(TianhuaPreDeliveryImportItem(batch_id=batch.id,**x))
    db.commit(); db.refresh(batch); return batch


def _delivery_total(db: Session, delivery_id: int) -> int:
    return int(
        db.scalar(
            select(func.coalesce(func.sum(DeliveryItem.delivered_quantity), 0))
            .where(DeliveryItem.delivery_id == delivery_id)
        )
        or 0
    )


def ensure_draft_delivery(
    db: Session,
    batch: TianhuaPreDeliveryImportBatch,
    draft: TianhuaPreDeliveryDraft,
    created_by: int | None = None,
) -> Delivery | None:
    delivery = db.get(Delivery, draft.delivery_id) if draft.delivery_id else None
    if delivery is not None and delivery.status == "voided":
        raise ValueError(
            "关联送货单已作废；为保留历史审计，不能在原天华草稿上重建，"
            "请重新上传预送货截图生成新草稿"
        )
    if delivery is not None and delivery.status != "pending":
        raise ValueError("关联送货单已确认发货，不能再修改预送货拿货结果")
    rows = db.scalars(
        select(TianhuaPreDeliveryDraftItem)
        .where(TianhuaPreDeliveryDraftItem.draft_id == draft.id)
        .order_by(TianhuaPreDeliveryDraftItem.row_no)
    ).all()
    has_positive_qty = any(int(row.delivery_qty or 0) > 0 for row in rows)
    if not has_positive_qty:
        for row in rows:
            row.delivery_item_id = None
        if delivery is not None:
            db.execute(delete(DeliveryItem).where(DeliveryItem.delivery_id == delivery.id))
            db.delete(delivery)
        draft.delivery_id = None
        draft.updated_at = utc_now_naive()
        db.flush()
        return None

    if delivery is None:
        delivery_date = batch.pre_delivery_date or beijing_today()
        customer = db.get(Customer, batch.customer_id)
        if customer is None:
            raise ValueError("送货客户不存在，无法生成送货单号")
        delivery = Delivery(
            delivery_number=next_delivery_number(
                db,
                customer=customer,
                delivery_date=delivery_date,
            ),
            customer_id=batch.customer_id,
            delivery_date=delivery_date,
            status="pending",
            total_quantity=0,
            created_by=created_by,
        )
        db.add(delivery)
        db.flush()
        draft.delivery_id = delivery.id
    else:
        delivery.delivery_date = batch.pre_delivery_date or delivery.delivery_date

    existing = {
        value.order_item_id: value
        for value in db.scalars(
            select(DeliveryItem).where(DeliveryItem.delivery_id == delivery.id)
        ).all()
    }
    wanted_ids: set[int] = set()
    positive_order_items: set[int] = set()
    for row in rows:
        qty = int(row.delivery_qty or 0)
        if qty <= 0:
            if row.delivery_item_id:
                old = db.get(DeliveryItem, row.delivery_item_id)
                if old is not None and old.delivery_id == delivery.id:
                    db.delete(old)
            existing.pop(row.order_item_id, None)
            row.delivery_item_id = None
            continue
        if row.order_item_id in positive_order_items:
            raise ValueError(f"第 {row.row_no} 行与其他行重复绑定同一订单明细，请只保留一行生成正式送货单")
        positive_order_items.add(row.order_item_id)
        order_item = db.get(OrderItem, row.order_item_id)
        if order_item is None or order_item.order_id != row.order_id:
            raise ValueError(f"第 {row.row_no} 行订单绑定无效")
        remaining = _available_delivery_quantity(db, order_item)
        if qty > remaining:
            raise ValueError(f"第 {row.row_no} 行数量超过系统未送数量")
        delivery_item = existing.get(row.order_item_id)
        if delivery_item is None:
            delivery_item = DeliveryItem(
                delivery_id=delivery.id,
                order_item_id=row.order_item_id,
                delivered_quantity=qty,
                remarks=None,
            )
            db.add(delivery_item)
        else:
            delivery_item.delivered_quantity = qty
        db.flush()
        row.delivery_item_id = delivery_item.id
        wanted_ids.add(delivery_item.id)

    for delivery_item in db.scalars(
        select(DeliveryItem).where(DeliveryItem.delivery_id == delivery.id)
    ).all():
        if delivery_item.id not in wanted_ids:
            db.delete(delivery_item)
    db.flush()
    delivery.total_quantity = _delivery_total(db, delivery.id)
    draft.updated_at = utc_now_naive()
    return delivery


def _selection(db,batch_id,submitted,zero_allowed=None):
    zero_allowed=zero_allowed or set()
    stored={i.row_no:i for i in db.scalars(select(TianhuaPreDeliveryImportItem).where(TianhuaPreDeliveryImportItem.batch_id==batch_id)).all()}; result=[]; seen=set()
    selected_order_items=set()
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
        if qty<0 or (qty==0 and item.id not in zero_allowed) or item.order_item_id is None or item.order_id is None or not item.order_number or item.product_id is None: raise ValueError(f"第 {item.row_no} 行数据不完整")
        bound_order_item=db.get(OrderItem,item.order_item_id)
        if bound_order_item is None or bound_order_item.order_id!=item.order_id:
            raise ValueError(f"第 {item.row_no} 行订单绑定无效")
        available=_available_delivery_quantity(db,bound_order_item)
        if qty>available: raise ValueError(f"第 {item.row_no} 行数量超过系统可送数量")
        if qty>0 and item.order_item_id in selected_order_items:
            raise ValueError(f"第 {item.row_no} 行与其他行重复绑定同一订单明细，请只保留一行生成正式送货单")
        if qty>0:
            selected_order_items.add(item.order_item_id)
        item.final_delivery_qty=qty; result.append((item,qty))
    if not result: raise ValueError("至少选择一条可生成明细")
    return result


def save_draft(db,batch,submitted,remark,user_id,update_existing=False):
    draft=db.scalar(select(TianhuaPreDeliveryDraft).where(TianhuaPreDeliveryDraft.batch_id==batch.id))
    if batch.status=="draft_created" and not update_existing:
        raise RuntimeError("该批次已生成草稿，不能重复生成")
    if draft and not update_existing: raise RuntimeError(f"该批次已生成草稿 {draft.draft_number}，不能重复生成")
    if not draft and update_existing: raise ValueError("该批次尚未生成草稿")
    previous={}
    if draft:
        previous={
            value.import_item_id:value
            for value in db.scalars(
                select(TianhuaPreDeliveryDraftItem).where(
                    TianhuaPreDeliveryDraftItem.draft_id==draft.id
                )
            ).all()
        }
    zero_allowed={
        import_item_id
        for import_item_id,value in previous.items()
        if value.mobile_pick_status=="no_stock"
    }
    selected=_selection(db,batch.id,submitted,zero_allowed)
    if draft: db.execute(delete(TianhuaPreDeliveryDraftItem).where(TianhuaPreDeliveryDraftItem.draft_id==draft.id))
    else:
        draft=TianhuaPreDeliveryDraft(draft_number=f"THYSH-{beijing_now_naive():%Y%m%d-%H%M%S}-{batch.id}",batch_id=batch.id,customer_id=batch.customer_id,created_by=user_id)
        db.add(draft); db.flush(); batch.status="draft_created"
    draft.remark=f"来源：天华预送货图片导入，批次 ID：{batch.id}"+(f"；{remark.strip()}" if remark and remark.strip() else "")
    draft.updated_at=utc_now_naive()
    for item,qty in selected:
        old=previous.get(item.id)
        db.add(TianhuaPreDeliveryDraftItem(draft_id=draft.id,import_item_id=item.id,row_no=item.row_no,stock_code=item.stock_code or "",product_id=item.product_id,order_item_id=item.order_item_id,order_id=item.order_id,order_number=item.order_number or "",customer_order_no=item.customer_order_no,delivery_item_id=old.delivery_item_id if old else None,delivery_qty=qty,warning=item.warning,mobile_pick_status=old.mobile_pick_status if old else "pending",mobile_picked_qty=old.mobile_picked_qty if old else None,mobile_pick_note=old.mobile_pick_note if old else None,mobile_picked_at=old.mobile_picked_at if old else None,mobile_picked_by=old.mobile_picked_by if old else None))
    db.flush()
    ensure_draft_delivery(db,batch,draft,user_id)
    db.commit(); db.refresh(draft); return draft
