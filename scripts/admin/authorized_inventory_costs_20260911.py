"""Owner-authorized missing inventory cost plan. Defaults to read-only.
No product/order/quantity facts are changed. Inferences live only in batch cost evidence.
"""
from pathlib import Path
import sys, json, hashlib, sqlite3, argparse
from decimal import Decimal
from types import SimpleNamespace
from datetime import datetime
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session
from app.models.product import Product
from app.models.material import Material
from app.models.warehouse_inventory import InventoryLot, FinishedGoodsInventoryDetail
from app.models.audit import OperationLog
from app.services.inventory_cost_snapshot import estimate_finished_product_cost, apply_cost_snapshot, InventoryCostEstimate
from app.services.box_type_rules import recommend_box_type

TASK = "owner-authorized-missing-cost-20260911"
# Product -> current material / supporting current common-box ID. Kept explicit
# so a future unrelated product cannot silently enter this one-time data task.
MATERIALS = {2:(120,102),8:(417,None),101:(123,32),105:(339,19),110:(389,51),114:(123,32),115:(120,102),140:(396,1210),146:(339,19),157:(72,100),194:(389,51),350:(398,6),375:(397,20),485:(385,514),529:(339,19),663:(389,45),1385:(141,1386),1449:(139,2897),1486:(417,None),1857:(144,1985),2330:(120,102),2406:(385,1531)}
FORMULA = {2:"single",105:"single",485:"single",663:"double",1385:"single",2330:"double",2406:"single"}
SPECIAL = {8:(890,910,1,"衬板按当前产品长宽"),101:(1890,820,2,"旧报料厘米转毫米；大外箱双拼"),115:(1625,775,2,"旧报料厘米转毫米；大外箱双拼"),140:(1265,1358,1,"参照同系列1210内盒展开：长=2W+35，宽=L+4H+35，仅作批次成本估算"),375:(1030,750,1,"参照同系列103安全鞋盒产品20报料，作为近似成本，不作为生产尺寸"),1449:(1395,1275,1,"低高度内盒按已存旧报料厘米转毫米，不套开槽箱公式"),1486:(420,400,1,"三层B楞衬板按产品长宽，A7A近似材质参考"),1857:(1750,675,1,"同尺寸同系列产品1985已维护报料")}
MISSING = {238:"白板纸无价格合同",2750:"无真实尺寸",3326:"1×1×1占位尺寸不能计算",3821:"EPE无采购单价"}


def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,default=str).encode()).hexdigest()


