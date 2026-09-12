from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess


INDEX = Path("static/index.html").read_text(encoding="utf-8")


def _pdf_block() -> str:
    start = INDEX.index("<!-- PDF 草稿默认只保留现场核对必需信息")
    end = INDEX.index('<div v-else-if="modal.type === \'', start)
    return INDEX[start:end]


def _manual_order_block() -> str:
    start = INDEX.index("<!-- P1-02：固定一行一款")
    end = INDEX.index('<div v-else-if="modal.type === \'orderEdit\'">', start)
    return INDEX[start:end]


def _method_source(name: str) -> str:
    start_match = re.search(
        rf"(?m)^\s{{10}}(?:async\s+)?{re.escape(name)}\(",
        INDEX,
    )
    assert start_match, f"method {name} is missing"
    end_match = re.search(
        r"(?m)^\s{10}(?:async\s+)?[A-Za-z_$][A-Za-z0-9_$]*\(",
        INDEX[start_match.end() :],
    )
    start = start_match.start()
    end = (
        start_match.end() + end_match.start()
        if end_match
        else len(INDEX)
    )
    return INDEX[start:end]


def _inline_app_script() -> str:
    return next(
        script
        for script in re.findall(
            r"<script(?:\s[^>]*)?>(.*?)</script>",
            INDEX,
            flags=re.DOTALL,
        )
        if script.strip()
    )


def test_pdf_default_table_prioritizes_products_quantity_and_permissioned_prices() -> None:
    block = _pdf_block()
    table_start = block.index('<table class="line-items pdf-inventory-table"')
    table_end = block.index("</table>", table_start)
    table = block[table_start:table_end]
    header = table[table.index("<thead>") : table.index("</thead>")]

    assert len(re.findall(r"<th(?:\s|>)", header)) == 9
    for label in (
        "序号",
        "存货编码",
        "产品名称",
        "数量",
        "单价",
        "金额",
        "库存 / 需报",
        "异常",
        "操作",
    ):
        assert label in header

    column_count = _method_source("pdfDraftColumnCount")
    assert "this.canViewSalesAmounts ? 9 : 7" in column_count
    assert "draft?._show_advanced_details ? 10 : 5" not in column_count


def test_pdf_uses_dedicated_inventory_helpers_without_changing_manual_order() -> None:
    pdf = _pdf_block()
    manual = _manual_order_block()

    for helper in (
        "pdfInventoryDisplayState(item)",
        "pdfInventoryStatusText(item)",
        "pdfInventoryShortageText(item)",
        "pdfInventoryRequisitionText(item)",
        "pdfInventoryHasButton(item)",
    ):
        assert helper in pdf or helper in INDEX

    assert "orderLineInventoryAutoSummary(item)" not in pdf
    assert "查看库存安排" not in pdf
    assert "orderLineInventoryAutoSummary(item)" in manual
    assert "查看库存安排" in manual
    assert INDEX.count("orderLineInventoryAutoSummary(item)") == 1
    preview = _method_source("loadPdfDraftInventoryAuthority")
    formal_save = _method_source("saveConfirmedImportDrafts")
    assert "material:this.pdfImportItemMaterialCode(line)" in preview
    assert "flute_type:line.flute_type || null" in preview
    assert "const matCode = this.pdfImportItemMaterialCode(item);" in formal_save

    shared = _method_source("orderLineInventoryAutoSummary")
    for marker in (
        "成品${reservedFinished}",
        "半成品${semiPieces}",
        "需生产${productionRequired}",
    ):
        assert marker in shared
    assert "下单${orderQuantity}" not in shared
    assert "现有成品" not in shared
    assert "text-overflow: ellipsis" not in INDEX[
        INDEX.index(".order-entry-quantity .inventory-auto-summary") :
        INDEX.index(".order-entry-quantity .inventory-auto-detail-button")
    ]


def test_pdf_product_name_is_always_visible_and_none_state_has_no_button() -> None:
    block = _pdf_block()
    row_start = block.index("<!-- 主行 -->")
    row_end = block.index("</tr>", row_start)
    row = block[row_start:row_end]

    product_name_position = row.index("pdfCommonBoxProductName(item)")
    quantity_position = row.index("pdfInventoryStatusText(item)")
    assert product_name_position < quantity_position
    assert 'v-if="draft._show_advanced_details"' not in row[
        row.rfind("<td", 0, product_name_position) : product_name_position
    ]
    assert 'v-if="pdfInventoryHasButton(item)"' in row
    assert "pdfInventoryRequisitionText(item)" in row


def test_pdf_table_css_prevents_horizontal_scrolling_in_standard_and_large_modes() -> None:
    block = _pdf_block()
    assert 'class="pdf-inventory-table-wrap"' in block
    assert '<table class="line-items pdf-inventory-table"' in block
    assert "overflow-x:auto" not in block[
        block.index('class="pdf-inventory-table-wrap"') :
        block.index("</table>", block.index('class="pdf-inventory-table-wrap"'))
    ]

    assert re.search(
        r"\.pdf-inventory-table-wrap\s*\{[^}]*overflow-x:\s*hidden",
        INDEX,
        flags=re.DOTALL,
    )
    table_css = re.search(
        r"\.pdf-inventory-table\s*\{(?P<body>[^}]*)\}",
        INDEX,
        flags=re.DOTALL,
    )
    assert table_css
    assert re.search(r"table-layout:\s*fixed", table_css.group("body"))
    assert re.search(r"min-width:\s*0", table_css.group("body"))
    assert ".ui-large .pdf-order-import-modal .pdf-inventory-table" in INDEX


