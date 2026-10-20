from decimal import Decimal
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_stock_preparation_finish import prepared, stock_replenishment_app
from app.api.stock_preparation import router
from app.models.warehouse_inventory import InventoryLot
from app.services.inventory_cost_listing import read_inventory_cost_page
from app.services.inventory_valuation import cost_payload
from app.services.warehouse_inventory import transfer_finished_lot_between_locations


def test_converted_and_split_stock_summary_matches_fully_loaded_cost(stock_replenishment_app, monkeypatch):
    # Historical manual-processing receipts predate automatic completion.
    from app.services import stock_preparation_processing
    monkeypatch.setattr(stock_preparation_processing, 'auto_complete_receipt', stock_preparation_processing.auto_plan_receipt)
    app,factory=stock_replenishment_app
    app.include_router(router,prefix='/api/production')
    with TestClient(app) as client:
        receipt_id,_,body=prepared(client)
        response=client.post(f'/api/production/stock-preparation/{receipt_id}/finish-output',json=body)
        assert response.status_code==200,response.text
        lid=response.json()['output_lot_id']
    with factory() as db:
        for split in (False,True):
            output=db.get(InventoryLot,lid)
            if split:
                output=transfer_finished_lot_between_locations(db,lot_id=lid,expected_version=output.version,
                    quantity=2,location_id=8,operator_id=1,idempotency_key='converted-cost-split',expected_target_layout_version=1).target_lot
                db.commit()
            expected=cost_payload(output,db)
            assert expected['inventory_value'] is not None
            page=read_inventory_cost_page(db,select(InventoryLot.id).where(InventoryLot.id==output.id))
            assert page['missing_lots']==0
            assert Decimal(page['inventory_value'])==Decimal(expected['inventory_value'])
            assert page['rows'][0]['unit_cost']==expected['unit_cost']
