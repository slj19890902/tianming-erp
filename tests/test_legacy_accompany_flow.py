from datetime import datetime
from decimal import Decimal
import pytest
from sqlalchemy import select
from fastapi.testclient import TestClient
from tests.test_n039_composite_bom_requisition import composite_requisition_app, _login
from tests.test_p1_81_receipt_purpose_flow import _p181_published_map_identity


def seed(factory):
    from tests.test_p1_81_receipt_purpose_flow import _seed_material_and_staging
    from app.models.warehouse_inventory import WarehouseLocation
    with factory() as db:
        db.get(WarehouseLocation,1).location_code='OLD-FIXTURE'
        db.commit()
    _seed_material_and_staging(factory)
    from app.models.order import OrderItem
    from app.models.product_bom import ProductBomComponent, SalesOrderItemBomComponent
    from app.models.multilevel_bom import ProductBomInventoryRelation
    from app.models.production import ProductionTask, ProductionCompletionBatch, ProductionCompletion
    from app.models.warehouse_inventory import InventoryReservation
    from app.services.warehouse_inventory import manual_finished_in
    from app.services.production_workflow import _receipt_auto_finished_ground_target
    from app.core.time_contract import beijing_today
    with factory() as db:
        item = db.get(OrderItem, 1)
        item.delivered_quantity = 7
        item.material_status = 'received'
        item.composite_fulfillment_mode_snapshot = 'parent_delivery'
        snapshot = db.scalar(select(SalesOrderItemBomComponent).where(SalesOrderItemBomComponent.component_product_id == 2))
        edge = db.scalar(select(ProductBomComponent).where(ProductBomComponent.component_product_id == 2))
        snapshot.product_bom_component_id = edge.id
        snapshot.quantity_per_set = Decimal(4)
        snapshot.required_piece_quantity = 40
        db.add(ProductBomInventoryRelation(bom_component_id=edge.id, relation='accompany'))
        ids = []
        for pid, qty in ((1, 10), (2, 1350)):
            target = _receipt_auto_finished_ground_target(db, claim=True, customer_id=1, product_id=pid)
            lot = manual_finished_in(db, customer_id=1, product_id=pid, location_id=target.location.id,
                quantity=qty, stock_date=beijing_today(), source_type='manual', remarks='isolated fixture',
                operator_id=1, idempotency_key=f'legacy-seed-{pid}', expected_layout_version=target.layout_version)
            ids.append(lot.id)
            if pid == 1:
                lot.quantity_available = 0; lot.quantity_reserved = 3; lot.quantity_consumed = 7
                db.add(InventoryReservation(reservation_number='legacy-parent', inventory_lot_id=lot.id,
                    reservation_type='finished_order', order_id=1, order_item_id=1,
                    reserved_stock_quantity=10, credited_requirement_quantity=10,
                    consumed_stock_quantity=7, consumed_requirement_quantity=7, yield_factor=1,
                    status='partial', reserved_by=1, idempotency_key='legacy-parent'))
        task = ProductionTask(order_item_id=1)
        batch = ProductionCompletionBatch(idempotency_key='legacy-completed',request_hash='a'*64,item_count=1,completed_at=datetime(2026,9,15))
        db.add_all([task,batch]); db.flush()
        db.add(ProductionCompletion(batch_id=batch.id,task_id=task.id,order_item_id=1,
            expected_version=1,quantity=10,initial_disposition='stock',origin='receipt_auto',
            warehouse_location_id=db.get(type(lot),ids[0]).warehouse_location_id,
            inventory_lot_id=ids[0], completed_at=datetime(2026,9,15)))
        db.commit()
        return ids


