"""Owner-approved first-floor pending anchors; no inventory/map file rewrites."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
from datetime import datetime

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def configure(db, operator_id):
    from sqlalchemy import select
    from app.models.receipt_putaway import ReceiptStagingArea, ProductStoragePreference
    from app.models.user import User
    from app.models.warehouse_inventory import WarehouseArea, WarehouseLocation, InventoryLot
    from app.services.warehouse_area_activation import set_area_location_active
    from app.services.receipt_putaway import location_issue
    from app.services.audit_log import append_audit_event
    actor = db.get(User, operator_id)
    if not actor or actor.role not in {"admin", "boss"}:
        raise ValueError("需要正式管理员身份记录配置审计")
    report = []
    for aid, code in [(29, "RAW-002"), (30, "RAW-003"), (42, "RAW-005"), (44, "RAW-006")]:
        area = db.get(WarehouseArea, aid)
        if not area or area.area_code != code or area.floor.floor_number != 1:
            raise ValueError(f"现场区域身份不一致：{aid}/{code}")
        policy = area.storage_policy
        if not policy or policy.status != "published" or area.construction_status != "enabled":
            raise ValueError(f"区域未发布：{code}")
        rows = db.scalars(select(WarehouseLocation).where(WarehouseLocation.warehouse_floor == 1,
            WarehouseLocation.area_code == code).order_by(WarehouseLocation.id)).all()
        if not rows or any(x.source_version != "CURRENT_MAP" or x.address_kind != "functional"
                or x.storage_type != "temporary_aisle" or not x.floor3_layout
                or x.floor3_layout.layout_kind != "logical_anchor" for x in rows):
            raise ValueError(f"不是已核对的原料暂存锚点：{code}")
        before = {"name":area.area_name,"policy":policy.allowed_inventory_types_json,
            "locations":[{"id":x.id,"active":x.is_active,"type":x.warehouse_type} for x in rows]}
        if db.get(ReceiptStagingArea, aid) is None:
            db.add(ReceiptStagingArea(area_id=aid))
        policy.allowed_inventory_types_json = '["finished","semi_finished","raw_material"]'
        for row in rows:
            if not row.is_active:
                set_area_location_active(db, location_id=row.id, is_active=True,
                    expected_version=row.floor3_layout.version, operator_id=operator_id)
            row.warehouse_type = "shared"
        area.area_name = f"待入库区 {code.removeprefix('RAW-')}"
        db.flush()
        for row in rows:
            for kind in ("finished", "semi_finished"):
                issue = location_issue(db, row, kind)
                if issue:
                    raise ValueError(f"{row.id}: {issue}")
        report.append({"area_id":aid,"code":code,"location_ids":[x.id for x in rows]})
        append_audit_event(db,event_category="business",result="success",source="system",module_code="warehouse",
            action_code="warehouse.receipt_staging.restore",resource="warehouse_area",actor=actor,
            object_ref=str(aid),details={"authorization":"老板2026-09-12确认恢复一楼原料区作待入库区", "before":before,"after":report[-1]})
    from app.services.receipt_putaway import remember_stocktake
    before_products=set(db.scalars(select(ProductStoragePreference.product_id)).all())
    lots=db.scalars(select(InventoryLot).join(WarehouseLocation,WarehouseLocation.id==InventoryLot.warehouse_location_id).where(
        InventoryLot.source_type=='stocktake',InventoryLot.inventory_type=='finished',InventoryLot.status=='active',
        WarehouseLocation.storage_type=='rack',WarehouseLocation.is_active.is_(True),
        InventoryLot.quantity_available+InventoryLot.quantity_reserved+InventoryLot.quantity_damaged>0)).all()
    for lot in lots:
        remember_stocktake(db,lot,operator_id)
    added=sorted(set(db.scalars(select(ProductStoragePreference.product_id)).all())-before_products)
    return {"areas":report,"remembered_product_ids":added}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--database",required=True)
    parser.add_argument("--operator-id",type=int,required=True)
    parser.add_argument("--apply",action="store_true")
    parser.add_argument("--allow-formal",action="store_true")
    args=parser.parse_args()
    path=Path(args.database).resolve(strict=True)
    if args.apply and path == Path("D:/纸箱厂erp软件搭建/data/carton_erp.sqlite3").resolve() and not args.allow_formal:
        raise SystemExit("正式写入必须显式 --allow-formal，使用已批准发布窗口")
    os.environ["ERP_DATABASE_PATH"]=str(path)
    if args.apply:
        backup=path.with_name(path.stem + '.before-receipt-staging-' + datetime.now().strftime('%Y%m%d%H%M%S') + '.sqlite3')
        with sqlite3.connect(f"file:{path.as_posix()}?mode=ro",uri=True) as source, sqlite3.connect(backup) as dest:
            source.backup(dest)
            if dest.execute('pragma integrity_check').fetchone()[0]!='ok' or dest.execute('pragma foreign_key_check').fetchall():
                raise SystemExit("备份检查不通过")
        print(json.dumps({"backup":str(backup),"sha256":hashlib.sha256(backup.read_bytes()).hexdigest()}))
    from app.core.database import SessionLocal
    with SessionLocal() as db:
        report=configure(db,args.operator_id)
        from sqlalchemy import text
        if db.execute(text('pragma foreign_key_check')).all():
            raise ValueError("外键检查失败")
        if args.apply:db.commit()
        else:db.rollback()
        print(json.dumps({"applied":args.apply,"areas":report},ensure_ascii=False))


if __name__=='__main__': main()
