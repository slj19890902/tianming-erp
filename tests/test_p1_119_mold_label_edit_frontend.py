from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from tests.test_p1_62_mold_40x80_label import (
    _current_print_styles,
    _dump_rendered_dom,
    _qr_data_url,
)


ROOT = Path(__file__).resolve().parents[1]
WAREHOUSE = (ROOT / "static" / "warehouse.html").read_text(encoding="utf-8")
LABEL_PAGE = (ROOT / "static" / "mold-label.html").read_text(encoding="utf-8")
LAYOUT_JS = (ROOT / "static" / "assets" / "mold-label-layout.js").read_text(
    encoding="utf-8"
)


@pytest.fixture
def headless_browser() -> Path:
    """This task's browser probes must use isolated Google Chrome, never Edge."""
    candidates = [Path("C:/Program Files/Google/Chrome/Application/chrome.exe")]
    for variable in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        if base := os.environ.get(variable):
            candidates.append(
                Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe"
            )
    if resolved := shutil.which("chrome"):
        candidates.insert(0, Path(resolved))
    chrome = next((candidate for candidate in candidates if candidate.is_file()), None)
    if chrome is None:
        pytest.skip("当前隔离环境未找到 Google Chrome")
    return chrome


def _function_source(name: str, next_name: str, *, next_async: bool) -> str:
    start = WAREHOUSE.index(f"    async function {name}(")
    prefix = "async " if next_async else ""
    end = WAREHOUSE.index(f"    {prefix}function {next_name}(", start)
    return WAREHOUSE[start:end]


