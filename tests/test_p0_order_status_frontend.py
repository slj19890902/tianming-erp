from pathlib import Path


INDEX = (
    Path(__file__).resolve().parents[1] / "static" / "index.html"
).read_text(encoding="utf-8")


def _orders_page() -> str:
    return INDEX.split("activePage === 'orders'", 1)[1].split(
        "activePage === 'orders_legacy'", 1
    )[0]


def _order_detail() -> str:
    return INDEX.split("modal.type === 'orderDetail'", 1)[1].split(
        "modal.type === 'mergeSuggestions'", 1
    )[0]


def test_expanded_order_rows_use_narrow_sequence_not_full_item_number() -> None:
    orders = _orders_page()
    assert 'class="col-order-item-sequence">序号</th>' in orders
    assert "item.item_sequence || itemIndex + 1" in orders
    assert "item.item_order_number" not in orders
    assert ".col-order-item-sequence" in INDEX
    assert "max-width: 4em" in INDEX
    assert "<th>展开</th>" not in orders
    assert orders.count('class="col-actions">操作</th>') == 1


def test_full_item_number_remains_available_only_in_readonly_detail() -> None:
    detail = _order_detail()
    assert "<th>明细系统单号</th>" in detail
    assert 'item.item_order_number || "-"' in detail
    assert "itemBusinessStatusKey(item)" in detail


def test_delivery_progress_is_compact_and_hidden_when_zero() -> None:
    orders = _orders_page()
    assert "Number(item.business_delivered_quantity || 0) > 0" in orders
    assert "已送 {{ item.business_delivered_quantity }}/{{ item.quantity }}" in orders
    assert (
        "已送 {{ item.business_delivered_quantity }}/{{ item.quantity }} · 序号"
        not in orders
    )
    assert "已送 0" not in orders
    assert "businessDeliveryProgressText" not in INDEX
    assert "order-status-progress" not in INDEX


def test_expanded_material_hides_weight_but_readonly_detail_keeps_it() -> None:
    orders = _orders_page()
    detail = _order_detail()
    assert "orderItemMaterialText(item, false)" in orders
    assert "orderItemMaterialText(item)" in detail
    assert "orderItemMaterialText(item, includeWeight = true)" in INDEX
    assert "if (includeWeight && weight) parts.push(weight)" in INDEX


def test_supplier_short_name_is_display_only_and_new_suppliers_remain_dynamic() -> None:
    assert 'if (name.includes("嘉林亿")) return "嘉林亿"' in INDEX
    assert 'if (name.includes("鸣朋")) return "鸣朋"' in INDEX
    assert "return name;" in INDEX
    assert 'const dynamic = (this.materialSuppliers || []).filter(Boolean)' in INDEX
    assert 'return ["全部供应商", ...new Set([...standard, ...dynamic])]' in INDEX
    assert "supplierDisplayName(item.snapshot_supplier_name)" in INDEX


def test_frontend_does_not_rederive_line_status_from_delivery_quantities() -> None:
    assert "itemDeliveryStatusKey" not in INDEX
    assert "deliveryStatusFromQuantities" not in INDEX
    assert 'return row?.business_status || row?.status || "pending_material"' in INDEX
    assert 'return item?.business_status || "pending_material"' in INDEX


def test_status_labels_tabs_and_colors_match_p0_04_contract() -> None:
    for label, key in [
        ("待报料", "pending_material"),
        ("待收料", "pending_incoming"),
        ("待生产", "pending_production"),
        ("待送货", "pending_delivery"),
        ("部分送完", "partially_delivered"),
        ("待回单", "waiting_receipt"),
        ("待对账", "pending_reconciliation"),
        ("待开票", "pending_invoice"),
        ("待结款", "pending_payment"),
        ("订单完成", "completed"),
    ]:
        assert f'{key}:"{label}"' in INDEX
    for tone in [
        "gray-orange",
        "cyan",
        "purple",
        "indigo",
        "ochre",
        "deep-yellow",
        "pink-purple",
    ]:
        assert f".status.{tone}" in INDEX
