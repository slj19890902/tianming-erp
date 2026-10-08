from fastapi.testclient import TestClient
from sqlalchemy import select
from tests.test_n039_composite_bom_requisition import composite_requisition_app, _login
from tests.test_p1_81_receipt_purpose_flow import _p181_published_map_identity, _freeze_receipt_fact, _receive
from tests.test_multilevel_bom_receipt_flow import seed_graph, purchase_sources
from app.models.multilevel_bom import BomAssembly
from app.models.warehouse_inventory import InventoryLot


def command(row, key='confirm-physical-assembly'):
    return dict(operation_key=key, physical_assembly_confirmed=True,
        source_lot_versions=row['source_lot_versions'], available_lot_ids=row['available_lot_ids'],
        expected_outputs=row['expected_outputs'],
        target_locations={o['product_id']: o['location_id'] for o in row['outputs']})


def test_receipts_wait_then_explicit_assembly_and_history(composite_requisition_app, _p181_published_map_identity, monkeypatch):
    app, factory = composite_requisition_app
    from app.api.deliveries import router as delivery_router
    app.include_router(delivery_router,prefix='/api/deliveries')
    mid, snapshots = seed_graph(factory)
    from app.models.processing_cost import ProcessingCostSettings
    with factory() as db:
        settings = db.get(ProcessingCostSettings, 1)
        settings.average_worker_monthly_salary = 5500
        settings.average_worker_monthly_social_cost = 0
        db.commit()
    with TestClient(app) as client:
        _login(client)
        for index, source in enumerate(purchase_sources(client, factory, mid, snapshots)):
            fact = _freeze_receipt_fact(client, source, idempotency_key=f'price396-{index}', unit_price='0.1234')
            assert fact.status_code == 200, fact.text
            result = _receive(client, source, fact.json(), quantity=source.order_purpose_sheet_qty,
                idempotency_key=f'receipt396-{index}')
            assert result.status_code == 200, result.text
        with factory() as db:
            assert list(db.scalars(select(BomAssembly))) == []
            lots = list(db.scalars(select(InventoryLot).where(InventoryLot.inventory_type == 'finished')))
            assert {l.finished_detail.product_id: l.quantity_reserved for l in lots} == {2:30,3:40}
            assert all(l.quantity_consumed == 0 for l in lots)
        from sqlalchemy import event
        def read_only(conn, cursor, statement, parameters, context, many):
            assert not statement.lstrip().upper().startswith(('INSERT','UPDATE','DELETE','REPLACE'))
        event.listen(factory.kw['bind'], 'before_cursor_execute', read_only)
        pending = client.get('/api/production/pending-assemblies')
        event.remove(factory.kw['bind'], 'before_cursor_execute', read_only)
        assert pending.status_code == 200, pending.text
        row = pending.json()['items'][0]
        assert row['expected_outputs'] == {'1':10}, row
        payload = command(row)
        refused = client.post('/api/production/assemblies/1/confirm', json={**payload,'physical_assembly_confirmed':False})
        assert refused.status_code == 409
        invalid = [
            {**payload,'source_lot_versions':{k:v+1 for k,v in row['source_lot_versions'].items()}},
            {**payload,'expected_outputs':{'1':11}},
            {**payload,'expected_outputs':{'1':-1}},
            {**payload,'available_lot_ids':row['available_lot_ids']+[999999]},
        ]
        for body in invalid:
            rejected = client.post('/api/production/assemblies/1/confirm', json=body)
            assert rejected.status_code == 409, rejected.text
        from app.services import audit_log
        from app.services.bom_subkits import SubkitError
        with monkeypatch.context() as patch:
            original = audit_log.append_audit_event
            def fail_final_audit(*args, **kwargs):
                if kwargs.get('action_code') == 'bom.confirm_assembly':
                    raise SubkitError('模拟审计失败')
                return original(*args, **kwargs)
            patch.setattr(audit_log, 'append_audit_event', fail_final_audit)
            failed = client.post('/api/production/assemblies/1/confirm', json=payload)
            assert failed.status_code == 409, failed.text
        with factory() as db:
            assert list(db.scalars(select(BomAssembly))) == []
        confirmed = client.post('/api/production/assemblies/1/confirm', json=payload)
        assert confirmed.status_code == 200, confirmed.text
        replay = client.post('/api/production/assemblies/1/confirm', json=payload)
        assert replay.status_code == 200 and replay.json() == confirmed.json(), replay.text
        assert client.get('/api/production/pending-assemblies').json()['total'] == 0
        history = client.get('/api/production/completions',params={'include_stock':True,'page':1,'page_size':1})
        assert history.status_code == 200, history.text
        assert history.json()['total'] == 1, history.text
        finished = history.json()['items'][0]
        assert finished['origin'] == 'bom_assembly'
        assert finished['actual_output_quantity'] == 10
        assert finished['current_inventory_quantity'] == 10
        assert finished['current_warehouse_location_id']
        assert sorted(r['quantity'] for r in finished['assembly_inputs']) == [30,40]
        from app.core.time_contract import beijing_today
        delivered = client.post('/api/deliveries',json={'customer_id':1,'delivery_date':beijing_today().isoformat(),
            'items':[{'order_item_id':1,'delivered_quantity':4}]})
        assert delivered.status_code == 201, delivered.text
        delivery_id = delivered.json()['id']
        sent = client.put(f'/api/deliveries/{delivery_id}/dispatch')
        assert sent.status_code == 200, sent.text
        from app.services.multilevel_bom_cost_lineage import graph_material_sources
        with factory() as db:
            assert sum(a.quantity for a in db.scalars(select(BomAssembly))) == 10
            output = db.scalar(select(InventoryLot).where(InventoryLot.source_ref_type=='bom_assembly'))
            assert output.quantity_consumed == 4 and output.quantity_reserved == 6
            from app.services.inventory_valuation import cost_payload
            frozen = cost_payload(output, db)
            assert frozen['standard_labour_unit_cost'] == '0.5288'
            settings = db.get(ProcessingCostSettings, 1)
            settings.average_worker_monthly_salary = 11000
            db.flush()
            assert cost_payload(output, db)['standard_labour_unit_cost'] == '0.5288'
            assert len(graph_material_sources(db,output)) == 2
            from app.services.bom_assembly_history import history as assembly_history
            from app.services.bom_pending_assembly import pending as pending_assembly
            assert assembly_history(db, {999})[0] == []
            assert pending_assembly(db, {999}) == []
            from app.models.user import User
            db.get(User,1).role = 'finance'
            db.commit()
        assert client.post('/api/production/assemblies/1/confirm',json=payload).status_code == 403


