import json
from fastapi.testclient import TestClient
from sqlalchemy import select
from tests.test_phase11_requisition import requisition_app,_login
from tests.test_p1_81_receipt_purpose_flow import (_p181_published_map_identity,_seed_material_and_staging,
    _create_frozen_sources,_freeze_receipt_fact,_receive,_posted_finished_quantity)


def test_extra_a3_cover_is_dedicated_unfinished_stock_and_replay_keeps_direction(requisition_app):
    from app.models.warehouse_goods import WarehouseGoodsProfile
    from app.models.warehouse_inventory import InventoryLot
    from app.models.purchase_receipt import IncomingReceiptPurposeAllocation
    app,factory=requisition_app
    _seed_material_and_staging(factory)
    with TestClient(app) as client:
        _login(client,'admin')
        sources=_create_frozen_sources(client,factory,order_quantity=20,purchase_total=40,order_purpose=20,stock_purpose=0,composite=True)
        cover=next(s for s in sources if s.component_type=='cover')
        fact=_freeze_receipt_fact(client,cover,idempotency_key='r07-cover-price').json()
        options={'surplus_disposition':'semi_finished_reserve','processed_component_direction':'length'}
        response=_receive(client,cover,fact,quantity=21,idempotency_key='r07-cover-extra',overrides=options)
        assert response.status_code==200,response.text
        assert _posted_finished_quantity(factory)==0
        with factory() as db:
            allocation=db.scalar(select(IncomingReceiptPurposeAllocation))
            assert allocation.receipt_reserve_purpose_sheet_qty==1
            lot=db.get(InventoryLot,allocation.semi_finished_inventory_lot_id)
            assert lot.inventory_type=='semi_finished' and lot.quantity_available==1
            profile=json.loads(db.get(WarehouseGoodsProfile,lot.id).data_json)
            assert profile['processing']=='dedicated_component'
            assert profile['component_type']=='cover' and profile['remaining_processes']==['nailing']
            assert profile['flute_direction']=='length' and profile['product_ids']
            assert not lot.semi_finished_detail.customer_generic_eligible
            import pytest
            from dataclasses import replace
            from app.models.product import Product
            from app.services.semi_finished_inventory import lot_signature,ensure_semi_finished_lot_eligibility
            from app.services.warehouse_inventory import WarehouseInventoryError
            signature=lot_signature(lot.semi_finished_detail)
            arguments=dict(db=db,lot=lot,product_id=profile['product_ids'][0],customer_id=profile['customer_ids'][0])
            assert ensure_semi_finished_lot_eligibility(**arguments,expected=signature)=='customer_generic'
            for changed in [replace(signature,component_type='base'),replace(signature,component_type='whole'),replace(signature,board_length_mm=signature.board_length_mm+1),replace(signature,flute_type='B')]:
                with pytest.raises(WarehouseInventoryError):ensure_semi_finished_lot_eligibility(**arguments,expected=changed)
            product=db.get(Product,profile['product_ids'][0]);product.box_style='衬板'
            with pytest.raises(WarehouseInventoryError):ensure_semi_finished_lot_eligibility(**arguments,expected=signature)
            db.rollback()

        replay=_receive(client,cover,fact,quantity=21,idempotency_key='r07-cover-extra',overrides=options)
        assert replay.status_code==200,replay.text
        changed=_receive(client,cover,fact,quantity=21,idempotency_key='r07-cover-extra',overrides={**options,'processed_component_direction':'width'})
        assert changed.status_code==409,changed.text
        # A real receipt-backed spare retains its purchase evidence in later processing.
        from tests.test_p1_81_receipt_purpose_flow import _seed_order_semi_reservation
        from app.models.warehouse_inventory import InventoryReservation
        from app.models.order import OrderItem
        from app.services.component_processing import material_sources
        _seed_order_semi_reservation(factory,credited_piece_quantity=1,pieces_per_box=1)
        with factory() as db:
            allocation=db.scalar(select(IncomingReceiptPurposeAllocation))
            lot=db.get(InventoryLot,allocation.semi_finished_inventory_lot_id)
            reservation=db.scalar(select(InventoryReservation))
            reservation.inventory_lot_id=lot.id
            rows,currency=material_sources(db,db.get(OrderItem,1),profile['product_ids'][0],[dict(kind='reservation',id=reservation.id,lot_id=lot.id,before=0,quantity=1,total_cost=str(lot.estimated_unit_cost_snapshot))])
            assert rows[0]['purchase_receipt_fact_id']==allocation.purchase_receipt_fact_id
            assert rows[0]['amount']==lot.estimated_unit_cost_snapshot
            assert currency=='CNY'
            db.rollback()


