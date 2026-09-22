import json
from fastapi.testclient import TestClient
from sqlalchemy import select
from tests.test_phase11_requisition import requisition_app,_login
from tests.test_p1_80_purchase_purpose_allocation import _prepare_order_item
from tests.test_p1_81_receipt_purpose_flow import _seed_material_and_staging,_p181_published_map_identity


def setup_raw(app,factory):
    from app.models.order import OrderItem
    from app.models.product import Product
    _seed_material_and_staging(factory)
    _prepare_order_item(factory,quantity=20)
    with factory() as db:
        item=db.get(OrderItem,1);product=db.get(Product,item.product_id)
        product.box_style='A3 天地盖';item.snapshot_base_report_length_mm=780;item.snapshot_base_report_width_mm=190
        db.commit()


def raw_payload(client):
    response=client.post('/api/requisition/raw-plans/choices',json={'order_item_ids':[1]})
    assert response.status_code==200,response.text
    return dict(order_item_ids=[1],length_mm=800,width_mm=200,quantity=45,sheet_type='raw_board',flute_direction='length',
        allocations=[dict(source_key=r['source_key'],yield_factor=1,rotate=False,piece_flute_direction='length') for r in response.json()['requirements']])


def test_raw_45_plan_preserves_20_cover_20_base_and_no_inventory_before_receipt(requisition_app):
    from decimal import Decimal
    from app.models.order import OrderItem
    from app.models.warehouse_inventory import InventoryLot,InventoryReservation
    from app.models.supplier_requisition_order import SupplierRequisitionOrderItem
    from app.models.raw_purchase_plan import RawPurchasePlan,RawPurchaseDemand
    app,factory=requisition_app;setup_raw(app,factory)
    with TestClient(app) as client:
        _login(client,'admin')
        payload=raw_payload(client)
        review=client.post('/api/requisition/raw-plans/preview',json=payload)
        assert review.status_code==200,review.text
        assert review.json()['allocated_quantity']==40 and review.json()['reserve_quantity']==5
        bad=client.post('/api/requisition/raw-plans/preview',json={**payload,'length_mm':700})
        assert bad.status_code==409
        body=dict(plan=payload,reviewed_hash=review.json()['reviewed_hash'],operation_key='r08-45')
        result=client.post('/api/requisition/raw-plans',json=body)
        assert result.status_code==200,result.text
        replay=client.post('/api/requisition/raw-plans',json=body)
        assert replay.status_code==200 and replay.json()==result.json(),replay.text
        with factory() as db:
            assert len(list(db.scalars(select(RawPurchasePlan))))==1
            assert [d.piece_quantity for d in db.scalars(select(RawPurchaseDemand))]==[20,20]
            line=db.scalar(select(SupplierRequisitionOrderItem))
            assert (line.requisition_qty,line.report_length_mm,line.report_width_mm)==(45,800,200)
            item=db.get(OrderItem,1)
            assert item.quantity==20 and item.snapshot_base_report_length_mm==780
            assert not list(db.scalars(select(InventoryLot)))
            assert not list(db.scalars(select(InventoryReservation)))
        received=client.put(f"/api/incoming/receive/sr{result.json()['stock_item_id']}",json={'received_quantity':45,'idempotency_key':'r08-receive-45'})
        assert received.status_code==200,received.text
        with factory() as db:
            lot=db.scalar(select(InventoryLot))
            assert lot.inventory_type=='semi_finished'
            assert (lot.quantity_available,lot.quantity_reserved,lot.quantity_consumed)==(5,40,0)
            from app.core.inventory_entry_guard import validate_entries
            from app.services.inventory_valuation import cost_payload
            db.info['new_inventory_entry_ids']={lot.id}
            validate_entries(db)
            assert Decimal(cost_payload(lot,db)['unit_cost']) == lot.estimated_unit_cost_snapshot
            reservations=list(db.scalars(select(InventoryReservation)))
            assert len(reservations)==2 and sum(r.credited_requirement_quantity for r in reservations)==40
            from app.services.receipt_managed_production import receipt_purpose_summaries_by_order_item_ids
            summary=receipt_purpose_summaries_by_order_item_ids(db,[1])[1]
            assert summary['requires_component_processing'] and summary['pending_processing_quantity']==20
            assert summary['automatic_finished_output_qty']==0
        listing=client.get('/api/requisition/raw-plans')
        assert listing.status_code==200,listing.text
        assert listing.json()['items'][0]['receipts'][0]['reserved']==40
        printed=client.get(f"/api/requisition/supplier-orders/{result.json()['supplier_order_id']}")
        assert printed.status_code==200,printed.text
        assert printed.json()['raw_purchase_plans'][0]['requirements'][1]['report_length_mm']==780
        from app.api.bom_cutover import router as processing_router
        app.include_router(processing_router,prefix='/api/orders')
        with factory() as db:pid=db.get(OrderItem,1).product_id
        process=client.post('/api/orders/items/1/component-processing/preview',json={'product_id':pid})
        assert process.status_code==200,process.text
        assert process.json()['quantity']==20
        saved=client.post('/api/orders/items/1/component-processing/execute',json=dict(product_id=pid,reviewed_hash=process.json()['reviewed_hash'],operation_key='r08-processing'))
        assert saved.status_code==200,saved.text
        with factory() as db:
            raw=db.scalar(select(InventoryLot).where(InventoryLot.inventory_type=='semi_finished'))
            finished=db.scalar(select(InventoryLot).where(InventoryLot.inventory_type=='finished'))
            assert (raw.quantity_available,raw.quantity_reserved,raw.quantity_consumed)==(5,0,40)
            assert finished.quantity_reserved==20
            summary=receipt_purpose_summaries_by_order_item_ids(db,[1])[1]
            assert not summary['requires_component_processing'] and not summary['projection_inconsistent']
            assert summary['manual_processing_output_qty']==20
            from app.services.multilevel_bom_cost_lineage import graph_material_sources
            assert finished.cost_snapshot_source=='component_processing_actual'
            db.info['new_inventory_entry_ids']={finished.id}
            validate_entries(db)
            assert Decimal(cost_payload(finished,db)['unit_cost']) == finished.estimated_unit_cost_snapshot
            evidence=graph_material_sources(db,finished)
            assert len(evidence)==2 and all(r['raw_receipt_allocation_id'] for r in evidence)
        undone=client.post(f"/api/orders/items/1/component-processing/{saved.json()['completion_id']}/revert")
        assert undone.status_code==200,undone.text
        with factory() as db:
            raw=db.scalar(select(InventoryLot).where(InventoryLot.inventory_type=='semi_finished'))
            assert (raw.quantity_available,raw.quantity_reserved,raw.quantity_consumed)==(5,40,0)
        process=client.post('/api/orders/items/1/component-processing/preview',json={'product_id':pid})
        saved=client.post('/api/orders/items/1/component-processing/execute',json=dict(product_id=pid,reviewed_hash=process.json()['reviewed_hash'],operation_key='r08-processing-again'))
        assert saved.status_code==200,saved.text
        from app.api.deliveries import router as delivery_router
        from app.core.time_contract import beijing_today
        app.include_router(delivery_router,prefix='/api/deliveries')
        draft=client.post('/api/deliveries',json=dict(customer_id=1,delivery_date=beijing_today().isoformat(),items=[dict(order_item_id=1,delivered_quantity=4)]))
        assert draft.status_code==201,draft.text
        dispatched=client.put(f"/api/deliveries/{draft.json()['id']}/dispatch")
        assert dispatched.status_code==200,dispatched.text
        from app.models.graph_material_cost import FinanceDeliveryGraphCostFact
        from app.models.raw_purchase_plan import RawPurchaseDeliveryCostPortion
        from app.services.graph_delivery_cost import graph_cost_report_sources,active_graph_cost
        with factory() as db:
            fact=db.scalar(select(FinanceDeliveryGraphCostFact))
            portions=list(db.scalars(select(RawPurchaseDeliveryCostPortion)))
            assert len(portions)==2 and sum(p.charged_cost for p in portions)==fact.total_cost
            assert active_graph_cost(fact,portions,4)==fact.total_cost
            assert graph_cost_report_sources(db,[fact.delivery_item_id])
        cancelled=client.put(f"/api/deliveries/{draft.json()['id']}/cancel")
        assert cancelled.status_code==200,cancelled.text
        undone=client.post(f"/api/orders/items/1/component-processing/{saved.json()['completion_id']}/revert")
        # Existing downstream-history gate stays intact after dispatch/cancel.
        assert undone.status_code==409,undone.text
        with factory() as db:
            raw=db.scalar(select(InventoryLot).where(InventoryLot.inventory_type=='semi_finished'))
            assert (raw.quantity_available,raw.quantity_reserved,raw.quantity_consumed)==(5,0,40)


