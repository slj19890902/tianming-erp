"""Physical semi children reserve, assemble and reverse against disposable API data."""
import json
from datetime import date
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.test_multilevel_bom_receipt_flow import composite_requisition_app, _p181_published_map_identity, seed_graph
from tests.test_n039_composite_bom_requisition import _login
from tests.test_bom_confirmation396 import command
from app.models.product import Product
from app.models.warehouse_goods import WarehouseGoodsProfile
from app.models.warehouse_inventory import InventoryLot, InventoryReservation
from app.services.multilevel_bom_orders import read_compiled_order_bom
from app.services.finished_stock_identity import compiled_product_bases
from app.services.bom_auto_reservation import reserve_new_order_stock
from app.services.multilevel_bom_requirements import read_graph_requirements


def seed_physical_graph(factory, **kwargs):
    with factory() as db:
        for product in db.scalars(select(Product)):
            product.flute_type='AB'
        db.commit()
    result=seed_graph(factory, **kwargs)
    with factory() as db:
        material=db.get(Product,2).material
        material.quote_price=2;material.price_unit='元/㎡'
        material.purchase_currency='CNY';material.purchase_tax_included=True
        from app.models.processing_cost import ProcessingCostSettings
        settings=db.get(ProcessingCostSettings,1)
        settings.average_worker_monthly_salary=5500
        settings.average_worker_monthly_social_cost=0
        db.commit()
    return result


def processed_lots(factory, quantities=((2,32),(3,38))):
    from app.services.production_workflow import _receipt_auto_finished_ground_target
    from app.services.warehouse_inventory import manual_semi_finished_in
    ids=[]
    with factory() as db:
        compiled=read_compiled_order_bom(db,1)
        bases=compiled_product_bases(compiled)
        for pid,qty in quantities:
            product=db.get(Product,pid);material=product.material
            target=_receipt_auto_finished_ground_target(db,claim=True,customer_id=1,product_id=pid)
            lot=manual_semi_finished_in(db,location_id=target.location.id,quantity=qty,
                stock_date=date(2026,10,8),source_type='manual',material_code=material.code,
                layer_count=material.layer_count,flute_type=product.flute_type,
                board_length_mm=2000,board_width_mm=1400,sheet_type='net_sheet',component_type='whole',
                pieces_per_box=1,stock_yield_per_sheet=1,supplier_name=material.supplier_name,customer_id=1,
                crease_type=None,crease_left_mm=None,crease_middle_mm=None,crease_right_mm=None,
                cutting_note='已加工子件；尺寸为来源原板',remarks='隔离子件闭环测试',
                operator_id=1,idempotency_key=f'processed-test-{pid}',
                expected_layout_version=target.layout_version)
            db.add(WarehouseGoodsProfile(lot_id=lot.id,data_json=json.dumps(dict(scope='customers',
                customer_ids=[1],product_ids=[pid],processing='cut',output_piece=True,
                dimension_basis='source_board',quantity_unit='pieces',remaining_processes=[],
                physical_basis=bases[pid]))))
            ids.append(lot.id)
        db.commit()
    return ids


