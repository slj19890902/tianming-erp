from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def _inline_script() -> str:
    scripts = [
        script
        for script in re.findall(
            r"<script(?:\s[^>]*)?>(.*?)</script>", INDEX, flags=re.DOTALL
        )
        if script.strip()
    ]
    assert len(scripts) == 1
    return scripts[0]


def _method_body(name: str) -> str:
    """Return one Vue method without relying on source line numbers."""

    pattern = rf"(?:async\s+)?{re.escape(name)}\([^)]*\)\s*\{{"
    match = re.search(pattern, INDEX)
    assert match is not None, f"missing Vue method: {name}"
    next_method = re.search(
        r"\n\s{10,}(?:async\s+)?[A-Za-z_$][\w$]*\([^)]*\)\s*\{",
        INDEX[match.end() :],
    )
    assert next_method is not None, f"cannot delimit Vue method: {name}"
    return INDEX[match.start() : match.end() + next_method.start()]


def _delivery_save_branch() -> str:
    body = _method_body("saveModal")
    start = body.index('if (this.modal.type === "delivery")')
    end = body.index('if (this.modal.type === "statement")', start)
    return body[start:end]


def _run_node(source: str) -> subprocess.CompletedProcess[str]:
    node = shutil.which("node")
    assert node is not None, "Node.js is required for the delivery draft contract"
    return subprocess.run(
        [node],
        input=source,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )


def _vue_harness(assertions: str) -> str:
    return f"""
const vm = require("vm");
const writes = [];
const sandbox = {{
  axios: {{
    defaults: {{}},
    interceptors: {{ response: {{ use() {{}} }} }},
    get() {{ throw new Error("unexpected GET"); }},
    post(...args) {{ writes.push(["post", ...args]); }},
    put(...args) {{ writes.push(["put", ...args]); }},
    delete(...args) {{ writes.push(["delete", ...args]); }},
  }},
  Vue: {{
    createApp(definition) {{
      sandbox.definition = definition;
      return {{ component() {{ return this; }}, mount() {{ return this; }} }};
    }},
  }},
  localStorage: {{ getItem() {{ return null; }}, setItem() {{}}, removeItem() {{}} }},
  window: {{ addEventListener() {{}}, open() {{}} }},
  URLSearchParams,
  console,
  setTimeout,
  clearTimeout,
}};
vm.createContext(sandbox);
vm.runInContext({json.dumps(_inline_script())}, sandbox);
const methods = sandbox.definition.methods;
{assertions}
"""


def test_import_selected_closes_picker_and_never_issues_a_write_request() -> None:
    """Picking/importing is only local draft assembly, never a database action."""

    result = _run_node(
        _vue_harness(
            """
const context = {
  deliveryForm: { lines: [] },
  deliveryBatchPicker: {
    visible: true,
    selected: { 101: true },
    items: [{ order_item_id: 101, order_remaining_quantity: 12, product_code: "P1-15" }],
  },
  isDeliveryItemAlreadyInForm() { return false; },
  emptyDeliveryLine() { return null; },
  createDeliveryLine() { return { key: "local-line" }; },
  applyDeliveryBatchItemToLine(line, item) {
    line.order_item_id = item.order_item_id;
    line.delivered_quantity = item.order_remaining_quantity;
  },
  clearDeliveryBatchSelection: methods.clearDeliveryBatchSelection,
  showToast() {},
};
methods.importSelectedDeliveryBatchItems.call(context);
if (writes.length !== 0) throw new Error("import must not issue axios writes: " + JSON.stringify(writes));
if (context.deliveryForm.lines.length !== 1 || context.deliveryForm.lines[0].order_item_id !== 101) {
  throw new Error("selected candidate was not kept as a local draft line");
}
if (context.deliveryBatchPicker.visible !== false) {
  throw new Error("candidate picker must close after importing selected lines");
}
"""
        )
    )
    assert result.returncode == 0, result.stderr


