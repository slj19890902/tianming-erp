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


def test_delivery_modal_names_the_order_picker_and_reports_preload_state() -> None:
    start = INDEX.index('<div v-else-if="modal.type === \'delivery\'">')
    end = INDEX.index('<div v-else-if="modal.type === \'tianhuaPreimport\'">', start)
    modal = INDEX[start:end]

    assert "选择待送订单" in modal
    assert "待送订单正在载入" in modal
    assert "新增一行" not in modal
    assert "新增5行" not in modal
    assert "新增 5 行" not in modal


def test_save_auto_imports_selected_unordered_inventory() -> None:
    primary = _method_body("deliveryPrimaryAction")
    importer = _method_body("importUnorderedFinishedCandidates")

    assert "hasPendingUnorderedFinishedSelections" in primary
    assert "importUnorderedFinishedCandidates({silent:true})" in primary
    assert "return false" in importer
    assert "return true" in importer


def test_saved_modal_print_is_one_click_dispatch_without_second_confirmation() -> None:
    label = _method_body("deliveryPrimaryLabel")
    current_dispatch = _method_body("dispatchCurrentDeliveryDraft")
    dispatch = _method_body("dispatchDelivery")

    assert 'return this.deliveryFormIsDirty() ? "保存修改" : "打印"' in label
    assert "dispatchDelivery(row, {confirmAction:false})" in current_dispatch
    assert "options?.confirmAction !== false" in dispatch


def test_inline_javascript_remains_valid(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node, "Node.js is required for delivery frontend validation"
    scripts = [
        script
        for script in re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", INDEX, re.DOTALL)
        if script.strip()
    ]
    assert len(scripts) == 1
    target = tmp_path / "p1-09c-101-index.js"
    target.write_text(scripts[0], encoding="utf-8")
    result = subprocess.run(
        [node, "--check", str(target)],
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
