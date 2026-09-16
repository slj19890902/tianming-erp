"""Explicitly approved, bounded master/profile correction. Caller owns backup/transaction.

No CLI apply: use only from the verified release transaction or isolated rehearsal.
"""
import json
from sqlalchemy import select
from app.models.product import Product
from app.models.audit import OperationLog
from app.models.warehouse_goods import WarehouseGoodsProfile
from app.models.warehouse_inventory import InventoryLot, SemiFinishedLotAllowedProduct
from app.models.mold_tool import MoldTool
from app.services.warehouse_inventory import utc_now

RESOURCE = 'approved-sheet-match-20260916'


def apply_approved_facts(db):
    existing=db.scalar(select(OperationLog).where(OperationLog.resource==RESOURCE))
    if existing:
        return dict(replayed=True, audit_id=existing.id)
    lot=db.get(InventoryLot,871);detail=lot.semi_finished_detail if lot else None
    assert detail and lot.status=='active' and lot.quantity_reserved==0, '871 status changed'
    assert (detail.internal_name,detail.board_length_mm,detail.board_width_mm,detail.flute_type,detail.material_code_snapshot)==('18*46依工空白',976,1303,'B','R7A'), '871 identity changed'
    products=[db.get(Product,i) for i in (203,404)]
    assert [p.product_code for p in products]==['21301411','21301501']
    assert all(p.customer_id==5 and p.mold_tool_id==158 and p.flute_type=='B' and p.layer_count==3 and p.deleted_at is None for p in products)
    mold=db.get(MoldTool,158)
    assert mold and mold.is_active and mold.version==1
    profile=db.get(WarehouseGoodsProfile,871)
    before=json.loads(profile.data_json)
    assert before['processing']=='die_cut' and not before['product_ids'] and before['scope']=='general'
    after={**before,'product_ids':[203,404],'mold_tool_id':158,'mold_version':1,'blank_unprinted':True}
    profile.data_json=json.dumps(after,ensure_ascii=False)
    lot.version+=1
    now=utc_now()
    for p in products:
        if not db.scalar(select(SemiFinishedLotAllowedProduct.id).where(SemiFinishedLotAllowedProduct.inventory_lot_id==871,SemiFinishedLotAllowedProduct.product_id==p.id)):
            db.add(SemiFinishedLotAllowedProduct(inventory_lot_id=871,product_id=p.id,confirmed_at=now,confirmed_by=None))
    renamed=[]
    for p in db.scalars(select(Product).where(Product.deleted_at.is_(None),Product.box_style.in_(['CB 单瓦衬板','SC 双瓦衬板']))):
        renamed.append(dict(id=p.id,code=p.product_code,before=p.box_style,version_before=p.version,layer_count=p.layer_count,flute=p.flute_type))
        p.box_style='衬板';p.version+=1
    audit=OperationLog(username='codex-approved-release',role='system',action='UPDATE',resource=RESOURCE,
        entity_type='sheet_matching_correction',entity_id=871,
        description='老板2026-09-16批准：871实际依工用途203/404；衬板主档命名归一；不改数量、实际材质、价格或历史快照',
        details=json.dumps(dict(profile_before=before,profile_after=after,renamed_products=renamed),ensure_ascii=False))
    db.add(audit);db.flush()
    return dict(replayed=False,audit_id=audit.id,lot_id=871,product_ids=[203,404],renamed=len(renamed))
