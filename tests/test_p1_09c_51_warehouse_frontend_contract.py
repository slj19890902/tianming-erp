from __future__ import annotations

from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]
WAREHOUSE = (ROOT / "static" / "warehouse.html").read_text(encoding="utf-8")


def _function_source(name: str) -> str:
    start = WAREHOUSE.index(f"function {name}(")
    next_sync = WAREHOUSE.find("\n    function ", start + 1)
    next_async = WAREHOUSE.find("\n    async function ", start + 1)
    candidates = [position for position in (next_sync, next_async) if position >= 0]
    end = min(candidates) if candidates else len(WAREHOUSE)
    return WAREHOUSE[start:end]


def _async_function_source(name: str) -> str:
    start = WAREHOUSE.index(f"async function {name}(")
    next_sync = WAREHOUSE.find("\n    function ", start + 1)
    next_async = WAREHOUSE.find("\n    async function ", start + 1)
    candidates = [position for position in (next_sync, next_async) if position >= 0]
    end = min(candidates) if candidates else len(WAREHOUSE)
    return WAREHOUSE[start:end]


def _run_node(source: str, tmp_path: Path, name: str) -> None:
    script = tmp_path / name
    script.write_text(source, encoding="utf-8")
    completed = subprocess.run(
        ["node", str(script)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_inventory_lot_loader_has_explicit_state_and_rejects_stale_results() -> None:
    method = _async_function_source("loadLots")

    assert "requestId=++state.loadRequests.lots" in method
    assert "const inventoryType=state.tab" in method
    assert "warehouseLotQuery(inventoryType)" in method
    assert "requestId!==state.loadRequests.lots" in method
    assert "inventoryType!==state.tab" in method
    assert "query!==warehouseLotQuery(inventoryType)" in method
    assert "正在加载库存批次" in method
    assert "正在刷新库存，保留当前结果" in method
    assert "库存刷新失败，仍显示上次结果" in method


def test_inventory_lot_loader_runtime_keeps_latest_filter_result(tmp_path: Path) -> None:
    functions = "\n".join(
        (
            _function_source("queryString"),
            _function_source("warehouseLotQuery"),
            _async_function_source("loadLots"),
        )
    )
    harness = f"""
const nodes={{
  floorFilter:{{value:""}},areaFilter:{{value:""}},statusFilter:{{value:""}},lotCustomerFilter:{{value:""}},
  locationFilter:{{value:""}},staleFilter:{{value:""}},keywordFilter:{{value:"旧"}},locationKeywordFilter:{{value:""}},palletKeywordFilter:{{value:""}},
  finishedProductCodeFilter:{{value:""}},finishedProductNameFilter:{{value:""}},finishedSpecFilter:{{value:""}},
  semiSupplierFilter:{{value:""}},semiMaterialFilter:{{value:""}},semiFluteFilter:{{value:""}},semiLengthFilter:{{value:""}},semiWidthFilter:{{value:""}},semiAllowedProductFilter:{{value:""}},
  lotFilterSummary:{{textContent:""}},lotPrevPage:{{disabled:false}},lotNextPage:{{disabled:false}},
}};
const $=id=>nodes[id];
const state={{tab:"finished",lotPage:1,lotPageSize:25,lotTotal:0,lots:[],loadRequests:{{lots:0}}}};
const pending=[];
const api=url=>new Promise(resolve=>pending.push({{url,resolve}}));
let rendered=0;
function renderLotLoading(message){{nodes.lotFilterSummary.textContent=message}}
function renderLots(){{rendered+=1}}
function renderLotPager(){{}}
function toast(){{}}
{functions}
(async()=>{{
  const first=loadLots();
  nodes.keywordFilter.value="新";
  const second=loadLots();
  pending[1].resolve({{items:[{{id:2}}],total:1,page:1,page_size:25}});
  await second;
  pending[0].resolve({{items:[{{id:1}}],total:1,page:1,page_size:25}});
  await first;
  if(state.lots.length!==1||state.lots[0].id!==2)throw new Error("旧筛选响应覆盖了新结果");
  if(rendered!==1)throw new Error(`应只渲染最新响应，实际 ${{rendered}} 次`);
}})().catch(error=>{{console.error(error);process.exit(1)}});
"""
    _run_node(harness, tmp_path, "warehouse-lot-race.js")


def test_floor3_formal_mutations_are_single_flight_and_report_refresh_failure() -> None:
    assert "operationPending:new Set()" in WAREHOUSE
    helper = _async_function_source("runWarehouseMutation")
    assert "state.operationPending.has(key)" in helper
    assert "正在处理，请勿重复点击" in helper
    assert "业务已成功，请勿重复提交" in helper

    for name in (
        "saveFloor3Pallet",
        "saveFloor3AdditionalItem",
        "confirmFloor3MapMove",
        "moveFloor3Pallet",
        "clearFloor3Pallet",
        "setFloor3Relocation",
        "confirmFloor3Placement",
    ):
        method = _async_function_source(name)
        assert "runWarehouseMutation(" in method, f"{name} 未接入统一单飞门禁"

    promotion = _async_function_source("promoteFloor3FinishedItem")
    assert "业务已成功，请勿重复提交" in promotion
    assert "promotionPending" in promotion


def test_inventory_quantity_mutations_block_duplicates_and_do_not_misreport_refresh_failure() -> None:
    for name in ("saveFinished", "saveSemi", "operate"):
        method = _async_function_source(name)
        assert "runWarehouseMutation(" in method, f"{name} 未接入统一单飞门禁"

    for name in ("saveLotEditor", "saveStagingTransfer"):
        method = _async_function_source(name)
        assert "saving)return" in method, f"{name} 未保留表单单飞门禁"
        assert "业务已成功，请勿重复提交" in method, f"{name} 未区分写入成功后的刷新失败"

    assert "lot-operation:${id}:${operation}" in _async_function_source("operate")


def test_mutation_runtime_blocks_duplicate_and_distinguishes_refresh_failure(tmp_path: Path) -> None:
    helper = _async_function_source("runWarehouseMutation")
    harness = f"""
const state={{operationPending:new Set()}};
const messages=[];
function toast(message,error=false){{messages.push({{message,error}})}}
let requestCount=0,releaseRequest;
const request=()=>{{requestCount+=1;return new Promise(resolve=>{{releaseRequest=resolve}})}};
{helper}
(async()=>{{
  const first=runWarehouseMutation("move:1",request,async()=>{{throw new Error("断网")}},"栈板移动成功");
  const duplicate=await runWarehouseMutation("move:1",request,null,"栈板移动成功");
  if(duplicate.started!==false||requestCount!==1)throw new Error("重复点击未被单飞门禁阻止");
  releaseRequest({{id:1}});
  const result=await first;
  if(!result.succeeded||!result.refreshFailed)throw new Error("写入成功后的刷新失败未被单独标记");
  if(!messages.some(row=>row.message.includes("业务已成功，请勿重复提交")))throw new Error("缺少成功后刷新失败提示");
  if(state.operationPending.size!==0)throw new Error("操作结束后未释放单飞状态");
}})().catch(error=>{{console.error(error);process.exit(1)}});
"""
    _run_node(harness, tmp_path, "warehouse-mutation-guard.js")


def test_floor3_loading_preserves_current_rows_and_exposes_error() -> None:
    method = _async_function_source("loadFloor3Locations")
    render = _function_source("renderFloor3Locations")

    assert "state.floor3.loading=true" in method
    assert "state.floor3.error=\"\"" in method
    assert "state.floor3.loading=false" in method
    assert "state.floor3.error=error.message" in method
    assert "正在刷新货位，保留当前结果" in render
    assert "三楼货位加载失败" in render
    assert 'state.tab!=="locations"' in method
    assert 'state.locationView!=="floor3"' in method


def test_secondary_warehouse_tabs_have_loading_error_and_stale_guards() -> None:
    locations = _async_function_source("loadLocations")
    insights = _async_function_source("loadInsights")
    movements = _async_function_source("loadMovements")
    molds = _async_function_source("loadMolds")

    for name, method in (
        ("locations", locations),
        ("insights", insights),
        ("movements", movements),
        ("molds", molds),
    ):
        assert f"++state.loadRequests.{name}" in method
        assert f"state.loadRequests.{name}" in method

    for phrase in (
        "正在加载全部库位台账",
        "库位台账加载失败",
        "正在加载库存看板",
        "库存看板加载失败",
        "正在加载库存流水",
        "库存流水加载失败",
        "正在加载模具位置",
        "模具位置加载失败",
    ):
        assert phrase in WAREHOUSE