@pytest.mark.parametrize('dispatch_output',[False,True])
def test_processed_children_reserve_once_assemble_api_reverse_and_release(composite_requisition_app,_p181_published_map_identity,dispatch_output):
    app,factory=composite_requisition_app;seed_physical_graph(factory)
    ids=processed_lots(factory,((2,32),(3,40 if dispatch_output else 38)))
    with factory() as db:
        assert len(reserve_new_order_stock(db,order_item_id=1,operator_id=1))==2
        assert reserve_new_order_stock(db,order_item_id=1,operator_id=1)==[]
        requirements=read_graph_requirements(db,1)
        assert requirements.finished_units[2]==30 and requirements.finished_units[3]==(40 if dispatch_output else 38)
        assert all(q==0 for q in requirements.physical_credits.values())
        assert [(db.get(InventoryLot,lid).quantity_available,db.get(InventoryLot,lid).quantity_reserved) for lid in ids]==[(2,30),(0,40 if dispatch_output else 38)]
        # Changing the master cannot reject the already frozen order identity.
        db.get(Product,2).default_cutting_mode='一开四';db.get(Product,2).report_length_mm=1200
        db.commit()
    with TestClient(app) as client:
        _login(client)
        pending=client.get('/api/production/pending-assemblies')
        assert pending.status_code==200,pending.text
        row=next(r for r in pending.json()['items'] if r.get('order_item_id')==1)
        assert row['expected_outputs']=={'1':10 if dispatch_output else 9},row
        request=command(row,key='processed-components-assembly')
        result=client.post('/api/production/assemblies/1/confirm',json=request)
        assert result.status_code==200,result.text
        assert client.post('/api/production/assemblies/1/confirm',json=request).json()==result.json()
        from app.models.multilevel_bom import BomAssembly
        with factory() as db:
            conversion=db.scalar(select(BomAssembly).where(BomAssembly.quantity>0))
            cid=conversion.id
            assert [(db.get(InventoryLot,lid).quantity_consumed,db.get(InventoryLot,lid).quantity_reserved) for lid in ids]==([(30,0),(40,0)] if dispatch_output else [(27,3),(36,2)])
            assert all(db.get(InventoryLot,lid).inventory_type=='semi_finished' for lid in ids)
            output=db.get(InventoryLot,conversion.output_lot_id)
            old_cost=output.estimated_unit_cost_snapshot
            assert old_cost>0
        if dispatch_output:
            from app.core.time_contract import beijing_today
            from app.models.delivery import Delivery,DeliveryItem
            from app.models.order import OrderItem
            from app.services.delivery_snapshots import build_order_delivery_snapshot
            from app.services.composite_bom_workflow import execute_delivery_component_consumption
            with factory() as db:
                item=db.get(OrderItem,1)
                delivery=Delivery(delivery_number='PROCESSED-OUTPUT-DISPATCH',customer_id=1,
                    delivery_date=beijing_today(),status='dispatched')
                db.add(delivery);db.flush()
                line=DeliveryItem(delivery_id=delivery.id,order_item_id=item.id,delivered_quantity=1,
                    **build_order_delivery_snapshot(db,item,1))
                db.add(line);db.flush()
                execute_delivery_component_consumption(db,delivery_item_id=line.id,delivery_sets=1,
                    operator_id=1,operation_key='processed-output-dispatch')
                item.delivered_quantity=1;db.commit()
        reversed_result=client.post(f'/api/production/assemblies/{cid}/reverse',json={'confirm_reverse':True})
        if dispatch_output:
            assert reversed_result.status_code==409,reversed_result.text
            with factory() as db:
                assert [db.get(InventoryLot,lid).quantity_consumed for lid in ids]==[30,40]
            return
        assert reversed_result.status_code==200,reversed_result.text
    with factory() as db:
        assert [(db.get(InventoryLot,lid).quantity_available,db.get(InventoryLot,lid).quantity_reserved,db.get(InventoryLot,lid).quantity_consumed) for lid in ids]==[(2,30,0),(0,38,0)]
        from app.api.orders import _release_order_reservations
        _release_order_reservations(db,order_item_ids=[1],operator_id=1,reason='取消测试订单',idempotency_prefix='processed-order-cancel')
        db.commit()
        assert [(db.get(InventoryLot,lid).quantity_available,db.get(InventoryLot,lid).quantity_reserved) for lid in ids]==[(32,0),(38,0)]


