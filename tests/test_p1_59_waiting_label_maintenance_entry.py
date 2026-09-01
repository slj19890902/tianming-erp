from pathlib import Path


INDEX = Path(__file__).resolve().parents[1] / "static" / "index.html"


def _slice(source: str, start_marker: str, end_marker: str) -> str:
    start = source.index(start_marker)
    return source[start : source.index(end_marker, start)]


def test_production_first_screen_has_only_a_compact_conditional_maintenance_entry():
    source = INDEX.read_text(encoding="utf-8")
    production = _slice(
        source,
        '<template v-else-if="activePage === \'production\'">',
        '<template v-else-if="activePage === \'deliveries\'">',
    )
    load = _slice(
        source,
        "async loadProduction()",
        "async openProductionLabelMaintenance()",
    )
    pending_load = _slice(
        load,
        'if (this.productionTab === "pending") {',
        'if (this.productionTab === "placement")',
    )

    assert "标签异常 / 维护" in production
    assert "productionLabelMaintenanceEntryVisible()" in production
    assert "待料标签维护" not in production
    assert "productionWaitingLabels" not in production
    assert "refreshProductionLabelPlan(row)" not in production
    assert "loadProductionWaitingLabelPage" not in load
    assert "this.loadProductionHistory()" in load
    assert "this.loadProductionPlacement()" in load
    assert "this.loadProductionPage(this.pages.productionPending || 1)" in pending_load
    assert load.count("loadProductionPage(") == 1
    assert 'status:"waiting_material"' not in load


def test_maintenance_is_an_independent_read_only_modal_loaded_only_after_click():
    source = INDEX.read_text(encoding="utf-8")
    modal = _slice(
        source,
        '<div v-else-if="modal.type === \'productionLabelMaintenance\'">',
        '<div v-else-if="modal.type === \'statement\'">',
    )
    opener = _slice(
        source,
        "async openProductionLabelMaintenance()",
        "productionLabelPlanNeedsMaintenance(row)",
    )
    closer = _slice(source, "closeModal() {", "this.statementDetail = null")

    assert "待料任务标签计划" in modal
    assert "productionWaitingLabels" in modal
    assert "productionPendingLabelMaintenanceRows()" in modal
    assert "loadProductionWaitingLabelPage" in opener
    assert "window.scrollY" in opener
    assert 'this.modal?.type === "productionLabelMaintenance"' in closer
    assert "requestAnimationFrame(() => window.scrollTo(0, productionLabelMaintenanceScrollY))" in closer
    assert "actual_input_quantity" not in modal
    assert "completion_mode" not in modal
    assert "batchConfirmProduction" not in modal
    assert "生产数量、材料状态、库存和送货不会因这里的操作改变" in modal


def test_entry_visibility_is_admin_or_a_real_pending_label_anomaly_only():
    source = INDEX.read_text(encoding="utf-8")
    methods = _slice(
        source,
        "productionLabelPlanNeedsMaintenance(row)",
        "async loadProductionWaitingLabelPage(requestedPage = 1)",
    )

    assert "this.canAdmin" in methods
    assert "this.canProductionExecute" in methods
    assert "productionLabelPlanInconsistent(row)" in methods
    assert "productionLabelSnapshot(row)" in methods
    assert "productionPendingLabelMaintenanceRows" in methods
    assert "productionLabelMaintenanceAnomalyCount" in methods


def test_waiting_loader_is_latest_session_scoped_and_logout_clears_low_frequency_data():
    source = INDEX.read_text(encoding="utf-8")
    loader = _slice(
        source,
        "async loadProductionWaitingLabelPage(requestedPage = 1)",
        "async loadProductionPage(requestedPage = 1)",
    )
    reset = _slice(source, "resetPagePerformanceState()", "pageCacheFresh(page)")

    assert "this.authGeneration" in loader
    assert "this.user?.id" in loader
    assert 'this.activePage === "production"' in loader
    assert 'this.modal?.type === "productionLabelMaintenance"' in loader
    assert 'status:"waiting_material"' in loader
    assert "this.productionWaitingLabels = []" in reset
    assert "this.productionWaitingLabelTotal = 0" in reset
    assert "this.productionLabelRefreshAttempts = {}" in reset


def test_refresh_keeps_server_version_idempotency_and_print_fact_contracts():
    source = INDEX.read_text(encoding="utf-8")
    refresh = _slice(
        source,
        "async refreshProductionLabelPlan(row)",
        "async ensureProductionLocations",
    )

    for required in (
        "expected_task_version",
        "expected_product_version",
        "idempotency_key",
        "confirmed_not_started:true",
        "confirmed_no_prior_print:true",
        "/label-plan-refresh",
        'row?.status === "waiting_material"',
    ):
        assert required in refresh
