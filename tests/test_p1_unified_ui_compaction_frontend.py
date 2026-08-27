from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def _orders_page() -> str:
    start = INDEX.index("activePage === 'orders'")
    end = INDEX.index("activePage === 'orders_legacy'", start)
    return INDEX[start:end]


def _order_modal() -> str:
    start = INDEX.index('<div v-else-if="modal.type === \'order\'">')
    end = INDEX.index('<div v-else-if="modal.type === \'orderEdit\'">', start)
    return INDEX[start:end]


def _production_page() -> str:
    start = INDEX.index("activePage === 'production'")
    end = INDEX.index("activePage === 'deliveries'", start)
    return INDEX[start:end]


def test_order_filters_keep_the_table_as_the_visual_focus() -> None:
    page = _orders_page()
    assert "order-keyword-input" in page
    assert "搜索客户全称/简称/缩写、订单号、存货编码、产品或规格" in page
    assert "order-stage-select" in page
    assert "order-filter-footer" in page
    assert "orderActiveFilterChips.length" in page
    assert "showAdvancedFilter" in page
    assert ".order-filter-compact > .order-keyword-input" in INDEX
    assert "width: 300px" in INDEX
    assert ".order-filter-footer .pager.top" in INDEX


def test_manual_size_row_has_a_narrower_code_and_readable_dimensions() -> None:
    modal = _order_modal()
    assert "manual-size-inline" in modal
    assert "manual-size-dimensions" in modal
    assert ".order-entry-code { width: 10%; }" in INDEX
    assert ".order-entry-spec { width: 18%; }" in INDEX
    assert ".manual-size-inline { display:grid; grid-template-columns:minmax(0,1fr); gap:4px; }" in INDEX
    assert ".manual-size-dimensions .input" in INDEX
    assert "font-variant-numeric:tabular-nums" in INDEX
    assert ".order-entry-modal { width: min(1760px, 100%); }" in INDEX
    for placeholder in ("长", "宽", "高"):
        assert f'placeholder="{placeholder}"' in modal


def test_production_rows_use_plain_language_and_two_line_product_summary() -> None:
    page = _production_page()
    for marker in (
        "production-product-summary",
        "productionBoardPreparationSummary(row)",
        "productionProductTitle(row)",
        "数量情况",
        "订单",
        "已到纸板",
        "本次做",
        "用纸板",
        "做成品",
        "用于订单",
        "多出",
    ):
        assert marker in page
    assert "订单 / 收料 / 计划" not in page
    assert "实际投入 / 合格 / 损耗" not in page
    assert "{{ row.special_process || '一开一' }} × {{ row.output_factor || 1 }}" not in page
    assert "max-height:2.7em" in INDEX


def test_production_destination_is_horizontal_without_changing_location_gates() -> None:
    page = _production_page()
    assert "production-destination-column" in page
    assert ".production-destination-column { width:300px; }" in INDEX
    assert "grid-template-columns:64px 92px minmax(112px,1fr)" in INDEX
    assert ".production-table .production-location-picker" in INDEX
    for marker in (
        "onProductionFloorChange",
        "onProductionAreaChange",
        "onProductionLocationSelection",
        "productionLocationUsedByOther",
        'value="direct"',
        'value="stock"',
    ):
        assert marker in page
