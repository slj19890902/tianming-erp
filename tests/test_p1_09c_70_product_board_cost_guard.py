from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def _method_body(signature: str, next_signature: str) -> str:
    assert signature in INDEX, f"missing Vue method: {signature}"
    assert next_signature in INDEX, f"missing Vue method boundary: {next_signature}"
    return INDEX.split(signature, 1)[1].split(next_signature, 1)[0].rsplit("}", 1)[0]


def _run_node(source: str, tmp_path: Path, name: str) -> None:
    node = shutil.which("node")
    assert node is not None, "Node.js is required for the board-cost regression"
    target = tmp_path / name
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


def test_board_cost_ui_and_watchers_expose_truthful_debounced_state() -> None:
    product_material = INDEX.split('<div class="field product-material-picker">', 1)[1].split('<section class="product-material-context-panel">', 1)[0]
    assert "productBoardCostLoading" in product_material
    assert "正在计算纸板成本…" in product_material
    assert "productBoardCostError" in product_material
    assert '@click="refreshProductBoardCost()"' in product_material
    assert "重新计算" in product_material

    watch = INDEX.split("watch: {", 1)[1].split("priceAdjustForm:", 1)[0]
    for field in ("material_id", "box_category", "length_mm", "width_mm", "height_mm", "layer_count", "flute_type"):
        line = next(row for row in watch.splitlines() if f'"productForm.{field}"' in row)
        assert "queueProductBoardCost()" in line
        assert "refreshProductBoardCost()" not in line


def test_board_cost_changes_are_debounced_into_one_request(tmp_path: Path) -> None:
    queue_body = _method_body("queueProductBoardCost() {", "async refreshProductBoardCost() {")
    assert 'const timerKey = "preview:product-board-cost";' in queue_body
    assert "searchDebounceTimers.set" in queue_body
    assert "setTimeout" in queue_body

    script = f"""
global.searchDebounceTimers=new Map();const timers=[];
global.setTimeout=(callback,delay)=>{{const timer={{callback,delay,cancelled:false}};timers.push(timer);return timer;}};
global.clearTimeout=(timer)=>{{timer.cancelled=true;}};
const vm={{calls:0,invalidateProductBoardCost(){{const timer=global.searchDebounceTimers.get("preview:product-board-cost");if(timer)clearTimeout(timer);global.searchDebounceTimers.delete("preview:product-board-cost");}},productBoardCostRequestSnapshot(){{return {{key:"ready",body:{{}}}};}},refreshProductBoardCost(){{this.calls+=1;}}}};
vm.queueProductBoardCost=new Function({json.dumps(queue_body, ensure_ascii=False)}).bind(vm);
const expect=(value,message)=>{{if(!value)throw new Error(message)}};
vm.queueProductBoardCost();vm.queueProductBoardCost();vm.queueProductBoardCost();
expect(timers.length===3,"expected each input to replace the debounce timer");
expect(timers.filter(timer=>!timer.cancelled).length===1,"more than one debounce timer remained active");
expect(vm.calls===0,"cost request ran before debounce elapsed");
timers.find(timer=>!timer.cancelled).callback();
expect(vm.calls===1,"latest debounce did not trigger exactly one calculation");
"""
    _run_node(script, tmp_path, "board-cost-debounce.js")


def test_board_cost_latest_snapshot_wins_and_old_finally_cannot_clear_loading(tmp_path: Path) -> None:
    snapshot_body = _method_body("productBoardCostRequestSnapshot() {", "invalidateProductBoardCost() {")
    refresh_body = _method_body("async refreshProductBoardCost() {", "// 默认楞型")
    assert 'const requestKey = "materials:board-cost";' in refresh_body
    assert "signal:controller.signal" in refresh_body
    assert "this.productBoardCostRequestSnapshot()?.key !== requestSnapshot.key" in refresh_body
    assert "latestRequestControllers.get(requestKey) === controller" in refresh_body

    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
