from decimal import Decimal
from types import SimpleNamespace
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from tests.test_p1_76_mobile_portal import mobile_portal_app, mobile_erp_app, _login
from app.models.order import Order, OrderItem
from app.services.mobile_order_dimensions import parse_dimensions, best_match


def test_units_direction_tolerance_and_snapshot():
    assert parse_dimensions("80 x 60 cm") == [Decimal(800), Decimal(600)]
    item = SimpleNamespace(cardboard_len=800, cardboard_width=600, snapshot_report_length_mm=None, snapshot_report_width_mm=None, snapshot_spec="420×310×260mm")
    assert best_match(item, None, "board", [Decimal(600), Decimal(800)], 10, "any") is None
    assert best_match(item, None, "board", [Decimal(600)], 0, "any")["differences"][0]["dimension"] == "宽"
    assert best_match(item, None, "board", [Decimal(806)], 5, "length") is None
    assert best_match(item, None, "board", [Decimal(806)], 10, "length")["level"] == "扩大范围"
    assert best_match(item, None, "box", parse_dimensions("418×313"), 5, "any")["source"] == "订单规格"
    with pytest.raises(ValueError): parse_dimensions("800板600")


def test_dimension_api_scope_siblings_validation_and_readonly(mobile_portal_app):
    app, ids, factory = mobile_portal_app
    with factory() as db:
        order = db.scalar(select(Order).where(Order.order_number == "SO-MOBILE-PORTAL-001"))
        item = db.scalar(select(OrderItem).where(OrderItem.order_id == order.id))
        item.cardboard_len = 800; item.cardboard_width = 600
        order_id, item_id = order.id, item.id
        other_id = db.scalar(select(Order.id).where(Order.order_number == "SO-OTHER-MOBILE-001"))
        db.commit()
    with TestClient(app) as client:
        _login(client, "mobile-admin")
        url = "/api/mobile/erp/orders/by-dimensions"
        response = client.get(url, params={"dimensions":"798×603", "tolerance": "5"})
        assert response.status_code == 200, response.text
        group = response.json()["groups"][0]
        assert group["total"] == 1
        assert group["items"][0]["dimension_match"]["differences"] == [{"dimension":"长", "mm":2}, {"dimension":"宽", "mm":-3}]
        assert "unit_price" not in str(response.json())
        assert client.get(url, params={"dimensions":"600×800"}).json()["groups"][0]["total"] == 0
        assert client.get(url, params={"dimensions":"418×310", "domain":"box"}).json()["groups"][0]["total"] >= 1
        assert client.get(url, params={"dimensions":"800×600×1"}).status_code == 422
        assert client.get(url, params={"dimensions":"800", "tolerance":50}).status_code == 422
        siblings = client.get(f"/api/mobile/erp/orders/{order_id}/unfulfilled")
        assert siblings.status_code == 200, siblings.text
        assert siblings.json()["items"][0]["order_item_id"] == item_id
        _login(client, "mobile-scoped")
        assert client.get(f"/api/mobile/erp/orders/{other_id}/unfulfilled").status_code in (403,404)
    with factory() as db:
        item = db.get(OrderItem, item_id)
        assert item.quantity == 20 and item.delivered_quantity == 0
        item.is_force_closed = True; db.commit()
    with TestClient(app) as client:
        _login(client, "mobile-admin")
        assert client.get(url, params={"dimensions":"800×600"}).json()["groups"][0]["total"] == 0


def test_dimension_settings_admin_version_idempotency_and_query(mobile_portal_app, monkeypatch, tmp_path):
    from app.services.mobile_dimension_settings import read
    monkeypatch.setenv("ERP_MOBILE_DIMENSION_SETTINGS_PATH",str(tmp_path/"dimension.json"))
    app,ids,factory=mobile_portal_app
    with TestClient(app) as client:
        endpoint="/api/mobile/erp/dimension-settings"
        assert client.get(endpoint).status_code==401
        _login(client,"mobile-admin")
        assert client.get(endpoint).json()["near_mm"]==5
        body=dict(near_mm=7,expanded_mm=12,expected_version=0,operation_key="settings-save-001")
        assert client.put(endpoint,json=body).status_code==200
        assert client.put(endpoint,json=body).json()["version"]==1
        assert len(read()["history"])==1
        assert client.put(endpoint,json={**body,"operation_key":"settings-save-002"}).status_code==409
        assert client.put(endpoint,json={**body,"near_mm":8}).status_code==422
        assert client.put(endpoint,json={**body,"near_mm":True}).status_code==422
        assert client.get("/api/mobile/erp/orders/by-dimensions",params={"dimensions":"800","tolerance":12}).status_code==200
        assert client.get("/api/mobile/erp/orders/by-dimensions",params={"dimensions":"800","tolerance":10}).status_code==422
        _login(client,"mobile-scoped")
        assert client.put(endpoint,json={**body,"expected_version":1,"operation_key":"settings-save-003"}).status_code==403
        assert len(read()["history"])==1
