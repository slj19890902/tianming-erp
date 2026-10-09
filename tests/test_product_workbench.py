"""The workbench is a scoped reader, not a new inventory ledger."""
from decimal import Decimal
from datetime import date

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api.product_workbench import router
from app.models.access_control import UserPermissionOverride
from app.models.product import Product
from app.models.mold_tool import MoldTool
from app.models.order import Order, OrderItem
from app.models.user import User
from app.models.warehouse_inventory import InventoryLot
from test_p1_21b_mobile_admin_product_search import _login, mobile_erp_app


def _app(mobile_erp_app):
    app, ids, factory = mobile_erp_app
    app.include_router(router, prefix="/api/product-workbench")
    return app, ids, factory


def test_zero_stock_inactive_exact_dimensions_and_no_price(mobile_erp_app):
    app, ids, factory = _app(mobile_erp_app)
    with factory() as db:
        product = db.get(Product, ids["product_two"])
        product.is_active = False
        product.report_length_mm = Decimal("120.25")
        product.report_width_mm = Decimal("80.50")
        db.commit()
    with TestClient(app) as client:
        _login(client, "mobile-admin")
        result = client.get("/api/product-workbench/search", params={"q": "MB002"})
        assert result.status_code == 200, result.text
        item = result.json()["items"][0]
        assert item["id"] == ids["product_two"]
        assert item["is_active"] is False
        assert item["inventory"]["finished"]["actual"] == 0
        assert item["drawings"]["status"] in ("none", "available")
        assert client.get("/api/product-workbench/search", params={
            "length": "120.25", "width": "80.50", "dimension_basis": "report"}).json()["total"] == 1
        detail = client.get(f"/api/product-workbench/products/{ids['product_two']}")
        assert detail.status_code == 200, detail.text
        assert detail.json()["actions"]["stock_only"] is True
        assert "unit_price" not in detail.text
        assert "cost_unit_price" not in detail.text


def test_scope_hides_search_detail_and_reverse(mobile_erp_app):
    app, ids, factory = _app(mobile_erp_app)
    with factory() as db:
        user = db.scalar(select(User).where(User.username == "mobile-scoped"))
        db.add(UserPermissionOverride(user_id=user.id, permission_code="products.view", is_allowed=True))
        db.commit()
    with TestClient(app) as client:
        _login(client, "mobile-scoped")
        result = client.get("/api/product-workbench/search", params={"q": "MOBILE"})
        assert result.status_code == 200, result.text
        assert ids["other_product"] not in {x["id"] for x in result.json()["items"]}
        assert client.get(f"/api/product-workbench/products/{ids['other_product']}").status_code == 404
        assert client.get("/api/product-workbench/reverse", params={
            "length": 420, "width": 310, "known_customer_id": 999999}).status_code == 403


def test_free_reverse_is_review_only_and_lot_dimensions_must_match(mobile_erp_app):
    app, ids, factory = _app(mobile_erp_app)
    with factory() as db:
        p = db.get(Product, ids["product"])
        p.report_length_mm, p.report_width_mm = 420, 310
        lot = db.get(InventoryLot, ids["finished_lot"])
        before = lot.quantity_available, lot.quantity_reserved
        db.commit()
    with TestClient(app) as client:
        _login(client, "mobile-admin")
        free = client.get("/api/product-workbench/reverse", params={"length": 420, "width": 310})
        assert free.status_code == 200, free.text
        match = next(x for x in free.json()["items"] if x["id"] == ids["product"])
        assert match["match_class"] == "needs_review" and match["deductible"] is False
        actual = client.get("/api/product-workbench/reverse", params={
            "length": 420, "width": 310, "lot_id": ids["finished_lot"]})
        assert actual.status_code == 422
    with factory() as db:
        lot = db.get(InventoryLot, ids["finished_lot"])
        assert before == (lot.quantity_available, lot.quantity_reserved)


def test_actual_lot_reverse_reuses_matcher_without_reserving(mobile_erp_app):
    app, ids, factory = _app(mobile_erp_app)
    with factory() as db:
        product = db.get(Product, ids["product"])
        product.report_length_mm, product.report_width_mm = 800, 600
        product.default_material_code = "K=A"
        product.flute_type, product.layer_count = "B", 3
        lot = db.scalar(select(InventoryLot).where(InventoryLot.lot_number == "SF-MOBILE-001"))
        lot_id = lot.id
        before = (lot.quantity_available, lot.quantity_reserved, lot.version)
        db.commit()
    with TestClient(app) as client:
        _login(client, "mobile-admin")
        response = client.get("/api/product-workbench/reverse", params={
            "length": 800, "width": 600, "material_code": "K=A", "flute_type": "B",
            "layer_count": 3, "processed_state": "raw", "lot_id": lot_id})
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["source"] == "actual_lot"
        assert payload["total"] >= 1
        assert all(item["deductible"] is False for item in payload["items"])
        assert all(item["customer_id"] == payload["registered_owner_customer_id"] for item in payload["items"])
    with factory() as db:
        lot = db.get(InventoryLot, lot_id)
        assert before == (lot.quantity_available, lot.quantity_reserved, lot.version)


def test_search_order_mold_and_detail_are_price_free(mobile_erp_app):
    app, ids, factory = _app(mobile_erp_app)
    with factory() as db:
        product = db.get(Product, ids["product"])
        mold = MoldTool(mold_code="MOLD-MOBILE-001", mold_name="测试刀模", rack_location="1F-M01")
        db.add(mold)
        db.flush()
        product.mold_tool_id = mold.id
        order = Order(order_number="SO-WORKBENCH-1", customer_id=product.customer_id,
                      customer_po="PO-WORKBENCH-1", order_date=date(2026, 10, 10),
                      status="pending_production")
        db.add(order)
        db.flush()
        db.add(OrderItem(order_id=order.id, product_id=product.id,
                         quantity=20, unit_price=Decimal("2.5"), subtotal=Decimal("50"),
                         snapshot_product_name=product.product_name,
                         snapshot_product_code=product.product_code,
                         requisition_status="未报料"))
        db.commit()
    with TestClient(app) as client:
        _login(client, "mobile-admin")
        for term in ("PO-WORKBENCH-1", "MOLD-MOBILE-001"):
            result = client.get("/api/product-workbench/search", params={"q": term})
            assert result.status_code == 200, result.text
            assert ids["product"] in {row["id"] for row in result.json()["items"]}
        detail = client.get(f"/api/product-workbench/products/{ids['product']}")
        assert detail.status_code == 200, detail.text
        body = detail.json()
        assert body["inventory"]["summary"]["finished"]["actual"] > 0
        assert body["inventory"]["items"][0]["location_id"] is not None
        assert body["orders"]["items"][0]["customer_po"] == "PO-WORKBENCH-1"
        assert "unit_price" not in detail.text and "cost_unit_price" not in detail.text