def test_pending_reserve_dispatch_retry_cancel(composite_requisition_app, _p181_published_map_identity):
    from app.api.deliveries import router
    from app.models.warehouse_inventory import InventoryLot
    from app.models.multilevel_bom import LegacyAccompanyContract, ProductBomInventoryRelation
    from app.models.product import Product
    from app.models.customer import Customer
    from app.models.delivery import Delivery
    from app.services.production_packaging_label import build_delivery_packaging_label_package
    from app.core.time_contract import beijing_today
    app, factory = composite_requisition_app
    app.include_router(router, prefix='/api/deliveries')
    ids = seed(factory)
    def balances():
        with factory() as db:
            return [(l.quantity_available,l.quantity_reserved,l.quantity_consumed) for l in [db.get(InventoryLot,i) for i in ids]]
    with TestClient(app) as client:
        _login(client)
        response = client.post('/api/deliveries', json={'customer_id':1,'delivery_date':beijing_today().isoformat(),
            'items':[{'order_item_id':1,'delivered_quantity':3}]})
        assert response.status_code == 201, response.text
        did = response.json()['id']
        assert balances() == [(0,3,7),(1338,12,0)]
        assert client.post(f'/api/deliveries/{did}/prepare-accompany').status_code == 200
        assert balances() == [(0,3,7),(1338,12,0)]
        with factory() as db:
            assert db.get(LegacyAccompanyContract,1)
            # A later master change cannot silently change the held recipe.
            db.scalar(select(ProductBomInventoryRelation)).relation = 'assembly'
            db.get(Customer,1).chinese_short_name = '测试'
            for pid in (1,2):
                p=db.get(Product,pid); p.production_label_enabled=True; p.production_label_units_per_label=1
            db.commit()
            package=build_delivery_packaging_label_package(db,db.get(Delivery,did))
            assert {p['product_id']:p['total_quantity'] for p in package['plans']} == {1:3,2:12}, package['review_messages']
        picked=client.post(f'/api/deliveries/{did}/pick-task')
        assert picked.status_code == 201, picked.text
        lines=picked.json()['items'][0]['location_lines']
        assert sum(l['pick_quantity'] for l in lines if l['component_snapshot_id']) == 12, lines
        assert sum(l['pick_quantity'] for l in lines if not l['component_snapshot_id']) == 3, lines
        sent=client.put(f'/api/deliveries/{did}/dispatch')
        assert sent.status_code == 200, sent.text
        assert balances() == [(0,0,10),(1338,0,12)]
        client.put(f'/api/deliveries/{did}/dispatch')
        assert balances() == [(0,0,10),(1338,0,12)]
        cancel=client.put(f'/api/deliveries/{did}/cancel')
        assert cancel.status_code == 200, cancel.text
        assert balances() == [(0,3,7),(1338,12,0)]


def test_shortage_rollback_and_admin_guard(composite_requisition_app, _p181_published_map_identity):
    from app.services.legacy_accompany import prepare_order
    from app.models.user import User
    from app.models.multilevel_bom import LegacyAccompanyContract
    from app.models.warehouse_inventory import InventoryLot, InventoryReservation
    from app.services.warehouse_inventory import WarehouseInventoryError
    _,factory=composite_requisition_app
    ids=seed(factory)
    with factory() as db:
        actor=db.get(User,1)
        actor.role='sales'; db.flush()
        with pytest.raises(WarehouseInventoryError,match='管理员'):
            prepare_order(db,1,actor)
        db.rollback()
        db.get(InventoryLot,ids[1]).quantity_available=11; db.commit()
        with pytest.raises(WarehouseInventoryError,match='缺1'):
            prepare_order(db,1,db.get(User,1))
        assert db.get(LegacyAccompanyContract,1) is None
        assert db.get(InventoryLot,ids[1]).quantity_reserved == 0
        assert not list(db.scalars(select(InventoryReservation).where(InventoryReservation.sales_order_item_bom_component_id.is_not(None))))


def test_audit_failure_rolls_back_contract_and_stock(composite_requisition_app, _p181_published_map_identity, monkeypatch):
    from app.services.legacy_accompany import prepare_order
    from app.models.user import User
    from app.models.multilevel_bom import LegacyAccompanyContract
    from app.models.warehouse_inventory import InventoryLot
    from app.services import audit_log
    _,factory=composite_requisition_app
    ids=seed(factory)
    def fail(*args,**kwargs):
        raise RuntimeError('audit unavailable')
    monkeypatch.setattr(audit_log,'append_audit_event',fail)
    with factory() as db:
        with pytest.raises(RuntimeError,match='audit unavailable'):
            prepare_order(db,1,db.get(User,1))
        assert db.get(LegacyAccompanyContract,1) is None
        lot=db.get(InventoryLot,ids[1])
        assert (lot.quantity_available,lot.quantity_reserved,lot.quantity_consumed)==(1350,0,0)


def test_post_reservation_failure_and_cross_customer_stock(composite_requisition_app, _p181_published_map_identity, monkeypatch):
    from app.services import legacy_accompany as svc
    from app.models.user import User
    from app.models.multilevel_bom import LegacyAccompanyContract
    from app.models.warehouse_inventory import InventoryLot
    from app.models.customer import Customer
    from app.services.warehouse_inventory import WarehouseInventoryError
    _,factory=composite_requisition_app
    ids=seed(factory)
    def fail(*args,**kwargs):raise RuntimeError('movement unavailable')
    with factory() as db:
        with monkeypatch.context() as m:
            m.setattr(svc,'_movement',fail)
            with pytest.raises(RuntimeError,match='movement unavailable'):
                svc.prepare_order(db,1,db.get(User,1))
        assert db.get(LegacyAccompanyContract,1) is None
        lot=db.get(InventoryLot,ids[1]); db.refresh(lot)
        assert (lot.quantity_available,lot.quantity_reserved)==(1350,0)
        other=Customer(customer_number=2,customer_code='OTHER',name='Other customer')
        db.add(other);db.flush()
        lot.finished_detail.owner_customer_id=other.id;db.commit()
        with pytest.raises(WarehouseInventoryError,match='可用0'):
            svc.prepare_order(db,1,db.get(User,1))
        assert db.get(LegacyAccompanyContract,1) is None