def test_raw_plan_stale_permissions_atomicity_duplicate_and_void(requisition_app,monkeypatch):
    import pytest
    from app.models.raw_purchase_plan import RawPurchasePlan
    from app.models.stock_replenishment import StockReplenishmentOrder
    from app.models.supplier_requisition_order import SupplierRequisitionOrder
    from app.models.order import OrderItem
    import app.services.audit_log as audit
    app,factory=requisition_app;setup_raw(app,factory)
    with TestClient(app) as client:
        _login(client,'sales')
        assert client.post('/api/requisition/raw-plans/choices',json={'order_item_ids':[1]}).status_code==403
        _login(client,'admin');payload=raw_payload(client)
        review=client.post('/api/requisition/raw-plans/preview',json=payload).json()
        body=dict(plan=payload,reviewed_hash=review['reviewed_hash'],operation_key='r08-guards')
        assert client.post('/api/requisition/raw-plans',json={**body,'reviewed_hash':'a'*64}).status_code==409
        bad={**payload,'allocations':[{**r,'piece_flute_direction':'width'} for r in payload['allocations']]}
        assert client.post('/api/requisition/raw-plans/preview',json=bad).status_code==409
        bad={**payload,'allocations':[{**r,'yield_factor':2} for r in payload['allocations']]}
        assert client.post('/api/requisition/raw-plans/preview',json=bad).status_code==409
        with monkeypatch.context() as patch:
            def injected(*a,**kw):raise RuntimeError('r08-audit-failure')
            patch.setattr(audit,'append_audit_event',injected)
            with pytest.raises(RuntimeError,match='r08-audit-failure'):client.post('/api/requisition/raw-plans',json=body)
        with factory() as db:
            assert not list(db.scalars(select(RawPurchasePlan)))
            assert not list(db.scalars(select(StockReplenishmentOrder)))
            assert not list(db.scalars(select(SupplierRequisitionOrder)))
            assert db.get(OrderItem,1).requisition_status=='未报料'
        created=client.post('/api/requisition/raw-plans',json=body);assert created.status_code==200,created.text
        assert client.post('/api/requisition/raw-plans',json={**body,'operation_key':'different'}).status_code==409
        assert client.post('/api/requisition/raw-plans',json={**body,'plan':{**payload,'quantity':46}}).status_code==409
        assert client.post('/api/requisition/raw-plans/choices',json={'order_item_ids':[1]}).status_code==409
        voided=client.put(f"/api/requisition/supplier-orders/{created.json()['supplier_order_id']}/void")
        assert voided.status_code==200,voided.text
        assert client.post('/api/requisition/raw-plans',json=body).status_code==409
        assert client.post('/api/requisition/raw-plans/choices',json={'order_item_ids':[1]}).status_code==200
        with factory() as db:
            assert db.scalar(select(StockReplenishmentOrder)).status=='voided'
            assert db.scalar(select(RawPurchasePlan)).status=='voided'
            assert db.get(OrderItem,1).requisition_status=='未报料'


