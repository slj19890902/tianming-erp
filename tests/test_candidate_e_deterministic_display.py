from __future__ import annotations

from datetime import datetime
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from app.core.time_contract import utc_naive_to_api


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
LEGACY_CUSTOMERS = (ROOT / "static" / "customers.html").read_text(
    encoding="utf-8"
)
PRINT_PAGE = (ROOT / "static" / "external-purchase-print.html").read_text(
    encoding="utf-8"
)
PURCHASE_SERVICE = (
    ROOT / "app" / "services" / "external_packaging_purchase.py"
).read_text(encoding="utf-8")
VERSION = (ROOT / "app" / "version.py").read_text(encoding="utf-8")


@pytest.fixture()
def purchase_app(tmp_path: Path):
    from tests.test_p1_33c3_external_packaging_purchase_confirmation import (
        purchase_app as purchase_app_fixture,
    )

    yield from purchase_app_fixture.__wrapped__(tmp_path)


def _method_block(name: str, next_name: str) -> str:
    start = INDEX.index(f"async {name}(")
    return INDEX[start : INDEX.index(f"async {next_name}(", start)]


def _run_node(source: str, tmp_path: Path, name: str) -> str:
    node = shutil.which("node")
    assert node, "Node.js is required for frontend behavior validation"
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
    return result.stdout.strip()


def test_phone_display_removes_only_empty_separators_and_flags_placeholders(
    tmp_path: Path,
) -> None:
    phone_asset = ROOT / "static" / "assets" / "phone-display.js"
    assert phone_asset.exists()
    source = phone_asset.read_text(encoding="utf-8")
    cache_bytes = phone_asset.read_bytes().replace(b"\r\n", b"\n")
    cache_key = hashlib.sha256(cache_bytes).hexdigest()[:12]
    assert f"phone-display.js?v={cache_key}" in INDEX
    assert f"phone-display.js?v={cache_key}" in LEGACY_CUSTOMERS
    harness = source + r'''
const samples = [
  null,
  " , / , ",
  "15895581373, / ,",
  "1, / ,",
  "0, / ,",
  "123456",
  "1111111",
  "0512-12345678,, / 13800138000//",
];
console.log(JSON.stringify(samples.map(value => TmPhone.analyze(value))));
'''
    rows = json.loads(_run_node(harness, tmp_path, "candidate-e-phone.js"))
    assert [row["text"] for row in rows] == [
        "",
        "",
        "15895581373",
        "1",
        "0",
        "123456",
        "1111111",
        "0512-12345678 / 13800138000",
    ]
    assert [row["needs_review"] for row in rows] == [
        False,
        False,
        False,
        True,
        True,
        True,
        True,
        False,
    ]
    assert rows[3]["raw"] == "1, / ,"
    assert rows[3]["reason"] == "疑似占位，待核对"


def test_customer_phone_cleanup_is_display_only_and_governance_is_read_only() -> None:
    assert '/static/assets/phone-display.js' in INDEX
    assert '/static/assets/phone-display.js' in LEGACY_CUSTOMERS
    assert "customerPhoneDisplay(row.phone)" in INDEX
    assert "疑似占位，待核对" in INDEX
    assert '$("phone").value = customer.phone || "";' in LEGACY_CUSTOMERS
    assert '$("phone").value = cleanPhone(customer.phone);' not in LEGACY_CUSTOMERS

    export = _method_block(
        "exportCustomerPhoneGovernance", "loadCustomerOptions"
    )
    assert 'axios.get("/api/master/customers"' in export
    assert "page_size:200" in export
    assert "include_inactive:true" in export
    assert "raw_phone" in export
    assert "display_phone" in export
    assert "疑似占位，待核对" in export
    assert r"/^\s*[=+\-@]/.test(original)" in export
    for write_call in ("axios.post", "axios.put", "axios.patch", "axios.delete"):
        assert write_call not in export


