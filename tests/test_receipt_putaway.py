from sqlalchemy import select
import pytest
from app.models.receipt_putaway import ProductStoragePreference, ReceiptStagingArea
from app.models.fixed_shelf import ShelfBinding, ShelfProfile
from app.models.warehouse_inventory import WarehouseArea, WarehouseLocation
from app.services import receipt_putaway as service
from app.services.fixed_shelf import ShelfError
from test_fixed_shelf import setup, incoming
from test_p1_123_warehouse_region_rack_labels import rack_factory


@pytest.fixture(autouse=True)
def _isolate_receipt_storage_from_entry_cost(monkeypatch):
    monkeypatch.setattr("app.services.inventory_valuation.freeze_entry_cost", lambda *_args, **_kwargs: None)


def choose(db, pid, lid, version=0):
    loc=db.get(WarehouseLocation,lid)
    area=db.scalar(select(WarehouseArea).where(WarehouseArea.area_code==loc.area_code))
    return service.save(db,pid,expected_version=version,area_id=area.id,location_id=lid,
        address_version=loc.address_version,layout_version=loc.floor3_layout.version)


def test_mixed_cell_preference_and_cancel_are_not_exclusive_binding(setup):
    factory,pid,cid,ids=setup
    with factory() as db:
        first=incoming(db,pid,cid,ids[0])
        choose(db,pid,ids[0]);db.flush()
        assert service.info(db,pid)['location_id']==ids[0]
        assert first.quantity_available==125
        with pytest.raises(ShelfError):choose(db,pid,ids[1],0)
        service.save(db,pid,expected_version=1,area_id=None,location_id=None)
        service.remember_stocktake(db,first,1)
        assert db.get(ProductStoragePreference,pid).location_id is None
        assert db.get(ProductStoragePreference,pid).version==2


def test_first_admin_stocktake_remembers_without_moving_stock(setup):
    factory,pid,cid,ids=setup
    with factory() as db:
        first=incoming(db,pid,cid,ids[0])
        service.remember_stocktake(db,first,1)
        assert service.info(db,pid)['location_id']==ids[0]
        second=incoming(db,pid,cid,ids[1],key='second')
        service.remember_stocktake(db,second,1)
        assert service.info(db,pid)['location_id']==ids[0]
        assert first.warehouse_location_id==ids[0] and second.warehouse_location_id==ids[1]


def test_remembered_and_manually_changed_locations_drive_receipt_routing(setup):
    factory,pid,cid,ids=setup
    with factory() as db:
        first=incoming(db,pid,cid,ids[0])
        service.remember_stocktake(db,first,1)
        preference=db.get(ProductStoragePreference,pid)
        db.add(ReceiptStagingArea(area_id=preference.area_id))
        db.flush()
        location,kind,_warning=service.resolve(db,product_id=pid,customer_id=cid)
        assert (location.id,kind)==(ids[0],'product_storage')
        choose(db,pid,ids[1],version=1)
        service.remember_stocktake(db,first,1)
        assert service.info(db,pid)['version']==2
        location,kind,_warning=service.resolve(db,product_id=pid,customer_id=cid)
        assert (location.id,kind)==(ids[1],'product_storage')


def test_existing_fixed_shelf_binding_is_not_replaced_by_automatic_preference(setup):
    factory,pid,cid,ids=setup
    with factory() as db:
        db.add(ShelfProfile(product_id=pid,version=1))
        db.flush()
        db.add(ShelfBinding(location_id=ids[1],product_id=pid,priority=0))
        first=incoming(db,pid,cid,ids[0])
        service.remember_stocktake(db,first,1)
        assert service.info(db,pid)['configured'] is False
        assert db.get(ShelfBinding,ids[1]).product_id==pid


def test_general_finished_stock_does_not_create_customer_product_preference(setup):
    factory,pid,cid,ids=setup
    with factory() as db:
        first=incoming(db,pid,cid,ids[0])
        first.finished_detail.is_general=True
        service.remember_stocktake(db,first,1)
        assert service.info(db,pid)['configured'] is False


def test_first_explicit_add_is_remembered_even_when_product_exists_elsewhere(setup):
    factory,pid,cid,ids=setup
    with factory() as db:
        first=incoming(db,pid,cid,ids[0])
        incoming(db,pid,cid,ids[1],key='second')
        service.remember_stocktake(db,first,1)
        assert service.info(db,pid)['location_id']==ids[0]


def test_stale_map_identity_is_rejected(setup):
    factory,pid,cid,ids=setup
    with factory() as db:
        loc=db.get(WarehouseLocation,ids[0])
        area=db.scalar(select(WarehouseArea).where(WarehouseArea.area_code==loc.area_code))
        with pytest.raises(ShelfError,match='身份已变化'):
            service.save(db,pid,expected_version=0,area_id=area.id,location_id=loc.id,address_version=999,layout_version=999)
        assert service.info(db,pid)['version']==0


def test_no_configured_staging_retains_old_installation_contract(setup):
    factory,pid,cid,ids=setup
    with factory() as db:
        assert service.resolve(db,product_id=pid,customer_id=cid) is None


def test_api_admin_idempotency_employee_and_customer_scope(setup):
    from app.api import fixed_shelf as api
    from app.models.user import User
    from fastapi import HTTPException
    from starlette.requests import Request
    factory,pid,cid,ids=setup
    with factory() as db:
        user=db.get(User,1)
        loc=db.get(WarehouseLocation,ids[0])
        area=db.scalar(select(WarehouseArea).where(WarehouseArea.area_code==loc.area_code))
        payload=api.StoragePayload(expected_version=0,area_id=area.id,location_id=loc.id,
            address_version=loc.address_version,layout_version=loc.floor3_layout.version,idempotency_key='storage-api')
        request=Request({'type':'http','method':'PUT','path':'/test','headers':[]})
        result=api.save_receipt_storage(pid,payload,request,db,user)
        assert result['version']==1
        assert api.save_receipt_storage(pid,payload,request,db,user)==result
        with pytest.raises(HTTPException) as exc:
            api.save_receipt_storage(pid,payload.model_copy(update={'location_id':ids[1]}),request,db,user)
        assert exc.value.status_code==409
        employee=User(username='storage-employee',password_hash='unused',role='workshop',real_name='测试员工',customer_access_mode='all')
        db.add(employee);db.flush()
        with pytest.raises(HTTPException) as exc:
            api.save_receipt_storage(pid,payload,request,db,employee)
        assert exc.value.status_code==403
        employee.role='sales';employee.customer_access_mode='selected'
        with pytest.raises(HTTPException) as exc:
            api.receipt_storage(pid,db,employee)
        assert exc.value.status_code==403
