from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import event, func, select
import pytest

from test_p1_40a_packaging_masterdata import _login, p1_40a_app as _packaging_app


@pytest.fixture(params=['model', 'migrated'])
def p1_40a_app(request, tmp_path, monkeypatch):
    if request.param == 'migrated':
        from alembic import command
        from alembic.config import Config
        from app.models import Base
        def create_migrated_schema(engine):
            monkeypatch.setenv('ERP_DATABASE_PATH', str(engine.url.database))
            config = Config('alembic.ini')
            config.set_main_option('sqlalchemy.url', str(engine.url))
            command.upgrade(config, 'head')
        monkeypatch.setattr(Base.metadata, 'create_all', create_migrated_schema)
    yield from _packaging_app.__wrapped__(tmp_path)


@pytest.mark.parametrize('scenario', ['ordinary', 'profile_constraint', 'shared', 'shared_private_supplier',
                                     'shared_manufacturing_only', 'shared_audit_failure'])
def test_legacy_corner_can_save_complete_external_contract(p1_40a_app, monkeypatch, scenario):
    from app.models.product import Product
    from app.models.master_data_object_version import MasterDataObjectVersion
    from app.models.shared_finished_stock import SharedFinishedGroup

    profile_constraint_failure = scenario == 'profile_constraint'
    is_shared = scenario.startswith('shared')

    ids = p1_40a_app.state.fixture
    with p1_40a_app.state.factory() as db:
        product = Product(
            customer_id=ids['customer_a'], product_code='3D30444',
            customer_material_code='3D30444', product_name='护角290*50*50*4',
            box_category='normal', box_style='ZHJ 纸护角',
            supply_mode='corrugated_production', length_mm=290, width_mm=50,
            height_mm=4, unit='只', sale_unit_price=Decimal('0.7'),
            print_content=None, splice_mode=None, pieces_per_box=None, flap_mm=None,
            sheet_cutting_settings={'schema_version': 2, 'whole': {
                'length_parts': 1, 'width_parts': 1, 'mold_count': 1, 'is_die_cut': False}},
            version=2,
        )
        db.add(product)
        db.commit()
        product_id = product.id
        engine = db.get_bind()
        if is_shared:
            from app.models.user import User
            from app.services import shared_finished_stock as shared
            from app.services.customer_sharing import activate_master
            peer = Product(
                customer_id=ids['customer_b'], product_code='3D30444',
                customer_material_code='3D30444', product_name='另一客户原护角名称',
                box_category='normal', box_style='ZHJ 纸护角',
                supply_mode='corrugated_production', length_mm=290, width_mm=50,
                height_mm=4, unit='只', sale_unit_price=Decimal('0.9'),
                print_content=None, splice_mode=None, pieces_per_box=None, flap_mm=None,
                sheet_cutting_settings=product.sheet_cutting_settings, version=2,
            )
            db.add(peer)
            db.flush()
            peer_id = peer.id
            actor = db.scalar(select(User).where(User.username == 'p1-40a-admin'))
            args = dict(product_ids=[product.id, peer.id], lot_ids=[], auto_enroll=True)
            preview = shared.preview(db, **args)
            group_id = shared.confirm(db, **args, preview_hash=preview['preview_hash'],
                operation_key='test-shared-corner', evidence='同产品共用生产报料资料', actor=actor)['group_id']
            if scenario == 'shared_manufacturing_only':
                from app.services.shared_product_master import preview_reconcile, reconcile
                plan = preview_reconcile(db, product.id)
                reconcile(db, product_id=product.id, preview_hash=plan['sha256'],
                    operation_key='test-manufacturing-only', evidence='仅共用生产资料', actor=actor)
            else:
                activate_master(db, group_id, actor)
            db.commit()
            initial_group_version = db.get(SharedFinishedGroup, group_id).version

    errors = []
    def capture_error(context):
        errors.append(str(context.original_exception))
    event.listen(engine, 'handle_error', capture_error)
    if profile_constraint_failure:
        from app.api import products
        validate = products._validated_product_versioned_updates
        def invalid_profile(*args, **kwargs):
            updates = validate(*args, **kwargs)
            updates['external_packaging_specification_json'] = None
            return updates
        monkeypatch.setattr(products, '_validated_product_versioned_updates', invalid_profile)
    if scenario == 'shared_audit_failure':
        from app.services import shared_product_master
        finish_sync = shared_product_master.finish_sync
        def fail_after_sync(*args):
            finish_sync(*args)
            raise RuntimeError('injected shared audit failure')
        monkeypatch.setattr(shared_product_master, 'finish_sync', fail_after_sync)
    try:
        with TestClient(p1_40a_app) as client:
            _login(client)
            before = client.get(f'/api/master/products/{product_id}').json()
            assert before['shared_manufacturing_enabled'] is is_shared
            assert before['shared_catalog_policy_enabled'] is (is_shared and scenario != 'shared_manufacturing_only')
            from app.api.products import ProductPayload
            payload = {k: v for k, v in before.items() if k in ProductPayload.model_fields}
            payload.update(
                expected_version=2, product_name='护角290*50*50*5',
                box_style='其他', supply_mode='external_purchase',
                external_packaging_category_code='paper_corner_guard',
                external_packaging_default_order_quantity_basis='1',
                external_packaging_default_purchase_quantity_basis='1',
                external_supply={
                    'customer_specification': {'shape': 'L', 'length_mm': 290,
                        'side_a_mm': 50, 'side_b_mm': 50, 'thickness_mm': 5},
                    'candidates': [{'external_product_id': ids['CG-870-A' if scenario == 'shared_private_supplier' else 'CG-870-G'], 'is_default': True}],
                },
                remark='旧账补录：历史采购数量及采购价待补。',
            )
            response = client.put(f'/api/master/products/{product_id}', json=payload)
            if response.status_code == 409 and isinstance(response.json().get('detail'), dict):
                token = response.json()['detail'].get('confirmation_token')
                assert token, response.text
                payload['confirmation_token'] = token
                if scenario == 'shared_audit_failure':
                    with pytest.raises(RuntimeError, match='injected shared audit failure'):
                        client.put(f'/api/master/products/{product_id}', json=payload)
                else:
                    response = client.put(f'/api/master/products/{product_id}', json=payload)
            if scenario in {'shared_private_supplier', 'shared_manufacturing_only', 'shared_audit_failure'}:
                assert response.status_code == 409, response.text
                if scenario == 'shared_private_supplier':
                    assert '其他客户专用产品' in response.text
                elif scenario == 'shared_manufacturing_only':
                    assert '整套产品共用' in response.text
                with p1_40a_app.state.factory() as db:
                    for pid in [product_id, peer_id]:
                        row = db.get(Product, pid)
                        assert row.version == 2 and row.supply_mode == 'corrugated_production'
                    assert db.get(SharedFinishedGroup, group_id).version == initial_group_version
                    assert db.scalar(select(func.count()).select_from(MasterDataObjectVersion)) == 0
            elif profile_constraint_failure:
                assert response.status_code == 409, response.text
                assert errors and 'ck_products_external_supply_profile' in errors[-1]
                assert '外购供货资料约束冲突' in response.json()['detail'], response.text
                assert '组合不能重复' not in response.text
                with p1_40a_app.state.factory() as db:
                    unchanged = db.get(Product, product_id)
                    assert unchanged.version == 2
                    assert unchanged.product_name == '护角290*50*50*4'
                    assert unchanged.supply_mode == 'corrugated_production'
                    assert unchanged.external_packaging_specification_json is None
                    assert db.scalar(select(func.count()).select_from(MasterDataObjectVersion)) == 0
            else:
                assert response.status_code == 200, (response.text, errors)
                assert response.json()['version'] == 3
                assert response.json()['external_supply']['specification']['length_mm'] == 290
                if is_shared:
                    with p1_40a_app.state.factory() as db:
                        source, peer = db.get(Product, product_id), db.get(Product, peer_id)
                        assert peer.version == 3
                        assert peer.supply_mode == 'external_purchase'
                        for column in ['external_packaging_category_code', 'external_packaging_specification_json',
                                       'external_packaging_specification_summary', 'external_packaging_purchase_unit',
                                       'external_packaging_candidate_snapshot_json',
                                       'external_packaging_default_order_quantity_basis',
                                       'external_packaging_default_purchase_quantity_basis']:
                            assert getattr(peer, column) == getattr(source, column)
                        assert peer.product_name == '另一客户原护角名称'
                        assert peer.sale_unit_price == Decimal('0.9')
                        assert source.sale_unit_price == Decimal('0.7')
                        assert source.unit == peer.unit == '根'
                        assert db.get(SharedFinishedGroup, group_id).version == initial_group_version + 1
                    # A later profile-only edit must keep the shared contract complete.
                    payload['expected_version'] = 3
                    payload.pop('confirmation_token', None)
                    payload['external_supply']['customer_specification']['length_mm'] = 291
                    payload['external_packaging_default_purchase_quantity_basis'] = '2'
                    updated = client.put(f'/api/master/products/{product_id}', json=payload)
                    if updated.status_code == 409 and isinstance(updated.json().get('detail'), dict):
                        payload['confirmation_token'] = updated.json()['detail']['confirmation_token']
                        updated = client.put(f'/api/master/products/{product_id}', json=payload)
                    assert updated.status_code == 200, updated.text
                    with p1_40a_app.state.factory() as db:
                        source, peer = db.get(Product, product_id), db.get(Product, peer_id)
                        assert source.version == peer.version == 4
                        assert source.external_packaging_specification_json == peer.external_packaging_specification_json
                        assert source.external_packaging_default_purchase_quantity_basis == peer.external_packaging_default_purchase_quantity_basis == 2
                        assert peer.product_name == '另一客户原护角名称'
                        assert peer.sale_unit_price == Decimal('0.9')
                    stale = client.put(f'/api/master/products/{product_id}', json=payload)
                    assert stale.status_code == 409
    finally:
        event.remove(engine, 'handle_error', capture_error)


