from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, func

from test_direct_external_finished import prepare, routing_app, p1_40a_app, _p181_published_map_identity
from app.models.external_packaging_purchase import ExternalPackagingReceipt, ExternalPackagingReceiptItem
from app.models.delivery import Delivery, DeliveryItem
from app.models.order import OrderItem
from app.models.user import User
from app.models.warehouse_inventory import InventoryLot, InventoryMovement
from app.services.external_legacy_stock import preview, reconcile, record
from app.services.external_packaging_purchase import ExternalPurchaseContractError
from app.services.semi_finished_inventory import consume_delivery_item_inventory, reverse_delivery_item_inventory
from app.services.warehouse_inventory import WarehouseInventoryError


def seed(app):
    with TestClient(app) as client:
        oid, pid, line = prepare(app, client)
    with app.state.factory() as db:
        item = db.scalar(select(OrderItem).where(OrderItem.order_id == oid))
        old = ExternalPackagingReceipt(purchase_order_id=pid, receipt_number='OLD',
            idempotency_key='old', request_fingerprint='0'*64, received_by=1)
        db.add(old)
        db.flush()
        receipt = ExternalPackagingReceiptItem(receipt_id=old.id, purchase_item_id=line,
            received_quantity=1000, purchase_unit_snapshot='根', converted_finished_quantity=0)
        db.add(receipt)
        delivery = Delivery(delivery_number='OLD-800', customer_id=app.state.fixture['customer_a'],
            delivery_date=date.today(), status='dispatched')
        db.add(delivery)
        db.flush()
        dl = DeliveryItem(delivery_id=delivery.id, order_item_id=item.id, delivered_quantity=800)
        db.add(dl)
        item.delivered_quantity = 800
        db.commit()
        return receipt.id, item.id, dl.id


def arguments(db, rid):
    from app.services.production_workflow import list_temporary_locations
    loc = next(r for r in list_temporary_locations(db, stock_materials=True) if r['is_empty'])
    _, fingerprint = preview(db, rid)
    return dict(receipt_item_id=rid, expected_hash=fingerprint, confirmed_quantity=200,
        location_id=loc['id'], layout_version=loc['layout_version'],
        actor=db.scalar(select(User).where(User.username == 'p1-40a-admin')),
        reason='实收1000已送800，现场确认余货200')


def test_cutover_only_remainder_and_new_dispatch_reversal(routing_app):
    rid, iid, old_line_id = seed(routing_app)
    with routing_app.state.factory() as db:
        args = arguments(db, rid)
        result = reconcile(db, **args)
        assert reconcile(db, **args)['lot_id'] == result['lot_id']
        lot = db.get(InventoryLot, result['lot_id'])
        assert lot.quantity_reserved == 200 and lot.quantity_consumed == 0
        assert lot.warehouse_location_id == args['location_id']
        assert db.get(ExternalPackagingReceiptItem, rid).converted_finished_quantity == 0
        assert db.get(DeliveryItem, old_line_id).delivered_quantity == 800
        item = db.get(OrderItem, iid)
        delivery = Delivery(delivery_number='NEW-200', customer_id=result['customer_id'],
            delivery_date=date.today(), status='dispatched')
        db.add(delivery)
        db.flush()
        line = DeliveryItem(delivery_id=delivery.id, order_item_id=iid, delivered_quantity=200)
        db.add(line)
        db.flush()
        consume_delivery_item_inventory(db, delivery_item_id=line.id,
            delivered_quantity_after_dispatch=1000, operator_id=args['actor'].id, operation_key='new-dispatch')
        item.delivered_quantity = 1000
        db.flush()
        db.refresh(lot)
        assert lot.quantity_consumed == 200 and lot.quantity_reserved == 0
        reverse_delivery_item_inventory(db, delivery_item_id=line.id,
            delivered_quantity_after_cancel=800, operator_id=args['actor'].id, operation_key='new-cancel')
        item.delivered_quantity = 800
        db.flush()
        db.refresh(lot)
        assert lot.quantity_consumed == 0 and lot.quantity_reserved == 200
        assert db.scalar(select(func.count()).select_from(InventoryLot)) == 1
        with pytest.raises(WarehouseInventoryError, match='旧单'):
            reverse_delivery_item_inventory(db, delivery_item_id=old_line_id,
                delivered_quantity_after_cancel=0, operator_id=args['actor'].id, operation_key='old-cancel')


def test_cutover_rejects_stale_quantity_permission_and_conflicting_replay(routing_app):
    rid, iid, _ = seed(routing_app)
    with routing_app.state.factory() as db:
        args = arguments(db, rid)
        for changes in ({'expected_hash':'0'*64}, {'confirmed_quantity':201},
                        {'actor':SimpleNamespace(role='employee', is_active=True)}):
            with pytest.raises(ExternalPurchaseContractError):
                reconcile(db, **(args | changes))
        assert db.scalar(select(func.count()).select_from(InventoryLot)) == 0
        reconcile(db, **args)
        with pytest.raises(ExternalPurchaseContractError, match='不能重复'):
            reconcile(db, **(args | {'confirmed_quantity':201}))
        assert record(db, iid)['remaining'] == 200


def test_cutover_reservation_failure_rolls_back_everything(routing_app, monkeypatch):
    from app.services import production_workflow
    rid, iid, _ = seed(routing_app)
    with routing_app.state.factory() as db:
        args = arguments(db, rid)
        def fail(*a, **kw):
            raise RuntimeError('reserve failure')
        monkeypatch.setattr(production_workflow, '_reserve_component_completion_lot', fail)
        with pytest.raises(RuntimeError, match='reserve failure'):
            reconcile(db, **args)
        assert db.scalar(select(func.count()).select_from(InventoryLot)) == 0
        assert record(db, iid) is None
