"""Customer prices may differ while one physical inventory ledger is shared."""
from decimal import Decimal
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.models.master_data_object_version import MasterDataObjectVersion
from app.models.product import Product
from app.models.shared_finished_stock import SharedFinishedGroup, SharedFinishedMutation
from app.services import customer_sharing as sharing, shared_customer_access as access, shared_product_master as master
from tests.test_customer_sharing import args, save, composite_requisition_app, _p181_published_map_identity
from tests.test_shared_customer_access import prepare as reference_args
from tests.test_shared_finished_stock import reservation_db, setup_pair
from tests.test_yg_gy_shared_master import edit


def priced_pair(db, data):
    target, item, lot = setup_pair(db, data)
    source = data['product']
    source.sale_unit_price = Decimal('5')
    source.sale_unit_price_no_tax = Decimal('4.424779')
    target.sale_unit_price = Decimal('8')
    target.sale_unit_price_no_tax = Decimal('7.079646')
    db.commit()
    return source, target, item, lot


def test_existing_customer_prices_survive_share_price_edit_and_reenable(reservation_db):
    db, data = reservation_db
    source, target, item, lot = priced_pair(db, data)
    frozen = (item.unit_price, item.subtotal, lot.quantity_available, lot.estimated_unit_cost_snapshot,
              lot.finished_detail.physical_basis_json)
    result = save(db, data, args(data))
    assert (source.sale_unit_price, target.sale_unit_price) == (Decimal('5'), Decimal('8'))
    before_source_version = source.version
    edit(db, data, target, {'sale_unit_price': Decimal('9.123456'), 'sale_unit_price_no_tax': Decimal('8.073855')})
    db.commit()
    assert source.sale_unit_price == Decimal('5') and source.version == before_source_version
    assert target.sale_unit_price == Decimal('9.123456')
    save(db, data, args(data))
    assert (source.sale_unit_price, target.sale_unit_price) == (Decimal('5'), Decimal('9.123456'))
    assert frozen == (item.unit_price, item.subtotal, lot.quantity_available, lot.estimated_unit_cost_snapshot,
                      lot.finished_detail.physical_basis_json)
    policy = master.receipt(db, result['linked'][0]['group_id'])
    assert policy['customer_sale_price_initializations'][str(target.id)]['status'] == 'preserved'


def test_blank_price_initializes_once_and_explicit_clear_is_not_refilled(reservation_db):
    db, data = reservation_db
    source, target, _, _ = priced_pair(db, data)
    target.sale_unit_price = target.sale_unit_price_no_tax = None
    db.commit()
    result = save(db, data, args(data))
    assert target.sale_unit_price == Decimal('5') and target.sale_unit_price_no_tax == Decimal('4.424779')
    policy = master.receipt(db, result['linked'][0]['group_id'])
    evidence = policy['customer_sale_price_initializations'][str(target.id)]
    assert evidence['status'] == 'initialized' and evidence['source_product_id'] == source.id
    edit(db, data, target, {'sale_unit_price': None, 'sale_unit_price_no_tax': None})
    db.commit()
    check = master.preview_reconcile(db, source.id)
    master.reconcile(db, product_id=source.id, preview_hash=check['sha256'], operation_key='preserve-price-reconcile',
        evidence='仅核对共享实物，不恢复客户价格', actor=data['admin'])
    db.commit()
    save(db, data, args(data))
    assert target.sale_unit_price is target.sale_unit_price_no_tax is None


def test_partial_customer_price_is_preserved_as_a_pair(reservation_db):
    db, data = reservation_db
    source, target, _, _ = priced_pair(db, data)
    target.sale_unit_price_no_tax = None
    db.commit()
    save(db, data, args(data))
    assert target.sale_unit_price == Decimal('8') and target.sale_unit_price_no_tax is None


def test_price_edit_does_not_check_peer_scope_or_physical_role(reservation_db):
    db, data = reservation_db
    source, target, _, _ = priced_pair(db, data)
    save(db, data, args(data))
    target.is_composite = True  # Unrelated legacy physical drift must still allow a local selling-price correction.
    db.commit()
    with patch('app.api.deps.require_customer_access', side_effect=AssertionError('peer access')):
        edit(db, data, source, {'sale_unit_price': Decimal('6.123456')})
        db.commit()
    assert source.sale_unit_price == Decimal('6.123456') and target.sale_unit_price == Decimal('8')
    with pytest.raises(HTTPException, match='BOM'):
        edit(db, data, source, {'report_length_mm': 802})
    db.rollback()


