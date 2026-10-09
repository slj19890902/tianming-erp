"""End-to-end regressions discovered by the 2026-10-09 production audit."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from test_stock_replenishment_flow import stock_replenishment_app
from test_stock_processing_auto import completion, receive, row
from test_stock_preparation_groups import prepare
from app.api.stock_preparation import router
from app.models.stock_preparation import StockPreparationJob as Job, StockPreparationCommand as Command
from app.models.warehouse_inventory import InventoryLot, WarehouseLocation


def test_independent_assembled_bom_history_is_readable(stock_replenishment_app):
    from app.api.production import router as production_router
    from app.models.multilevel_bom import ProductBomProfile, ProductBomInventoryRelation
    from app.models.product_bom import ProductBomComponent
    from app.models.customer import Customer

    app, factory = stock_replenishment_app
    app.include_router(router, prefix='/api/production')
    app.include_router(production_router, prefix='/api/production')
    with TestClient(app, raise_server_exceptions=False) as client:
        pid = prepare(app, factory, client)
        with factory() as db:
            db.add(ProductBomProfile(product_id=pid, source='assembled'))
            for edge in db.scalars(select(ProductBomComponent).where(ProductBomComponent.parent_product_id == pid)):
                db.add(ProductBomInventoryRelation(bom_component_id=edge.id, relation='assembly'))
            customer = db.get(Customer, 1)
            customer_name = customer.chinese_short_name or customer.name
            db.commit()
        for source in client.get('/api/production/stock-preparation').json()['items']:
            result = client.post(f"/api/production/stock-preparation/{source['receipt_item_id']}/actions",
                                 json=completion(source, 30, f"audit-process-{source['receipt_item_id']}"))
            assert result.status_code == 200, result.text
        preview = client.get(f'/api/production/stock-preparation/assembly/{pid}/preview', params={'sets': 5})
        assert preview.status_code == 200, preview.text
        plan = preview.json()
        assembled = client.post('/api/production/stock-preparation/group-actions', json=dict(
            action='assemble_stock', parent_id=pid, sets=5, basis_hash=plan['basis_hash'], jobs=plan['sources'],
            location_id=7, layout_version=1, operation_key='audit-independent-assemble'))
        assert assembled.status_code == 200, assembled.text
        # Older successful commands did not freeze a customer label. Reading
        # them must remain possible without mutating that append-only record.
        with factory() as db:
            saved = db.get(Command, 'audit-independent-assemble').result_json
        response = client.get('/api/production/completions', params={'include_stock': True, 'record_type': 'stock_assembly'})
        assert response.status_code == 200, response.text
        assert response.json()['total'] == 1
        history = response.json()['items'][0]
        assert history['customer_name'] == customer_name
        assert history['actual_output_quantity'] == 5
        with factory() as db:
            assert db.get(Command, 'audit-independent-assemble').result_json == saved


@pytest.mark.parametrize('source_change', ['disabled', 'unplaced', 'retired_pallet'])
def test_reverse_processing_rejects_unusable_original_storage(stock_replenishment_app, source_change):
    from app.models.warehouse_inventory import InventoryPallet, InventoryPalletItem
    from app.services.stock_preparation_history import rows
    from app.services.stock_preparation import source as original_source

    app, factory = stock_replenishment_app
    app.include_router(router, prefix='/api/production')
    with TestClient(app) as client:
        receive(client)
        source = row(client)
        response = client.post(f"/api/production/stock-preparation/{source['receipt_item_id']}/actions",
                               json=completion(source, 20, 'audit-complete-all'))
        assert response.status_code == 200, response.text
        with factory() as db:
            job = db.get(Job, response.json()['completed_job_id'])
            _, _, material = original_source(db, job.receipt_item_id)
            history = rows(db)[0]
            payload = dict(operation_key=f'audit-reverse-{source_change}', confirm_unused=True,
                           jobs=history['reverse_versions'])
            if source_change == 'retired_pallet':
                if material.pallet_item:
                    pallet = material.pallet_item.pallet
                else:
                    pallet = InventoryPallet(pallet_code='AUDIT-RETIRED-RAW', location_id=material.warehouse_location_id,
                                             is_current=False, status='closed')
                    db.add(pallet)
                    db.flush()
                    db.add(InventoryPalletItem(pallet_id=pallet.id, inventory_lot_id=material.id,
                                              item_type='semi_finished', quantity=20, unit='张'))
                pallet.is_current = False
                pallet.status = 'closed'
            else:
                place = db.get(WarehouseLocation, material.warehouse_location_id)
                if source_change == 'disabled':
                    place.is_active = False
                else:
                    place.placement_status = 'unplaced'
            db.commit()
            expected = {lot.id: (lot.quantity_available, lot.quantity_reserved, lot.quantity_consumed, lot.version)
                        for lot in db.scalars(select(InventoryLot))}
        rejected = client.post('/api/production/stock-preparation/completions/' + history['preparation_key'] + '/revert', json=payload)
        assert rejected.status_code == 409, rejected.text
        with factory() as db:
            assert db.get(Job, response.json()['completed_job_id']).status == 'completed'
            assert db.get(Command, payload['operation_key']) is None
            assert {lot.id: (lot.quantity_available, lot.quantity_reserved, lot.quantity_consumed, lot.version)
                    for lot in db.scalars(select(InventoryLot))} == expected
            assert not rows(db)[0]['can_revert']
