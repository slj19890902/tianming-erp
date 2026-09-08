from datetime import date

from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.test_p1_76_mobile_portal import mobile_portal_app, mobile_erp_app, _login
from app.models.order import Order, OrderItem


def test_unfulfilled_dates_remaining_scope_and_pagination(mobile_portal_app):
    app, ids, factory = mobile_portal_app
    with factory() as db:
        order = db.scalar(select(Order).where(Order.order_number == "SO-MOBILE-PORTAL-001"))
        item = db.scalar(select(OrderItem).where(OrderItem.order_id == order.id))
        item.delivered_quantity = 7
        db.commit()
    with TestClient(app) as client:
        _login(client, "mobile-admin")
        params = {"category": "orders", "unfulfilled_only": True, "page_size": 1}
        first = client.get("/api/mobile/erp/search", params=params)
        assert first.status_code == 200, first.text
        group = first.json()["groups"][0]
        assert group["total"] == 2
        second = client.get("/api/mobile/erp/search", params={**params, "page": 2}).json()["groups"][0]
        assert group["items"][0]["order_item_id"] != second["items"][0]["order_item_id"]
        filtered = client.get("/api/mobile/erp/search", params={**params, "date_field": "delivery_date", "date_from": "2026-08-20", "date_to": "2026-08-20"})
        row = filtered.json()["groups"][0]["items"][0]
        assert row["delivered_quantity"] == 7 and row["remaining_quantity"] == 13
        assert client.get("/api/mobile/erp/search", params={**params, "date_from": "2026-08-19"}).json()["groups"][0]["total"] == 0
        assert client.get("/api/mobile/erp/search", params={**params, "date_from": "2026-09-01", "date_to": "2026-08-01"}).status_code == 422
        assert client.get("/api/mobile/erp/search", params={**params, "q": "OTHER"}).json()["groups"][0]["total"] == 1


def test_unfulfilled_excludes_delivered_cancelled_and_force_closed(mobile_portal_app):
    app, _, factory = mobile_portal_app
    with TestClient(app) as client:
        _login(client, "mobile-admin")
        params = {"q": "SO-MOBILE-PORTAL", "category": "orders", "unfulfilled_only": True}
        for kind in ("delivered", "cancelled", "force_closed"):
            with factory() as db:
                order = db.scalar(select(Order).where(Order.order_number == "SO-MOBILE-PORTAL-001"))
                item = db.scalar(select(OrderItem).where(OrderItem.order_id == order.id))
                order.status = "cancelled" if kind == "cancelled" else "pending_production"
                item.delivered_quantity = item.quantity if kind == "delivered" else 0
                item.is_force_closed = kind == "force_closed"
                db.commit()
            response = client.get("/api/mobile/erp/search", params=params)
            assert response.status_code == 200, response.text
            assert response.json()["groups"][0]["total"] == 0
