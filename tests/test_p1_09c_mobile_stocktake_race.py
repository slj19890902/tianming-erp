from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MOBILE = (ROOT / "static" / "mobile_stocktake.html").read_text(encoding="utf-8")


def _script() -> str:
    match = re.search(r"<script>(.*?)</script>", MOBILE, re.DOTALL)
    assert match is not None
    return match.group(1)


def _between(start: str, end: str) -> str:
    source = _script()
    start_index = source.index(start)
    return source[start_index : source.index(end, start_index)]


def _run_node(source: str, tmp_path: Path, name: str) -> None:
    node = shutil.which("node")
    assert node, "Node.js is required for JavaScript behavior validation"
    target = tmp_path / name
    target.write_text(source, encoding="utf-8")
    result = subprocess.run(
        [node, str(target)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_mobile_stocktake_has_latest_request_and_recovery_guards() -> None:
    for marker in (
        "let locationListGeneration=0",
        "locationDetailGeneration=0",
        "new AbortController()",
        "generation!==locationListGeneration",
        "generation!==locationDetailGeneration",
        "invalidateLocationDetail",
        "handleStocktakeAuth",
        "无法连接 ERP 服务，请检查网络后重试",
        "重新加载当前库位",
    ):
        assert marker in MOBILE


def test_fast_location_switch_keeps_only_latest_detail(tmp_path: Path) -> None:
    open_source = _between("async function openLocation(id)", "function fillAll()")
    harness = f"""
class FakeAbortController {{ constructor() {{ this.signal={{aborted:false}}; }} abort() {{ this.signal.aborted=true; }} }}
global.AbortController=FakeAbortController;
const state={{locations:[{{id:1,location_code:"A-01"}},{{id:2,location_code:"A-02"}}],selectedLocation:null,lots:[],locked:false,submitting:false,clientLineIds:{{}},lotSnapshots:{{}}}};
const classList=()=>({{add:()=>{{}},remove:()=>{{}},toggle:()=>{{}}}});
const nodes={{}};
for(const id of ["locationPanel","stocktakePanel","selectedLocationCode","selectedLocationName","locationNotice","lockedResult","stocktakeNumber","lotSummary","lotList","submitArea"])nodes[id]={{classList:classList(),textContent:"",innerHTML:""}};
const $=id=>nodes[id];
const pick=(row,keys,fallback=null)=>{{for(const key of keys)if(row&&row[key]!==undefined&&row[key]!==null)return row[key];return fallback}};
const locationCode=row=>row.location_code||"";
const isTemporary=()=>false;
const listItems=(row,keys=["items"] )=>{{for(const key of keys)if(Array.isArray(row?.[key]))return row[key];return []}};
const lotId=row=>row.id;
const numberValue=()=>0;
const calls=[];
function api(url,options){{return new Promise((resolve,reject)=>calls.push({{url,options,resolve,reject}}))}}
function resetMobileSubmitAttempt(){{}}
function showMessage(){{}}
function captureLotSnapshots(){{}}
let rendered=[];
function renderLots(){{rendered.push(state.lots.map(row=>row.id).join(","))}}
function resetLockedInputs(){{}}
function updateSubmitState(){{}}
function isStocktakeDrift(){{return false}}
function renderLocationLoadError(){{}}
function resetToLocations(){{}}
function handleStocktakeAuth(){{return false}}
function isAbortError(error){{return error?.name==="AbortError"}}
let locationDetailGeneration=0,locationDetailController=null;
function invalidateLocationDetail(){{locationDetailGeneration+=1;locationDetailController?.abort();locationDetailController=null}}
{open_source}
(async()=>{{
  const first=openLocation(1);
  const second=openLocation(2);
  calls[0].resolve({{items:[{{id:101}}]}});
  await first;
  if(rendered.length!==0)throw new Error("older location response rendered after a newer selection");
  calls[1].resolve({{items:[{{id:202}}]}});
  await second;
  if(state.selectedLocation?.id!==2)throw new Error("latest location selection was lost");
  if(state.lots.length!==1||state.lots[0].id!==202)throw new Error("latest location lots were not retained");
  if(rendered.length!==1)throw new Error("only the latest location detail should render");
}})().catch(error=>{{console.error(error);process.exit(1)}});
"""
    _run_node(harness, tmp_path, "p1-09c-mobile-stocktake-detail-race.js")


def test_repeated_location_list_load_keeps_only_latest_response(tmp_path: Path) -> None:
    load_source = _between("async function loadLocations()", "async function openLocation(id)")
    harness = f"""
class FakeAbortController {{ constructor() {{ this.signal={{aborted:false}}; }} abort() {{ this.signal.aborted=true; }} }}
global.AbortController=FakeAbortController;
const state={{locations:[]}};
const calls=[];
function api(url,options){{return new Promise((resolve,reject)=>calls.push({{url,options,resolve,reject}}))}}
const listItems=data=>data.items||[];
function setLocationLoading(){{}}
function showMessage(){{}}
function renderFloors(){{}}
function renderAreas(){{}}
let rendered=[];
function renderLocations(){{rendered.push(state.locations.map(row=>row.id).join(","))}}
function handleStocktakeAuth(){{return false}}
function isAbortError(error){{return error?.name==="AbortError"}}
let locationListGeneration=0,locationListController=null,locationDetailGeneration=0,locationDetailController=null;
function invalidateLocationDetail(){{locationDetailGeneration+=1;locationDetailController?.abort();locationDetailController=null}}
{load_source}
(async()=>{{
  const first=loadLocations();
  const second=loadLocations();
  calls[0].resolve({{items:[{{id:1}}]}});
  await first;
  if(rendered.length!==0)throw new Error("older location list rendered after retry");
  calls[1].resolve({{items:[{{id:2}}]}});
  await second;
  if(state.locations.length!==1||state.locations[0].id!==2)throw new Error("latest location list was not retained");
  if(rendered.length!==1)throw new Error("only the latest location list should render");
}})().catch(error=>{{console.error(error);process.exit(1)}});
"""
    _run_node(harness, tmp_path, "p1-09c-mobile-stocktake-list-race.js")


def test_stocktake_api_translates_network_and_non_json_errors(tmp_path: Path) -> None:
    api_source = _between("function apiErrorCode(body)", "function isStocktakeDrift(error)")
    harness = f"""
{api_source}
const expect=(condition,message)=>{{if(!condition)throw new Error(message)}};
(async()=>{{
  global.fetch=async()=>{{throw new TypeError("Failed to fetch")}};
  try{{await api("/offline")}}catch(error){{expect(error.message.includes("无法连接 ERP"),"network error must be Chinese")}}
  global.fetch=async()=>({{ok:false,status:500,text:async()=>"broken"}});
  try{{await api("/server")}}catch(error){{expect(error.message.includes("HTTP 500"),"non-JSON error must retain HTTP status");return}}
  throw new Error("server request must fail");
}})().catch(error=>{{console.error(error);process.exit(1)}});
"""
    _run_node(harness, tmp_path, "p1-09c-mobile-stocktake-errors.js")


def test_mobile_stocktake_inline_javascript_is_valid(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node, "Node.js is required for JavaScript syntax validation"
    target = tmp_path / "mobile_stocktake.js"
    target.write_text(_script(), encoding="utf-8")
    result = subprocess.run(
        [node, "--check", str(target)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr
