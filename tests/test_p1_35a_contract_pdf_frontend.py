from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def _pdf_method_block() -> str:
    start = "contractPdfExportBusy(contractId=null) {"
    end = "addContractLine() {"
    assert start in INDEX
    assert end in INDEX
    return start + INDEX.split(start, 1)[1].split(end, 1)[0]


def _run_node(source: str, tmp_path: Path, name: str) -> None:
    node = shutil.which("node")
    assert node is not None, "Node.js is required for the contract PDF frontend regression"
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


def test_contract_pdf_is_primary_download_and_keeps_secondary_preview() -> None:
    assert INDEX.count("导出 PDF（未盖章）") >= 2
    assert INDEX.count("网页预览/自助打印") >= 2
    assert 'contractPdfExportState:{contractId:null,requestId:0}' in INDEX
    assert '@click="exportContractPdf(contractDraft)"' in INDEX
    assert '@click="exportContractPdf(row)"' in INDEX
    assert "contractPdfExportBusy(contractDraft.id) ? '正在导出…'" in INDEX
    assert "contractPdfExportBusy(row.id) ? '正在导出…'" in INDEX
    assert "`/api/contracts/${target.id}/pdf?expected_version=${encodeURIComponent(target.version)}`" in INDEX
    assert 'headers:{Accept:"application/pdf"}' in INDEX
    assert 'window.open(`/contract-print.html?id=${encodeURIComponent(row.id)}`' in INDEX


def test_contract_pdf_success_freezes_target_blocks_double_click_and_releases_url(
    tmp_path: Path,
) -> None:
    methods = _pdf_method_block()
    script = f"""
const methods={{
{methods}
}};
const expect=(value,message)=>{{if(!value)throw new Error(message)}};
const pdf=new Blob(["%PDF-1.7\\ncontract"],{{type:"application/pdf"}});
const headers={{
  "content-type":"application/pdf",
  "content-disposition":"attachment; filename=\\\"contract.pdf\\\"; filename*=UTF-8''%E5%90%88%E5%90%8C_CT-007_v3.pdf",
  "x-request-id":"REQ-OK",
}};
let releaseFetch,fetchCount=0,requestedUrl="",clicked=0,downloadName="",revoked="",createdUrl="";
global.fetch=(url)=>{{fetchCount+=1;requestedUrl=url;return new Promise(resolve=>{{releaseFetch=()=>resolve({{ok:true,status:200,headers:{{get:name=>headers[String(name).toLowerCase()]||""}},blob:async()=>pdf}});}});}};
global.URL.createObjectURL=blob=>{{expect(blob===pdf,"download did not use returned PDF blob");createdUrl="blob:contract";return createdUrl;}};
global.URL.revokeObjectURL=url=>{{revoked=url;}};
global.setTimeout=fn=>{{fn();return 1;}};
const anchor={{href:"",download:"",click(){{clicked+=1;downloadName=this.download;}},remove(){{}}}};
global.document={{body:{{appendChild(node){{expect(node===anchor,"unexpected download node");}}}},createElement:tag=>{{expect(tag==="a","download did not create anchor");return anchor;}}}};
const toasts=[];
const vm={{contractPdfExportState:{{contractId:null,requestId:0}},showToast:(message,error=false)=>toasts.push({{message,error}}),...methods}};
const row={{id:7,version:3,contract_no:"CT-007"}};
(async()=>{{
  const pending=vm.exportContractPdf(row);
  row.id=99;row.version=88;row.contract_no="CHANGED";
  const duplicate=await vm.exportContractPdf(row);
  expect(duplicate===false,"double click was not blocked");
  expect(fetchCount===1,"double click issued another request");
  expect(requestedUrl==="/api/contracts/7/pdf?expected_version=3","contract id/version were not frozen");
  releaseFetch();
  expect(await pending===true,"successful export did not report success");
  expect(clicked===1,"download anchor was not clicked exactly once");
  expect(downloadName==="合同_CT-007_v3.pdf","UTF-8 server filename was not used");
  expect(revoked===createdUrl,"object URL was not released");
  expect(vm.contractPdfExportState.contractId===null,"success did not release export busy state");
  expect(toasts.length===1&&!toasts[0].error&&toasts[0].message.includes("未盖章 PDF 已下载"),"success feedback missing");
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path, "contract-pdf-success.js")


def test_contract_pdf_errors_parse_blob_detail_and_recover_busy_state(tmp_path: Path) -> None:
    methods = _pdf_method_block()
    script = f"""