global.latestRequestControllers=new Map();global.searchDebounceTimers=new Map();const pending=[];
global.axios={{post:(url,body,options)=>new Promise((resolve,reject)=>pending.push({{url,body,options,resolve,reject}}))}};
const vm={{
  modal:{{type:"product"}},isWorkshop:false,
  allMaterials:[{{id:1,quote_price:1,supplier_name:"甲",layer_count:3}},{{id:2,quote_price:2,supplier_name:"乙",layer_count:3}}],materials:[],
  productForm:{{id:10,material_id:1,box_category:"normal",length_mm:400,width_mm:300,height_mm:200,layer_count:3,flute_type:"A"}},
  productBoardCost:null,productBoardCostDetail:null,productBoardCostLoading:false,productBoardCostError:"",
  beginLatestRequest(key){{global.latestRequestControllers.get(key)?.abort();const controller={{signal:{{}},abort(){{this.aborted=true;}}}};global.latestRequestControllers.set(key,controller);return controller;}},
  finishLatestRequest(key,controller){{if(global.latestRequestControllers.get(key)===controller)global.latestRequestControllers.delete(key);}},
  isCancelledRequest(error){{return error?.name==="CanceledError";}},errorMessage(error){{return error?.message||String(error);}}
}};
vm.productBoardCostRequestSnapshot=new Function({json.dumps(snapshot_body, ensure_ascii=False)}).bind(vm);
vm.refreshProductBoardCost=new AsyncFunction({json.dumps(refresh_body, ensure_ascii=False)}).bind(vm);
const expect=(value,message)=>{{if(!value)throw new Error(message)}};
(async()=>{{
  const first=vm.refreshProductBoardCost();
  vm.productForm.material_id=2;
  const second=vm.refreshProductBoardCost();
  pending[0].resolve({{data:{{supported:true,board_cost:111}}}});
  expect(await first===false,"stale cost request was accepted");
  expect(vm.productBoardCostLoading===true,"stale finally cleared current loading state");
  pending[1].resolve({{data:{{supported:true,board_cost:222}}}});
  expect(await second===true,"latest cost request did not complete");
  expect(vm.productBoardCost===222&&vm.productBoardCostDetail.board_cost===222,"latest cost was not retained");
  expect(vm.productBoardCostLoading===false&&vm.productBoardCostError==="","latest request did not release cleanly");
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path, "board-cost-latest.js")


def test_invalid_or_closed_product_form_cancels_request_and_blocks_late_result(tmp_path: Path) -> None:
    snapshot_body = _method_body("productBoardCostRequestSnapshot() {", "invalidateProductBoardCost() {")
    invalidate_body = _method_body("invalidateProductBoardCost() {", "queueProductBoardCost() {")
    refresh_body = _method_body("async refreshProductBoardCost() {", "// 默认楞型")
    close_body = _method_body("closeModal() {", "handleMasterSaveRefreshFailure(")
    assert 'if (this.modal?.type === "product") this.invalidateProductBoardCost();' in close_body

    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
global.latestRequestControllers=new Map();global.searchDebounceTimers=new Map();let release;
global.axios={{post:()=>new Promise(resolve=>{{release=resolve;}})}};
const vm={{
  modal:{{type:"product"}},isWorkshop:false,allMaterials:[{{id:1,quote_price:1,supplier_name:"甲",layer_count:3}}],materials:[],
  productForm:{{id:10,material_id:1,box_category:"normal",length_mm:400,width_mm:300,height_mm:200,layer_count:3,flute_type:"A"}},
  productBoardCost:null,productBoardCostDetail:null,productBoardCostLoading:false,productBoardCostError:"",
  beginLatestRequest(key){{const controller={{signal:{{}},abort(){{this.aborted=true;}}}};global.latestRequestControllers.set(key,controller);return controller;}},
  finishLatestRequest(key,controller){{if(global.latestRequestControllers.get(key)===controller)global.latestRequestControllers.delete(key);}},
  isCancelledRequest(){{return false;}},errorMessage(error){{return error?.message||String(error);}}
}};
vm.productBoardCostRequestSnapshot=new Function({json.dumps(snapshot_body, ensure_ascii=False)}).bind(vm);
vm.invalidateProductBoardCost=new Function({json.dumps(invalidate_body, ensure_ascii=False)}).bind(vm);
vm.refreshProductBoardCost=new AsyncFunction({json.dumps(refresh_body, ensure_ascii=False)}).bind(vm);
const expect=(value,message)=>{{if(!value)throw new Error(message)}};
(async()=>{{
  const request=vm.refreshProductBoardCost();
  vm.modal=null;vm.invalidateProductBoardCost();
  release({{data:{{supported:true,board_cost:999}}}});
  expect(await request===false,"closed product form accepted a late cost response");
  expect(vm.productBoardCost===null&&vm.productBoardCostDetail===null&&vm.productBoardCostLoading===false,"closed form retained stale cost state");
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path, "board-cost-close.js")


def test_board_cost_failure_is_visible_and_contract_remains_read_only() -> None:
    refresh_body = _method_body("async refreshProductBoardCost() {", "// 默认楞型")
    assert "productBoardCostError" in refresh_body
    assert "纸板成本计算失败" in refresh_body
    assert "axios.post" in refresh_body
    assert '"/api/master/materials/board-cost"' in refresh_body
    assert "axios.patch" not in refresh_body
    assert "axios.delete" not in refresh_body
