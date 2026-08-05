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
    assert node is not None, "Node.js is required for the price-history regression"
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


def test_price_history_modal_distinguishes_loading_error_and_true_empty_state() -> None:
    modal = INDEX.split("<!-- 材质价格历史弹窗 -->", 1)[1].split("</template>", 1)[0]
    assert '@click.self="closePriceHistory()"' in modal
    assert '@click="closePriceHistory()"' in modal
    assert 'v-if="priceHistoryLoading"' in modal
    assert "价格历史加载中…" in modal
    assert 'v-else-if="priceHistoryError"' in modal
    assert '@click="retryPriceHistory()"' in modal
    assert "重新加载" in modal
    assert 'v-if="!(priceHistory.items||[]).length"' in modal


def test_price_history_latest_request_wins_and_old_finally_cannot_clear_loading(tmp_path: Path) -> None:
    body = _method_body("async openPriceHistory(row) {", "closePriceHistory() {")
    assert 'const requestKey = "materials:price-history";' in body
    assert "const materialSnapshot" in body
    assert "signal:controller.signal" in body
    assert "latestRequestControllers.get(requestKey) !== controller" in body
    assert "Number(this.priceHistory.material_id) !== materialSnapshot.material_id" in body

    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
global.latestRequestControllers=new Map();const pending=[];
global.axios={{get:(url,options)=>new Promise((resolve,reject)=>pending.push({{url,options,resolve,reject}}))}};
const vm={{
  showPriceHistoryModal:false,priceHistoryLoading:false,priceHistoryError:"",priceHistory:{{material_code:"",items:[]}},
  beginLatestRequest(key){{global.latestRequestControllers.get(key)?.abort();const controller={{signal:{{}},abort(){{this.aborted=true;}}}};global.latestRequestControllers.set(key,controller);return controller;}},
  finishLatestRequest(key,controller){{if(global.latestRequestControllers.get(key)===controller)global.latestRequestControllers.delete(key);}},
  isCancelledRequest(error){{return error?.name==="CanceledError";}},errorMessage(error){{return error?.message||String(error);}}
}};
vm.openPriceHistory=new AsyncFunction("row",{json.dumps(body, ensure_ascii=False)}).bind(vm);
const expect=(value,message)=>{{if(!value)throw new Error(message)}};
(async()=>{{
  const first=vm.openPriceHistory({{id:1,code:"AAA",supplier_name:"甲",quote_price:1.1}});
  const second=vm.openPriceHistory({{id:2,code:"BBB",supplier_name:"乙",quote_price:2.2}});
  pending[0].resolve({{data:{{material_code:"AAA",items:[{{id:1}}]}}}});
  expect(await first===false,"stale price-history request was accepted");
  expect(vm.priceHistoryLoading===true,"stale request finally cleared latest loading state");
  expect(vm.priceHistory.material_code==="BBB","stale response replaced current material identity");
  pending[1].resolve({{data:{{material_code:"BBB",supplier_name:"乙",current_price:2.2,items:[{{id:2}}]}}}});
  expect(await second===true,"latest price-history request did not complete");
  expect(vm.priceHistory.material_id===2&&vm.priceHistory.items[0].id===2,"latest history was not retained");
  expect(vm.priceHistoryLoading===false&&vm.priceHistoryError==="","latest request did not release cleanly");
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path, "price-history-latest.js")


def test_close_price_history_cancels_pending_request_and_blocks_late_result(tmp_path: Path) -> None:
    open_body = _method_body("async openPriceHistory(row) {", "closePriceHistory() {")
    close_body = _method_body("closePriceHistory() {", "retryPriceHistory() {")
    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
global.latestRequestControllers=new Map();let release;
global.axios={{get:()=>new Promise(resolve=>{{release=resolve;}})}};
const vm={{
  showPriceHistoryModal:false,priceHistoryLoading:false,priceHistoryError:"",priceHistory:{{material_code:"",items:[]}},
  beginLatestRequest(key){{const controller={{signal:{{}},abort(){{this.aborted=true;}}}};global.latestRequestControllers.set(key,controller);return controller;}},
  finishLatestRequest(key,controller){{if(global.latestRequestControllers.get(key)===controller)global.latestRequestControllers.delete(key);}},
  isCancelledRequest(){{return false;}},errorMessage(error){{return error?.message||String(error);}}
}};
vm.openPriceHistory=new AsyncFunction("row",{json.dumps(open_body, ensure_ascii=False)}).bind(vm);
vm.closePriceHistory=new Function({json.dumps(close_body, ensure_ascii=False)}).bind(vm);
const expect=(value,message)=>{{if(!value)throw new Error(message)}};
(async()=>{{
  const request=vm.openPriceHistory({{id:7,code:"C7",supplier_name:"甲",quote_price:3}});
  expect(vm.closePriceHistory()===true&&vm.showPriceHistoryModal===false,"history modal did not close");
  release({{data:{{material_code:"C7",items:[{{id:7}}]}}}});
  expect(await request===false,"closed modal accepted a late response");
  expect(vm.priceHistory.items.length===0&&vm.priceHistoryLoading===false,"closed modal retained stale state");
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path, "price-history-close.js")


def test_price_history_current_error_is_visible_and_retry_keeps_material_snapshot(tmp_path: Path) -> None:
    open_body = _method_body("async openPriceHistory(row) {", "closePriceHistory() {")
    retry_body = _method_body("retryPriceHistory() {", "// 常用箱纸板成本参考")
    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
global.latestRequestControllers=new Map();let calls=0;
global.axios={{get:async()=>{{calls+=1;throw new Error("服务器忙");}}}};
const vm={{
  showPriceHistoryModal:false,priceHistoryLoading:false,priceHistoryError:"",priceHistory:{{material_code:"",items:[]}},
  beginLatestRequest(key){{const controller={{signal:{{}},abort(){{}}}};global.latestRequestControllers.set(key,controller);return controller;}},
  finishLatestRequest(key,controller){{if(global.latestRequestControllers.get(key)===controller)global.latestRequestControllers.delete(key);}},
  isCancelledRequest(){{return false;}},errorMessage(error){{return error?.message||String(error);}}
}};
vm.openPriceHistory=new AsyncFunction("row",{json.dumps(open_body, ensure_ascii=False)}).bind(vm);
vm.retryPriceHistory=new Function({json.dumps(retry_body, ensure_ascii=False)}).bind(vm);
const expect=(value,message)=>{{if(!value)throw new Error(message)}};
(async()=>{{
  expect(await vm.openPriceHistory({{id:9,code:"D9",supplier_name:"乙",quote_price:4}})===false,"failed request reported success");
  expect(vm.priceHistoryError.includes("服务器忙")&&vm.priceHistory.material_id===9,"current error or material snapshot was lost");
  const retry=vm.retryPriceHistory();
  expect(retry&&typeof retry.then==="function","retry did not return the request promise");
  await retry;
  expect(calls===2&&vm.priceHistory.material_id===9&&vm.priceHistory.material_code==="D9","retry changed material identity");
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path, "price-history-error-retry.js")


def test_price_history_preserves_existing_endpoint_and_read_only_contract() -> None:
    body = _method_body("async openPriceHistory(row) {", "closePriceHistory() {")
    assert "/api/master/materials/${materialSnapshot.material_id}/price-history" in body
    assert "axios.get" in body
    assert "axios.post" not in body
    assert "axios.patch" not in body
    assert "axios.delete" not in body
