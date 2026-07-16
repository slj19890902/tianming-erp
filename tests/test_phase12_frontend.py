from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
INCOMING = (ROOT / "static" / "incoming.html").read_text(encoding="utf-8")
SPA_PAGE_PATHS = (
    "/dashboard",
    "/customers",
    "/quotations",
    "/products",
    "/orders",
    "/orders_legacy",
    "/requisition",
    "/incoming",
    "/deliveries",
    "/finance",
    "/system",
)


def test_phase12_customer_and_product_uat_controls_are_present() -> None:
    assert "显示已停用客户" in INDEX
    assert "delivery_method" in INDEX
    assert "信用额度</label>" not in INDEX
    assert "customer-group-title" in INDEX
    assert "图纸版本历史" in INDEX
    # v0.19.2 下一轮：产品材质选择器统一为「材质供应商 + 可搜索材质下拉」，
    # 标签由「材质（克重）」改为「材质（代码｜供应商｜克重｜报价）」。
    assert "材质供应商" in INDEX
    assert "材质（代码｜供应商｜克重｜报价）" in INDEX


def test_product_inactive_switch_and_toggle_are_available() -> None:
    assert "showInactiveProducts" in INDEX
    assert "toggleProductStatus" in INDEX
    assert "显示已停用纸箱" in INDEX


def test_inactive_customer_has_reenable_action() -> None:
    assert "toggleCustomerStatus" in INDEX
    assert "/api/master/customers/${row.id}/status" in INDEX
    assert 'row.is_active ? "停用" : "启用"' in INDEX


def test_phase12_bulk_selection_and_exports_are_present() -> None:
    assert "toggleAllRequisition" in INDEX
    assert "toggleAllDelivery" in INDEX
    assert "toggleAllStatement" in INDEX
    assert "导出 Excel" in INDEX
    assert "/export" in INDEX


def test_supplier_schedule_ui_is_removed_and_wms_is_direct() -> None:
    assert "登记排单" not in INDEX
    assert "供应商预计到达" not in INCOMING
    assert "确认实收" in INCOMING


def test_modal_does_not_use_internal_scroll_container() -> None:
    assert "width: min(1400px, 98vw); overflow: visible;" in INDEX
    assert "max-height: 92vh; overflow: auto;" not in INDEX


def test_requisition_print_page_is_registered() -> None:
    from app.main import app

    assert any(
        route.path == "/requisition-print.html" for route in app.routes
    )


@pytest.mark.parametrize("page_path", SPA_PAGE_PATHS)
def test_desktop_spa_deep_links_return_an_erp_page(page_path: str) -> None:
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        response = client.get(page_path)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "ERP" in response.text
    assert "Not Found" not in response.text


def test_requisition_spa_route_returns_index_page_after_refresh() -> None:
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        response = client.get("/requisition")

    assert response.status_code == 200
    assert "天明包装ERP" in response.text
    assert "智能报料工作台" in response.text
    assert "Not Found" not in response.text


def test_desktop_spa_preserves_deep_link_and_defaults_root_to_dashboard() -> None:
    assert "initialPageFromLocation" in INDEX
    assert 'window.location.pathname.replace(/^\\/+|\\/+$/g, "")' in INDEX
    assert 'return pages.has(pathPage) ? pathPage : "dashboard"' in INDEX
    assert "if (!this.pageAllowed(this.activePage)) this.activePage = this.firstAllowedPage()" in INDEX
    assert "await this.loadPage(this.activePage)" in INDEX


def test_root_address_still_returns_the_dashboard_shell() -> None:
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert "天明包装ERP" in response.text


def test_incoming_pending_cards_show_cardboard_requisition_size() -> None:
    assert "报料尺寸" in INCOMING
    assert "item.cardboard_len" in INCOMING
    assert "item.cardboard_width" in INCOMING
    # v0.22.1 阶段 1A Task B：压线尺寸需与报料尺寸同样在卡片主视觉显示
    assert "压线尺寸" in INCOMING
    assert "item.snapshot_crease_left_mm" in INCOMING
    assert "item.snapshot_crease_middle_mm" in INCOMING
    assert "item.snapshot_crease_right_mm" in INCOMING


def test_incoming_mobile_cards_show_crease_in_primary_view() -> None:
    assert "formatCreaseDisplay(item)" in INCOMING
    assert "压线：" in INCOMING


