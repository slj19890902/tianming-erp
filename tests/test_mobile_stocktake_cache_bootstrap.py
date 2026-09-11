import json
import subprocess
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]

def test_old_document_reloads_before_runtime_and_new_document_loads_once():
    source=(ROOT/"static/mobile_initial_stocktake.js").read_text(encoding="utf8")
    harness=r"""
const assert=require('node:assert/strict'),vm=require('node:vm');
function run(fresh,query=''){
 const redirects=[],scripts=[],links=[];
 const box={replaceChildren(link){links.push(link)}};
 const context={URL,Date,window:{location:{href:'http://192.168.3.80:8000/mobile/stocktake.html?location_id=42&return_area=F'+query,replace(url){redirects.push(url)}}},document:{getElementById(id){return id==='message'?box:fresh?{}:null},body:box,createElement(){return{}},head:{appendChild(script){scripts.push(script)}}}};
 vm.createContext(context);vm.runInContext(SOURCE,context);
 return {redirects,scripts,links,context};
}
const old=run(false);assert.equal(old.scripts.length,0);assert.equal(old.redirects.length,1);
const target=new URL(old.redirects[0]);assert.equal(target.searchParams.get('location_id'),'42');assert.equal(target.searchParams.get('return_area'),'F');assert.equal(target.searchParams.get('stocktake_ui'),'3');
const repeated=run(false,'&stocktake_ui=3');assert.equal(repeated.redirects.length,0);assert.equal(repeated.scripts.length,0);assert.equal(repeated.links.length,1);
const current=run(true);assert.equal(current.redirects.length,0);assert.equal(current.scripts.length,1);assert.match(current.scripts[0].src,/initial-stocktake-runtime/);vm.runInContext(SOURCE,current.context);assert.equal(current.scripts.length,1);
"""
    result=subprocess.run(["node","-e","const SOURCE="+json.dumps(source)+";\n"+harness],capture_output=True,text=True)
    assert result.returncode==0,result.stderr
