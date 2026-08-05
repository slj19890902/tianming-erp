from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def _method_body(signature: str, next_signature: str) -> str:
    return INDEX.split(signature, 1)[1].split(next_signature, 1)[0].rsplit("}", 1)[0]


def _run_node(source: str, tmp_path: Path, name: str) -> None:
    node = shutil.which("node")
    assert node is not None, "Node.js is required for the invoice seller regression"
    target = tmp_path / name
    target.write_text(source, encoding="utf-8")
    result = subprocess.run(
        [node, str(target)], capture_output=True, text=True, encoding="utf-8"
    )
    assert result.returncode == 0, result.stderr


def test_invoice_seller_modal_has_loading_retry_and_save_lock_contract() -> None:
    modal = INDEX[
        INDEX.index("<div v-else-if=\"modal.type === 'invoiceSellers'\"") :
        INDEX.index("<div v-else-if=\"modal.type === 'customer'\"")
    ]
    assert "invoiceSellerState" in INDEX
    assert "正在读取销方主体" in modal
    assert "销方主体读取失败" in modal
    assert "重新加载" in modal
    assert "保存中…" in modal
    assert ':disabled="!invoiceSellerState.loaded || invoiceSellerState.saving"' in modal

    open_body = _method_body(
        "async openInvoiceSellers() {", "editInvoiceSeller(seller) {"
    )
    assert open_body.index('this.modal = {type:"invoiceSellers"') < open_body.index(
        "await this.loadInvoiceSellers()"
    )


def test_invoice_seller_old_response_and_failure_cannot_expose_stale_list(
    tmp_path: Path,
) -> None:
    body = _method_body(
        "async loadInvoiceSellers() {", "async openInvoiceSellers() {"
    )
    script = f"""
const AsyncFunction = Object.getPrototypeOf(async function(){{}}).constructor;
globalThis.latestRequestControllers = new Map();
const pending = [];
globalThis.axios = {{ get(url, options) {{ return new Promise((resolve, reject) => pending.push({{resolve, reject, options}})); }} }};
const vm = {{
  canManageInvoiceProfiles: true,
  invoiceSellers: [{{seller_name:"原列表"}}],
  invoiceSellerState: {{loading:false,error:"",saving:false,loaded:true}},
  beginLatestRequest(key) {{
    latestRequestControllers.get(key)?.abort();
    const controller = new AbortController();
    latestRequestControllers.set(key, controller);
    return controller;
  }},
  finishLatestRequest(key, controller) {{ if (latestRequestControllers.get(key) === controller) latestRequestControllers.delete(key); }},
  isCancelledRequest(error) {{ return error?.name === "AbortError" || error?.code === "ERR_CANCELED"; }},
  errorMessage(error) {{ return error?.message || String(error); }}
}};
vm.loadInvoiceSellers = new AsyncFunction({json.dumps(body, ensure_ascii=False)}).bind(vm);

(async () => {{
  const first = vm.loadInvoiceSellers();
  const second = vm.loadInvoiceSellers();
  pending[1].resolve({{data:{{items:[{{seller_name:"最新销方"}}]}}}});
  const secondOk = await second;
  pending[0].reject(new Error("旧请求失败"));
  await first;
  if (!secondOk) throw new Error("latest successful load did not return true");
  if (vm.invoiceSellers.length !== 1 || vm.invoiceSellers[0].seller_name !== "最新销方") throw new Error("stale request changed latest sellers");
  if (vm.invoiceSellerState.error || !vm.invoiceSellerState.loaded || vm.invoiceSellerState.loading) throw new Error("latest seller state is incorrect");

  const failed = vm.loadInvoiceSellers();
  pending[2].reject(new Error("网络断开"));
  const failedOk = await failed;
  if (failedOk !== false) throw new Error("failed load must return false");
  if (vm.invoiceSellers.length !== 0) throw new Error("failed current request retained stale sellers");
  if (!vm.invoiceSellerState.error.includes("网络断开")) throw new Error("current load error was not shown");
  if (vm.invoiceSellerState.loaded || vm.invoiceSellerState.loading) throw new Error("failed load left the form enabled");
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    _run_node(script, tmp_path, "invoice-seller-load-race.js")


def test_invoice_seller_save_is_single_flight_and_clears_form(tmp_path: Path) -> None:
    body = _method_body(
        "async saveInvoiceSeller() {", "async loadCustomerInvoiceProfile("
    )
    script = f"""
