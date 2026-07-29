import re
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
    "/production",
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
    assert "await this.loadPage(this.activePage, { force:true })" in INDEX
    assert "await this.loadBase()" not in INDEX


def test_n029_production_page_deep_link_and_manual_destination_are_present() -> None:
    assert "activePage === 'production'" in INDEX
    assert 'key: "production", label: "生产确认"' in INDEX
    assert 'production: "orders.view"' in INDEX
    assert '<option value="">请选择完工去向</option>' in INDEX
    assert '<option value="direct">订单内直接待送</option>' in INDEX
    assert '<option value="stock" :disabled="!canWarehouseExecute">合格品全部入库</option>' in INDEX
    assert "productionAvailableLocations" in INDEX
    assert 'v-model="row.location_area_code"' in INDEX
    assert 'v-model="row.transfer_area_code"' in INDEX
    assert "productionLocationsForArea" in INDEX
    assert 'v-model.number="row.location_floor_number"' in INDEX
    assert 'v-model.number="row.transfer_floor_number"' in INDEX
    assert "先选楼层" in INDEX
    assert "再选区域" in INDEX
    assert "再选库位" in INDEX
    assert ':checked="!!productionSelected[row.id]"' in INDEX
    assert 'activePage === \'production\'' in INDEX

    deep_link_start = INDEX.index("initialPageFromLocation()")
    deep_link_end = INDEX.index("redirectAfterLogin()", deep_link_start)
    assert '"production"' in INDEX[deep_link_start:deep_link_end]


def test_n029_production_requests_disable_duplicates_and_reuse_idempotency_keys() -> None:
    start = INDEX.index("async loadProduction()")
    end = INDEX.index("async loadIncoming()", start)
    logic = INDEX[start:end]
    assert 'axios.get("/api/production/tasks", { params: { status: "pending" }, signal:controller.signal })' in logic
    assert 'axios.get("/api/production/completions", { params })' in logic
    assert 'axios.get("/api/production/temporary-locations", {signal:controller.signal})' in logic
    assert 'axios.post("/api/production/completion-batches"' in logic
    assert 'axios.post(`/api/production/completions/${row.id}/stock-transfers`' in logic
    assert "if (this.productionBusy) return" in logic
    assert "this.productionBusy = true" in logic
    assert "finally { this.productionBusy = false; }" in logic
    assert "this.productionCompletionAttempts[customerId]" in logic
    assert "this.productionDirectAttempts[row.id]" in logic
    assert "const idempotencyKey = this.productionDirectAttempts[row.id].idempotency_key" in logic
    assert "this.productionTransferAttempts[row.id]" in logic
    assert "重试将复用同一幂等键" in logic
    assert ':disabled="productionBusy"' in INDEX


def test_n029_production_menu_is_between_incoming_and_warehouse() -> None:
    menu_start = INDEX.index("menus() {")
    menu_end = INDEX.index("];", menu_start)
    menu = INDEX[menu_start:menu_end]
    assert menu.index('key: "incoming"') < menu.index('key: "production"')
    assert menu.index('key: "production"') < menu.index('key: "warehouse"')


def test_n029_production_tables_are_compact_and_do_not_require_horizontal_scroll() -> None:
    start = INDEX.index("activePage === 'production'")
    end = INDEX.index("activePage === 'deliveries'", start)
    page = INDEX[start:end]
    assert 'table-layout:fixed;width:100%' in page
    assert 'min-width:1320px' not in page
    assert 'min-width:1160px' not in page
    for merged_heading in (
        "订单 / 客户",
        "存货编码 / 产品",
        "数量情况",
        "本次生产",
        "订单 / 多出",
        "数量 / 操作人",
        "完工去向",
    ):
        assert merged_heading in page


def test_n029_batch_completion_groups_customers_and_prevents_duplicate_locations() -> None:
    start = INDEX.index("onProductionSelectionChange(row, checked)")
    end = INDEX.index("async loadIncoming()", start)
    logic = INDEX[start:end]
    assert "一次只能确认同一客户" not in logic
    assert "const groups = new Map();" in logic
    assert "for (const [customerId, groupRows] of groups.entries())" in logic
    assert "this.productionCompletionAttempts[customerId]" in logic
    assert "失败项已保留，可直接重试" in logic
    assert "productionLocationUsedByOther(locationId, currentRow)" in logic
    assert "new Set(stockLocationIds).size !== stockLocationIds.length" in logic
    assert "同一批入库明细不能选择同一空库位" in logic
    assert ':disabled="productionLocationUsedByOther(location.id,row)"' in INDEX


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
    assert 'title="查看图纸">图纸</a>' in INDEX
    assert "isPdfDrawing" in INDEX
    assert "打开图纸" in INCOMING
    assert "item.drawing_is_pdf" in INCOMING
    assert "item.drawing_path" in INCOMING
    assert 'id="drawingViewer"' in INCOMING
    assert "返回来料入库" in INCOMING


