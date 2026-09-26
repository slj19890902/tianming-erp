from datetime import datetime
from pathlib import Path
import shutil
import subprocess

from tests.test_p1_09c_product_query_scaling import _fixture


def test_newest_product_discovery_preserves_scope_filters_and_pagination(tmp_path):
    from app.api.products import list_products
    from app.models.product import Product
    from app.models.user import User
    from sqlalchemy import select
    engine, factory, user_id = _fixture(tmp_path, visible_count=55)
    try:
        with factory() as db:
            user = db.get(User, user_id)
            products = db.scalars(select(Product).where(Product.product_code != 'P1-09C-P-HIDDEN').order_by(Product.id)).all()
            customer_id = products[0].customer_id
            for product in products:
                product.created_at = datetime(2020, 1, 1)
            new = Product(customer_id=customer_id, product_code='ZZ-NEW', customer_material_code='ZZ-NEW', product_name='New box',
                          is_active=True, created_at=datetime(2026, 9, 24), box_category='normal')
            inactive = Product(customer_id=customer_id, product_code='ZZ-INACTIVE', customer_material_code='ZZ-INACTIVE', product_name='Inactive',
                               is_active=False, created_at=datetime(2026, 9, 25), box_category='normal')
            db.add_all([new, inactive]); db.commit()
            def read(**kwargs):
                return list_products(db=db, user=user, customer_id=customer_id, selection_context='order',
                                     response_mode='summary', page_size=50, **kwargs)
            first = read(page=1, sort_by='newest')
            assert first['items'][0]['id'] == new.id
            assert first['total'] == 56
            second = read(page=2, sort_by='newest')
            ids = [r['id'] for r in first['items']+second['items']]
            assert len(ids) == len(set(ids)) == 56
            assert inactive.id not in ids
            assert all(r['customer_id'] == customer_id for r in first['items']+second['items'])
            assert all('cost_unit_price' not in r or r['cost_unit_price'] is None for r in first['items'])
            code = read(page=1, sort_by='newest', product_code='ZZ-NEW')
            assert [r['id'] for r in code['items']] == [new.id]
            default = read(page=1)
            assert default['items'][0]['product_code'] == 'P1-09C-P-000'
            assert new.id not in [r['id'] for r in default['items']]
    finally:
        engine.dispose()


def test_product_picker_methods_and_template():
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([shutil.which('node'), str(root/'tests/order_product_discovery.cjs'), str(root)],
                            capture_output=True, text=True, encoding='utf-8', errors='replace')
    assert result.returncode == 0, result.stdout+result.stderr


def test_batched_readiness_matches_single_product_for_present_and_absent_bom(tmp_path):
    from app.api.products import list_products
    from app.models.product import Product
    from app.models.multilevel_bom import ProductBomProfile
    from app.models.user import User
    from app.services.product_readiness import product_readiness
    from sqlalchemy import select
    engine, factory, user_id = _fixture(tmp_path, visible_count=3)
    try:
        with factory() as db:
            user=db.get(User,user_id)
            products=db.scalars(select(Product).where(Product.product_code!='P1-09C-P-HIDDEN').order_by(Product.id)).all()
            products[0].box_style='BOM组合';products[0].is_composite=True
            products[1].box_style='BOM组合';products[1].is_composite=True
            db.add(ProductBomProfile(product_id=products[0].id,source='assembled',material_mode='expand_children',delivery_mode='parent'))
            db.commit()
            expected={p.id:product_readiness(p) for p in products}
            assert expected[products[0].id]['ready']
            assert not expected[products[1].id]['ready']
            for mode in ('full','summary'):
                response=list_products(db=db,user=user,page=1,page_size=50,response_mode=mode)
                assert {p['id']:p['readiness'] for p in response['items']}==expected
    finally:engine.dispose()
