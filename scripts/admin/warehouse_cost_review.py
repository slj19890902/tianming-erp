"""Read-only cost source review; no global application database engine."""
import argparse
import json
import sqlite3
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from app.models.product import Product
from app.models.order import OrderItem, Order
from app.models.warehouse_inventory import InventoryLot
from app.services.inventory_valuation import positive, resolve_lot_cost

def main():
    parser=argparse.ArgumentParser();parser.add_argument("database");args=parser.parse_args()
    path=Path(args.database).resolve()
    def connect():
        con=sqlite3.connect(path.as_uri()+"?mode=ro",uri=True)
        con.execute("PRAGMA query_only=ON");con.execute("BEGIN");return con
    engine=create_engine("sqlite://",creator=connect)
    with Session(engine,autoflush=False) as db:
        lots=list(db.scalars(select(InventoryLot).where(InventoryLot.quantity_available+InventoryLot.quantity_reserved+InventoryLot.quantity_damaged>0)))
        missing,ready=[],[]
        for lot in lots:
            if positive(lot.estimated_unit_cost_snapshot):continue
            result=resolve_lot_cost(db,lot)
            if result.estimate:
                ready.append(dict(lot_id=lot.id,unit_cost=str(result.estimate.unit_cost)))
                continue
            p=db.get(Product,lot.finished_detail.product_id) if lot.finished_detail else None
            record=dict(lot_id=lot.id,reason=result.missing)
            if p:
                record.update(product_id=p.id,code=p.product_code,name=p.product_name,box_style=p.box_style,
                    dims=[str(p.length_mm),str(p.width_mm),str(p.height_mm)],material_id=p.material_id,legacy_material=p.legacy_material_text)
                orders=list(db.scalars(select(OrderItem).join(Order,Order.id==OrderItem.order_id).where(OrderItem.product_id==p.id,
                    Order.status.not_in(["cancelled","dead"])).order_by(Order.order_date.desc(),OrderItem.id.desc()).limit(2)))
                record["orders"]=[dict(id=o.id,material_id=o.material_id,supplier=o.snapshot_supplier_name,material=o.snapshot_material,
                    report=[o.snapshot_report_length_mm,o.snapshot_report_width_mm],spec=o.snapshot_spec,
                    cardboard=[str(o.cardboard_len),str(o.cardboard_width)]) for o in orders]
            missing.append(record)
        print(json.dumps(dict(total=len(lots),ready=ready,missing=missing),ensure_ascii=True,default=str))
        db.rollback()
if __name__=="__main__":main()
