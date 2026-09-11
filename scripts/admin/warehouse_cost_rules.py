"""Exact manifest only; default read-only, verified backup and fingerprint required for writes."""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from sqlalchemy import select
from sqlalchemy.orm import Session
from scripts.admin.warehouse_cost_backfill import engine_for
from app.models.product import Product
from app.models.user import User
from app.models.inventory_cost_rule import InventoryCostMutation
from app.models.warehouse_inventory import InventoryLot
from app.services import inventory_cost_rules as rules
from app.services.inventory_cost_backfill import COST_FIELDS
from app.services.material_cost_supplement import canonical, fingerprint


def preview_manifest(db, manifest):
    if not manifest.get('authorization') or manifest.get('unmatched'):
        raise ValueError('缺少授权或存在未核对清单')
    rows=[];ids=set();lot_ids=set()
    for item in manifest['rules']:
        pid=item['product_id']
        if pid in ids:raise ValueError('清单产品ID重复')
        ids.add(pid)
        p=db.get(Product,pid)
        if not p or p.deleted_at is not None or any(getattr(p,k)!=item[k] for k in ('customer_id','product_code','product_name')) or p.version!=item['product_version']:
            raise ValueError(f'产品身份或版本已变化：{pid}')
        before=rules.rule_payload(db,p)
        config=rules.config_dict(item['config'])
        after_version=before['version']+(0 if before['version'] and canonical(config)==canonical(before['config']) else 1)
        result=rules.estimate_rule(db,p,config,version=after_version)
        if not result or not result.estimate:raise ValueError(f'产品{pid}成本不完整：{result.missing if result else "不允许自动模式批量补定"}')
        e=result.estimate;lots=[]
        for lid in item['lot_ids']:
            if lid in lot_ids:raise ValueError('清单批次重复')
            lot_ids.add(lid)
            lot=db.get(InventoryLot,lid)
            if not lot or not lot.finished_detail or lot.finished_detail.product_id!=pid or not rules.is_revaluable(db,lot):
                raise ValueError(f'批次来源或绑定不符：{lid}')
            qty=lot.quantity_available+lot.quantity_reserved+lot.quantity_damaged
            if qty<=0:raise ValueError(f'批次不在库：{lid}')
            lots.append(dict(lot_id=lid,version=lot.version,quantity=qty,location_id=lot.warehouse_location_id,
                before={f:str(getattr(lot,f)) if getattr(lot,f) is not None else None for f in COST_FIELDS}))
        rows.append(dict(product_id=pid,before_rule=before,config=config,after_version=after_version,lots=lots,
            unit_cost=str(e.unit_cost),square_price=str(e.square_price),area_m2=str(e.area_m2),evidence=e.detail))
    plan=dict(manifest_fingerprint=fingerprint(manifest),rules=rows,rule_count=len(rows),lot_count=len(lot_ids))
    return {**plan,'fingerprint':fingerprint(plan)}


def adopted(db, manifest, expected, batch):
    prior=db.get(InventoryCostMutation,batch)
    if not prior:return None
    if prior.request_json!=canonical(dict(manifest_fingerprint=fingerprint(manifest),expected=expected)):
        raise ValueError('同一批次不得更换清单或预览')
    return {**json.loads(prior.response_json),'replayed':True}


def adopt_manifest(db, manifest, *, expected, batch, user):
    if not user or not user.is_active or user.role!='admin':raise PermissionError('仅有效管理员可执行')
    old=adopted(db,manifest,expected,batch)
    if old:return old
    plan=preview_manifest(db,manifest)
    if plan['fingerprint']!=expected:raise ValueError('清单、库存或报价已变化，禁止写入')
    results=[]
    for item in plan['rules']:
        p=db.get(Product,item['product_id'])
        rules.save_rule(db,p,item['config'],user=user,expected_version=item['before_rule']['version'],expected_product_version=p.version)
        ids=[l['lot_id'] for l in item['lots']]
        if ids:
            current=rules.preview_revalue(db,p,ids)
            result=rules.revalue(db,p,ids,user=user,expected=current['fingerprint'],batch_id=f'{batch}:{p.id}')
            results.append({k:v for k,v in result.items() if k!='plan'})
    result=dict(rule_count=plan['rule_count'],lot_count=plan['lot_count'],fingerprint=expected,results=results,replayed=False)
    db.add(InventoryCostMutation(batch_id=batch,product_id=plan['rules'][0]['product_id'],
        request_json=canonical(dict(manifest_fingerprint=fingerprint(manifest),expected=expected)),response_json=canonical(result),created_by=user.id))
    db.flush()
    return result


def run():
    parser=argparse.ArgumentParser()
    parser.add_argument('database',type=Path);parser.add_argument('manifest',type=Path)
    parser.add_argument('--apply',action='store_true');parser.add_argument('--expected');parser.add_argument('--operator',type=int)
    parser.add_argument('--batch',default='owner-cost-rules-20260911');parser.add_argument('--backup',type=Path);parser.add_argument('--report',type=Path)
    args=parser.parse_args();path=args.database.resolve()
    if not path.is_file():raise ValueError('数据库不存在')
    if len(args.batch)>40 or not args.batch.strip():raise ValueError('批次标记无效')
    manifest=json.loads(args.manifest.read_text(encoding='utf-8'));backup_info=None
    with Session(engine_for(path,True),autoflush=False) as db:
        db.connection().exec_driver_sql('BEGIN')
        replay=adopted(db,manifest,args.expected,args.batch) if args.apply else None
        plan=preview_manifest(db,manifest) if not replay else replay
    if args.apply and not replay:
        if not args.expected or not args.operator or not args.backup:raise ValueError('缺少预览指纹、操作人或新备份路径')
        if plan['fingerprint']!=args.expected:raise ValueError('正式预览已变化，未备份/未写入')
        backup=args.backup.resolve()
        if backup.exists() or backup==path:raise ValueError('备份须为新的独立文件')
        backup.parent.mkdir(parents=True,exist_ok=True)
        with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as src,sqlite3.connect(str(backup)) as dst:
            src.backup(dst)
            if dst.execute('PRAGMA integrity_check').fetchall()!=[('ok',)] or dst.execute('PRAGMA foreign_key_check').fetchall():raise ValueError('备份校验失败')
        with Session(engine_for(backup,True),autoflush=False) as db:
            if preview_manifest(db,manifest)['fingerprint']!=args.expected:raise ValueError('备份指纹不符')
        backup_info=dict(path=str(backup),sha256=hashlib.sha256(backup.read_bytes()).hexdigest(),integrity='ok',foreign_keys=0)
        with engine_for(path,False).connect() as con:
            con.exec_driver_sql('BEGIN IMMEDIATE')
            try:
                with Session(bind=con,autoflush=False) as db:
                    plan=adopt_manifest(db,manifest,expected=args.expected,batch=args.batch,user=db.get(User,args.operator))
                    if con.exec_driver_sql('PRAGMA foreign_key_check').fetchall():raise ValueError('外键异常')
                    con.commit()
            except Exception:
                con.rollback();raise
    output=dict(database=str(path),applied=args.apply,backup=backup_info,result=plan)
    if args.report:args.report.write_text(json.dumps(output,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
    print(json.dumps({**output,'result':{k:v for k,v in plan.items() if k!='rules'}},ensure_ascii=False,default=str))


if __name__=='__main__':run()
