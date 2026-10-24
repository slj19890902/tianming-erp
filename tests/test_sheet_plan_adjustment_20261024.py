import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from tests.test_n039_composite_bom_requisition import composite_requisition_app, _login
from tests.test_manual_sheet_deduction_20261013 import seed
from tests.test_manual_sheet_modal_20261019 import batch_request, change_stock
from tests.test_manual_sheet_adjustment_20261023 import adjustment

BASE = '/api/warehouse/semi-finished/bom-components/1'


def prepared(client, sessions, *, total=75):
    from app.models.warehouse_inventory import InventoryLot
    lot_id = seed(sessions)
    change_stock(sessions, lot_id, board_length_mm=2000, board_width_mm=1400)
    with sessions() as db:
        db.get(InventoryLot, lot_id).quantity_available = total
        db.commit()
    _login(client)
    preview = client.get(BASE + '/manual-inventory?match_mode=auto').json()
    request = {**batch_request(preview, 70), 'produce_extra_stock':True}
    result = client.post(BASE + '/manual-reserve-batch', json=request)
    assert result.status_code == 200, result.text
    return lot_id, result.json()['reservation_ids'][0]


@pytest.mark.parametrize('target,unused', [(75,0),(72,3),(68,7)])
def test_covered_plan_supports_all_leave_some_and_reduce(composite_requisition_app, target, unused):
    from app.models.warehouse_inventory import InventoryLot
    app, sessions = composite_requisition_app
    with TestClient(app) as client:
        lot_id, rid = prepared(client, sessions)
        data = client.get(BASE + '/manual-inventory?match_mode=auto').json()
        assert data['remaining_required_piece_qty'] == 0
        request = adjustment(client, target, key=f'plan-target-{target}', base=BASE)
        if target > 70:
            request['items'][0]['warning_acknowledged_codes'] = data['existing_reservations'][0].get('warning_codes', [])
        result = client.post(BASE + '/manual-adjust', json=request)
        assert result.status_code == 200, result.text
        assert result.json()['remaining_required_piece_qty'] == 0
        assert client.post(BASE + '/manual-adjust', json=request).json()['replayed']
    with sessions() as db:
        lot = db.get(InventoryLot, lot_id)
        assert (lot.quantity_available, lot.quantity_reserved, lot.quantity_consumed) == (unused,target,0)


def test_covered_plan_can_append_another_physical_batch_without_double_order_credit(composite_requisition_app):
    from app.models.warehouse_inventory import InventoryLot, InventoryReservation, WarehouseLocation
    app, sessions = composite_requisition_app
    with TestClient(app) as client:
        first_id, _ = prepared(client, sessions)
        # Reuse only the seed helper, with unique physical batch/location identity.
        with sessions() as db:
            db.get(InventoryLot, first_id).lot_number = 'FIRST-EXTRA'
            db.get(WarehouseLocation, db.get(InventoryLot, first_id).warehouse_location_id).location_code = 'FIRST-EXTRA'
            db.commit()
        second_id = seed(sessions)
        change_stock(sessions, second_id, board_length_mm=2000, board_width_mm=1400)
        data = client.get(BASE + '/manual-inventory?match_mode=auto').json()
        row = next(row for row in data['items'] if row['lot_id'] == second_id)
        assert row['max_stock_quantity'] == 0 and row['actual_max_stock_quantity'] > 0
        assert row['extra_stock_allowed']
        request = {**batch_request({**data,'items':[row]}, 4), 'produce_extra_stock':True,
            'idempotency_key':'additional-batch-after-covered'}
        result = client.post(BASE + '/manual-reserve-batch', json=request)
        assert result.status_code == 200, result.text
        assert result.json()['allocated_requirement_quantity'] == 0
        assert result.json()['stock_quantity'] == 4
        assert client.post(BASE + '/manual-reserve-batch', json=request).json()['replayed']
        with sessions() as db:
            rows = db.scalars(select(InventoryReservation).where(InventoryReservation.reservation_type=='semi_order')).all()
            assert sum(row.credited_requirement_quantity for row in rows) == 20
            assert db.get(InventoryLot, second_id).quantity_reserved == 4
            from app.services.semi_finished_inventory import consume_semi_finished_reservation
            from app.services.warehouse_inventory import WarehouseInventoryError
            extra = next(row for row in rows if row.inventory_lot_id == second_id)
            with pytest.raises(WarehouseInventoryError, match='需求片数余额'):
                consume_semi_finished_reservation(db,reservation_id=extra.id,stock_quantity=4,
                    expected_version=db.get(InventoryLot,second_id).version,operator_id=1,
                    idempotency_key='not-an-actual-completion')


