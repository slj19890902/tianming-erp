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


def test_invoice_pdf_upload_has_real_file_input_and_busy_error_ui() -> None:
    view = INDEX[
        INDEX.index("v-else-if=\"financeView==='invoice_tasks'\"") :
        INDEX.index("<template v-else>", INDEX.index("v-else-if=\"financeView==='invoice_tasks'\""))
    ]
    assert 'ref="invoiceTaskPdfInput"' in view
    assert 'type="file"' in view
    assert 'accept="application/pdf,.pdf"' in view
    assert '@change="onInvoiceTaskPdfSelected"' in view
    assert "invoiceTaskUploadState.uploading" in view
    assert "invoiceTaskUploadState.error" in view
    assert "上传中…" in view
    assert "canManageInvoiceAttachment && task.status==='issued'" in view


def test_invoice_pdf_upload_cancel_single_flight_success_and_failure(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node is not None, "Node.js is required for the invoice PDF upload regression"
    choose_body = _method_body(
        "uploadInvoiceTaskPdf(task) {", "retryInvoiceTaskPdf() {"
    )
    selected_body = _method_body(
        "async onInvoiceTaskPdfSelected(event) {", "async openCustomerInvoiceProfile(customerId) {"
    )
    script = f"""
const AsyncFunction = Object.getPrototypeOf(async function(){{}}).constructor;
class FakeFormData {{
  constructor() {{ this.values = new Map(); }}
  append(key, value, filename) {{ this.values.set(key, {{value, filename}}); }}
  get(key) {{ return this.values.get(key); }}
}}
globalThis.FormData = FakeFormData;
const pending = [];
globalThis.axios = {{
  post(url, payload) {{
    return new Promise((resolve, reject) => pending.push({{url, payload, resolve, reject}}));
  }}
}};
let pickerClicks = 0;
let listLoads = 0;
const messages = [];
const input = {{value:"old", files:[], click(){{ pickerClicks += 1; }}}};
const vm = {{
  canManageInvoiceAttachment:true,
  $refs:{{invoiceTaskPdfInput:input}},
  invoiceTaskUploadState:{{uploading:false,taskId:null,taskNumber:"",error:""}},
  async loadInvoiceTasks() {{ listLoads += 1; return true; }},
  errorMessage(error) {{ return error?.message || String(error); }},
  showToast(message, danger=false) {{ messages.push({{message,danger}}); }}
}};
vm.uploadInvoiceTaskPdf = new Function("task", {json.dumps(choose_body, ensure_ascii=False)}).bind(vm);
vm.onInvoiceTaskPdfSelected = new AsyncFunction("event", {json.dumps(selected_body, ensure_ascii=False)}).bind(vm);

(async () => {{
  if (!vm.uploadInvoiceTaskPdf({{id:7,task_number:"INV-TASK-7"}})) throw new Error("file picker did not open");
  if (pickerClicks !== 1 || input.value !== "" || vm.invoiceTaskUploadState.taskId !== 7) throw new Error("upload target was not frozen before selection");

  const cancelled = await vm.onInvoiceTaskPdfSelected({{target:{{files:[],value:""}}}});
  if (cancelled !== false || pending.length || vm.invoiceTaskUploadState.error) throw new Error("cancelled selection created side effects");

  const invalidInput = {{files:[{{name:"invoice.txt",type:"text/plain"}}],value:"invoice.txt"}};
  const invalid = await vm.onInvoiceTaskPdfSelected({{target:invalidInput}});
  if (invalid !== false || pending.length || !vm.invoiceTaskUploadState.error.includes("PDF") || invalidInput.value !== "") {{
    throw new Error("invalid extension was not rejected locally");
  }}

  vm.uploadInvoiceTaskPdf({{id:7,task_number:"INV-TASK-7"}});
  const file = {{name:"invoice.pdf",type:"application/pdf"}};
  const validInput = {{files:[file],value:"invoice.pdf"}};
  const saving = vm.onInvoiceTaskPdfSelected({{target:validInput}});
  await Promise.resolve();
  const duplicatePromise = vm.onInvoiceTaskPdfSelected({{target:validInput}});
  await Promise.resolve();
  if (pending.length !== 1 || !vm.invoiceTaskUploadState.uploading) throw new Error("upload was duplicated or not locked");
  if (pending[0].url !== "/api/finance/invoice-tasks/7/attachments/original") throw new Error("wrong upload endpoint");
  const uploaded = pending[0].payload.get("file");
  if (uploaded?.value !== file || uploaded?.filename !== "invoice.pdf") throw new Error("PDF was not sent in the file field");
  if (await duplicatePromise !== false) throw new Error("duplicate upload was not rejected");
  pending[0].resolve({{data:{{id:1}}}});
  if (await saving !== true) throw new Error("successful upload did not report success");
  if (vm.invoiceTaskUploadState.uploading || vm.invoiceTaskUploadState.error || validInput.value !== "" || listLoads !== 1) {{
    throw new Error("successful upload did not refresh and reset state");
  }}

  vm.uploadInvoiceTaskPdf({{id:8,task_number:"INV-TASK-8"}});
  const failedInput = {{files:[file],value:"invoice.pdf"}};
  const failed = vm.onInvoiceTaskPdfSelected({{target:failedInput}});
  pending[1].reject(new Error("原始 PDF 已存在"));
  if (await failed !== false) throw new Error("failed upload returned success");
  if (vm.invoiceTaskUploadState.uploading || !vm.invoiceTaskUploadState.error.includes("原始 PDF 已存在")) {{
    throw new Error("failed upload did not retain a retryable error");
  }}
  if (!messages.some(row => row.danger && row.message.includes("原始 PDF 已存在"))) throw new Error("failed upload was not explained");
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    target = tmp_path / "invoice-pdf-upload.js"
    target.write_text(script, encoding="utf-8")
    result = subprocess.run(
        [node, str(target)], capture_output=True, text=True, encoding="utf-8"
    )
    assert result.returncode == 0, result.stderr


def test_invoice_pdf_upload_inline_javascript_is_valid(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node is not None, "Node.js is required for the frontend syntax check"
    scripts = [
        script
        for script in re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", INDEX, re.DOTALL)
        if script.strip()
    ]
    assert len(scripts) == 1
    target = tmp_path / "invoice-pdf-upload-inline.js"
    target.write_text(scripts[0], encoding="utf-8")
    result = subprocess.run(
        [node, "--check", str(target)], capture_output=True, text=True, encoding="utf-8"
    )
    assert result.returncode == 0, result.stderr
