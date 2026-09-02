from pathlib import Path


INDEX = Path("static/index.html").read_text(encoding="utf-8")


def _body(start: str, end: str) -> str:
    begin = INDEX.index(start)
    return INDEX[begin : INDEX.index(end, begin)]


def test_price_adoption_ui_explains_missing_facts_and_never_hides_rejections() -> None:
    payables = _body(
        '<template v-else-if="financeView===\'payables\'">',
        '<template v-else-if="financeView===\'invoice_tasks\'">',
    )
    assert "检查历史缺价" in payables
    assert "可唯一匹配" in payables
    assert "待人工核对" in payables
    assert "缺少：" in payables
    assert "处理：" in payables
    assert "历史缺价 dry-run 清单" in payables
    assert "网页没有历史采用写入口" in payables
    assert "数据库 SHA" in payables
    assert "采用全部唯一匹配" not in payables
    assert "['admin','boss'].includes(user?.role)" in payables


def test_price_adoption_has_no_browser_write_path() -> None:
    assert "async adoptSupplierPriceFacts()" not in INDEX
    assert (
        'axios.post("/api/finance/supplier-settlements/price-adoptions/adopt"'
        not in INDEX
    )
    assert "backupReference" not in INDEX


def test_price_adoption_preview_is_read_only_and_month_scoped() -> None:
    method = _body(
        "async previewSupplierPriceAdoptions() {",
        "async generateSupplierSettlements(silent = false) {",
    )
    assert 'axios.get("/api/finance/supplier-settlements/price-adoptions/preview"' in method
    assert "settlement_month:this.supplierSettlementMonth" in method
    assert "data.eligible || []" in method
    assert "data.rejected || []" in method
    assert "data.adoption_reason ||" in method
    assert "data.plan_hash ||" in method


def test_new_external_packaging_price_defaults_to_shipping_included() -> None:
    assert 'shipping_fee_mode:"included", shipping_fee:""' in INDEX
