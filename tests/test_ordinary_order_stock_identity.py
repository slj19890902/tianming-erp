from datetime import date
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.user import User
from app.models.warehouse_inventory import InventoryLot, InventoryReservation
from app.services.finished_stock_identity import product_basis, new_order_stock_basis, order_product_basis
from app.services.warehouse_inventory import reserve_finished_inventory, finished_inventory_candidates_for_product, WarehouseInventoryError
from tests.test_p1_18_pdf_inventory_contract import inventory_preview_app, _login, _preview_item, _finished_plan


@pytest.fixture
def ordinary(inventory_preview_app):
    app, factory, ids = inventory_preview_app
    with factory() as db:
        product = db.get(Product, ids['full_product'])
        product.unit = '只'
        product.length_mm = product.width_mm = product.height_mm = 100
        db.flush()
        db.get(InventoryLot, ids['full_lot']).finished_detail.physical_basis_json = product_basis(product)
        db.commit()
    return app, factory, ids


def add_item(db, ids, *, frozen=True):
    product = db.get(Product, ids['full_product'])
    order = Order(order_number='IDENTITY-TEST', customer_id=ids['customer'], order_date=date.today())
    db.add(order); db.flush()
    item = OrderItem(order_id=order.id, product_id=product.id, quantity=10, unit_price=1, subtotal=10,
        snapshot_product_name=product.product_name, snapshot_product_code=product.product_code,
        sales_unit_snapshot=product.unit, requisition_status='未报料')
    if frozen:
        item.stock_identity_snapshot_json = new_order_stock_basis(product, item)
    db.add(item); db.flush()
    return item


def reserve(db, ids, item):
    actor = db.scalar(select(User).where(User.username == 'preview-allowed'))
    return reserve_finished_inventory(db, order_item_id=item.id, inventory_lot_id=ids['full_lot'],
        quantity=10, expected_version=1, operator_id=actor.id,
        idempotency_key='ordinary-identity-reserve', warning_acknowledged_codes=[])


@pytest.mark.parametrize('change', ['size', 'process', 'material', 'unit', 'unknown'])
def test_changed_or_unknown_stock_rejected_in_preview_and_reserve(ordinary, change):
    app, factory, ids = ordinary
    with factory() as db:
        product = db.get(Product, ids['full_product'])
        if change == 'size': product.length_mm = 200
        elif change == 'process': product.production_process = '印刷,模切'
        elif change == 'material': product.legacy_material_text = 'OTHER'
        elif change == 'unit': product.unit = '套'
        else: db.get(InventoryLot, ids['full_lot']).finished_detail.physical_basis_json = None
        db.commit()
    with TestClient(app) as client:
        _login(client, 'preview-allowed')
        response = client.post('/api/orders/inventory-draft-preview', json={'customer_id': ids['customer'],
            'items': [_preview_item(line='CHANGE', product_id=ids['full_product'], quantity=10,
                finished=[_finished_plan(ids['full_lot'], 10)])]})
        assert response.status_code == 409, response.text
        assert '依据' in response.json()['detail']
    with factory() as db:
        item = add_item(db, ids)
        with pytest.raises(WarehouseInventoryError): reserve(db, ids, item)
        lot = db.get(InventoryLot, ids['full_lot'])
        assert (lot.quantity_available, lot.quantity_reserved) == (100, 0)
        assert not list(db.scalars(select(InventoryReservation)))


def test_code_rename_and_later_master_edit_preserve_frozen_order(ordinary):
    _, factory, ids = ordinary
    with factory() as db:
        item = add_item(db, ids)
        frozen = item.stock_identity_snapshot_json
        product = db.get(Product, ids['full_product'])
        product.product_code = '00012-RENAMED'
        db.flush()
        assert ids['full_lot'] in [lot.id for lot in finished_inventory_candidates_for_product(
            db, customer_id=ids['customer'], product_id=product.id)]
        product.length_mm = 250
        db.flush()
        assert order_product_basis(db, item.id, product.id) == frozen
        row = reserve(db, ids, item)
        assert row.reserved_stock_quantity == 10
        lot = db.get(InventoryLot, ids['full_lot'])
        assert (lot.quantity_available, lot.quantity_reserved) == (90, 10)
        assert reserve(db, ids, item).id == row.id
        assert (lot.quantity_available, lot.quantity_reserved) == (90, 10)