def test_order_status_display_consumes_backend_business_projection() -> None:
    assert "orderDisplayStatusKey" in INDEX
    assert 'pending_material:"待报料"' in INDEX
    assert 'pending_incoming:"待收料"' in INDEX
    assert 'return row?.business_status || row?.status || "pending_material"' in INDEX
    assert "aggregateOrderBusinessStatus(group.orders)" in INDEX
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
    assert '{ label: "待收料", value: "pending_incoming" }' in INDEX
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
        "item.item_sequence",
        "item.snapshot_product_code",
        "item.snapshot_product_name",
        "item.snapshot_spec",
        "orderItemMaterialText(item, false)",
        "item.quantity",
        "item.business_delivered_quantity",
        "item.unit_price",
        "item.subtotal",
        "item.total_estimated_cost",
        "itemBusinessStatusKey(item)",
        "item.completion_date",
        "openOrderDetail(row)",
    ]
    positions = [detail_block.index(field) for field in ordered_fields]
    assert positions == sorted(positions)
    assert "item.item_order_number" not in detail_block


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
    assert "orderSearchHighlightParts(item.item_order_number || '-')" not in order_page_block
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
    assert '"/api/incoming/surplus-locations"' in INDEX


def test_delivery_variance_ui_separates_return_difference_and_authorized_over_delivery() -> None:
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
    assert "订单待送 / 可用成品 / 可超送" in INDEX
    assert "二次确认超量送货" in INDEX
    assert 'this.hasPermission("deliveries.over_delivery")' in INDEX
    assert "当前账号没有超量送货权限" in INDEX
    assert "请填写超量送货原因" in INDEX


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
    assert "isSimairImportCustomer(draft)" in INDEX
    assert "importProductCandidateLabel(draft, p)" in INDEX


def test_order_pdf_multi_candidate_picker_uses_full_width_subrow() -> None:
    table_start = INDEX.index('<!-- PDF 草稿默认只保留现场核对必需信息')
    table_end = INDEX.index("</table>", table_start)
    table_block = INDEX[table_start:table_end]
    header_start = table_block.index("<thead>")
    header_end = table_block.index("</thead>", header_start)
    column_count = len(
        re.findall(r"<th(?:\s|>)", table_block[header_start:header_end])
    )

    main_row_start = table_block.index("<!-- 主行 -->")
    main_row_end = table_block.index("</tr>", main_row_start) + len("</tr>")
    edit_row_start = table_block.index("<!-- 编辑行 -->", main_row_end)
    main_row = table_block[main_row_start:main_row_end]
    candidate_row = table_block[main_row_end:edit_row_start]

    assert column_count == 10
    assert 'class="order-item-sub-row import-product-candidate-row"' in candidate_row
    assert ':colspan="pdfDraftColumnCount(draft)"' in candidate_row
    assert "匹配候选（只选择常用箱，不会改写 PDF 存货编码）" in candidate_row
    assert 'v-model="item.matched_product_id"' in candidate_row
    assert '@change="selectImportProduct(draft,item)"' in candidate_row
    assert "importProductCandidateLabel(draft, p)" in candidate_row
    assert '@change="selectImportProduct(draft,item)"' not in main_row


def test_order_and_pdf_general_semi_finished_candidates_keep_source_and_confirmation_gate() -> None:
    assert "通用半成品，可跨客户，需人工确认" in INDEX
    assert "general_confirmation" in INDEX
    assert "明确确认并抵扣" in INDEX
    assert 'const recommendationSource = candidate.recommendation_source || candidate.source || "manual"' in INDEX
    assert 'source: recommendationSource === "general_signature" ? "general_signature" : "manual"' in INDEX
    assert 'recommendation_source:recommendationSource' in INDEX
    assert 'warning_acknowledged_codes:warningAcknowledgedCodes' in INDEX
    assert 'confirmed:!general || part.general_confirmation' in INDEX


def test_order_pdf_candidate_label_uses_confirmed_customer_for_simair_reference() -> None:
    customer_check_start = INDEX.index("isSimairImportCustomer(draft)")
    label_start = INDEX.index(
        "importProductCandidateLabel(draft, candidate)", customer_check_start
    )
    customer_check = INDEX[customer_check_start:label_start]
    label_end = INDEX.index("canConfirmImportDraft(draft)", label_start)
    label_block = INDEX[label_start:label_end]

    assert "draft?.matched_customer_id" in customer_check
    assert "Number(draft.matched_customer_id)" in customer_check
    assert "draft.customer_candidates || []" in customer_check
    assert 'includes("思迈尔")' in customer_check
    assert "customer_name_raw" not in customer_check
    assert "customer_type" not in customer_check

    assert '常用箱编码 ${candidate?.product_code || "-"}' in label_block
    assert '名称 ${candidate?.product_name || "-"}' in label_block
    assert '规格 ${candidate?.specification || "-"}' in label_block
    assert "candidate?.sale_unit_price" in label_block
    assert "默认单价 ${defaultPrice}" in label_block
    assert "if (this.isSimairImportCustomer(draft))" in label_block
    assert '思迈尔变体参考号 ${candidate?.customer_material_code || "-"}' in label_block


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
