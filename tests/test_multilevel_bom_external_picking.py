from decimal import Decimal
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models.external_packaging_purchase import ExternalPackagingPurchaseItem
from app.models.warehouse_inventory import InventoryLot, InventoryReservation
from app.models.order import OrderItem, Order
from app.models.user import User
from app.models.production import ProductionTask
from test_p1_33c3_external_packaging_purchase_confirmation import purchase_app
from tests.test_multilevel_bom_external_receipts import prepare, receive, _login, _confirm
from tests.test_p1_81_receipt_purpose_flow import _seed_material_and_staging, _p181_published_map_identity


@pytest.mark.parametrize('old_reserved', [0,3,10])
@pytest.mark.parametrize('split', [False, True])
def test_direct_purchased_root_reserves_only_missing_units(purchase_app, _p181_published_map_identity, old_reserved, split):
    factory = purchase_app.state.session_factory
    _seed_material_and_staging(factory)
    order_id, item_id, pid = prepare(purchase_app, stock_basis=3, purchase_basis=1, direct=True)
    if old_reserved:
        from app.core.time_contract import beijing_today
        from app.services.production_workflow import _receipt_auto_finished_ground_target
        from app.services.warehouse_inventory import manual_finished_in, reserve_finished_inventory
        with factory() as db:
            order = db.get(Order, order_id)
            actor = db.scalar(select(User).where(User.username == 'purchase-admin'))
            target = _receipt_auto_finished_ground_target(db, claim=True, customer_id=order.customer_id, product_id=pid)
            lot = manual_finished_in(db, customer_id=order.customer_id, product_id=pid, location_id=target.location.id,
                quantity=old_reserved, stock_date=beijing_today(), source_type='manual', remarks='隔离已有库存',
                operator_id=actor.id, idempotency_key='old-stock', expected_layout_version=target.layout_version)
            reserve_finished_inventory(db, order_item_id=item_id, inventory_lot_id=lot.id, quantity=old_reserved,
                expected_version=lot.version, operator_id=actor.id, idempotency_key='old-reserve', warning_acknowledged_codes=[])
            db.commit()
    with TestClient(purchase_app) as client:
        _login(client, 'purchase-admin')
        from app.services.multilevel_bom_receipts import graph_material_receipts_closed
        with factory() as db:
            assert graph_material_receipts_closed(db, db.get(OrderItem, item_id)) == (old_reserved == 10)
        if old_reserved == 10:
            return
        _confirm(client, order_id)
        with factory() as db:
            row = db.scalar(select(ExternalPackagingPurchaseItem).where(ExternalPackagingPurchaseItem.sales_order_item_id == item_id))
            purchase_id, line_id, quantity = row.purchase_order_id, row.id, row.purchase_quantity
            assert quantity == (4 if old_reserved == 0 else 3)
            assert graph_material_receipts_closed(db, db.get(OrderItem, item_id)) is False
        if split:
            response = receive(client, purchase_id, line_id, 'direct-first', 1)
            assert response.status_code == 200, response.text
            quantity -= 1
        response = receive(client, purchase_id, line_id, 'direct-receipt', quantity)
        assert response.status_code == 200, response.text
        retry = receive(client, purchase_id, line_id, 'direct-receipt', quantity)
        assert retry.status_code == 200 and retry.json()['created'] is False
        with factory() as db:
            lots = list(db.scalars(select(InventoryLot).where(InventoryLot.source_ref_type == 'bom_external_receipt')))
            assert all(l.finished_detail.product_id == pid for l in lots)
            assert sum(l.quantity_reserved for l in lots) == 10-old_reserved
            assert sum(l.quantity_available for l in lots) == 2
            rows = list(db.scalars(select(InventoryReservation).where(InventoryReservation.order_item_id == item_id)))
            assert sum(r.reserved_stock_quantity for r in rows) == 10
            assert len(rows) == 1 + bool(old_reserved) + split
            task = db.scalar(select(ProductionTask).where(ProductionTask.order_item_id == item_id))
            assert task.finished_coverage_snapshot == 10
            assert graph_material_receipts_closed(db, db.get(OrderItem, item_id)) is True
            assert db.get(OrderItem, item_id).material_status == 'received'
