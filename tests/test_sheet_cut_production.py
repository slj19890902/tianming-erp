import json
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


def test_completion_uses_frozen_physical_input_and_inherits_cost(production_app):
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
        reservation.cut_plan_json = json.dumps(plan)
        reservation.yield_factor = 2; reservation.reserved_stock_quantity = lot.quantity_reserved = 3
        lot.estimated_unit_cost_snapshot = Decimal('4')
        lot.cost_snapshot_source = 'manual_sheet_unit_cost'
        lot.cost_snapshot_detail_json = json.dumps(dict(currency='CNY'))
        source_id = lot.id
        db.commit()
    with TestClient(app) as client:
        _login(client)
        listed = client.get('/api/production/tasks', params={'status':'pending'})
        assert listed.status_code == 200, listed.text
        row = next(row for row in listed.json()['items'] if row['id'] == ids['cases']['direct']['task'])
        assert row['available_material_input_quantity'] == 3 and row['planned_output_quantity'] == 5
        response = _complete(client, ids, 'direct', idempotency_key='cut-complete')
        assert response.status_code == 200, response.text
        replay = _complete(client, ids, 'direct', idempotency_key='cut-complete')
        assert replay.status_code == 200, replay.text
    with factory() as db:
        completion = db.get(ProductionCompletion, response.json()['items'][0]['id'])
        assert completion.material_input_quantity == 3
        assert completion.actual_output_quantity == completion.planned_output_quantity == 5
        assert db.get(InventoryLot, source_id).quantity_consumed == 3
        output = db.get(InventoryLot, completion.inventory_lot_id)
        unit, evidence = frozen_cost(output, db)
        assert unit == Decimal('2.4'), evidence
        assert evidence['total_cost'] == '12.0000'
        assert output.cost_snapshot_source == 'sheet_cut_production'
