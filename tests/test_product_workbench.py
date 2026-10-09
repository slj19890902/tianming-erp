"""The workbench is a scoped reader, not a new inventory ledger."""
from decimal import Decimal
from datetime import date, datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api.product_workbench import router
from app.api.deps import get_db, get_current_user
from app.models.access_control import UserPermissionOverride, UserCustomerScope
from app.models.product import Product
from app.models.mold_tool import MoldTool
from app.models.material import Material
from app.models.warehouse_goods import WarehouseGoodsProfile
from app.models.order import Order, OrderItem
from app.models.user import User
from app.models.warehouse_inventory import InventoryLot, SemiFinishedLotAllowedProduct
from test_p1_21b_mobile_admin_product_search import _login, mobile_erp_app
from test_bidirectional_sheet_cut import eligibility_db, prepare as prepare_cut


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
        assert item["drawings"]["status"] == "unavailable_inactive"
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
        assert match["match_class"] == "review" and match["deductible"] is False
        actual = client.get("/api/product-workbench/reverse", params={
            "length": 420, "width": 310, "lot_id": ids["finished_lot"]})
        assert actual.status_code == 404
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
        assert all(item["match_class"] in {"confirmed", "cut_candidate", "review"} for item in payload["items"])
        assert all(item["lot_available_quantity"] == 8 for item in payload["items"])
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
        assert body["orders"]["items"][0]["product_id"] == ids["product"]
        assert "unit_price" not in detail.text and "cost_unit_price" not in detail.text


def test_real_cutting_projection_and_free_measurement_review(mobile_erp_app):
    app, ids, factory = _app(mobile_erp_app)
    with factory() as db:
        product = db.get(Product, ids["product"])
        material = Material(code="K=A", layer_count=3, flute_type="B", quote_price=Decimal("999"))
        db.add(material)
        db.flush()
        product.material_id = material.id
        product.default_material_code = None
        product.flute_type = None
        product.layer_count = None
        product.report_length_mm = 318
        product.report_width_mm = 540
        product.sheet_cutting_settings = {"schema_version": 2, "whole": {
            "length_parts": 3, "width_parts": 1, "mold_count": 2, "is_die_cut": True}}
        product.production_notes = "生产备注：采购价999，仅内部核对"
        product.production_process = "无需结合，模切"
        db.commit()
    with TestClient(app) as client:
        _login(client, "mobile-admin")
        detail = client.get(f"/api/product-workbench/products/{ids['product']}")
        assert detail.status_code == 200, detail.text
        production = detail.json()["production"]
        assert production["cutting"]["supplier_length_mm"] == "954"
        assert production["cutting"]["supplier_width_mm"] == "540"
        assert production["mold_count"] == 2
        assert any("954×540" in x["detail"] for x in production["process_steps"])
        assert not any(x["label"] == "无需结合" for x in production["process_steps"])
        assert production["production_notes"] is None
        assert "999" not in detail.text
        free = client.get("/api/product-workbench/reverse", params={
            "length": 954, "width": 540, "material_code": "K=A", "flute_type": "B", "layer_count": 3})
        assert free.status_code == 200, free.text
        hit = next(x for x in free.json()["items"] if x["id"] == ids["product"])
        assert hit["match_class"] == "cut_candidate" and hit["deductible"] is False
        assert hit["cut_plan"] is None
        processed = client.get("/api/product-workbench/reverse", params={
            "length": 954, "width": 540, "material_code": "K=A", "processed_state": "die_cut"})
        assert processed.status_code == 200
        assert ids["product"] not in {x["id"] for x in processed.json()["items"]}


def test_products_reader_without_warehouse_grant_hides_stock(mobile_erp_app):
    app, ids, factory = _app(mobile_erp_app)
    with factory() as db:
        user = db.scalar(select(User).where(User.username == "mobile-scoped"))
        db.add_all([
            UserPermissionOverride(user_id=user.id, permission_code="products.view", is_allowed=True),
            UserPermissionOverride(user_id=user.id, permission_code="warehouse.view", is_allowed=False),
        ])
        db.commit()
    with TestClient(app) as client:
        _login(client, "mobile-scoped")
        search = client.get("/api/product-workbench/search", params={"q": "MB001"})
        assert search.status_code == 200, search.text
        assert search.json()["items"][0]["inventory"] == {"visibility": "hidden_by_permission"}
        detail = client.get(f"/api/product-workbench/products/{ids['product']}")
        assert detail.status_code == 200, detail.text
        assert detail.json()["inventory"]["visibility"] == "hidden_by_permission"


