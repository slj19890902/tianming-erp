"""Unified body/complete contract: isolated stock, no formal business writes."""
from datetime import date
from decimal import Decimal
import json
import pytest
from tests.test_bom_entry_cost import db, seed
from tests.test_multilevel_bom_receipt_flow import (
    composite_requisition_app, _p181_published_map_identity, seed_graph,
)
from test_p1_33c3_external_packaging_purchase_confirmation import purchase_app


def test_complete_manufactured_parent_cost_and_body_are_distinct(db):
    from app.models.multilevel_bom import ProductBomProfile
    from app.services.inventory_valuation import resolve_product_cost
    from app.services.warehouse_inventory import manual_finished_in, WarehouseInventoryError
    from app.services.bom_inventory_contract import is_body_lot
    parent, children, material, location = seed(db)
    db.get(ProductBomProfile, parent.id).source = 'manufactured'
    parent.box_style = '隔板'
    parent.material_id = material.id
    parent.report_length_mm = 1000
    parent.report_width_mm = 1000
    parent.pieces_per_box = 1
    parent.layer_count = 3
    parent.flute_type = 'B'
    db.flush()
    body = resolve_product_cost(db, parent, stock_stage='body')
    complete = resolve_product_cost(db, parent)
    assert body.estimate, body.missing
    assert complete.estimate, complete.missing
    assert complete.estimate.unit_cost == body.estimate.unit_cost + sum(
        resolve_product_cost(db, child).estimate.unit_cost * q for child,q in zip(children,(5,9,1)))
    args = dict(customer_id=parent.customer_id, product_id=parent.id, location_id=location.id,
        quantity=4, stock_date=date(2026,9,16), source_type='stocktake', remarks=None,
        operator_id=None, idempotency_key='body-stage-test', stock_stage='body')
    lot = manual_finished_in(db, **args)
    assert is_body_lot(lot)
    assert lot.estimated_unit_cost_snapshot == body.estimate.unit_cost
    assert manual_finished_in(db, **args).id == lot.id
    with pytest.raises(WarehouseInventoryError):
        manual_finished_in(db, **{**args, 'stock_stage':'complete'})


def test_body_reservation_reduces_only_body_production_then_assembles_once(
    composite_requisition_app, _p181_published_map_identity
):
    from app.services.production_workflow import _receipt_auto_finished_ground_target
    from app.services.warehouse_inventory import manual_finished_in, finished_inventory_candidates_for_bom_component
    from app.services.bom_auto_reservation import reserve_new_order_stock
    from app.services.multilevel_bom_requirements import read_graph_requirements
    from app.services.bom_pending_assembly import preview
    from app.services.multilevel_bom_inventory import assemble_order_inventory
    from app.services.inventory_valuation import CONFIRMED_SOURCE
    from app.services.bom_inventory_contract import is_body_lot
    app, factory = composite_requisition_app
    material_id, _ = seed_graph(factory, liner=True, body=True)
    with factory() as db:
        from app.models.material import Material
        material = db.get(Material, material_id)
        material.quote_price = Decimal('1')
        material.price_unit = '元/㎡'
        material.purchase_currency = 'CNY'
        material.purchase_tax_included = True
        lots = {}
        for pid, quantity, stage in [(1,4,'body'),(2,20,'complete'),(3,60,'complete')]:
            target = _receipt_auto_finished_ground_target(db,claim=True,customer_id=1,product_id=pid)
            lot = manual_finished_in(db,customer_id=1,product_id=pid,location_id=target.location.id,
                quantity=quantity,stock_date=date(2026,9,16),source_type='manual',remarks='isolated fixture',
                operator_id=1,idempotency_key=f'unified-{pid}',stock_stage=stage,
                expected_layout_version=target.layout_version)
            lot.estimated_unit_cost_snapshot=Decimal('1')
            lot.cost_snapshot_source=CONFIRMED_SOURCE
            lot.cost_snapshot_detail_json=json.dumps({'currency':'CNY','basis':'isolated_test_reviewed_cost'})
            lots[pid]=lot
        reserve_new_order_stock(db,order_item_id=1,operator_id=1)
        assert reserve_new_order_stock(db,order_item_id=1,operator_id=1)==[]
        req=read_graph_requirements(db,1)
        root=next(p for p in req.plan.products if p.product_id==1)
        assert root.make_units==10 and root.body_credited_units==4
        assert {m.product_id:m.purchase_sheets for m in req.plan.materials}=={1:6,2:0,3:0}
        snapshot=next(s for s in req.compiled.snapshots if s.component_product_id==1)
        assert finished_inventory_candidates_for_bom_component(db,order_item_id=1,bom_snapshot_id=snapshot.id)==[]
        row=preview(db,1)
        assert row['expected_outputs']=={4:10,1:4}
        assert any('未组装本体' in s['product_name'] for s in row['sources'])
        command=dict(order_item_id=1,source_lot_versions=row['source_lot_versions'],
            target_locations={o['product_id']:o['location_id'] for o in row['outputs']},
            available_lot_ids=row['available_lot_ids'], expected_outputs=row['expected_outputs'],
            operation_key='unified-assembly',operator_id=1)
        made=assemble_order_inventory(db,**command)
        assert {r.output_product_id:r.quantity for r in made}=={4:10,1:4}
        assert lots[1].quantity_consumed==4 and is_body_lot(lots[1])
        assert [r.id for r in assemble_order_inventory(db,**command)]==[r.id for r in made]
        from app.services.bom_subkit_inventory import reverse_subkit_conversion
        for result in reversed(made):
            reverse_subkit_conversion(db, conversion_id=result.id, operator_id=1, graph_assembly=True)
        assert lots[1].quantity_consumed == 0 and lots[1].quantity_reserved == 4
        assert is_body_lot(lots[1])


