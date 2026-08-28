from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def test_n034_product_editor_has_compact_internal_bom_cards() -> None:
    assert "bom-editor-panel" in INDEX
    assert "bom-component-card" in INDEX
    assert "添加内部组件" in INDEX
    assert "同客户常用箱" in INDEX
    assert "@search=\"searchBomProducts\"" in INDEX
    assert "每套数量" in INDEX
    assert "交付标签和成品方式" in INDEX
    assert "必需" in INDEX
    assert "内部编码：{{ bomComponentInternalCode(component) }}" in INDEX
    assert ".bom-component-compact-row {" in INDEX
    assert "@media (max-width: 560px)" in INDEX

    bom_start = INDEX.index('<fieldset class="bom-editor-panel"')
    bom_end = INDEX.index("</fieldset>", bom_start) + len("</fieldset>")
    bom_block = INDEX[bom_start:bom_end]
    assert "<table" not in bom_block
    assert "整套统一计价" in bom_block
    assert "组件分别计价" in bom_block
    assert "父件交付" in bom_block
    assert "子件交付" in bom_block


def test_n034_bom_uses_same_customer_products_and_versioned_get_put() -> None:
    assert 'axios.get(`/api/master/products/${productId}/bom`)' in INDEX
    assert 'axios.put(`/api/master/products/${productId}/bom`, this.bomPayload(expectedVersion))' in INDEX
    assert "expected_version: expectedVersion ?? fields.expected_version ?? 1" in INDEX
    assert 'change_reason: "维护父产品内部 BOM"' not in INDEX
    assert "components: fields.components" in INDEX
    assert "const requestedCustomerId = Number(this.productForm.customer_id || 0)" in INDEX
    assert "customer_id: requestedCustomerId" in INDEX
    assert "String(row.id) !== String(requestedProductId)" in INDEX
    for field in (
        "component_product_id",
        "quantity_per_set",
        "is_die_cut",
        "mold_tool_id",
        "mold_max_yield_per_sheet",
        "spare_sheet_quantity",
        "display_mode",
        "show_on_delivery",
        "is_required",
        "remark",
    ):
        assert field in INDEX
    bom_start = INDEX.index('<fieldset class="bom-editor-panel"')
    bom_end = INDEX.index("</fieldset>", bom_start) + len("</fieldset>")
    bom_block = INDEX[bom_start:bom_end]
    # P1-79 将客户单据展示收口为父件级交付模式，不再允许逐组件制造混合口径。
    assert 'v-model="productForm.composite_fulfillment_mode"' in bom_block
    assert 'v-model="component.show_on_delivery"' not in bom_block
    assert 'value="production"' not in bom_block
    assert 'value="requisition"' not in bom_block
    assert 'value="all_internal"' not in bom_block
    assert "component.die_cut_mold" not in INDEX
    assert "component.qty_per_set" not in INDEX
    assert "component.spare_sheets" not in INDEX
    assert "onBomDieCutToggle(component)" in INDEX
    assert "模切组件必须选择生产模具" in INDEX
    assert 'v-model.number="component.spare_sheet_quantity" :disabled' not in bom_block
    toggle_start = INDEX.index("onBomDieCutToggle(component) {")
    toggle_end = INDEX.index("_productBomSaveFields()", toggle_start)
    assert "component.spare_sheet_quantity = 0" not in INDEX[toggle_start:toggle_end]
    assert "非模切组件不能填写模具、最大产出或备用纸张" not in INDEX
    assert "component.display_mode" in INDEX
    assert "component.show_on_delivery" in INDEX
    assert "component.is_required" in INDEX
    assert "validateProductBom" in INDEX
    assert "同一个内部组件不能重复添加" in INDEX


def test_n034_bom_save_does_not_change_sales_order_item_flow() -> None:
    save_start = INDEX.index('if (this.modal.type === "product") {')
    save_end = INDEX.index('if (this.modal.type === "material") {', save_start)
    save_block = INDEX[save_start:save_end]
    assert "saveProductBom(saved.id, saved.version ?? null)" in save_block
    assert "/api/orders" not in save_block
    assert "sales_order_items" not in save_block


def test_n034_order_detail_and_expanded_rows_render_read_only_bom_preview() -> None:
    assert "hasBomComponents(item)" in INDEX
    assert "item.bom_components" in INDEX
    assert "bomPreviewComponentLabel(component)" in INDEX
    assert "内部 BOM 预览（客户订单仍为一个父产品 / 套）" in INDEX
    assert "客户订单仍为一个父产品 / 套" in INDEX
    assert "只读订单详情" in INDEX


def test_n034_inline_javascript_is_syntactically_valid(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node is not None, "Node.js is required for frontend syntax validation"
    scripts = [script for script in re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", INDEX, re.DOTALL) if script.strip()]
    assert len(scripts) == 1
    target = tmp_path / "n034-index-inline.js"
    target.write_text(scripts[0], encoding="utf-8")
    result = subprocess.run(
        [node, "--check", str(target)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr
