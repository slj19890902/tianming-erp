from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MOBILE = (ROOT / "static" / "mobile_mold_lookup.html").read_text(encoding="utf-8")


def _script() -> str:
    match = re.search(r"<script>(.*?)</script>", MOBILE, re.DOTALL)
    assert match is not None
    return match.group(1)


def _between(start: str, end: str) -> str:
    source = _script()
    return source.split(start, 1)[1].split(end, 1)[0]


def _run_node(source: str, tmp_path: Path, name: str) -> None:
    node = shutil.which("node")
    if node is None:
        return
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


def test_mobile_mold_search_and_preview_have_latest_request_guards() -> None:
    for marker in (
        "let searchRequestId=0",
        "movePreviewRequestId=0",
        "requestId=++searchRequestId",
        "requestId!==searchRequestId",
        "requestId=++movePreviewRequestId",
        "requestId!==movePreviewRequestId",
        "invalidateMovePreview",
        "正在查询模具",
        "找到 ${items.length} 个模具",
        "连接 ERP 失败",
        "detail.message",
        "detail.code",
    ):
        assert marker in MOBILE


def test_mobile_mold_search_ignores_older_response(tmp_path: Path) -> None:
    search_source = "async function search(){" + _between(
        "async function search(){", "    function renderMovePreview()"
    )
    harness = f"""
let searchRequestId=0;
const nodes={{keyword:{{value:"A"}},message:{{innerHTML:""}},results:{{innerHTML:"old"}},searchButton:{{disabled:false,textContent:"查询"}}}};
const $=id=>nodes[id];
const h=value=>String(value??"");
let rendered=[];
function render(items){{rendered=items}}
function setSearchLoading(loading){{nodes.searchButton.disabled=loading;nodes.searchButton.textContent=loading?"查询中…":"查询"}}
function loginNext(){{throw new Error("unexpected login redirect")}}
const pending=[];
function api(url){{return new Promise((resolve,reject)=>pending.push({{url,resolve,reject}}))}}
{search_source}
(async()=>{{
  const first=search();
  nodes.keyword.value="B";
  const second=search();
  pending[1].resolve({{items:[{{mold_code:"B"}}]}});
  await second;
  if(rendered[0]?.mold_code!=="B")throw new Error("latest search must render first");
  pending[0].resolve({{items:[{{mold_code:"A"}}]}});
  await first;
  if(rendered[0]?.mold_code!=="B")throw new Error("older response must not overwrite latest search");
  if(nodes.searchButton.disabled)throw new Error("latest request must restore search button");
}})().catch(error=>{{console.error(error);process.exit(1)}});
"""
    _run_node(harness, tmp_path, "p1-09c-mobile-mold-search-race.js")


def test_mobile_mold_input_change_invalidates_pending_preview(tmp_path: Path) -> None:
    script = _script()
    invalidate_source = "function invalidateMovePreview(){" + script.split(
        "function invalidateMovePreview(){", 1
    )[1].split("    async function previewMove()", 1)[0]
    preview_source = "async function previewMove(){" + _between(
        "async function previewMove(){", "    async function confirmMove()"
    )
    harness = f"""
let canMove=true,movePreview=null,moveIdempotencyKey=null,moveSource="manual_input",movePreviewRequestId=0;
const nodes={{moveMoldCode:{{value:"MJ-1"}},moveLocationCode:{{value:"3F-M-R01-L1-D01-P01"}},moveMessage:{{innerHTML:""}},previewButton:{{disabled:false}},confirmButton:{{disabled:true}}}};
const $=id=>nodes[id];
const h=value=>String(value??"");
let keyCounter=0;
function createIdempotencyKey(){{keyCounter+=1;return `key-${{keyCounter}}`}}
function renderMovePreview(){{nodes.confirmButton.disabled=!movePreview}}
function loginNext(){{throw new Error("unexpected login redirect")}}
let resolvePreview;
function api(){{return new Promise(resolve=>{{resolvePreview=resolve}})}}
{invalidate_source}
{preview_source}
(async()=>{{
  const pending=previewMove();
  nodes.moveLocationCode.value="3F-M-R02-L1-D01-P01";
  invalidateMovePreview();
  resolvePreview({{mold:{{mold_code:"MJ-1"}},target_location:"3F-M-R01-L1-D01-P01",can_confirm:true}});
  await pending;
  if(movePreview!==null)throw new Error("stale preview must stay cleared after input change");
  if(!nodes.confirmButton.disabled)throw new Error("stale preview must not re-enable confirmation");
}})().catch(error=>{{console.error(error);process.exit(1)}});
"""
    _run_node(harness, tmp_path, "p1-09c-mobile-mold-preview-race.js")


def test_mobile_mold_error_parser_handles_object_and_network_failures(tmp_path: Path) -> None:
    script = _script()
    error_source = "function errorMessage(" + script.split(
        "function errorMessage(", 1
    )[1].split("    async function api(", 1)[0]
    api_source = "async function api(" + script.split(
        "async function api(", 1
    )[1].split("    function createIdempotencyKey()", 1)[0]
    harness = f"""
{error_source}
{api_source}
const expect=(value,message)=>{{if(!value)throw new Error(message)}};
expect(errorMessage({{detail:{{message:"目标位置已占用",code:"MOLD_LOCATION_OCCUPIED"}}}},409).includes("目标位置已占用"),"object message must be shown");
global.fetch=async()=>{{throw new TypeError("Failed to fetch")}};
(async()=>{{try{{await api("/offline")}}catch(error){{expect(error.message.includes("连接 ERP 失败"),"network failure must be Chinese");return}}throw new Error("network call must fail")}})().catch(error=>{{console.error(error);process.exit(1)}});
"""
    _run_node(harness, tmp_path, "p1-09c-mobile-mold-errors.js")
