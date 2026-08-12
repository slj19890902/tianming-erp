from pathlib import Path


INDEX = (
    Path(__file__).resolve().parents[1] / "static" / "index.html"
).read_text(encoding="utf-8")


def test_frontend_loads_authoritative_box_type_rules_and_preview() -> None:
    assert 'axios.get("/api/products/box-type-rules")' in INDEX
    assert 'axios.post("/api/products/box-type-recommendation", payload)' in INDEX
    assert "productBoxTypeRule(boxType)" in INDEX
    assert "?.supports_splice === true" in INDEX
    assert "?.uses_flap === true" in INDEX
    assert "?.supports_cutting_mode === true" in INDEX
    assert "rule.required_dimensions" in INDEX
    assert "rule.supported_crease_types" in INDEX


def test_frontend_does_not_keep_a_second_box_formula_engine() -> None:
    assert "calculateBoardSizeByBoxType" not in INDEX
    assert "calculateCreaseByBoxType" not in INDEX
    assert "2 * (L + W) + flap" not in INDEX
    assert "2 * W + H" not in INDEX
    assert "Math.round(W / 2)" not in INDEX
    assert "围套推荐" not in INDEX
    assert "全搭盖箱推荐" not in INDEX


def test_box_type_names_and_legacy_aliases_are_safe() -> None:
    options = INDEX.split("productBoxStyleOptions: [", 1)[1].split("],", 1)[0]
    for display_name in (
        "A1/0201 普通开槽箱",
        "A3 天地盖",
        "独立天盖",
        "独立底",
        "围板",
        "满摇盖纸箱",
        "半开槽箱",
        "衬板",
        "模切内盒",
    ):
        assert f'"{display_name}"' in options
    assert '"围套"' not in options
    assert '"全搭盖箱"' not in options
    assert '"围套": "围板"' in INDEX
    assert '"全搭盖箱": "满摇盖纸箱"' in INDEX
    assert "rule.display_name" in INDEX
    assert "...(rule.aliases || [])" in INDEX


def test_product_fields_follow_rule_capabilities() -> None:
    product_form = INDEX.split("modal.type === 'product'", 1)[1].split(
        "modal.type === 'customerScope'", 1
    )[0]
    assert "productRequiresDimension(productForm.box_style,'height_mm')" in product_form
    assert "productSupportedSpliceModes(productForm.box_style)" in product_form
    assert "usesProductTongue(productForm.box_style)" in product_form
    assert "productSupportedCreaseTypes(productForm.box_style)" in product_form
    assert "usesProductDefaultCuttingMode(productForm.box_style)" in product_form
    assert "cuttingModeFactor(productForm.default_cutting_mode)" in product_form
    assert "normalizeCuttingMode($event.target.value)" in product_form
    assert "productSupportsCreaseSegments(productForm.box_style)" in product_form


def test_product_size_row_layout_follows_each_box_type_capability() -> None:
    assert (
        'class="product-form-row product-size-report-row" '
        ':class="productSizeReportRowClass(productForm.box_style)"'
    ) in INDEX
    assert "productSizeReportRowClass(productForm.box_style)" in INDEX
    method = INDEX.split("productSizeReportRowClass(boxType) {", 1)[1].split(
        "productRequiresDimension(boxType, dimension)", 1
    )[0]
    for layout_class in (
        "product-size-report-row--standard",
        "product-size-report-row--telescoping",
        "product-size-report-row--plain-3d-with-crease",
        "product-size-report-row--plain-3d",
        "product-size-report-row--flat-2d",
        "product-size-report-row--no-dimensions",
    ):
        assert layout_class in method
        assert f".{layout_class}" in INDEX
    assert "rule.required_dimensions.length" in method
    assert "this.productSupportsCreaseSegments(boxType)" in method


def test_manual_dimensions_are_only_forced_by_recommend_button() -> None:
    assert "const applyMain = force || !form._report_dims_manual;" in INDEX
    assert "const applyCrease = force || !form._crease_dims_manual;" in INDEX
    assert "const applyBase = force || !form._base_report_dims_manual;" in INDEX
    assert "this.autoApplyProductRecommendations({ force: true });" in INDEX
    reapply = INDEX.split("reapplyProductRecommendations() {", 1)[1].split(
        "// ===== 材质显示构建", 1
    )[0]
    assert "_report_dims_manual = false" not in reapply
    assert "已保留当前手工值" in INDEX
    assert "sequence !== this.productRecommendationSequence" in INDEX


def test_unknown_and_manual_formula_types_fail_closed() -> None:
    assert "当前箱型未识别，请手工填写报料尺寸。" in INDEX
    assert "当前箱型需人工填写报料尺寸。" in INDEX
    assert "recommendation?.auto_calculated" in INDEX
    assert "form.report_length_mm =" not in INDEX.split(
        "async autoApplyProductRecommendations", 1
    )[1].split("applyBoxTypeRecommendation", 1)[0]


def test_order_item_editor_reuses_box_type_capabilities_and_explicit_recommendation() -> None:
    order_item = INDEX.split("modal.type === 'orderItem'", 1)[1].split(
        "modal.type === 'stock'", 1
    )[0]
    assert '@change="onOrderItemBoxStyleChange"' in order_item
    assert 'v-if="usesProductSplice(orderItemForm.box_style)"' in order_item
    assert 'v-if="usesProductTongue(orderItemForm.box_style)"' in order_item
    assert 'v-if="usesProductDefaultCuttingMode(orderItemForm.box_style)"' in order_item
    assert 'cuttingModeFactor(orderItemForm.special_process)' in order_item
    assert 'normalizeCuttingMode($event.target.value)' in order_item
    assert 'productSupportedCreaseTypes(orderItemForm.box_style)' in order_item
    assert 'v-if="productSupportsCreaseSegments(orderItemForm.box_style)"' in order_item
    assert '@click="reapplyOrderItemRecommendations"' in order_item
    assert "async reapplyOrderItemRecommendations()" in INDEX
    assert "await this.requestBoxTypeRecommendation({" in INDEX
    assert "手工报料长宽已保留" in INDEX


def test_order_item_box_change_only_normalizes_unsupported_fields() -> None:
    method = INDEX.split("onOrderItemBoxStyleChange()", 1)[1].split(
        "async reapplyOrderItemRecommendations()", 1
    )[0]
    assert "if (!this.usesProductSplice(form.box_style))" in method
    assert "if (!this.usesProductTongue(form.box_style)) form.snapshot_flap_mm = null;" in method
    assert "if (!this.usesProductDefaultCuttingMode(form.box_style)) form.special_process = \"一开一\";" in method
    assert "form.snapshot_report_length_mm =" not in method
    assert "form.snapshot_report_width_mm =" not in method
    assert "form.sync_product = true;" in method
    assert 'product_change_reason = "订单编辑修改箱型并同步常用箱"' not in method
    assert "productRequiresDimension(orderItemForm.box_style,'height_mm')" in INDEX


def test_composite_a3_draft_uses_physical_source_identity() -> None:
    assert "bomSourceSelectionKey(component)" in INDEX
    assert '`component:${snapshotId}:${component?.component_type || "whole"}`' in INDEX
    assert "component_type:component.component_type || null" in INDEX
    assert "bom_snapshot_id:Number(component.snapshot_id)" in INDEX
    assert "${line.bom_snapshot_id || 'parent'}-${line.component_type || 'whole'}" in INDEX
    assert "row.suppress_parent_requisition === true" in INDEX
    assert "if (parent?.can_requisition && !suppressParent)" in INDEX
