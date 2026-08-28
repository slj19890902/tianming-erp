from pathlib import Path


INDEX = (Path(__file__).resolve().parents[1] / "static" / "index.html").read_text(
    encoding="utf-8"
)


def _method(name: str, next_name: str) -> str:
    return INDEX.split(f"          {name} {{", 1)[1].split(
        f"          {next_name} {{", 1
    )[0]


def test_order_requisition_incoming_and_guide_use_compact_main_flow() -> None:
    common_box_import = INDEX.split("async importSelectedOrderCommonBoxes", 1)[1].split(
        "async searchOrderProducts", 1
    )[0]
    assert "_compact_label:product.product_code || \"未登记存货编码\"" in common_box_import
    assert "product.product_name].filter(Boolean).join" not in common_box_import

    request_params = _method(
        "requisitionPendingRequestParams(page, supplierName)",
        "clearPendingRequisitionSelection()",
    )
    assert "return {};" in request_params
    assert "page_size" not in request_params
    assert '<pager v-if="requisitionTab===\'pending\'"' not in INDEX
    filtered = _method("filteredRequisitionPending()", "filteredExternalPurchaseRouting()")
    assert "requisitionSupplierFilter" in filtered
    assert ".filter(" in filtered

    incoming_size = _method("incomingListPageSize()", "incomingRowsForTab()")
    assert "window.innerHeight" in incoming_size
    assert "Math.floor" in incoming_size

    incoming_guide = INDEX.split("incomingNextStepGuide.visible", 1)[1].split(
        "productionNextStepGuide.visible", 1
    )[0]
    assert "下一步：去送货" in incoming_guide
    assert "去生产" not in incoming_guide
    assert "goToDeliveryFromIncomingGuide" in incoming_guide


def test_delivery_picker_is_one_compact_two_line_location_aware_table() -> None:
    delivery_modal = INDEX.split("modal.type === 'delivery'", 1)[1].split(
        "modal.type === 'deliveryRoutePlan'", 1
    )[0]
    assert "订单待送（默认优先）" not in delivery_modal
    assert "选择待送订单（{{" in delivery_modal
    assert "选定客户后已预载，点击即可选择" not in delivery_modal
    assert 'class="btn small success delivery-batch-select-all"' in delivery_modal
    assert 'class="delivery-batch-table"' in delivery_modal
    assert 'class="delivery-batch-select-cell"' in delivery_modal
    assert 'class="delivery-batch-order-location"' in delivery_modal
    assert "deliveryBatchOrderLocationText(item)" in delivery_modal

    assert "delivery-batch-order-location" in INDEX
    assert "-webkit-line-clamp:2" in INDEX
    assert "delivery-batch-select-cell" in INDEX


def test_delivery_finance_headers_dropdown_and_manual_invoice_rule_are_visible() -> None:
    assert 'class="page-head delivery-page-head"' in INDEX
    assert 'class="page-head finance-page-head"' in INDEX
    assert "finance-filter-panel" in INDEX
    assert ".finance-filter-panel { overflow:visible" in INDEX
    assert "canFinance && statement.confirmation_status!=='confirmed'" not in INDEX
    assert (
        'v-if="canFinance" class="btn small" '
        ':disabled="statement.confirmation_status!==\'confirmed\' || '
        'Number(statement.pending_invoice_amount)<=0'
        in INDEX
    )
    assert "statement.invoice_status==='invoiced'" in INDEX


def test_production_defaults_to_history_adjustment_and_staging_placement() -> None:
    assert 'productionTab: "history"' in INDEX
    production = INDEX.split("<template v-else-if=\"activePage === 'production'\">", 1)[1].split(
        "<template v-else-if=\"activePage === 'deliveries'\">", 1
    )[0]
    assert "生产与成品" in production
    assert "@click=\"productionTab='pending'\"" not in production
    assert "完工历史" in production
    assert "待送成品归位" in production
    assert "modifyProductionActualQuantity(row)" in production
    assert "productionPlacement" in production
    assert ">归位</button>" in production
    assert "归位并同步地图" not in production

    adjust = _method(
        "async modifyProductionActualQuantity(row)",
        "async supplementProductionCompletion(row)",
    )
    assert "/actual-quantity" in adjust
    assert "expected_task_version" in adjust
    assert "expected_lot_version" in adjust
    assert "confirm(" in adjust

    transfer = _method(
        "async transferProductionCompletionToStock(row)",
        "async modifyProductionActualQuantity(row)",
    )
    assert "confirm(" not in transfer
    assert "loadProductionPlacement" in transfer
