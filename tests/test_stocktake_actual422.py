from pathlib import Path
import json
import re
import subprocess

from fastapi.testclient import TestClient
from sqlalchemy import select
from tests.test_n035_stocktake_api import stocktake_api, _login, _submission_payload
from app.models.warehouse_inventory import InventoryLot, InventoryMovement

ROOT = Path(__file__).resolve().parents[1]


def test_map_uses_actual_count_not_decrement_or_new_receipt():
    source = (ROOT / 'factory_twin/frontend/src/WarehouseTwinApp.tsx').read_text(encoding='utf8')
    dialog = (ROOT / 'factory_twin/frontend/src/ActualStocktakeDialog.tsx').read_text(encoding='utf8')
    assert 'queueStocktakeDecreaseDraft' not in source
    assert '本次调减数量' not in source
    assert 'setActualStocktakeLocationId(selectedLocation.location_id)' in source
    assert 'ActualStocktakeDialog locationId={actualStocktakeLocationId}' in source
    assert 'void refreshDashboard().catch' in source
    assert '/mobile/stocktake.html?location_id=' in dialog
    assert '&embedded=1' in dialog
    assert 'event.origin !== window.location.origin' in dialog
    assert 'event.source !== frame.current?.contentWindow' in dialog
    assert 'disabled={busy}' in dialog


def test_actual_count_gain_preserves_other_batches_and_identity(stocktake_api):
    app, factory, ids = stocktake_api
    with factory() as db:
        before = {lot.id: (lot.warehouse_location_id, lot.quantity_available,
                  lot.quantity_reserved, lot.quantity_consumed, lot.quantity_damaged,
                  getattr(lot.finished_detail, 'product_id', None)) for lot in db.scalars(select(InventoryLot))}
    with TestClient(app) as client:
        _login(client, 'n035-admin')
        payload = _submission_payload(client, ids['location'], key='map-real-count-422', counts={ids['lot1']: 22})
        for _ in range(2):
            response = client.post('/api/warehouse/stocktakes/confirm', json=payload)
            assert response.status_code == 201, response.text
    with factory() as db:
        after = {lot.id: (lot.warehouse_location_id, lot.quantity_available,
                 lot.quantity_reserved, lot.quantity_consumed, lot.quantity_damaged,
                 getattr(lot.finished_detail, 'product_id', None)) for lot in db.scalars(select(InventoryLot))}
        expected = dict(before)
        original = before[ids['lot1']]
        expected[ids['lot1']] = (original[0], 20, *original[2:])
        assert after == expected
        moves = list(db.scalars(select(InventoryMovement)))
        assert len(moves) == 1 and moves[0].quantity == 10
        assert moves[0].movement_type == 'adjust'


def test_embedded_count_separates_initial_receipt_and_has_no_upper_limit():
    html = (ROOT / 'static/mobile_stocktake.html').read_text(encoding='utf8')
    runtime = (ROOT / 'static/mobile_initial_stocktake_runtime.js').read_text(encoding='utf8')
    assert 'notifyEmbeddedStocktakeBusy(true)' in html
    assert 'notifyEmbeddedStocktakeBusy(false)' in html
    assert 'embeddedStocktake){$("initialInbound").classList.add("hidden");return;}' in runtime
    count_input = html.split('class="count-input"', 1)[1].split('>', 1)[0]
    assert 'min="0"' in count_input and 'max=' not in count_input
    assert "'/api/warehouse/stocktakes/confirm':'/api/warehouse/stocktakes'" in html
    assert 'stocktakeUnit(lot)' in html


def test_count_form_accepts_surplus_and_keeps_chinese_bom_units():
    html = (ROOT / 'static/mobile_stocktake.html').read_text(encoding='utf8')
    functions = '\n'.join(re.search(r'    function ' + name + r'\(.*?\n', html)[0]
                          for name in ('allCounted', 'stocktakeUnit', 'notifyEmbeddedStocktakeBusy'))
    scripts = re.findall(r'<script(?:\s[^>]*)?>(.*?)</script>', html, re.S)
    script = "const scripts=" + json.dumps(scripts) + ";scripts.forEach(s=>new Function(s));\n" + r'''
const assert=require('node:assert/strict');
let inputs=[{value:'110'}];
const document={querySelectorAll:()=>inputs};
const messages=[];const embeddedStocktake=true;
const window={location:{origin:'http://erp.local'},parent:{postMessage:(m,o)=>messages.push([m,o])}};
FUNCTIONS
assert.equal(allCounted(),true);
for(const value of ['0','9999']){inputs=[{value}];assert.equal(allCounted(),true)}
for(const value of ['','-1','1.5','NaN']){inputs=[{value}];assert.equal(allCounted(),false)}
assert.equal(stocktakeUnit({unit:'boxes',display_unit:'套'}),'套');
assert.equal(stocktakeUnit({unit:'sheets'}),'张');
notifyEmbeddedStocktakeBusy(true);notifyEmbeddedStocktakeBusy(false);
assert.deepEqual(messages.map(x=>x[0].busy),[true,false]);
assert.ok(messages.every(x=>x[1]==='http://erp.local'));
'''.replace('FUNCTIONS', functions)
    result = subprocess.run(['node'], input=script, capture_output=True, text=True, encoding='utf8')
    assert result.returncode == 0, result.stderr