def test_product_drawing_upload_and_mobile_page_support_pdf() -> None:
    assert "application/pdf,.pdf" in INDEX
    assert "查看图纸(PDF)" in INDEX
    assert "isPdfDrawing" in INDEX
    assert "打开图纸" in INCOMING
    assert "item.drawing_is_pdf" in INCOMING
    assert "item.drawing_path" in INCOMING
    assert 'id="drawingViewer"' in INCOMING
    assert "返回来料入库" in INCOMING


def test_new_order_status_displays_as_pending_material_until_requisitioned() -> None:
    # v0.23.0 P0-1：新建订单在明细尚未报料前，后端状态仍保持
    # pending_production（状态机与筛选逻辑不变），但列表/详情展示需要
    # 显示为"待报料"，等至少一条明细报料后再恢复显示"待生产"。
    assert "orderDisplayStatusKey" in INDEX
    assert 'pending_material:"待报料"' in INDEX
    assert "group_status: this.orderDisplayStatusKey(row)" in INDEX
    assert ':value="group.group_status"' in INDEX
    assert ':value="orderDisplayStatusKey(orderDetail)"' in INDEX


def test_order_list_n026_search_sort_finished_view_and_detail_columns() -> None:
    assert "toggleOrderSort('customer_name')" in INDEX
    assert "toggleOrderSort('order_date')" in INDEX
    assert "toggleOrderSort('delivery_date')" in INDEX
    assert "params.sort_by = this.filters.orderSortBy" in INDEX
    assert "params.sort_direction = this.filters.orderSortDirection" in INDEX
    assert "params.keyword = this.filters.orderKeyword" in INDEX
    assert "params.customer_name = this.filters.orderKeyword" not in INDEX
    assert '{ label: "已送完", value: "finished_delivery" }' in INDEX
    assert ':style="customerRowStyle(group.customer_id)"' in INDEX

    delivery_search_start = INDEX.index("async searchDeliveryLine(line)")
    delivery_search_end = INDEX.index(
        "selectDeliveryCandidateById(line, orderItemId)", delivery_search_start
    )
    delivery_search_block = INDEX[delivery_search_start:delivery_search_end]
    assert "q: keyword" in delivery_search_block
    assert "inventory_code: keyword" not in delivery_search_block

    style_start = INDEX.index("customerRowStyle(customerId)")
    style_end = INDEX.index("showToast(message", style_start)
    customer_style_block = INDEX[style_start:style_end]
    assert "137.508" in customer_style_block
    assert "hsl(${hue} 58% 94%)" in customer_style_block
    assert "Math.random" not in customer_style_block

    detail_start = INDEX.index('<div class="order-group-detail-card">')
    detail_end = INDEX.index("</table>", detail_start)
    detail_block = INDEX[detail_start:detail_end]
    ordered_fields = [
        "item.item_order_number",
        "item.snapshot_product_code",
        "item.snapshot_product_name",
        "item.snapshot_spec",
        "orderItemMaterialText(item)",
        "item.delivered_quantity",
        "item.unit_price",
        "item.subtotal",
        "item.total_estimated_cost",
        "itemDeliveryStatusKey(item)",
        "item.completion_date",
        "openOrderDetail(row)",
    ]
    positions = [detail_block.index(field) for field in ordered_fields]
    assert positions == sorted(positions)
    assert "item.remaining_quantity" in detail_block


def test_order_search_highlights_visible_text_without_html_injection() -> None:
    assert ".order-search-highlight" in INDEX
    assert "orderSearchHighlightParts(value)" in INDEX

    highlight_start = INDEX.index("orderSearchHighlightParts(value)")
    highlight_end = INDEX.index("toggleOrderSort(field)", highlight_start)
    highlight_block = INDEX[highlight_start:highlight_end]
    assert "text.toLowerCase()" in highlight_block
    assert "keyword.toLowerCase()" in highlight_block
    assert "text.slice(matchIndex, matchIndex + keyword.length)" in highlight_block
    assert "innerHTML" not in highlight_block
    assert "v-html" not in highlight_block

    order_page_start = INDEX.index("activePage === 'orders'")
    order_page_end = INDEX.index("activePage === 'orders_legacy'", order_page_start)
    order_page_block = INDEX[order_page_start:order_page_end]
    assert "orderSearchHighlightParts(group.customer_name || '-')" in order_page_block
    assert "orderSearchHighlightParts(group.customer_po || '-')" in order_page_block
    assert "orderSearchHighlightParts(item.item_order_number || '-')" in order_page_block
    assert "orderSearchHighlightParts(plainProductText(item.snapshot_product_code))" in order_page_block
    assert "orderSearchHighlightParts(plainProductText(item.snapshot_product_name))" in order_page_block
    assert "orderSearchHighlightParts(item.snapshot_spec || '-')" in order_page_block
    assert "{{ part.text }}" in order_page_block


