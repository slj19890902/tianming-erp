from decimal import Decimal
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, func

from app.models.external_packaging_purchase import ExternalPackagingPurchaseItem, ExternalPackagingReceipt
from app.models.multilevel_bom import OrderBomExternalComponent, BomAssembly, BomAssemblyInput
from app.models.warehouse_inventory import InventoryLot
from app.models.production import ProductionCompletion, ProductionTask
from app.services.bom_subkits import SubkitError
from test_p1_33c3_external_packaging_purchase_confirmation import purchase_app
from tests.test_multilevel_bom_external_receipts import prepare, receive, _login, _confirm
from tests.test_p1_81_receipt_purpose_flow import _seed_material_and_staging, _p181_published_map_identity


@pytest.mark.parametrize('fault', [False, True])
def test_two_real_external_children_assemble_atomically_and_reserve_output(purchase_app, _p181_published_map_identity, monkeypatch, fault):
    factory = purchase_app.state.session_factory
    _seed_material_and_staging(factory)
    order_id, item_id, first_pid = prepare(purchase_app, two=True)
    with TestClient(purchase_app) as client:
        _login(client, 'purchase-admin')
        _confirm(client, order_id)
        with factory() as db:
            rows = list(db.scalars(select(ExternalPackagingPurchaseItem).where(ExternalPackagingPurchaseItem.sales_order_item_id == item_id)))
            rows.sort(key=lambda r: db.get(OrderBomExternalComponent, r.order_component_id).product_id)
            purchase_id = rows[0].purchase_order_id
            first, second = [r.id for r in rows]
        response = receive(client, purchase_id, first, 'assembly-first', 6)
        assert response.status_code == 200, response.text
        with factory() as db:
            assert not db.scalar(select(BomAssembly.id).where(BomAssembly.quantity > 0))
        import app.services.multilevel_bom_receipts as module
        original = module.assemble_graph_order_receipt
        if fault:
            def fail(*args, **kwargs):
                original(*args, **kwargs)
                raise SubkitError('isolated-after-assembly-failure')
            monkeypatch.setattr(module, 'assemble_graph_order_receipt', fail)
            response = receive(client, purchase_id, second, 'assembly-second', 18)
            assert response.status_code == 409, response.text
            with factory() as db:
                assert db.scalar(select(func.count()).select_from(ExternalPackagingReceipt)) == 1
                assert not db.scalar(select(BomAssembly.id).where(BomAssembly.quantity > 0))
                lot = db.scalar(select(InventoryLot).where(InventoryLot.source_ref_type == 'bom_external_receipt'))
                assert lot.quantity_available == 2 and lot.quantity_consumed == 0
            monkeypatch.setattr(module, 'assemble_graph_order_receipt', original)
        response = receive(client, purchase_id, second, 'assembly-second', 18)
        assert response.status_code == 200, response.text
        retry = receive(client, purchase_id, second, 'assembly-second', 18)
        assert retry.status_code == 200 and retry.json()['created'] is False
        with factory() as db:
            outputs = list(db.scalars(select(BomAssembly).where(BomAssembly.quantity > 0)))
            assert len(outputs) == 1 and outputs[0].quantity == 1
            result = outputs[0]
            assert result.total_cost == Decimal('237.6000')
            inputs = list(db.scalars(select(BomAssemblyInput).where(BomAssemblyInput.conversion_id == result.id)))
            assert sorted(i.quantity for i in inputs) == [2,6]
            assert first_pid in {i.product_id for i in inputs}
            lot = db.get(InventoryLot, result.output_lot_id)
            assert lot.quantity_reserved == 1 and lot.quantity_available == 0
            assert db.scalar(select(func.count()).select_from(ProductionCompletion)) == 0
            tasks = list(db.scalars(select(ProductionTask).where(ProductionTask.order_item_id == item_id)))
            assert len(tasks) == 1 and tasks[0].finished_coverage_snapshot == 1
