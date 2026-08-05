from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def _method_body(name: str) -> str:
    pattern = rf"(?:async\s+)?{re.escape(name)}\([^)]*\)\s*\{{"
    match = re.search(pattern, INDEX)
    assert match is not None, f"missing Vue method: {name}"
    next_method = re.search(
        r"\n\s{10,}(?:async\s+)?[A-Za-z_$][\w$]*\([^)]*\)\s*\{",
        INDEX[match.end() :],
    )
    assert next_method is not None, f"cannot delimit Vue method: {name}"
    return INDEX[match.start() : match.end() + next_method.start()]


def test_delivery_modal_is_selection_first_without_blank_manual_rows() -> None:
    start = INDEX.index('<div v-else-if="modal.type === \'delivery\'">')
    end = INDEX.index('<div v-else-if="modal.type === \'tianhuaPreimport\'">', start)
    modal = INDEX[start:end]

    assert "选择待送订单" in modal
    assert "选择客户专用库存" in modal
    assert "新增一行" not in modal
    assert "新增5行" not in modal
    assert "新增 5 行" not in modal
    assert 'v-if="deliveryBatchPicker.loading"' in modal
    assert "正在加载待送订单" in modal


def test_saved_draft_exposes_pick_and_one_step_dispatch_print() -> None:
    assert "modal?.type === 'delivery' && deliveryForm.editingId && !deliveryFormIsDirty()" in INDEX
    assert "createCurrentDeliveryPickTask" in INDEX
    assert "deliveryPrimaryAction()" in INDEX
    label = _method_body("deliveryPrimaryLabel")
    primary = _method_body("deliveryPrimaryAction")
    pick = _method_body("createCurrentDeliveryPickTask")
    dispatch = _method_body("dispatchCurrentDeliveryDraft")

    assert ': "发货打印"' in label
    assert "dispatchCurrentDeliveryDraft" in primary
    assert "createDeliveryPickTask" in pick
    assert "dispatchDelivery" not in pick
    assert "dispatchDelivery" in dispatch


def test_unordered_finished_selection_is_a_valid_saveable_draft(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node, "Node.js is required for delivery frontend behavior validation"
    method_names = (
        "createDeliveryLine",
        "unorderedFinishedLotId",
        "unorderedFinishedAvailableQuantity",
        "isUnorderedFinishedLotInForm",
        "importUnorderedFinishedCandidates",
        "deliveryLineHasInput",
        "deliverySourceModeFromLines",
        "validateDeliveryForm",
    )
    methods = ",\n".join(_method_body(name).strip().rstrip(",") for name in method_names)
    script = tmp_path / "p1-09c-36-unordered-save.js"
    script.write_text(
        f"""
const methods = {{
{methods}
}};
const notices = [];
const context = {{
  deliveryForm: {{customer_id: 7, source_tab: "unordered_finished", lines: []}},
  unorderedFinishedPicker: {{
    visible: true,
    selected: {{
      55: {{
        _selected: true,
        _quantity: 3,
        inventory_lot_id: 55,
        product_id: 9,
        product_code: "UAT-STOCK",
        product_name: "匿名库存成品",
        specification: "300×200×150",
        available_quantity: 5,
        order_pending_quantity: 0,
        unit_price: 0,
      }},
    }},
  }},
  showToast(message, error) {{ notices.push([message, error]); }},
}};
for (const name of Object.keys(methods)) context[name] = methods[name];
methods.importUnorderedFinishedCandidates.call(context);
if (context.deliveryForm.lines.length !== 1) throw new Error("selected inventory must become one draft line");
const line = context.deliveryForm.lines[0];
if (line.source_type !== "unordered_finished" || line.delivered_quantity !== 3) throw new Error("unordered quantity was not preserved");
if (line.unit_price !== null) throw new Error("zero default price must stay pending instead of blocking draft save");
if (line.allocations.length !== 1 || line.allocations[0].inventory_lot_id !== 55 || line.allocations[0].quantity !== 3) throw new Error("lot allocation was not preserved");
const validation = methods.validateDeliveryForm.call(context);
if (validation) throw new Error("valid unordered inventory draft was blocked: " + validation);
line.unit_price = 0;
const explicitZeroValidation = methods.validateDeliveryForm.call(context);
if (explicitZeroValidation) throw new Error("explicit zero must remain a saveable pending price: " + explicitZeroValidation);
if (methods.deliverySourceModeFromLines.call(context, context.deliveryForm.lines) !== "unordered_finished") throw new Error("source mode must remain unordered_finished");
if (notices.some(([, error]) => error)) throw new Error("valid import must not show an error: " + JSON.stringify(notices));
""",
        encoding="utf-8",
    )
    result = subprocess.run(
        [node, str(script)],
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_unordered_zero_price_is_submitted_as_pending() -> None:
    save = INDEX.split('if (this.modal.type === "delivery") {', 1)[1].split(
        'if (this.modal.type === "statement") {', 1
    )[0]

    assert 'unit_price: Number(line.unit_price || 0) > 0 ? Number(line.unit_price) : null' in save
    assert 'Number(line.unit_price) < 0' in _method_body("validateDeliveryForm")


def test_delivery_inline_script_is_valid(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node, "Node.js is required for frontend syntax validation"
    scripts = [
        script
        for script in re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", INDEX, re.DOTALL)
        if script.strip()
    ]
    assert len(scripts) == 1
    target = tmp_path / "p1-09c-36-index.js"
    target.write_text(scripts[0], encoding="utf-8")
    result = subprocess.run(
        [node, "--check", str(target)],
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