def test_two_up_partial_raw_receipts_do_not_mix_sheets_and_pieces(requisition_app):
    from decimal import Decimal
    from app.models.warehouse_inventory import InventoryLot,InventoryReservation
    from app.models.raw_purchase_plan import RawPurchaseReceiptAllocation
    from app.services.receipt_managed_production import receipt_purpose_summaries_by_order_item_ids
    app,factory=requisition_app;setup_raw(app,factory)
    with TestClient(app) as client:
        _login(client,'admin');payload=raw_payload(client)
        payload.update(length_mm=1600,quantity=25)
        for row in payload['allocations']:row['yield_factor']=2
        review=client.post('/api/requisition/raw-plans/preview',json=payload);assert review.status_code==200,review.text
        assert review.json()['allocated_quantity']==20 and review.json()['reserve_quantity']==5
        created=client.post('/api/requisition/raw-plans',json=dict(plan=payload,reviewed_hash=review.json()['reviewed_hash'],operation_key='r08-two-up')).json()
        for n,qty in enumerate((10,15)):
            body=dict(received_quantity=qty,idempotency_key=f'r08-part-{n}')
            if n==0:body.update(resolution_action='await_supplier',resolution_reason='分批收料')
            response=client.put(f"/api/incoming/receive/sr{created['stock_item_id']}",json=body)
            assert response.status_code==200,response.text
            assert client.put(f"/api/incoming/receive/sr{created['stock_item_id']}",json=body).status_code==200
            with factory() as db:
                summary=receipt_purpose_summaries_by_order_item_ids(db,[1])[1]
                assert summary['pending_processing_quantity']==(0 if n==0 else 20)
        with factory() as db:
            lots=list(db.scalars(select(InventoryLot)));res=list(db.scalars(select(InventoryReservation)))
            assert sum(l.quantity_available+l.quantity_reserved for l in lots)==25
            assert sum(r.reserved_stock_quantity for r in res)==20
            assert sum(r.credited_requirement_quantity for r in res)==40
            allocations=list(db.scalars(select(RawPurchaseReceiptAllocation)))
            assert sum(a.raw_quantity for a in allocations)==20
            # Per-receipt allocation plus spare material retains the exact frozen total.
            total=sum(Decimal(json.loads(l.cost_snapshot_detail_json)['total_cost']) for l in lots)
            assigned=sum(a.total_cost for a in allocations)
            assert assigned>0 and total>assigned
        assert client.put(f"/api/requisition/supplier-orders/{created['supplier_order_id']}/void").status_code==409


