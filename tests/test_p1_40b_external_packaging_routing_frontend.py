from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def _method_source(name: str) -> str:
    match = re.search(
        rf"(?m)^\s{{10}}(?:async\s+)?{re.escape(name)}\([^\n]*\)\s*\{{",
        INDEX,
    )
    assert match is not None, f"missing Vue method: {name}"
    next_method = re.search(
        r"(?m)^\s{10}(?:async\s+)?[A-Za-z_$][A-Za-z0-9_$]*\([^\n]*\)\s*\{",
        INDEX[match.end() :],
    )
    assert next_method is not None, f"cannot delimit Vue method: {name}"
    return INDEX[match.start() : match.end() + next_method.start()]


def test_mixed_mode_and_external_routing_are_visible_without_replacing_paper_flow() -> None:
    assert '<option value="mixed_bom">纸板＋外购组件</option>' in INDEX
    assert "外购包材待确认" in INDEX
    assert "仅核对纯外购包材与混合 BOM 外购组件" in INDEX
    assert "/api/external-packaging-purchases/pending-confirmations" in INDEX
    assert "核对并确认采购" in INDEX
    assert "请管理员在成本权限下确认采购" in INDEX
    assert "外购包材已采购" not in INDEX  # 状态由服务端事实返回，不由前端猜测。


def test_external_routing_is_lazy_with_independent_generation_and_session_reset() -> None:
    board_load = _method_source("loadRequisition")
    external_load = _method_source("loadExternalPurchaseRouting")
    assert "/api/external-packaging-purchases/pending-confirmations" not in board_load
    assert "externalPurchaseRoutingLoading" not in board_load
    assert 'this.beginLatestRequest("requisition:external-packaging")' in external_load
    assert "/api/external-packaging-purchases/pending-confirmations" in external_load
    assert "externalPurchaseRoutingLoading = true" in external_load
    assert "externalPurchaseRouting = Array.isArray" in external_load
    assert "externalPurchaseRoutingError" in external_load
    assert "externalPurchaseRoutingLoaded = true" in external_load

    reset = INDEX.split("resetPagePerformanceState()", 1)[1].split(
        "pageCacheFresh(page)", 1
    )[0]
    assert "this.externalPurchaseRouting = [];" in reset
    assert "this.externalPurchaseRoutingLoading = false;" in reset
    assert 'this.externalPurchaseRoutingError = "";' in reset
    assert 'this.externalPurchaseRoutingFilter = "";' in reset
    assert "this.externalPurchaseRoutingLoaded = false;" in reset
    assert 'this.requisitionWorkspace = "board";' in reset
    assert "this.externalIncomingSavingId = null;" in reset


def test_product_modal_keeps_external_other_and_mixed_non_other_boundaries() -> None:
    modal = INDEX.split('<div v-else-if="modal.type === \'product\'">', 1)[1].split(
        '<div v-else-if="modal.type === \'productStockPolicy\'">', 1
    )[0]
    assert "productForm.box_style!=='其他'" in modal
    assert "productForm.supply_mode==='external_purchase'" in modal
    assert "混合 BOM 必须保留纸板主件" in (ROOT / "app" / "api" / "products.py").read_text(encoding="utf-8")
    assert 'class="field product-supply-compact"' in INDEX
    assert "混合模式请在下方“外购包装组件”维护" not in INDEX


def test_regular_and_pdf_orders_auto_fill_common_box_purchase_ratio() -> None:
    regular = _method_source("selectOrderProduct")
    pdf_select = _method_source("selectImportProduct")
    pdf_apply = _method_source("applyPdfDraftToOrderForm")
    for source in (regular, pdf_select):
        assert "external_packaging_default_order_quantity_basis" in source
        assert "external_packaging_default_purchase_quantity_basis" in source
    assert (
        "external_packaging_order_quantity_basis: item.external_packaging_order_quantity_basis ?? null"
        in pdf_apply
    )
    assert (
        "external_packaging_purchase_quantity_basis: item.external_packaging_purchase_quantity_basis ?? null"
        in pdf_apply
    )
    assert "selectedProduct.external_packaging_default_order_quantity_basis" in pdf_apply
    assert "selectedProduct.external_packaging_default_purchase_quantity_basis" in pdf_apply
