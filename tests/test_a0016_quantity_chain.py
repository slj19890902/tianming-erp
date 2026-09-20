"""Exact handoff quantity regression on disposable synthetic data."""
import json
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select
from tests.test_phase11_requisition import requisition_app, _login
from tests.test_semi_finished_order_reservation import b1_app, login, order_item
from tests.test_p1_81_receipt_purpose_flow import (
    _p181_published_map_identity, _seed_material_and_staging,
    _create_frozen_sources, _freeze_receipt_fact, _receive,
)


def test_sx11_300_plus_300_dispatch_300_and_print_frozen_customer(requisition_app):
    from app.api.deliveries import router, _delivery_remaining_quantity
    from app.models.order import OrderItem
    from app.models.customer import Customer
    from app.models.warehouse_inventory import InventoryLot
    app, factory = requisition_app
    app.include_router(router, prefix='/api/deliveries')
    _seed_material_and_staging(factory)
    facts = []

    def snapshot(stage):
        with factory() as db:
            item = db.get(OrderItem, 1)
            lots = list(db.scalars(select(InventoryLot).where(InventoryLot.status == 'active')))
            remaining = lambda kind: sum(int(x.quantity_available) + int(x.quantity_reserved)
                                         for x in lots if x.inventory_type == kind)
            row = dict(stage=stage, order=int(item.quantity), sheets=remaining('semi_finished'),
                       finished=remaining('finished'), delivered=int(item.delivered_quantity or 0),
                       to_deliver=int(item.quantity)-int(item.delivered_quantity or 0),
                       ready_to_dispatch=int(_delivery_remaining_quantity(db, item)))
            facts.append(row)
            return row

    with TestClient(app) as client:
        _login(client, 'admin')
        source = _create_frozen_sources(client, factory, order_quantity=500, purchase_total=600,
                                        order_purpose=500, stock_purpose=100)[0]
        fact = _freeze_receipt_fact(client, source, idempotency_key='audit-price')
        assert fact.status_code == 200, fact.text
        snapshot('initial')
        for index, expected in [(1, (0, 300)), (2, (100, 500))]:
            kwargs = dict(quantity=300, idempotency_key=f'audit-receive-{index}')
            if index == 2:
                kwargs['overrides'] = {'surplus_disposition': 'semi_finished_reserve'}
            received = _receive(client, source, fact.json(), **kwargs)
            assert received.status_code == 200, received.text
            before = snapshot(f'receive-{index}')
            assert (before['sheets'], before['finished']) == expected
            repeat = _receive(client, source, fact.json(), **kwargs)
            assert repeat.status_code == 200, repeat.text
            after = snapshot(f'replay-{index}')
            assert {k:v for k,v in before.items() if k != 'stage'} == {k:v for k,v in after.items() if k != 'stage'}
        created = client.post('/api/deliveries', json={'customer_id':1,
                              'items':[{'order_item_id':1, 'delivered_quantity':300}]})
        assert created.status_code == 201, created.text
        delivery_id = created.json()['id']
        dispatched = client.put(f'/api/deliveries/{delivery_id}/dispatch')
        assert dispatched.status_code == 200, dispatched.text
        final = snapshot('dispatch-300')
        assert (final['order'], final['sheets'], final['finished'], final['delivered'], final['to_deliver']) == (500,100,200,300,200)
        first_print = client.get(f'/api/deliveries/{delivery_id}/print')
        assert first_print.status_code == 200, first_print.text
        first_payload = first_print.json()
        assert first_payload['print_snapshot_basis'] == 'frozen_v1'
        frozen_customer = dict(first_payload['customer'])
        with factory() as db:
            customer = db.get(Customer, 1)
            customer.name = 'AUDIT-RENAMED'
            customer.address = 'AUDIT-NEW-ADDRESS'
            db.commit()
        second_print = client.get(f'/api/deliveries/{delivery_id}/print')
        assert second_print.status_code == 200, second_print.text
        second_payload = second_print.json()
        assert second_payload['print_snapshot_basis'] == 'frozen_v1'
        assert second_payload['customer'] == frozen_customer
        assert first_payload['items'] == second_payload['items']
        after_print = snapshot('reprint')
        assert {k:v for k,v in final.items() if k != 'stage'} == {k:v for k,v in after_print.items() if k != 'stage'}
