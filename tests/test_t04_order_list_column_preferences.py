from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / 'static' / 'index.html').read_text(encoding='utf-8')


def test_order_list_template_exposes_only_local_column_preferences() -> None:
    for expected in ('aria-label="订单列表列偏好"', 'orderListColumnOptions()', 'setOrderListColumnVisible(column.id,$event.target.checked)', 'resetOrderListColumns', "orderListColumnVisible('total_amount')", ':colspan="orderListTableColumnCount()"'):
        assert expected in INDEX
    assert 'localStorage.setItem(key,JSON.stringify({schema:"t04-v1",columns}))' in INDEX
    assert 'erp_order_list_columns_v1:${id}' in INDEX


def test_column_preferences_are_account_scoped_resettable_and_do_not_expose_amounts(tmp_path: Path) -> None:
    node = shutil.which('node')
    assert node is not None
    scripts = [script for script in re.findall(r'<script(?:\s[^>]*)?>(.*?)</script>', INDEX, re.DOTALL) if script.strip()]
    assert len(scripts) == 1
    harness = tmp_path / 't04-columns.cjs'
    harness.write_text(r'''
const fs = require('fs'), vm = require('vm');
const html = fs.readFileSync(process.argv[2], 'utf8');
const source = [...html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)].map(m=>m[1]).filter(Boolean);
const storage = new Map();
const sandbox = {axios:{defaults:{},interceptors:{response:{use(){}}}},Vue:{createApp(definition){sandbox.definition=definition;return {component(){return this},mount(){return this}}}},HTMLElement: class {},window:{},document:{},URLSearchParams,setTimeout,clearTimeout,console,localStorage:{getItem:key=>storage.get(key)||null,setItem:(key,value)=>storage.set(key,String(value)),removeItem:key=>storage.delete(key)},TMOrderReference:{component:{}}};
vm.createContext(sandbox); vm.runInContext(source[0], sandbox);
const methods = sandbox.definition.methods;
function context(id, canViewSalesAmounts=true) { const ctx={user:{id},canViewSalesAmounts,orderListColumnPanelOpen:false,orderListColumns:{},showToast(){}}; for (const key of ['orderListColumnDefaults','orderListColumnPreferenceKey','orderListColumnOptions','orderListColumnVisible','orderListTableColumnCount','loadOrderListColumnPreferences','persistOrderListColumns','setOrderListColumnVisible','resetOrderListColumns']) ctx[key]=methods[key]; return ctx; }
function assert(value,message){if(!value) throw new Error(message)}
const first=context(101,true); first.loadOrderListColumnPreferences(); first.setOrderListColumnVisible('delivery_date',false); assert(!first.orderListColumnVisible('delivery_date'),'first user preference'); assert(storage.has('erp_order_list_columns_v1:101'),'stored by user');
const second=context(202,true); second.loadOrderListColumnPreferences(); assert(second.orderListColumnVisible('delivery_date'),'preferences must not cross accounts');
const repeat=context(101,true); repeat.loadOrderListColumnPreferences(); assert(!repeat.orderListColumnVisible('delivery_date'),'same user restores preference'); assert(repeat.orderListColumnVisible('customer_po') && repeat.orderListColumnVisible('status') && repeat.orderListColumnVisible('actions'),'required columns stay visible');
const restricted=context(101,false); restricted.loadOrderListColumnPreferences(); assert(!restricted.orderListColumnVisible('total_amount'),'amount must remain unavailable without permission'); assert(!restricted.orderListColumnOptions().some(item=>item.id==='total_amount'),'amount toggle must remain hidden without permission');
repeat.resetOrderListColumns(); assert(!storage.has('erp_order_list_columns_v1:101'),'reset clears only this account key'); assert(repeat.orderListTableColumnCount()===9,'all visible columns count'); process.stdout.write('ok');
''', encoding='utf-8')
    completed = subprocess.run([node, str(harness), str(ROOT / 'static' / 'index.html')], capture_output=True, text=True, encoding='utf-8', check=False)
    assert completed.returncode == 0, completed.stderr or completed.stdout
