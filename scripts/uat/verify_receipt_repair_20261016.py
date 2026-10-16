"""Rollback-only rehearsal of the three old jobs and one physical putaway."""
import json
import os
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import create_sqlite_engine
from app.models.incoming_receipt import IncomingReceiptItem
from app.models.stock_preparation import StockPreparationJob
from app.models.user import User
from app.models.warehouse_inventory import InventoryLot, WarehouseLocation
from app.services import stock_preparation as preparation
from app.services.production_workflow import _receipt_auto_finished_ground_target
from app.services.warehouse_inventory import transfer_finished_lot_between_locations


def main():
    database=Path(os.environ['ERP_DATABASE_PATH']).resolve()
    assert os.environ.get('ERP_ENVIRONMENT')=='test' and os.environ.get('ERP_UAT_ISOLATION_ID')
    assert '/Library/TianmingERP/' not in str(database)
    engine=create_sqlite_engine(database)
    with Session(engine,autoflush=True,expire_on_commit=False) as db:
        actor=db.get(User,1)
        assert actor and actor.role in {'admin','boss'}
        results=[]
        for job_id,expected_product,expected_input,expected_output in (
                (30,3411,3000,6000),(29,3406,200,200),(28,3407,400,400)):
            job=db.get(StockPreparationJob,job_id)
            assert job and job.status=='pending' and job.product_id==expected_product
            assert (job.input_quantity,job.expected_output)==(expected_input,expected_output)
            receipt=db.get(IncomingReceiptItem,job.receipt_item_id)
            source=db.get(InventoryLot,receipt.received_inventory_lot_id)
            target=_receipt_auto_finished_ground_target(db,claim=False,
                customer_id=137,product_id=expected_product)
            payload=dict(action='complete',operation_key=f'old-receipt-repair-20261016-job-{job_id}',
                lot_version=source.version,job_id=job.id,job_version=job.version,
                actual_output=expected_output,confirm_overproduction=False,
                location_id=target.location.id,layout_version=target.layout_version)
            result=preparation.mutate(db,receipt_id=receipt.id,payload=payload,actor=actor)
            output=db.get(InventoryLot,db.get(StockPreparationJob,job_id).output_lot_id)
            assert output and output.inventory_type=='finished' and output.quantity_available==expected_output
            assert output.warehouse_location_id==target.location.id
            results.append(dict(job_id=job_id,result=result,output_lot_id=output.id,
                output_quantity=output.quantity_available,target_location_id=target.location.id))
        lot=db.get(InventoryLot,1465)
        target=db.get(WarehouseLocation,1620)
        source=db.get(WarehouseLocation,1300)
        assert lot and lot.inventory_type=='finished' and lot.warehouse_location_id==source.id
        assert lot.quantity_available==4430 and lot.quantity_reserved==0 and lot.quantity_consumed==570
        moved=transfer_finished_lot_between_locations(db,lot_id=lot.id,
            expected_version=lot.version,quantity=4430,location_id=target.id,
            operator_id=actor.id,idempotency_key='old-receipt-putaway-20261016-1465',
            expected_source_location_id=source.id,
            expected_source_address_version=source.address_version,
            expected_source_layout_version=source.floor3_layout.version,
            expected_target_address_version=target.address_version,
            expected_target_layout_version=target.floor3_layout.version)
        results.append(dict(lot_id=1465,moved_lot_id=moved.target_lot.id,
            target_location_id=moved.target_lot.warehouse_location_id,
            moved_quantity=moved.target_lot.quantity_available))
        db.flush()
        print(json.dumps({'status':'rehearsed_rolled_back','results':results},ensure_ascii=False,default=str))
        db.rollback()
    engine.dispose()


if __name__=='__main__':
    main()
