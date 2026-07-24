from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def _method_block(name: str, until: str) -> str:
    start = INDEX.index(name)
    return INDEX[start:INDEX.index(until, start)]


def test_component_priced_parent_auto_expands_to_real_component_lines() -> None:
    block = _method_block("async selectOrderProduct(index, productId)", "async selectOrderMaterial")
    assert 'data.is_composite && data.combination_mode === "component_priced"' in block
    assert "expandComponentPricedOrderLine(index, data, previousLine)" in block
    expand = _method_block("async expandComponentPricedOrderLine", "orderFormItemPayload")
    assert 'axios.get(`/api/master/products/${parent.id}/bom`)' in expand
    assert 'axios.get(`/api/master/products/${component.component_product_id}`)' in expand
    assert "this.orderForm.items.splice(index, 1, ...lines)" in expand
    assert "this.orderForm.items.splice(index, lines.length, previousLine)" in expand
    assert "recalculateOrderCost" in expand
    assert "refreshOrderLineInventory" in expand


def test_component_group_sets_update_and_payload_freezes_provenance() -> None:
    assert "updateCombinationGroupSetQuantity(item, value)" in INDEX
    sets = _method_block("updateCombinationGroupSetQuantity(item, value) {", "removeCombinationGroup(groupKey) {")
    assert "row.quantity = sets * Number(row.combination_quantity_per_set_snapshot || 1)" in sets
    assert "removeCombinationGroup(groupKey)" in INDEX
    payload = _method_block("orderFormItemPayload(item)", "async searchOrderProducts")
    for field in (
        "combination_mode_snapshot: \"component_priced\"",
        "combination_role: \"priced_component\"",
        "combination_group_key",
        "combination_parent_product_id",
        "combination_parent_name_snapshot",
        "combination_set_quantity_snapshot",
        "combination_quantity_per_set_snapshot",
    ):
        assert field in payload
    assert "items: this.orderForm.items.map(item => this.orderFormItemPayload(item))" in INDEX


def test_component_priced_group_ui_and_parent_priced_path_are_distinct() -> None:
    assert "组合：{{ item.combination_parent_name_snapshot }}｜套数" in INDEX
    assert "各组件可单独修改数量和单价" in INDEX
    assert "删除整组" in INDEX
    selection = _method_block("async selectOrderProduct(index, productId)", "async selectOrderMaterial")
    assert 'data.combination_mode === "component_priced"' in selection
    assert "item.is_new_product = false;" in selection
    assert "item.product_id = data.id;" in selection
    assert selection.index('data.combination_mode === "component_priced"') < selection.index("item.product_id = data.id;")


def test_pending_bom_component_can_auto_cover_matching_inventory_without_extra_form() -> None:
    for label in ("需求：", "成品库存：", "半成品库存：", "剩余：", "理论报料："):
        assert label in INDEX
    assert "source.finished_component_reserved_piece_qty || 0" in INDEX
    assert "source.finished_reserved_piece_qty || 0" not in INDEX
    assert "自动使用可匹配库存" in INDEX
    assert "source.can_requisition && Number(source.remaining_required_piece_qty || 0)>0" in INDEX
    block = _method_block("async autoCoverBomComponent(row, source)", "toggleCompositeRowComponents(row, checked) {")
    assert "axios.post(`/api/warehouse/finished/bom-components/${snapshotId}/auto-cover`" in block
    assert "order_item_id: orderItemId" in block
    assert "idempotency_key: createIdempotencyKey()" in block
    assert "await this.loadRequisition()" in block
    assert "confirm(" not in block


def test_inline_javascript_remains_syntactically_valid(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node is not None
    scripts = [script for script in re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", INDEX, re.DOTALL) if script.strip()]
    assert len(scripts) == 1
    target = tmp_path / "component-priced-order.js"
    target.write_text(scripts[0], encoding="utf-8")
    result = subprocess.run([node, "--check", str(target)], capture_output=True, text=True, encoding="utf-8", check=False)
    assert result.returncode == 0, result.stderr
