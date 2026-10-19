from datetime import datetime
from decimal import Decimal
import pytest

from fastapi.testclient import TestClient
from sqlalchemy import select, text

from app.models.access_control import UserCustomerScope, UserPermissionOverride
from app.models.product import Product
from app.models.user import User
from app.models.warehouse_goods import WarehouseGoodsProfile
from app.models.warehouse_inventory import InventoryLot, SemiFinishedInventoryDetail, SemiFinishedLotAllowedProduct
from test_p1_21b_mobile_admin_product_search import _login, mobile_erp_app
from test_product_workbench import _app


@pytest.mark.parametrize('stock_length,stock_width,expected_cuts,expected_score', [
    (1000, 200, 1, 100), (991, 200, 1, 99.1), (990, 200, 1, 99),
    (989, 200, None, None), (1000, 190, 1, 95), (1000, 189, None, None),
    (2000, 200, 2, 100), (1000, 400, 2, 100), (2000, 400, 4, 100),
    (1100, 200, 1, 90.91), (1991, 200, 2, 99.55),
    (200, 1000, None, None), (3000, 1000, 15, 100),
])
def test_sheet_size_fit_examples(mobile_erp_app, stock_length, stock_width, expected_cuts, expected_score):
    app, ids, factory = _app(mobile_erp_app)
    with factory() as db:
        lot = db.scalar(select(InventoryLot).where(InventoryLot.lot_number == 'SF-MOBILE-001'))
        lot.semi_finished_detail.board_length_mm = stock_length
        lot.semi_finished_detail.board_width_mm = stock_width
        db.commit()
        before = db.execute(text('select * from inventory_lots order by id')).all()
    with TestClient(app) as client:
        _login(client, 'mobile-admin')
        response = client.get('/api/product-workbench/sheets?length=1000&width=200&flute_type=B&expand_scope=true')
        assert response.status_code == 200, response.text
        rows = response.json()['items']
        default_rows = client.get('/api/product-workbench/sheets?length=1000&width=200&flute_type=B').json()['items']
        if stock_length > 1100 or stock_width > 220:
            assert default_rows == []  # An empty near-size result must never auto-expand.
        else:
            assert default_rows == rows
        if expected_cuts is None:
            assert rows == []
        else:
            assert len(rows) == 1
            assert rows[0]['cuts_per_sheet'] == expected_cuts
            assert rows[0]['match_score'] == expected_score
            assert rows[0]['reverse_source']['length'] == stock_length
            assert rows[0]['reverse_source']['width'] == stock_width
    with factory() as db:
        assert db.execute(text('select * from inventory_lots order by id')).all() == before


def test_sheet_fit_ranks_and_filters_all_batches_before_paging(mobile_erp_app):
    app, ids, factory = _app(mobile_erp_app)
    with factory() as db:
        original = db.scalar(select(InventoryLot).where(InventoryLot.lot_number == 'SF-MOBILE-001'))
        dimensions = [(1100, 200), (2000, 400), (1000, 400), (991, 200), (1000, 200),
                      (2000, 200), (1000, 200), (100000, 200)]
        lot_ids = []
        for index, (length, width) in enumerate(dimensions):
            lot = InventoryLot(lot_number=f'FIT-{index}', inventory_type='semi_finished',
                               warehouse_location_id=original.warehouse_location_id,
                               quantity_available=10, quantity_reserved=0, unit='sheets',
                               status='active', source_type='manual', stock_date=original.stock_date,
                               last_movement_at=original.last_movement_at)
            db.add(lot)
            db.flush()
            db.add(SemiFinishedInventoryDetail(inventory_lot_id=lot.id,
                   owner_customer_id=original.semi_finished_detail.owner_customer_id,
                   material_code_snapshot='K=A', normalized_material_code='K=A', sheet_type='raw_board',
                   board_length_mm=length, board_width_mm=width, flute_type='B', layer_count=3,
                   component_type='whole', pieces_per_box=1, stock_yield_per_sheet=1))
            lot_ids.append(lot.id)
        db.commit()
    with TestClient(app) as client:
        _login(client, 'mobile-admin')
        params = dict(length=1000, width=200, flute_type='B', page_size=2, expand_scope=True)
        near = client.get('/api/product-workbench/sheets', params={**params, 'expand_scope': False, 'page_size': 50}).json()
        assert [r['lot_id'] for r in near['items']] == [lot_ids[i] for i in (4, 6, 3, 0)]
        rows = []
        for page in range(1, 5):
            response = client.get('/api/product-workbench/sheets', params={**params, 'page': page})
            assert response.status_code == 200, response.text
            body = response.json()
            assert body['total'] == 8
            assert body['has_more'] == (page < 4)
            rows.extend(body['items'])
        assert [r['lot_id'] for r in rows] == [lot_ids[i] for i in (4, 6, 3, 0, 2, 5, 1, 7)]
        filtered = client.get('/api/product-workbench/sheets', params={**params, 'min_score': 100}).json()
        assert filtered['total'] == 6 and filtered['has_more']
        assert all(r['match_score'] == 100 for r in filtered['items'])
        assert client.get('/api/product-workbench/sheets', params={**params, 'page': 5}).json()['items'] == []
        for score in (-1, 101):
            assert client.get('/api/product-workbench/sheets', params={**params, 'min_score': score}).status_code == 422


