from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / 'factory_twin/frontend/src/WarehouseTwinApp.tsx').read_text(encoding='utf-8')


def test_sidebar_keeps_formal_gates_and_removes_duplicate_stocktake_banner():
    assert '盘点只在右侧所选正式货位形成新增或调减草稿' not in SOURCE
    assert '优先添加待归位货物' not in SOURCE
    assert 'selectedLocationCanReceiveStocktakeProduct' in SOURCE
    assert 'stocktakeSupplementConfirmed' in SOURCE
    assert 'pendingRefreshRequired || Boolean(selectedLocationFinishedAddBlockReason)' in SOURCE
    assert 'const [locationDetailOpen, setLocationDetailOpen] = useState(false)' in SOURCE
    assert SOURCE.index('aria-expanded={locationDetailOpen}') < SOURCE.index('className="twin-location-stock-heading"')


def test_shared_search_and_separate_label_details_actions():
    assert 'const stocktakePendingMatches' in SOURCE
    assert 'String(item.customer_id) === stocktakeCustomerId' in SOURCE
    assert 'stocktakeMissingOpen &&' in SOURCE
    assert 'stocktakeStockProductIds.has(item.product_id)' in SOURCE
    assert 'title="查看产品标签" onClick={() => setSidebarLabelLotId' in SOURCE
    assert 'aria-expanded={Boolean(sidebarExpandedLots[item.lot_id])}' in SOURCE
    assert 'onClick={queueStocktakeAddDraft}>加入盘点</button>' in SOURCE


def test_compact_css_uses_one_scroll_surface():
    css = (ROOT / 'factory_twin/frontend/src/warehouseTwin.css').read_text(encoding='utf-8')
    assert '.twin-stocktake-search-row' in css
    assert 'max-height: none; overflow: visible' in css
    assert 'background: #f0f8ff' in css
    assert 'background: #edf9f5' in css
