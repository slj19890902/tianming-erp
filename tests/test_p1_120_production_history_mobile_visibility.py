from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
MOBILE = (ROOT / "static" / "mobile_erp.html").read_text(encoding="utf-8")


def _history_markup() -> str:
    return INDEX.split('<table class="production-history-table"', 1)[1].split(
        "</table>", 1
    )[0]


def test_history_is_two_line_customer_quantity_location_and_undo_only() -> None:
    history = _history_markup()
    for marker in (
        "客户 / 客户订单号",
        "存货编码 / 产品名称",
        "实际 / 理论数量",
        "customer_short_name",
        "customer_order_number",
        "客户订单号未填写",
        "beginProductionActualQuantityEdit(row)",
        "cancelProductionActualQuantityEdit(row)",
        "productionCurrentLocationClickable(row)",
        "openProductionInventory(row)",
        "row.is_fully_delivered",
        "history-delivered\">已送完",
        ">撤销</button>",
    ):
        assert marker in history
    assert "row.customer_short_name || row.customer_name" not in history
    for removed in (
        "数量 / 操作人",
        "完工处理 / 当前库位",
        "投入 {{",
        "合格 {{",
        "损耗 {{",
        "去送货",
        "撤销生产确认</button>",
        "production-history-action-menu",
    ):
        assert removed not in history
    assert history.count('class="history-two-lines"') >= 4


def test_actual_quantity_uses_inline_edit_and_map_opens_exact_new_page() -> None:
    begin = INDEX.split("beginProductionActualQuantityEdit(row) {", 1)[1].split(
        "cancelProductionActualQuantityEdit(row) {", 1
    )[0]
    save = INDEX.split("async modifyProductionActualQuantity(row) {", 1)[1].split(
        "async supplementProductionCompletion(row) {", 1
    )[0]
    open_map = INDEX.split("openProductionInventory(row) {", 1)[1].split(
        "async revertProductionCompletion(row) {", 1
    )[0]
    assert "row._actual_editing = true" in begin
    assert "prompt(" not in save
    assert "row._actual_draft" in save
    assert "/actual-quantity" in save
    assert 'tab:"locations", view:"2d"' in open_map
    assert 'window.open(`/warehouse.html?' in open_map
    assert '"_blank", "noopener"' in open_map
    assert 'row.is_fully_delivered !== true' in open_map
    assert "current_warehouse_location_map_issue" in open_map


def test_mobile_defaults_to_three_day_status_without_reclassifying_pending_queue() -> None:
    production_page = MOBILE.split('<section id="productionPage"', 1)[1].split(
        "</section>", 1
    )[0]
    assert "近 3 天生产状态" in production_page
    assert "data-period=\"3d\"" in production_page
    assert "已完工待送仍保留真实状态" in production_page
    assert production_page.index("productionList") < production_page.index(
        "productionStationTabs"
    )
    enter_production = MOBILE.split('if (page === "production") {', 1)[1].split(
        "persistMobilePortalState", 1
    )[0]
    assert "loadProduction();" in enter_production
    assert "loadProductionStation();" in enter_production
    assert 'status="pending"' not in MOBILE
