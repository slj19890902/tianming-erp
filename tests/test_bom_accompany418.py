"""Isolated 00139-shaped contract: separately received body and 4 liners."""
from types import SimpleNamespace
from dataclasses import replace
from datetime import datetime
from sqlalchemy import select
from fastapi.testclient import TestClient

from tests.test_n039_composite_bom_requisition import composite_requisition_app, _login
from tests.test_p1_81_receipt_purpose_flow import _p181_published_map_identity, _freeze_receipt_fact, _receive
from tests.test_multilevel_bom_receipt_flow import seed_graph, purchase_sources


def test_separate_receipts_accompany_four_dispatch_and_reverse(composite_requisition_app, _p181_published_map_identity):
    from app.api.deliveries import router
    from app.core.time_contract import beijing_today
    from app.models.delivery import Delivery
    from app.models.product import Product
    from app.models.warehouse_inventory import InventoryLot
    from app.services.production_packaging_label import build_delivery_packaging_label_package
    from app.services.shelf_lot_history import shelf_related_inventory
    app, factory = composite_requisition_app
    app.include_router(router, prefix='/api/deliveries')
    material, snapshots = seed_graph(factory, accompany=True, quantity=300)
    with TestClient(app) as client:
        _login(client)
        sources = purchase_sources(client, factory, material, snapshots)
        assert len(sources) == 2  # independent parent material and child material
        for i, source in enumerate(sources):
            fact = _freeze_receipt_fact(client, source, idempotency_key=f'accompany-price-{i}', unit_price='0.1234')
            assert fact.status_code == 200, fact.text
            received = _receive(client, source, fact.json(), quantity=source.order_purpose_sheet_qty,
                                idempotency_key=f'accompany-receipt-{i}')
            assert received.status_code == 200, received.text
        def balances():
            with factory() as db:
                return {pid: (sum(l.quantity_reserved for l in db.scalars(select(InventoryLot)) if l.finished_detail and l.finished_detail.product_id == pid),
                              sum(l.quantity_consumed for l in db.scalars(select(InventoryLot)) if l.finished_detail and l.finished_detail.product_id == pid)) for pid in (1, 2)}
        assert balances() == {1: (300, 0), 2: (1200, 0)}
        created = client.post('/api/deliveries', json={'customer_id':1, 'delivery_date':beijing_today().isoformat(),
            'items':[{'order_item_id':1, 'delivered_quantity':30}]})
        assert created.status_code == 201, created.text
        did = created.json()['id']
        detail = client.get(f'/api/deliveries/{did}').json()['items'][0]
        assert {r['component_product_id']:r['planned_delivery_quantity'] for r in detail['component_lines']} == {1:30, 2:120}
        assert len({r['location_id'] for r in detail['inventory_sources']}) == 2
        with factory() as db:
            from app.models.customer import Customer
            db.get(Customer, 1).chinese_short_name = '隔离客户'
            for pid in (1, 2):
                product = db.get(Product, pid)
                product.production_label_enabled = True
                product.production_label_units_per_label = 10
            db.flush()
            labels = build_delivery_packaging_label_package(db, db.get(Delivery, did))
            assert {r['product_id']:r['total_quantity'] for r in labels['plans']} == {1:30, 2:120}, (labels['review_messages'], labels['excluded_items'])
            child = next(l for l in db.scalars(select(InventoryLot)) if l.finished_detail and l.finished_detail.product_id == 2)
            related = shelf_related_inventory(db, child, {1})['bom_relations']
            assert len(related) == 1 and related[0]['direction'] == 'parent'
            assert related[0]['quantity_per_set'] == 4 and related[0]['locations']
            assert 'bom_relations' not in shelf_related_inventory(db, child, set())
            db.rollback()
        sent = client.put(f'/api/deliveries/{did}/dispatch')
        assert sent.status_code == 200, sent.text
        assert balances() == {1:(270,30), 2:(1080,120)}
        client.put(f'/api/deliveries/{did}/dispatch')  # retry cannot debit twice
        assert balances() == {1:(270,30), 2:(1080,120)}
        cancelled = client.put(f'/api/deliveries/{did}/cancel')
        assert cancelled.status_code == 200, cancelled.text
        assert balances() == {1:(300,0), 2:(1200,0)}


