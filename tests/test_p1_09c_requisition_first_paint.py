from pathlib import Path


INDEX = Path("static/index.html").read_text(encoding="utf-8")


def _method_block(name: str, next_name: str) -> str:
    start = INDEX.index(name)
    end = INDEX.index(next_name, start)
    return INDEX[start:end]


def test_requisition_cold_entry_requests_only_pending_business_data() -> None:
    load_page = _method_block("async loadPage(page", "refreshCurrent()")
    requisition_load = _method_block(
        "async loadRequisition({skipAutoRelease=false}={})",
        "externalPurchaseRoutingRequestIsCurrent(controller, authGeneration, userId)",
    )

    requisition_branch = load_page.split('if (page === "requisition") {', 1)[1].split(
        'if (page === "incoming")', 1
    )[0]
    assert 'this.requisitionWorkspace === "external-packaging"' in requisition_branch
    assert "this.loadExternalPurchaseRouting()" in requisition_branch
    assert "this.loadRequisition()" in requisition_branch
    assert "loadCustomerOptions(force)" not in load_page.split('if (page === "requisition")')[1].split('if (page === "incoming")')[0]
    assert "loadMaterials()" not in load_page.split('if (page === "requisition")')[1].split('if (page === "incoming")')[0]
    assert 'axios.get("/api/requisition/pending", {' in requisition_load
    assert "params:this.requisitionPendingRequestParams(requestedPage, requestedSupplier)" in requisition_load
    assert "/api/requisition/reported-documents" not in requisition_load
    assert "/api/requisition/merge-suggestions" not in requisition_load
    assert "/api/requisition/stock-policies" not in requisition_load
    assert "/api/external-packaging-purchases/pending-confirmations" not in requisition_load
    assert 'beginLatestRequest("requisition:pending")' in requisition_load


def test_requisition_secondary_tabs_and_tools_load_on_demand() -> None:
    tab_switch = _method_block("async selectRequisitionTab(tab)", "async refreshRequisitionTab()")
    merge_open = _method_block("async openMergeSuggestions()", "async ensureRequisitionMaterials()")
    replenishment_open = _method_block("async openStockReplenishment(options={})", "addBlankStockReplenishmentLine()")

    assert "if (tab === \"submitted\")" in tab_switch
    assert "this.loadReportedDocuments()" in tab_switch
    assert "this.loadReportedCustomerOptions()" in tab_switch
    assert 'beginLatestRequest("requisition:reported")' in INDEX
    assert 'beginLatestRequest("requisition:merge-suggestions")' in merge_open
    assert 'axios.get("/api/requisition/merge-suggestions", {signal:controller.signal})' in merge_open
    assert "mergeSuggestionsError" in merge_open
    assert 'beginLatestRequest("requisition:stock-replenishment-bootstrap")' in replenishment_open
    assert 'axios.get("/api/requisition/stock-policies", {params:{warning_only:true}, signal:controller.signal})' in replenishment_open
    assert '/api/requisition/stock-replenishment/locations' not in replenishment_open
    assert "if (!this.allMaterials.length) tasks.push(this.loadMaterials());" in replenishment_open


def test_requisition_materials_are_deferred_without_hiding_existing_merge_supplier() -> None:
    material_change = _method_block("async openRequisitionMaterialChange(row)", "async loadRequisitionMaterialCandidates(itemId)")

    assert "async ensureRequisitionMaterials()" in INDEX
    assert "await this.ensureRequisitionMaterials();" in material_change
    assert '@focus="ensureRequisitionMaterials()"' in INDEX
    assert "row.supplier_name && !materialSupplierSelectOptions.includes(row.supplier_name)" in INDEX
    assert "（历史值）" in INDEX
    assert 'v-model="group.supplier_name" @focus="ensureRequisitionMaterials()"' in INDEX
    assert "!materialSupplierSelectOptions.includes(group.supplier_name)" in INDEX


def test_requisition_toolbar_refreshes_only_visible_tab() -> None:
    refresh = _method_block("async refreshRequisitionTab()", "resetReportedFilters()")

    assert "if (this.requisitionTab === \"submitted\") return this.loadReportedDocuments();" in refresh
    assert "return this.loadRequisition();" in refresh
    assert "refreshRequisitionTab())" in INDEX
    assert '@click="selectRequisitionTab(\'submitted\')"' in INDEX


def test_requisition_page_hides_order_entry_actions_and_keeps_merge_workflow() -> None:
    report_page = INDEX.split("<template v-else-if=\"activePage === 'requisition'\">", 1)[1].split(
        "<template v-else-if=\"activePage === 'incoming'\">", 1
    )[0]

    assert '@click="openOrder">新建订单</button>' not in report_page
    assert '@click="openOrderPdfImport">识别PDF订单</button>' not in report_page
    assert '@click="openSupplierRequisitionDraft()"' in report_page
    assert 'supplierRequisitionPreviewLoading ? "正在生成草稿…" : "合并报料"' in report_page
    assert '@click="openMergeSuggestions"' not in report_page
