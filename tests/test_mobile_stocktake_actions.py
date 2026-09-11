import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_stocktake_actions_do_not_overlay_inbound_save():
    html = (ROOT / 'static/mobile_stocktake.html').read_text(encoding='utf-8')
    assert '.sticky-submit{position:static;' in html
    assert 'id="inboundCancel"' in html


def test_submit_state_tracks_current_lots_and_inbound_edit():
    html = (ROOT / 'static/mobile_stocktake.html').read_text(encoding='utf-8')
    source = re.search(r'    function updateSubmitState\(\).*?\n', html)[0]
    script = r'''
const assert=require('node:assert/strict');
const nodes=new Map();
const $=id=>{if(!nodes.has(id))nodes.set(id,{value:'',textContent:'',disabled:false,classList:{toggle(k,v){this[k]=v}}});return nodes.get(id)};
const state={user:{role:'admin'},lots:[],submitting:false,locked:false};
const inbound={busy:false,attempt:null,type:'finished'};
const allCounted=()=>true;
SOURCE
updateSubmitState();assert.equal($('submitButton').textContent,'确认现场为空');
$('inboundProduct').value='10';updateSubmitState();assert.equal($('submitArea').classList.hidden,true);
$('inboundProduct').value='';state.lots=[{id:5}];updateSubmitState();assert.equal($('submitButton').textContent,'确认盘点并更新库存');assert.equal($('submitArea').classList.hidden,false);
state.lots=[];updateSubmitState();assert.equal($('submitButton').textContent,'确认现场为空');
inbound.attempt={};updateSubmitState();assert.equal($('submitButton').disabled,true);assert.equal($('submitArea').classList.hidden,true);
inbound.attempt=null;state.user.role='worker';state.lots=[{id:5}];updateSubmitState();assert.equal($('submitButton').textContent,'提交盘点');
inbound.type='semi_finished';updateSubmitState();assert.equal($('submitArea').classList.hidden,true);inbound.type='finished';
state.locked=true;updateSubmitState();assert.equal($('submitButton').disabled,true);assert.equal($('submitArea').classList.hidden,true);
'''.replace('SOURCE', source)
    result = subprocess.run(['node', '-e', script], capture_output=True, text=True, encoding='utf-8')
    assert result.returncode == 0, result.stderr
