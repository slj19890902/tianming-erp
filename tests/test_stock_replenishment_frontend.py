from pathlib import Path


INDEX = Path("static/index.html").read_text(encoding="utf-8")


def test_requisition_page_exposes_stock_replenishment_entry_and_warning_count() -> None:
    assert "手动库存补库" in INDEX
    assert "stockPolicyWarnings.length" in INDEX
    assert "addStockPolicyDraft(policy)" in INDEX


def test_legacy_historical_purchase_workbench_is_not_exposed() -> None:
    assert "库存补库 / 历史采购" not in INDEX
    assert "搜索 2020.1-2026" not in INDEX
    assert 'axios.get("/api/requisition/historical-purchases/search"' not in INDEX
    assert "addHistoricalStockLine(row)" not in INDEX
    assert "历史报料事实仍在原单据中只读保留" in INDEX


def test_replenishment_form_always_saves_a_draft_before_stocking() -> None:
    assert "保存后立即进入仓库可用库存" not in INDEX
    assert "当前只是库存补库报料草稿" in INDEX
    assert "到料后进入客户专用纸板备料" in INDEX
    assert "本次报料张数（可多报）" in INDEX
    assert "stockWarningExtraSheets(line)" in INDEX
    assert 'source_type:"customer_request"' in INDEX
    assert "stock_now:false" in INDEX
    assert "stock_now:false," in INDEX
    assert "stockReplenishmentForm.stock_now = false" in INDEX
    assert "stockReplenishmentForm.idempotency_key || createIdempotencyKey()" in INDEX
    assert "stockLocationsForType(line.target_inventory_type)" in INDEX
    assert "location_id:this.defaultStockLocation" in INDEX
    assert 'v-model="line.target_inventory_type" disabled' in INDEX
    assert '<option value="finished">成品</option>' not in INDEX


def test_replenishment_save_and_print_paths_are_wired() -> None:
    assert 'axios.post("/api/requisition/stock-replenishment/orders"' in INDEX
    assert "/api/requisition/stock-replenishment/orders/${data.id}/print" in INDEX
    assert "openStockReplenishmentPrint(printable)" in INDEX
    assert "补库单已保存，库存批次已生成" not in INDEX
    assert "补库报料草稿已保存；到料确认后进入客户专用纸板备料" in INDEX


def test_stock_policy_can_be_saved_from_a_replenishment_line() -> None:
    assert "saveStockPolicyFromLine(line)" in INDEX
    assert 'axios.post("/api/requisition/stock-policies"' in INDEX
    assert "库存低于或等于多少时预警" in INDEX
    assert "触发后建议补到多少" in INDEX


def test_manual_replenishment_uses_common_box_and_material_supplier() -> None:
    assert "请选择客户和常用箱，系统使用当前已维护资料" in INDEX
    assert "供应商（随材质联动）" in INDEX
    assert "applyStockProduct(line)" in INDEX
    assert "applyStockMaterial(line)" in INDEX
    assert "请先选择客户和常用箱" in INDEX


def test_saved_replenishment_is_reopenable_from_reported_history() -> None:
    assert 'row.source_type === "stock_replenishment"' in INDEX
    assert 'return "库存补库单"' in INDEX
    assert 'stockReportedReplenishment(row)' in INDEX
    assert "this.openStockReplenishmentPrint(data)" in INDEX


def test_replenishment_print_reuses_supplier_purchase_order_sheet() -> None:
    assert "(modal.type === 'supplierOrderPrint' || modal.type === 'stockReplenishmentPrint')" in INDEX
    assert "stockReplenishmentPrintData(data)" in INDEX
    assert 'title: `供应商报料单 ${data.order_number}`' in INDEX
    assert "纸板长宽（mm）" in INDEX
    assert "压线(mm）" in INDEX
    assert "材质/楞型" in INDEX
    assert "报料备注" in INDEX
    assert "printStockReplenishmentArea" not in INDEX
    assert "stock-replenishment-print-area" not in INDEX
    assert "<div>库存补库报料单</div>" not in INDEX


def test_replenishment_links_common_box_material_and_crease_fields() -> None:
    assert "输入存货编码、产品名称或规格" in INDEX
    assert 'v-model="line.material_id"' in INDEX
    assert "applyStockMaterial(line)" in INDEX
    assert "product.report_length_mm" in INDEX
    assert "product.crease_left_mm" in INDEX
    assert "上摇盖" in INDEX
    assert "下摇盖" in INDEX
    assert "line.crease_type==='压线'" in INDEX
    assert "压线三段合计" in INDEX
    assert "stockProductSelectOptions(line.customer_id)" in INDEX
    assert "输入存货编码、产品名称或规格" in INDEX
    assert "searchStockProducts(line.customer_id,$event)" in INDEX
    assert "limit:keyword?100:2000" in INDEX
    assert "applyStockProduct(line)" in INDEX
    assert "async addHistoricalStockLine(row)" not in INDEX


def test_replenishment_crease_edit_syncs_width_and_manual_width_mismatch_blocks_save() -> None:
    assert '@input="syncStockReportWidthFromCrease(line)"' in INDEX
    assert "line.report_width_mm = segments.reduce" in INDEX
    assert "stockCreaseMismatch(line)" in INDEX
    assert "stockReplenishmentCreaseBlocked" in INDEX
    assert "两者必须相等" in INDEX


def test_manual_replenishment_requires_operator_quantity() -> None:
    assert "新增补库行" in INDEX
    assert "quantity:null" in INDEX
    assert 'placeholder="请人工填写"' in INDEX
    assert "stockReplenishmentSaveBlocked" in INDEX
    assert "source_record_matches" not in INDEX
    assert "历史 {{ row.history_count }} 次" not in INDEX


def test_replenishment_supplier_follows_material_and_layout_is_responsive() -> None:
    assert "material_supplier_name" in INDEX
    assert "syncStockReplenishmentSupplier()" in INDEX
    assert "stockReplenishmentMaterialSuppliers()" in INDEX
    assert "供应商（随材质联动）" in INDEX
    assert "stock-replenishment-line-card" in INDEX
    assert "stock-replenishment-line-grid" in INDEX
    assert "removeStockReplenishmentLine(index)" in INDEX
