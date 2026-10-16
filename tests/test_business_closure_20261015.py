"""User-visible closure; receipt tests never submit manual assembly commands."""
import json
from pathlib import Path
from fastapi.testclient import TestClient
from sqlalchemy import select
from tests.test_n039_composite_bom_requisition import composite_requisition_app, _login
from tests.test_p1_81_receipt_purpose_flow import _p181_published_map_identity, _freeze_receipt_fact, _receive
from tests.test_multilevel_bom_receipt_flow import seed_graph, purchase_sources
from tests.test_receipt_putaway import setup, incoming, choose, rack_factory
from tests.test_p1_09c_104_finished_reservation_action_guard import _method_contents, _run_node


def test_external_pending_stock_entry_matches_backend(tmp_path):
    body = _method_contents('canReserveFinishedForOrderItem')
    _run_node("const fn=new Function('item',"+json.dumps(body)+");" + """
    for (const [item,want] of [
      [{requisition_status:'未报料'},true],
      [{requisition_status:'外购包材待确认',supply_mode_snapshot:'external_purchase'},true],
      [{requisition_status:'外购包材已采购',supply_mode_snapshot:'external_purchase'},false],
      [{requisition_status:'已报料'},false],
      [{requisition_status:'外购包材待确认',supply_mode_snapshot:'corrugated_production'},false]]) {
        if (fn(item)!==want) throw Error(JSON.stringify(item));
    }
    """, tmp_path, 'pending-external-stock.js')
    html=Path('static/index.html').read_text()
    assert 'canSales && canReserveFinishedForOrderItem(orderItemForm)' in html
    assert 'supply_mode_snapshot:itemSnapshot.supply_mode_snapshot' in html


def test_order_receipts_auto_assemble_without_manual_post(composite_requisition_app, _p181_published_map_identity):
    from app.models.multilevel_bom import BomAssembly
    from app.models.warehouse_inventory import InventoryLot
    from app.core.receipt_price_guard import ReceiptPriceGuardSession
    app, factory = composite_requisition_app
    factory.class_ = ReceiptPriceGuardSession
    mid,snapshots=seed_graph(factory)
    with TestClient(app) as client:
        _login(client)
        for index,source in enumerate(purchase_sources(client,factory,mid,snapshots)):
            fact=_freeze_receipt_fact(client,source,idempotency_key=f'auto-price-{index}',unit_price='0.1234')
            assert fact.status_code==200, fact.text
            args=dict(quantity=source.order_purpose_sheet_qty,idempotency_key=f'auto-receipt-{index}')
            result=_receive(client,source,fact.json(),**args)
            assert result.status_code==200,result.text
            assert _receive(client,source,fact.json(),**args).status_code==200
            with factory() as db:
                rows=list(db.scalars(select(BomAssembly)))
                if index==0: assert not rows
        with factory() as db:
            rows=list(db.scalars(select(BomAssembly)))
            assert len(rows)==1
            lot=db.get(InventoryLot,rows[0].output_lot_id)
            assert lot.finished_detail.product_id==1
            assert lot.quantity_reserved==10 and lot.quantity_available==0
            assert lot.cost_snapshot_at is not None


def test_empty_saved_location_and_follow_position(setup):
    from app.services import receipt_putaway as service
    factory,pid,cid,ids=setup
    with factory() as db:
        first=incoming(db,pid,cid,ids[0])
        choose(db,pid,ids[0]);db.flush()
        first.quantity_available=0;db.flush()
        assert service.resolve(db,product_id=pid,customer_id=cid)[0].id==ids[0]
        second=incoming(db,pid,cid,ids[1],key='relocated')
        service.remember_stocktake(db,second,1,follow_position=True)
        assert service.resolve(db,product_id=pid,customer_id=cid)[0].id==ids[1]
        assert first.warehouse_location_id==ids[0]


def test_single_move_updates_default_without_moving_other_stock(setup):
    from tests.test_fixed_shelf import move
    from app.services import receipt_putaway as service
    factory,pid,cid,ids=setup
    with factory() as db:
        first=incoming(db,pid,cid,ids[0])
        choose(db,pid,ids[0])
        original_version=first.version
        result=move(db,first,ids[1],25)
        assert service.info(db,pid)['location_id']==ids[1]
        assert first.quantity_available==100 and result.target_lot.quantity_available==25
        assert first.warehouse_location_id==ids[0]
        version=service.info(db,pid)['version']
        from app.services.warehouse_inventory import transfer_finished_lot_between_locations
        source=first.location; target=result.target_lot.location
        replay=transfer_finished_lot_between_locations(db,lot_id=first.id,expected_version=original_version,
            quantity=25,location_id=target.id,operator_id=1,idempotency_key='move-1',
            expected_source_location_id=source.id,expected_source_address_version=source.address_version,
            expected_source_layout_version=source.floor3_layout.version,
            expected_target_address_version=target.address_version,expected_target_layout_version=target.floor3_layout.version)
        assert replay.replayed
        assert service.info(db,pid)['version']==version


def test_automatic_assembly_audit_failure_rolls_back_receipt(composite_requisition_app, _p181_published_map_identity, monkeypatch):
    from app.models.multilevel_bom import BomAssembly
    from app.services import audit_log
    from app.services.bom_subkits import SubkitError
    from app.core.receipt_price_guard import ReceiptPriceGuardSession
    app,factory=composite_requisition_app;factory.class_=ReceiptPriceGuardSession
    mid,snapshots=seed_graph(factory)
    with TestClient(app) as client:
        _login(client)
        sources=purchase_sources(client,factory,mid,snapshots)
        for index,source in enumerate(sources):
            fact=_freeze_receipt_fact(client,source,idempotency_key=f'fault-price-{index}',unit_price='0.1234')
            assert fact.status_code==200,fact.text
            args=dict(quantity=source.order_purpose_sheet_qty,idempotency_key=f'fault-receipt-{index}')
            if index==len(sources)-1:
                original=audit_log.append_audit_event
                def fail(*a,**kw):
                    if kw.get('action_code')=='bom.auto_assembly':raise SubkitError('模拟自动组套审计失败')
                    return original(*a,**kw)
                with monkeypatch.context() as patch:
                    patch.setattr(audit_log,'append_audit_event',fail)
                    response=_receive(client,source,fact.json(),**args)
                    assert response.status_code==409,response.text
                with factory() as db: assert not list(db.scalars(select(BomAssembly)))
            response=_receive(client,source,fact.json(),**args)
            assert response.status_code==200,response.text
        with factory() as db: assert len(list(db.scalars(select(BomAssembly))))==1


from tests.test_stock_replenishment_flow import stock_replenishment_app

def test_bom_stock_replenishment_receipts_assemble_without_manual_post(stock_replenishment_app):
    from tests.test_stock_replenishment_save_recovery import seed_two_component_plan, login, PATH
    from app.models.warehouse_inventory import InventoryLot
    from app.models.stock_preparation import StockPreparationCommand
    from app.core.receipt_price_guard import ReceiptPriceGuardSession
    app,factory=stock_replenishment_app;factory.class_=ReceiptPriceGuardSession
    policy=seed_two_component_plan(factory)
    from app.models.customer_finished_storage_preference import CustomerFinishedStoragePreference
    from app.models.warehouse_inventory import WarehouseArea
    with factory() as db:
        area=db.scalar(select(WarehouseArea).where(WarehouseArea.area_code=='FG-004'))
        db.add(CustomerFinishedStoragePreference(customer_id=1,warehouse_area_id=area.id,priority=1))
        db.commit()
    with TestClient(app) as client:
        login(client)
        preview=client.get(f'/api/requisition/stock-policies/{policy}/replenishment-draft',params={'finished_quantity':4})
        assert preview.status_code==200,preview.text
        body={**preview.json(),'idempotency_key':'auto-bom-stock-plan','expected_actor_id':1}
        saved=client.post(PATH,json=body);assert saved.status_code==201,saved.text
        ids={r['id'] for r in saved.json()['items']}
        rows=[r for r in client.get('/api/requisition/pending').json()['items'] if r.get('stock_replenishment_item_id') in ids]
        groups={}
        for row in rows:
            groups.setdefault(row['supplier_name'],[]).append(dict(stock_replenishment_item_id=row['stock_replenishment_item_id'],source_fingerprint=row['source_fingerprint']))
        purchase=client.post('/api/requisition/supplier-orders/from-pending-selection',json={'supplier_groups':[
            dict(supplier_name=name,request_key=f'auto-bom-purchase-{i}',stock_sources=parts) for i,(name,parts) in enumerate(groups.items())]})
        assert purchase.status_code==201,purchase.text
        for item in saved.json()['items']:
            payload=dict(received_quantity=item['quantity'],idempotency_key=f"auto-bom-stock-in-{item['id']}")
            receipt=client.put(f"/api/incoming/receive/sr{item['id']}",json=payload)
            assert receipt.status_code==200,receipt.text
            assert client.put(f"/api/incoming/receive/sr{item['id']}",json=payload).status_code==200
        with factory() as db:
            commands=list(db.scalars(select(StockPreparationCommand).where(StockPreparationCommand.operation_key.like('auto-stock-assembly:%'))))
            assert len(commands)==1
            data=json.loads(commands[0].result_json)
            output=db.get(InventoryLot,data['output_lot_id'])
            assert output.quantity_available==4 and output.quantity_reserved==0
            assert output.cost_snapshot_at is not None