def test_cost_conflict_is_still_blocked_and_cost_sync_remains(reservation_db):
    db, data = reservation_db
    source, target, _, _ = priced_pair(db, data)
    source.cost_unit_price, target.cost_unit_price = Decimal('1'), Decimal('2')
    db.commit()
    plan = sharing.preview(db, actor=data['admin'], **args(data))
    assert plan['blocked'] and 'cost_unit_price' in plan['blocked'][0]['reason']
    target.cost_unit_price = None
    db.commit()
    save(db, data, args(data))
    assert target.cost_unit_price == Decimal('1')
    edit(db, data, source, {'cost_unit_price': Decimal('1.75'), 'sale_unit_price': Decimal('6')})
    db.commit()
    assert target.cost_unit_price == Decimal('1.75') and target.sale_unit_price == Decimal('8')


def test_reference_creation_freezes_initial_price_in_preview_and_is_idempotent(reservation_db):
    db, data = reservation_db
    values, lot = reference_args(db, data)
    source = data['product']
    source.sale_unit_price, source.sale_unit_price_no_tax = Decimal('5.123456'), Decimal('4.533147')
    db.commit()
    plan = access.preview(db, **values)
    assert Decimal(plan['new_sales_references'][0]['sale_unit_price']) == Decimal('5.123456')
    call = dict(**values, actor=data['admin'], preview_hash=plan['preview_hash'],
                operation_key='priced-reference', evidence='确认初始客户售价')
    result = access.confirm(db, **call)
    db.commit()
    target = db.get(Product, result['created_sales_references'][0]['product_id'])
    assert target.sale_unit_price == Decimal('5.123456')
    assert target.sale_unit_price_no_tax == Decimal('4.533147')
    assert result['created_sales_references'][0]['price_initialization']['source_version'] == source.version
    edit(db, data, target, {'sale_unit_price': Decimal('9')})
    db.commit()
    assert access.confirm(db, **call)['replayed']
    assert target.sale_unit_price == Decimal('9') and source.sale_unit_price == Decimal('5.123456')


def test_price_initialization_and_audit_failure_roll_back_together(reservation_db):
    db, data = reservation_db
    source, target, _, _ = priced_pair(db, data)
    target.sale_unit_price = target.sale_unit_price_no_tax = None
    db.commit()
    before = target.version
    plan = sharing.preview(db, actor=data['admin'], **args(data))
    with patch.object(sharing, 'append_audit_event', side_effect=RuntimeError('audit failed')):
        with pytest.raises(RuntimeError):
            sharing.confirm(db, actor=data['admin'], preview_hash=plan['preview_hash'], **args(data))
    db.rollback()
    assert target.sale_unit_price is None and target.version == before
    assert db.query(SharedFinishedGroup).count() == db.query(SharedFinishedMutation).count() == 0


def test_local_price_edit_keeps_cas_and_version_audit(reservation_db):
    from app.services.master_data_versioning import apply_versioned_update, preview_versioned_update
    db, data = reservation_db
    source, target, _, _ = priced_pair(db, data)
    save(db, data, args(data))
    call = dict(object_type='product', entity=target, updates={'sale_unit_price': Decimal('9')},
                expected_version=target.version, user=data['admin'])
    token = preview_versioned_update(db, **call)['confirmation_token']
    edit(db, data, target, {'sale_unit_price': Decimal('8.5')})
    db.commit()
    with pytest.raises(HTTPException):
        apply_versioned_update(db, **call, reason='stale price', source='test', confirmation_token=token)
    db.rollback()
    assert target.sale_unit_price == Decimal('8.5') and source.sale_unit_price == Decimal('5')
    revision = db.scalar(select(MasterDataObjectVersion).where(MasterDataObjectVersion.object_type == 'product',
        MasterDataObjectVersion.object_id == target.id, MasterDataObjectVersion.version == target.version))
    assert revision and 'sale_unit_price' in revision.changed_fields_json


@pytest.mark.parametrize('basis', ['tax_mode', 'tax_rate', 'unit'])
def test_unmatched_price_basis_is_not_guessed(reservation_db, basis):
    from app.models.invoice_task import CustomerInvoiceProfile
    db, data = reservation_db
    source = data['product']
    source.sale_unit_price = Decimal('5')
    if basis == 'tax_mode':
        db.add(CustomerInvoiceProfile(customer_id=data['other_customer'].id,
            price_tax_mode='tax_exclusive', is_enabled=True))
    elif basis == 'tax_rate':
        data['other_customer'].default_tax_rate = Decimal('0.09')
    db.commit()
    initial = access.sale_price_initialization(db, source, target_customer_id=data['other_customer'].id,
        target_unit='套' if basis == 'unit' else source.unit)
    assert initial['status'] == 'incompatible_basis'
    assert all(value is None for value in initial['values'].values())


