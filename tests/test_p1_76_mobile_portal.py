from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
import json
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.test_p1_21b_mobile_admin_product_search import (
    _login,
    mobile_erp_app,
)


ROOT = Path(__file__).resolve().parents[1]
MAIN_SOURCE = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
MOBILE_HTML = (ROOT / "static" / "mobile_erp.html").read_text(encoding="utf-8")


@pytest.fixture()
def mobile_portal_app(mobile_erp_app):
    from app.models.customer import Customer
    from app.models.mold_tool import MoldTool
    from app.models.order import Order, OrderItem
    from app.models.product import Product
    from app.models.production import ProductionTask

    app, ids, factory = mobile_erp_app
    with factory() as db:
        product = db.get(Product, ids["product"])
        other_product = db.get(Product, ids["other_product"])
        assert product is not None and other_product is not None

        mold = MoldTool(
            mold_code="MOLD-MOBILE-001",
            mold_name="匿名外箱刀模",
            rack_location="1F-M01",
        )
        other_mold = MoldTool(
            mold_code="MOLD-OTHER-MOBILE",
            mold_name="其他客户刀模",
            rack_location="1F-M02",
        )
        db.add_all([mold, other_mold])
        db.flush()
        product.mold_tool_id = mold.id
        other_product.mold_tool_id = other_mold.id

        order = Order(
            order_number="SO-MOBILE-PORTAL-001",
            customer_id=product.customer_id,
            customer_po="PO-MOBILE-001",
            order_date=date(2026, 8, 18),
            delivery_date=date(2026, 8, 20),
            status="pending_production",
        )
        other_order = Order(
            order_number="SO-OTHER-MOBILE-001",
            customer_id=other_product.customer_id,
            customer_po="PO-OTHER-MOBILE",
            order_date=date(2026, 8, 18),
            status="pending_production",
        )
        db.add_all([order, other_order])
        db.flush()

        item = OrderItem(
            order_id=order.id,
            product_id=product.id,
            item_order_number="SO-MOBILE-PORTAL-001-01",
            quantity=20,
            unit_price=Decimal("2.5"),
            subtotal=Decimal("50"),
            material_status="received",
            snapshot_product_name=product.product_name,
            snapshot_product_code=product.product_code,
            snapshot_spec="420×310×260mm",
            snapshot_material="K=A",
            requisition_status="已报料",
        )
        other_item = OrderItem(
            order_id=other_order.id,
            product_id=other_product.id,
            item_order_number="SO-OTHER-MOBILE-001-01",
            quantity=10,
            unit_price=Decimal("9.9"),
            subtotal=Decimal("99"),
            snapshot_product_name=other_product.product_name,
            snapshot_product_code=other_product.product_code,
            requisition_status="未报料",
        )
        db.add_all([item, other_item])
        db.flush()
        db.add_all(
            [
                ProductionTask(
                    order_item_id=item.id,
                    status="pending",
                    planned_quantity=20,
                    ordered_quantity_snapshot=20,
                    material_received_quantity=20,
                    material_input_quantity=20,
                ),
                ProductionTask(
                    order_item_id=other_item.id,
                    status="pending",
                    planned_quantity=10,
                    ordered_quantity_snapshot=10,
                    material_received_quantity=10,
                    material_input_quantity=10,
                ),
            ]
        )
        db.commit()
    return app, ids, factory


def _group(payload: dict, group_id: str) -> dict:
    return next(group for group in payload["groups"] if group["id"] == group_id)


def _assert_no_financial_projection(value) -> None:
    forbidden = {"unit_price", "subtotal", "amount", "cost", "margin", "supplier"}
    if isinstance(value, dict):
        assert not (forbidden & set(value))
        for child in value.values():
            _assert_no_financial_projection(child)
    elif isinstance(value, list):
        for child in value:
            _assert_no_financial_projection(child)


