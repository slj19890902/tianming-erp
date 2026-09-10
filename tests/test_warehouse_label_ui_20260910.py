from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_all_map_modes_share_compact_location_and_label_actions():
    source = (ROOT / 'factory_twin/frontend/src/WarehouseTwinApp.tsx').read_text(encoding='utf-8')
    assert '<section className="twin-location-card twin-stocktake-compact">' in source
    assert source.count('<InventoryLabelSummary item=') >= 3
    assert 'title="查看产品标签" onClick={onLabel}' in source
    assert 'aria-expanded={expanded} onClick={onDetails}' in source
    assert '楼层切换不丢页面草稿' not in source
    assert 'detailOpen && <ShelfLotHistory' in source
    assert 'onClick={queueStocktakeAddDraft}' in source


def test_mobile_goods_precede_operation_and_history_remains_lazy():
    source = (ROOT / 'static/mobile_erp.html').read_text(encoding='utf-8')
    start = source.index('function renderWarehouseLocationGoods(')
    end = source.index('function renderEmptyWarehouseLocationSearch(', start)
    body = source[start:end]
    assert body.index('goods.forEach') < body.index('container.append(addPanel)')
    assert 'code.addEventListener("click"' in body
    assert 'detailsToggle.addEventListener("click"' in body
    assert 'if (!history.open || historyLoading || historyLoaded) return' in body
    assert 'move.disabled = good.can_move !== true' in body
    assert 'location_id: location.location_id' in body


def test_long_labels_wrap_and_touch_targets_remain_usable():
    css = (ROOT / 'factory_twin/frontend/src/warehouseTwin.css').read_text(encoding='utf-8')
    assert 'flex-wrap:wrap' in css
    assert 'overflow-wrap:anywhere' in css
    assert 'grid-auto-columns:minmax(240px,1fr)' in css
    assert '.warehouse-label-code-row button,.warehouse-label-add' in css
