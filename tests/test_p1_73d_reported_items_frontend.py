from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def _method_body(name: str) -> str:
    match = re.search(
        rf"(?m)^\s{{10}}(?:async\s+)?{re.escape(name)}\([^\n]*\)\s*\{{",
        INDEX,
    )
    assert match is not None, f"missing Vue method: {name}"
    next_method = re.search(
        r"(?m)^\s{10}(?:async\s+)?[A-Za-z_$][A-Za-z0-9_$]*\([^\n]*\)\s*\{",
        INDEX[match.end() :],
    )
    assert next_method is not None, f"cannot delimit Vue method: {name}"
    return INDEX[match.end() : match.end() + next_method.start()].rsplit("}", 1)[0]


def _run_node(source: str, tmp_path: Path, name: str) -> None:
    node = shutil.which("node")
    assert node is not None, "Node.js is required for P1-73D frontend regressions"
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


def test_compact_reported_item_table_drawer_and_fixed_columns_are_present() -> None:
    block = INDEX.split('aria-label="已报料逐明细紧凑表格"', 1)[1].split(
        'aria-label="已报料明细详情"', 1
    )[0]
    for heading in (
        "序号",
        "报料长",
        "报料宽",
        "压线尺寸 / 净毛",
        "采购数量",
        "材质 / 楞型",
        "客户简称 / 存货编码",
        "报料日期",
        "供应商",
        "状态",
    ):
        assert heading in block
    assert "操作</th>" not in block
    assert '<template v-for="row in requisitionItems" :key="row.stable_id">' in block
    assert "组合父件：" in block
    assert "撤销整组报料" in block
    assert '@click="openReportedItemDetail(row)"' in block
    assert "查看采购单并定位本行" in INDEX
    assert "supplier-order-line-" in INDEX
    assert ".reported-item-table thead th { position: sticky" in INDEX
    assert ".reported-item-select-column { position: sticky; left: 0" in INDEX
    assert ".reported-item-action-column { position: sticky; right: 0" not in INDEX
    assert "height: 38px" in INDEX
    assert ".reported-item-dimension { width: 92px" in INDEX
    assert ".ui-large .reported-item-table th" in INDEX and "height: 42px" in INDEX


def test_reported_item_loader_is_server_paged_sorted_and_latest_wins(
    tmp_path: Path,
) -> None:
    load = _method_body("loadReportedDocuments")
    current = _method_body("reportedItemsRequestIsCurrent")
    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