def test_processed_parent_finished_first_and_cost_failure_rolls_back(composite_requisition_app,_p181_published_map_identity):
    app,factory=composite_requisition_app;seed_physical_graph(factory)
    ids=processed_lots(factory,((2,30),(3,40)))
    from app.services.production_workflow import _receipt_auto_finished_ground_target
    from app.services.warehouse_inventory import manual_finished_in
    with factory() as db:
        target=_receipt_auto_finished_ground_target(db,claim=True,customer_id=1,product_id=1)
        parent=manual_finished_in(db,customer_id=1,product_id=1,location_id=target.location.id,
            quantity=4,stock_date=date(2026,10,8),source_type='manual',remarks=None,operator_id=1,
            expected_layout_version=target.layout_version,idempotency_key='processed-parent-stock')
        db.commit();parent_id=parent.id
    with factory() as db:
        reserve_new_order_stock(db,order_item_id=1,operator_id=1)
        assert db.get(InventoryLot,parent_id).quantity_reserved==4
        assert [db.get(InventoryLot,lid).quantity_reserved for lid in ids]==[18,24]
        db.rollback()
    with factory() as db:
        lot=db.get(InventoryLot,ids[-1]);lot.estimated_unit_cost_snapshot=None;db.commit()
    from app.services.bom_subkits import SubkitError
    with factory() as db:
        with pytest.raises(SubkitError):reserve_new_order_stock(db,order_item_id=1,operator_id=1)
        db.commit()
        assert db.get(InventoryLot,parent_id).quantity_reserved==0
        assert [db.get(InventoryLot,lid).quantity_reserved for lid in ids]==[0,0]
        assert list(db.scalars(select(InventoryReservation)))==[]


@pytest.mark.parametrize('change',['inactive_location','missing_identity','stale_version'])
def test_processed_reserve_rechecks_candidate_before_mutation(composite_requisition_app,_p181_published_map_identity,change):
    app,factory=composite_requisition_app;seed_physical_graph(factory)
    ids=processed_lots(factory)
    from app.services.processed_component_stock import available_outputs,reserve_output
    from app.services.warehouse_inventory import WarehouseInventoryError
    with factory() as db:
        compiled=read_compiled_order_bom(db,1);bases=compiled_product_bases(compiled)
        lot=available_outputs(db,product_id=2,customer_id=1,expected_basis=bases[2])[0]
        version=lot.version
        if change=='inactive_location':lot.location.is_active=False
        elif change=='missing_identity':
            profile=db.get(WarehouseGoodsProfile,lot.id)
            data=json.loads(profile.data_json);data.pop('physical_basis');profile.data_json=json.dumps(data)
        else:lot.version+=1
        db.commit()
        snapshot=next(s for s in compiled.snapshots if s.component_product_id==2)
        with pytest.raises(WarehouseInventoryError):
            reserve_output(db,compiled=compiled,order_item_id=1,snapshot_id=snapshot.id,lot=lot,
                quantity=3,expected_version=version,operator_id=1,key='reject-changed-output')
        assert lot.quantity_available==32 and lot.quantity_reserved==0
        assert list(db.scalars(select(InventoryReservation)))==[]


def test_processed_children_and_real_body_assemble_together(composite_requisition_app,_p181_published_map_identity):
    app,factory=composite_requisition_app;seed_physical_graph(factory,liner=True,body=True)
    ids=processed_lots(factory,((2,20),(3,60)))
    from app.services.production_workflow import _receipt_auto_finished_ground_target
    from app.services.warehouse_inventory import manual_finished_in
    from app.services.bom_pending_assembly import preview
    from app.services.multilevel_bom_inventory import assemble_order_inventory
    with factory() as db:
        target=_receipt_auto_finished_ground_target(db,claim=True,customer_id=1,product_id=1)
        body=manual_finished_in(db,customer_id=1,product_id=1,location_id=target.location.id,quantity=4,
            stock_date=date(2026,10,8),source_type='manual',remarks=None,operator_id=1,
            expected_layout_version=target.layout_version,idempotency_key='processed-real-body',stock_stage='body')
        reserve_new_order_stock(db,order_item_id=1,operator_id=1)
        row=preview(db,1)
        assert row['expected_outputs']=={4:10,1:4}
        made=assemble_order_inventory(db,order_item_id=1,source_lot_versions=row['source_lot_versions'],
            target_locations={o['product_id']:o['location_id'] for o in row['outputs']},
            available_lot_ids=row['available_lot_ids'],expected_outputs=row['expected_outputs'],
            operation_key='processed-and-body',operator_id=1)
        assert {r.output_product_id:r.quantity for r in made}=={4:10,1:4}
        assert body.quantity_consumed==4
        assert [db.get(InventoryLot,lid).quantity_consumed for lid in ids]==[20,60]
