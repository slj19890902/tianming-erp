from pathlib import Path


INDEX = Path("static/index.html").read_text(encoding="utf-8")


def _pdf_block() -> str:
    start = INDEX.index("<!-- PDF 草稿默认只保留现场核对必需信息")
    end = INDEX.index('<div class="pdf-savebar">', start)
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
    assert '<th class="pdf-sequence-col">序号</th>' in block
    assert '<th class="pdf-code-col">存货编码</th>' in block
    assert "存货编码" in block
    assert '<th class="pdf-quantity-col">数量</th>' in block
    assert "库存 / 需报" in block
    assert "异常" in block
    assert 'v-if="draft._show_advanced_details &&' in block
    candidate_start = block.index('class="order-item-sub-row import-product-candidate-row"')
    candidate = block[candidate_start : block.index("</tr>", candidate_start)]
    assert 'v-if="draft._show_advanced_details' in block[block.rfind("<tr", 0, candidate_start) : candidate_start]
    assert "匹配候选（只选择常用箱，不会改写 PDF 存货编码）" in candidate
    assert "匹配证据：最高分" in candidate
    assert "pdfDraftColumnCount(draft)" in candidate
    header = block[block.index("<thead>") : block.index("</thead>")]
    row_start = block.index("<!-- 主行 -->")
    row = block[row_start : block.index("</tr>", row_start)]
    advanced_start = block.index(
        '<tr v-if="draft._show_advanced_details && draft._detail_line===(item.client_line_id||item.line_no)" class="order-item-sub-row">'
    )
    advanced = block[advanced_start : block.index("</tr>", advanced_start)]
    assert "客户单价" not in header
    assert "图纸" not in header
    assert "客户单价" not in row
    assert "图纸" not in row
    assert advanced.index("客户单价") < advanced.index("图纸")
    assert "pdfCommonBoxProductName(item)" in row
    assert "pdfOriginalMaterialText(item)" not in row
    assert "PDF：{{ pdfOriginalMaterialText(item) }}" in advanced


def test_pdf_material_is_evidence_only_and_never_blocks_the_draft() -> None:
    assert "pdfMaterialDifference(item)" not in INDEX
    assert "PDF材质与常用箱不同，请核对。" not in INDEX
    assert "usePdfActualMaterialForOrder" not in INDEX
    assert "_material_difference_acknowledged" not in INDEX
    reason_start = INDEX.index("importDraftBlockReasons(draft) {")
    reasons = INDEX[reason_start : INDEX.index("return reasons;", reason_start)]
    assert "PDF材质与常用箱不同" not in reasons
    comparison = INDEX[INDEX.index("refreshPdfMaterialComparison(item, product)") : INDEX.index("_findMaterial(id)")]
    assert "material_evidence_only:true" in comparison
    assert "material_differs" not in comparison
    assert "size_differs" not in _pdf_block()
    normalized = INDEX[INDEX.index("normalizedMaterialCode(value)") : INDEX.index("refreshPdfMaterialComparison(item, product)")]
    assert "split(/[/-]/, 1)" in normalized


def test_new_order_refreshes_readiness_from_product_detail() -> None:
    select_start = INDEX.index("async selectOrderProduct(index, productId)")
    select_end = INDEX.index("async selectOrderMaterial", select_start)
    select = INDEX[select_start:select_end]
    assert "item._common_box_readiness = this.commonBoxReadiness(data);" in select
    order_start = INDEX.index("<!-- P1-02：固定一行一款")
    order_end = INDEX.index('<div v-else-if="modal.type === \'orderEdit\'">', order_start)
    order = INDEX[order_start:order_end]
    assert "资料已完善" in order
    assert "来自常用箱" in order
