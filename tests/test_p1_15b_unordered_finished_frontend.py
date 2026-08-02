"""Q1-03 前端静态契约：订单待送优先，并可受控补入无订单库存。"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
PRINT = (ROOT / "static" / "delivery-print.html").read_text(encoding="utf-8")


def test_delivery_editor_keeps_sources_explicit_and_allows_controlled_mix() -> None:
    modal = INDEX[
        INDEX.index('<div v-else-if="modal.type === \'delivery\'">') :
        INDEX.index('<div v-else-if="modal.type === \'tianhuaPreimport\'">')
    ]
    assert 'source_mode: "order"' in INDEX
    assert "订单待送（默认优先）" in modal
    assert "无订单成品库存" in modal
    assert "setDeliverySourceMode('unordered_finished')" in modal
    assert "本单混合两类来源" in modal
    assert 'deliveryForm.source_tab === \'order\'' in modal


def test_unordered_finished_picker_is_customer_scoped_and_compact() -> None:
    assert '"/api/deliveries/unordered-finished-candidates"' in INDEX
    assert "q: this.unorderedFinishedPicker.keyword || undefined" in INDEX
    assert "page_size: this.unorderedFinishedPicker.page_size" in INDEX
    assert "requestToken !== this.unorderedFinishedPicker.request_token" in INDEX
    assert "!Boolean(item.is_general)" in INDEX
    assert "owner_customer_id" in INDEX
    assert "grid-template-columns: minmax(0, 2fr)" in INDEX
    assert "<span>产品</span><span>库存批次</span><span>可用数</span><span>本次送货</span>" in INDEX


def test_unordered_finished_payload_requires_price_and_allocations() -> None:
    save_start = INDEX.index("const activeDeliveryLines =")
    save_end = INDEX.index('if (this.deliveryForm.editingId)', save_start)
    save_block = INDEX[save_start:save_end]
    assert 'source_type: "unordered_finished"' in save_block
    assert "product_id: Number(line.product_id)" in save_block
    assert "unit_price: Number(line.unit_price)" in save_block
    assert "inventory_lot_id:Number(allocation.inventory_lot_id)" in save_block
    validation_start = INDEX.index("validateDeliveryForm() {")
    validation_end = INDEX.index("groupDeliveryCandidatesByProduct", validation_start)
    validation = INDEX[validation_start:validation_end]
    assert 'line.source_type === "unordered_finished"' in validation
    assert "无默认单价，请填写大于零的单价" in validation
    assert "库存批次分配数量一致" in validation
    assert "还有订单待送" in validation


def test_both_pickers_keep_cross_page_selection_and_use_server_paging() -> None:
    assert "setDeliveryBatchSelection(item, checked)" in INDEX
    assert "Object.values(this.deliveryBatchPicker.selected || {})" in INDEX
    assert "page_size: this.deliveryBatchPicker.page_size" in INDEX
    assert "requestToken !== this.deliveryBatchPicker.request_token" in INDEX
    assert "unorderedFinishedSelection(item)" in INDEX
    assert "Object.values(this.unorderedFinishedPicker.selected || {})" in INDEX


def test_print_marks_unordered_finished_goods_without_a_fake_order_number() -> None:
    assert "<td>${escapeHtml(item.customer_po)}</td>" in PRINT
    assert "source_type" not in PRINT


def test_unordered_finished_frontend_inline_script_is_valid(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node, "Node.js is required for frontend syntax validation"
    scripts = [script for script in re.findall(r"<script(?:\\s[^>]*)?>(.*?)</script>", INDEX, re.DOTALL) if script.strip()]
    assert len(scripts) == 1
    target = tmp_path / "p1-15b-unordered-finished.js"
    target.write_text(scripts[0], encoding="utf-8")
    result = subprocess.run([node, "--check", str(target)], text=True, encoding="utf-8", capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
