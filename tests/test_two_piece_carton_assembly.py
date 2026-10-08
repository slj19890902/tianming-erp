"""Two different die-cut pieces form one carton only after physical gluing."""
from decimal import Decimal
from fastapi.testclient import TestClient
from sqlalchemy import select
from tests.test_n039_composite_bom_requisition import composite_requisition_app, _login
from tests.test_p1_81_receipt_purpose_flow import _p181_published_map_identity
from tests.test_bom_confirmation396 import seed_priced_graph, command


def test_two_specs_and_molds_remain_separate_in_requisition(composite_requisition_app, _p181_published_map_identity):
    from tests.test_multilevel_bom_receipt_flow import seed_graph
    from app.models.product_bom import SalesOrderItemBomComponent, RequisitionItemBomSource
    from app.models.requisition import RequisitionItem
    app,factory=composite_requisition_app
    _,snapshots=seed_graph(factory,quantity=10,two_piece=True)
    items=[]
    with factory() as db:
        for sid,pid in snapshots:
            s=db.get(SalesOrderItemBomComponent,sid)
            items.append(dict(order_item_id=1,bom_snapshot_id=sid,component_type='whole',
                cardboard_len=float(s.snapshot_component_report_length_mm),
                cardboard_width=float(s.snapshot_component_report_width_mm),
                special_process='一开一',actual_yield_per_sheet=1,requisition_qty=10))
    with TestClient(app) as client:
        _login(client)
        body=dict(request_key='two-piece-independent-materials',supplier_name='苏州纸板供应商',items=items)
        saved=client.post('/api/requisition/batches',json=body)
        assert saved.status_code==201,saved.text
        replay=client.post('/api/requisition/batches',json=body)
        assert replay.status_code==201,replay.text
    with factory() as db:
        rows=list(db.scalars(select(RequisitionItem)))
        assert len(rows)==2
        assert {(r.cardboard_len,r.cardboard_width,r.requisition_qty) for r in rows}=={(1800,900,10),(1500,750,10)}
        links=list(db.scalars(select(RequisitionItemBomSource)))
        assert len(links)==2
        frozen=[db.get(SalesOrderItemBomComponent,sid) for sid,_ in snapshots]
        assert len({s.snapshot_mold_tool_id for s in frozen})==2


