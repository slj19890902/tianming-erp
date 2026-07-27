from pathlib import Path


INDEX = Path("static/index.html").read_text(encoding="utf-8")


def _pdf_block() -> str:
    start = INDEX.index("<!-- PDF 草稿默认只保留现场核对必需信息")
    end = INDEX.index("识别结果只进入独立草稿层", start)
    return INDEX[start:end]


def test_common_box_readiness_replaces_manual_edit_as_business_status() -> None:
    assert "commonBoxReadiness(source)" in INDEX
    assert "commonBoxReadinessTitle(source)" in INDEX
    assert "资料已完善" in INDEX
    assert "commonBoxReadiness(item).ready ? '资料已完善' : '待完善'" in INDEX
    assert "已人工编辑（版本事实）" in INDEX
    assert "row.manual_modified ? \"已修改\" : \"未修改\"" not in INDEX


def test_pdf_default_is_compact_and_technical_match_details_stay_advanced() -> None:
    block = _pdf_block()
    assert '<th style="min-width:60px">序号</th>' in block
    assert "存货编码" in block
    assert "数量与库存" in block
    assert "异常" in block
    assert 'v-if="draft._show_advanced_details"' in block
    candidate_start = block.index('class="order-item-sub-row import-product-candidate-row"')
    candidate = block[candidate_start : block.index("</tr>", candidate_start)]
    assert 'v-if="draft._show_advanced_details' in block[block.rfind("<tr", 0, candidate_start) : candidate_start]
    assert "匹配候选（只选择常用箱，不会改写 PDF 存货编码）" in candidate
    assert "匹配证据：最高分" in candidate
    assert "pdfDraftColumnCount(draft)" in candidate
    header = block[block.index("<thead>") : block.index("</thead>")]
    row = block[block.index("<!-- 主行 -->") : block.index("<!-- 操作 -->")]
    assert header.index("客户单价") < header.index("图纸") < header.index("异常")
    assert row.index("<!-- 客户单价") < row.index("<!-- 图纸 -->") < row.index("PDF材质与常用箱不同")


def test_pdf_material_difference_requires_a_draft_only_choice_without_size_warning() -> None:
    assert "pdfMaterialDifference(item)" in INDEX
    assert "PDF材质与常用箱不同，请核对。" in INDEX
    assert "仅本订单使用" in INDEX
    assert "更新常用箱" in INDEX
    assert "_material_difference_acknowledged" in INDEX
    reason_start = INDEX.index("importDraftBlockReasons(draft) {")
    reasons = INDEX[reason_start : INDEX.index("return reasons;", reason_start)]
    assert "必须选择实际材质并确认仅本订单使用" in reasons
    helper = INDEX[INDEX.index("usePdfActualMaterialForOrder(draft, item)") : INDEX.index("_findMaterial(id)")]
    for field in ("item.material_id = material.id;", "item.material = material.code", "item.material_supplier_name", "item.layer_count", "item.flute_type"):
        assert field in helper
    assert "syncProductFieldsVersioned" not in helper
    assert "/api/warehouse/" not in helper
    assert "size_differs" not in _pdf_block()
    normalized = INDEX[INDEX.index("normalizedMaterialCode(value)") : INDEX.index("refreshPdfMaterialComparison(item, product)")]
    assert "split(/[/-]/, 1)" in normalized


def test_new_order_refreshes_readiness_from_product_detail() -> None:
    select_start = INDEX.index("async selectOrderProduct(index, productId)")
    select_end = INDEX.index("async selectOrderMaterial", select_start)
    select = INDEX[select_start:select_end]
    assert "item._common_box_readiness = this.commonBoxReadiness(data);" in select
    assert "资料已完善" in INDEX[INDEX.index("<!-- 新建订单明细表"):INDEX.index("<!-- 副行：成本/毛利", INDEX.index("<!-- 新建订单明细表"))]
    assert "来自常用箱" in INDEX[INDEX.index("<!-- 新建订单明细表"):INDEX.index("<!-- 副行：成本/毛利", INDEX.index("<!-- 新建订单明细表"))]
