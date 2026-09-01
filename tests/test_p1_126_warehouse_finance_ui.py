import json
from dataclasses import replace
from datetime import date
from hashlib import sha256
from pathlib import Path

from app.services.warehouse_stocktake_batch import (
    WarehouseStocktakeBatchItem,
    stocktake_batch_request_hash,
)


ROOT = Path(__file__).resolve().parents[1]
FINANCE = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
WAREHOUSE = (ROOT / "static" / "warehouse.html").read_text(encoding="utf-8")
TWIN = (
    ROOT / "factory_twin" / "frontend" / "src" / "WarehouseTwinApp.tsx"
).read_text(encoding="utf-8")
STOCKTAKE = (
    ROOT / "factory_twin" / "frontend" / "src" / "warehouseStocktakeDraft.mjs"
).read_text(encoding="utf-8")
MOVE_SERVICE = (
    ROOT / "app" / "services" / "warehouse_movement_batch.py"
).read_text(encoding="utf-8")
TWIN_CSS = (
    ROOT / "factory_twin" / "frontend" / "src" / "warehouseTwin.css"
).read_text(encoding="utf-8")
TWIN_BASE_CSS = (
    ROOT / "factory_twin" / "frontend" / "src" / "styles.css"
).read_text(encoding="utf-8")
TWIN_ENTRY = (
    ROOT / "factory_twin" / "frontend" / "warehouse-twin.html"
).read_text(encoding="utf-8")


def test_warehouse_primary_navigation_has_exactly_four_daily_entries() -> None:
    start = WAREHOUSE.index('<div class="tabs">')
    end = WAREHOUSE.index("</div>", start)
    primary = WAREHOUSE[start:end]
    assert primary.count("<button") + primary.count("<a ") == 4
    assert ">库存台账</button>" in primary
    assert ">库存流水</button>" in primary
    assert ">模具位置</button>" in primary
    assert ">盘点上架</a>" in primary
    assert "/warehouse.html?mode=move&amp;action=stocktake&amp;view=2d" in primary
    assert '<details class="warehouse-secondary">' in WAREHOUSE


def test_finance_navigation_and_statement_rows_keep_low_frequency_actions_folded() -> None:
    page_head_start = FINANCE.index('<div class="page-head finance-page-head">')
    nav_start = FINANCE.index('<div class="toolbar-group">', page_head_start)
    nav_end = FINANCE.index("</div>", nav_start)
    nav = FINANCE[nav_start:nav_end]
    assert nav.count("<button") == 4
    assert nav.count("<summary") == 0
    for label in ("经营概览", "客户对账", "开票任务", "应付支出"):
        assert label in nav

    row_start = FINANCE.index('<tr v-for="statement in row.statements"')
    row_end = FINANCE.index("</tr>", row_start)
    row = FINANCE[row_start:row_end]
    assert ">查看</button>" not in row
    assert "v-else-if=\"canGenerateInvoiceTask" in row
    assert "生成开票任务" in row and "客户异议" in row
    assert "Excel" in row and "PDF" in row
    assert "收款核销" not in row


def test_stocktake_records_partner_source_supports_rack_and_exposes_labels() -> None:
    assert 'source_kind: stocktakeSourceKind' in TWIN
    assert "合作纸箱厂搬入" in TWIN
    assert 'query.get("action") === "stocktake"' in TWIN
    assert "/location-label.html?location_id=" in TWIN
    assert "/static/finished-goods-label.html?lot_id=" in TWIN
    assert '["ground", "rack", "temporary_aisle"]' in STOCKTAKE
    assert 'pallet_storage_only=item.operation == "pallet_move"' in MOVE_SERVICE


def test_omitted_stocktake_source_keeps_legacy_idempotency_hash() -> None:
    item = WarehouseStocktakeBatchItem(
        client_item_id="legacy-item",
        operation="add",
        location_id=12,
        expected_layout_version=3,
        quantity=20,
        inventory_type="finished",
        unit="boxes",
        customer_id=4,
        product_id=5,
        stock_date=date(2026, 8, 29),
    )
    legacy_hash = stocktake_batch_request_hash(batch_id="legacy-batch", items=[item])
    legacy_canonical = {
        "batch_id": "legacy-batch",
        "items": [{
            "client_item_id": "legacy-item",
            "operation": "add",
            "location_id": 12,
            "expected_layout_version": 3,
            "quantity": 20,
            "inventory_type": "finished",
            "unit": "boxes",
            "customer_id": 4,
            "product_id": 5,
            "stock_date": "2026-08-29",
            "lot_id": None,
            "expected_version": None,
        }],
    }
    expected_legacy_hash = sha256(json.dumps(
        legacy_canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    partner_hash = stocktake_batch_request_hash(
        batch_id="legacy-batch",
        items=[replace(item, source_kind="partner_transfer")],
    )

    assert legacy_hash == expected_legacy_hash
    assert legacy_hash != partner_hash


def test_measured_warehouse_uses_the_same_visual_language_as_erp() -> None:
    erp_font = (
        '-apple-system, BlinkMacSystemFont, "SF Pro Display", "SF Pro Text", '
        '"PingFang SC", "Helvetica Neue", Helvetica, "Microsoft YaHei", sans-serif'
    )
    assert f"font-family: {erp_font};" in TWIN_BASE_CSS
    assert f"font-family: {erp_font};" in TWIN_CSS
    assert "--erp-bg: #f5f5f7;" in TWIN_CSS
    assert "--erp-primary: #0071e3;" in TWIN_CSS
    assert "--erp-radius-card: 18px;" in TWIN_CSS
    assert "--erp-radius-input: 10px;" in TWIN_CSS
    assert "--erp-radius-button: 980px;" in TWIN_CSS
    assert '<meta name="theme-color" content="#f5f5f7" />' in TWIN_ENTRY
    assert ".warehouse-twin-shell * {" in TWIN_CSS
    assert "font-family: inherit !important;" in TWIN_CSS
