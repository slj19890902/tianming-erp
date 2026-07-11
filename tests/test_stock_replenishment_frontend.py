from pathlib import Path


INDEX = Path("static/index.html").read_text(encoding="utf-8")


def test_requisition_page_exposes_stock_replenishment_entry_and_warning_count() -> None:
    assert "库存补库 / 历史采购" in INDEX
    assert "stockPolicyWarnings.length" in INDEX
    assert "addStockPolicyDraft(policy)" in INDEX


def test_historical_search_uses_new_read_only_source_endpoint() -> None:
    assert 'axios.get("/api/requisition/historical-purchases/search"' in INDEX
    assert "搜索 2020.1-2026" in INDEX
    assert "款号、产品名称或规格" in INDEX
    assert "addHistoricalStockLine(row)" in INDEX


def test_replenishment_form_keeps_stock_now_explicit_and_location_visible() -> None:
    assert "保存后立即进入仓库可用库存" in INDEX
    assert "如果纸板尚未实际到仓，请取消勾选" in INDEX
    assert "stockLocationsForType(line.target_inventory_type)" in INDEX
    assert "location_id:this.defaultStockLocation" in INDEX


def test_replenishment_save_and_print_paths_are_wired() -> None:
    assert 'axios.post("/api/requisition/stock-replenishment/orders"' in INDEX
    assert "/api/requisition/stock-replenishment/orders/${data.id}/print" in INDEX
    assert "stock-replenishment-print-area" in INDEX
    assert "补库单已保存，库存批次已生成" in INDEX


def test_stock_policy_can_be_saved_from_a_replenishment_line() -> None:
    assert "saveStockPolicyFromLine(line)" in INDEX
    assert 'axios.post("/api/requisition/stock-policies"' in INDEX
    assert "库存低于或等于多少时预警" in INDEX
    assert "触发后建议补到多少" in INDEX


def test_historical_supplier_aliases_are_normalized_for_the_select() -> None:
    assert '"佳丰":"苏州佳丰"' in INDEX
    assert '"鸣朋":"昆山鸣朋"' in INDEX
    assert '"嘉林亿":"苏州嘉林亿"' in INDEX
    assert "normalizeStockSupplierName(row.supplier_name)" in INDEX


def test_saved_replenishment_is_reopenable_from_reported_history() -> None:
    assert 'row.source_type === "stock_replenishment"' in INDEX
    assert 'return "库存补库单"' in INDEX
    assert 'stockReportedReplenishment(row)' in INDEX
    assert 'this.modal={type:"stockReplenishmentPrint"' in INDEX


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
    assert "async addHistoricalStockLine(row)" in INDEX
    assert "product_id:row.product_id || null" in INDEX
    assert "await this.loadStockProducts(line.customer_id)" in INDEX


def test_replenishment_crease_edit_syncs_width_and_manual_width_mismatch_blocks_save() -> None:
    assert '@input="syncStockReportWidthFromCrease(line)"' in INDEX
    assert "line.report_width_mm = segments.reduce" in INDEX
    assert "stockCreaseMismatch(line)" in INDEX
    assert "stockReplenishmentCreaseBlocked" in INDEX
    assert "两者必须相等" in INDEX


def test_replenishment_history_keeps_source_dimensions_and_requires_manual_quantity() -> None:
    assert "applyStockProduct(line,{preserveExisting:true})" in INDEX
    assert "quantity:null" in INDEX
    assert 'placeholder="请人工填写"' in INDEX
    assert "stockReplenishmentSaveBlocked" in INDEX
    assert "source_record_matches" in INDEX
    assert "历史 {{ row.history_count }} 次" in INDEX


def test_replenishment_supplier_follows_material_and_layout_is_responsive() -> None:
    assert "material_supplier_name" in INDEX
    assert "syncStockReplenishmentSupplier()" in INDEX
    assert "stockReplenishmentMaterialSuppliers()" in INDEX
    assert "供应商（随材质联动）" in INDEX
    assert "stock-replenishment-line-card" in INDEX
    assert "stock-replenishment-line-grid" in INDEX
    assert "removeStockReplenishmentLine(index)" in INDEX
