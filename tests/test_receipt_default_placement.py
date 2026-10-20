"""Latest receipt priority uses saved destinations, never a mandatory default."""
import pytest
from sqlalchemy import select, delete
from app.models.receipt_putaway import ProductStoragePreference, ReceiptStagingArea
from app.models.customer_finished_storage_preference import CustomerFinishedStoragePreference
from app.models.warehouse_inventory import WarehouseArea, WarehouseFloor, WarehouseLocation, InventoryPallet
from app.services.production_workflow import _receipt_auto_finished_ground_target, ProductionWorkflowError
from tests.test_p1_134_customer_finished_storage import customer_finished_storage_app
from tests.test_warehouse_inventory_foundation import seed_product


@pytest.mark.parametrize('product_setting', ['missing', 'cleared', 'area', 'location'])
@pytest.mark.parametrize('customer_floor', [1, 3])
def test_product_destination_then_customer_area_on_selected_floor(customer_finished_storage_app, monkeypatch, product_setting, customer_floor):
    from app.services import location_candidates
    app = customer_finished_storage_app
    ids = app.state.ids
    identity = location_candidates.load_warehouse_twin_published_floor_identity(4)
    monkeypatch.setattr(location_candidates, 'load_warehouse_twin_published_floor_identity',
                        lambda floor: identity if int(floor) == customer_floor else None)
    with app.state.session_factory() as db:
        floor = db.scalar(select(WarehouseFloor))
        floor.floor_number = customer_floor
        floor.floor_code = f'{customer_floor}F'
        for loc in db.scalars(select(WarehouseLocation)):
            loc.warehouse_floor = customer_floor
        db.execute(delete(InventoryPallet))
        _, product = seed_product(db)
        product.customer_id = ids['customer']
        db.flush()
        # The first-floor staging registration must never beat the customer area.
        if customer_floor == 1:
            db.add(ReceiptStagingArea(area_id=ids['a1']))
        db.add(CustomerFinishedStoragePreference(customer_id=ids['customer'], warehouse_area_id=ids['a2'], priority=1))
        product_loc = db.scalar(select(WarehouseLocation).where(WarehouseLocation.address_area_id == ids['a1']).order_by(WarehouseLocation.id))
        if product_setting != 'missing':
            db.add(ProductStoragePreference(product_id=product.id,
                area_id=ids['a1'] if product_setting in {'area', 'location'} else None,
                location_id=product_loc.id if product_setting == 'location' else None, version=1))
        db.flush()
        preview = _receipt_auto_finished_ground_target(db, claim=False, customer_id=ids['customer'], product_id=product.id)
        posted = _receipt_auto_finished_ground_target(db, claim=True, customer_id=ids['customer'], product_id=product.id)
        expected_area = 'A1' if product_setting in {'area', 'location'} else 'A2'
        assert preview.location.id == posted.location.id
        assert posted.location.area_code == expected_area
        assert posted.location.warehouse_floor == customer_floor
        if product_setting == 'location':
            assert posted.location.id == product_loc.id
        if product_setting == 'missing':
            assert db.get(ProductStoragePreference, product.id) is None


def test_invalid_explicit_destination_and_customer_mismatch_do_not_fall_back(customer_finished_storage_app):
    app = customer_finished_storage_app
    ids = app.state.ids
    with app.state.session_factory() as db:
        _, product = seed_product(db)
        product.customer_id = ids['customer']
        loc = db.scalar(select(WarehouseLocation).where(WarehouseLocation.address_area_id == ids['a1']))
        db.add(ProductStoragePreference(product_id=product.id, area_id=ids['a1'], location_id=loc.id, version=1))
        loc.is_active = False
        db.flush()
        with pytest.raises(ProductionWorkflowError, match='产品默认货位不可用'):
            _receipt_auto_finished_ground_target(db, claim=True, customer_id=ids['customer'], product_id=product.id)
        with pytest.raises(ProductionWorkflowError, match='产品与收料客户不一致'):
            _receipt_auto_finished_ground_target(db, claim=False, customer_id=ids['customer']+999, product_id=product.id)