global.latestRequestControllers=new Map();
const pending=[];
global.axios={{get:(url,options)=>new Promise((resolve,reject)=>pending.push({{url,options,resolve,reject}}))}};
const vm={{
  activePage:'requisition',requisitionWorkspace:'board',requisitionTab:'submitted',authGeneration:4,user:{{id:9}},
  pageSize:25,pages:{{requisitionReported:2}},reportedSort:{{by:'report_width_mm',direction:'asc'}},
  reportedFilters:{{customer_id:7,product_code:'ABC'}},reportedItemLoading:false,reportedItemError:'',
  requisitionItems:[{{stable_id:'last-good'}}],requisitionReportedTotal:1,requisitionReportedMatchedLines:1,
  requisitionReportedLoaded:true,stockReplenishmentVoidState:{{uncertainIds:{{}}}},
  beginLatestRequest(key){{const controller=new AbortController();latestRequestControllers.set(key,controller);return controller;}},
  finishLatestRequest(key,controller){{if(latestRequestControllers.get(key)===controller)latestRequestControllers.delete(key);}},
  isCancelledRequest(error){{return error?.name==='AbortError';}},errorMessage(error){{return error.message;}},
  resetPagePerformanceState(){{throw new Error('unexpected reset');}},showToast(){{}},
}};
vm.reportedItemsRequestIsCurrent=new Function('controller','authGeneration','userId',{json.dumps(current, ensure_ascii=False)}).bind(vm);
vm.loadReportedDocuments=new AsyncFunction({json.dumps(load, ensure_ascii=False)}).bind(vm);
const expect=(value,message)=>{{if(!value)throw new Error(message);}};
(async()=>{{
  const old=vm.loadReportedDocuments();const latest=vm.loadReportedDocuments();
  expect(pending.length===2,'two request generations were not started');
  expect(pending[1].url==='/api/requisition/reported-items','new item endpoint was not used');
  expect(pending[1].options.params.sort_by==='report_width_mm'&&pending[1].options.params.sort_direction==='asc','server sort was omitted');
  expect(pending[1].options.params.customer_id===7&&pending[1].options.params.product_code==='ABC','server filters were omitted');
  pending[0].resolve({{data:{{items:[{{stable_id:'stale'}}],total:1}}}});
  expect(await old===false,'stale request reported success');
  expect(vm.requisitionItems[0].stable_id==='last-good','stale response replaced last-good rows');
  pending[1].resolve({{data:{{items:[{{stable_id:'fresh'}}],total:30}}}});
  expect(await latest===true,'latest request did not succeed');
  expect(vm.requisitionItems[0].stable_id==='fresh'&&vm.requisitionReportedTotal===30,'latest result was not applied');
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path, "p1-73d-latest-wins.js")


def test_cross_page_selection_is_explicit_ordered_and_clearable(tmp_path: Path) -> None:
    names = [
        "reportedSelectedItems",
        "reportedItemSelected",
        "toggleReportedItemSelection",
        "reportedCurrentPageAllSelected",
        "toggleReportedCurrentPage",
        "clearReportedItemSelections",
    ]
    bodies = {name: _method_body(name) for name in names}
    script = f"""
const vm={{reportedItemSelections:{{}},reportedItemSelectionSequence:0,reportedItemPrintBusy:false,requisitionItems:[],
  productionPrintAttemptUncertain(){{return false;}},resetProductionPrintBatchOutcome(){{}},reportedItemPrintErrors:[]}};
const methods={json.dumps(bodies, ensure_ascii=False)};
vm.reportedSelectedItems=new Function(methods.reportedSelectedItems).bind(vm);
vm.reportedItemSelected=new Function('row',methods.reportedItemSelected).bind(vm);
vm.toggleReportedItemSelection=new Function('row','checked',methods.toggleReportedItemSelection).bind(vm);
vm.reportedCurrentPageAllSelected=new Function(methods.reportedCurrentPageAllSelected).bind(vm);
vm.toggleReportedCurrentPage=new Function('checked',methods.toggleReportedCurrentPage).bind(vm);
vm.clearReportedItemSelections=new Function(methods.clearReportedItemSelections).bind(vm);
const expect=(value,message)=>{{if(!value)throw new Error(message);}};
const first={{stable_id:'supplier_order:1:11',item_id:11}};const second={{stable_id:'supplier_order:1:12',item_id:12}};const third={{stable_id:'supplier_order:2:21',item_id:21}};
vm.requisitionItems=[first,second];vm.toggleReportedCurrentPage(true);
expect(vm.reportedCurrentPageAllSelected(),'current-page all selection failed');
vm.requisitionItems=[third];vm.toggleReportedItemSelection(third,true);
expect(vm.reportedSelectedItems().map(row=>row.item_id).join(',')==='11,12,21','cross-page selection order was not preserved');
expect(!vm.reportedCurrentPageAllSelected()===false,'selected next page was not recognized');
vm.toggleReportedItemSelection(second,false);
expect(vm.reportedSelectedItems().map(row=>row.item_id).join(',')==='11,21','one removal polluted other pages');
expect(vm.clearReportedItemSelections()&&vm.reportedSelectedItems().length===0,'clear selection failed');
"""
    _run_node(script, tmp_path, "p1-73d-cross-page-selection.js")


def test_item_void_reuses_same_idempotency_key_after_uncertain_result(
    tmp_path: Path,
) -> None:
    names = [
        "reportedItemVoidAttempt",
        "reportedItemVoidBusy",
        "reportedItemVoidBlocked",
        "reportedItemVoidSaving",
        "reportedItemVoidButtonText",
        "voidReportedItem",
    ]
    bodies = {name: _method_body(name) for name in names}
    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
global.confirm=()=>true;global.createIdempotencyKey=()=> 'same-key';
const calls=[];let first=true;
global.axios={{put:async(url,payload)=>{{calls.push({{url,payload}});if(first){{first=false;throw new Error('network lost');}}return {{data:{{status:'voided'}}}};}}}};
const row={{stable_id:'supplier_order:5:51',source_type:'supplier_order',document_id:5,document_number:'SRO-5',item_id:51,version:3,can_void_item:true,report_length_mm:600,report_width_mm:400,material_code:'A+B',flute_type:'B',requisition_qty:20,unit:'张'}};
const vm={{reportedItemVoidAttempts:{{}},reportedItemSelections:{{[row.stable_id]:row}},reportedItemDetail:null,authGeneration:2,user:{{id:8}},toasts:[],
  showToast(message){{this.toasts.push(String(message));}},errorMessage(error){{return error.message;}},resetPagePerformanceState(){{throw new Error('unexpected reset');}},
  async loadReportedDocuments(){{return true;}},
}};
const methods={json.dumps(bodies, ensure_ascii=False)};
vm.reportedItemVoidAttempt=new Function('row',methods.reportedItemVoidAttempt).bind(vm);
vm.reportedItemVoidBusy=new Function('row',methods.reportedItemVoidBusy).bind(vm);
vm.reportedItemVoidBlocked=new Function(methods.reportedItemVoidBlocked).bind(vm);
vm.reportedItemVoidSaving=new Function(methods.reportedItemVoidSaving).bind(vm);
vm.reportedItemVoidButtonText=new Function('row',methods.reportedItemVoidButtonText).bind(vm);
vm.voidReportedItem=new AsyncFunction('row',methods.voidReportedItem).bind(vm);
const expect=(value,message)=>{{if(!value)throw new Error(message);}};
(async()=>{{
  expect(await vm.voidReportedItem(row)===false,'uncertain request reported success');
  expect(vm.reportedItemVoidAttempt(row).uncertain===true,'network uncertainty was not retained');
  expect(vm.reportedItemVoidButtonText(row)==='重试核对','uncertain retry was not exposed');
  expect(await vm.voidReportedItem(row)===true,'same-key retry did not recover');
  expect(calls.length===2&&calls[0].payload.idempotency_key===calls[1].payload.idempotency_key,'retry generated a different idempotency key');
  expect(calls[0].payload.expected_version===3&&calls[1].payload.expected_version===3,'retry changed frozen version');
  expect(!vm.reportedItemSelections[row.stable_id]&&!vm.reportedItemVoidAttempt(row),'success did not clear selection and attempt');
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path, "p1-73d-item-void-retry.js")


def test_task_print_maps_only_fully_selected_physical_lines(tmp_path: Path) -> None:
    candidates = _method_body("reportedProductionCandidates")
    prepare = _method_body("prepareReportedItemTaskPrint")
    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
const calls=[];
global.axios={{get:async(url)=>{{calls.push(url);return {{data:{{supplier_order_number:'SRO-1',cards:[{{source_identity:'order_item:7',selection_fingerprint:'f'.repeat(64),production_task_versions:[{{task_id:91,version:2}}],selection_eligible:true,components:[{{supplier_order_item_id:11}},{{supplier_order_item_id:12}}],product_code:'BOX'}}]}}}};}}}};
const rows=[11,12].map(item_id=>({{stable_id:`supplier_order:1:${{item_id}}`,source_type:'supplier_order',status:'active',can_print_task:true,document_id:1,document_number:'SRO-1',item_id}}));
const vm={{activePage:'requisition',requisitionTab:'submitted',authGeneration:1,user:{{id:3}},reportedItemPrintBusy:false,reportedItemPrintErrors:[],reportedLabelRecoveryUrls:[],productionPrintBatchAttempt:null,productionPrintRecoveryUrl:'',productionPrintSelections:{{}},productionPrintSelectionSequence:0,
  reportedSelectedItems(){{return this.selection;}},selection:rows,productionPrintAttemptUncertain(){{return false;}},resetProductionPrintBatchOutcome(){{}},
  async prepareProductionPrintBatch(){{this.prepared=true;return true;}},
}};
vm.reportedProductionCandidates=new Function('orderId','row','data',{json.dumps(candidates, ensure_ascii=False)}).bind(vm);
vm.prepareReportedItemTaskPrint=new AsyncFunction({json.dumps(prepare, ensure_ascii=False)}).bind(vm);
const expect=(value,message)=>{{if(!value)throw new Error(message);}};
(async()=>{{
  expect(await vm.prepareReportedItemTaskPrint()===true,'fully selected task was not prepared');
  expect(calls.length===1&&vm.prepared===true,'task package was not loaded exactly once');
  expect(Object.keys(vm.productionPrintSelections).length===1,'one physical production card was duplicated');
  vm.selection=[rows[0]];vm.prepared=false;vm.productionPrintSelections={{}};vm.reportedItemPrintErrors=[];
  expect(await vm.prepareReportedItemTaskPrint()===false,'partial shared task selection was accepted');
  expect(vm.reportedItemPrintErrors.some(message=>message.includes('一并勾选')),'partial shared task did not explain its blocker');
  expect(vm.prepared===false&&Object.keys(vm.productionPrintSelections).length===0,'invalid task selection was partially submitted');
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path, "p1-73d-task-print-selection.js")


def test_composite_reported_component_opens_task_package_and_batch(
    tmp_path: Path,
) -> None:
    candidates = _method_body("reportedProductionCandidates")
    prepare = _method_body("prepareReportedItemTaskPrint")
    payload_items = _method_body("productionPrintPayloadItems")
    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
const calls=[];
global.axios={{get:async(url)=>{{calls.push(url);return {{data:{{source_type:'composite_bom_requisition',supplier_order_number:'BL-C1',cards:[{{source_identity:'bom:77:whole',selection_fingerprint:'a'.repeat(64),production_task_versions:[{{task_id:501,version:3}}],selection_eligible:true,material_requisition_item_ids:[41],components:[{{material_requisition_item_id:41}}],product_code:'C-41'}}]}}}};}}}};
const row={{stable_id:'composite_bom_requisition:9:41',source_type:'composite_bom_requisition',status:'已入库',can_print_task:true,document_id:9,document_number:'BL-C1',item_id:41}};
const vm={{activePage:'requisition',requisitionTab:'submitted',authGeneration:2,user:{{id:8}},reportedItemPrintBusy:false,reportedItemPrintErrors:[],reportedLabelRecoveryUrls:[],productionPrintBatchAttempt:null,productionPrintRecoveryUrl:'',productionPrintSelections:{{}},productionPrintSelectionSequence:0,
  reportedSelectedItems(){{return [row];}},productionPrintAttemptUncertain(){{return false;}},resetProductionPrintBatchOutcome(){{}},
  async prepareProductionPrintBatch(){{this.preparedPayload=this.productionPrintPayloadItems(Object.values(this.productionPrintSelections));return true;}},
  errorMessage(error){{return error.message;}},resetPagePerformanceState(){{throw new Error('unexpected reset');}},
}};
vm.reportedProductionCandidates=new Function('orderId','row','data',{json.dumps(candidates, ensure_ascii=False)}).bind(vm);
vm.productionPrintPayloadItems=new Function('items',{json.dumps(payload_items, ensure_ascii=False)}).bind(vm);
vm.prepareReportedItemTaskPrint=new AsyncFunction({json.dumps(prepare, ensure_ascii=False)}).bind(vm);
const expect=(value,message)=>{{if(!value)throw new Error(message);}};
(async()=>{{
  expect(await vm.prepareReportedItemTaskPrint()===true,'composite component task was blocked');
  expect(calls[0]==='/api/requisition/batches/9/production-print-package?item_ids=41','wrong composite task preflight URL');
  expect(vm.preparedPayload.length===1,'composite task was not prepared once');
  expect(vm.preparedPayload[0].source_type==='composite_bom_requisition'&&vm.preparedPayload[0].document_id===9,'composite batch identity was lost');
  expect(!('supplier_order_id' in vm.preparedPayload[0]),'composite task impersonated supplier order');
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path, "p1-73d-composite-task-print.js")


def test_label_print_accepts_independent_tasks_within_one_supplier_order(
    tmp_path: Path,
) -> None:
    body = _method_body("openReportedItemLabels")
    candidates = _method_body("reportedProductionCandidates")
    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
global.confirm=()=>true;const gets=[];const opened=[];
global.window={{open(){{const tab={{location:{{href:'about:blank'}},close(){{this.closed=true;}}}};opened.push(tab);return tab;}}}};
global.axios={{get:async(url)=>{{gets.push(url);if(url.endsWith('/production-print-package'))return {{data:{{supplier_order_number:'SRO-3',cards:[
  {{source_identity:'item:31',production_task_versions:[{{task_id:501,version:2}}],components:[{{supplier_order_item_id:31}}],product_code:'P31'}},
  {{source_identity:'item:32',production_task_versions:[{{task_id:502,version:3}}],components:[{{supplier_order_item_id:32}}],product_code:'P32'}},
]}}}};return {{data:{{label_count:2}}}};}}}};
const rows=[31,32].map(item_id=>({{stable_id:`supplier_order:3:${{item_id}}`,source_type:'supplier_order',status:'active',can_print_label:true,active_item_count:2,document_id:3,document_number:'SRO-3',item_id,product_code:`P${{item_id}}`}}));
const vm={{reportedItemPrintBusy:false,reportedItemPrintErrors:[],reportedLabelRecoveryUrls:[],authGeneration:1,user:{{id:2}},activePage:'requisition',requisitionTab:'submitted',selection:[rows[0]],
  reportedSelectedItems(){{return this.selection;}},showToast(){{}},errorMessage(error){{return error.message;}},resetPagePerformanceState(){{throw new Error('unexpected reset');}},
}};
vm.reportedProductionCandidates=new Function('orderId','row','data',{json.dumps(candidates, ensure_ascii=False)}).bind(vm);
vm.openReportedItemLabels=new AsyncFunction({json.dumps(body, ensure_ascii=False)}).bind(vm);
const expect=(value,message)=>{{if(!value)throw new Error(message);}};
(async()=>{{
  expect(await vm.openReportedItemLabels()===true,'one selected task was blocked by its supplier order');
  expect(gets.length===2&&opened.length===1,'one selected task did not run task and label preflight once');
  expect(gets[1]==='/api/requisition/supplier-orders/3/production-packaging-label-package?task_ids=501','one-task label preflight lost its task identity');
  expect(opened[0].location.href==='/production-packaging-label.html?id=3&task_ids=501','one-task label page URL is wrong');
  vm.selection=rows;vm.reportedItemPrintErrors=[];
  expect(await vm.openReportedItemLabels()===true,'two selected tasks failed');
  expect(gets.length===4&&opened.length===2,'two selected tasks did not preflight one order once');
  expect(gets[3]==='/api/requisition/supplier-orders/3/production-packaging-label-package?task_ids=501%2C502','two-task preflight URL is wrong');
  expect(opened[1].location.href==='/production-packaging-label.html?id=3&task_ids=501%2C502','two-task label page URL is wrong');
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path, "p1-73d-label-print-selection.js")


def test_label_print_mixed_invalid_products_stop_whole_batch_and_list_each(
    tmp_path: Path,
) -> None:
    body = _method_body("openReportedItemLabels")
    candidates = _method_body("reportedProductionCandidates")
    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
global.confirm=()=>true;const opened=[];
global.window={{open(){{const tab={{location:{{href:'about:blank'}},closed:false,close(){{this.closed=true;}}}};opened.push(tab);return tab;}}}};
global.axios={{get:async(url)=>{{
  const documentId=Number(url.split('/supplier-orders/')[1]?.split('/')[0]||0);
  if(url.endsWith('/production-print-package'))return {{data:{{supplier_order_number:`SRO-${{documentId}}`,cards:[{{source_identity:`item:${{documentId}}`,production_task_versions:[{{task_id:500+documentId,version:1}}],components:[{{supplier_order_item_id:documentId}}],product_code:`P${{documentId}}`}}]}}}};
  const error=new Error(`label ${{documentId}} blocked`);error.response={{status:409,data:{{detail:{{reasons:[`P${{documentId}}｜产品${{documentId}}｜生产任务 #${{500+documentId}} 的当前产品未启用生产包装标签`]}}}}}};throw error;
}}}};
const rows=[3,4].map(document_id=>({{stable_id:`supplier_order:${{document_id}}:${{document_id}}`,source_type:'supplier_order',status:'active',can_print_label:true,document_id,document_number:`SRO-${{document_id}}`,item_id:document_id,product_code:`P${{document_id}}`}}));
const vm={{reportedItemPrintBusy:false,reportedItemPrintErrors:[],reportedLabelRecoveryUrls:[],authGeneration:1,user:{{id:2}},activePage:'requisition',requisitionTab:'submitted',
  reportedSelectedItems(){{return rows;}},showToast(){{}},errorMessage(error){{return error.message;}},resetPagePerformanceState(){{throw new Error('unexpected reset');}},
}};
vm.reportedProductionCandidates=new Function('orderId','row','data',{json.dumps(candidates, ensure_ascii=False)}).bind(vm);
vm.openReportedItemLabels=new AsyncFunction({json.dumps(body, ensure_ascii=False)}).bind(vm);
const expect=(value,message)=>{{if(!value)throw new Error(message);}};
(async()=>{{
  expect(await vm.openReportedItemLabels()===false,'mixed invalid products were accepted');
  expect(opened.length===2&&opened.every(tab=>tab.closed),'failed batch left a label tab open');
  expect(vm.reportedItemPrintErrors.length===2,'not every invalid product was listed');
  expect(vm.reportedItemPrintErrors.some(value=>value.includes('P3')),'P3 reason missing');
  expect(vm.reportedItemPrintErrors.some(value=>value.includes('P4')),'P4 reason missing');
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path, "p0-18-label-mixed-failures.js")


def test_session_reset_clears_reported_item_selection_detail_and_attempts() -> None:
    reset = _method_body("resetPagePerformanceState")
    for marker in (
        "this.reportedItemSelections = {};",
        "this.reportedItemSelectionSequence = 0;",
        "this.reportedItemDetail = null;",
        'this.reportedItemError = "";',
        "this.reportedItemPrintErrors = [];",
        "this.reportedLabelRecoveryUrls = [];",
        "this.reportedItemVoidAttempts = {};",
    ):
        assert marker in reset