def test_sheet_fit_decimal_boundary_and_tolerance_is_not_multiplied():
    from app.services.workbench_sheet_search import sheet_fit
    assert sheet_fit('110.10', '80.20', '120.10', '80.20') is not None
    assert sheet_fit('110.09', '80.20', '120.10', '80.20') is None
    assert sheet_fit(1980, 200, 1000, 200)['cuts_per_sheet'] == 1
    assert sheet_fit(1990, 200, 1000, 200)['cuts_per_sheet'] == 2
    assert sheet_fit(1000, 400, 1000, 200)['length_cuts'] == 1
    assert sheet_fit(1000, 400, 1000, 200)['width_cuts'] == 2
    assert sheet_fit(0, 200, 1000, 200) is None


def test_sheet_search_original_axes_all_batches_paging_and_no_writes(mobile_erp_app):
    app, ids, factory = _app(mobile_erp_app)
    with factory() as db:
        before = db.execute(text('select * from inventory_lots order by id')).all()
    with TestClient(app) as client:
        assert client.get('/api/product-workbench/sheets?length=800&width=600').status_code == 401
        _login(client, 'mobile-admin')
        params = dict(length=800, width=600, page_size=1)
        first = client.get('/api/product-workbench/sheets', params=params)
        assert first.status_code == 200, first.text
        body = first.json()
        assert body['total'] == 2 and body['has_more']
        assert body['items'][0]['actual'] == 10
        assert body['items'][0]['available'] == 8 and body['items'][0]['reserved'] == 2
        assert body['items'][0]['location_label']
        second = client.get('/api/product-workbench/sheets', params={**params, 'page': 2}).json()
        assert not second['has_more']
        assert second['items'][0]['lot_id'] != body['items'][0]['lot_id']
        assert second['items'][0]['actual'] == 88  # unbound stock is discoverable too
        assert client.get('/api/product-workbench/sheets?length=600&width=800').json()['total'] == 0
        assert client.get('/api/product-workbench/sheets?length=801&width=600').json()['total'] == 2
        assert client.get('/api/product-workbench/sheets?length=811&width=600').json()['total'] == 0
        assert client.get('/api/product-workbench/sheets?length=800&width=600&flute_type=A').json()['total'] == 0
        for suffix in ('&page=9223372036854775808', '&flute_type=BAD'):
            assert client.get('/api/product-workbench/sheets?length=800&width=600'+suffix).status_code == 422
    with factory() as db:
        assert db.execute(text('select * from inventory_lots order by id')).all() == before
        lot = db.scalar(select(InventoryLot).where(InventoryLot.lot_number == 'SF-MOBILE-UNBOUND'))
        lot.quantity_available = 0
        db.commit()
    with TestClient(app) as client:
        _login(client, 'mobile-admin')
        assert client.get('/api/product-workbench/sheets?length=800&width=600').json()['total'] == 1


def test_sheet_search_permission_and_cross_customer_redaction(mobile_erp_app):
    app, ids, factory = _app(mobile_erp_app)
    with factory() as db:
        user = db.scalar(select(User).where(User.username == 'mobile-scoped'))
        override = UserPermissionOverride(user_id=user.id, permission_code='warehouse.view', is_allowed=False)
        db.add(override)
        db.commit()
    with TestClient(app) as client:
        _login(client, 'mobile-scoped')
        assert client.get('/api/product-workbench/sheets?length=800&width=600').status_code == 403
    with factory() as db:
        db.get(UserPermissionOverride, override.id).is_allowed = True
        scope = db.scalar(select(UserCustomerScope).where(UserCustomerScope.user_id == user.id))
        scope.customer_id = db.get(Product, ids['other_product']).customer_id
        db.commit()
    with TestClient(app) as client:
        _login(client, 'mobile-scoped')
        assert client.get('/api/product-workbench/sheets?length=800&width=600').json()['total'] == 0
    with factory() as db:
        lot = db.scalar(select(InventoryLot).where(InventoryLot.lot_number == 'SF-MOBILE-001'))
        lot.semi_finished_detail.internal_name = 'PRIVATE OWNER NAME'
        db.add(SemiFinishedLotAllowedProduct(inventory_lot_id=lot.id, product_id=ids['other_product'], confirmed_at=datetime.now()))
        db.commit()
    with TestClient(app) as client:
        _login(client, 'mobile-scoped')
        response = client.get('/api/product-workbench/sheets?length=800&width=600')
        assert response.json()['total'] == 1
        assert response.json()['items'][0]['name'] == '已授权共用片料'
        assert 'PRIVATE OWNER NAME' not in response.text and 'SF-MOBILE' not in response.text


