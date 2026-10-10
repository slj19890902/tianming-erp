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
from app.models.product_bom import ProductBomComponent
from app.models.fixed_shelf import ShelfBinding, ShelfProfile
from app.models.warehouse_inventory import WarehouseLocation
from app.models.material import Material
from app.models.warehouse_goods import WarehouseGoodsProfile
from app.models.order import Order, OrderItem
from app.models.user import User
from app.models.warehouse_inventory import InventoryLot, SemiFinishedLotAllowedProduct
from test_p1_21b_mobile_admin_product_search import _login, mobile_erp_app
from test_bidirectional_sheet_cut import eligibility_db, prepare as prepare_cut
from tests.test_finished_goods_inventory_reservation import reservation_db


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


def test_near_dimensions_keep_axis_delta_zero_stock_and_exact_first(mobile_erp_app):
    app, ids, factory = _app(mobile_erp_app)
    with factory() as db:
        stocked = db.get(Product, ids["product"])
        zero = db.get(Product, ids["product_two"])
        stocked.report_length_mm, stocked.report_width_mm = Decimal("421"), Decimal("311")
        zero.report_length_mm, zero.report_width_mm = Decimal("420"), Decimal("310")
        stocked.default_material_code, stocked.flute_type = "K=A", "B"
        zero.default_material_code, zero.flute_type = "K=B", "E"
        db.commit()
    with TestClient(app) as client:
        _login(client, "mobile-admin")
        params = {"dimension_basis": "report", "length": 420, "width": 310,
                  "tolerance_mm": 2}
        body = client.get("/api/product-workbench/search", params=params).json()
        assert [item["id"] for item in body["items"]] == [ids["product_two"], ids["product"]]
        assert body["items"][0]["inventory"]["finished"]["actual"] == 0
        assert body["items"][0]["dimension_match"]["exact"] is True
        assert body["items"][1]["dimension_match"]["delta_mm"] == {
            "length": 1.0, "width": 1.0, "height": None}
        assert client.get("/api/product-workbench/search", params={
            **params, "tolerance_mm": 0}).json()["total"] == 1
        assert client.get("/api/product-workbench/search", params={
            **params, "tolerance_mm": 101}).status_code == 422
        filtered = client.get("/api/product-workbench/search", params={
            **params, "material_code": "k=a", "flute_type": "b"})
        assert filtered.status_code == 200, filtered.text
        assert [row["id"] for row in filtered.json()["items"]] == [ids["product"]]


def test_bom_child_mold_uses_edge_identity_and_scope_safe_location(mobile_erp_app):
    app, ids, factory = _app(mobile_erp_app)
    with factory() as db:
        parent, child = db.get(Product, ids["product"]), db.get(Product, ids["product_two"])
        parent_mold = MoldTool(mold_code="PARENT-MOLD", mold_name="父模具", rack_location="1F-M01")
        child_mold = MoldTool(mold_code="CHILD-MOLD", mold_name="子模具", rack_location="3F-M-R01-L1")
        grandchild_mold = MoldTool(mold_code="GRANDCHILD-MOLD", mold_name="孙件模具", rack_location="3F-M-R02-L1")
        grandchild = Product(customer_id=parent.customer_id, product_code="MOBILE-BOM-GRANDCHILD",
            customer_material_code="BOM-GRANDCHILD", product_name="孙件", sale_unit_price=Decimal("1"))
        db.add_all([parent_mold, child_mold, grandchild_mold, grandchild])
        db.flush()
        parent.mold_tool_id = parent_mold.id
        db.add(ProductBomComponent(parent_product_id=parent.id, component_product_id=child.id,
            quantity_per_set=1, display_order=0, internal_component_code="CHILD",
            is_die_cut=True, mold_tool_id=child_mold.id))
        db.add(ProductBomComponent(parent_product_id=child.id, component_product_id=grandchild.id,
            quantity_per_set=2, display_order=0, internal_component_code="GRANDCHILD",
            is_die_cut=True, mold_tool_id=grandchild_mold.id))
        # Corrupt legacy graph must terminate safely without recursing forever.
        db.add(ProductBomComponent(parent_product_id=grandchild.id, component_product_id=parent.id,
            quantity_per_set=1, display_order=0, internal_component_code="CYCLE"))
        scoped = db.scalar(select(User).where(User.username == "mobile-scoped"))
        db.add_all([
            UserPermissionOverride(user_id=scoped.id, permission_code="products.view", is_allowed=True),
            UserPermissionOverride(user_id=scoped.id, permission_code="warehouse.view", is_allowed=False),
        ])
        db.commit()
    with TestClient(app) as client:
        _login(client, "mobile-admin")
        detail = client.get(f"/api/product-workbench/products/{ids['product']}").json()
        assert detail["production"]["molds"][0]["id"] == parent_mold.id
        row = detail["production"]["bom"][0]
        assert row["path_product_ids"] == [ids["product"], ids["product_two"]]
        assert row["molds"][0]["id"] == child_mold.id
        assert row["molds"][0]["location"] == "3F-M-R01-L1"
        assert row["molds"][0]["location_label"]
        nested = detail["production"]["bom"][1]
        assert nested["product_id"] == grandchild.id and nested["depth"] == 2
        assert nested["path_product_ids"] == [ids["product"], ids["product_two"], grandchild.id]
        assert nested["molds"][0]["id"] == grandchild_mold.id
        assert len(detail["production"]["bom"]) == 3
        assert detail["production"]["bom"][2]["cycle_detected"] is True
        assert "unit_price" not in str(detail)
        _login(client, "mobile-scoped")
        restricted = client.get(f"/api/product-workbench/products/{ids['product']}").json()
        hidden = restricted["production"]["bom"][0]["molds"][0]
        assert hidden["location_visibility"] == "hidden_by_permission"
        assert hidden["location"] is None and hidden["short_label"] is None
        assert hidden["map_url"] is None


def test_location_search_and_map_separate_default_from_actual_goods(mobile_erp_app):
    app, ids, factory = _app(mobile_erp_app)
    with factory() as db:
        location = db.scalar(select(WarehouseLocation).where(WarehouseLocation.location_code == "C1-L01"))
        product = db.get(Product, ids["product_two"])
        db.add(ShelfProfile(product_id=product.id, version=1))
        db.flush()
        db.add(ShelfBinding(location_id=location.id, product_id=product.id, priority=0))
        db.commit()
    with TestClient(app) as client:
        _login(client, "mobile-admin")
        search = client.get("/api/mobile/erp/warehouse/map/locations-search", params={"q": "C1-L01"})
        assert search.status_code == 200, search.text
        location_row = search.json()["items"][0]
        assert location_row["location_id"] == location.id
        assert location_row["default_binding"]["product_id"] == ids["product_two"]
        assert "quantity" not in location_row["default_binding"]
        map_response = client.get("/api/mobile/erp/warehouse/map/floors/3F", params={"area_code": "C1"})
        assert map_response.status_code == 200, map_response.text
        map_row = next(x for x in map_response.json()["locations"] if x["location_id"] == location.id)
        assert map_row["default_binding"]["product_id"] == ids["product_two"]
        assert map_row["goods"] and map_row["goods"][0]["product_id"] == ids["product"]


def test_scoped_location_reader_cannot_see_other_customer_default(mobile_erp_app):
    app, ids, factory = _app(mobile_erp_app)
    with factory() as db:
        location = db.scalar(select(WarehouseLocation).where(WarehouseLocation.location_code == "C1-L01"))
        product = db.get(Product, ids["other_product"])
        db.add(ShelfProfile(product_id=product.id, version=1))
        db.flush()
        db.add(ShelfBinding(location_id=location.id, product_id=product.id, priority=0))
        db.commit()
    with TestClient(app) as client:
        _login(client, "mobile-scoped")
        response = client.get("/api/mobile/erp/warehouse/map/locations-search", params={"q": "C1-L01"})
        assert response.status_code == 200, response.text
        row = response.json()["items"][0]
        assert row["default_binding"] is None
        assert row["default_binding_visibility"] == "hidden_by_permission"
        assert "OTHER-MOBILE" not in response.text


def test_shared_finished_member_search_validates_confirmed_lot(reservation_db):
    from test_shared_finished_stock import setup_pair, activate
    from app.services.product_workbench import find_products
    from app.models.shared_finished_stock import SharedFinishedMember, SharedFinishedGroup
    db, data = reservation_db
    zero = Product(customer_id=data["other_customer"].id, product_code="MATCH-ZERO",
        customer_material_code="ZERO", product_name="MATCH zero", sale_unit_price=Decimal("1"))
    db.add(zero)
    db.flush()
    target, _, lot = setup_pair(db, data, same_code=False)
    target.product_name = "MATCH shared"
    db.commit()
    activate(db, data, target, lot)
    rows, total = find_products(db, None, q="MATCH", customer_id=None,
        dimension_basis="finished", length=None, width=None, height=None,
        page=1, page_size=20, allow_order_search=False, allow_inventory_rank=True)
    assert total == 2 and [row.id for row in rows] == [target.id, zero.id]
    member = db.get(SharedFinishedMember, target.id)
    db.get(SharedFinishedGroup, member.group_id).enabled = False
    db.commit()
    rows, _ = find_products(db, None, q="MATCH", customer_id=None,
        dimension_basis="finished", length=None, width=None, height=None,
        page=1, page_size=20, allow_order_search=False, allow_inventory_rank=True)
    assert [row.id for row in rows] == [zero.id, target.id]
    assert db.get(InventoryLot, lot.id).quantity_available == 50


def test_source_lot_without_allowed_binding_can_rank_product(mobile_erp_app):
    from app.models.stock_replenishment import StockReplenishmentOrder, StockReplenishmentOrderItem
    from app.services.product_workbench import find_products
    app, ids, factory = _app(mobile_erp_app)
    with factory() as db:
        customer_id = db.get(Product, ids["product_two"]).customer_id
        zero = Product(customer_id=customer_id, product_code="FIELDMATCH-ZERO",
            customer_material_code="FIELDZERO", product_name="零库存候选", sale_unit_price=Decimal("1"))
        target = Product(customer_id=customer_id, product_code="FIELDMATCH-SOURCE",
            customer_material_code="FIELDSOURCE", product_name="有来源片料候选", sale_unit_price=Decimal("1"))
        db.add_all([zero, target])
        db.flush()
        source_lot = db.scalar(select(InventoryLot).where(InventoryLot.lot_number == "SF-MOBILE-UNBOUND"))
        assert source_lot is not None
        order = StockReplenishmentOrder(order_number="SOURCE-SORT-1",
            source_type="manual_history", status="stocked", customer_id=target.customer_id)
        db.add(order)
        db.flush()
        db.add(StockReplenishmentOrderItem(replenishment_order_id=order.id,
            target_inventory_type="semi_finished", product_id=target.id,
            reference_product_id=target.id, customer_id=target.customer_id,
            product_name_snapshot=target.product_name, quantity=5, stocked_quantity=5,
            inventory_lot_id=source_lot.id))
        db.commit()
        from app.services.product_activity import source_lots
        assert source_lot.id in {lot.id for lot in source_lots(db, target)}
        rows, total = find_products(db, {target.customer_id}, q="FIELDMATCH", customer_id=None,
            dimension_basis="finished", length=None, width=None, height=None,
            page=1, page_size=20, allow_order_search=False, allow_inventory_rank=True)
        assert total == 2 and [row.id for row in rows] == [target.id, zero.id]


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
        assert all(item["lot_unit"] == "张" for item in payload["items"])
        assert all(item["customer_id"] == payload["registered_owner_customer_id"] for item in payload["items"])
        net = client.get("/api/product-workbench/reverse", params={
            "length": 800, "width": 600, "processed_state": "net_raw", "lot_id": lot_id})
        assert net.status_code == 200, net.text
        assert all(item["match_class"] == "review" and item["cut_plan"] is None
                   for item in net.json()["items"])
        assert all(item["source_processed_state"] == "raw" for item in net.json()["items"])
    with factory() as db:
        lot = db.get(InventoryLot, lot_id)
        assert before == (lot.quantity_available, lot.quantity_reserved, lot.version)