const AsyncFunction = Object.getPrototypeOf(async function(){{}}).constructor;
const pendingWrites = [];
globalThis.axios = {{
  post(url, payload) {{ return new Promise((resolve, reject) => pendingWrites.push({{method:"post",url,payload,resolve,reject}})); }},
  put(url, payload) {{ return new Promise((resolve, reject) => pendingWrites.push({{method:"put",url,payload,resolve,reject}})); }}
}};
const notices = [];
const vm = {{
  invoiceSellerForm: {{id:null,seller_name:"天明销方",tax_no:"9132TEST",address_phone:"",bank_account:"",is_active:true,version:null}},
  invoiceSellerState: {{loading:false,error:"",saving:false,loaded:true}},
  resetInvoiceSellerForm() {{ this.invoiceSellerForm={{id:null,seller_name:"",tax_no:"",address_phone:"",bank_account:"",is_active:true,version:null}}; }},
  async loadInvoiceSellers() {{ return true; }},
  showToast(message, error) {{ notices.push([message,error]); }},
  errorMessage(error) {{ return error?.message || String(error); }}
}};
vm.saveInvoiceSeller = new AsyncFunction({json.dumps(body, ensure_ascii=False)}).bind(vm);

(async () => {{
  const first = vm.saveInvoiceSeller();
  const duplicate = vm.saveInvoiceSeller();
  if (pendingWrites.length !== 1) throw new Error("duplicate click issued more than one seller write");
  pendingWrites[0].resolve({{data:{{id:1}}}});
  await Promise.all([first, duplicate]);
  if (vm.invoiceSellerState.saving) throw new Error("save lock was not released");
  if (vm.invoiceSellerForm.seller_name || vm.invoiceSellerForm.tax_no) throw new Error("successful save did not clear seller form");
  if (!notices.some(([message]) => message.includes("销方主体已新增"))) throw new Error("success was not reported");

  vm.invoiceSellerForm={{id:9,seller_name:"编辑销方",tax_no:"9132EDIT",address_phone:"",bank_account:"",is_active:true,version:7}};
  vm.invoiceSellerState.loaded=true;
  vm.loadInvoiceSellers=async () => false;
  const edited = vm.saveInvoiceSeller();
  if (pendingWrites.length !== 2 || pendingWrites[1].method !== "put") throw new Error("edit did not issue one PUT");
  if (pendingWrites[1].payload.expected_version !== 7) throw new Error("edit lost expected_version");
  pendingWrites[1].resolve({{data:{{id:9}}}});
  await edited;
  if (!notices.some(([message,error]) => message.includes("资料已保存") && message.includes("不要重复") && error === true)) throw new Error("refresh failure did not protect against repeat save");
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    _run_node(script, tmp_path, "invoice-seller-save-lock.js")


def test_invoice_seller_frontend_inline_javascript_is_valid(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node is not None
    scripts = [
        script
        for script in re.findall(
            r"<script(?:\s[^>]*)?>(.*?)</script>", INDEX, re.DOTALL
        )
        if script.strip()
    ]
    assert len(scripts) == 1
    target = tmp_path / "invoice-seller-inline.js"
    target.write_text(scripts[0], encoding="utf-8")
    result = subprocess.run(
        [node, "--check", str(target)], capture_output=True, text=True, encoding="utf-8"
    )
    assert result.returncode == 0, result.stderr
