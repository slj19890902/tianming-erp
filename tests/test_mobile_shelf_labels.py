from urllib.parse import urlsplit, parse_qs
from types import SimpleNamespace
from pathlib import Path
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select
from tests.test_p1_47d_inventory_adjustment import stocktake_app, _login
from app.models.customer import Customer
from app.models.user import User
from app.models.warehouse_inventory import InventoryLot, WarehouseArea, WarehouseLocation
from app.services.mobile_shelf_labels import readable_address, product_key, mobile_url, print_address


def test_pdf_print_address_uses_area_and_human_rack_not_internal_code():
    result = print_address(dict(display_path="三楼·D02·A·2层·3格", level_no=2, slot_no=3))
    assert result == dict(print_title="D02-A架", print_floor="三楼", print_position="02层-03格",
                         compact_title="A", compact_position="2层-3格")
    assert print_address(dict(display_path="三楼·北货架G1·G1·2层·1格", level_no=2, slot_no=1))["print_title"] == "北货架G1-G1架"
    assert print_address(dict(display_path="三楼·D02·A架·2层·3格", level_no=2, slot_no=3))["print_title"] == "D02-A架"
    with pytest.raises(HTTPException):
        print_address(dict(display_path="三楼·EDIT-076·A·2层·1格", level_no=2, slot_no=1))


def test_product_label_compact_address_keeps_rack_identity_and_location_modes():
    result = print_address(dict(display_path="二楼·C货架·R013架·02层·02格", level_no=2, slot_no=2))
    assert result['compact_title'] == 'R013'
    assert result['compact_position'] == '2层-2格'
    assert result['print_title'] == 'C货架-R013架'
    assert result['print_floor'] == '二楼'
    ground = print_address(dict(display_path="一楼·成品待送区"))
    assert ground['compact_title'] == ground['print_title']
    assert ground['compact_position'] == ''


def test_readable_address_hides_internal_identity():
    assert readable_address(dict(display_path="三楼·北货架G1·G1·2层·1格", level_no=2, slot_no=1)) == (
        "三楼 北货架G1", "02层-01格", "三楼 北货架G1 -02层-01格")
    with pytest.raises(HTTPException):
        readable_address(dict(display_path="3F-EDIT-076-A-2-1"))
    key = "a" * 24
    assert mobile_url("https://example.com/", 10, key) == f"https://example.com/q/10/{key}"


def test_identity_keeps_customer_spec_unit_and_product_separate():
    def lot(**kwargs):
        return SimpleNamespace(id=1, inventory_type="finished", unit=kwargs.pop("unit", "pcs"),
            finished_detail=SimpleNamespace(owner_customer_id=kwargs.pop("customer",1), product_id=kwargs.pop("product",2),
                                            length_mm=kwargs.pop("length",100), **kwargs))
    first=product_key(lot())
    assert first==product_key(lot())
    assert len({first, product_key(lot(customer=2)),product_key(lot(product=3)),
                product_key(lot(length=101)),product_key(lot(unit="sets"))})==5


def test_labels_scan_live_filter_scope_and_no_inventory_writes(stocktake_app):
    app,factory,ids,_=stocktake_app
    with factory() as db:
        db.get(Customer, ids["customer"]).chinese_short_name="甲客户"
        db.get(User, ids["other"]).customer_access_mode="selected"
        for area in db.scalars(select(WarehouseArea)):
            area.area_name="成品"+str(area.id)+"区"
        for location in db.scalars(select(WarehouseLocation)):
            location.location_name="成品货位"+str(location.id)
        db.commit()
        lot=db.scalar(select(InventoryLot).where(InventoryLot.warehouse_location_id==ids["loc_fg1"],
                                               InventoryLot.inventory_type=="finished"))
        lot_id=lot.id
        before=[(x.id,x.quantity_available,x.quantity_reserved,x.warehouse_location_id) for x in db.scalars(select(InventoryLot))]
    endpoint=f"/api/warehouse/locations/{ids['loc_fg1']}"
    with TestClient(app) as client:
        assert client.get(endpoint+"/scan").status_code==401
        _login(client,"p147d-admin")
        position=client.get(endpoint+"/mobile-label")
        assert position.status_code==200,position.text
        assert "product" not in position.json()
        assert f"/q/{ids['loc_fg1']}" in position.json()["lookup_url"]
        label=client.get(endpoint+f"/mobile-label?lot_id={lot_id}")
        assert label.status_code==200,label.text
        assert not any("quantity" in x for x in label.json()["product"])
        key=urlsplit(label.json()["lookup_url"]).path.rsplit("/",1)[1]
        scan=client.get(endpoint+"/scan?product="+key)
        assert scan.status_code==200,scan.text
        assert scan.headers["cache-control"]=="no-store"
        assert len(scan.json()["items"])==1
        original=scan.json()["items"][0]["quantity"]
        assert client.get(endpoint+"/scan?product="+"0"*24).json()["items"]==[]
        assert client.get(f"/api/warehouse/locations/{ids['loc_fg1_add']}/mobile-label?lot_id={lot_id}").status_code==409
        with factory() as db:
            assert before==[(x.id,x.quantity_available,x.quantity_reserved,x.warehouse_location_id) for x in db.scalars(select(InventoryLot))]
            row=db.get(InventoryLot,lot_id);row.quantity_available+=3;db.commit()
        assert client.get(endpoint+"/scan?product="+key).json()["items"][0]["quantity"]==original+3
        with factory() as db:
            db.get(InventoryLot,lot_id).warehouse_location_id=ids["loc_fg1_add"];db.commit()
        old=client.get(endpoint+"/scan?product="+key).json()
        assert all(lot_id!=batch["id"] for item in old["items"] for batch in item["lots"])
        _login(client,"p147d-other")
        assert client.get(f"/api/warehouse/locations/{ids['loc_fg1_add']}/mobile-label?lot_id={lot_id}").status_code==403
        assert client.get(endpoint+"/scan?product="+key).json()["items"]==[]