def test_qr_map_identities_are_permissioned_read_only_and_use_current_location(
    mobile_portal_app,
) -> None:
    from app.models.warehouse_inventory import InventoryLot, WarehouseLocation

    app, ids, factory = mobile_portal_app
    with factory() as db:
        location = db.get(WarehouseLocation, ids["mapped_location"])
        location.map_rack_id = "rack-mobile-A"
        location.rack_display_name = "A架"
        db.commit()
        before = (
            db.get(InventoryLot, ids["finished_lot"]).warehouse_location_id,
            db.get(InventoryLot, ids["finished_lot"]).quantity_available,
            db.get(InventoryLot, ids["finished_lot"]).quantity_reserved,
        )

    with TestClient(app) as client:
        assert client.get(
            f'/api/mobile/erp/warehouse/map/lots/{ids["finished_lot"]}'
        ).status_code == 401
        _login(client, "mobile-admin")
        lot = client.get(
            f'/api/mobile/erp/warehouse/map/lots/{ids["finished_lot"]}'
        )
        assert lot.status_code == 200, lot.text
        assert lot.json() == {
            "lot_id": ids["finished_lot"],
            "location_id": ids["mapped_location"],
            "floor_code": "3F",
            "area_code": "C1",
            "rack_id": "rack-mobile-A",
        }
        rack = client.get(
            "/api/mobile/erp/warehouse/map/racks/3F/rack-mobile-A"
        )
        assert rack.status_code == 200, rack.text
        assert rack.json()["location_id"] == ids["mapped_location"]
        assert rack.json()["rack_display_name"] == "A架"
        assert rack.headers["cache-control"] == "private, no-store"

    with TestClient(app) as client:
        _login(client, "mobile-picker")
        assert client.get(
            f'/api/mobile/erp/warehouse/map/lots/{ids["finished_lot"]}'
        ).status_code == 403

    with factory() as db:
        after = (
            db.get(InventoryLot, ids["finished_lot"]).warehouse_location_id,
            db.get(InventoryLot, ids["finished_lot"]).quantity_available,
            db.get(InventoryLot, ids["finished_lot"]).quantity_reserved,
        )
        assert after == before
def test_shell_and_unified_search_are_permission_derived_and_scope_safe(
    mobile_portal_app,
) -> None:
    app, _ids, _factory = mobile_portal_app
    with TestClient(app) as client:
        _login(client, "mobile-admin")
        shell = client.get("/api/mobile/erp/shell")
        assert shell.status_code == 200, shell.text
        shell_payload = shell.json()
        assert shell_payload["search_categories"] == [
            "orders",
            "materials",
            "molds",
            "production",
            "inventory",
        ]
        assert any(entry["id"] == "lookup" for entry in shell_payload["entries"])

        result = client.get(
            "/api/mobile/erp/search",
            params={"q": "MOBILE", "category": "all"},
        )
        assert result.status_code == 200, result.text
        payload = result.json()
        assert result.headers["cache-control"] == "private, no-store"
        assert payload["read_only"] is True
        assert payload["allowed_categories"] == shell_payload["search_categories"]
        assert _group(payload, "orders")["total"] == 2
        assert _group(payload, "inventory")["total"] == 3
        assert _group(payload, "molds")["total"] == 2
        assert _group(payload, "production")["total"] == 2
        _assert_no_financial_projection(payload)

    with TestClient(app) as client:
        _login(client, "mobile-scoped")
        scoped = client.get(
            "/api/mobile/erp/search",
            params={"q": "OTHER-MOBILE", "category": "all"},
        )
        assert scoped.status_code == 200, scoped.text
        for group in scoped.json()["groups"]:
            assert group["total"] == 0
            assert group["items"] == []

    with TestClient(app) as client:
        _login(client, "mobile-picker")
        shell = client.get("/api/mobile/erp/shell")
        assert shell.status_code == 200, shell.text
        assert shell.json()["search_categories"] == []
        assert not any(entry["id"] == "lookup" for entry in shell.json()["entries"])
        denied = client.get(
            "/api/mobile/erp/search",
            params={"q": "MOBILE", "category": "orders"},
        )
        assert denied.status_code == 403
        denied_all = client.get(
            "/api/mobile/erp/search",
            params={"q": "MOBILE", "category": "all"},
        )
        assert denied_all.status_code == 403


def test_unified_search_specific_group_is_paginated_and_does_not_leak_direct_ids(
    mobile_portal_app,
) -> None:
    app, _ids, _factory = mobile_portal_app
    with TestClient(app) as client:
        _login(client, "mobile-scoped")
        page = client.get(
            "/api/mobile/erp/search",
            params={
                "q": "MOBILE",
                "category": "orders",
                "page": 99,
                "page_size": 1,
            },
        )
        assert page.status_code == 200, page.text
        group = _group(page.json(), "orders")
        assert group["total"] == 1
        assert group["page"] == 1
        assert group["page_size"] == 1
        assert len(group["items"]) == 1
        assert group["items"][0]["order_number"] == "SO-MOBILE-PORTAL-001"

        denied_other = client.get(
            "/api/mobile/erp/search",
            params={"q": "SO-OTHER-MOBILE-001", "category": "orders"},
        )
        assert denied_other.status_code == 200
        assert _group(denied_other.json(), "orders")["total"] == 0