def test_purchased_body_receipt_is_not_complete_delivery_stock(purchase_app, _p181_published_map_identity):
    from tests.test_multilevel_bom_external_receipts import prepare, receive, _login, _confirm
    from tests.test_p1_81_receipt_purpose_flow import _seed_material_and_staging
    from fastapi.testclient import TestClient
    from sqlalchemy import select
    from app.models.external_packaging_purchase import ExternalPackagingPurchaseItem
    from app.models.order import OrderItem
    from app.models.user import User
    from app.models.warehouse_inventory import InventoryLot
    from app.services.bom_inventory_contract import is_body_lot
    from app.services.bom_pending_assembly import preview
    from app.services.multilevel_bom_inventory import assemble_order_inventory
    from app.services.composite_bom_workflow import kit_availability
    factory=purchase_app.state.session_factory
    _seed_material_and_staging(factory)
    order_id,item_id,child_id=prepare(purchase_app,body=True,stock_basis=1,purchase_basis=1)
    with TestClient(purchase_app) as client:
        _login(client,'purchase-admin')
        _confirm(client,order_id)
        with factory() as db:
            lines=[(p.purchase_order_id,p.id,p.purchase_quantity) for p in db.scalars(select(
                ExternalPackagingPurchaseItem).where(ExternalPackagingPurchaseItem.sales_order_item_id==item_id))]
        assert len(lines)==2
        for po,pid,qty in lines:
            response=receive(client,po,pid,f'body-receipt-{pid}',qty)
            assert response.status_code==200,response.text
    with factory() as db:
        root=db.get(OrderItem,item_id).product_id
        bodies=[lot for lot in db.scalars(select(InventoryLot)) if is_body_lot(lot)]
        assert len(bodies)==1 and bodies[0].quantity_available+bodies[0].quantity_reserved==10
        ready=kit_availability(db,item_id)
        assert ready['available_sets']==0
        row=preview(db,item_id)
        assert row['expected_outputs']=={root:10}
        actor=db.scalar(select(User).where(User.username=='purchase-admin'))
        made=assemble_order_inventory(db,order_item_id=item_id,source_lot_versions=row['source_lot_versions'],
            target_locations={o['product_id']:o['location_id'] for o in row['outputs']},
            available_lot_ids=row['available_lot_ids'],expected_outputs=row['expected_outputs'],
            operation_key='purchased-body-assembly',operator_id=actor.id)
        assert len(made)==1 and made[0].quantity==10
        assert bodies[0].quantity_consumed==10
        db.commit()
        output_id = made[0].output_lot_id
        assembly_id = made[0].id
    # The real confirmation endpoint above normally reserves the output. Here
    # reserve it through the same existing service before dispatch integration.
    from app.api.deliveries import router
    from app.core.time_contract import beijing_today
    from app.models.order import Order
    from app.services.production_workflow import _reserve_component_completion_lot
    from app.services.multilevel_bom_orders import read_compiled_order_bom
    from app.models.multilevel_bom import BomAssembly
    purchase_app.include_router(router, prefix='/api/deliveries')
    with factory() as db:
        item = db.get(OrderItem, item_id)
        order = db.get(Order, item.order_id)
        customer_id = order.customer_id
        lot = db.get(InventoryLot, output_id)
        snapshot = next(s for s in read_compiled_order_bom(db,item_id).snapshots if s.component_product_id==root)
        _reserve_component_completion_lot(db, completion=db.get(BomAssembly,assembly_id),order=order,item=item,
            snapshot_id=snapshot.id,lot=lot,operator_id=actor.id,reserve_quantity=10,
            idempotency_key=f'bom-output-reserve:{assembly_id}',reservation_number_prefix='BARS')
        db.commit()
    with TestClient(purchase_app) as client:
        _login(client,'purchase-admin')
        created=client.post('/api/deliveries',json={'customer_id':customer_id,'delivery_date':beijing_today().isoformat(),
            'items':[{'order_item_id':item_id,'delivered_quantity':4}]})
        assert created.status_code==201,created.text
        did=created.json()['id']
        dispatched=client.put(f'/api/deliveries/{did}/dispatch')
        assert dispatched.status_code==200,dispatched.text
        cancelled=client.put(f'/api/deliveries/{did}/cancel')
        assert cancelled.status_code==200,cancelled.text
    with factory() as db:
        from app.services.bom_subkit_inventory import reverse_subkit_conversion
        reverse_subkit_conversion(db,conversion_id=assembly_id,operator_id=actor.id,graph_assembly=True)
        db.commit()
    with TestClient(purchase_app) as client:
        _login(client,'purchase-admin')
        from tests.test_multilevel_bom_external_reversal import reverse
        undone=reverse(client,response.json()['receipt']['id'],key='unified-undo-receipt')
        assert undone.status_code==200,undone.text
