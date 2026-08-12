from pathlib import Path


INDEX = Path("static/index.html").read_text(encoding="utf-8")


def _block(start_marker: str, end_marker: str) -> str:
    start = INDEX.index(start_marker)
    return INDEX[start:INDEX.index(end_marker, start)]


def test_product_cold_entry_only_loads_customer_options() -> None:
    load_page = _block("async loadPage(page", "refreshCurrent()")

    assert 'if (page === "products") {' in load_page
    assert "await requirePageLoad(this.loadCustomerOptions(force));" in load_page
    assert 'if (this.productTab === "products" && this.selectedProductCustomer) await requirePageLoad(this.loadProducts());' in load_page
    assert 'if (this.productTab === "materials") await requirePageLoad(this.loadMaterials());' in load_page
    assert "loadMoldTools()" not in load_page


def test_product_list_is_customer_scoped_and_cancels_stale_requests() -> None:
    products = _block("async loadProducts()", "async loadMoldTools()")

    assert "!this.selectedProductCustomer" in products
    assert 'const requestKey = "products:list"' in products
    assert "this.beginLatestRequest(requestKey)" in products
    assert 'response_mode: "summary"' in products
    assert 'axios.get("/api/master/products", { params, signal:controller.signal })' in products
    assert "this.productsLoading = true" in products
    assert "this.productsError = \"\"" in products
    assert "this.finishLatestRequest(requestKey, controller)" in products
    assert "async selectProductCustomer(row)" in INDEX
    assert 'v-if="productsLoading"' in INDEX
    assert 'v-else-if="productsError"' in INDEX


def test_material_list_is_tab_driven_and_cancels_stale_requests() -> None:
    tab = _block("async selectProductTab(tab)", "resetProductFilters()")
    materials = _block("async loadMaterials()", "async openMaterialCandidateMaintenance()")

    assert 'if (tab === "materials") await this.loadMaterials();' in tab
    assert 'const requestKey = "materials:list"' in materials
    assert "this.beginLatestRequest(requestKey)" in materials
    assert 'this.fetchAllMaterials(params, controller.signal)' in materials
    assert "const isUnfilteredCommonView" in materials
    assert "allMaterials = isUnfilteredCommonView" in materials
    assert "this.allMaterials = allMaterials" in materials
    assert "this.finishLatestRequest(requestKey, controller)" in materials


def test_product_material_cell_uses_list_snapshot_before_full_material_cache() -> None:
    cell = _block("          productMaterialCell(row) {", "          materialFullStructure(row) {")
    title = _block("          materialFullStructure(row) {", "          productLayerFluteText(row) {")

    assert "row.material_code" in cell
    assert "row.material_supplier_name" in cell
    assert "row.material_weight" in cell
    assert "row.material_code" in title
    assert "row.material_weight" in title


def test_product_editor_loads_materials_and_molds_only_when_opened() -> None:
    options = _block("async ensureProductEditorOptions({force=false,refreshMolds=false} = {})", "moldToolSelectOptions()")
    open_product = _block("async openProduct(row=null)", "async loadProductMaterialContext")

    assert "const needsMaterials = force || !this.allMaterials.length;" in options
    assert "const needsMolds = !this.isWorkshop && (force || refreshMolds || !this.moldTools.length);" in options
    assert "if (needsMaterials) tasks.push(this.loadMaterials());" in options
    assert "if (needsMolds) tasks.push(this.loadMoldTools());" in options
    assert "productEditorOptionsLoading = true" in options
    assert 'this.productEditorOptionsError = "";' in options
    assert 'this.productEditorOptionsError = "材质、模具与挂板选择数据读取失败，请重试";' in options
    assert "await this.ensureProductEditorOptions({refreshMolds:true});" in open_product
    assert "正在读取材质与模具选择数据" in INDEX
    assert "ensureProductEditorOptions({force:true})" in INDEX