def test_old_finished_stock_reserved_not_assembled(composite_requisition_app, _p181_published_map_identity):
    from app.services.bom_auto_reservation import reserve_new_order_stock
    from app.services.production_workflow import _receipt_auto_finished_ground_target
    from app.services.warehouse_inventory import manual_finished_in
    from app.services.multilevel_bom_requirements import read_graph_requirements
    from app.core.time_contract import beijing_today
    app, factory = composite_requisition_app
    seed_graph(factory)
    with factory() as db:
        from app.models.product import Product
        material=db.get(Product,2).material
        material.quote_price=2;material.price_unit='元/㎡'
        material.purchase_currency='CNY';material.purchase_tax_included=True
        ids = []
        for pid, qty in [(2,32),(3,38)]:
            target = _receipt_auto_finished_ground_target(db, claim=True, customer_id=1, product_id=pid)
            lot = manual_finished_in(db, customer_id=1, product_id=pid, location_id=target.location.id,
                quantity=qty, stock_date=beijing_today(), source_type='manual', remarks='隔离已加工子件',
                operator_id=1, idempotency_key=f'old396-{pid}',expected_layout_version=target.layout_version)
            from decimal import Decimal
            lot.estimated_unit_cost_snapshot = Decimal('0.1234')
            # Old-stock fixture needs explicit reviewed entry-cost evidence,
            # not just an amount that bypasses the inherited-cost gate.
            from app.services.inventory_valuation import CONFIRMED_SOURCE
            lot.cost_snapshot_source = CONFIRMED_SOURCE
            lot.cost_snapshot_detail_json = '{"currency":"CNY","basis":"isolated_test_reviewed_cost"}'
            ids.append(lot.id)
        # Free stock now appears in the read-only queue, without being reserved.
        from app.services.bom_pending_assembly import pending as pending_assembly
        stock_rows = pending_assembly(db, None)
        assert len(stock_rows) == 1 and stock_rows[0]['source_kind'] == 'stock'
        assert stock_rows[0]['available_sets'] == 9
        assert pending_assembly(db, {999}) == []
        assert [(db.get(InventoryLot,lid).quantity_available,db.get(InventoryLot,lid).quantity_reserved) for lid in ids] == [(32,0),(38,0)]
        reserve_new_order_stock(db, order_item_id=1, operator_id=1)
        assert reserve_new_order_stock(db, order_item_id=1, operator_id=1) == []
        assert [(db.get(InventoryLot,lid).quantity_available,db.get(InventoryLot,lid).quantity_reserved) for lid in ids] == [(2,30),(0,38)]
        assert list(db.scalars(select(BomAssembly))) == []
        plan = read_graph_requirements(db,1).plan
        assert {m.product_id:m.purchase_sheets for m in plan.materials} == {2:0,3:2}
        db.commit()
    with TestClient(app) as client:
        _login(client)
        row = client.get('/api/production/pending-assemblies').json()['items'][0]
        assert row['expected_outputs'] == {'1':9}
        assert {c['product_id']:c['missing'] for c in row['children']} == {2:0,3:2}
        partial = command(row, 'partial396')
        partial['expected_outputs'] = {'1':5}
        saved = client.post('/api/production/assemblies/1/confirm', json=partial)
        assert saved.status_code == 200, saved.text
        changed = client.post('/api/production/assemblies/1/confirm', json={**partial,'expected_outputs':{'1':6}})
        assert changed.status_code == 409
        remaining = client.get('/api/production/pending-assemblies').json()['items'][0]
        assert remaining['expected_outputs'] == {'1':4}
        undone = client.post('/api/production/assemblies/'+str(saved.json()['assembly_ids'][0])+'/reverse',json={'confirm_reverse':True})
        assert undone.status_code == 200, undone.text
        assert client.get('/api/production/pending-assemblies').json()['items'][0]['expected_outputs'] == {'1':9}