def test_new_dispatch_does_not_catch_up_old_missing_child_consumption(monkeypatch):
    from app.services import composite_bom_workflow as service
    demand = service.ComponentDemand(6, 1, 2, '00139', '衬板', None, 4, True, 150, 600)
    monkeypatch.setattr(service, 'delivery_component_demands', lambda *_: [demand])
    monkeypatch.setattr(service, '_delivered_component_quantity', lambda *_: 0)
    db = SimpleNamespace(get=lambda *_:SimpleNamespace(delivered_quantity=120))
    assert service.delivery_component_required_quantities(db, order_item_id=1, delivery_sets=30) == {6:120}
    monkeypatch.setattr(service, 'delivery_component_demands', lambda *_:[replace(demand, delivered_before_cutover=120, required_piece_quantity=120)])
    assert service.delivery_component_required_quantities(db, order_item_id=1, delivery_sets=30) == {6:120}


def test_new_order_reserves_existing_accompany_stock(composite_requisition_app, _p181_published_map_identity):
    from app.services.production_workflow import _receipt_auto_finished_ground_target
    from app.services.warehouse_inventory import manual_finished_in
    from app.core.time_contract import beijing_today
    from app.models.warehouse_inventory import InventoryLot, InventoryReservation
    from tests.test_multilevel_bom_order_entry import payload
    from app.services.multilevel_bom_requirements import read_graph_requirements
    app, factory = composite_requisition_app
    seed_graph(factory, accompany=True)
    with factory() as db:
        target = _receipt_auto_finished_ground_target(db, claim=True, customer_id=1, product_id=2)
        lot = manual_finished_in(db, customer_id=1, product_id=2, location_id=target.location.id,
            quantity=1350, stock_date=beijing_today(), source_type='manual', remarks='隔离库存衬板',
            operator_id=1, idempotency_key='accompany-existing', expected_layout_version=target.layout_version)
        lid = lot.id
        db.commit()
    with TestClient(app) as client:
        _login(client)
        body = payload(factory, key='accompany-existing-order')
        body['items'][0]['quantity'] = 30
        created = client.post('/api/orders', json=body)
        assert created.status_code == 201, created.text
        iid = created.json()['items'][0]['id']
    with factory() as db:
        lot = db.get(InventoryLot, lid)
        assert (lot.quantity_available, lot.quantity_reserved) == (1230,120)
        assert db.scalar(select(InventoryReservation.order_item_id).where(InventoryReservation.inventory_lot_id == lid)) == iid
        requirements = read_graph_requirements(db, iid)
        assert dict(requirements.plan.picking) == {1:30,2:120}
        assert next(m.purchase_sheets for m in requirements.plan.materials if m.product_id == 2) == 0


def test_legacy_relationship_is_advisory_without_rewriting_snapshot(composite_requisition_app):
    from app.models.product_bom import ProductBomComponent, SalesOrderItemBomComponent
    from app.models.multilevel_bom import ProductBomInventoryRelation, OrderBomGraph
    from app.models.production import ProductionTask, ProductionCompletionBatch, ProductionCompletion
    from app.models.order import OrderItem
    from app.services.bom_accompany import legacy_accompany_preview
    _, factory = composite_requisition_app
    with factory() as db:
        snapshot = db.scalar(select(SalesOrderItemBomComponent).where(SalesOrderItemBomComponent.component_product_id == 2))
        edge = db.scalar(select(ProductBomComponent).where(ProductBomComponent.component_product_id == 2))
        snapshot.product_bom_component_id = edge.id
        db.add(ProductBomInventoryRelation(bom_component_id=edge.id, relation='accompany'))
        task = ProductionTask(order_item_id=1)
        batch = ProductionCompletionBatch(idempotency_key='legacy',request_hash='a'*64,item_count=1,completed_at=datetime(2026,9,15))
        db.add_all([task,batch]); db.flush()
        db.add(ProductionCompletion(batch_id=batch.id,task_id=task.id,order_item_id=1,
            expected_version=1,quantity=10,initial_disposition='direct',origin='receipt_auto',completed_at=datetime(2026,9,15)))
        db.flush()
        rows = legacy_accompany_preview(db, db.get(OrderItem,1), 3)
        assert len(rows) == 1 and rows[0]['planned_delivery_quantity'] == 6
        assert rows[0]['relation_basis'] == 'legacy_advisory_not_frozen'
        assert db.get(OrderBomGraph,1) is None and snapshot.quantity_per_set == 2
        db.get(ProductBomInventoryRelation,edge.id).relation = 'assembly'
        db.flush()
        assert legacy_accompany_preview(db,db.get(OrderItem,1),3) == []
