"""One explicit snapshot, in-memory transaction only; no attachment access."""
import json,os,sqlite3
from pathlib import Path
import pytest
from sqlalchemy import create_engine,text
from sqlalchemy.orm import Session
from app.models import *
from app.services import shared_customer_access as access
from scripts.admin.confirm_shared_12139_access import plan,apply


def test_reviewed_12139_snapshot_in_memory():
    name=os.environ.get('ERP_12139_READONLY_SNAPSHOT')
    if not name:pytest.skip('explicit read-only snapshot required')
    source=sqlite3.connect(Path(name).as_uri()+'?mode=ro',uri=True)
    source.execute('PRAGMA query_only=ON')
    memory=sqlite3.connect(':memory:',check_same_thread=False);source.backup(memory);source.close()
    engine=create_engine('sqlite://',creator=lambda:memory)
    with Session(engine) as db:
        db.execute(text('PRAGMA foreign_keys=ON'))
        value,_=plan(db)
        result=apply(db,expected_hash=value['preview_hash'],backup_receipt={'verified':True,'sha256':'a'*64})
        db.commit()
        assert apply(db,expected_hash=value['preview_hash'],backup_receipt={'verified':True,'sha256':'a'*64})['replayed']
        assert not db.execute(text('PRAGMA foreign_key_check')).fetchall()
        out=Path('/Users/kimiyf/ERP-Migration/evidence/shared-stock-12139-20261017')
        (out/'reviewed-data-plan.json').write_text(access.canonical(value))
        (out/'isolated-data-validation.json').write_text(access.canonical(result))

        # Exercise both raw-unit masters against the same ledger in memory.
        from datetime import date
        from decimal import Decimal
        from sqlalchemy import select
        from app.models.order import Order,OrderItem
        from app.models.product import Product
        from app.models.user import User
        from app.models.warehouse_inventory import InventoryLot
        from app.services import shared_finished_stock as shared
        from app.services.shared_finished_receipts import PAIRS
        from app.services.box_type_rules import order_snapshot_box_configuration
        from app.services.warehouse_inventory import reserve_finished_inventory,WarehouseInventoryError
        admin=db.scalar(select(User).where(User.role=='admin',User.is_active.is_(True)))
        lot=db.get(InventoryLot,1140);identity=shared.lot_identity(lot);items=[]
        for pid,cid in ((3886,143),(3567,137)):
            product=db.get(Product,pid);basis=json.loads(shared.product_basis(product))
            order=Order(order_number='ISOLATED-12139-'+str(pid),customer_id=cid,
                order_date=date(2026,10,17),status='pending_production',payment_status='unpaid',total_amount=200)
            db.add(order);db.flush()
            item=OrderItem(order_id=order.id,product_id=pid,quantity=200,unit_price=1,subtotal=200,
                material_status='pending',requisition_status='未报料',snapshot_product_code=product.product_code,
                snapshot_product_name=product.product_name,snapshot_spec=basis['spec'],
                supply_mode_snapshot='corrugated_production',combination_role='standalone')
            for field,key in PAIRS.items():setattr(item,field,basis.get(key))
            config=order_snapshot_box_configuration(product)
            for key in ('splice_mode','pieces_per_box','flap_mm'):setattr(item,'snapshot_'+key,config[key])
            item.special_process=config['default_cutting_mode'];item.sales_unit_snapshot='片'
            for key in ('report_length_mm','report_width_mm','base_report_length_mm','base_report_width_mm'):
                setattr(item,'snapshot_'+key,getattr(product,key))
            item.sheet_cutting_settings_snapshot=product.sheet_cutting_settings
            db.add(item);db.flush();items.append(item)
        db.commit()
        def reserve(item,qty,key):
            return reserve_finished_inventory(db,order_item_id=item.id,inventory_lot_id=1140,quantity=qty,
                expected_version=lot.version,operator_id=admin.id,idempotency_key=key,warning_acknowledged_codes=[])
        reserve(items[0],100,'isolated-overseas-100');db.commit()
        assert (lot.quantity_available,lot.quantity_reserved)==(190,100)
        with pytest.raises(WarehouseInventoryError):reserve(items[1],200,'isolated-no-double-use')
        db.rollback()
        reserve(items[1],190,'isolated-yg-190');db.commit()
        assert (lot.quantity_available,lot.quantity_reserved)==(0,290)
        assert shared.lot_identity(lot)==identity
        assert db.get(Product,3567).unit=='只' and db.get(Product,3886).unit=='片'
        (out/'isolated-two-customer-reservations.json').write_text(json.dumps({'passed':True,
            'overseas_reserved':100,'yangguang_reserved':190,'available':0,'reserved':290,
            'overbooking_blocked':True,'raw_units_and_lot_identity_preserved':True}))