def _run_node(source: str) -> dict:
    result = subprocess.run(
        ["node", "-e", source],
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _v8_layout() -> dict:
    text = {
        "kind": "text",
        "font_size_mm": 4.4,
        "font_weight": 900,
        "text_align": "left",
        "visible": True,
    }
    return {
        "catalog_version": "p1-119-v1",
        "paper": {"width_mm": 80, "height_mm": 40},
        "elements": [
            {**text, "id": "rack_location", "x_mm": 1.2, "y_mm": 0.6, "width_mm": 50, "height_mm": 5},
            {**text, "id": "cutting_mode", "x_mm": 53, "y_mm": 0.6, "width_mm": 25.8, "height_mm": 5, "text_align": "right"},
            {**text, "id": "custom_note", "x_mm": 1.2, "y_mm": 6.6, "width_mm": 77.6, "height_mm": 9},
            {"id": "section_rule_top", "kind": "rule", "x_mm": 1.2, "y_mm": 16.6, "width_mm": 77.6, "height_mm": 0.2, "line_width_mm": 0.2, "color": "#111111", "visible": True},
            {**text, "id": "product_name", "x_mm": 1.2, "y_mm": 17.2, "width_mm": 60.6, "height_mm": 4.2, "font_size_mm": 3.6},
            {**text, "id": "customer_inventory_code", "x_mm": 1.2, "y_mm": 22, "width_mm": 60.6, "height_mm": 11.6, "font_size_mm": 6},
            {"id": "mold_qr", "kind": "qr", "x_mm": 63.6, "y_mm": 18.2, "width_mm": 15, "height_mm": 15, "visible": True},
            {"id": "section_rule_bottom", "kind": "rule", "x_mm": 1.2, "y_mm": 34.8, "width_mm": 77.6, "height_mm": 0.2, "line_width_mm": 0.2, "color": "#111111", "visible": True},
            {**text, "id": "report_specification", "x_mm": 1.2, "y_mm": 35, "width_mm": 77.6, "height_mm": 5},
        ],
    }


def test_editor_reuses_print_renderer_and_preserves_unloaded_overrides() -> None:
    assert "/api/warehouse/molds/${moldId}/label-preview" in WAREHOUSE
    assert "TmMoldLabelLayout.labelHtml(moldLabelPreviewDraft(),preview.envelope)" in WAREHOUSE
    assert "TmMoldLabelLayout.fitAndValidate(paper)" in WAREHOUSE
    assert "label_overrides:moldLabelOverridesPayload()" in WAREHOUSE
    assert "id&&state.moldLabelPreview.loaded" in WAREHOUSE
    assert "requestId!==state.moldLabelPreview.requestId" in WAREHOUSE
    assert "Number(state.moldEditLocation.moldId)!==Number(moldId)" in WAREHOUSE
    assert "本次不会清空已有标签专用内容" in WAREHOUSE
    assert "40×30 标签保持原版式" in WAREHOUSE
    assert "当前不能打印" in WAREHOUSE


def test_multi_customer_and_bound_product_unbind_contract_is_reachable() -> None:
    assert 'id="moldPrimaryCustomer2Field" class="field"' in WAREHOUSE
    assert "适用客户（可多选，主标签最多显示两个）" in WAREHOUSE
    assert "新增模具只能选择一个正式客户" not in WAREHOUSE
    assert "/bound-products`" in WAREHOUSE
    assert "?expected_version=${attempt.expectedVersion}" in WAREHOUSE
    assert "! 解绑说明" in WAREHOUSE
    assert "不会盲目重复解绑" in WAREHOUSE
    remove_body = WAREHOUSE.split("async function removeMoldBinding(index){", 1)[1].split(
        "function setPrintingPlateLocationBuilderDisabled", 1
    )[0]
    assert "window.confirm" not in remove_body


def test_preview_auto_field_refresh_preserves_unsaved_override_draft() -> None:
    function_source = _function_source(
        "loadMoldLabelPreview", "clearMoldBoundProducts", next_async=False
    )
    result = _run_node(
        r"""
const elements={
  moldLabelDisplayIdentity:{value:"未保存身份",placeholder:"",disabled:false},
  moldLabelDisplayProductName:{value:"未保存产品",placeholder:"",disabled:false},
  moldLabelDisplayReportSpecification:{value:"",placeholder:"",disabled:false},
  moldLabelDisplayCuttingMode:{value:"",placeholder:"",disabled:false},
  moldLabelDisplayRemarks:{value:"",placeholder:"",disabled:false},
  moldLabelEditWorkspace:{hidden:false},moldLabelPreviewState:{textContent:"",className:""},
  moldLabelPreviewPaper:{innerHTML:""}
};
const $=id=>elements[id];
const moldLabelOverrideFields=Object.freeze({display_identity:"moldLabelDisplayIdentity",product_name:"moldLabelDisplayProductName",report_specification:"moldLabelDisplayReportSpecification",cutting_mode:"moldLabelDisplayCuttingMode",remarks:"moldLabelDisplayRemarks"});
const state={moldEditLocation:{moldId:42},moldLabelPreview:{moldId:42,row:{},envelope:{},autoFields:{},loaded:true,loading:false,error:"",requestId:0,overflow:[]}};
function currentMoldAutomaticIdentity(){return "自动身份新值"}
function moldLabelAutoValue(key){return String(state.moldLabelPreview.autoFields[key]||"")}
function setMoldLabelOverrideControlsDisabled(disabled){Object.values(moldLabelOverrideFields).forEach(id=>elements[id].disabled=disabled)}
function updateMoldSaveAvailability(){}
function renderMoldLabelPreview(){}
async function api(){return {label_layout:{version:1},label_auto_fields:{display_identity:"服务端自动身份",product_name:"服务端自动产品"},label_overrides:{display_identity:"服务端旧覆写",product_name:"服务端旧产品覆写"}}}
"""
        + function_source
        + r"""
(async()=>{await loadMoldLabelPreview(42,{preserveDraft:true});console.log(JSON.stringify({identity:elements.moldLabelDisplayIdentity.value,product:elements.moldLabelDisplayProductName.value,identityPlaceholder:elements.moldLabelDisplayIdentity.placeholder,productPlaceholder:elements.moldLabelDisplayProductName.placeholder,loaded:state.moldLabelPreview.loaded}))})().catch(error=>{console.error(error);process.exit(1)});
"""
    )
    assert result == {
        "identity": "未保存身份",
        "product": "未保存产品",
        "identityPlaceholder": "自动：自动身份新值",
        "productPlaceholder": "自动：服务端自动产品",
        "loaded": True,
    }


def test_unknown_unbind_result_reads_current_state_without_second_delete() -> None:
    reconcile_source = _function_source(
        "reconcileMoldUnbind", "unbindMoldBoundProduct", next_async=True
    )
    unbind_source = _function_source(
        "unbindMoldBoundProduct", "renderMoldCustomerCandidates", next_async=True
    )
    result = _run_node(
        r"""
let state,apiCalls,refreshes,toasts,presentAfterUnknown;
function renderMoldBoundProducts(){}
function updateMoldSaveAvailability(){}
function applyMoldBoundProducts(data){state.moldBoundProducts.items=data.items;state.moldBoundProducts.loading=false;return true}
async function refreshMoldAfterBindingChange(moldId,message){refreshes.push({moldId,message});state.moldBoundProducts.unbindAttempt=null}
async function loadMoldBoundProducts(){throw new Error("not used")}
function toast(message,isError=false){toasts.push({message,isError})}
async function api(url,options={}){
  apiCalls.push({url,method:options.method||"GET"});
  if(options.method==="DELETE")throw new Error("network result unknown");
  return {items:presentAfterUnknown?[{id:7,version:3,product_code:"80011946"}]:[]};
}
"""
        + reconcile_source
        + unbind_source
        + r"""
async function scenario(present){presentAfterUnknown=present;apiCalls=[];refreshes=[];toasts=[];state={moldBoundProducts:{moldId:42,items:[{id:7,version:3,product_code:"80011946"}],loading:false,requestId:0,unbindAttempt:null}};await unbindMoldBoundProduct(7);return {apiCalls,refreshes,attempt:state.moldBoundProducts.unbindAttempt,toasts}}
(async()=>{console.log(JSON.stringify({absent:await scenario(false),stillBound:await scenario(true)}))})().catch(error=>{console.error(error);process.exit(1)});
"""
    )
    for case in (result["absent"], result["stillBound"]):
        assert [call["method"] for call in case["apiCalls"]] == ["DELETE", "GET"]
    assert result["absent"]["attempt"] is None
    assert result["absent"]["refreshes"][0]["message"] == "已核对：当前常用箱已解绑"
    assert result["stillBound"]["attempt"] is None
    assert not result["stillBound"]["refreshes"]
    assert "请再次点击解绑" in result["stillBound"]["toasts"][-1]["message"]


def test_v8_is_current_without_reclassifying_frozen_v7() -> None:
    assert 'CURRENT_WIDE_CATALOG="p1-119-v1"' in LABEL_PAGE
    assert 'const V7_CATALOG_VERSION = "p1-118-v1"' in LAYOUT_JS
    assert 'const V8_CATALOG_VERSION = "p1-119-v1"' in LAYOUT_JS
    assert "dataset.truncated" in LAYOUT_JS
    assert "failures.push(node.dataset.layoutLabel" in LAYOUT_JS


def test_v8_uses_full_identity_and_reports_overflow_without_ellipsis(
    headless_browser: Path,
    tmp_path: Path,
) -> None:
    full_identity = "光洋/研光 " + "80011946-APS4-完整身份文字" * 12
    row = {
        "label_display_identity": full_identity,
        "label_display_product_name": "APS4",
        "label_display_report_specification": "772 × 336 / 876 × 336",
        "label_display_cutting_mode": "一开四",
        "label_display_remarks": "",
        "label_rack_location": "1F-M-R04",
        "label_customer_name": "不应替代专用身份",
        "label_inventory_code": "不应重复拼接",
        "qr_data_url": _qr_data_url(),
        "products": [],
    }
    fixture = tmp_path / "p1-119-v8-overflow.html"
    fixture.write_text(
        '<!doctype html><html><head><meta charset="utf-8">'
        + _current_print_styles()
        + '</head><body><main id="labels"></main><script>'
        + LAYOUT_JS.replace("</script>", "<\\/script>")
        + "</script><script>"
        + f"const row={json.dumps(row, ensure_ascii=False)};"
        + f"const envelope={{version:1,layout:{json.dumps(_v8_layout(), ensure_ascii=False)}}};"
        + 'const labels=document.getElementById("labels");'
        + "labels.innerHTML=TmMoldLabelLayout.labelHtml(row,envelope);"
        + "const failures=TmMoldLabelLayout.fitAndValidate(labels);"
        + 'const identity=labels.querySelector("[data-layout-id=customer_inventory_code]");'
        + 'const qr=labels.querySelector("[data-layout-id=mold_qr]");'
        + 'document.body.dataset.failures=failures.join("|");'
        + 'document.body.dataset.identity=identity.textContent;'
        + 'document.body.dataset.truncated=String(identity.dataset.truncated==="true");'
        + 'document.body.dataset.qrWidth=qr.style.width;'
        + "</script></body></html>",
        encoding="utf-8",
    )
    rendered = _dump_rendered_dom(headless_browser, fixture, tmp_path)
    assert 'data-failures="客户简称与存货编码"' in rendered
    assert 'data-truncated="false"' in rendered
    assert 'data-qr-width="15mm"' in rendered
    assert full_identity in rendered


def test_v8_normal_sample_fits_and_prefers_display_identity(
    headless_browser: Path,
    tmp_path: Path,
) -> None:
    row = {
        "label_display_identity": "光洋/研光 80011946 APS4",
        "label_display_product_name": "APS4",
        "label_display_report_specification": "772 × 336 / 876 × 336",
        "label_display_cutting_mode": "一开四",
        "label_display_remarks": "",
        "label_rack_location": "1F-M-R04",
        "label_customer_name": "光洋/研光",
        "label_inventory_code": "80011946",
        "qr_data_url": _qr_data_url(),
        "products": [],
    }
    fixture = tmp_path / "p1-119-v8-normal.html"
    fixture.write_text(
        '<!doctype html><html><head><meta charset="utf-8">'
        + _current_print_styles()
        + '</head><body><main id="labels"></main><script>'
        + LAYOUT_JS.replace("</script>", "<\\/script>")
        + "</script><script>"
        + f"const row={json.dumps(row, ensure_ascii=False)};"
        + f"const envelope={{version:1,layout:{json.dumps(_v8_layout(), ensure_ascii=False)}}};"
        + 'const labels=document.getElementById("labels");'
        + "labels.innerHTML=TmMoldLabelLayout.labelHtml(row,envelope);"
        + "const failures=TmMoldLabelLayout.fitAndValidate(labels);"
        + 'document.body.dataset.failures=failures.join("|");'
        + 'document.body.dataset.identity=labels.querySelector("[data-layout-id=customer_inventory_code]").textContent;'
        + "</script></body></html>",
        encoding="utf-8",
    )
    rendered = _dump_rendered_dom(headless_browser, fixture, tmp_path)
    assert 'data-failures=""' in rendered
    assert 'data-identity="光洋/研光 80011946 APS4"' in rendered
    assert "光洋/研光 80011946 80011946" not in rendered
