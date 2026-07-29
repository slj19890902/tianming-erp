from pathlib import Path


INDEX = Path("static/index.html").read_text(encoding="utf-8")


def _block(start_marker: str, end_marker: str) -> str:
    start = INDEX.index(start_marker)
    return INDEX[start:INDEX.index(end_marker, start)]


def test_production_cold_entry_requests_only_pending_tasks() -> None:
    production = _block("async loadProduction()", "async ensureProductionLocations")

    assert 'axios.get("/api/production/tasks", { params: { status: "pending" }, signal:controller.signal })' in production
    assert "/api/production/temporary-locations" not in production
    assert "loadProductionHistory" not in production
    assert 'this.beginLatestRequest("production:pending")' in production


def test_production_locations_are_mode_driven_and_fail_closed() -> None:
    locations = _block("async ensureProductionLocations", "async loadProductionHistory")
    mode = _block("async ensureProductionMode(row)", "async onProductionLocationSelection")

    assert 'axios.get("/api/production/temporary-locations", {signal:controller.signal})' in locations
    assert 'this.beginLatestRequest("production:locations")' in locations
    assert 'this.productionLocationsError = "可用库位读取失败，请重试";' in locations
    assert "const locationsReady = await this.ensureProductionLocations();" in mode
    assert "if (!locationsReady) return;" in mode
    assert 'v-if="productionLocationsLoading"' in INDEX
    assert 'ensureProductionLocations({force:true})' in INDEX


def test_direct_delivery_without_surplus_keeps_fast_path_without_locations() -> None:
    mode = _block("async ensureProductionMode(row)", "async onProductionLocationSelection")

    assert 'if (this.productionNeedsLocation(row)) {' in mode
    assert 'if (row.completion_mode !== "direct") return;' in mode
    assert "await this.confirmProductionDirectRow(row);" in mode


def test_history_transfer_loads_locations_only_when_a_transfer_candidate_exists() -> None:
    history = _block("async loadProductionHistory()", "resetProductionHistoryFilters()")

    assert "row.can_transfer_to_stock" in history
    assert "&& !this.productionLocations.length" in history
    assert "await this.ensureProductionLocations();" in history
    assert "productionLocationsError" in INDEX
    assert "正在读取转库存可用库位" in INDEX