def test_external_purchase_confirmation_uses_explicit_utc_rfc3339() -> None:
    assert utc_naive_to_api(datetime(2026, 8, 12, 16, 30, 0)) == (
        "2026-08-12T16:30:00Z"
    )
    assert "from app.core.time_contract import beijing_today, utc_naive_to_api" in (
        PURCHASE_SERVICE
    )
    # The service now serializes cancellation timestamps through the same UTC
    # contract as well, so the safety assertion must not cap future fields at
    # the original three confirmation call sites.
    assert PURCHASE_SERVICE.count("utc_naive_to_api(") >= 3
    assert ".confirmed_at.isoformat()" not in PURCHASE_SERVICE
    assert ".cancelled_at.isoformat()" not in PURCHASE_SERVICE


def test_phone_governance_export_is_session_bound_and_logout_releases_state() -> None:
    export = _method_block(
        "exportCustomerPhoneGovernance", "loadCustomerOptions"
    )
    for marker in (
        "const requestGeneration = this.authGeneration",
        "const requestUserId = this.user?.id ?? null",
        "requestGeneration !== this.authGeneration",
        "requestUserId !== (this.user?.id ?? null)",
    ):
        assert marker in export
    reset_start = INDEX.index("resetPagePerformanceState() {")
    reset_end = INDEX.index("pageSearchIsCurrent", reset_start)
    assert "this.customerPhoneGovernanceExporting = false" in INDEX[
        reset_start:reset_end
    ]


def test_phone_governance_runtime_never_downloads_partial_or_cross_account_data(
    tmp_path: Path,
) -> None:
    start = INDEX.index("async exportCustomerPhoneGovernance() {")
    end = INDEX.index("async loadCustomerOptions(", start)
    method = INDEX[start:end].strip().rstrip(",")
    source = f"""
const method = ({{{method}}}).exportCustomerPhoneGovernance;
const analyze = value => ({{raw:String(value ?? ''),text:String(value ?? ''),needs_review:true,reason:'疑似占位，待核对'}});
let downloads=0,toasts=[];
global.Blob=class Blob{{constructor(parts){{this.parts=parts}}}};
global.URL={{createObjectURL(){{return 'blob:test'}},revokeObjectURL(){{}}}};
global.document={{createElement(){{return {{click(){{downloads+=1}}}}}}}};
global.setTimeout=callback=>callback();
async function run(mode){{
  let calls=0;
  const context={{
    authGeneration:1,user:{{id:7}},customerPhoneGovernanceExporting:false,
    hasPermission:()=>true,customerPhoneAnalysis:analyze,
    showToast(message,error){{toasts.push([message,!!error])}},errorMessage:error=>error.message,
  }};
  global.axios={{get:async()=>{{
    calls+=1;
    if(calls===1)return {{data:{{items:[{{customer_number:1,customer_code:'A',name:'客户A',phone:'1'}}],total_pages:2}}}};
    if(mode==='fail')throw new Error('page two failed');
    if(mode==='switch'){{
      context.authGeneration=2;
      context.user={{id:8}};
      context.customerPhoneGovernanceExporting=false;
    }}
    return {{data:{{items:[{{customer_number:2,customer_code:'B',name:'客户B',phone:'0'}}],total_pages:2}}}};
  }}}};
  await method.call(context);
  return {{downloads,toasts,locked:context.customerPhoneGovernanceExporting}};
}}
(async()=>{{
  const failed=await run('fail');downloads=0;toasts=[];
  const switched=await run('switch');
  console.log(JSON.stringify({{failed,switched}}));
}})().catch(error=>{{console.error(error);process.exit(1)}});
"""
    result = json.loads(_run_node(source, tmp_path, "candidate-e-governance.js"))
    assert result["failed"]["downloads"] == 0
    assert result["failed"]["locked"] is False
    assert result["failed"]["toasts"][-1][1] is True
    assert result["switched"] == {"downloads": 0, "toasts": [], "locked": False}


