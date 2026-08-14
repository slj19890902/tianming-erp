from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static/index.html").read_text(encoding="utf-8")


def test_moq_configuration_is_permission_gated_and_not_loaded_by_default() -> None:
    assert '"requisition.config"' in INDEX
    assert 'canConfigureRequisition() { return this.hasPermission("requisition.config"); }' in INDEX
    assert "供应商 MOQ 规则" in INDEX
    assert "requisitionTab==='moq'" in INDEX
    assert '@click="selectRequisitionTab(\'moq\')"' in INDEX
    load_requisition = INDEX[INDEX.index("async loadRequisition({"):INDEX.index("applyRequisitionHoldSummary", INDEX.index("async loadRequisition({"))]
    assert "/api/requisition/moq-rules" not in load_requisition


def test_moq_ui_uses_one_config_api_and_never_touches_orders_or_inventory() -> None:
    assert 'axios.get("/api/requisition/moq-rules"' in INDEX
    assert 'axios.post("/api/requisition/moq-rules"' in INDEX
    assert "`/api/requisition/moq-rules/${ruleId}`" in INDEX
    assert "`/api/requisition/moq-rules/${rule.id}/status`" in INDEX
    moq_block = INDEX[INDEX.index("async loadSupplierMoqRules("):INDEX.index("async loadOrders()", INDEX.index("async loadSupplierMoqRules("))]
    assert "/api/orders" not in moq_block
    assert "/api/warehouse" not in moq_block
    assert "/api/requisition/supplier-orders" not in moq_block
    assert "minimum_quantity" in moq_block
    assert "expected_version" in moq_block
    assert "evidence_reference" in moq_block
