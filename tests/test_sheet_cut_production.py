import json
import pytest
from decimal import Decimal
from sqlalchemy import select
from fastapi.testclient import TestClient
from test_n029_production_service import production_app, _login, _complete
from app.models.product import Product
from app.models.warehouse_inventory import InventoryReservation, InventoryLot, OrderItemSemiRequirement
from app.models.warehouse_goods import WarehouseGoodsProfile
from app.models.production import ProductionCompletion
from app.services.sheet_cut_plan import rectangular_cut_plan
from app.services.semi_finished_inventory import requirement_signature
from app.services.inventory_valuation import frozen_cost


@pytest.mark.parametrize('full_sheet,selected,unused', [(False,3,0), (True,3,0), (True,70,0), (True,70,60)])
def test_completion_uses_frozen_physical_input_and_inherits_cost(production_app, full_sheet,selected,unused):
    input_count=selected-unused
    app, factory, ids = production_app
    with factory() as db:
        reservation = db.scalar(select(InventoryReservation).where(
            InventoryReservation.order_item_id == ids['cases']['direct']['item'],
            InventoryReservation.reservation_type == 'semi_order'))
        lot = db.get(InventoryLot, reservation.inventory_lot_id)
        requirement = db.get(OrderItemSemiRequirement, reservation.semi_requirement_id)
        product = db.get(Product, ids['product_a'])
        product.box_style = '衬板'; product.layer_count = 3; product.flute_type = 'A'
        product.length_mm = product.report_length_mm = requirement.board_length_mm = 1000
        product.width_mm = product.report_width_mm = requirement.board_width_mm = 200
        detail = lot.semi_finished_detail
        detail.board_length_mm = 1120; detail.board_width_mm = 440
        db.add(WarehouseGoodsProfile(lot_id=lot.id, data_json=json.dumps(dict(
            scope='general', processing='cut', customer_ids=[], product_ids=[],
            material_confidence='confirmed', material_code='K=A', face_paper='kraft', mold_tool_id=None, verified_material_id=None))))
        db.flush()
        plan = rectangular_cut_plan(db, lot, product, requirement_signature(requirement))
        assert plan and plan['yield_factor'] == 2
        if full_sheet:
            plan['full_sheet_output_v1'] = True
        if selected==70:
            plan['actual_sheet_input_v1'] = True
        reservation.cut_plan_json = json.dumps(plan)
        reservation.yield_factor = 2; reservation.reserved_stock_quantity = lot.quantity_reserved = selected
        lot.estimated_unit_cost_snapshot = Decimal('4')
        lot.cost_snapshot_source = 'manual_sheet_unit_cost'
        lot.cost_snapshot_detail_json = json.dumps(dict(currency='CNY'))
        source_id = lot.id
        db.commit()
        if unused:
            from app.services.semi_finished_inventory import release_semi_finished_reservation
            release_semi_finished_reservation(db,reservation_id=reservation.id,stock_quantity=unused,
                expected_version=lot.version,operator_id=1,idempotency_key='unused-sheet-return',release_reason='本次未投入')
            db.commit()
            assert reservation.released_requirement_quantity==0
    with TestClient(app) as client:
        _login(client)
        listed = client.get('/api/production/tasks', params={'status':'pending'})
        assert listed.status_code == 200, listed.text
        row = next(row for row in listed.json()['items'] if row['id'] == ids['cases']['direct']['task'])
        assert row['available_material_input_quantity'] == input_count and row['planned_output_quantity'] == (input_count*2 if full_sheet else 5)
        response = _complete(client, ids, 'direct', idempotency_key='cut-complete',expected_version=row['version'])
        assert response.status_code == 200, response.text
        replay = _complete(client, ids, 'direct', idempotency_key='cut-complete',expected_version=row['version'])
        assert replay.status_code == 200, replay.text
    with factory() as db:
        completion = db.get(ProductionCompletion, response.json()['items'][0]['id'])
        assert completion.material_input_quantity == input_count
        assert completion.actual_output_quantity == completion.planned_output_quantity == (input_count*2 if full_sheet else 5)
        if full_sheet:
            assert completion.surplus_finished_quantity == input_count*2-5
        assert db.get(InventoryLot, source_id).quantity_consumed == input_count
        output = db.get(InventoryLot, completion.inventory_lot_id)
        if full_sheet:
            assert output.quantity_available == input_count*2-5
            assert output.quantity_reserved == 5
        unit, evidence = frozen_cost(output, db)
        assert unit == (Decimal('2') if full_sheet else Decimal('2.4')), evidence
        assert Decimal(evidence['total_cost']) == Decimal(4*input_count)
        assert output.cost_snapshot_source == 'sheet_cut_production'

        from test_transfer_cost_lineage import relocate, assert_cost
        first = relocate(db, output, ids['temp1'], 3, 'cut-split')
        assert_cost(db, first, unit)
        second = relocate(db, first, ids['temp2'], 2, 'cut-chain')
        assert_cost(db, second, unit)
        assert_cost(db, relocate(db, second, ids['temp1'], 2, 'cut-full'), unit)
