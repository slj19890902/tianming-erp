from pathlib import Path


INDEX = (Path(__file__).resolve().parents[1] / "static" / "index.html").read_text(
    encoding="utf-8"
)


def test_order_group_table_keeps_actions_visible_at_1366_without_squeezing_details():
    table_start = INDEX.index(".order-group-table th,")
    responsive_start = INDEX.index("@media (max-width: 1800px) {", table_start)
    responsive = INDEX[responsive_start : INDEX.index(".main-order-number", responsive_start)]

    assert ".order-group-table" in responsive
    assert "table-layout: fixed" in responsive
    assert ".order-group-table > thead > tr > th" in responsive
    assert ".order-group-table > tbody > tr.order-group-row > td" in responsive
    assert ".order-group-table .col-actions { width: 180px; min-width: 0; }" in responsive
    assert ".order-group-table .cell-actions .toolbar-group" in responsive
    assert ".order-group-detail-row .order-group-detail-card > table" in responsive
    assert ".order-group-detail-row .order-delivery-compact { white-space: normal; }" in responsive
    assert "table-layout: fixed" in responsive
    assert "@media (max-width: 1800px)" in responsive


def test_pending_requisition_table_uses_a_fixed_non_scrolling_layout():
    start = INDEX.index(".requisition-pending-table {")
    layout = INDEX[start : INDEX.index(".requisition-demand-cell", start)]

    assert "width: 100%; min-width: 0; table-layout: fixed" in layout
    assert ".requisition-pending-table th:nth-child(12) { width: 10%; }" in layout
    assert ".requisition-pending-table .status" in layout
    assert ".requisition-pending-table td:last-child .btn" in layout
    assert ".requisition-pending-table .merge-size-editor" in layout
    assert ".requisition-pending-table .merge-supplier-select" in layout
    assert ".requisition-pending-table th:nth-child(2) { width: 10%; }" in layout
    assert "@media (max-width: 1800px)" in layout
    assert "white-space: normal" in layout
    assert "min-width: 1580px" not in layout


def test_delivery_pick_status_only_repeats_when_it_adds_information():
    assert (
        'v-if="row.pick_task && deliveryPickStatusLabel(row) '
        '!== statusText(row.status)"'
    ) in INDEX
