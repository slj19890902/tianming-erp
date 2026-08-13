from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
WAREHOUSE = (ROOT / "static" / "warehouse.html").read_text(encoding="utf-8")


def test_shared_top_pager_and_order_filters_are_wired() -> None:
    assert "筛选查找｜已筛选 {{ filterCount == null ? 0 : filterCount }} 项" in INDEX
    assert 'props: ["page", "total", "pageSize", "always", "compact", "filterCount"]' in INDEX
    assert ':always="true" :compact="true"' in INDEX
    assert 'params.order_number = this.filters.orderNumber' in INDEX
    assert 'params.customer_po = this.filters.orderCustomerPo' in INDEX
    assert 'params.product_code = this.filters.orderProductCode' in INDEX
    assert 'params.product_name = this.filters.orderProductName' in INDEX
    assert 'params.specification = this.filters.orderSpecification' in INDEX
    assert 'params.delivery_date_from = this.filters.orderDeliveryDateFrom' in INDEX


def test_reported_incoming_production_and_delivery_use_server_pages() -> None:
    assert 'page: this.pages.requisitionReported' in INDEX
    assert 'this.requisitionReportedTotal = Number(data.total || 0)' in INDEX
    assert 'page: this.pages.incomingHistory' in INDEX
    assert 'this.incomingHistoryTotal = Number(data.total || 0)' in INDEX
    assert ':key="row.history_key || row.receipt_item_id || row.item_id"' in INDEX
    assert 'page: this.pages.productionHistory' in INDEX
    assert 'this.productionHistoryTotal = Number(data.total || 0)' in INDEX
    assert 'axios.get("/api/production/completions", { params })' in INDEX
    assert 'Object.entries(this.deliveryListFilters || {})' in INDEX
    assert 'axios.get("/api/deliveries", { params })' in INDEX


def test_warehouse_finished_and_semi_finished_use_real_backend_paging() -> None:
    assert 'lotPage:1,lotPageSize:25,lotTotal:0' in WAREHOUSE
    assert 'page:state.lotPage,page_size:state.lotPageSize' in WAREHOUSE
    assert 'finished_product_code:inventoryType==="finished"' in WAREHOUSE
    assert 'semi_supplier:inventoryType==="semi_finished"' in WAREHOUSE
    assert 'semi_allowed_product:inventoryType==="semi_finished"' in WAREHOUSE
    assert 'location_keyword:$("locationKeywordFilter").value' in WAREHOUSE
    assert 'pallet_keyword:$("palletKeywordFilter").value' in WAREHOUSE
    assert "state.lotTotal=Number(data.total||0)" in WAREHOUSE
    assert "筛选查找｜已筛选 ${state.lotTotal} 项" in WAREHOUSE
    assert 'page_size:"200"' not in WAREHOUSE[
        WAREHOUSE.index("async function loadLots()") :
        WAREHOUSE.index("function applyFinishedLotColumnWidths")
    ]
