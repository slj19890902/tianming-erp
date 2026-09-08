"""Contract checks for stocktake information priority; no business writes."""
from pathlib import Path
import re
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def test_stocktake_name_and_specification_precede_secondary_identifiers():
    source = (ROOT / 'static/mobile_stocktake.html').read_text(encoding='utf-8')
    render = source[source.index('function renderLots()'):source.index('function renderLots()') + 2500]
    assert '<div class="lot-title">${h(product)}</div>' in render
    assert render.index('h(product)') < render.index('h(specification ||') < render.index('h(customer)') < render.index('h(code)')
    assert 'data-lot-id="${h(id)}"' in render
    assert '实盘数量' in render
    assert 'counted_quantity' in render


def test_mobile_location_and_search_cards_use_name_specification_physical_quantity_order():
    source = (ROOT / 'static/mobile_erp.html').read_text(encoding='utf-8')
    for marker in ('goods.forEach(good =>', '(data.items || []).forEach(good =>'):
        render = source[source.index(marker):source.index(marker) + 1800]
        assert render.index('warehouse-stocktake-name') < render.index('warehouse-stocktake-spec') < render.index('warehouse-stocktake-quantity') < render.index('good.customer_name')
        assert 'quantity_total ?? "待核对"' in render


def test_modified_mobile_scripts_parse(tmp_path):
    for filename in ('mobile_erp.html', 'mobile_stocktake.html'):
        source = (ROOT / 'static' / filename).read_text(encoding='utf-8')
        for i, code in enumerate(re.findall(r'<script(?:\s[^>]*)?>(.*?)</script>', source, re.S)):
            if not code.strip():
                continue
            script = tmp_path / f'{filename}-{i}.js'
            script.write_text(code, encoding='utf-8')
            result = subprocess.run([shutil.which('node'), '--check', str(script)], capture_output=True, text=True)
            assert result.returncode == 0, result.stderr


def test_stocktake_return_keeps_exact_location_and_reopens_its_rack():
    stocktake = (ROOT / 'static/mobile_stocktake.html').read_text(encoding='utf-8')
    mobile = (ROOT / 'static/mobile_erp.html').read_text(encoding='utf-8')
    assert "returnParams.set('location_id',String(locationId))" in stocktake
    assert 'Number.isSafeInteger(locationId)&&locationId>0' in stocktake
    assert 'state.warehouseSelectedRack = focusedLocation.map_rack_id' in mobile
    assert 'location.map_rack_id && !notDisclosed' in mobile
    assert 'visibleGoods.slice(0, 2)' in mobile
    assert '点击查看全部' in mobile
