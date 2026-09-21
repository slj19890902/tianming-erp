from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def test_order_entry_template_declares_keyboard_path_and_discoverable_save() -> None:
    for expected in (
        'data-order-entry-focus="customer"',
        'data-order-entry-focus="customer-po"',
        'data-order-entry-next="order-date"',
        'data-order-entry-next="requisition-strategy"',
        'data-order-entry-next="product-0"',
        'data-order-entry-focus="add-line"',
        "data-order-entry-enter-next",
        "onOrderEntryKeydown($event)",
        "title=\"保存（Ctrl+Enter）\"",
        'this.modal?.type === "order"',
        '<label>备注（选填）</label><textarea class="input" rows="3" v-model.trim="orderForm.remark"></textarea>',
    ):
        assert expected in INDEX


def test_order_entry_keyboard_handler_preserves_composition_textarea_and_save_boundaries(
    tmp_path: Path,
) -> None:
    node = shutil.which("node")
    assert node is not None
    scripts = [
        script
        for script in re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", INDEX, re.DOTALL)
        if script.strip()
    ]
    assert len(scripts) == 1
    app_script = tmp_path / "t03-order-keyboard-app.js"
    app_script.write_text(scripts[0], encoding="utf-8")
    harness = tmp_path / "t03-order-keyboard.cjs"
    harness.write_text(
        r"""
const fs = require("fs");
const vm = require("vm");
const html = fs.readFileSync(process.argv[2], "utf8");
const source = [...html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)].map(m => m[1]).filter(Boolean);
if (source.length !== 1) throw new Error("expected one application script");
class Element {
  constructor(tagName = "INPUT", marked = false) {
    this.tagName = tagName;
    this.dataset = { orderEntryNext: "next-field" };
    this.marked = marked;
  }
  matches(selector) { return selector === "[data-order-entry-enter-next]" && this.marked; }
}
const sandbox = {
  axios: { defaults: {}, interceptors: { response: { use() {} } } },
  Vue: { createApp(definition) { sandbox.definition = definition; return { component() { return this; }, mount() { return this; } }; } },
  HTMLElement: Element,
  window: {}, document: {}, localStorage: { getItem() { return ""; }, setItem() {}, removeItem() {} },
  TMOrderReference: { component: {} }, URLSearchParams, setTimeout, clearTimeout, console,
};
vm.createContext(sandbox);
vm.runInContext(source[0], sandbox);
const handler = sandbox.definition.methods.onOrderEntryKeydown;
function assert(value, message) { if (!value) throw new Error(message); }
function event(overrides = {}) {
  return {
    key: "Enter", keyCode: 13, isComposing: false, ctrlKey: false, metaKey: false, altKey: false,
    target: new Element(), prevented: false, stopped: false,
    preventDefault() { this.prevented = true; }, stopPropagation() { this.stopped = true; },
    ...overrides,
  };
}
function context() {
  return {
    modal: { type: "order" }, orderCreateSaveState: { saving: false, committed: false },
    orderReminderBlocking: () => false, closeCount: 0, saveCount: 0, focused: "",
    closeModal() { this.closeCount += 1; }, saveModal() { this.saveCount += 1; },
    focusOrderEntryField(name) { this.focused = name; return true; },
  };
}
let ctx = context(); let e = event({ key: "Escape" }); handler.call(ctx, e);
assert(ctx.closeCount === 1 && ctx.saveCount === 0 && e.prevented && e.stopped, "Escape must cancel without saving");
ctx = context(); e = event({ isComposing: true, ctrlKey: true }); handler.call(ctx, e);
assert(ctx.saveCount === 0 && !e.prevented, "IME composition must not submit");
ctx = context(); e = event({ target: new Element("TEXTAREA", true) }); handler.call(ctx, e);
assert(ctx.saveCount === 0 && !e.prevented, "textarea Enter must retain newline behavior");
ctx = context(); e = event({ ctrlKey: true }); handler.call(ctx, e);
assert(ctx.saveCount === 1 && e.prevented && e.stopped, "Ctrl+Enter must use the existing save path");
ctx = context(); e = event({ target: new Element("INPUT", true) }); handler.call(ctx, e);
assert(ctx.focused === "next-field" && e.prevented && e.stopped, "declared Enter field must move focus");
ctx = context(); e = event({ target: new Element("INPUT", false) }); handler.call(ctx, e);
assert(ctx.focused === "" && !e.prevented, "unmarked controls must keep their native Enter behavior");
""",
        encoding="utf-8",
    )
    result = subprocess.run(
        [node, str(harness), str(ROOT / "static" / "index.html")],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr
