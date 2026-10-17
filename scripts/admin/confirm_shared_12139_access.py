"""Reviewed existing 12139 masters; original units and inventory stay frozen."""
import hashlib,json,re
from sqlalchemy import select
from app.models.shared_finished_stock import SharedFinishedGroup
from app.models.product import Product
from app.models.warehouse_inventory import InventoryLot
from app.services import shared_finished_stock as shared
ARGS=dict(product_ids=[3567,3886],lot_ids=[1140],auto_enroll=True)
EVIDENCE='研光与海外80012139共用；复用现有主档，均为无需结合的单片模切成品，按既有工艺单位规则显示片；只/片原值及售价图号不改。批次1140仅补充描述规格确认，不重写原身份、数量、成本、预占或货位。'
KEY='shared-12139-existing-20261017-v2'


def facts(db):
    lot=db.get(InventoryLot,1140)
    return {c.name:getattr(lot,c.name) for c in InventoryLot.__table__.columns if c.name not in {'version','updated_at'}} | {'physical_identity':shared.lot_identity(lot)}


def plan(db):
    source=db.get(Product,3567);target=db.get(Product,3886);lot=db.get(InventoryLot,1140)
    if not (source and target and source.customer_id==137 and target.customer_id==143
        and source.product_code==target.product_code=='80012139' and source.mold_tool_id==target.mold_tool_id==358
        and source.unit=='只' and target.unit=='片' and lot and lot.lot_number=='FG-20260930-76AB45900E'
        and lot.quantity_available==290 and lot.quantity_reserved==0 and lot.location.id==2481):
        raise shared._error('本次12139已审阅对象或库存发生变化，请重新核对')
    frozen=json.loads(lot.finished_detail.physical_basis_json);current=json.loads(shared.product_basis(source))
    if not (frozen.get('spec') is None and frozen.get('mold_tool_id')=='358' and current.get('spec')):
        raise shared._error('批次差异超出本次补充确认范围')
    supplement={1140:dict(identity_sha256=hashlib.sha256(shared.lot_identity(lot).encode()).hexdigest(),fields={'spec':current['spec']})}
    return shared.preview(db,**ARGS,confirmed_lot_fields=supplement),supplement


def apply(db,*,expected_hash,backup_receipt):
    if not (backup_receipt.get('verified') is True and re.fullmatch('[0-9a-f]{64}',backup_receipt.get('sha256',''))):
        raise shared._error('缺少本次验证过的备份')
    previous=db.scalar(select(SharedFinishedGroup).where(SharedFinishedGroup.operation_key==KEY))
    if previous:
        request=json.loads(previous.request_json)
        if (request['preview_hash']!=expected_hash or request['product_ids']!=ARGS['product_ids']
                or request['lot_ids']!=ARGS['lot_ids'] or request['evidence']!=EVIDENCE):
            raise shared._error('既有凭证不符合本次范围')
        return dict(group_id=previous.id,replayed=True)
    value,proof=plan(db)
    if value['preview_hash']!=expected_hash:raise shared._error('正式资料与审阅清单不同，停止设置')
    before=facts(db)
    masters={pid:{c.name:getattr(db.get(Product,pid),c.name) for c in Product.__table__.columns} for pid in ARGS['product_ids']}
    result=shared._apply_confirmed(db,**ARGS,preview_hash=expected_hash,operation_key=KEY,evidence=EVIDENCE,
        actor=None,source='script',confirmed_lot_fields=proof)
    db.expire_all()
    assert facts(db)==before,'库存原事实变化，必须回滚'
    for pid,original in masters.items():
        assert original=={c.name:getattr(db.get(Product,pid),c.name) for c in Product.__table__.columns},'原客户产品资料变化，必须回滚'
    from app.services.warehouse_inventory import finished_inventory_candidates_for_product
    for pid,cid in ((3567,137),(3886,143)):
        assert any(l.id==1140 for l in finished_inventory_candidates_for_product(db,product_id=pid,customer_id=cid))
    return {**result,'existing_product_ids':[3567,3886],'created_sales_references':[],
        'original_available':'290','original_reserved':'0','original_inventory_preserved':True,
        'both_customers_can_use_original_lot':True,'both_product_masters_unchanged':True,
        'original_units_prices_and_drawings_preserved':True}
