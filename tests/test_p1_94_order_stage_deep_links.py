from pathlib import Path
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
WAREHOUSE = (ROOT / "static" / "warehouse.html").read_text(encoding="utf-8")


def test_trace_stage_opens_revalidated_business_target_in_new_tab() -> None:
    assert "async openTraceEventTarget(event)" in INDEX
    method_start = INDEX.index("async openTraceEventTarget(event)")
    method_end = INDEX.index("traceEventTime(event)", method_start)
    method = INDEX[method_start:method_end]
    assert 'window.open("", "_blank")' in method
    assert "opened.opener = null" in method
    assert "/documents/${sourceType}/${sourceId}`" in method
    assert "data.navigation_url" in method
    assert "opened.location.replace(data.navigation_url)" in method
    assert "openTraceEventDetail(event)" not in method
    inventory_start = INDEX.index("openTraceInventoryTarget(lot) {")
    inventory_end = INDEX.index("traceEventTime(event)", inventory_start)
    inventory_method = INDEX[inventory_start:inventory_end]
    assert 'window.open(url, "_blank")' in inventory_method
    assert "opened.opener = null" in inventory_method


def test_business_target_reauthorizes_exact_identity_and_shows_read_only_focus() -> None:
    assert "async applyOrderStageDeepLink()" in INDEX
    request_start = INDEX.index("orderStageDeepLinkRequest()")
    request_end = INDEX.index("async focusOrderStageBusinessList", request_start)
    request_method = INDEX[request_start:request_end]
    focus_start = INDEX.index("async focusOrderStageBusinessList", request_end)
    method_start = INDEX.index("async applyOrderStageDeepLink()")
    method_end = INDEX.index("async loadProductBoxTypeRules", method_start)
    focus_method = INDEX[focus_start:method_start]
    method = INDEX[method_start:method_end]
    assert 'params.get("trace_order_id")' in request_method
    assert 'params.get("trace_order_item_id")' in request_method
    assert 'params.get("trace_source_type")' in request_method
    assert 'params.get("trace_source_id")' in request_method
    assert "/documents/${sourceType}/${request.sourceId}`" in method
    assert "stageDeepLink.detail = data" in method
    assert 'module === "finance"' in focus_method
    assert "detail?.event?.details?.statement_id" in focus_method
    assert "await this.openStatementDetail({id:statementId})" in focus_method
    assert "trace-deep-link-panel" in INDEX
    assert "只读定位" in INDEX


def test_warehouse_target_reauthorizes_before_map_focus_and_stays_read_only() -> None:
    assert "async function resolveWarehouseTraceDeepLink(params)" in WAREHOUSE
    method_start = WAREHOUSE.index("async function resolveWarehouseTraceDeepLink(params)")
    method_end = WAREHOUSE.index("async function applyWarehouseDeepLink", method_start)
    method = WAREHOUSE[method_start:method_end]
    assert 'params.get("trace_order_id")' in method
    assert 'params.get("trace_order_item_id")' in method
    assert 'params.get("trace_source_type")' in method
    assert 'params.get("trace_source_id")' in method
    assert "/api/orders/${orderId}/items/${itemId}/documents/${sourceType}/${sourceId}" in method
    assert 'detail.target?.module!=="warehouse"' in method
    assert 'warehouseSearchParams.get("readonly") === "1"' in WAREHOUSE
    assert "warehouseTraceMode" in WAREHOUSE
    assert "|| warehouseTraceMode" in WAREHOUSE


def test_navigation_url_contract_uses_only_stable_trace_identity() -> None:
    from app.services.order_document_trace import trace_navigation_url

    event = {
        "target": {"module": "incoming"},
        "source_type": "incoming_receipt",
        "source_id": 44,
        "details": {},
    }
    url = trace_navigation_url(order_id=12, item_id=34, event=event)
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    assert parsed.path == "/"
    assert query == {
        "page": ["incoming"],
        "trace_order_id": ["12"],
        "trace_order_item_id": ["34"],
        "trace_source_type": ["incoming_receipt"],
        "trace_source_id": ["44"],
    }
    assert "customer" not in url
    assert "product" not in url


def test_inventory_navigation_uses_exact_lot_and_location() -> None:
    from app.services.order_document_trace import trace_navigation_url

    event = {
        "target": {"module": "warehouse"},
        "source_type": "inventory_movement",
        "source_id": 8,
        "details": {"inventory_lot_id": 91, "location_id": 27},
    }
    url = trace_navigation_url(order_id=12, item_id=34, event=event)
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    assert parsed.path == "/warehouse.html"
    assert query["lot_id"] == ["91"]
    assert query["location_id"] == ["27"]
    assert query["readonly"] == ["1"]
    assert query["trace_order_item_id"] == ["34"]