def test_registered_net_sheet_is_identified_without_cut_profile(mobile_erp_app):
    app, ids, factory = _app(mobile_erp_app)
    with factory() as db:
        product = db.get(Product, ids["product"])
        product.length_mm, product.width_mm = 800, 600
        product.report_length_mm, product.report_width_mm = 800, 600
        product.default_material_code = "K=A"
        product.flute_type, product.layer_count = "B", 3
        lot = db.scalar(select(InventoryLot).where(InventoryLot.lot_number == "SF-MOBILE-001"))
        lot.semi_finished_detail.sheet_type = "net_sheet"
        db.commit()
        lot_id = lot.id
    with TestClient(app) as client:
        _login(client, "mobile-admin")
        result = client.get("/api/product-workbench/reverse", params={
            "length": 800, "width": 600, "processed_state": "net_raw", "lot_id": lot_id})
        assert result.status_code == 200, result.text
        assert result.json()["items"]
        assert all(item["source_processed_state"] == "net_raw"
                   and item["match_class"] == "review" and item["cut_plan"] is None
                   for item in result.json()["items"])


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


def test_search_does_not_reveal_order_reference_without_orders_view(mobile_erp_app):
    app, ids, factory = _app(mobile_erp_app)
    with factory() as db:
        user = db.scalar(select(User).where(User.username == "mobile-scoped"))
        db.add_all([
            UserPermissionOverride(user_id=user.id, permission_code="products.view", is_allowed=True),
            UserPermissionOverride(user_id=user.id, permission_code="orders.view", is_allowed=False),
        ])
        product = db.get(Product, ids["product"])
        order = Order(order_number="SO-PRIVATE-REF-101", customer_id=product.customer_id,
                      customer_po="PO-PRIVATE-REF-101", order_date=date(2026, 10, 10),
                      status="pending_production")
        db.add(order)
        db.flush()
        db.add(OrderItem(order_id=order.id, product_id=product.id,
                         quantity=1, unit_price=Decimal("1"), subtotal=Decimal("1"),
                         snapshot_product_name=product.product_name,
                         snapshot_product_code=product.product_code))
        db.commit()
    with TestClient(app) as client:
        _login(client, "mobile-scoped")
        assert client.get("/api/product-workbench/search", params={
            "q": "MOBILE-BOX-001"}).json()["total"] == 1
        for private_term in ("SO-PRIVATE-REF-101", "PO-PRIVATE-REF-101"):
            response = client.get("/api/product-workbench/search", params={"q": private_term})
            assert response.status_code == 200, response.text
            assert response.json()["total"] == 0
        _login(client, "mobile-admin")
        assert client.get("/api/product-workbench/search", params={
            "q": "PO-PRIVATE-REF-101"}).json()["total"] == 1


def test_report_dimension_token_rejects_third_axis(mobile_erp_app):
    app, ids, factory = _app(mobile_erp_app)
    with factory() as db:
        product = db.get(Product, ids["product_two"])
        product.report_length_mm = Decimal("120.25")
        product.report_width_mm = Decimal("80.50")
        db.commit()
    with TestClient(app) as client:
        _login(client, "mobile-admin")
        two = client.get("/api/product-workbench/search", params={
            "q": "120.25x80.50", "dimension_basis": "report"})
        three = client.get("/api/product-workbench/search", params={
            "q": "120.25x80.50x5", "dimension_basis": "report"})
        assert two.status_code == three.status_code == 200
        assert ids["product_two"] in {item["id"] for item in two.json()["items"]}
        assert three.json()["total"] == 0


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
        assert found.json()["items"]
        assert all(item["customer_id"] == product.customer_id for item in found.json()["items"])
        assert client.get(f"/api/product-workbench/products/{ids['product']}").status_code == 404