def test_two_piece_carton_frozen_molds_partial_gluing_delivery_and_reversal(composite_requisition_app, _p181_published_map_identity):
    from app.api.deliveries import router
    from app.core.time_contract import beijing_today
    from app.models.product import Product
    from app.models.warehouse_inventory import InventoryLot
    from app.models.multilevel_bom import BomAssembly
    from app.services.production_workflow import _receipt_auto_finished_ground_target, refresh_production_task
    from app.services.warehouse_inventory import manual_finished_in
    from app.services.bom_auto_reservation import reserve_new_order_stock
    from app.services.multilevel_bom_orders import read_compiled_order_bom
    from app.services.multilevel_bom_requirements import read_graph_requirements
    app, factory = composite_requisition_app
    app.include_router(router,prefix='/api/deliveries')
    seed_priced_graph(factory,quantity=100,two_piece=True)
    lots={}
    with factory() as db:
        refresh_production_task(db,1,create_if_missing=True)
        compiled=read_compiled_order_bom(db,1)
        children=[s for s in compiled.snapshots if s.component_product_id in (2,3)]
        assert len({s.snapshot_mold_tool_id for s in children})==2
        assert {(s.snapshot_component_report_length_mm,s.snapshot_component_report_width_mm) for s in children}=={(1800,900),(1500,750)}
        assert next(n for n in compiled.graph.nodes if n.product_id==1).unit=='只'
        before=read_graph_requirements(db,1).plan
        assert {m.product_id:m.purchase_sheets for m in before.materials}=={2:100,3:100}
        for pid,qty in ((2,120),(3,80)):
            target=_receipt_auto_finished_ground_target(db,claim=True,customer_id=1,product_id=pid)
            lots[pid]=manual_finished_in(db,customer_id=1,product_id=pid,location_id=target.location.id,
                quantity=qty,stock_date=beijing_today(),source_type='manual',remarks='虚构已模切片料，待实际粘合',
                operator_id=1,idempotency_key=f'two-piece-stock-{pid}',expected_layout_version=target.layout_version).id
        reserve_new_order_stock(db,order_item_id=1,operator_id=1)
        assert list(db.scalars(select(BomAssembly)))==[]
        after=read_graph_requirements(db,1).plan
        assert {m.product_id:m.purchase_sheets for m in after.materials}=={2:0,3:20}
        original_locations={pid:db.get(InventoryLot,lid).warehouse_location_id for pid,lid in lots.items()}
        # A later mold change must not reinterpret already frozen pieces/orders.
        old_mold=children[0].snapshot_mold_tool_id
        db.get(Product,children[0].component_product_id).mold_tool_id=children[1].snapshot_mold_tool_id
        assert read_compiled_order_bom(db,1).snapshots[0].id
        assert next(s for s in read_compiled_order_bom(db,1).snapshots if s.component_product_id==children[0].component_product_id).snapshot_mold_tool_id==old_mold
        db.commit()
    with TestClient(app) as client:
        _login(client)
        row=next(r for r in client.get('/api/production/pending-assemblies').json()['items'] if r.get('order_item_id')==1)
        assert row['expected_outputs']=={'1':80}
        assert next(o for o in row['outputs'] if o['product_id']==1)['unit']=='只'
        body=command(row,'two-piece-glue-75');body['expected_outputs']={'1':75}
        assert client.post('/api/production/assemblies/1/confirm',json={**body,'physical_assembly_confirmed':False}).status_code==409
        posted=client.post('/api/production/assemblies/1/confirm',json=body)
        assert posted.status_code==200,posted.text
        assert client.post('/api/production/assemblies/1/confirm',json=body).json()==posted.json()
        assembly_id=posted.json()['assembly_ids'][0]
        with factory() as db:
            assembly=db.get(BomAssembly,assembly_id)
            assert assembly.quantity==75 and assembly.total_cost>Decimal('0')
            for pid,remaining in ((2,45),(3,5)):
                lot=db.get(InventoryLot,lots[pid])
                assert lot.quantity_available+lot.quantity_reserved==remaining
                assert lot.quantity_consumed==75
                assert lot.warehouse_location_id==original_locations[pid]
            output=db.get(InventoryLot,assembly.output_lot_id)
            from app.services.warehouse_display_units import lot_display_unit
            assert lot_display_unit(output)=='只'
            assert output.quantity_available+output.quantity_reserved==75
        history=client.get('/api/production/completions',params={'include_stock':True}).json()['items']
        assert next(r for r in history if r.get('bom_assembly_id')==assembly_id)['output_unit']=='只'
        sent=client.post('/api/deliveries',json=dict(customer_id=1,delivery_date=beijing_today().isoformat(),items=[dict(order_item_id=1,delivered_quantity=10)]))
        assert sent.status_code==201,sent.text
        did=sent.json()['id'];dispatch=client.put(f'/api/deliveries/{did}/dispatch')
        assert dispatch.status_code==200,dispatch.text
        with factory() as db:
            assert all(db.get(InventoryLot,lid).quantity_consumed==75 for lid in lots.values())
            output=db.get(InventoryLot,db.get(BomAssembly,assembly_id).output_lot_id)
            assert output.quantity_consumed==10 and output.quantity_available+output.quantity_reserved==65
        assert client.post(f'/api/production/assemblies/{assembly_id}/reverse',json={'confirm_reverse':True}).status_code==409
        cancelled=client.put(f'/api/deliveries/{did}/cancel');assert cancelled.status_code==200,cancelled.text
        reversed_result=client.post(f'/api/production/assemblies/{assembly_id}/reverse',json={'confirm_reverse':True})
        assert reversed_result.status_code==200,reversed_result.text
        with factory() as db:
            for pid,quantity in ((2,120),(3,80)):
                lot=db.get(InventoryLot,lots[pid]);assert lot.quantity_available+lot.quantity_reserved==quantity
                assert lot.quantity_consumed==0