def test_phone_governance_csv_formula_prefix_is_encoded_as_text(tmp_path: Path) -> None:
    start = INDEX.index("const csvCell = value => {")
    end = INDEX.index("const csv =", start)
    block = INDEX[start:end]
    source = block + r'''
console.log(JSON.stringify([
  csvCell("=HYPERLINK(\"https://example.invalid\")"),
  csvCell(" +SUM(1,1)"),
  csvCell("13800138000"),
]));
'''
    cells = json.loads(_run_node(source, tmp_path, "candidate-e-csv.js"))
    assert cells[0].startswith('"\'=HYPERLINK')
    assert cells[1].startswith('"\' +SUM')
    assert cells[2] == '"13800138000"'


def test_modern_customer_editor_and_payload_keep_raw_phone(tmp_path: Path) -> None:
    start = INDEX.index("openCustomer(row=null) {")
    end = INDEX.index("contractStatusText", start)
    opener = INDEX[start:end].strip().rstrip(",")
    payload_start = INDEX.index("buildCustomerWritePayload() {")
    payload_end = INDEX.index("async prepareCustomerChangeConfirmation", payload_start)
    payload = INDEX[payload_start:payload_end].strip().rstrip(",")
    source = f"""
const openCustomer = ({{{opener}}}).openCustomer;
const buildCustomerWritePayload = ({{{payload}}}).buildCustomerWritePayload;
const raw='1, / ,';
const context={{
  customers:[],customerForm:null,canManageInvoiceProfiles:false,canEditCustomers:true,
  customerInvoiceState:{{}},
  resetCustomerFinishedStorage(){{}},loadCustomerFinishedStorage(){{}},
  loadCustomerQuotePreferences(){{}},blankCustomerQuotePreferenceDraft(){{return {{}}}},
  beginMasterEdit(){{}},ensureCustomerQuotePreferenceOptions(){{}},
}};
openCustomer.call(context,{{id:3,customer_number:3,phone:raw,statement_cycle_start_day:20}});
const output=buildCustomerWritePayload.call({{customerForm:context.customerForm}});
console.log(JSON.stringify({{form:context.customerForm.phone,payload:output.phone}}));
"""
    assert json.loads(_run_node(source, tmp_path, "candidate-e-raw-editor.js")) == {
        "form": "1, / ,",
        "payload": "1, / ,",
    }


def test_print_confirmation_date_is_beijing_date_and_rejects_naive_input(
    tmp_path: Path,
) -> None:
    assert "TmTime.formatBeijingDate(value)" in PRINT_PAGE
    assert "TmTime.formatBusinessDate(value)" not in PRINT_PAGE
    source = (ROOT / "static" / "assets" / "time-utils.js").read_text(
        encoding="utf-8"
    )
    harness = source + r'''
const output = {
  utcCrossDay: TmTime.formatBeijingDate("2026-08-12T16:30:00Z"),
  offsetSameDay: TmTime.formatBeijingDate("2026-08-12T23:30:00+08:00"),
  rejectedNaive: false,
};
try { TmTime.formatBeijingDate("2026-08-12T16:30:00"); }
catch (_) { output.rejectedNaive = true; }
console.log(JSON.stringify(output));
'''
    assert json.loads(_run_node(harness, tmp_path, "candidate-e-time.js")) == {
        "utcCrossDay": "2026-08-13",
        "offsetSameDay": "2026-08-12",
        "rejectedNaive": True,
    }


