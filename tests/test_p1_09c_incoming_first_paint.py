from pathlib import Path


INDEX = Path("static/index.html").read_text(encoding="utf-8")
INCOMING = Path("static/incoming.html").read_text(encoding="utf-8")


def _block(source: str, start_marker: str, end_marker: str) -> str:
    start = source.index(start_marker)
    end = source.index(end_marker, start)
    return source[start:end]


def test_desktop_incoming_cold_entry_only_requests_pending() -> None:
    load_page = _block(INDEX, "async loadPage(page", "refreshCurrent()")
    load_pending_page = _block(
        INDEX,
        "async loadIncomingPendingPage(",
        "async changeIncomingPendingPage",
    )
    load_incoming = _block(INDEX, "async loadIncoming()", "externalIncomingDraftKey(")

    assert 'if (page === "incoming") {' in load_page
    assert 'if (this.incomingWorkspace === "external-packaging") await requirePageLoad(this.loadExternalIncoming());' in load_page
    assert 'else await requirePageLoad(this.loadIncoming());' in load_page
    assert 'axios.get("/api/incoming/pending"' in load_pending_page
    assert "params:this.incomingPendingRequestParams(requestedPage)" in load_pending_page
    assert "this.loadIncomingPendingPage" in load_incoming
    assert "this.loadExternalIncoming()" not in load_incoming
    assert "/api/external-packaging-purchases/pending-receipts" not in load_incoming
    assert "/api/incoming/received" not in load_incoming
    assert "/api/incoming/surplus-locations" not in load_incoming
    assert 'beginLatestRequest("incoming:pending")' in load_pending_page


def test_desktop_incoming_secondary_data_is_tab_or_action_driven() -> None:
    received = _block(INDEX, "async loadIncomingReceived", "async ensureIncomingLocations")
    locations = _block(INDEX, "async ensureIncomingLocations", "onIncomingResolutionAction")
    tabs = _block(INDEX, "async selectIncomingTab(tab)", "async refreshIncomingTab()")

    assert 'axios.get("/api/incoming/received", {' in received
    assert "params:{page:this.pages.incomingReceived,page_size:this.incomingListPageSize()}" in received
    assert "signal:controller.signal" in received
    assert 'beginLatestRequest("incoming:received")' in received
    assert 'axios.get("/api/incoming/surplus-locations", {signal:controller.signal})' in locations
    assert 'beginLatestRequest("incoming:surplus-locations")' in locations
    assert "this.ensureIncomingLocations();" in INDEX
    assert "if (tab === \"received\") await this.loadIncomingReceived();" in tabs
    assert "if (tab === \"history\")" in tabs
    assert "this.loadCustomerOptions()" in tabs
    assert '@click="refreshIncomingTab"' in INDEX


def test_desktop_incoming_writes_only_refresh_received_after_its_tab_was_loaded() -> None:
    refresh = _block(INDEX, "async refreshIncomingAfterWrite()", "incomingProjectedVariance")

    assert "this.loadIncomingPendingPage" in refresh
    assert "page:this.incomingPendingAppliedPage" in refresh
    assert "this.loadKpi()" in refresh
    assert "this.loadExternalIncoming()" not in refresh
    assert "if (this.incomingReceivedLoaded)" in refresh
    assert "this.loadIncomingReceived({force:true})" in refresh
    for method, next_method in (
        ("async batchReceiveIncoming()", "async receiveIncoming(row)"),
        ("async receiveIncoming(row)", "async acceptShortIncoming(row)"),
        ("async acceptShortIncoming(row)", "async revertIncoming(row)"),
    ):
        block = _block(INDEX, method, next_method)
        assert "await this.refreshIncomingAfterWrite();" in block


def test_standalone_incoming_matches_deferred_loading_contract() -> None:
    pending = _block(
        INCOMING,
        "async function loadPending({page = state.pendingPage || 1} = {})",
        "async function loadReceived",
    )
    received = _block(INCOMING, "async function loadReceived", "async function ensureSurplusLocations")
    locations = _block(INCOMING, "async function ensureSurplusLocations", "async function loadData")

    assert 'endpoint = "/api/incoming/pending"' in pending
    assert 'endpoint = "/api/mobile/erp/incoming/search"' in pending
    assert "/api/incoming/received" not in pending
    assert "/api/incoming/surplus-locations" not in pending
    assert '`/api/incoming/history?${params.toString()}`' in received
    assert "page_size: String(state.receivedPageSize)" in received
    assert 'api("/api/incoming/surplus-locations", {signal: controller.signal})' in locations
    assert "beginLatestRequest" in INCOMING
    assert "await loadReceived({page: state.receivedPage});" in INCOMING
    assert "await ensureSurplusLocations();" in INCOMING


def test_standalone_incoming_writes_refresh_loaded_received_without_cold_request() -> None:
    refresh = _block(INCOMING, "async function refreshAfterIncomingWrite()", "function findItem")
    receive = _block(INCOMING, "async function receive(itemId)", "async function acceptShortNow")
    accept_short = _block(INCOMING, "async function acceptShortNow", "function openRevert")

    assert "const tasks = [loadPending({page: state.pendingPage})];" in refresh
    assert "if (state.receivedLoaded)" in refresh
    assert "loadReceived({page: state.receivedPage, force: true})" in refresh
    assert "await refreshAfterIncomingWrite();" in receive
    assert "await refreshAfterIncomingWrite();" in accept_short