def test_bom_auto_assembly_never_borrows_another_replenishment_demand(stock_replenishment_app,monkeypatch):
    from tests.test_stock_replenishment_save_recovery import seed_two_component_plan, login, PATH
    from app.models.product import Product
    from app.models.stock_replenishment import StockReplenishmentOrder,StockReplenishmentOrderItem
    from app.services.stock_replenishment_plan import frozen_bom_plan
    from app.services.stock_preparation_assembly import _plan_identity,preview as assembly_preview
    from app.services import stock_preparation_processing
    from app.core.receipt_price_guard import ReceiptPriceGuardSession
    app,factory=stock_replenishment_app;factory.class_=ReceiptPriceGuardSession
    policy=seed_two_component_plan(factory)
    monkeypatch.setattr(stock_preparation_processing,'_auto_assemble_frozen_parent',lambda *args:None)
    with TestClient(app) as client:
        login(client)
        draft=client.get(f'/api/requisition/stock-policies/{policy}/replenishment-draft',
            params={'finished_quantity':4})
        assert draft.status_code==200,draft.text
        saved=client.post(PATH,json={**draft.json(),'idempotency_key':'scope-order',
            'expected_actor_id':1})
        assert saved.status_code==201,saved.text
        items=saved.json()['items']
        ids={row['id'] for row in items}
        pending=[row for row in client.get('/api/requisition/pending').json()['items']
            if row.get('stock_replenishment_item_id') in ids]
        groups={}
        for row in pending:
            groups.setdefault(row['supplier_name'],[]).append(dict(
                stock_replenishment_item_id=row['stock_replenishment_item_id'],
                source_fingerprint=row['source_fingerprint']))
        purchase=client.post('/api/requisition/supplier-orders/from-pending-selection',json={
            'supplier_groups':[dict(supplier_name=name,request_key=f'scope-purchase-{index}',stock_sources=parts)
                for index,(name,parts) in enumerate(groups.items())]})
        assert purchase.status_code==201,purchase.text
        for row in items:
            result=client.put(f"/api/incoming/receive/sr{row['id']}",json={
                'received_quantity':row['quantity'],'idempotency_key':f"scope-receipt-{row['id']}"})
            assert result.status_code==200,result.text
    with factory() as db:
        rows=[db.get(StockReplenishmentOrderItem,row['id']) for row in items]
        contract=frozen_bom_plan(rows[0])
        parent=db.scalar(select(Product).where(Product.product_code=='RECOVERY-BOM-PARENT'))
        scope=dict(order_id=rows[0].replenishment_order_id,parent_id=parent.id,
            plan_hash=_plan_identity(contract),policy_id=contract['policy_id'],target_sets=4)
        assert assembly_preview(db,parent.id,4,scope=scope)['available_sets']==4
        other=StockReplenishmentOrder(order_number='SCOPE-OTHER',source_type='customer_request',
            customer_id=1,status='confirmed')
        db.add(other);db.flush()
        rows[1].replenishment_order_id=other.id
        db.flush()
        scoped=assembly_preview(db,parent.id,4,scope=scope)
        assert scoped['available_sets']==0
        assert scoped['shortages']