def test_pdf_warehouse_locator_keeps_draft_mounted_and_has_three_return_paths() -> None:
    assert "pdfWarehouseLocator:" in INDEX
    assert 'v-if="pdfWarehouseLocator.visible"' in INDEX
    assert "返回 PDF 订单" in INDEX
    assert 'ref="pdfWarehouseLocatorFrame"' in INDEX
    assert 'src="about:blank"' in INDEX

    open_source = _method_source("openPdfWarehouseLocator")
    close_source = _method_source("closePdfWarehouseLocator")
    popstate_source = _method_source("handlePdfWarehousePopState")

    for marker in ("location_id", "lot_id", 'readonly:"1"', "window.history.pushState"):
        assert marker in open_source
    assert "visible:false" in close_source
    assert "$nextTick" in close_source
    assert "scrollTop" in close_source
    assert "focus" in close_source
    assert "closePdfWarehouseLocator" in popstate_source
    assert 'window.addEventListener("popstate", this.handlePdfWarehousePopState)' in INDEX

    escape = INDEX[
        INDEX.index('document.addEventListener("keydown"') :
        INDEX.index("methods:", INDEX.index('document.addEventListener("keydown"'))
    ]
    locator_position = escape.index("pdfWarehouseLocator.visible")
    modal_position = escape.index("this.closeModal()")
    assert locator_position < modal_position
    assert "closePdfWarehouseLocator" in escape


def test_pdf_inventory_three_state_helpers_use_authoritative_contract() -> None:
    node = shutil.which("node")
    assert node, "Node.js is required for the PDF inventory state contract test"
    script = _inline_app_script()
    harness = f"""
const vm = require("vm");
const sandbox = {{
  axios: {{ defaults: {{}}, interceptors: {{ response: {{ use() {{}} }} }} }},
  Vue: {{ createApp(definition) {{ sandbox.definition = definition; return {{ component() {{ return this; }}, mount() {{ return this; }} }}; }} }},
  localStorage: {{ getItem() {{ return ""; }}, setItem() {{}}, removeItem() {{}} }},
  window: {{ addEventListener() {{}}, history: {{ pushState() {{}}, back() {{}} }} }},
  document: {{ addEventListener() {{}} }},
  console, URLSearchParams, setTimeout, clearTimeout,
}};
vm.createContext(sandbox);
vm.runInContext({json.dumps(script)}, sandbox);
const methods = sandbox.definition.methods;
const context = {{
  pdfInventoryDisplayState: methods.pdfInventoryDisplayState,
  inventoryDecisionRequired() {{ return ""; }},
}};
const line = (authoritative, overrides={{}}) => ({{
  product_id: 99,
  quantity: authoritative?.order_quantity || 100,
  _inventory: {{
    loading: false,
    stale: false,
    api_error: false,
    authoritative,
    finished: {{ candidates: [], allocations: [] }},
    semi: {{}},
    ...overrides,
  }},
}});
const full = line({{
  coverage_state: "full", interaction_state: "ready",
  order_quantity: 100, finished_planned_quantity: 100,
  production_required_quantity: 0, shortage_quantity: 0,
  requisition_sheet_quantity: 0, requisition_unit: "张",
}});
const partial = line({{
  coverage_state: "partial", interaction_state: "ready",
  order_quantity: 100, finished_planned_quantity: 60,
  production_required_quantity: 40, shortage_quantity: 40,
  requisition_sheet_quantity: 40, requisition_unit: "张",
}});
const none = line({{
  coverage_state: "none", interaction_state: "ready",
  order_quantity: 100, finished_planned_quantity: 0,
  production_required_quantity: 100, shortage_quantity: 100,
  requisition_sheet_quantity: 100, requisition_unit: "张",
}});
const stale = line(partial._inventory.authoritative, {{stale:true}});
const missing = line(null);

const state = value => methods.pdfInventoryDisplayState.call(context, value);
if (state(full) !== "full") throw new Error(`full became ${{state(full)}}`);
if (state(partial) !== "partial") throw new Error(`partial became ${{state(partial)}}`);
if (state(none) !== "none") throw new Error(`none became ${{state(none)}}`);
if (state(stale) !== "exception") throw new Error(`stale became ${{state(stale)}}`);
if (state(missing) !== "exception") throw new Error(`missing authority became ${{state(missing)}}`);

if (methods.pdfInventoryStatusText.call(context, full) !== "有库存") throw new Error("full label mismatch");
if (methods.pdfInventoryStatusText.call(context, partial) !== "库存不足") throw new Error("partial label mismatch");
if (methods.pdfInventoryShortageText.call(context, partial) !== "缺 40 个") throw new Error("shortage label mismatch");
if (methods.pdfInventoryRequisitionText.call(context, none) !== "100 张") throw new Error("requisition label mismatch");
if (!methods.pdfInventoryHasButton.call(context, full)) throw new Error("full must have button");
if (!methods.pdfInventoryHasButton.call(context, partial)) throw new Error("partial must have button");
if (methods.pdfInventoryHasButton.call(context, none)) throw new Error("no-stock row must not have empty button");
if (!methods.pdfInventoryHasButton.call(context, stale)) throw new Error("stale must retain an exception entry");
"""
    result = subprocess.run(
        [node],
        input=harness,
        text=True,
        encoding="utf-8",
        capture_output=True,
        env=os.environ.copy(),
        check=False,
    )
    assert result.returncode == 0, result.stderr
