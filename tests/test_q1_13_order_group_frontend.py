from pathlib import Path


INDEX = (
    Path(__file__).resolve().parents[1] / "static" / "index.html"
).read_text(encoding="utf-8")


def _modern_orders_template() -> str:
    start = INDEX.index('<table class="order-group-table">')
    end = INDEX.index('<template v-else-if="activePage === \'orders_legacy\'">', start)
    return INDEX[start:end]


def test_group_row_has_one_group_detail_and_item_rows_only_trace_and_edit() -> None:
    orders = _modern_orders_template()
    assert '@click="openOrderGroupDetail(group)"' in orders
    assert orders.count('@click="openOrderGroupDetail(group)"') == 1

    item_actions_start = orders.index(
        '<button class="btn small primary" @click="openOrderTrace(row,item)">追溯</button>'
    )
    item_actions_end = orders.index("</td>", item_actions_start)
    item_actions = orders[item_actions_start:item_actions_end]
    assert "openOrderTrace(row,item)" in item_actions
    assert "openOrderItem(row,item)" in item_actions
    assert "openOrderDetail(row)" not in item_actions
    assert "previewDrawingFile" not in item_actions
    assert ">图纸<" not in item_actions


def test_group_detail_loads_authoritative_backend_group_and_keeps_drawings() -> None:
    assert 'axios.get("/api/orders/group-detail"' in INDEX
    assert "anchor_order_id: group.orders[0].id" in INDEX
    assert "modal.type === 'orderGroupDetail'" in INDEX
    detail_start = INDEX.index("modal.type === 'orderGroupDetail'")
    detail_end = INDEX.index("modal.type === 'orderDetail'", detail_start)
    detail = INDEX[detail_start:detail_end]
    assert "orderGroupDetail.orders" in detail
    assert "displayOrderNumber(order)" in detail
    assert "order.items" in detail
    for core_field in (
        "orderItemMaterialText(item)",
        "item.snapshot_production_notes",
        "item.finished_inventory_reserved_qty",
        "item.production_required_qty",
        "formatReportDims(item)",
        "formatCrease(item)",
        "item.unit_price",
        "item.subtotal",
    ):
        assert core_field in detail
    assert "previewDrawingFile(item.drawing_file || item.product_drawing_file)" in detail
    assert 'scope: this.filters.orderScope || "active"' in INDEX
    assert "订单整组详情加载失败" in INDEX


def test_item_trace_remains_bound_to_exact_order_and_item() -> None:
    assert (
        "`/api/orders/${orderId}/items/${itemId}/documents`"
        in INDEX
    )
    assert "const orderId = Number(order?.id || 0)" in INDEX
    assert "const itemId = Number(item?.id || 0)" in INDEX
    assert "latestRequestControllers.get(requestKey) !== controller" in INDEX
    assert "openOrderTrace(group" not in INDEX


def test_frontend_group_key_trims_customer_po_like_backend() -> None:
    grouped_start = INDEX.index("groupedOrderRows()")
    grouped_end = INDEX.index("orderStatusOptions()", grouped_start)
    grouped = INDEX[grouped_start:grouped_end]
    assert 'String(row.customer_po || "").trim()' in grouped
