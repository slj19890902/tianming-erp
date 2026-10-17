"""Narrow reviewed department grant; never a bulk inventory repair entry."""
import hashlib,json
from sqlalchemy import select
from app.models.product import Product
from app.models.customer import Customer
from app.models.warehouse_inventory import InventoryLot
from app.models.shared_finished_stock import SharedFinishedMember, SharedFinishedMutation
from app.services import shared_customer_access as access,shared_finished_stock as shared

ARGS=dict(source_product_id=3567,product_ids=[3567],new_customer_ids=[143],lot_ids=[1140],auto_enroll=True)
EVIDENCE='老板确认80012139研光与海外订单共用；同一模具358、VIK/B；旧批次仅缺描述规格，原身份快照保持；不新增实物库存，不复制售价或客户图号。'
KEY='shared-12139-overseas-20261017-v1'


def facts(db):
    lot=db.get(InventoryLot,1140)
    return {c.name:getattr(lot,c.name) for c in InventoryLot.__table__.columns if c.name not in {'version','updated_at'}} | {'physical_identity':shared.lot_identity(lot)}


def plan(db):
    source=db.get(Product,3567);target=db.get(Customer,143);lot=db.get(InventoryLot,1140)
    if not (source and source.customer_id==137 and source.product_code=='80012139' and source.mold_tool_id==358
            and target and target.name.endswith('--海外订单') and lot and lot.lot_number=='FG-20260930-76AB45900E'
            and lot.quantity_available==290 and lot.quantity_reserved==0 and lot.location.id==2481):
        raise shared._error('12139本次已审阅对象、余额或原货位发生变化，停止设置并重新核对')
    frozen=json.loads(lot.finished_detail.physical_basis_json);current=json.loads(shared.product_basis(source))
    if not (frozen.get('spec') is None and frozen.get('mold_tool_id')=='358'
            and current.get('mold_tool_id')=='358' and current.get('spec')):
        raise shared._error('旧批次规格差异不符合本次受限确认范围')
    supplement={1140:dict(identity_sha256=hashlib.sha256(shared.lot_identity(lot).encode()).hexdigest(),fields={'spec':current['spec']})}
    value=access.preview(db,**ARGS,confirmed_lot_fields=supplement)
    return value,supplement


def apply(db,*,expected_hash,backup_receipt):
    previous=db.scalar(select(SharedFinishedMutation).where(SharedFinishedMutation.operation_key==KEY))
    if previous:
        request=json.loads(previous.request_json)
        if request['preview_hash']!=expected_hash or request['args']!=ARGS or request['evidence']!=EVIDENCE:
            raise shared._error('原维护凭证不符合本次审阅范围')
        return {**json.loads(previous.result_json),'replayed':True}
    value,supplement=plan(db)
    if value['preview_hash']!=expected_hash:raise shared._error('正式事实与审阅清单不同，未执行设置')
    before=facts(db);source=db.get(Product,3567)
    source_before={c.name:getattr(source,c.name) for c in Product.__table__.columns}
    result=access.confirm(db,**ARGS,actor=None,source='script',backup_receipt=backup_receipt,
        preview_hash=expected_hash,operation_key=KEY,evidence=EVIDENCE,confirmed_lot_fields=supplement)
    db.expire_all()
    if facts(db)!=before:raise AssertionError('原库存事实发生变化，必须回滚')
    source=db.get(Product,3567)
    if source_before!={c.name:getattr(source,c.name) for c in Product.__table__.columns}:
        raise AssertionError('研光原主档发生变化，必须回滚')
    from app.services.warehouse_inventory import finished_inventory_candidates_for_product
    created=result['created_sales_references'];assert len(created)==1 and created[0]['customer_id']==143
    new=db.get(Product,created[0]['product_id'])
    assert new.sale_unit_price is new.sale_unit_price_no_tax is new.customer_drawing_number is None
    for pid,cid in ((3567,137),(new.id,143)):
        assert any(l.id==1140 for l in finished_inventory_candidates_for_product(db,product_id=pid,customer_id=cid))
    return {**result,'original_available':str(db.get(InventoryLot,1140).quantity_available),
        'original_reserved':str(db.get(InventoryLot,1140).quantity_reserved),'original_inventory_preserved':True,
        'source_product_unchanged':True,'both_customers_can_use_original_lot':True,'prices_and_drawing_left_blank':True}