from tests.test_n039_composite_bom_requisition import composite_requisition_app


def test_raw_graph_each_real_product_waits_for_processing(composite_requisition_app):
    from tests.test_multilevel_bom_receipt_flow import seed_graph
    from tests.test_n039_composite_bom_requisition import _login as graph_login
    from app.api.bom_cutover import router
    from app.models.production import ProductionCompletion
    from app.models.warehouse_inventory import InventoryLot
    from app.services.receipt_managed_production import receipt_purpose_summaries_by_order_item_ids
    app,factory=composite_requisition_app;app.include_router(router,prefix='/api/orders')
    seed_graph(factory,a3=True)
    with TestClient(app) as client:
        graph_login(client)
        choices=client.post('/api/requisition/raw-plans/choices',json={'order_item_ids':[1]})
        assert choices.status_code==200,choices.text
        rows=choices.json()['requirements'];assert len(rows)==3
        payload=dict(order_item_ids=[1],length_mm=1000,width_mm=700,quantity=105,sheet_type='raw_board',flute_direction='length',
            allocations=[dict(source_key=r['source_key'],yield_factor=1,rotate=False,piece_flute_direction='length') for r in rows])
        review=client.post('/api/requisition/raw-plans/preview',json=payload);assert review.status_code==200,review.text
        assert review.json()['allocated_quantity']==100
        created=client.post('/api/requisition/raw-plans',json=dict(plan=payload,reviewed_hash=review.json()['reviewed_hash'],operation_key='r08-graph'))
        assert created.status_code==200,created.text
        receipt=client.put(f"/api/incoming/receive/sr{created.json()['stock_item_id']}",json=dict(received_quantity=105,idempotency_key='r08-graph-in'))
        assert receipt.status_code==200,receipt.text
        with factory() as db:
            assert not list(db.scalars(select(ProductionCompletion)))
            assert receipt_purpose_summaries_by_order_item_ids(db,[1])[1]['requires_component_processing']
        for pid,qty in ((2,30),(3,40)):
            review=client.post('/api/orders/items/1/component-processing/preview',json={'product_id':pid})
            assert review.status_code==200,review.text
            assert review.json()['quantity']==qty
            saved=client.post('/api/orders/items/1/component-processing/execute',json=dict(product_id=pid,reviewed_hash=review.json()['reviewed_hash'],operation_key=f'r08-graph-process-{pid}'))
            assert saved.status_code==200,saved.text
        with factory() as db:
            lot=db.scalar(select(InventoryLot).where(InventoryLot.cost_snapshot_source=='raw_purchase_receipt'))
            assert (lot.quantity_available,lot.quantity_reserved,lot.quantity_consumed)==(5,0,100)
            summary=receipt_purpose_summaries_by_order_item_ids(db,[1])[1]
            assert not summary['requires_component_processing'] and not summary['projection_inconsistent']


