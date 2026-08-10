import json
import shutil
import subprocess
from pathlib import Path


INDEX = Path("static/index.html").read_text(encoding="utf-8")


def _block(start_marker: str, end_marker: str) -> str:
    start = INDEX.index(start_marker)
    return INDEX[start:INDEX.index(end_marker, start)]


def _method_body(signature: str, next_signature: str) -> str:
    return INDEX.split(signature, 1)[1].split(next_signature, 1)[0].rsplit("}", 1)[0]


def _run_node(source: str, tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node is not None, "Node.js is required for production paging regression"
    target = tmp_path / "production-paging-race.js"
    target.write_text(source, encoding="utf-8")
    result = subprocess.run(
        [node, str(target)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_production_cold_entry_requests_only_pending_tasks() -> None:
    production = _block("async loadProduction()", "async ensureProductionLocations")

    assert 'params: { status: "pending", page, page_size: this.pageSize }' in production
    assert "return this.loadProductionPage(this.pages.productionPending || 1);" in production
    assert "/api/production/temporary-locations" not in production
    assert "loadProductionHistory" not in production
    assert 'this.beginLatestRequest("production:pending")' in production


def test_production_pending_uses_server_paging_and_preserves_last_good_page() -> None:
    production = _block("async loadProduction()", "async ensureProductionLocations")

    assert ':page="pages.productionPending"' in INDEX
    assert ':total="productionPendingTotal"' in INDEX
    assert '@change="loadProductionPage($event)"' in INDEX
    assert "const total = Number(pending.data.total || 0);" in production
    assert "this.productionPendingTotal = total;" in production
    assert "this.pages.productionPending = Number(pending.data.page || page);" in production
    assert 'latestRequestControllers.get("production:pending") !== controller' in production
    assert "if (page > lastPage) return this.loadProductionPage(lastPage);" in production
    assert "this.productionPending = []" not in production
    assert "待生产读取失败" in production
    assert "return false;" in production


def test_production_pending_latest_page_wins_and_failure_keeps_last_good_rows(
    tmp_path: Path,
) -> None:
    body = _method_body(
        "async loadProductionPage(requestedPage = 1) {",
        "async ensureProductionLocations",
    )
    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
global.latestRequestControllers=new Map();const pending=[];
global.axios={{get:(url,options)=>new Promise((resolve,reject)=>pending.push({{url,options,resolve,reject}}))}};
const vm={{
  pageSize:1,pages:{{productionPending:1}},productionPending:[{{id:1,version:1}}],productionPendingTotal:1,
  productionPendingLoading:false,productionPendingError:"",productionLocations:[],productionSelected:{{1:true}},orders:[],
  beginLatestRequest(key){{const previous=global.latestRequestControllers.get(key);previous?.abort();const controller={{signal:{{}},abort(){{this.aborted=true;}}}};global.latestRequestControllers.set(key,controller);return controller;}},
  finishLatestRequest(key,controller){{if(global.latestRequestControllers.get(key)===controller)global.latestRequestControllers.delete(key);}},
  isCancelledRequest(error){{return error?.code==="ERR_CANCELED";}},errorMessage(error){{return error?.message||String(error);}},
  productionLocationFloorNumber(){{return null;}},productionLocationAreaCode(){{return "";}}
}};
vm.loadProductionPage=new AsyncFunction("requestedPage",{json.dumps(body, ensure_ascii=False)}).bind(vm);
const expect=(value,message)=>{{if(!value)throw new Error(message)}};
(async()=>{{
  const first=vm.loadProductionPage(1);const second=vm.loadProductionPage(2);
  pending[0].resolve({{data:{{items:[{{id:10,version:1}}],total:2,page:1,page_size:1}}}});
  expect(await first===false,"stale production page was accepted");
  expect(vm.productionPending[0].id===1,"stale page replaced the last good rows");
  expect(vm.productionPendingLoading===true,"stale finally cleared the latest loading state");
  pending[1].resolve({{data:{{items:[{{id:20,version:1,available_material_input_quantity:5,planned_output_quantity:5}}],total:2,page:2,page_size:1}}}});
  expect(await second===true,"latest production page did not complete");
  expect(vm.productionPending[0].id===20&&vm.pages.productionPending===2,"latest page was not retained");
  expect(vm.productionPendingTotal===2&&!vm.productionSelected[1],"total or current-page selection cleanup failed");
  const failed=vm.loadProductionPage(1);pending[2].reject(new Error("offline"));
  expect(await failed===false,"failed page was reported as success");
  expect(vm.productionPending[0].id===20&&vm.pages.productionPending===2,"failed page cleared the last good result");
  expect(vm.productionPendingError.includes("offline")&&vm.productionPendingLoading===false,"failed page state is not retryable");
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path)


def test_production_locations_are_mode_driven_and_fail_closed() -> None:
    locations = _block("async ensureProductionLocations", "async loadProductionHistory")
    mode = _block("async ensureProductionMode(row)", "async onProductionLocationSelection")

    assert 'axios.get("/api/production/temporary-locations", {signal:controller.signal})' in locations
    assert 'this.beginLatestRequest("production:locations")' in locations
    assert 'this.productionLocationsError = "可用库位读取失败，请重试";' in locations
    assert "const locationsReady = await this.ensureProductionLocations();" in mode
    assert "if (!locationsReady) return;" in mode
    assert 'v-if="productionLocationsLoading"' in INDEX
    assert 'ensureProductionLocations({force:true})' in INDEX


def test_direct_delivery_without_surplus_keeps_fast_path_without_locations() -> None:
    mode = _block("async ensureProductionMode(row)", "async onProductionLocationSelection")

    assert 'if (this.productionNeedsLocation(row)) {' in mode
    assert 'if (row.completion_mode !== "direct") return;' in mode
    assert "await this.confirmProductionDirectRow(row);" in mode


def test_history_transfer_loads_locations_only_when_a_transfer_candidate_exists() -> None:
    history = _block("async loadProductionHistory()", "resetProductionHistoryFilters()")

    assert "row.can_transfer_to_stock" in history
    assert "&& !this.productionLocations.length" in history
    assert "await this.ensureProductionLocations();" in history
    assert "productionLocationsError" in INDEX
    assert "正在读取转库存可用库位" in INDEX
