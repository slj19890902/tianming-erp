from decimal import Decimal
from fastapi.testclient import TestClient
from sqlalchemy import select
from tests.test_n039_composite_bom_requisition import composite_requisition_app, _login
from tests.test_p1_81_receipt_purpose_flow import _p181_published_map_identity
from tests.test_multilevel_bom_receipt_flow import seed_graph


def test_r03_real_parent_and_two_children_are_three_independent_lines(composite_requisition_app, _p181_published_map_identity):
    from app.models.product import Product
    from app.models.order import OrderItem
    from app.models.user import User
    from app.services.composite_bom import replace_product_bom
    from app.services.multilevel_bom_orders import read_compiled_order_bom
    from app.services.multilevel_bom_plan import plan_bom
    app,factory=composite_requisition_app
    from app.api.deliveries import router as deliveries
    from app.api.finance import router as finance
    from tests.test_multilevel_bom_master import save
    app.include_router(deliveries,prefix='/api/deliveries')
    app.include_router(finance,prefix='/api/finance')
    material,_=seed_graph(factory,accompany=True,quantity=10)
    with factory() as db:
        parent=db.get(Product,1);parent.combination_mode='component_priced'
        parent_name=parent.product_name
        replace_product_bom(db,parent_product_id=1,expected_version=parent.version,user=db.get(User,1),
            inventory_mode='manufactured',material_mode='expand_children',delivery_mode='components',
            components=[dict(component_product_id=2,quantity_per_set=1,inventory_relation='accompany'),dict(component_product_id=3,quantity_per_set=2,inventory_relation='accompany')])
        for pid in (2,3):
            save(db,db.get(User,1),pid,'manufactured',[])
        db.commit()
    items=[dict(product_id=pid,quantity=10*ratio,unit_price=price,combination_mode_snapshot='component_priced',
        combination_role='priced_component',combination_group_key='R03-CARTON-GRID-LINER',
        combination_parent_product_id=1,combination_parent_name_snapshot=parent_name,
        combination_set_quantity_snapshot=10,combination_quantity_per_set_snapshot=ratio,
        reservation_plan={'finished':[],'semi':[]}) for pid,ratio,price in [(1,1,'5'),(2,1,'2'),(3,2,'1')]]
    with TestClient(app) as client:
        _login(client)
        r=client.post('/api/orders',json={'customer_id':1,'customer_po':'R03','idempotency_key':'r03-physical-parent','items':items})
        assert r.status_code==201,r.text
        result=r.json();assert len(result['items'])==3
        assert sum(Decimal(str(i['unit_price']))*i['quantity'] for i in result['items'])==Decimal('90')
        assert client.post('/api/orders',json={'customer_id':1,'customer_po':'R03','idempotency_key':'r03-physical-parent','items':items}).json()['id']==result['id']
        from tests.test_n039_composite_bom_requisition import _component_payload
        from tests.test_multilevel_bom_receipt_flow import read_purchase_sources
        from tests.test_p1_81_receipt_purpose_flow import _freeze_receipt_fact, _receive
        from tests.test_bom_commercial_settlement import _dispatch, _confirm_receipt
        from app.core.time_contract import beijing_today
        req=[]
        with factory() as db:
            for row in result['items']:
                graph=read_compiled_order_bom(db,row['id'])
                assert dict(plan_bom(graph.graph,row['quantity']).picking)=={row['product_id']:row['quantity']}
                req.extend({**_component_payload(s.id),'order_item_id':row['id']} for s in graph.snapshots)
        purchased=client.post('/api/requisition/batches',json={'request_key':'r03-material-batch-01','supplier_name':'苏州纸板供应商','items':req})
        assert purchased.status_code==201,purchased.text
        for n,source in enumerate(read_purchase_sources(factory,material)):
            fact=_freeze_receipt_fact(client,source,idempotency_key=f'r03-price-{n}',unit_price='0.5')
            assert fact.status_code==200,fact.text
            received=_receive(client,source,fact.json(),quantity=source.order_purpose_sheet_qty,idempotency_key=f'r03-receive-{n}')
            assert received.status_code==200,received.text
        delivered=[]
        for row in result['items']:
            delivery=_dispatch(client,1,[(row['id'],row['quantity'])])
            _confirm_receipt(client,delivery)
            delivered.append(delivery['id'])
        statement=client.post('/api/finance/statements',json={'customer_id':1,'statement_month':beijing_today().strftime('%Y-%m'),'delivery_ids':delivered})
        assert statement.status_code==201,statement.text
        from app.models.finance import Statement
        with factory() as db:
            assert db.get(Statement,statement.json()['id']).total_receivable==Decimal('90')
    with factory() as db:
        records=list(db.scalars(select(OrderItem).where(OrderItem.order_id==result['id'])))
        parent=next(i for i in records if i.product_id==1)
        compiled=read_compiled_order_bom(db,parent.id)
        assert dict(plan_bom(compiled.graph,10).picking)=={1:10}
        assert {i.component_product_id for i in compiled.snapshots}=={1}
        db.get(Product,1).product_name='未来主档改名';db.commit()
        assert parent.combination_parent_name_snapshot==parent_name
        assert dict(plan_bom(read_compiled_order_bom(db,parent.id).graph,10).picking)=={1:10}
