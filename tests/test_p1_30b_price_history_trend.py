from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def _method_body(signature: str, next_signature: str) -> str:
    assert signature in INDEX
    assert next_signature in INDEX
    return INDEX.split(signature, 1)[1].split(next_signature, 1)[0].rsplit("}", 1)[0]


def _run_node(source: str, tmp_path: Path, name: str) -> None:
    node = shutil.which("node")
    assert node is not None, "Node.js is required for the price-history trend regression"
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


def test_price_history_modal_uses_step_chart_summary_filters_and_accountable_table() -> None:
    modal = INDEX.split("<!-- 材质价格历史弹窗 -->", 1)[1].split(
        '<template v-else-if="activePage === \'orders\'">', 1
    )[0]
    assert "price-history-summary" in modal
    assert "当前有效价" in modal
    assert "上次有效价" in modal
    assert "下个待生效价" in modal
    assert 'v-model="priceHistoryFilter.range"' in modal
    assert 'v-model="priceHistoryFilter.status"' in modal
    assert "priceHistoryEffectiveStepPath" in modal
    assert "priceHistoryPendingStepPath" in modal
    assert "<polyline" not in modal
    for header in ("生效日期", "记录时间", "旧价", "新价", "涨跌", "状态", "账号"):
        assert header in modal
    assert "点击节点可定位下方明细" in modal
    assert "overflow-x:hidden" in INDEX
    assert ".ui-large .price-history-table" in INDEX


def test_price_history_request_uses_recent_range_status_and_bounded_pages(
    tmp_path: Path,
) -> None:
    body = _method_body("async openPriceHistory(row) {", "closePriceHistory() {")
    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
global.latestRequestControllers=new Map();const calls=[];
global.axios={{get:async(url,options)=>{{calls.push({{url,options}});return {{data:{{material_id:31,material_code:"A+A",supplier_name:"供应商甲",current_price:4.9,total:75,items:[]}}}};}}}};
const vm={{
  showPriceHistoryModal:false,priceHistoryLoading:false,priceHistoryError:"",priceHistory:{{material_id:null,items:[]}},
  priceHistoryFilter:{{range:"12m",status:"all"}},priceHistoryPage:1,priceHistoryPageSize:50,priceHistorySelectedId:null,
  beginLatestRequest(key){{const controller={{signal:{{}},abort(){{}}}};global.latestRequestControllers.set(key,controller);return controller;}},
  finishLatestRequest(key,controller){{if(global.latestRequestControllers.get(key)===controller)global.latestRequestControllers.delete(key);}},
  isCancelledRequest(){{return false;}},errorMessage(error){{return error?.message||String(error);}},showToast(){{}}
}};
vm.openPriceHistory=new AsyncFunction("row",{json.dumps(body, ensure_ascii=False)}).bind(vm);
const expect=(value,message)=>{{if(!value)throw new Error(message)}};
(async()=>{{
  expect(await vm.openPriceHistory({{id:31,code:"A+A",supplier_name:"供应商甲",quote_price:4.9}})===true,"initial load failed");
  const first=calls[0].options.params;
  expect(first.limit===50&&first.offset===0&&first.order==="desc","initial page was not bounded newest-first");
  expect(first.supplier_name==="供应商甲"&&/^\\d{{4}}-\\d{{2}}-\\d{{2}}$/.test(first.date_from),"supplier or recent range missing");
  vm.priceHistoryFilter={{range:"12m",status:"time_unknown"}};vm.priceHistoryPage=2;
  expect(await vm.openPriceHistory({{id:31,code:"A+A",supplier_name:"供应商甲",quote_price:4.9}})===true,"unknown-time load failed");
  const second=calls[1].options.params;
  expect(second.offset===50&&second.effective_status==="time_unknown","status page filter missing");
  expect(!("date_from" in second),"unknown-time records were incorrectly constrained by guessed dates");
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path, "p1-30b-price-history-query.js")