def test_historical_order_without_identity_cannot_guess_current_master(ordinary):
    _, factory, ids = ordinary
    with factory() as db:
        item = add_item(db, ids, frozen=False)
        with pytest.raises(WarehouseInventoryError, match='订单缺少冻结实物身份'):
            reserve(db, ids, item)
        assert item.stock_identity_snapshot_json is None


def test_new_order_http_freezes_identity_before_reserving_and_replays(ordinary):
    app, factory, ids = ordinary
    with TestClient(app) as client:
        _login(client, 'preview-allowed')
        payload = {'customer_id': ids['customer'], 'order_date': date.today().isoformat(),
            'idempotency_key': 'ordinary-identity-http-create', 'items': [{
                'product_id': ids['full_product'], 'quantity': 10, 'unit_price': 1,
                'reservation_plan': {'finished': [_finished_plan(ids['full_lot'], 10)], 'semi': []}}]}
        response = client.post('/api/orders', json=payload)
        assert response.status_code == 201, response.text
        again = client.post('/api/orders', json=payload)
        assert again.status_code in (200, 201), again.text
    with factory() as db:
        item = db.scalar(select(OrderItem))
        assert json.loads(item.stock_identity_snapshot_json)['product_id'] == ids['full_product']
        lot = db.get(InventoryLot, ids['full_lot'])
        assert (lot.quantity_available, lot.quantity_reserved) == (90, 10)
        assert len(list(db.scalars(select(InventoryReservation)))) == 1


def test_unknown_stock_can_be_explicitly_confirmed_then_used_by_new_order(ordinary):
    from app.services.finished_stock_identity import identity_preview, confirm_identity
    app,factory,ids=ordinary
    with factory() as db:
        lot=db.get(InventoryLot,ids['full_lot']);lot.finished_detail.physical_basis_json=None
        actor=db.scalar(select(User).where(User.username=='preview-allowed'));actor.role='admin';db.commit()
        preview=identity_preview(db,lot.id)
        confirm_identity(db,lot_id=lot.id,preview_hash=preview['preview_hash'],operation_key='confirm-counted-fg',actor=actor)
        db.commit()
        frozen=lot.finished_detail.physical_basis_json
        item=add_item(db,ids)
        result=reserve_finished_inventory(db,order_item_id=item.id,inventory_lot_id=lot.id,quantity=10,
            expected_version=lot.version,operator_id=actor.id,idempotency_key='reserve-confirmed-fg',warning_acknowledged_codes=[])
        assert result.reserved_stock_quantity==10
        assert lot.finished_detail.physical_basis_json==frozen
        assert lot.quantity_available+lot.quantity_reserved==100


@pytest.mark.parametrize('pdf_import, expected_status', [(False, 409), (True, 200)])
def test_pdf_material_uses_the_same_master_as_save_while_manual_override_remains_explicit(ordinary, pdf_import, expected_status):
    from app.models.material import Material
    from app.api.orders import OrderItemCreate, _preflight_reservation_plans
    from app.services.finished_stock_identity import draft_order_stock_basis
    app, factory, ids = ordinary
    with factory() as db:
        product = db.get(Product, ids['full_product'])
        master = Material(code='MASTER-MATERIAL', layer_count=3, flute_type=product.flute_type)
        scanned = Material(code='SCANNED-MATERIAL', layer_count=3, flute_type=product.flute_type)
        db.add_all([master, scanned]); db.flush()
        product.material = master
        db.get(InventoryLot, ids['full_lot']).finished_detail.physical_basis_json = product_basis(product)
        db.commit()
        line = _preview_item(line='MATERIAL', product_id=product.id, quantity=10,
            finished=[_finished_plan(ids['full_lot'], 10)])
        line.update(material=scanned.code, material_id=scanned.id, layer_count=3)
        create = OrderItemCreate(**line, unit_price=1)
        if pdf_import:
            _preflight_reservation_plans(db, customer_id=ids['customer'], payload_items=[create],
                resolved_products={1: product}, pdf_import=True)
            saved = OrderItem(snapshot_material=master.code, layer_count=master.layer_count)
            assert draft_order_stock_basis(product, create, pdf_matched_product=True) == new_order_stock_basis(product, saved)
        else:
            with pytest.raises(WarehouseInventoryError):
                _preflight_reservation_plans(db, customer_id=ids['customer'], payload_items=[create],
                    resolved_products={1: product})
    with TestClient(app) as client:
        _login(client, 'preview-allowed')
        response = client.post('/api/orders/inventory-draft-preview', json={
            'customer_id': ids['customer'], 'pdf_import': pdf_import, 'items': [line]})
        assert response.status_code == expected_status, response.text