def test_raw_migration_immutable_facts_and_nonempty_downgrade_refused(requisition_app):
    import importlib.util
    import pytest
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError
    from app.models import Base
    app,factory=requisition_app;setup_raw(app,factory)
    engine=factory.kw['bind']
    spec=importlib.util.spec_from_file_location('r08_migration','alembic/versions/rp0919_raw_purchase_plans.py')
    migration=importlib.util.module_from_spec(spec);spec.loader.exec_module(migration)
    with engine.begin() as conn:
        for name in ('raw_purchase_delivery_cost_portions','raw_purchase_receipt_allocations','raw_purchase_demands','raw_purchase_plans'):
            Base.metadata.tables[name].drop(conn)
        migration.op=Operations(MigrationContext.configure(conn));migration.upgrade()
    with TestClient(app) as client:
        _login(client,'admin');payload=raw_payload(client)
        review=client.post('/api/requisition/raw-plans/preview',json=payload)
        created=client.post('/api/requisition/raw-plans',json=dict(plan=payload,reviewed_hash=review.json()['reviewed_hash'],operation_key='r08-migration'))
        assert created.status_code==200,created.text
    for sql in ('UPDATE raw_purchase_plans SET snapshot_json=\'{}\'','DELETE FROM raw_purchase_plans','UPDATE raw_purchase_demands SET raw_quantity=21'):
        with engine.begin() as conn:
            with pytest.raises(IntegrityError):conn.execute(text(sql))
    with engine.begin() as conn:
        migration.op=Operations(MigrationContext.configure(conn))
        with pytest.raises(RuntimeError,match='禁止删除历史'):migration.downgrade()
        assert conn.execute(text('SELECT count(*) FROM raw_purchase_plans')).scalar_one()==1
        assert conn.execute(text('SELECT count(*) FROM raw_purchase_demands')).scalar_one()==2


def test_explicit_raw_plan_for_ordinary_box_requires_real_processing(requisition_app):
    from app.models.product import Product
    from app.models.order import OrderItem
    from app.api.bom_cutover import router
    app,factory=requisition_app;setup_raw(app,factory);app.include_router(router,prefix='/api/orders')
    with factory() as db:
        item=db.get(OrderItem,1);pid=item.product_id;db.get(Product,pid).box_style='A1';db.commit()
    with TestClient(app) as client:
        _login(client,'admin');payload=raw_payload(client);payload['quantity']=25
        review=client.post('/api/requisition/raw-plans/preview',json=payload);assert review.status_code==200,review.text
        assert len(review.json()['requirements'])==1 and review.json()['allocated_quantity']==20
        result=client.post('/api/requisition/raw-plans',json=dict(plan=payload,reviewed_hash=review.json()['reviewed_hash'],operation_key='r08-a1'))
        assert result.status_code==200,result.text
        receipt=client.put(f"/api/incoming/receive/sr{result.json()['stock_item_id']}",json=dict(received_quantity=25,idempotency_key='r08-a1-in'))
        assert receipt.status_code==200,receipt.text
        review=client.post('/api/orders/items/1/component-processing/preview',json={'product_id':pid});assert review.status_code==200,review.text
        assert review.json()['quantity']==20
        result=client.post('/api/orders/items/1/component-processing/execute',json=dict(product_id=pid,reviewed_hash=review.json()['reviewed_hash'],operation_key='r08-a1-process'))
        assert result.status_code==200,result.text