@pytest.mark.parametrize('identity_field', ['product_code', 'customer_material_code'])
def test_real_product_identity_duplicate_still_rejected(p1_40a_app, identity_field):
    from app.models.product import Product
    from test_p1_40a_packaging_masterdata import _external_payload

    ids = p1_40a_app.state.fixture
    with TestClient(p1_40a_app) as client:
        _login(client)
        payload = _external_payload(ids)
        original = client.post('/api/master/products', json=payload)
        assert original.status_code == 201, original.text
        other_identity = 'customer_material_code' if identity_field == 'product_code' else 'product_code'
        payload[other_identity] = 'DIFFERENT-CODE'
        duplicate = client.post('/api/master/products', json=payload)
        assert duplicate.status_code == 409
        assert duplicate.json()['detail'] == '同一客户下，存货编码与产品名称的组合不能重复'
        with p1_40a_app.state.factory() as db:
            assert db.scalar(select(func.count()).select_from(Product)) == 1


@pytest.mark.parametrize(('message', 'expected'), [
    ('NOT NULL constraint failed: products.unit', '数据约束冲突'),
    ('FOREIGN KEY constraint failed', '数据约束冲突'),
    ('UNIQUE constraint failed: master_data_object_versions.object_type, master_data_object_versions.object_id, master_data_object_versions.version', '版本记录冲突'),
])
def test_unrelated_integrity_errors_are_not_product_identity_duplicates(message, expected):
    import sqlite3
    from sqlalchemy.exc import IntegrityError
    from app.api.products import _product_integrity_detail

    detail = _product_integrity_detail(IntegrityError('private SQL', {'secret': 'not for UI'}, sqlite3.IntegrityError(message)))
    assert expected in detail
    assert '组合不能重复' not in detail
    assert 'private' not in detail and 'secret' not in detail