def test_processed_output_is_counted_as_pieces_not_sheets(mobile_erp_app):
    app, ids, factory = _app(mobile_erp_app)
    with factory() as db:
        lot = db.scalar(select(InventoryLot).where(InventoryLot.lot_number == "SF-MOBILE-001"))
        db.add(WarehouseGoodsProfile(lot_id=lot.id, data_json='{"output_piece":true,"processing":"cut"}'))
        product = db.get(Product, ids["product"])
        product.report_length_mm, product.report_width_mm = 800, 600
        product.default_material_code, product.flute_type, product.layer_count = "K=A", "B", 3
        before = lot.quantity_available, lot.quantity_reserved, lot.version
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
        assert row["unit"] == "片"
        assert all(x["unit"] == "片" for x in stock["groups"]["processed_component"]["positions"])
        assert row["reverse_source"]["processed_state"] == "output_piece"
        reverse = client.get("/api/product-workbench/reverse", params=row["reverse_source"])
        assert reverse.status_code == 200, reverse.text
        assert reverse.json()["items"] and all(x["lot_unit"] == "片" for x in reverse.json()["items"])
    with factory() as db:
        lot = db.scalar(select(InventoryLot).where(InventoryLot.lot_number == "SF-MOBILE-001"))
        assert before == (lot.quantity_available, lot.quantity_reserved, lot.version)


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


def test_database_ids_and_entire_pagination_offset_are_bounded(mobile_erp_app):
    app, ids, factory = _app(mobile_erp_app)
    maximum = (1 << 63) - 1
    with factory() as db:
        before = list(db.execute(select(InventoryLot.id, InventoryLot.quantity_available,
                                        InventoryLot.quantity_reserved, InventoryLot.version)))
    with TestClient(app, raise_server_exceptions=False) as client:
        _login(client, "mobile-admin")
        for url, params in (
            (f"/api/product-workbench/products/{maximum + 1}", {}),
            ("/api/product-workbench/search", {"q": "MOBILE", "customer_id": maximum + 1}),
            ("/api/product-workbench/search", {"q": "MOBILE", "page": maximum}),
            ("/api/product-workbench/search", {"q": "MOBILE", "page_size": 50, "page": maximum // 50 + 2}),
            ("/api/product-workbench/reverse", {"length": 800, "width": 600, "lot_id": maximum + 1}),
            ("/api/product-workbench/reverse", {"length": 800, "width": 600, "known_customer_id": maximum + 1}),
            ("/api/product-workbench/reverse", {"length": 800, "width": 600, "page": maximum}),
        ):
            response = client.get(url, params=params)
            assert response.status_code == 422, (url, params, response.text)
            assert isinstance(response.json()["detail"], str)
        assert client.get(f"/api/product-workbench/products/{maximum}").status_code == 404
        assert client.get("/api/product-workbench/reverse", params={"length": 800, "width": 600, "lot_id": maximum}).status_code == 404
        # Largest valid OFFSET and absent optional fields are still legal reads.
        assert client.get("/api/product-workbench/search", params={"q": "MOBILE", "page_size": 1, "page": maximum}).status_code == 200
        normal = client.get(f"/api/product-workbench/products/{ids['product_two']}")
        assert normal.status_code == 200 and normal.json()["production"]["cutting"] is None
    with factory() as db:
        assert before == list(db.execute(select(InventoryLot.id, InventoryLot.quantity_available,
                                               InventoryLot.quantity_reserved, InventoryLot.version)))
