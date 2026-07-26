from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
INDEX_PAGE = PROJECT_ROOT / "static" / "index.html"
PRINT_PAGE = PROJECT_ROOT / "static" / "delivery-print.html"
MOBILE_PICK_PAGE = PROJECT_ROOT / "static" / "mobile_delivery_pick.html"


def _index_source() -> str:
    return INDEX_PAGE.read_text(encoding="utf-8")


def _print_source() -> str:
    return PRINT_PAGE.read_text(encoding="utf-8")


def _mobile_pick_source() -> str:
    return MOBILE_PICK_PAGE.read_text(encoding="utf-8")


def test_parent_priced_delivery_shows_read_only_component_goods_plan() -> None:
    source = _index_source()
    delivery_modal = source[
        source.index('<div v-else-if="modal.type === \'delivery\'">') :
        source.index('<div v-else-if="modal.type === \'tianhuaPreimport\'">')
    ]

    assert "统一计价组合 · 本次实际随货明细" in delivery_modal
    assert "组件数量来自订单级需求，只读；子件不单独计价" in delivery_modal
    assert "deliveryComponentTargetQuantity(component)" in delivery_modal
    assert "deliveryComponentDeliveredQuantity(component)" in delivery_modal
    assert "deliveryComponentRemainingQuantity(component)" in delivery_modal
    assert "deliveryComponentPlannedQuantity(line, component)" in delivery_modal
    assert 'v-model.number="component' not in delivery_modal


def test_delivery_payload_keeps_component_quantities_server_derived() -> None:
    source = _index_source()
    start = source.index("const items = (this.deliveryForm.lines || [])")
    end = source.index('if (this.modal.type === "statement")', start)
    save_block = source[start:end]

    assert "component_deliveries" not in save_block
    assert "component_lines" not in save_block
    assert "order_item_id: Number(line.order_item_id)" in save_block
    assert "delivered_quantity: Number(line.delivered_quantity)" in save_block
    assert "kit_availability: candidate.kit_availability || null" in source
    assert "component_lines: it.component_lines || []" in source


def test_delivery_list_counts_and_displays_actual_component_goods() -> None:
    source = _index_source()

    assert "deliveryActualGoodsQuantity(row)" in source
    assert "deliverySavedComponentLines(item)" in source
    assert "套内子件：" in source
    assert "（不单独计价）" in source
    assert "parentQuantity + componentQuantity" in source


def test_print_flattens_parent_and_component_into_real_goods_rows(
    tmp_path: Path,
) -> None:
    node = shutil.which("node")
    assert node is not None, "Node.js is required for the print contract test"
    source = _print_source()
    helpers = re.search(
        r"(function normalizeComponentPrintLine.*?)(?=\n    function populateSheet)",
        source,
        re.DOTALL,
    )
    assert helpers is not None

    target = tmp_path / "component-print-contract.js"
    target.write_text(
        helpers.group(1)
        + """
const rows = normalizeDeliveryPrintItems({
  items: [{
    customer_po: "UAT-PO",
    product_code: "T250-OUTER",
    product_name: "T250 外包装盒",
    unit: "PCS",
    quantity: 3000,
    component_lines: [{
      snapshot_id: 9,
      component_code: "T250-LINER",
      component_name: "T250 内衬",
      delivered_quantity: 2700,
      pricing_included: false
    }]
  }],
  component_lines: [{
    component_code: "SHOULD-NOT-DUPLICATE",
    delivered_quantity: 999
  }]
});
process.stdout.write(JSON.stringify(rows));
""",
        encoding="utf-8",
    )
    result = subprocess.run(
        [node, str(target)],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr
    rows = json.loads(result.stdout)

    assert [row["product_code"] for row in rows] == ["T250-OUTER", "T250-LINER"]
    assert [row["quantity"] for row in rows] == [3000, 2700]
    assert rows[0]["pricing_included"] is True
    assert rows[1]["pricing_included"] is False
    assert rows[1]["is_component_line"] is True


def test_print_total_uses_all_actual_goods_without_creating_a_price_line() -> None:
    source = _print_source()

    assert "Array.isArray(data.actual_goods_items)" in source
    assert 'item.line_type === "component"' in source
    assert "本页数量（实际货物）：" in source
    assert "计价主件总数：" in source
    assert "实际货物总数：" in source
    assert 'getField(sheet, "pricedQuantity").textContent = data.total_quantity ?? 0' in source
    assert "total_actual_goods_quantity: actualGoodsTotalQuantity" in source
    assert "const actualGoodsTotalQuantity = normalizedItems.reduce(" in source
    assert "(total, item) => total + (Number(item.quantity) || 0)" in source
    assert "套内子件，不单独计价" in source
    assert "pricing_included: false" in source
    assert "<th>单价</th>" not in source
    assert "<th>金额</th>" not in source


def test_mobile_pick_shows_components_as_read_only_derived_goods() -> None:
    source = _mobile_pick_source()
    start = source.index("function renderPickComponents(item)")
    end = source.index("function renderItem(item)", start)
    component_block = source[start:end]

    assert "本次随货子件（只读，不单独确认）" in component_block
    assert "item.component_lines" in component_block
    assert "planned_delivery_quantity" in component_block
    assert "pick-component-quantity" in component_block
    assert "updateItem(" not in component_block
    assert 'class="picked-qty"' not in component_block