def test_one_unfinished_cover_reduces_purchase_but_waits_for_actual_processing(requisition_app,monkeypatch):
    from decimal import Decimal
    from app.api.bom_cutover import router as processing_router
    from app.models.order import OrderItem
    from app.models.user import User
    from app.models.warehouse_goods import WarehouseGoodsProfile
    from app.models.warehouse_inventory import InventoryLot,InventoryReservation,OrderItemSemiRequirement
    from app.models.production import ProductionCompletion
    from app.models.purchase_receipt import IncomingReceiptPurposeAllocation
    import tests.test_p1_81_receipt_purpose_flow as fixtures
    app,factory=requisition_app
    app.include_router(processing_router,prefix='/api/orders')
    _seed_material_and_staging(factory)
    original_seed=fixtures._seed_order_semi_reservation
    def seed(*args,**kwargs):
        original_seed(*args,**kwargs)
        with factory() as db:
            requirement=db.scalar(select(OrderItemSemiRequirement));requirement.component_type='cover'
            reservation=db.scalar(select(InventoryReservation));lot=db.get(InventoryLot,reservation.inventory_lot_id)
            lot.estimated_unit_cost_snapshot=Decimal('2.5000')
            lot.semi_finished_detail.component_type='cover';lot.semi_finished_detail.sheet_type='net_sheet'
            item=db.get(OrderItem,1)
            db.add(WarehouseGoodsProfile(lot_id=lot.id,data_json=json.dumps(dict(processing='dedicated_component',completed_processes=['creasing','slotting'],remaining_processes=['nailing'],component_type='cover',flute_direction='length',scope='customers',customer_ids=[requirement.customer_id],product_ids=[item.product_id],verified_material_id=None,material_code='KAKAK',face_paper='kraft',mold_tool_id=None,material_confidence='confirmed',usage_confirmed=True,allow_material_substitution=False))))
            db.commit()
    original_purpose=fixtures._set_purpose_plan
    def purpose(line,**kwargs):
        if line['source_items'][0]['component_type']=='cover':kwargs.update(purchase_total=19,order_purpose=19,stock_purpose=0)
        return original_purpose(line,**kwargs)
    monkeypatch.setattr(fixtures,'_seed_order_semi_reservation',seed)
    monkeypatch.setattr(fixtures,'_set_purpose_plan',purpose)
    with TestClient(app) as client:
        _login(client,'admin')
        sources=_create_frozen_sources(client,factory,order_quantity=20,purchase_total=39,order_purpose=20,stock_purpose=0,composite=True,semi_reserved_piece_quantity=1)
        assert {s.component_type:s.order_purpose_sheet_qty for s in sources}=={'cover':19,'base':20}
        for source in sources:
            fact=_freeze_receipt_fact(client,source,idempotency_key='r07-price-'+source.component_type).json()
            response=_receive(client,source,fact,quantity=source.order_purpose_sheet_qty,idempotency_key='r07-receive-'+source.component_type)
            assert response.status_code==200,response.text
            assert _posted_finished_quantity(factory)==0
        listing=client.get('/api/orders/items/1/component-processing')
        assert listing.status_code==200,listing.text
        from app.services.receipt_managed_production import receipt_purpose_summaries_by_order_item_ids
        with factory() as db:
            summary=receipt_purpose_summaries_by_order_item_ids(db,[1])[1]
            assert summary['requires_component_processing']
            assert summary['pending_processing_quantity']==20
            assert not summary['projection_inconsistent']
        pid=listing.json()['products'][0]['id']
        review=client.post('/api/orders/items/1/component-processing/preview',json={'product_id':pid})
        assert review.status_code==200,review.text
        assert review.json()['quantity']==20
        payload={'product_id':pid,'reviewed_hash':review.json()['reviewed_hash'],'operation_key':'r07-process-20'}
        import pytest
        import app.services.audit_log as audit
        original_audit=audit.append_audit_event
        def fail_audit(*args,**kwargs):raise RuntimeError('r07-injected-audit-failure')
        monkeypatch.setattr(audit,'append_audit_event',fail_audit)
        with pytest.raises(RuntimeError,match='r07-injected-audit-failure'):
            client.post('/api/orders/items/1/component-processing/execute',json=payload)
        monkeypatch.setattr(audit,'append_audit_event',original_audit)
        with factory() as db:
            assert db.scalar(select(InventoryReservation)).consumed_stock_quantity==0
            assert not list(db.scalars(select(ProductionCompletion)))
        stale=client.post('/api/orders/items/1/component-processing/execute',json={**payload,'reviewed_hash':'0'*64})
        assert stale.status_code==409
        saved=client.post('/api/orders/items/1/component-processing/execute',json=payload)
        assert saved.status_code==200,saved.text
        replay=client.post('/api/orders/items/1/component-processing/execute',json=payload)
        assert replay.status_code==200 and replay.json()==saved.json(),replay.text
        with factory() as db:
            completion=db.get(ProductionCompletion,saved.json()['completion_id'])
            assert completion.actual_output_quantity==20 and completion.origin=='manual'
            reservation=db.scalar(select(InventoryReservation))
            assert reservation.consumed_stock_quantity==1
            lot=db.get(InventoryLot,completion.inventory_lot_id)
            actual=sum(a.order_purpose_cost for a in db.scalars(select(IncomingReceiptPurposeAllocation)))+Decimal('2.5000')
            detail=json.loads(lot.cost_snapshot_detail_json)
            assert Decimal(detail['capitalized_material_cost'])==actual
            assert lot.cost_snapshot_source=='component_processing_estimate'
            from app.services.multilevel_bom_cost_lineage import graph_material_sources
            assert graph_material_sources(db,lot) is None
            summary=receipt_purpose_summaries_by_order_item_ids(db,[1])[1]
            assert summary['manual_processing_output_qty']==20
            assert not summary['projection_inconsistent']
            assert summary['currently_unposted_finished_capacity_qty']==0
            allocation=db.scalar(select(IncomingReceiptPurposeAllocation))
            receipt_id=allocation.incoming_receipt_item_id
        blocked=client.put(f'/api/incoming/receipt-items/{receipt_id}/revert',json={'reason':'不得先撤来源收料','idempotency_key':'r07-revert-source'})
        assert blocked.status_code==409,blocked.text
        undone=client.post(f"/api/orders/items/1/component-processing/{saved.json()['completion_id']}/revert")
        assert undone.status_code==200,undone.text
        with factory() as db:
            reservation=db.scalar(select(InventoryReservation))
            assert reservation.consumed_stock_quantity==0
            assert db.get(InventoryLot,reservation.inventory_lot_id).quantity_reserved==1