def test_print_page_keeps_print_disabled_for_missing_or_invalid_confirmation_date(
    tmp_path: Path,
) -> None:
    inline = [
        source
        for source in re.findall(
            r"<script(?:\s[^>]*)?>(.*?)</script>", PRINT_PAGE, flags=re.DOTALL
        )
        if source.strip()
    ]
    assert len(inline) == 1
    display_start = inline[0].index("function displayDate")
    display_end = inline[0].index("function parseErrorDetail", display_start)
    display_source = inline[0][display_start:display_end]
    time_source = (ROOT / "static" / "assets" / "time-utils.js").read_text(
        encoding="utf-8"
    )
    harness = time_source + display_source + r'''
const values = [null, "2026-08-12T16:30:00", "invalid"];
console.log(JSON.stringify(values.map(value => {
  try { return {ok:true,value:displayDate(value)}; }
  catch (error) { return {ok:false,message:error.message}; }
})));
'''
    rows = json.loads(_run_node(harness, tmp_path, "candidate-e-print-date.js"))
    assert all(row["ok"] is False for row in rows)
    assert "日期缺失" in rows[0]["message"]
    assert all("日期格式异常" in row["message"] for row in rows[1:])
    render_start = PRINT_PAGE.index("function renderPurchase")
    render_end = PRINT_PAGE.index("async function loadPurchase", render_start)
    render = PRINT_PAGE[render_start:render_end]
    assert render.index("const confirmedDate=displayDate(data.confirmed_at)") < (
        render.index("printButton.disabled=false")
    )


def test_external_purchase_confirmation_three_public_shapes_are_rfc3339(
    purchase_app,
) -> None:
    from app.services.external_packaging_purchase import (
        get_external_purchase_summary,
    )
    from fastapi.testclient import TestClient
    from tests.test_p1_33c3_external_packaging_purchase_confirmation import (
        _confirmation_payload,
        _login,
    )

    order_id = purchase_app.state.fixture["order_id"]
    with TestClient(purchase_app) as client:
        _login(client, "purchase-admin")
        preview = client.get(
            f"/api/orders/{order_id}/external-packaging-purchase"
        ).json()
        response = client.post(
            f"/api/orders/{order_id}/external-packaging-purchase/confirm",
            json=_confirmation_payload(preview, "candidate-e-rfc3339"),
        )
        assert response.status_code == 200, response.text
        confirmation = response.json()["confirmation"]
        purchase_id = confirmation["purchase_orders"][0]["id"]
        summary = client.get(
            f"/api/orders/{order_id}/external-packaging-purchase"
        ).json()
        printed = client.get(
            f"/api/external-packaging-purchases/{purchase_id}/print"
        ).json()
    with purchase_app.state.session_factory() as db:
        service_summary = get_external_purchase_summary(db, order_id)

    values = [
        confirmation["confirmed_at"],
        summary["confirmation"]["confirmed_at"],
        service_summary["confirmed_at"],
        printed["confirmed_at"],
    ]
    assert all(
        re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})",
            value,
        )
        for value in values
    )


def test_order_flow_count_keeps_existing_math_and_explains_each_quantity() -> None:
    menu_start = INDEX.index("menus() {")
    menu_end = INDEX.index("deliveryCustomers()", menu_start)
    menu = INDEX[menu_start:menu_end]
    assert (
        "count: this.ordersUnfinishedTotal + this.requisitionPendingOverallTotal + "
        "this.incomingPendingTotal"
    ) in menu
    for label in ("订单", "报料", "纸板待入库"):
        assert label in menu
    assert "item.countDetails" in INDEX
    assert "待办" in INDEX


def test_supplier_print_remains_read_only_and_structurally_redacted() -> None:
    assert "/api/external-packaging-purchases/${encodeURIComponent(id)}/print" in (
        PRINT_PAGE
    )
    for write_method in (
        'method:"POST"',
        'method:"PUT"',
        'method:"PATCH"',
        'method:"DELETE"',
    ):
        assert write_method not in PRINT_PAGE
    for forbidden in (
        "source_customer_name",
        "source_order_number",
        "source_item_order_number",
        "unit_price",
        "currency",
        "tax_rate",
        "line_amount",
        "total_amount",
        "order_drawing_file_name",
    ):
        assert forbidden not in PRINT_PAGE


def test_deterministic_display_keeps_current_release_external_acceptance_gate() -> None:
    # The concrete phone, RFC3339 and display contracts are asserted in this
    # module.  Historical candidate wording is not a permanent current-version
    # title; later releases retain the durable external-acceptance flag.
    assert "APP_VERSION = " in VERSION
    assert "APP_VERSION_NAME = " in VERSION
    assert "APP_EXTERNAL_ACCEPTANCE_REQUIRED = True" in VERSION