const methods={{
{methods}
}};
const expect=(value,message)=>{{if(!value)throw new Error(message)}};
const response=(status,type,content,requestId)=>({{ok:status>=200&&status<300,status,headers:{{get:name=>{{const key=String(name).toLowerCase();return key==="content-type"?type:key==="x-request-id"?requestId:"";}}}},blob:async()=>new Blob([content],{{type}})}});
const queue=[
  response(409,"application/json",JSON.stringify({{detail:{{message:"合同版本已变化",code:"CONTRACT_VERSION_CONFLICT"}}}}),"REQ-409"),
  response(500,"text/html","<html>broken</html>","REQ-500"),
  response(200,"application/pdf","","REQ-EMPTY"),
  new TypeError("Failed to fetch"),
];
let fetchCount=0,objectUrls=0;
global.fetch=async()=>{{fetchCount+=1;const next=queue.shift();if(next instanceof Error)throw next;return next;}};
global.URL.createObjectURL=()=>{{objectUrls+=1;return "blob:wrong";}};
global.URL.revokeObjectURL=()=>{{}};
global.document={{body:{{appendChild(){{}}}},createElement:()=>({{click(){{}},remove(){{}}}})}};
const toasts=[];
const vm={{contractPdfExportState:{{contractId:null,requestId:0}},showToast:(message,error=false)=>toasts.push({{message,error}}),...methods}};
(async()=>{{
  for(let index=0;index<4;index+=1){{
    expect(await vm.exportContractPdf({{id:7,version:3,contract_no:"CT-007"}})===false,"failure returned success");
    expect(vm.contractPdfExportState.contractId===null,"failure did not release export busy state");
  }}
  expect(fetchCount===4,"not all failure paths were exercised");
  expect(objectUrls===0,"invalid/error response created a download URL");
  expect(toasts.length===4&&toasts.every(row=>row.error),"failures were not reported as errors");
  expect(toasts[0].message.includes("合同版本已变化")&&toasts[0].message.includes("CONTRACT_VERSION_CONFLICT")&&toasts[0].message.includes("HTTP 409")&&toasts[0].message.includes("REQ-409"),"409 object detail/code/status/request id missing");
  expect(toasts[1].message.includes("服务器内部错误")&&toasts[1].message.includes("HTTP 500")&&toasts[1].message.includes("REQ-500")&&!toasts[1].message.includes("<html>"),"500 non-JSON error was exposed or unclear");
  expect(toasts[2].message.includes("服务器未返回有效的合同 PDF")&&toasts[2].message.includes("HTTP 200")&&toasts[2].message.includes("REQ-EMPTY"),"empty PDF error missing");
  expect(toasts[3].message.includes("无法连接 ERP 服务"),"network failure message missing");
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    _run_node(script, tmp_path, "contract-pdf-errors.js")


def test_contract_pdf_filename_falls_back_safely(tmp_path: Path) -> None:
    methods = _pdf_method_block()
    script = f"""
const methods={{
{methods}
}};
const vm={{contractPdfExportState:{{contractId:null,requestId:0}},...methods}};
const fallback=vm.contractPdfFallbackFilename({{id:7,version:3,contractNo:"CT/007"}});
if(fallback!=="CT_007_v3_未盖章.pdf")throw new Error(`unsafe fallback: ${{fallback}}`);
const filename=vm.contractPdfDownloadFilename("attachment; filename=contract",fallback);
if(filename!=="contract.pdf")throw new Error(`plain filename was not normalized: ${{filename}}`);
"""
    _run_node(script, tmp_path, "contract-pdf-filename.js")
