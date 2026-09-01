from __future__ import annotations

from pathlib import Path


INDEX = (Path(__file__).resolve().parents[1] / "static" / "index.html").read_text(
    encoding="utf-8"
)


def _block(start: str, end: str) -> str:
    begin = INDEX.index(start)
    return INDEX[begin : INDEX.index(end, begin)]


def test_customer_finished_storage_uses_independent_get_and_put_contract() -> None:
    load = _block(
        "async loadCustomerFinishedStorage(",
        "addCustomerFinishedStorageArea() {",
    )
    save = _block(
        "async saveCustomerFinishedStorage() {",
        "openCustomer(row=null) {",
    )
    customer_payload = _block(
        "buildCustomerWritePayload() {",
        "async prepareCustomerChangeConfirmation(",
    )

    endpoint = "/finished-storage-preferences"
    assert endpoint in load
    assert "await axios.get(" in load
    assert endpoint in save
    assert "await axios.put(" in save
    assert "finished_storage" not in customer_payload
    assert "area_ids" not in customer_payload


def test_customer_finished_storage_is_ordered_multi_select() -> None:
    editor = _block(
        "addCustomerFinishedStorageArea() {",
        "async saveCustomerFinishedStorage() {",
    )
    save = _block(
        "async saveCustomerFinishedStorage() {",
        "openCustomer(row=null) {",
    )

    assert "this.customerFinishedStorageState.preferences.push(" in editor
    assert "const [row] = rows.splice(index,1);" in editor
    assert "rows.splice(target,0,row);" in editor
    assert "this.customerFinishedStorageState.preferences.splice(index,1);" in editor
    assert (
        "area_ids:this.customerFinishedStorageState.preferences.map(row=>Number(row.area_id))"
        in save
    )
    assert '@click="moveCustomerFinishedStorageArea(index,-1)"' in INDEX
    assert '@click="moveCustomerFinishedStorageArea(index,1)"' in INDEX


def test_customer_finished_storage_updates_all_customer_version_copies() -> None:
    save = _block(
        "async saveCustomerFinishedStorage() {",
        "openCustomer(row=null) {",
    )

    assert "expected_version:expectedVersion" in save
    assert "const nextVersion = Number(data.customer_version || expectedVersion);" in save
    assert "customerVersion:nextVersion" in save
    assert "this.customerForm.version = nextVersion;" in save
    assert "this.masterEditBaseline.customer.version = nextVersion;" in save
    assert "if (customer) customer.version = nextVersion;" in save


def test_customer_finished_storage_requires_saved_clean_customer_basics() -> None:
    save = _block(
        "async saveCustomerFinishedStorage() {",
        "openCustomer(row=null) {",
    )

    assert "if (!customerId || !this.canEditCustomers) return;" in save
    assert 'if (this.masterLocalChanges("customer").length)' in save
    assert "请先保存客户基本资料，再保存成品区域" in save
    assert "先保存客户资料，再设置成品区域。" in INDEX


def test_production_locations_filter_by_area_id_in_preference_order() -> None:
    routing = _block(
        "productionPreferredAreas(row) {",
        "productionLocationFloorsForRow(row) {",
    )

    assert "preferred_finished_storage_area_ids" in routing
    assert "new Map(preferredIds.map((areaId,index) => [areaId,index]))" in routing
    assert ".filter(location => rank.has(Number(location.area_id)))" in routing
    assert (
        ".sort((a,b) => (rank.get(Number(a.area_id)) ?? 999) - "
        "(rank.get(Number(b.area_id)) ?? 999))"
        in routing
    )
    assert "if (!preferredIds.length) return this.productionAvailableLocations;" in routing


def test_preferred_stock_has_no_fallback_and_auto_selects_first_free_location() -> None:
    stock = _block(
        "async ensureProductionMode(row) {",
        "async onProductionLocationSelection(row) {",
    )

    assert "const allowedLocations = this.productionLocationsForRow(row);" in stock
    assert (
        "allowedLocations.find(location => "
        "!this.productionLocationUsedByOther(location.id,row))"
        in stock
    )
    assert "row.location_floor_number = first ?" in stock
    assert "row.location_area_code = first ?" in stock
    assert "row.location_id = first ? Number(first.id) : null;" in stock
    assert "if (row.location_id) this.autoSelectProductionRow(row);" in stock
    assert "指定区域暂无空位；不会改放其他区域" in INDEX
    assert "所选区域都无空位时会停止并提示，不会改放一楼。" in INDEX


def test_direct_completion_copy_is_generic_and_routing_has_no_customer_hardcode() -> None:
    routing = _block(
        "productionPreferredAreas(row) {",
        "onProductionFloorChange(row, floorField, areaField, locationField) {",
    )
    mode = _block(
        "async ensureProductionMode(row) {",
        "async onProductionLocationSelection(row) {",
    )
    direct = _block(
        "async confirmProductionDirectRow(row) {",
        "autoSelectProductionRow(row) {",
    )

    assert 'if (row.completion_mode !== "direct") return;' in mode
    assert "await this.confirmProductionDirectRow(row);" in mode
    assert "系统选择的真实成品位置" in INDEX
    assert "整批已进入系统选择的真实成品位置" in direct
    assert "一楼待送区" not in direct

    for source in (routing, mode, direct):
        assert "新振" not in source
        assert "customer_name" not in source
        assert "customer_id" not in source
