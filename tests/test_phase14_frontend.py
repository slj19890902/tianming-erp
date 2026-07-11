from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
INCOMING = (ROOT / "static" / "incoming.html").read_text(encoding="utf-8")
GO_LIVE_DOCS = [
    ROOT / "docs" / "go_live_checklists" / name
    for name in (
        "PASSWORD_AND_ACCOUNT_SECURITY.md",
        "RBAC_PERMISSION_CHECK.md",
        "BACKUP_AND_RESTORE_GUIDE.md",
        "MANUAL_ACCEPTANCE_CHECKLIST.md",
        "DAILY_OPERATION_GUIDE.md",
        "GO_LIVE_READINESS_SUMMARY.md",
    )
]


def _orders_section() -> str:
    return INDEX.split("activePage === 'orders'", 1)[1].split(
        "activePage === 'orders_legacy'", 1
    )[0]


def test_product_editor_has_no_slash_based_field_splitting() -> None:
    assert "cleanProductName" not in INDEX
    assert 'split(" / "' not in INDEX
    assert 'v-model.trim="productForm.customer_material_code"' in INDEX
    assert 'v-model.trim="productForm.product_name"' in INDEX


def test_products_use_customer_first_master_detail_view() -> None:
    assert "selectedProductCustomer" in INDEX
    assert "productCustomerSearch" in INDEX
    assert "selectProductCustomer" in INDEX
    assert "customer-product-master-list" in INDEX


def test_order_page_hides_old_system_name_and_uses_tm_search_copy() -> None:
    orders = _orders_section()
    assert "RUIDA" not in orders
    assert "ruida" not in orders
    assert "瑞达" not in orders
    assert "filters.orderKeyword" in orders
    assert "openOrderDetail" in orders
    assert "modal.type === 'orderDetail'" in INDEX
    assert "系统单号" in orders
    assert "明细单号" in orders


def test_order_page_uses_customer_po_group_as_default() -> None:
    assert 'orderView:"customer_po"' in INDEX
    assert "groupedOrderRows" in INDEX
    assert "customer_po: (row) => row.customer_po || `NO_PO_${row.id}`" in INDEX
    assert "const normalizedKey = this.filters.orderView === \"customer_po\" && row.customer_po" in INDEX


def test_order_page_uses_redesigned_group_table_columns() -> None:
    orders = _orders_section()
    assert 'class="order-group-table"' in orders
    assert 'class="col-customer-name"' in orders
    assert 'class="col-customer-po"' in orders
    assert 'class="cell-right"' in orders
    assert 'class="col-actions"' in orders


def test_order_page_hides_main_order_number_in_default_list_and_moves_actions_to_end() -> None:
    orders = _orders_section()
    table_head = orders.split("<thead>", 1)[1].split("</thead>", 1)[0]
    assert "<th>系统单号</th>" not in table_head
    assert "<th>主系统单号</th>" not in table_head
    assert 'class="col-customer-po"' in orders
    assert 'class="col-actions"' in orders
    assert "openOrderEditor(group)" in orders


def test_order_page_expand_card_shows_item_and_main_order_numbers() -> None:
    # v0.19.2-B Hotfix: 主系统单号后台仍生成但不显示在展开明细中（§二.1）；
    # 明细系统单号保留显示。
    orders = _orders_section()
    assert 'class="order-group-detail-card"' in orders
    assert "item.item_order_number" in orders
    # row.order_number 仍存在（用于其它地方如 displayOrderNumber）
    assert "row.order_number" in INDEX
    assert "<th>明细系统单号</th>" in orders
    # 主系统单号列已从展开明细表头移除
    assert "<th>主系统单号</th>" not in orders


def test_new_order_and_edit_order_forms_include_customer_po() -> None:
    assert 'modal.type === "order"' in INDEX or "modal.type === 'order'" in INDEX
    assert 'v-model.trim="orderForm.customer_po"' in INDEX
    assert "openOrderEditor(group)" in INDEX
    assert 'modal.type === "orderEdit"' in INDEX or "modal.type === 'orderEdit'" in INDEX
    assert 'v-model.trim="orderEditForm.customer_po"' in INDEX


def test_new_order_modal_requires_customer_first_and_shows_number_preview() -> None:
    assert ":disabled=\"!orderForm.customer_id\"" in INDEX
    assert "normalizeOrderSaveError" in INDEX
    assert "orderNumberPreview" in INDEX
    assert "itemOrderNumberPreview" in INDEX


def test_open_order_sets_modal_before_item_initialization_to_avoid_null_modal_error() -> None:
    snippet = INDEX.split("openOrder() {", 1)[1].split("},", 1)[0]
    assert 'this.modal = { type:"order"' in snippet
    assert snippet.index('this.modal = { type:"order"') < snippet.index("this.addOrderItem();")
    assert "this.modal?.type === \"order\"" in INDEX


def test_new_order_modal_uses_strict_frontend_validation_for_product_and_price() -> None:
    assert "validateOrderForm()" in INDEX
    assert "if (!item.product_id)" in INDEX
    assert "请先选择产品" in INDEX
    assert "请填写单价" in INDEX


def test_requisition_modal_has_frontend_dimension_validation_and_friendly_error_copy() -> None:
    assert "validateRequisitionForm()" in INDEX
    assert "纸板长" in INDEX
    assert "纸板宽" in INDEX
    assert "请输入纸板长" in INDEX
    assert "请输入纸板宽" in INDEX