from tests.test_n039_composite_bom_requisition import composite_requisition_app


def test_component_processing_is_admin_only(requisition_app):
    from app.api.bom_cutover import router
    app,_=requisition_app
    app.include_router(router,prefix='/api/orders')
    with TestClient(app) as client:
        for role in ('sales','workshop'):
            _login(client,role)
            assert client.get('/api/orders/items/1/component-processing').status_code==403
            assert client.post('/api/orders/items/1/component-processing/preview',json={'product_id':1}).status_code==403
            assert client.post('/api/orders/items/1/component-processing/execute',json={'product_id':1,'reviewed_hash':'a'*64,'operation_key':'r07-denied'}).status_code==403
            assert client.post('/api/orders/items/1/component-processing/1/revert').status_code==403


def test_graph_a3_processing_is_scoped_to_its_product_and_reversible(composite_requisition_app):
    from decimal import Decimal
    from tests.test_multilevel_bom_receipt_flow import seed_graph,purchase_sources
    from tests.test_n039_composite_bom_requisition import _login as graph_login
    from tests.test_p1_81_receipt_purpose_flow import _seed_order_semi_reservation
    from app.api.bom_cutover import router
    from app.models.warehouse_inventory import InventoryReservation,InventoryLot,OrderItemSemiRequirement,SemiFinishedLotAllowedProduct
    from app.models.warehouse_goods import WarehouseGoodsProfile
    from app.models.production import ProductionCompletion,ProductionTask
    app,factory=composite_requisition_app;app.include_router(router,prefix='/api/orders')
    material_id,snapshots=seed_graph(factory,a3=True)
    _seed_order_semi_reservation(factory,credited_piece_quantity=1,pieces_per_box=1)
    with factory() as db:
        reservation=db.scalar(select(InventoryReservation));reservation.sales_order_item_bom_component_id=snapshots[0][0]
        requirement=db.get(OrderItemSemiRequirement,reservation.semi_requirement_id)
        requirement.sales_order_item_bom_component_id=snapshots[0][0];requirement.component_type='cover';requirement.required_piece_quantity=30
        requirement.board_length_mm,requirement.board_width_mm=1000,700
        lot=db.get(InventoryLot,reservation.inventory_lot_id);lot.estimated_unit_cost_snapshot=Decimal('0.5')
        lot.semi_finished_detail.board_length_mm,lot.semi_finished_detail.board_width_mm=1000,700
        lot.semi_finished_detail.component_type='cover';lot.semi_finished_detail.sheet_type='net_sheet'
        db.scalar(select(SemiFinishedLotAllowedProduct)).product_id=2
        db.add(WarehouseGoodsProfile(lot_id=lot.id,data_json=json.dumps(dict(processing='dedicated_component',completed_processes=['creasing','slotting'],remaining_processes=['nailing'],component_type='cover',flute_direction='length',scope='customers',customer_ids=[1],product_ids=[2],verified_material_id=material_id,material_code='KAKAK',face_paper='kraft',mold_tool_id=None,material_confidence='confirmed',usage_confirmed=True,allow_material_substitution=False))))
        db.commit()
    with TestClient(app) as client:
        graph_login(client)
        sources=purchase_sources(client,factory,material_id,snapshots,a3=True)
        assert sources[0].order_purpose_sheet_qty==29
        for index,source in enumerate(sources):
            fact=_freeze_receipt_fact(client,source,idempotency_key=f'r07-graph-price-{index}').json()
            receipt=_receive(client,source,fact,quantity=source.order_purpose_sheet_qty,idempotency_key=f'r07-graph-in-{index}')
            assert receipt.status_code==200,receipt.text
        with factory() as db:
            produced=list(db.scalars(select(ProductionCompletion)))
            assert len(produced)==1 and produced[0].actual_output_quantity==40
            task=db.get(ProductionTask,produced[0].task_id)
            assert task.sales_order_item_bom_component_id==snapshots[1][0]
        review=client.post('/api/orders/items/1/component-processing/preview',json={'product_id':2})
        assert review.status_code==200,review.text
        assert review.json()['quantity']==30
        result=client.post('/api/orders/items/1/component-processing/execute',json=dict(product_id=2,reviewed_hash=review.json()['reviewed_hash'],operation_key='r07-graph-finish'))
        assert result.status_code==200,result.text
        undone=client.post(f"/api/orders/items/1/component-processing/{result.json()['completion_id']}/revert")
        assert undone.status_code==200,undone.text
        with factory() as db:
            assert db.scalar(select(InventoryReservation)).consumed_stock_quantity==0