from tests.test_n029_production_service import production_app


def test_extra_zero_credit_batch_is_consumed_and_costed_in_actual_completion(production_app):
    import json
    from decimal import Decimal
    from app.models.product import Product
    from app.models.order import OrderItem
    from app.models.warehouse_inventory import InventoryLot, InventoryReservation, OrderItemSemiRequirement, SemiFinishedInventoryDetail
    from app.models.warehouse_goods import WarehouseGoodsProfile
    from app.models.production import ProductionCompletion
    from app.services.sheet_cut_plan import rectangular_cut_plan
    from app.services.semi_finished_inventory import requirement_signature
    from app.services.inventory_valuation import frozen_cost
    from tests.test_n029_production_service import _login as production_login, _complete
    app, sessions, ids = production_app
    with sessions() as db:
        reservation = db.scalar(select(InventoryReservation).where(
            InventoryReservation.order_item_id == ids['cases']['direct']['item'],
            InventoryReservation.reservation_type == 'semi_order'))
        lot = db.get(InventoryLot, reservation.inventory_lot_id)
        requirement = db.get(OrderItemSemiRequirement, reservation.semi_requirement_id)
        product = db.get(Product, ids['product_a'])
        product.box_style = '衬板'; product.layer_count = 3; product.flute_type = 'A'
        product.length_mm = product.report_length_mm = requirement.board_length_mm = 1000
        product.width_mm = product.report_width_mm = requirement.board_width_mm = 200
        lot.semi_finished_detail.board_length_mm = 1120
        lot.semi_finished_detail.board_width_mm = 440
        profile = dict(scope='general',processing='cut',customer_ids=[],product_ids=[],
            material_confidence='confirmed',material_code='K=A',face_paper='kraft',mold_tool_id=None,verified_material_id=None)
        db.add(WarehouseGoodsProfile(lot_id=lot.id,data_json=json.dumps(profile)))
        db.flush()
        plan = rectangular_cut_plan(db,lot,product,requirement_signature(requirement))
        assert plan['yield_factor'] == 2
        plan.update(full_sheet_output_v1=True,actual_sheet_input_v1=True)
        reservation.cut_plan_json = json.dumps(plan)
        reservation.yield_factor = 2
        reservation.reserved_stock_quantity = lot.quantity_reserved = 70
        lot.estimated_unit_cost_snapshot = Decimal('4')
        lot.cost_snapshot_source = 'manual_sheet_unit_cost'
        lot.cost_snapshot_detail_json = json.dumps(dict(currency='CNY'))
        first_id = lot.id
        values={column.name:getattr(lot,column.name) for column in InventoryLot.__table__.columns
            if column.name not in {'id','lot_number'}}
        second=InventoryLot(**values,lot_number='EXTRA-PHYSICAL-INPUT')
        second.quantity_available=6;second.quantity_reserved=4;second.estimated_unit_cost_snapshot=Decimal('6')
        db.add(second);db.flush()
        detail=lot.semi_finished_detail
        values={c.name:getattr(detail,c.name) for c in SemiFinishedInventoryDetail.__table__.columns if c.name!='inventory_lot_id'}
        db.add(SemiFinishedInventoryDetail(**values,inventory_lot_id=second.id))
        db.add(WarehouseGoodsProfile(lot_id=second.id,data_json=json.dumps(profile)))
        plan2={**plan,'lot_id':second.id}
        values={c.name:getattr(reservation,c.name) for c in InventoryReservation.__table__.columns
            if c.name not in {'id','reservation_number','inventory_lot_id','idempotency_key','reservation_group_key'}}
        extra=InventoryReservation(**values,reservation_number='EXTRA-PHYSICAL-RES',inventory_lot_id=second.id,
            idempotency_key='extra-physical-reserve',reservation_group_key='extra-physical-reserve')
        extra.reserved_stock_quantity=4;extra.credited_requirement_quantity=0
        extra.cut_plan_json=json.dumps(plan2);db.add(extra)
        second_id=second.id;db.commit()
    with TestClient(app) as client:
        production_login(client)
        response=client.get('/api/production/tasks',params={'status':'pending'})
        row=next(row for row in response.json()['items'] if row['id']==ids['cases']['direct']['task'])
        assert row['available_material_input_quantity']==74
        assert row['planned_output_quantity']==148
        assert sorted(source['remaining_sheet_quantity'] for source in row['customer_board_preparation_sources'])==[4,70]
        saved=_complete(client,ids,'direct',idempotency_key='extra-real-completion',expected_version=row['version'],disposition='stock',location_id=ids['temp1'])
        assert saved.status_code==200,saved.text
        assert _complete(client,ids,'direct',idempotency_key='extra-real-completion',expected_version=row['version'],disposition='stock',location_id=ids['temp1']).status_code==200
    with sessions() as db:
        completion=db.get(ProductionCompletion,saved.json()['items'][0]['id'])
        assert (completion.material_input_quantity,completion.actual_output_quantity,completion.surplus_finished_quantity)==(74,148,143)
        assert db.get(InventoryLot,first_id).quantity_consumed==70
        assert db.get(InventoryLot,second_id).quantity_consumed==4
        assert db.get(InventoryLot,second_id).quantity_available==6
        unit,evidence=frozen_cost(db.get(InventoryLot,completion.inventory_lot_id),db)
        assert Decimal(evidence['total_cost'])==Decimal('304')
        assert unit==Decimal('304')/Decimal('148')

    with TestClient(app) as client:
        production_login(client)
        reversed_result=client.post(f"/api/production/completions/{completion.id}/revert",json={})
        assert reversed_result.status_code==200,reversed_result.text
    with sessions() as db:
        assert db.get(InventoryLot,first_id).quantity_consumed==0
        assert db.get(InventoryLot,first_id).quantity_reserved==70
        assert db.get(InventoryLot,second_id).quantity_consumed==0
        assert db.get(InventoryLot,second_id).quantity_reserved==4