def test_delivery_batch_picker_tools_are_left_aligned() -> None:
    assert ".delivery-batch-picker-toolbar," in INDEX
    assert ".delivery-batch-picker-tools { justify-content: flex-start; }" in INDEX
    assert 'class="toolbar delivery-batch-picker-toolbar"' in INDEX
    assert 'class="toolbar-group delivery-batch-picker-tools"' in INDEX


def test_incoming_mobile_login_return_and_cache_protection_are_present() -> None:
    assert '/?redirect=/incoming.html' in INCOMING
    assert "loginRedirectPath" in INDEX
    assert "redirectAfterLogin" in INDEX
    assert '["/incoming.html", "/warehouse.html"].includes(target)' in INDEX
    assert 'cache: "no-store"' in INCOMING
    assert "errorRetryButton" in INCOMING
    assert "网络异常不能误报为未登录" in INCOMING


def test_incoming_page_response_disables_browser_cache() -> None:
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        response = client.get("/incoming.html")

    assert response.status_code == 200
    assert response.headers["cache-control"] == (
        "no-store, no-cache, must-revalidate, max-age=0"
    )
    assert response.headers["pragma"] == "no-cache"
    assert response.headers["expires"] == "0"


def test_desktop_and_mobile_incoming_layout_support_editable_quantity() -> None:
    assert 'activePage === \'incoming\'' in INDEX
    assert "仓库来料入库" in INDEX
    assert "incomingPending" in INDEX
    assert "received_quantity:Number(row.incoming_quantity)" in INDEX
    assert "resolution_action" in INDEX
    assert "继续等待供应商补货" in INDEX
    assert "超出部分转半成品库存" in INDEX
    assert "item.customer_name" in INCOMING
    assert "item.product_code" in INCOMING
    assert "item.product_name" in INCOMING
    assert "报料尺寸" in INCOMING
    assert "压线尺寸" in INCOMING
    assert 'data-quantity="${key}"' in INCOMING
    assert "data-resolution" in INCOMING
    assert "pending_receipt_item_id" in INCOMING
    assert "item.requisition_date" in INCOMING
    assert '/api/incoming/surplus-locations' in INCOMING
    assert 'api("/api/warehouse/locations")' not in INCOMING
    assert 'this.hasPermission("incoming.execute")' in INDEX
    assert 'axios.get("/api/incoming/surplus-locations")' in INDEX


def test_delivery_variance_ui_warns_and_requires_explicit_resolution_without_reason() -> None:
    assert "超送原因（必填）" not in INDEX
    assert "确认继续保存超送单吗" not in INDEX
    assert "请核对数量" in INDEX
    assert "差异备注（可选）" in INDEX
    assert "保留剩余数量，继续待送" in INDEX
    assert 'value="accept_short"' in INDEX
    assert 'value="accept_over"' in INDEX
    assert "validateReceiptForm" in INDEX
    assert "编辑回单" in INDEX
    assert '@click="openReceipt(row)"' in INDEX
    assert "this.pages.deliveries = 1;" in INDEX
    assert "短收结单必须填写原因" not in INDEX
    assert "短收结单必须填写原因" not in INCOMING


def test_system_version_panel_groups_major_releases_and_hides_legacy_tools() -> None:
    assert "versionMajorGroups" in INDEX
    assert "toggleVersionGroup(group.key)" in INDEX
    assert "group.visibleEntries" in INDEX
    assert "还有 {{ group.overflow }} 条，详见更新记录。" in INDEX
    assert "<template v-if=\"false\">" in INDEX
    assert "材质映射审批" in INDEX
    assert "楞型批量识别" in INDEX