def plan(db, allowed_ids=None):
    query=select(InventoryLot, Product).join(FinishedGoodsInventoryDetail, FinishedGoodsInventoryDetail.inventory_lot_id==InventoryLot.id).join(Product,Product.id==FinishedGoodsInventoryDetail.product_id).where(InventoryLot.status=="active",InventoryLot.quantity_available+InventoryLot.quantity_reserved>0, (InventoryLot.estimated_unit_cost_snapshot.is_(None)) | (InventoryLot.estimated_unit_cost_snapshot<=0)).order_by(InventoryLot.id)
    rows=[]; unresolved=[]
    for lot, product in db.execute(query):
        if allowed_ids is not None and lot.id not in allowed_ids: continue
        base={"lot_id":lot.id,"lot_number":lot.lot_number,"version":lot.version,"quantity_available":lot.quantity_available,"quantity_reserved":lot.quantity_reserved,"product_id":product.id,"product_code":product.product_code,"product_name":product.product_name}
        if product.id in MISSING:
            unresolved.append({**base,"reason":MISSING[product.id]});continue
        evidence={"authorization":"老板2026-09-11授权按常用箱及名称推测补录", "basis":"current_reference_cost_not_historical_purchase_fact", "product_version":product.version,"original_box_style":product.box_style,"original_material_id":product.material_id,"original_material_text":product.legacy_material_text,"original_dimensions":[product.report_length_mm,product.report_width_mm,product.default_cardboard_length,product.default_cardboard_width]}
        if product.id==3807:
            quote=db.execute(text("select * from external_packaging_price_versions where id=6 and external_product_id=3")).mappings().one()
            purchase=db.execute(text("select * from external_packaging_purchase_items where id=14 and customer_product_id_snapshot=3807")).mappings().one()
            factor=Decimal(str(purchase['purchase_quantity_basis_snapshot']))/Decimal(str(purchase['order_quantity_basis_snapshot']))
            assert factor==2 and quote['quote_unit']==purchase['purchase_unit'] and quote['currency']=='CNY'
            evidence.update(external_price_id=quote['id'],external_quote_fingerprint=quote['quote_fingerprint'],conversion_source_purchase_item_id=14,pieces_per_finished=factor,price_unit=quote['quote_unit'],currency=quote['currency'],tax_mode=quote['tax_mode'],tax_rate=quote['tax_rate'])
            estimate={"unit_cost":str((Decimal(str(quote['unit_price']))*factor).quantize(Decimal('.0001'))),"square_price":None,"area_m2":None,"source":"owner_current_external_reference","detail":evidence}
        else:
            attrs={col.key:getattr(product,col.key) for col in Product.__table__.columns}
            inferred=SimpleNamespace(**attrs)
            inferred.material=db.get(Material,product.material_id) if product.material_id else None
            if product.id in MATERIALS:
                mid,peer=MATERIALS[product.id];inferred.material_id=mid;inferred.material=db.get(Material,mid);evidence.update(inferred_material_id=mid,reference_product_id=peer)
                if peer:
                    source=db.get(Product,peer);evidence['reference_product_version']=source.version
                    assert source.material_id==mid
                inferred.layer_count=inferred.material.layer_count
                inferred.flute_type=product.flute_type or (db.get(Product,peer).flute_type if peer else None) or inferred.material.flute_type
            if product.id in SPECIAL:
                length,width,pieces,note=SPECIAL[product.id];inferred.report_length_mm=length;inferred.report_width_mm=width;inferred.pieces_per_box=pieces;inferred.box_style="异形箱";evidence['dimension_inference']=note
            elif product.id in FORMULA:
                result=recommend_box_type(box_style="A1/0201 普通开槽箱",length_mm=int(product.length_mm),width_mm=int(product.width_mm),height_mm=int(product.height_mm),splice_mode=FORMULA[product.id])
                inferred.report_length_mm=result['report_length_mm'];inferred.report_width_mm=result['report_width_mm'];inferred.pieces_per_box=2 if FORMULA[product.id]=='double' else 1;inferred.box_style="A1/0201 普通开槽箱";evidence['dimension_inference']=result
            elif not product.report_length_mm and product.default_cardboard_length and product.default_cardboard_width:
                inferred.report_length_mm=int(product.default_cardboard_length*10);inferred.report_width_mm=int(product.default_cardboard_width*10);evidence['dimension_inference']='旧常用箱厘米报料换算mm，仅成本快照采用'
            result=estimate_finished_product_cost(db,product=inferred)
            if result is None or result.unit_cost<=0:
                unresolved.append({**base,"reason":"仍缺有效材质/规格/单价"});continue
            evidence.update(result.detail)
            material=inferred.material
            evidence.update(material_version=material.version,currency=material.purchase_currency,tax_included=material.purchase_tax_included,tax_rate=str(material.purchase_tax_rate),box_style_for_cost=inferred.box_style)
            estimate={"unit_cost":str(result.unit_cost),"square_price":str(result.square_price),"area_m2":str(result.area_m2),"source":"owner_current_reference_backfill","detail":evidence}
        rows.append({**base,"estimate":estimate})
    return {"task":TASK,"rows":rows,"unresolved":unresolved,"plan_hash":digest(rows)}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--database',required=True);parser.add_argument('--output',required=True);parser.add_argument('--apply-plan');parser.add_argument('--backup-reference');args=parser.parse_args()
    path=Path(args.database).resolve(); read_only=not args.apply_plan
    connection=sqlite3.connect(f"file:{path.as_posix()}?mode={'ro' if read_only else 'rw'}",uri=True)
    connection.execute('pragma foreign_keys=on')
    engine=create_engine('sqlite://',creator=lambda:connection)
    with Session(engine) as db:
        if read_only: result=plan(db)
        else:
            approved=json.loads(Path(args.apply_plan).read_text(encoding='utf-8'))
            assert approved['task']==TASK and digest(approved['rows'])==approved['plan_hash']
            assert args.backup_reference and Path(args.backup_reference).is_file()
            with sqlite3.connect(f"file:{Path(args.backup_reference).as_posix()}?mode=ro",uri=True) as backup:
                assert backup.execute('pragma integrity_check').fetchall()==[('ok',)] and not backup.execute('pragma foreign_key_check').fetchall()
            db.execute(text('BEGIN IMMEDIATE'))
            if db.scalar(select(OperationLog.id).where(OperationLog.batch_id==TASK)):
                print('already applied');db.rollback();return
            current=plan(db,allowed_ids={row['lot_id'] for row in approved['rows']})
            assert current['plan_hash']==approved['plan_hash'], 'Sources changed; rebuild and rehearse plan'
            before_changes=connection.total_changes
            for row in approved['rows']:
                lot=db.get(InventoryLot,row['lot_id']); data=row['estimate']
                lot.estimated_unit_cost_snapshot=Decimal(data['unit_cost']);lot.estimated_square_price_snapshot=Decimal(data['square_price']) if data['square_price'] else None;lot.estimated_cost_area_m2_snapshot=Decimal(data['area_m2']) if data['area_m2'] else None
                lot.cost_snapshot_source=data['source'];lot.cost_snapshot_detail_json=json.dumps(data['detail'],ensure_ascii=False,sort_keys=True,default=str);lot.cost_snapshot_at=datetime.utcnow();lot.version+=1
            result={**approved,'backup_reference':args.backup_reference,'applied_count':len(approved['rows'])}
            db.add(OperationLog(user_id=None,username='Codex owner-authorized maintenance',action='cost_backfill',resource='InventoryLot',description='老板2026-09-11授权缺价库存按现有资料推算本次参考成本，逐批保留依据；不修改数量和历史采购价。',details=json.dumps(result,ensure_ascii=False,default=str),event_category='data_change',result='success',source='admin_script',module_code='warehouse',action_code='inventory.cost_backfill',operator_name_snapshot='Codex（老板本次授权）',object_ref=TASK,batch_id=TASK,schema_version=1))
            db.flush();assert connection.total_changes-before_changes==len(approved['rows'])+1
            assert not db.execute(text('pragma foreign_key_check')).all();db.commit()
        Path(args.output).write_text(json.dumps(result,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
        print(json.dumps({'planned':len(result['rows']),'unresolved':len(result['unresolved']),'hash':result['plan_hash']},ensure_ascii=False))

if __name__=='__main__':main()