def test_reverse_infers_layers_filters_before_paging_and_shows_report_fields(mobile_erp_app):
    app, ids, factory = _app(mobile_erp_app)
    with factory() as db:
        for key, length in [('product', 800), ('product_two', 600), ('other_product', 700)]:
            p = db.get(Product, ids[key])
            p.report_length_mm, p.report_width_mm = length, 600
            p.flute_type, p.layer_count = ('AB', 5) if key == 'other_product' else ('B', 3)
            p.default_material_code = 'REPORT-MATERIAL'
        db.commit()
    with TestClient(app) as client:
        _login(client, 'mobile-admin')
        params = dict(length=800, width=600, flute_type='b', page_size=1)
        result = client.get('/api/product-workbench/reverse', params=params)
        assert result.status_code == 200, result.text
        body = result.json()
        assert body['total'] == 2 and body['has_more']
        row = body['items'][0]
        assert row['match_score'] == 100 and row['layer_count'] == 3
        assert row['report_material'] == 'REPORT-MATERIAL'
        assert row['report_length_mm'] == 800 and row['report_width_mm'] == 600
        assert row['specification'] and 'drawings' in row
        second = client.get('/api/product-workbench/reverse', params={**params, 'page': 2}).json()
        assert second['items'][0]['match_score'] == 75
        filtered = client.get('/api/product-workbench/reverse', params={**params, 'min_score': 80}).json()
        assert filtered['total'] == 1 and not filtered['has_more']
        assert client.get('/api/product-workbench/reverse', params={**params, 'min_score': 75}).json()['total'] == 2
        assert client.get('/api/product-workbench/reverse', params={**params, 'layer_count': 5}).status_code == 422
        assert client.get('/api/product-workbench/reverse', params={**params, 'min_score': 101}).status_code == 422


def test_selected_processed_lot_reads_actual_state_without_user_input(mobile_erp_app):
    app, ids, factory = _app(mobile_erp_app)
    with factory() as db:
        p = db.get(Product, ids['product'])
        p.report_length_mm, p.report_width_mm = 800, 600
        p.flute_type, p.layer_count, p.default_material_code = 'B', 3, 'K=A'
        lot = db.scalar(select(InventoryLot).where(InventoryLot.lot_number == 'SF-MOBILE-001'))
        db.add(WarehouseGoodsProfile(lot_id=lot.id, data_json='{"output_piece":true,"processing":"cut"}'))
        before = lot.quantity_available, lot.quantity_reserved, lot.version
        db.commit()
    with TestClient(app) as client:
        _login(client, 'mobile-admin')
        found = client.get('/api/product-workbench/sheets?length=800&width=600').json()
        source = next(r for r in found['items'] if r['lot_id'] == lot.id)
        assert source['unit'] == '片'
        result = client.get('/api/product-workbench/reverse', params=source['reverse_source'])
        assert result.status_code == 200, result.text
        assert result.json()['items']
        assert all(r['source_processed_state'] == 'output_piece' and r['lot_unit'] == '片' and not r['deductible'] for r in result.json()['items'])
        filtered = client.get('/api/product-workbench/reverse', params={**source['reverse_source'], 'min_score': 100})
        assert all(r['match_score'] >= 100 for r in filtered.json()['items'])
    with factory() as db:
        actual = db.get(InventoryLot, lot.id)
        assert (actual.quantity_available, actual.quantity_reserved, actual.version) == before


def test_cardboard_decimal_dimensions_roundtrip_into_selected_reverse(mobile_erp_app):
    app, ids, factory = _app(mobile_erp_app)
    with factory() as db:
        lot = db.scalar(select(InventoryLot).where(InventoryLot.lot_number == 'SF-MOBILE-001'))
        lot.semi_finished_detail.board_length_mm = Decimal('120.10')
        lot.semi_finished_detail.board_width_mm = Decimal('80.20')
        lot.semi_finished_detail.flute_type, lot.semi_finished_detail.layer_count = 'NONE', 1
        p = db.get(Product, ids['product'])
        p.report_length_mm, p.report_width_mm = Decimal('120.10'), Decimal('80.20')
        p.flute_type, p.layer_count = 'NONE', 1
        db.commit()
    with TestClient(app) as client:
        _login(client, 'mobile-admin')
        found = client.get('/api/product-workbench/sheets?length=120.10&width=80.20&flute_type=NONE').json()
        assert found['total'] == 1
        response = client.get('/api/product-workbench/reverse', params=found['items'][0]['reverse_source'])
        assert response.status_code == 200, response.text
        free = client.get('/api/product-workbench/reverse?length=120.10&width=80.20&flute_type=NONE&min_score=100')
        assert free.status_code == 200, free.text
        assert free.json()['total'] == 1
        assert free.json()['items'][0]['match_score'] == 100
        assert free.json()['items'][0]['match_reasons'] == ['报料尺寸相同，仅供查找用途']
