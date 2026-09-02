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


def test_label_print_accepts_each_selected_supplier_item_independently(
    tmp_path: Path,
) -> None:
    body = _method_body("openReportedItemLabels")
    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
global.confirm=()=>true;const gets=[];const opened=[];
global.window={{open(){{const tab={{location:{{href:'about:blank'}},close(){{this.closed=true;}}}};opened.push(tab);return tab;}}}};
global.axios={{get:async(url)=>{{gets.push(url);return {{data:{{label_count:2}}}};}}}};
const rows=[31,32].map(item_id=>({{stable_id:`supplier_order:3:${{item_id}}`,source_type:'supplier_order',status:'active',can_print_label:true,active_item_count:2,document_id:3,document_number:'SRO-3',item_id,product_code:`P${{item_id}}`}}));
const vm={{reportedItemPrintBusy:false,reportedItemPrintErrors:[],reportedLabelRecoveryUrls:[],authGeneration:1,user:{{id:2}},activePage:'requisition',requisitionTab:'submitted',selection:[rows[0]],
  reportedSelectedItems(){{return this.selection;}},showToast(){{}},errorMessage(error){{return error.message;}},resetPagePerformanceState(){{throw new Error('unexpected reset');}},
}};
vm.openReportedItemLabels=new AsyncFunction({json.dumps(body, ensure_ascii=False)}).bind(vm);
const expect=(value,message)=>{{if(!value)throw new Error(message);}};
(async()=>{{
  expect(await vm.openReportedItemLabels()===true,'single selected supplier item was blocked');
  expect(gets[0]==='/api/requisition/supplier-orders/3/production-packaging-label-package?item_ids=31','single item was not used for live preview');
  expect(opened.length===1,'single item selection opened the wrong number of tabs');
  expect(opened[0].location.href==='/production-packaging-label.html?id=3&item_ids=31','selected item identity was lost from label page URL');
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path, "p1-73d-label-print-selection.js")


def test_label_print_opens_one_unified_page_for_multiple_supplier_orders(
    tmp_path: Path,
) -> None:
    body = _method_body("openReportedItemLabels")
    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
global.confirm=()=>true;const gets=[];const opened=[];
global.window={{open(){{const tab={{location:{{href:'about:blank'}},close(){{this.closed=true;}}}};opened.push(tab);return tab;}}}};
global.axios={{get:async(url)=>{{gets.push(url);return {{data:{{label_count:120}}}};}}}};
const rows=[
  {{stable_id:'supplier_order:147:1',source_type:'supplier_order',status:'active',can_print_label:true,active_item_count:1,document_id:147,document_number:'SRO-147',item_id:1,product_code:'A'}},
  {{stable_id:'supplier_order:148:2',source_type:'supplier_order',status:'active',can_print_label:true,active_item_count:1,document_id:148,document_number:'SRO-148',item_id:2,product_code:'B'}},
];
const vm={{reportedItemPrintBusy:false,reportedItemPrintErrors:[],reportedLabelRecoveryUrls:[],authGeneration:1,user:{{id:2}},activePage:'requisition',requisitionTab:'submitted',
  reportedSelectedItems(){{return rows;}},showToast(){{}},errorMessage(error){{return error.message;}},resetPagePerformanceState(){{throw new Error('unexpected reset');}},
}};
vm.openReportedItemLabels=new AsyncFunction({json.dumps(body, ensure_ascii=False)}).bind(vm);
const expect=(value,message)=>{{if(!value)throw new Error(message);}};
(async()=>{{
  expect(await vm.openReportedItemLabels()===true,'multi-order label selection failed');
  expect(gets.length===1,'multi-order selection did not use one batch preflight');
  expect(gets[0]==='/api/requisition/supplier-order-label-batches/package?order_ids=147%2C148&item_ids=1%2C2','wrong batch preflight URL');
  expect(opened.length===1,'multi-order selection opened more than one tab');
  expect(opened[0].location.href==='/production-packaging-label.html?ids=147%2C148&item_ids=1%2C2','wrong unified label page URL');
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path, "p0-jsd-unified-label-page.js")


def test_stock_replenishment_task_uses_shared_package_and_empty_task_versions(
    tmp_path: Path,
) -> None:
    candidates = _method_body("reportedProductionCandidates")
    prepare = _method_body("prepareReportedItemTaskPrint")
    payload_items = _method_body("productionPrintPayloadItems")
    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
const calls=[];
global.axios={{get:async(url)=>{{calls.push(url);return {{data:{{source_type:'stock_replenishment',supplier_order_number:'SRP-9',cards:[{{source_identity:'stock_replenishment_item:91',selection_fingerprint:'c'.repeat(64),production_task_versions:[],selection_eligible:true,stock_replenishment_item_ids:[91],product_code:'STOCK-91'}}]}}}};}}}};
const row={{stable_id:'stock_replenishment:9:91',source_type:'stock_replenishment',status:'confirmed',can_print_task:true,document_id:9,document_number:'SRP-9',item_id:91,product_code:'STOCK-91'}};
const vm={{activePage:'requisition',requisitionTab:'submitted',authGeneration:1,user:{{id:3}},reportedItemPrintBusy:false,reportedItemPrintErrors:[],reportedLabelRecoveryUrls:[],productionPrintBatchAttempt:null,productionPrintRecoveryUrl:'',productionPrintSelections:{{}},productionPrintSelectionSequence:0,
  reportedSelectedItems(){{return [row];}},productionPrintAttemptUncertain(){{return false;}},resetProductionPrintBatchOutcome(){{}},reportedPrintBlockMessage(){{throw new Error('unexpected blocker');}},
  async prepareProductionPrintBatch(){{this.payload=this.productionPrintPayloadItems(Object.values(this.productionPrintSelections));return true;}},
  errorMessage(error){{return error.message;}},resetPagePerformanceState(){{throw new Error('unexpected reset');}},
}};
vm.reportedProductionCandidates=new Function('orderId','row','data',{json.dumps(candidates, ensure_ascii=False)}).bind(vm);
vm.productionPrintPayloadItems=new Function('items',{json.dumps(payload_items, ensure_ascii=False)}).bind(vm);
vm.prepareReportedItemTaskPrint=new AsyncFunction({json.dumps(prepare, ensure_ascii=False)}).bind(vm);
const expect=(value,message)=>{{if(!value)throw new Error(message);}};
(async()=>{{
  expect(await vm.prepareReportedItemTaskPrint()===true,'stock replenishment task was blocked');
  expect(calls[0]==='/api/requisition/stock-replenishment/orders/9/production-print-package?item_ids=91','wrong stock task package URL');
  expect(vm.payload.length===1&&vm.payload[0].source_type==='stock_replenishment'&&vm.payload[0].document_id===9,'stock source identity was lost');
  expect(Array.isArray(vm.payload[0].task_versions)&&vm.payload[0].task_versions.length===0,'stock task invented a sales ProductionTask version');
  expect(!('supplier_order_id' in vm.payload[0]),'stock task impersonated a supplier order');
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path, "p0-38-stock-task-print.js")


def test_reported_labels_print_enabled_items_and_explain_excluded_items(
    tmp_path: Path,
) -> None:
    body = _method_body("openReportedItemLabels")
    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
let prompt='';const opened=[];
global.confirm=message=>{{prompt=String(message);return true;}};
global.window={{open(){{const tab={{location:{{href:'about:blank'}},close(){{this.closed=true;}}}};opened.push(tab);return tab;}}}};
global.axios={{get:async()=>({{data:{{label_count:3,production_task_count:1,excluded_items:[{{product_code:'NO-LABEL',reason:'常用箱未勾选打印标签'}}]}}}})}};
const rows=[31,32].map(item_id=>({{stable_id:`supplier_order:3:${{item_id}}`,source_type:'supplier_order',status:'active',can_print_label:true,document_id:3,document_number:'SRO-3',item_id,product_code:item_id===31?'PRINT':'NO-LABEL'}}));
const vm={{reportedItemPrintBusy:false,reportedItemPrintErrors:[],reportedLabelRecoveryUrls:[],authGeneration:1,user:{{id:2}},activePage:'requisition',requisitionTab:'submitted',
  reportedSelectedItems(){{return rows;}},reportedPrintBlockMessage(){{throw new Error('unexpected blocker');}},showToast(){{}},errorMessage(error){{return error.message;}},resetPagePerformanceState(){{throw new Error('unexpected reset');}},
}};
vm.openReportedItemLabels=new AsyncFunction({json.dumps(body, ensure_ascii=False)}).bind(vm);
const expect=(value,message)=>{{if(!value)throw new Error(message);}};
(async()=>{{
  expect(await vm.openReportedItemLabels()===true,'eligible label was blocked by disabled companion');
  expect(opened[0].location.href==='/production-packaging-label.html?id=3&item_ids=31%2C32','selected identities were lost');
  expect(prompt.includes('NO-LABEL')&&prompt.includes('到常用箱打开“打印标签”并保存'),'confirmation did not explain excluded item and action');
  expect(vm.reportedItemPrintErrors.some(message=>message.includes('NO-LABEL')&&message.includes('刷新后即可加入')),'excluded item was silently omitted after opening');
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path, "p0-38-reported-label-partial.js")


def test_reported_labels_all_disabled_show_full_reason_without_opening(
    tmp_path: Path,
) -> None:
    body = _method_body("openReportedItemLabels")
    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
let confirmCount=0;const opened=[];
global.confirm=()=>{{confirmCount++;return true;}};
global.window={{open(){{const tab={{location:{{href:'about:blank'}},close(){{this.closed=true;}}}};opened.push(tab);return tab;}}}};
global.axios={{get:async()=>{{const error=new Error('no labels');error.response={{status:409,data:{{detail:{{code:'production_label_no_eligible_items',reasons:['ALL-OFF：常用箱未勾选打印标签']}}}}}};throw error;}}}};
const row={{stable_id:'supplier_order:4:41',source_type:'supplier_order',status:'active',can_print_label:true,document_id:4,document_number:'SRO-4',item_id:41,product_code:'ALL-OFF'}};
const vm={{reportedItemPrintBusy:false,reportedItemPrintErrors:[],reportedLabelRecoveryUrls:[],authGeneration:1,user:{{id:2}},activePage:'requisition',requisitionTab:'submitted',
  reportedSelectedItems(){{return [row];}},reportedPrintBlockMessage(){{throw new Error('unexpected blocker');}},showToast(){{}},errorMessage(error){{return error.message;}},resetPagePerformanceState(){{throw new Error('unexpected reset');}},
}};
vm.openReportedItemLabels=new AsyncFunction({json.dumps(body, ensure_ascii=False)}).bind(vm);
const expect=(value,message)=>{{if(!value)throw new Error(message);}};
(async()=>{{
  expect(await vm.openReportedItemLabels()===false,'all-disabled label request reported success');
  expect(confirmCount===0,'all-disabled selection asked to print an empty package');
  expect(opened.length===1&&opened[0].closed===true,'pre-opened blank tab was left behind');
  expect(vm.reportedItemPrintErrors.some(message=>message.includes('ALL-OFF')&&message.includes('到常用箱打开“打印标签”并保存')),'complete reason and action were not shown');
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path, "p0-38-reported-label-all-disabled.js")


def test_incoming_label_entry_reuses_label_package_and_keeps_popup_recovery(
    tmp_path: Path,
) -> None:
    can_print = _method_body("canPrintIncomingProductLabel")
    blocker = _method_body("incomingProductLabelBlockMessage")
    body = _method_body("openSelectedIncomingProductLabels")
    script = f"""
const AsyncFunction=Object.getPrototypeOf(async function(){{}}).constructor;
global.confirm=()=>true;global.window={{open:()=>null}};const gets=[];
global.axios={{get:async(url)=>{{gets.push(url);return {{data:{{label_count:2,production_task_count:1,excluded_items:[]}}}};}}}};
const row={{receipt_item_id:81,receipt_status:'posted',supplier_order_id:8,supplier_order_item_id:18,supplier_order_number:'SRO-8',product_code:'BOX-8'}};
const vm={{incomingProductionCardBatchBusy:false,incomingProductLabelMessages:[],incomingProductLabelRecoveryUrls:[],incomingReceived:[],incomingHistory:[],incomingProductionCardSelections:{{81:row}},authGeneration:2,user:{{id:5}},activePage:'incoming',incomingTab:'received',
  showToast(){{}},errorMessage(error){{return error.message;}},resetPagePerformanceState(){{throw new Error('unexpected reset');}},
}};
vm.canPrintIncomingProductLabel=new Function('row',{json.dumps(can_print, ensure_ascii=False)}).bind(vm);
vm.incomingProductLabelBlockMessage=new Function('row',{json.dumps(blocker, ensure_ascii=False)}).bind(vm);
vm.openSelectedIncomingProductLabels=new AsyncFunction({json.dumps(body, ensure_ascii=False)}).bind(vm);
const expect=(value,message)=>{{if(!value)throw new Error(message);}};
(async()=>{{
  expect(await vm.openSelectedIncomingProductLabels()===true,'valid incoming label entry failed');
  expect(gets[0]==='/api/requisition/supplier-orders/8/production-packaging-label-package?item_ids=18','incoming entry did not reuse supplier label package');
  expect(vm.incomingProductLabelRecoveryUrls.length===1,'blocked popup did not keep recovery link');
  expect(vm.incomingProductLabelRecoveryUrls[0].url==='/production-packaging-label.html?id=8&item_ids=18','wrong incoming recovery URL');
  const reversed={{...row,receipt_status:'reversed'}};
  expect(!vm.canPrintIncomingProductLabel(reversed),'reversed receipt became printable');
  expect(vm.incomingProductLabelBlockMessage(reversed).includes('已撤销'),'reversed receipt blocker is vague');
  const stock={{receipt_item_id:82,receipt_status:'posted',source_type:'stock_replenishment',stock_replenishment_item_id:9,product_code:'STOCK'}};
  expect(!vm.canPrintIncomingProductLabel(stock),'stock receipt became an order product label');
  expect(vm.incomingProductLabelBlockMessage(stock).includes('库存补库'),'stock receipt blocker is vague');
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path, "p0-38-incoming-label-recovery.js")


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
