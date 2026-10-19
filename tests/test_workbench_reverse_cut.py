import json
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from app.api.product_workbench import router
from app.api.deps import get_db, get_current_user
from app.models.product import Product
from app.models.warehouse_inventory import InventoryLot
from test_p1_21b_mobile_admin_product_search import _login, mobile_erp_app
from test_product_workbench import _app
from test_bidirectional_sheet_cut import eligibility_db, prepare


@pytest.mark.parametrize('length,width,report_l,report_w,cuts,score', [
    (2000, 200, 1000, 200, 2, 100),
    (1000, 400, 1000, 200, 2, 100),
    (2000, 400, 1000, 200, 4, 100),
    (2200, 200, 1000, 200, 2, 90.91),
    (1991, 200, 1000, 200, 1, 50.23),
    ('240.20', '80.20', '120.10', '80.20', 2, 100),
])
def test_reverse_counts_all_report_blanks_for_both_sources(mobile_erp_app, length, width, report_l, report_w, cuts, score):
    app, ids, factory = _app(mobile_erp_app)
    with factory() as db:
        p = db.get(Product, ids['product'])
        p.report_length_mm, p.report_width_mm = Decimal(str(report_l)), Decimal(str(report_w))
        p.flute_type, p.layer_count, p.default_material_code = 'B', 3, 'K=A'
        lot = db.scalar(select(InventoryLot).where(InventoryLot.lot_number == 'SF-MOBILE-001'))
        lot.semi_finished_detail.board_length_mm = Decimal(str(length))
        lot.semi_finished_detail.board_width_mm = Decimal(str(width))
        db.commit()
        before = db.execute(text('select * from inventory_lots order by id')).all()
    with TestClient(app) as client:
        _login(client, 'mobile-admin')
        params = dict(length=length, width=width, flute_type='B')
        for source in ({}, {'lot_id': lot.id}):
            response = client.get('/api/product-workbench/reverse', params={**params, **source})
            assert response.status_code == 200, response.text
            row = next(r for r in response.json()['items'] if r['id'] == p.id)
            assert row['match_score'] == score
            assert row['cuts_per_sheet'] == cuts
            assert row['deductible'] is False and row['cut_plan'] is None
            filtered = client.get('/api/product-workbench/reverse', params={**params, **source, 'min_score': 100}).json()
            assert (p.id in {r['id'] for r in filtered['items']}) == (score == 100)
    with factory() as db:
        assert db.execute(text('select * from inventory_lots order by id')).all() == before


def test_reverse_cut_score_filter_and_order_precede_paging(mobile_erp_app):
    app, ids, factory = _app(mobile_erp_app)
    with factory() as db:
        for key, length in [('product', 1100), ('product_two', 1000)]:
            p = db.get(Product, ids[key])
            p.report_length_mm, p.report_width_mm = length, 200
            p.flute_type, p.layer_count = 'B', 3
        p = db.get(Product, ids['other_product'])
        p.report_length_mm, p.report_width_mm, p.flute_type, p.layer_count = 1000, 200, 'AB', 5
        db.commit()
    with TestClient(app) as client:
        _login(client, 'mobile-admin')
        params = dict(length=2000, width=200, flute_type='B', page_size=1)
        first = client.get('/api/product-workbench/reverse', params=params).json()
        assert first['total'] == 2 and first['has_more']
        assert first['items'][0]['id'] == ids['product_two']
        second = client.get('/api/product-workbench/reverse', params={**params, 'page': 2}).json()
        assert second['items'][0]['id'] == ids['product']
        filtered = client.get('/api/product-workbench/reverse', params={**params, 'min_score': 100}).json()
        assert filtered['total'] == 1 and not filtered['has_more']
        assert filtered['items'][0]['id'] == ids['product_two']
        assert client.get('/api/product-workbench/reverse', params={**params, 'length': 200, 'width': 2000}).json()['total'] == 0


def test_actual_plan_keeps_recorded_trim_kerf_and_separate_mold_yield(eligibility_db):
    db, data = eligibility_db
    product, lot, profile, facts, _, _ = prepare(db, data)
    facts.update(processing='raw', cut_trim_mm=20, cut_kerf_mm=2)
    profile.data_json = json.dumps(facts)
    lot.semi_finished_detail.sheet_type = 'raw_board'
    product.sheet_cutting_settings = {'schema_version': 2, 'whole': {
        'length_parts': 1, 'width_parts': 1, 'mold_count': 4, 'is_die_cut': True}}
    db.commit()
    api = FastAPI()
    api.include_router(router, prefix='/api/product-workbench')
    api.dependency_overrides[get_db] = lambda: db
    api.dependency_overrides[get_current_user] = lambda: data['admin']
    with TestClient(api) as client:
        response = client.get('/api/product-workbench/reverse', params=dict(length=1120, width=440, lot_id=lot.id))
        assert response.status_code == 200, response.text
        row = next(r for r in response.json()['items'] if r['id'] == product.id)
        plan = row['cut_plan']
        assert plan['rows'] * plan['columns'] == 1
        assert plan['yield_factor'] == 4
        assert row['cuts_per_sheet'] == 1
        assert row['match_score'] == plan['utilization']
        assert row['deductible'] is False


def test_actual_base_uses_base_report_dimensions(mobile_erp_app):
    app, ids, factory = _app(mobile_erp_app)
    with factory() as db:
        p = db.get(Product, ids['product'])
        p.report_length_mm, p.report_width_mm = 1500, 300
        p.base_report_length_mm, p.base_report_width_mm = 1000, 200
        p.flute_type, p.layer_count = 'B', 3
        lot = db.scalar(select(InventoryLot).where(InventoryLot.lot_number == 'SF-MOBILE-001'))
        lot.semi_finished_detail.board_length_mm, lot.semi_finished_detail.board_width_mm = 2000, 200
        lot.semi_finished_detail.component_type = 'base'
        db.commit()
    with TestClient(app) as client:
        _login(client, 'mobile-admin')
        response = client.get('/api/product-workbench/reverse', params=dict(length=2000, width=200, lot_id=lot.id))
        assert response.status_code == 200, response.text
        row = next(r for r in response.json()['items'] if r['id'] == p.id)
        assert row['report_component'] == '底料'
        assert (row['report_length_mm'], row['report_width_mm']) == (1000, 200)
        assert row['match_score'] == 100 and row['cuts_per_sheet'] == 2