def test_actual_cut_plan_is_exposed_as_reviewable_not_reserved(eligibility_db):
    db, data = eligibility_db
    product, lot, _profile, _facts, _item, _requirement = prepare_cut(db, data)
    app = FastAPI()
    app.include_router(router, prefix="/api/product-workbench")
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: data["admin"]
    before = (lot.quantity_available, lot.quantity_reserved, lot.version)
    with TestClient(app) as client:
        response = client.get("/api/product-workbench/reverse", params={
            "length": 1120, "width": 440, "lot_id": lot.id, "processed_state": "raw"})
        assert response.status_code == 200, response.text
        row = next(x for x in response.json()["items"] if x["product_id"] == product.id)
        assert row["match_class"] == "cut_candidate"
        assert row["cut_plan"]["yield_factor"] == 2
        assert row["cut_plan"]["rotated"] is False
        assert row["deductible"] is False
    db.refresh(lot)
    assert before == (lot.quantity_available, lot.quantity_reserved, lot.version)


def test_explicit_cross_customer_semi_binding_is_visible_once_without_owner_code(mobile_erp_app):
    app, ids, factory = _app(mobile_erp_app)
    with factory() as db:
        lot = db.scalar(select(InventoryLot).where(InventoryLot.lot_number == "SF-MOBILE-001"))
        product = db.get(Product, ids["other_product"])
        product.report_length_mm, product.report_width_mm = 800, 600
        product.flute_type, product.layer_count = "B", 3
        product.default_material_code = "K=A"
        scoped = db.scalar(select(User).where(User.username == "mobile-scoped"))
        scope_row = db.scalar(select(UserCustomerScope).where(UserCustomerScope.user_id == scoped.id))
        scope_row.customer_id = db.get(Product, ids["other_product"]).customer_id
        db.add(UserPermissionOverride(user_id=scoped.id, permission_code="warehouse.view", is_allowed=True))
        db.add(SemiFinishedLotAllowedProduct(inventory_lot_id=lot.id,
                                             product_id=ids["other_product"],
                                             confirmed_at=datetime(2026, 10, 10)))
        db.commit()
    with TestClient(app) as client:
        _login(client, "mobile-scoped")
        response = client.get(f"/api/product-workbench/products/{ids['other_product']}")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["inventory"]["summary"]["semi_finished"]["actual"] == 10
        rows = [x for x in body["inventory"]["groups"]["semi_finished"]["positions"]
                if x["lot_id"] == lot.id]
        assert len(rows) == 1 and rows[0]["shared_confirmed"] is True
        assert "lot_number" not in rows[0]
        assert body["inventory"]["items"][0]["location_id"] is not None
        reverse = rows[0]["reverse_source"]
        assert reverse["lot_id"] == lot.id and reverse["processed_state"] == "raw"
        found = client.get("/api/product-workbench/reverse", params=reverse)
        assert found.status_code == 200, found.text
        assert found.json()["registered_owner_customer_id"] is None
        assert all(item["customer_id"] == product.customer_id for item in found.json()["items"])
        assert client.get(f"/api/product-workbench/products/{ids['product']}").status_code == 404


def test_processed_output_is_counted_as_pieces_not_sheets(mobile_erp_app):
    app, ids, factory = _app(mobile_erp_app)
    with factory() as db:
        lot = db.scalar(select(InventoryLot).where(InventoryLot.lot_number == "SF-MOBILE-001"))
        db.add(WarehouseGoodsProfile(lot_id=lot.id, data_json='{"output_piece":true,"processing":"cut"}'))
        db.commit()
    with TestClient(app) as client:
        _login(client, "mobile-admin")
        response = client.get(f"/api/product-workbench/products/{ids['product']}")
        assert response.status_code == 200, response.text
        stock = response.json()["inventory"]
        assert stock["summary"]["processed_component"] == {
            "actual": 10, "available": 8, "reserved": 2, "unit": "片"}
        assert stock["summary"]["semi_finished"]["actual"] == 0
        row = next(x for x in stock["items"] if x["inventory_type"] == "processed_component")
        assert row["reverse_source"]["processed_state"] == "output_piece"


def test_report_placeholder_blocks_draft_without_falsely_claiming_stock_only(mobile_erp_app):
    app, ids, factory = _app(mobile_erp_app)
    with factory() as db:
        product = db.get(Product, ids["product"])
        product.report_length_mm = product.report_width_mm = 100
        product.default_material_code = "K=A"
        product.sale_unit_price = Decimal("1")
        db.commit()
    with TestClient(app) as client:
        _login(client, "mobile-admin")
        response = client.get(f"/api/product-workbench/products/{ids['product']}")
        assert response.status_code == 200, response.text
        actions = response.json()["actions"]
        assert actions["can_requisition"] is False
        assert actions["stock_only"] is False
        assert "占位" in actions["action_reason"]
