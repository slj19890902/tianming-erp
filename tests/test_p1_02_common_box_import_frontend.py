from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def _order_template() -> str:
    start = INDEX.index('<div v-else-if="modal.type === \'order\'">')
    end = INDEX.index('<div v-else-if="modal.type === \'orderEdit\'">', start)
    return INDEX[start:end]


def _picker_template() -> str:
    start = INDEX.index('<div v-if="orderCommonBoxPicker.visible"')
    end = INDEX.index('<div v-if="masterChangeConfirm.visible"', start)
    return INDEX[start:end]


def test_p1_02_order_entry_is_one_row_per_product_with_fixed_business_columns() -> None:
    block = _order_template()
    for heading in (
        "存货编码",
        "产品名称",
        "规格mm",
        "材质楞型",
        "数量",
        "单价",
        "总价",
        "图纸 / 删除",
    ):
        assert f">{heading}<" in block
    assert "order-entry-table-wrap" in block
    assert "order-entry-table" in block
    assert "overflow-x:auto" not in block
    assert "2行/明细" not in block
    assert "orderItemSaleAmount(item)" in block


def test_p1_02_order_entry_hides_production_notes_without_deleting_payload() -> None:
    block = _order_template()
    assert 'v-model.trim="item.production_notes"' not in block
    assert "production_notes: item.production_notes || null" in INDEX


def test_p1_02_picker_is_customer_scoped_and_keeps_cross_filter_selection() -> None:
    block = _picker_template()
    for field in ("product_code", "product_name", "spec"):
        assert f'orderCommonBoxPicker.filters.{field}' in block
    assert "toggleOrderCommonBoxSelection" in block
    assert "切换筛选不会丢失勾选" in block
    assert "selected:{}, selection_sequence:0" in INDEX
    assert "delete this.orderCommonBoxPicker.selected[key]" in INDEX
    assert "this.orderCommonBoxPicker.selected = {};" in INDEX
    assert "customer_id:this.orderForm.customer_id" in INDEX
    assert "page_size:this.orderCommonBoxPicker.page_size" in INDEX


def test_p1_02_import_preserves_blank_or_entered_quantity_and_avoids_duplicates() -> None:
    assert 'selection.quantity === "" ? null : Number(selection.quantity)' in INDEX
    assert "Number(item.product_id) === Number(product.id)" in INDEX
    assert "未重复添加" in INDEX
    assert "this.addOrderItem(false, false);" in INDEX
    select_call = INDEX.index("await this.selectOrderProduct(index, product.id);")
    next_import = INDEX.find("for (const selection of selections)", select_call + 1)
    assert next_import == -1


def test_p1_02_empty_quantity_blocks_save_and_focuses_the_exact_line() -> None:
    assert 'item._validation_error = "请填写数量";' in INDEX
    assert ":id=\"`order-quantity-${index}`\"" in INDEX
    assert "document.getElementById(`order-quantity-${index}`)" in INDEX
    assert "this.focusInvalidOrderLine();" in INDEX
    assert "scrollIntoView" in INDEX
    assert "input?.focus();" in INDEX


def test_p1_02_standard_and_large_order_modal_forbid_horizontal_scroll() -> None:
    for marker in (
        ".order-entry-modal { width: min(1760px, 100%); }",
        "overflow-x: hidden; overflow-y: auto;",
        "width: 100%; min-width: 0; table-layout: fixed;",
        ".order-entry-modal.order-entry-large",
        "'order-entry-large': modal.type === 'order' && isLargeUi",
    ):
        assert marker in INDEX


def test_p1_02_inline_javascript_is_syntactically_valid(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        raise AssertionError("Node.js is required for the frontend contract test")
    scripts = [
        script
        for script in re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", INDEX, re.DOTALL)
        if script.strip()
    ]
    assert len(scripts) == 1
    target = tmp_path / "p1-02-index-inline.js"
    target.write_text(scripts[0], encoding="utf-8")
    result = subprocess.run(
        [node, "--check", str(target)],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr
