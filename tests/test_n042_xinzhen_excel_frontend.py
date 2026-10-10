from pathlib import Path


INDEX = Path(__file__).resolve().parents[1] / "static" / "index.html"


def _source() -> str:
    return INDEX.read_text(encoding="utf-8")


def _between(source: str, start: str, end: str) -> str:
    start_index = source.index(start)
    end_index = source.index(end, start_index)
    return source[start_index:end_index]


def test_n042_adds_a_single_file_new_zhen_excel_entry_and_preview_route() -> None:
    source = _source()

    assert '导入新振Excel' in source
    assert '@click="openXinzhenExcelImport"' in source
    assert 'modal.type === \'orderXinzhenExcelImport\'' in source
    assert 'accept=".xls,.xlsx,application/vnd.ms-excel,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"' in source
    assert 'form.append("file", file, file.name)' in source
    assert 'axios.post("/api/orders/xinzhen-excel-preview", form)' in source
    assert 'modal.type === \'orderPdfImport\'' in source


def test_n042_preview_is_editable_and_transfers_only_to_standard_order_form() -> None:
    source = _source()
    modal = _between(
        source,
        '<div v-else-if="modal.type === \'orderXinzhenExcelImport\'">',
        '<div v-else-if="modal.type === \'order\'">',
    )
    methods = _between(source, "openXinzhenExcelImport()", "openOrderPdfImport()")

    for marker in (
        "来源文件",
        "客户单号",
        "Excel 收件方",
        "Excel 发件方",
        "ERP 客户",
        "纸箱尺寸",
        "原始款式 / 颜色 / 尺寸",
        "常用箱默认价",
        "价格来源：人工确认",
        "下单日期",
        "交货日期",
        "数量核对",
        "我已确认本行价格和日期",
        "选择现有常用箱",
    ):
        assert marker in modal

    assert "确认并转入新建订单" in source
    assert '@click="transferXinzhenExcelToOrderForm"' in source
    assert ':disabled="loading || !xinzhenExcelTransferReady(xinzhenExcelPreviewDraft)"' in source
    assert "const nextOrderForm = {" in methods
    assert "this.orderForm = nextOrderForm" in methods
    assert 'axios.post("/api/orders/xinzhen-excel-confirm"' in methods
    assert 'this.modal = { type:"order", title:"新建多明细订单（新振 Excel）" }' in methods
    assert "createIdempotencyKey()" in methods
    assert "newOrderInventoryState()" in methods
    assert "loadOrderLineInventory(item, customerId)" in methods
    assert 'axios.post("/api/orders",' not in methods


def test_n042_has_no_pdf_route_or_auto_product_creation_in_its_flow() -> None:
    source = _source()
    modal = _between(
        source,
        '<div v-else-if="modal.type === \'orderXinzhenExcelImport\'">',
        '<div v-else-if="modal.type === \'order\'">',
    )
    methods = _between(source, "openXinzhenExcelImport()", "openOrderPdfImport()")
    flow = modal + methods

    assert "pdf-preview" not in flow
    assert "orderPdfImport" not in flow
    assert "is_new_product" not in flow
    assert "新产品" not in flow
    assert 'axios.post("/api/master/products"' not in flow
    assert 'axios.post(`/api/master/products/' not in flow
    assert "role-no-costs" in source
    assert "cost-sensitive" not in modal


def test_n042_transfer_gate_requires_customer_existing_product_quantity_price_dates_and_confirmation() -> None:
    source = _source()
    methods = _between(source, "xinzhenExcelBlockReasons(draft)", "openOrderPdfImport()")

    for marker in (
        "未匹配到 ERP 客户",
        "未选择现有常用箱",
        "数量必须是大于0的整数",
        "客户单价必须填写为不小于0的数字",
        "下单日期未填写",
        "交货日期未填写",
        "尚未确认人工填写的价格和日期",
        "xinzhenExcelTransferReady(draft)",
    ):
        assert marker in methods


def test_n042_quantity_gate_checks_every_excel_line() -> None:
    source = _source()
    methods = _between(source, "xinzhenExcelQuantityChecks(draft)", "xinzhenExcelCandidateLabel(candidate)")

    assert "aggregate?.items" in methods
    assert "item?.quantity_check" in methods
    assert "checks.every(check => this.xinzhenExcelSingleQuantityReconciled(check))" in methods
    assert 'aggregate.status !== "passed"' in methods
    assert "存在未通过箱数核对的明细行" in methods


def test_n042_quantity_edits_invalidate_source_check_and_require_reconfirmation() -> None:
    source = _source()
    methods = _between(source, "openXinzhenExcelImport()", "openOrderPdfImport()")

    for marker in (
        "source_quantity",
        "source_quantity_check",
        "quantity_check_valid",
        "quantity_change_confirmed",
        "onXinzhenExcelQuantityInput(item)",
        "item.quantity_check_valid = !changed",
        "item.quantity_change_confirmed = false",
        "xinzhen_quantity_change_confirmed",
        "xinzhen_source_quantity",
        "this.invalidateXinzhenStandardOrderConfirmation()",
        "重新确认当前内容",
    ):
        assert marker in source or marker in methods


def test_n042_product_candidate_switch_ignores_stale_responses() -> None:
    source = _source()
    methods = _between(source, "openXinzhenExcelImport()", "openOrderPdfImport()")

    assert "_product_request_sequence" in methods
    assert "requestSequence" in methods
    assert "selectedProductId" in methods
    assert "item._product_request_sequence !== requestSequence" in methods
    assert "Number(item.matched_product_id) !== selectedProductId" in methods
