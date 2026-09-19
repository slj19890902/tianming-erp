"""Customer quantities stay separate from the two identical physical pieces."""
from decimal import Decimal
from fastapi.testclient import TestClient
from sqlalchemy import select
from tests.test_n039_composite_bom_requisition import composite_requisition_app, _login
from tests.test_p1_81_receipt_purpose_flow import _p181_published_map_identity, _freeze_receipt_fact, _receive
from tests.test_multilevel_bom_receipt_flow import seed_graph, purchase_sources


def test_r04_yl_two_pieces_no_assembly_partial_dispatch_and_reverse(composite_requisition_app, _p181_published_map_identity):
    from app.api.deliveries import router
    from app.models.product import Product
    from app.models.user import User
    from app.models.order import OrderItem
    from app.models.multilevel_bom import BomAssembly
    from app.models.warehouse_inventory import InventoryLot
    from app.services.composite_bom import replace_product_bom
    from app.services.multilevel_bom_orders import read_compiled_order_bom
    from app.services.multilevel_bom_plan import plan_bom
    from app.services.composite_bom_workflow import kit_availability
    app, factory = composite_requisition_app
    app.include_router(router, prefix='/api/deliveries')
    from app.api.finance import router as finance
    app.include_router(finance,prefix='/api/finance')
    material, _ = seed_graph(factory, separate=True, quantity=600)
    with factory() as db:
        parent, piece = db.get(Product, 1), db.get(Product, 2)
        parent.customer_material_code = '00006'
        parent.length_mm, parent.width_mm, parent.height_mm = 800, 180, 120
        parent.unit = '只'
        piece.length_mm, piece.width_mm, piece.height_mm = 800, 180, 60
        piece.unit = '片'
        replace_product_bom(db, parent_product_id=1, expected_version=parent.version, user=db.get(User, 1),
            inventory_mode='separate', material_mode='expand_children', delivery_mode='parent',
            components=[dict(component_product_id=2, quantity_per_set=2, inventory_relation='accompany')])
        db.commit()
    with TestClient(app) as client:
        _login(client)
        created = client.post('/api/orders', json={'customer_id':1, 'customer_po':'YL-00006',
            'idempotency_key':'yl-two-pieces', 'items':[{'product_id':1, 'quantity':600,
            'unit_price':'5', 'reservation_plan':{'finished':[], 'semi':[]}}]})
        assert created.status_code == 201, created.text
        result=created.json(); iid=result['items'][0]['id']
        assert Decimal(str(result['total_amount'])) == 3000
        with factory() as db:
            compiled=read_compiled_order_bom(db,iid)
            assert dict(plan_bom(compiled.graph,600).picking)=={2:1200}
            snapshots=[(s.id,s.component_product_id) for s in compiled.snapshots
                       if s.component_product_id == 2]
            assert {pid for _,pid in snapshots} == {2}
        sources=purchase_sources(client,factory,material,snapshots,order_item_id=iid)
        assert len(sources)==1 and sources[0].order_purpose_sheet_qty==1200
        source=sources[0]
        fact=_freeze_receipt_fact(client,source,idempotency_key='yl-price',unit_price='0.5')
        assert fact.status_code==200,fact.text
        received=_receive(client,source,fact.json(),quantity=1199,idempotency_key='yl-1199')
        assert received.status_code==200,received.text
        with factory() as db:
            assert list(db.scalars(select(BomAssembly))) == []
            assert kit_availability(db,iid)['available_sets']==599
        draft=client.post('/api/deliveries',json={'customer_id':1,'items':[{'order_item_id':iid,'delivered_quantity':599}]})
        assert draft.status_code==201,draft.text
        did=draft.json()['id']
        sent=client.put(f'/api/deliveries/{did}/dispatch')
        assert sent.status_code==200,sent.text
        replay=client.put(f'/api/deliveries/{did}/dispatch')
        assert replay.status_code in (200,409),replay.text
        with factory() as db:
            assert db.get(OrderItem,iid).delivered_quantity==599
            lots=[l for l in db.scalars(select(InventoryLot)) if l.finished_detail and l.finished_detail.product_id==2]
            assert sum(l.quantity_consumed for l in lots)==1198
            assert sum(l.quantity_available+l.quantity_reserved for l in lots)==1
            db.get(Product,1).height_mm=999
            db.get(Product,2).height_mm=888
            db.commit()
            assert dict(plan_bom(read_compiled_order_bom(db,iid).graph,600).picking)=={2:1200}
        cancelled=client.put(f'/api/deliveries/{did}/cancel')
        assert cancelled.status_code==200,cancelled.text
        with factory() as db:
            assert db.get(OrderItem,iid).delivered_quantity==0
            assert kit_availability(db,iid)['available_sets']==599
        from tests.test_bom_commercial_settlement import _dispatch,_confirm_receipt
        from app.core.time_contract import beijing_today
        from app.models.finance import Statement, StatementItem
        again=_dispatch(client,1,[(iid,599)])
        _confirm_receipt(client,again)
        statement=client.post('/api/finance/statements',json={'customer_id':1,
            'statement_month':beijing_today().strftime('%Y-%m'),'delivery_ids':[again['id']]})
        assert statement.status_code==201,statement.text
        with factory() as db:
            assert db.get(Statement,statement.json()['id']).total_receivable==Decimal('2995')
            line=db.scalar(select(StatementItem).where(StatementItem.statement_id==statement.json()['id']))
            assert line.actual_received_quantity==599
            assert line.unit_price_snapshot==Decimal('5')