def test_unified_search_is_read_only(mobile_portal_app) -> None:
    from app.models.audit import OperationLog
    from app.models.order import Order
    from app.models.production import ProductionTask
    from app.models.warehouse_inventory import InventoryLot

    app, _ids, factory = mobile_portal_app
    with TestClient(app) as client:
        _login(client, "mobile-admin")
        with factory() as db:
            before = {
                "orders": len(db.scalars(select(Order.id)).all()),
                "tasks": len(db.scalars(select(ProductionTask.id)).all()),
                "lots": len(db.scalars(select(InventoryLot.id)).all()),
                "audits": len(db.scalars(select(OperationLog.id)).all()),
            }
        response = client.get(
            "/api/mobile/erp/search",
            params={"q": "MOBILE", "category": "all"},
        )
        assert response.status_code == 200, response.text
    with factory() as db:
        after = {
            "orders": len(db.scalars(select(Order.id)).all()),
            "tasks": len(db.scalars(select(ProductionTask.id)).all()),
            "lots": len(db.scalars(select(InventoryLot.id)).all()),
            "audits": len(db.scalars(select(OperationLog.id)).all()),
        }
    assert after == before


def test_mobile_portal_has_canonical_url_and_independent_latest_wins_ui() -> None:
    assert 'route.path == "/mobile/"' in MAIN_SOURCE
    assert '"/mobile/erp.html"' in MAIN_SOURCE
    assert 'id="lookupPage"' in MOBILE_HTML
    assert 'data-page="lookup"' in MOBILE_HTML
    assert '"/api/mobile/erp/search"' in MOBILE_HTML
    assert "lookupController" in MOBILE_HTML
    assert "lookupGeneration" in MOBILE_HTML
    assert "generation !== state.lookupGeneration" in MOBILE_HTML
    assert 'redirect: "/mobile/"' in MOBILE_HTML
    assert "sessionStorage" in MOBILE_HTML
    assert "clearMobilePortalState" in MOBILE_HTML
    assert 'id="drawingZoomIn"' in MOBILE_HTML
    assert 'id="drawingZoomOut"' in MOBILE_HTML
    assert 'id="drawingRotate"' in MOBILE_HTML
    assert "applyDrawingTransform" in MOBILE_HTML
    assert "crease_values_mm" in MOBILE_HTML
    assert "process_tags" in MOBILE_HTML
    assert "static/index.html" not in MOBILE_HTML
    assert "min-height: 44px" in MOBILE_HTML
    assert "overflow-x: hidden" in MOBILE_HTML


def test_mobile_portal_canonical_route_is_get_only() -> None:
    from app.main import create_app

    app = create_app()
    canonical = [route for route in app.routes if route.path == "/mobile/"]
    legacy = [route for route in app.routes if route.path == "/mobile/erp.html"]
    assert len(canonical) == 1
    assert len(legacy) == 1
    assert canonical[0].methods == {"GET"}
    assert legacy[0].methods == {"GET"}


def test_mobile_portal_latest_response_wins_in_real_javascript() -> None:
    node = shutil.which("node")
    assert node, "Node.js is required for the mobile latest-response test"
    start = MOBILE_HTML.index("      async function runUnifiedSearch(")
    end = MOBILE_HTML.index("\n      function clearLookup()", start)
    function_source = MOBILE_HTML[start:end]
    script = f"""
const vm = require("vm");
const deferred = [];
const rendered = [];
const elements = {{
  lookupInput: {{value: "FIRST"}},
  lookupCategory: {{value: "all"}},
  lookupResults: {{hidden: true}},
}};
const state = {{
  lookupController: null,
  lookupGeneration: 0,
  lookupQuery: "",
  lookupCategory: "all",
  lookupPage: 1,
  lookupGroups: [],
  activePage: "lookup",
}};
const sandbox = {{
  AbortController,
  URLSearchParams,
  state,
  byId: id => elements[id],
  showStatus: () => {{}},
  renderLookupResults: data => rendered.push(data.query),
  apiGet: url => new Promise(resolve => deferred.push({{url, resolve}})),
  console,
}};
vm.createContext(sandbox);
vm.runInContext({json.dumps(function_source, ensure_ascii=False)}, sandbox);
(async () => {{
  const first = sandbox.runUnifiedSearch(1);
  elements.lookupInput.value = "SECOND";
  const second = sandbox.runUnifiedSearch(1);
  deferred[1].resolve({{query: "SECOND", groups: []}});
  await second;
  deferred[0].resolve({{query: "FIRST", groups: []}});
  await first;
  if (rendered.length !== 1 || rendered[0] !== "SECOND") {{
    throw new Error(`stale response rendered: ${{JSON.stringify(rendered)}}`);
  }}
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    result = subprocess.run(
        [node, "-e", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr
