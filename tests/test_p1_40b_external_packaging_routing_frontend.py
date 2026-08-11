from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def test_mixed_mode_and_external_routing_are_visible_without_replacing_paper_flow() -> None:
    assert '<option value="mixed_bom">纸板主件＋外购包材组件</option>' in INDEX
    assert "外购包材待确认" in INDEX
    assert "纯护角、EPE等不进入纸板报料" in INDEX
    assert "/api/external-packaging-purchases/pending-confirmations" in INDEX
    assert "核对并确认采购" in INDEX
    assert "请管理员在成本权限下确认采购" in INDEX
    assert "外购包材已采购" not in INDEX  # 状态由服务端事实返回，不由前端猜测。


def test_external_routing_uses_same_workbench_request_generation_and_session_reset() -> None:
    load = INDEX.split("async loadRequisition({skipAutoRelease=false}={})", 1)[1].split(
        "applyRequisitionHoldSummary(source)", 1
    )[0]
    assert 'this.beginLatestRequest("requisition:pending")' in load
    assert "pendingRequisitionRequestIsCurrent" in load
    assert "Promise.all([" in load
    assert "externalPurchaseRoutingLoading = true" in load
    assert "externalPurchaseRouting = Array.isArray" in load
    assert "externalPurchaseRoutingError" in load

    reset = INDEX.split("resetPagePerformanceState()", 1)[1].split(
        "pageCacheFresh(page)", 1
    )[0]
    assert "this.externalPurchaseRouting = [];" in reset
    assert "this.externalPurchaseRoutingLoading = false;" in reset
    assert 'this.externalPurchaseRoutingError = "";' in reset
    assert "this.externalIncomingSavingId = null;" in reset


def test_product_modal_keeps_external_other_and_mixed_non_other_boundaries() -> None:
    modal = INDEX.split('<div v-else-if="modal.type === \'product\'">', 1)[1].split(
        '<div v-else-if="modal.type === \'productStockPolicy\'">', 1
    )[0]
    assert "productForm.box_style!=='其他'" in modal
    assert "productForm.supply_mode==='external_purchase'" in modal
    assert "混合 BOM 必须保留纸板主件" in (ROOT / "app" / "api" / "products.py").read_text(encoding="utf-8")
    assert "混合模式请在下方“外购包装组件”维护" in INDEX