def test_requisition_submitted_list_offers_incoming_entry_and_delivery_page_explains_received_requirement() -> None:
    assert "/incoming.html" in INDEX
    assert "去入库" in INDEX
    assert "只有已入库，或已被成品库存全额预占" in INDEX


def test_order_pages_use_display_material_instead_of_raw_snapshot_material() -> None:
    # v0.19.2-B：订单材质改为「材质代码/实际楞型」(A6D/A)，统一经 orderItemMaterialText，
    # 仍以 display_material 为基础、绝不直接渲染原始 snapshot_material。
    orders = _orders_section()
    assert "displayMaterialText" in INDEX
    assert "display_material" in INDEX
    assert "orderItemMaterialText(item)" in orders
    assert "orderDetail.items" in INDEX


def test_products_page_defaults_to_25_rows() -> None:
    assert "pageSize: 25" in INDEX


def test_go_live_docs_hide_old_system_name_for_daily_usage() -> None:
    forbidden = ("RUIDA", "ruida", "Ruida", "瑞达")
    for path in GO_LIVE_DOCS:
        text = path.read_text(encoding="utf-8")
        for token in forbidden:
            assert token not in text, f"{path.name} still contains {token}"


def test_order_modal_backdrop_click_uses_guard_handler_instead_of_direct_close() -> None:
    assert '@click.self="onModalMaskClick"' in INDEX
    assert "onModalMaskClick()" in INDEX
    assert 'this.modal?.type === "order"' in INDEX


def test_new_order_form_has_dedicated_code_width_classes() -> None:
    assert "order-template-cell" in INDEX
    assert "order-code-input" in INDEX
    assert 'v-model.trim="item.product_code"' in INDEX


def test_requisition_page_keeps_pending_and_submitted_views() -> None:
    assert "待报料 {{ requisitionPending.length }}" in INDEX
    assert "已报料/已入库 {{ requisitionItems.length }}" in INDEX
    assert '@click="openSupplierRequisitionDraft()"' in INDEX


def test_finance_settlement_no_longer_prompts_for_account() -> None:
    assert 'prompt("请输入收款账户")' not in INDEX
    assert 'axios.put(`/api/finance/statements/${row.id}/settle`, { amount, settlement_date:today() })' in INDEX


def test_delivery_and_statement_controls_show_undo_and_edit_cancel_actions() -> None:
    assert "取消回单" in INDEX
    assert "/api/finance/return_receipts/${this.receiptForm.id}/cancel" in INDEX
    assert "statementCustomerOptions" in INDEX
    assert "/api/finance/statement-customers" in INDEX
    assert "/api/finance/statements/${row.id}/cancel" in INDEX
    assert "/api/finance/statements/${row.id}" in INDEX
    assert "编辑" in INDEX
    assert "取消" in INDEX


def test_board_dimensions_render_as_integer_mm_in_daily_pages() -> None:
    assert "formatBoardDimension" in INDEX
    assert "formatBoardSpec" in INDEX
    assert "formatBoardSpec(row.cardboard_len,row.cardboard_width)" in INDEX
    assert "mm" in INDEX
    assert "formatBoardSpec(item.cardboard_len, item.cardboard_width)" in INCOMING


def test_order_page_has_pdf_recognition_entry_and_draft_modal() -> None:
    orders = _orders_section()
    assert "识别PDF订单" in orders
    assert "openOrderPdfImport" in INDEX
    assert "modal.type === 'orderPdfImport'" in INDEX
    assert "上传客户采购订单 PDF" in INDEX
    assert "识别预览草稿" in INDEX
    assert "带入新建订单" in INDEX
def test_pdf_preview_action_reports_errors_instead_of_failing_silently() -> None:
    snippet = INDEX.split("async previewOrderPdfImport() {", 1)[1].split("},", 1)[0]
    assert "try {" in snippet
    assert "catch (error)" in snippet
    assert "this.showToast(this.errorMessage(error), true);" in snippet


def test_order_import_uses_independent_batch_drafts_and_preserves_customer_change() -> None:
    assert "orderImportBatch" in INDEX
    assert "orderImportDrafts" in INDEX
    assert "multiple" in INDEX
    assert "/api/orders/pdf-preview-batch" in INDEX
    assert "/api/orders/draft-rematch" in INDEX
    snippet = INDEX.split("async handleOrderCustomerChange() {", 1)[1].split(
        "async refreshOrderNumberPreview", 1
    )[0]
    assert "this.orderForm.items = []" not in snippet


def test_order_forms_show_cost_reference_and_closure_actions() -> None:
    assert "预估成本" in INDEX
    assert "预估毛利" in INDEX
    assert "/api/orders/cost-preview" in INDEX
    assert "已结档" in INDEX
    assert "死单" in INDEX
    assert "删除订单" in INDEX
    assert "已接档" not in INDEX
    assert 'orderStatus:"business"' in INDEX


def test_order_badge_uses_undelivered_count_and_has_workflow_rollback() -> None:
    assert "ordersUnfinishedTotal" in INDEX
    assert 'count: this.ordersUnfinishedTotal' in INDEX
    assert "撤回到未报料" in INDEX
    assert "/rollback-workflow" in INDEX
    assert "手机扫码入口" in INDEX
    assert "/api/incoming/mobile-entry" in INDEX


def test_finance_settlement_error_uses_friendly_validation_message() -> None:
    snippet = INDEX.split("async settle(row) {", 1)[1].split("},", 1)[0]
    assert "normalizeValidationErrors" in snippet
    assert "settlement_date" in snippet
    assert "account" in snippet
