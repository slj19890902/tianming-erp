from types import SimpleNamespace

import pytest

from app.services.box_type_rules import box_type_code, recommend_box_type
from app.services.legacy_product_classification import classification, new_order_issue, box_style_search_values, external_category_search_values
from app.services.product_readiness import product_readiness
from tests.test_phase5_orders import order_api_app, _login, _payload


@pytest.mark.parametrize(('name', 'target'), [
    ('NH 天华内盒1', 'irregular'), ('THNH1 腾华内盒1', 'irregular'),
    ('CTSNH 抽屉式内盒', 'irregular'), ('JB 简包', 'die_cut_partition'),
    ('SC AB白卡', 'die_cut_inner_box'), ('007 华元内盒', 'a1_0201'),
    ('009 内盒无钉', 'a1_0201'), ('HP01 恒鹏模切1', 'die_cut_partition'),
    ('HP02 恒鹏模切2', 'die_cut_partition'), ('GD2 格挡2', 'divider'),
    ('016 井字架', 'divider'), ('WGX 无盖箱', 'half_slotted_carton'),
    ('WDX 无底箱', 'half_slotted_carton'), ('013 半截天地盖', 'a3_set'),
    ('模切盖 模切盖', 'top_cover'), ('XH 鞋盒', 'die_cut_inner_box'),
    ('015 白卡内盒', 'die_cut_inner_box'), ('SAT 驶安特模', 'die_cut_inner_box'),
    ('TBM 天宝模', 'die_cut_inner_box'), ('003 思展模切', 'die_cut_inner_box'),
    ('011 佩特罗模', 'die_cut_inner_box'), ('014 恒鹏模切3', 'die_cut_inner_box'),
    ('LXBG 模切日本黄', 'die_cut_inner_box'), ('THYW 天华压外', 'a1_0201'),
    ('004 豪迪纸箱', 'a1_0201'), ('005 粘合成型', 'a1_0201'),
])
def test_confirmed_targets_and_existing_formula_contract(name, target):
    product = SimpleNamespace(box_style=name, supply_mode='corrugated_production',
                              report_length_mm=999, pieces_per_box=2, production_process='打钉')
    before = vars(product).copy()
    mapped = classification(product)
    assert box_type_code(name) == target
    values = dict(length_mm=400, width_mm=300, height_mm=200, splice_mode='double', flap_mm=30)
    assert recommend_box_type(box_style=name, **values) == recommend_box_type(box_style=mapped['box_style'], **values)
    assert vars(product) == before
    assert new_order_issue(product) is None
    assert name in box_style_search_values(mapped['box_style'])
    assert mapped['box_style'] in box_style_search_values(name)


@pytest.mark.parametrize(('name', 'category'), [
    ('ZHJ 纸护角', 'paper_corner_guard'), ('HRHJ 华融护角', 'paper_corner_guard'),
    ('HP03 恒鹏护角', 'paper_corner_guard'), ('EPE epe', 'epe_cushion'),
    ('FWB 蜂窝板', 'honeycomb_board'),
])
def test_external_classification_does_not_fabricate_supply_contract(name, category):
    product = SimpleNamespace(box_style=name, supply_mode='corrugated_production')
    before = vars(product).copy()
    result = classification(product)
    assert result['box_style'] == '其他'
    assert result['category_code'] == category
    assert result['needs_supply_completion']
    assert new_order_issue(product)
    assert product_readiness(product)['order_save_missing_labels']
    assert category in external_category_search_values(name)
    assert vars(product) == before
    assert box_type_code(name) is None


def test_saved_external_contract_and_expense_boundary():
    product = SimpleNamespace(box_style='其他', supply_mode='external_purchase',
                              external_packaging_category_code='epe_cushion')
    assert not classification(product)['needs_supply_completion']
    assert new_order_issue(product) is None
    expense = SimpleNamespace(box_style='00001 模具费')
    assert classification(expense)['kind'] == 'expense'
    assert box_type_code(expense.box_style) is None
    assert product_readiness(expense)['missing_fields'] == ['expense_item']


def test_expense_filtered_without_hiding_null_box_style(tmp_path):
    from sqlalchemy import create_engine, select
    from app.models import Base
    from app.models.product import Product
    from app.models.customer import Customer
    from app.services.composite_bom import order_selectable_product_condition
    from sqlalchemy.orm import Session
    engine = create_engine('sqlite:///' + str(tmp_path / 'isolated.db'))
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        customer = Customer(customer_code='T', name='Test')
        db.add(customer); db.flush()
        db.add_all([Product(customer_id=customer.id, product_code=str(i), customer_material_code=str(i),
                            product_name=str(i), box_style=style) for i, style in enumerate([None, '00001 模具费', 'NH 天华内盒1'])])
        db.flush()
        assert set(db.scalars(select(Product.product_code).where(order_selectable_product_condition()))) == {'0', '2'}


def test_http_projection_search_and_order_write_rejection_are_atomic(order_api_app):
    from fastapi.testclient import TestClient
    from sqlalchemy import select, func
    from app.models.product import Product
    from app.models.order import Order
    app, factory = order_api_app
    with factory() as db:
        db.get(Product, 1).box_style = 'NH 天华内盒1'
        db.get(Product, 2).box_style = 'ZHJ 纸护角'
        db.commit()
    with TestClient(app) as client:
        assert client.get('/api/master/products/1').status_code == 401
        _login(client)
        result = client.get('/api/master/products', params={'keyword': '异形箱', 'response_mode': 'summary'})
        assert result.status_code == 200, result.text
        assert [x['id'] for x in result.json()['items']] == [1]
        detail = client.get('/api/master/products/2').json()
        assert detail['classification']['category_code'] == 'paper_corner_guard'
        assert detail['supply_mode'] == 'corrugated_production'
        assert detail['external_supply']['candidates'] == []
        assert detail['readiness']['order_save_missing_labels']
        response = client.post('/api/orders', json=_payload())
        assert response.status_code == 422, response.text
        assert '供应商' in response.text
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Order)) == 0
        assert db.get(Product, 2).box_style == 'ZHJ 纸护角'
        assert db.get(Product, 2).version == 1


def test_processing_preview_missing_contract_and_explicit_snapshot_is_preserved(order_api_app):
    from app.models.product import Product
    from app.services.processing_cost import estimate_standard_processing_cost
    _app, factory = order_api_app
    with factory() as db:
        product = db.get(Product, 1)
        product.box_style = 'EPE epe'
        result = estimate_standard_processing_cost(db, product=product, quantity=100)
        assert result['calculation_status'] == 'incomplete'
        assert result['estimated_processing_cost'] is None
        assert any('供应商' in item for item in result['missing_items'])
        frozen = estimate_standard_processing_cost(db, product=product, quantity=100,
                                                   supply_mode='external_purchase')
        assert frozen['calculation_status'] == 'calculated'
        assert frozen['estimated_processing_cost'] == '0.00'