def test_pdf_training_uses_safe_sample_ids_in_frontend() -> None:
    assert "normalizePdfSampleId(value)" in INDEX
    assert "Number.parseInt(raw, 10)" in INDEX
    assert "样本编号无效，请刷新样本列表后重试" in INDEX
    assert "id: Number(row.id)" in INDEX


def test_pdf_training_detail_uses_form_based_ground_truth_editor() -> None:
    assert "人工标注表单" in INDEX
    assert "从解析结果生成草稿" in INDEX
    assert "新增明细行" in INDEX
    assert "高级：Ground Truth JSON 预览" in INDEX
    assert "pdfGroundTruthForm" in INDEX
    assert "fillPdfGroundTruthFromParsedResult" in INDEX
    assert "addPdfGroundTruthItem" in INDEX
    assert "removePdfGroundTruthItem" in INDEX
    assert "buildPdfGroundTruthJsonFromForm" in INDEX
    assert "loadPdfGroundTruthFormFromSample" in INDEX


def test_pdf_training_normalizes_sample_ids_for_detail_save_and_score() -> None:
    assert "normalizePdfSampleId(value)" in INDEX
    assert 'value.id ?? value.sample_id' in INDEX
    assert 'this.normalizePdfSampleId(sampleId)' in INDEX
    assert 'this.normalizePdfSampleId(this.pdfSampleDetail)' in INDEX
    assert "样本ID无效，请从样本列表重新打开详情。" in INDEX


def test_pdf_training_sample_list_uses_safe_page_number() -> None:
    assert '@click="loadPdfTrainingSamples(1)"' in INDEX
    assert "const normalizedPage = Number.parseInt(page, 10);" in INDEX
    assert "const safePage = Number.isInteger(normalizedPage) && normalizedPage > 0 ? normalizedPage : 1;" in INDEX
    assert "const offset = (safePage - 1) * 50;" in INDEX


def test_pdf_training_detail_shows_template_rule_warnings() -> None:
    assert "模板规则 / 解析警告" in INDEX
    assert "pdfParsedResult.warnings && pdfParsedResult.warnings.length" in INDEX


def test_pdf_training_detail_supports_single_sample_reparse() -> None:
    assert "重新解析当前样本" in INDEX
    assert "async reparsePdfSample()" in INDEX
    assert "/api/pdf-training/samples/${safeSampleId}/reparse" in INDEX


def test_order_pdf_preview_explains_simair_merge_and_candidate_evidence() -> None:
    assert "source_text_quality === 'garbled_text_layer'" in INDEX
    assert "思迈尔变体参考号" in INDEX
    assert "p.specification || '-'" in INDEX
    assert "p.sale_unit_price || '-'" in INDEX


def test_quotation_conversion_uses_visible_report_and_crease_form() -> None:
    assert "modal.type === 'quotationConvert'" in INDEX
    assert "正式存货编码" in INDEX
    assert "单片报料长宽" in INDEX
    assert "盖报料长宽" in INDEX
    assert "底报料长宽" in INDEX
    assert "syncQuotationConvertReportWidthFromCrease" in INDEX
    assert "quotationConvertCreaseMismatch" in INDEX
    assert "applyQuotationConvertRecommendations({force:true})" in INDEX
    assert "用户手填值不会被自动覆盖" in INDEX


def test_quotation_conversion_no_longer_uses_prompt_chain() -> None:
    start = INDEX.index("convertQuotationItem(quotation,item)")
    end = INDEX.index("async openProduct(row=null)", start)
    conversion_block = INDEX[start:end]

    assert "prompt(" not in conversion_block
    assert "quotationConvertPayload()" in conversion_block
    assert "report_width_mm = values.reduce" not in conversion_block
    assert "form[`${prefix}report_width_mm`] = values.reduce" in conversion_block


def test_order_item_crease_edit_syncs_width_without_blocking_untouched_legacy_data() -> None:
    assert '@input="syncOrderItemReportWidthFromCrease"' in INDEX
    assert "syncOrderItemReportWidthFromCrease()" in INDEX
    assert "form.snapshot_report_width_mm = values.reduce" in INDEX
    assert "_report_crease_touched:false" in INDEX
    assert "!this.orderItemForm._report_crease_touched" in INDEX