@pytest.mark.parametrize('fault',['stale','warning','audit'])
def test_growth_rejection_preserves_original_plan(composite_requisition_app,monkeypatch,fault):
    from app.models.warehouse_inventory import InventoryLot,InventoryReservation
    app,sessions=composite_requisition_app
    with TestClient(app,raise_server_exceptions=False) as client:
        lot_id,rid=prepared(client,sessions)
        data=client.get(BASE+'/manual-inventory?match_mode=auto').json()
        row=data['existing_reservations'][0]
        request=adjustment(client,75,key='growth-fault-'+fault,base=BASE)
        request['items'][0]['warning_acknowledged_codes']=row['warning_codes']
        if fault=='stale':
            with sessions() as db:db.get(InventoryLot,lot_id).version+=1;db.commit()
        elif fault=='warning':request['items'][0]['warning_acknowledged_codes']=['invented-warning']
        else:
            import app.services.audit_log as audit
            def fail(*args,**kwargs):raise RuntimeError('audit unavailable during growth')
            monkeypatch.setattr(audit,'append_audit_event',fail)
        answer=client.post(BASE+'/manual-adjust',json=request)
        assert answer.status_code==(500 if fault=='audit' else 409),answer.text
    with sessions() as db:
        assert (db.get(InventoryLot,lot_id).quantity_available,db.get(InventoryLot,lot_id).quantity_reserved)==(5,70)
        assert db.get(InventoryReservation,rid).reserved_stock_quantity==70


def test_concurrent_growth_cannot_consume_the_last_sheets_twice(composite_requisition_app):
    from concurrent.futures import ThreadPoolExecutor
    from app.models.warehouse_inventory import InventoryLot
    app,sessions=composite_requisition_app
    with TestClient(app) as first,TestClient(app) as second:
        lot_id,_=prepared(first,sessions);_login(second)
        data=first.get(BASE+'/manual-inventory?match_mode=auto').json()
        request=adjustment(first,75,key='grow-last-five-once',base=BASE)
        request['items'][0]['warning_acknowledged_codes']=data['existing_reservations'][0]['warning_codes']
        with ThreadPoolExecutor(max_workers=2) as pool:
            jobs=[pool.submit(client.post,BASE+'/manual-adjust',json=request) for client in (first,second)]
            answers=[job.result() for job in jobs]
        assert [r.status_code for r in answers]==[200,200],[(r.status_code,r.text) for r in answers]
        assert sum(bool(r.json().get('replayed')) for r in answers)==1
    with sessions() as db:
        assert (db.get(InventoryLot,lot_id).quantity_available,db.get(InventoryLot,lot_id).quantity_reserved)==(0,75)