def test_scan_and_print_are_lightweight_and_product_specific():
    root=Path(__file__).resolve().parents[1]
    scan=(root/"static/shelf-scan.js").read_text(encoding="utf-8")
    assert "cache:'no-store'" in scan
    assert "/api/auth/login" in scan
    assert "data.location?.id" in scan
    assert "warehouseTwin" not in scan
    ui=(root/"factory_twin/frontend/src/WarehouseTwinApp.tsx").read_text(encoding="utf-8")
    assert "&lot_id=${encodeURIComponent(lotId)}" in ui
    assert 'className="shelf-position-print"' in ui


def test_whole_rack_grouping_uses_identity_not_name(monkeypatch):
    import app.api.warehouse as api
    monkeypatch.setattr(api, 'load_warehouse_location_projection_contexts', lambda db, rows: {})
    monkeypatch.setattr(api, '_require_printable_location_label', lambda *args: None)
    monkeypatch.setattr(api, 'employee_location_name', lambda *args, **kwargs: '三楼·D02·A·2层·3格')
    def query(area=10, rack='rack-a', code='A', kind='rack_slot', location_id=1):
        row=SimpleNamespace(id=location_id,level_no=2,slot_no=3,address_area_id=area,
                            map_rack_id=rack,rack_code=code,address_kind=kind)
        return api._mobile_shelf_location(SimpleNamespace(get=lambda *args:row),location_id)[1]
    first=query()
    assert first['rack_label']=='D02-A架'
    assert query(location_id=2)['rack_key']==first['rack_key']
    assert query(area=11)['rack_key']!=first['rack_key']
    assert query(rack='rack-b')['rack_key']!=first['rack_key']
    assert query(rack=None)['rack_key']!=first['rack_key']
    assert 'rack_key' not in query(kind='ground_slot')


def test_whole_rack_qr_opens_rack_not_single_cell(monkeypatch):
    import app.api.warehouse as api
    from fastapi import Response
    row=SimpleNamespace(id=1,map_rack_id='rack-ABC',warehouse_floor=3)
    monkeypatch.setattr(api,'_mobile_shelf_location',lambda *args:(row,{'rack_key':'stable','rack_label':'A架'}))
    result=api.mobile_shelf_label(1,Response(),lot_id=None,db=None,user=None)
    parsed=urlsplit(result['rack_lookup_url'])
    assert parsed.path=='/scan/rack'
    assert parse_qs(parsed.query)=={'floor':['3F'],'rack_id':['rack-ABC']}
    assert result['rack_qr_data_url'].startswith('data:image/png;base64,')
    assert result['qr_data_url']!=result['rack_qr_data_url']


def test_mobile_rack_and_lot_entries_preserve_desktop_map():
    from app.main import app

    with TestClient(app) as client:
        rack = client.get('/scan/rack?floor=3F&rack_id=rack-A', follow_redirects=False)
        assert rack.status_code == 307
        assert rack.headers['location'] == '/mobile/?mobile_page=warehouse&warehouse_map=1&floor_code=3F&rack_id=rack-A#warehouse'
        lot = client.get('/I/123', follow_redirects=False)
        assert lot.status_code == 307
        assert lot.headers['location'] == '/mobile/?mobile_page=warehouse&warehouse_map=1&lot_id=123#warehouse'
        old_mobile = client.get(
            '/warehouse.html?floor=3F&rack_id=rack-A',
            headers={'user-agent': 'Mozilla/5.0 iPhone MicroMessenger'},
            follow_redirects=False,
        )
        assert old_mobile.status_code == 302
        assert old_mobile.headers['location'] == '/scan/rack?floor=3F&rack_id=rack-A'
        old_desktop = client.get(
            '/warehouse.html?floor=3F&rack_id=rack-A',
            headers={'user-agent': 'Mozilla/5.0 Windows NT 10.0'},
            follow_redirects=False,
        )
        assert old_desktop.status_code == 200
        assert client.get('/scan/rack?floor=3F&rack_id=../../bad', follow_redirects=False).status_code == 400