def test_real_new_order_automatically_reserves_exact_stock(composite_requisition_app, _p181_published_map_identity):
    from app.services.production_workflow import _receipt_auto_finished_ground_target
    from app.services.warehouse_inventory import manual_finished_in
    from app.core.time_contract import beijing_today
    from tests.test_multilevel_bom_order_entry import payload
    from app.services.multilevel_bom_requirements import read_graph_requirements
    from app.models.warehouse_inventory import InventoryReservation
    app, factory = composite_requisition_app
    seed_graph(factory)
    with factory() as db:
        from app.models.product import Product
        material=db.get(Product,2).material
        material.quote_price=2;material.price_unit='元/㎡'
        material.purchase_currency='CNY';material.purchase_tax_included=True
        target = _receipt_auto_finished_ground_target(db,claim=True,customer_id=1,product_id=2)
        lot = manual_finished_in(db,customer_id=1,product_id=2,location_id=target.location.id,
            quantity=80,stock_date=beijing_today(),source_type='manual',remarks='已加工',operator_id=1,
            idempotency_key='new-order396',expected_layout_version=target.layout_version)
        lid = lot.id
        db.commit()
    with TestClient(app) as client:
        _login(client)
        created = client.post('/api/orders',json=payload(factory,key='auto396'))
        assert created.status_code == 201, created.text
        iid = created.json()['items'][0]['id']
    with factory() as db:
        assert db.get(InventoryLot,lid).quantity_reserved == 30
        assert db.get(InventoryLot,lid).quantity_available == 50
        assert {m.product_id:m.purchase_sheets for m in read_graph_requirements(db,iid).plan.materials} == {2:0,3:40}
        assert db.scalar(select(InventoryReservation.order_item_id).where(InventoryReservation.inventory_lot_id==lid)) == iid
        from app.services.bom_pending_assembly import pending
        rows = pending(db, {1})
        order_row = next(row for row in rows if row.get('order_item_id') == iid)
        assert not any(order_row['expected_outputs'].values())
        assert {c['product_id']:c['missing'] for c in order_row['children']} == {2:0,3:40}
        assert order_row['sources'][0]['quantity'] == 30
        stock_row = next(row for row in rows if row.get('source_kind') == 'stock')
        assert stock_row['available_sets'] == 0
        assert stock_row['sources'][0]['quantity'] == 50
        assert {c['product_id']:c['missing_next_set'] for c in stock_row['children']} == {2:0,3:4}
        assert pending(db, {999}) == []
        assert list(db.scalars(select(BomAssembly))) == []