def test_delivery_primary_action_has_draft_save_edit_and_print_states() -> None:
    """The modal's primary action must never imply dispatching a saved draft."""

    assert re.search(
        r'@click="modal\?\.type\s*===\s*[\'\"]delivery[\'\"]\s*\?\s*'
        r'deliveryPrimaryAction\(\)\s*:\s*saveModal\(\)"',
        INDEX,
    )
    assert "deliveryPrimaryLabel()" in INDEX

    for name in (
        "deliveryFormIsDirty",
        "deliveryPrimaryLabel",
        "deliveryPrimaryAction",
        "printCurrentDeliveryDraft",
    ):
        _method_body(name)
    label_body = _method_body("deliveryPrimaryLabel")
    assert 'return "保存草稿"' in label_body
    assert '"保存修改"' in label_body
    assert '"打印"' in label_body

    result = _run_node(
        _vue_harness(
            """
function context(editingId, savedSignature, currentSignature) {
    return {
      deliveryForm: { editingId, saved_signature: savedSignature },
      deliveryFormSignature() { return currentSignature; },
      deliveryFormHasUnsavedChanges: methods.deliveryFormHasUnsavedChanges,
      deliveryFormIsDirty: methods.deliveryFormIsDirty,
    saveCalls: 0,
    printCalls: 0,
    saveModal() { this.saveCalls += 1; return "saved"; },
    printCurrentDeliveryDraft() { this.printCalls += 1; return "printed"; },
  };
}
const firstSave = context(null, "", "first");
const cleanSaved = context(55, "same", "same");
const changedSaved = context(55, "before", "after");
methods.deliveryPrimaryAction.call(firstSave);
methods.deliveryPrimaryAction.call(cleanSaved);
methods.deliveryPrimaryAction.call(changedSaved);
if (firstSave.saveCalls !== 1 || firstSave.printCalls !== 0) throw new Error("first action must save draft");
if (cleanSaved.saveCalls !== 0 || cleanSaved.printCalls !== 1) throw new Error("clean draft must print only");
if (changedSaved.saveCalls !== 1 || changedSaved.printCalls !== 0) throw new Error("changed draft must save changes");
"""
        )
    )
    assert result.returncode == 0, result.stderr


def test_first_save_keeps_delivery_modal_open_and_sets_editing_id() -> None:
    branch = _delivery_save_branch()

    assert "this.deliveryForm.editingId = Number(data.id)" in branch
    assert "this.deliveryForm.saved_signature = this.deliveryFormSignature()" in branch
    assert 'type:"delivery"' in branch
    assert "return true;" in branch
    assert "closeModal" not in branch


def test_draft_print_opens_only_print_page_without_dispatch_or_print_status_write() -> None:
    body = _method_body("printCurrentDeliveryDraft")

    assert "window.open" in body
    assert "/delivery-print.html?id=" in body
    assert "/dispatch" not in body
    assert "/printed" not in body
    assert "axios." not in body


def test_dirty_saved_delivery_requires_confirmation_before_closing() -> None:
    body = _method_body("closeModal")

    assert 'this.modal?.type === "delivery"' in body
    assert "this.deliveryFormHasUnsavedChanges()" in body
    assert "送货草稿有未保存的修改，确定要关闭吗？" in body

    result = _run_node(
        _vue_harness(
            """
function context() {
  return {
    isForcedPassword: false,
    modal: { type: "delivery" },
    deliveryForm: { editingId: null, saved_signature: "before" },
    deliveryFormSignature() { return "after"; },
    deliveryFormHasUnsavedChanges: methods.deliveryFormHasUnsavedChanges,
    _productFormDirty() { return false; },
    productEditReturnContext: null,
    statementDetail: { id: 1 },
    statementEditForm: { id: 1 },
    productFormSnapshot: {},
    bomEditor: {},
    masterEditBaseline: {},
    masterVersionConflict: {},
    masterChangeConfirm: {},
    masterPendingSaveOptions: {},
  };
}
const refused = context();
sandbox.confirm = () => false;
methods.closeModal.call(refused);
if (!refused.modal) throw new Error("refusing confirmation must keep the dirty draft open");
const accepted = context();
sandbox.confirm = () => true;
methods.closeModal.call(accepted);
if (accepted.modal !== null) throw new Error("accepting confirmation must close the dirty draft");
const clean = context();
clean.deliveryForm.saved_signature = "after";
sandbox.confirm = () => { throw new Error("clean draft must close without confirmation"); };
methods.closeModal.call(clean);
if (clean.modal !== null) throw new Error("clean draft must close directly");
"""
        )
    )
    assert result.returncode == 0, result.stderr


def test_new_delivery_records_its_initial_signature_and_all_close_paths_are_guarded() -> None:
    open_body = _method_body("openDelivery")
    mask_body = _method_body("onModalMaskClick")

    assert "this.deliveryForm.saved_signature = this.deliveryFormSignature()" in open_body
    assert '@click.self="onModalMaskClick"' in INDEX
    assert "this.closeModal();" in mask_body
    assert '@click="closeModal"' in INDEX
    mounted = INDEX[INDEX.index("document.addEventListener(\"keydown\"") :]
    assert 'if (e.key !== "Escape") return;' in mounted
    assert "this.closeModal();" in mounted


def test_delivery_inline_javascript_passes_node_check(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node is not None, "Node.js is required for frontend syntax validation"
    target = tmp_path / "p1-15-delivery-draft.js"
    target.write_text(_inline_script(), encoding="utf-8")
    result = subprocess.run(
        [node, "--check", str(target)],
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