def test_tax_profile_change_invalidates_customer_sharing_preview(reservation_db):
    from app.models.invoice_task import CustomerInvoiceProfile
    db, data = reservation_db
    source = data['product']
    source.sale_unit_price = Decimal('5')
    db.commit()
    values = args(data)
    plan = sharing.preview(db, actor=data['admin'], **values)
    db.add(CustomerInvoiceProfile(customer_id=data['other_customer'].id,
        price_tax_mode='tax_exclusive', is_enabled=True))
    db.commit()
    with pytest.raises(HTTPException, match='变化'):
        sharing.confirm(db, actor=data['admin'], preview_hash=plan['preview_hash'], **values)
    db.rollback()
    assert db.query(SharedFinishedGroup).count() == 0


def test_shared_price_api_user_only_needs_own_customer_scope(reservation_db):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api import products
    from app.api.deps import get_db, get_current_user
    from app.models.access_control import UserCustomerScope
    from app.models.user import User
    db, data = reservation_db
    source, target, _, _ = priced_pair(db, data)
    save(db, data, args(data))
    sales = User(username='one-customer-sales', real_name='单客户销售', password_hash='test', role='sales',
        customer_access_mode='selected', must_change_password=False)
    db.add(sales)
    db.flush()
    db.add(UserCustomerScope(user_id=sales.id, customer_id=source.customer_id, assigned_by=data['admin'].id))
    db.commit()
    app = FastAPI()
    app.include_router(products.router, prefix='/api/master/products')
    def session():
        yield db
    app.dependency_overrides[get_db] = session
    app.dependency_overrides[get_current_user] = lambda: sales
    body = {field: getattr(source, field) for field in products.ProductPayload.model_fields if hasattr(source, field)}
    body.update(sale_unit_price=Decimal('6.123456'), expected_version=source.version,
                structure_edit_fields=[], change_reason='仅调整本客户单价')
    payload = products.ProductUpdatePayload(**body).model_dump(mode='json')
    client = TestClient(app)
    preview = client.post(f'/api/master/products/{source.id}/update-preview', json=payload)
    assert preview.status_code == 200, preview.text
    payload['confirmation_token'] = preview.json()['confirmation_token']
    response = client.put(f'/api/master/products/{source.id}', json=payload)
    assert response.status_code == 200, response.text
    assert Decimal(str(response.json()['sale_unit_price'])) == Decimal('6.123456')
    assert target.sale_unit_price == Decimal('8')
    assert source.layer_count is None  # Correcting price must not backfill physical facts.
    assert client.get(f'/api/master/products/{target.id}').status_code == 403
    payload.update(expected_version=source.version, report_length_mm=802)
    assert client.put(f'/api/master/products/{source.id}', json=payload).status_code == 403
    assert source.report_length_mm == 800
    payload.update(report_length_mm=800, material_id=999999)
    assert client.post(f'/api/master/products/{source.id}/update-preview', json=payload).status_code in {400, 404, 422}
    payload.update(material_id=source.material_id, sale_unit_price_no_tax='1.1234567')
    assert client.post(f'/api/master/products/{source.id}/update-preview', json=payload).status_code == 422


def test_bom_new_customer_inherits_each_real_product_price_once(composite_requisition_app, _p181_published_map_identity):
    from app.models.customer import Customer
    from app.models.user import User
    from tests.test_shared_bom_stock import seed_pair
    app, factory = composite_requisition_app
    seed_pair(factory)
    with factory() as db:
        for pid, price in ((1, '5.123456'), (2, '0.123456'), (3, '0.654321')):
            db.get(Product, pid).sale_unit_price = Decimal(price)
        target_customer = Customer(customer_number=9031, customer_code='NEW-PRICE-BOM', name='BOM独立价客户')
        db.add(target_customer)
        db.commit()
        values = dict(customer_id=target_customer.id, source_customer_id=1, product_ids=[1],
            new_products_shared=True, delivery_heading_customer_id=None, delivery_address_customer_id=None)
        data = {'admin': db.get(User, 1)}
        save(db, data, values)
        targets = list(db.scalars(select(Product).where(Product.customer_id == target_customer.id)))
        source_prices = {p.product_code: p.sale_unit_price for p in db.scalars(select(Product).where(Product.customer_id == 1))}
        assert len(targets) == 3 and all(p.sale_unit_price == source_prices[p.product_code] for p in targets)
        parent = next(p for p in targets if p.is_composite)
        edit(db, data, parent, {'sale_unit_price': Decimal('8.123456')})
        db.commit()
        save(db, data, values)
        assert parent.sale_unit_price == Decimal('8.123456') and db.get(Product, 1).sale_unit_price == Decimal('5.123456')
