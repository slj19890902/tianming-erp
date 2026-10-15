"""Real commit guards must protect both stock assembly entry points."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from app.core.receipt_price_guard import ReceiptPriceGuardSession
from app.models.warehouse_inventory import InventoryLot
from test_stock_preparation_assembly_recovery_api import setup, URL
from test_stock_preparation_group_recovery_api import grouped, facts
from stock_preparation_legacy_fixture import stock_replenishment_app, base_stock_replenishment_app


@pytest.mark.parametrize('missing_timestamp', [False, True])
def test_group_assembly_freezes_time_and_rolls_back_missing_evidence(grouped, monkeypatch, missing_timestamp):
    app, factory = grouped
    with TestClient(app) as client:
        body = setup(client, app, factory)
        factory.class_ = ReceiptPriceGuardSession
        before = facts(factory)
        from app.services import stock_preparation_disposition as service
        original = service.assemble
        if missing_timestamp:
            def faulty(db, *args, **kwargs):
                result = original(db, *args, **kwargs)
                db.get(InventoryLot, result['output_lot_id']).cost_snapshot_at = None
                return result
            monkeypatch.setattr(service, 'assemble', faulty)
        first = client.post(URL, json=body)
        if missing_timestamp:
            assert first.status_code == 422, first.text
            assert '成本冻结时间' in first.json()['detail']['missing']
            assert '系统维护人员' in first.json()['detail']['message']
            assert facts(factory) == before
            monkeypatch.setattr(service, 'assemble', original)
            first = client.post(URL, json=body)
        assert first.status_code == 200, first.text
        with factory() as db:
            output = db.get(InventoryLot, first.json()['output_lot_id'])
            assert output.cost_snapshot_at is not None
            assert output.cost_snapshot_source == 'stock_preparation_assembly'
        after = facts(factory)
        assert client.post(URL, json=body).json() == first.json()
        assert facts(factory) == after
