from decimal import Decimal
import json
from pathlib import Path
import shutil
import subprocess

from app.services.product_specification import (
    dimension_specification,
    normalized_specification_text,
    resolved_product_specification,
)


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
CONTRACT_PRINT = (ROOT / "static" / "contract-print.html").read_text(encoding="utf-8")
MOBILE_ERP = (ROOT / "static" / "mobile_erp.html").read_text(encoding="utf-8")


def test_structured_dimensions_support_two_or_three_dimensions() -> None:
    assert dimension_specification(Decimal("778"), Decimal("1137"), None) == "778×1137mm"
    assert dimension_specification(Decimal("520"), Decimal("350"), Decimal("300")) == "520×350×300mm"
    assert dimension_specification(Decimal("430.50"), Decimal("68.00"), None) == "430.5×68mm"
    assert dimension_specification(None, Decimal("68"), None) is None
    assert dimension_specification(Decimal("430"), None, None) is None


def test_only_missing_placeholders_fall_back_to_structured_dimensions() -> None:
    for placeholder in (None, "", "-", "—", "－", "未登记", "规格未登记"):
        assert resolved_product_specification(
            placeholder,
            length_mm=778,
            width_mm=1137,
        ) == "778×1137mm"
    assert resolved_product_specification(
        "客户冻结规格 26×45",
        length_mm=778,
        width_mm=1137,
    ) == "客户冻结规格 26×45"


def test_meaningful_order_snapshot_precedes_current_product_dimensions() -> None:
    assert resolved_product_specification(
        "-",
        fallback_snapshots=("订单冻结规格",),
        length_mm=778,
        width_mm=1137,
    ) == "订单冻结规格"


def test_pure_dimension_snapshots_are_normalized_without_rewriting_custom_text() -> None:
    assert normalized_specification_text("778x1137") == "778×1137mm"
    assert normalized_specification_text("520*350*300 mm") == "520×350×300mm"
    assert normalized_specification_text("客户冻结规格 26×45") == "客户冻结规格 26×45"


def test_common_box_frontend_does_not_require_height_for_specification() -> None:
    start = INDEX.index("          spec(row) {")
    end = INDEX.index("          statusTone(value) {", start)
    body = INDEX[start:end]
    assert "[row.length_mm,row.width_mm].every" in body
    assert "[row.length_mm,row.width_mm,row.height_mm].every" not in body
    assert 'dimensions.map(fmt).join("×")' in body
    assert "Number(row.height_mm) > 0" in body


def test_order_page_moves_mm_unit_to_heading_and_keeps_values_compact(tmp_path: Path) -> None:
    assert "<th>规格 / 尺寸</th>" not in INDEX
    assert INDEX.count("规格mm") >= 8
    assert "orderSearchHighlightParts(orderSpecificationText(item.snapshot_spec))" in INDEX
    assert "{{ orderSpecificationText(item.snapshot_spec) }}" in INDEX
    assert "{{ orderSpecificationText(orderTrace.item.specification) }}" in INDEX
    assert "{{ orderSpecificationText(spec(row)) }}" in INDEX
    assert 'specification:this.orderSpecificationText(itemSnapshot.snapshot_spec, "")' in INDEX

    start = INDEX.index('          orderSpecificationText(value, emptyValue = "-") {')
    end = INDEX.index("          statusTone(value) {", start)
    method = INDEX[start:end].strip()
    script = f"""
const methods = {{
{method}
}};
const values = [
  methods.orderSpecificationText("778×1137mm"),
  methods.orderSpecificationText("520×350×300 MM"),
  methods.orderSpecificationText("客户冻结规格 26×45"),
  methods.orderSpecificationText("客户专用规格mm"),
  methods.orderSpecificationText(null),
  methods.orderSpecificationText("mm"),
  methods.orderSpecificationText(null, ""),
];
console.log(JSON.stringify(values));
"""
    node = shutil.which("node")
    assert node is not None, "Node.js is required for the order specification display test"
    target = tmp_path / "order-specification-display.js"
    target.write_text(script, encoding="utf-8")
    result = subprocess.run(
        [node, str(target)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [
        "778×1137",
        "520×350×300",
        "客户冻结规格 26×45",
        "客户专用规格mm",
        "-",
        "mm",
        "",
    ]


def test_contract_and_mobile_views_keep_two_dimensional_products_visible() -> None:
    contract_start = INDEX.index("          contractSpecification(row) {")
    contract_end = INDEX.index("          contractMaterialText(row) {", contract_start)
    contract_body = INDEX[contract_start:contract_end]
    assert "values.slice(0, 2).some" in contract_body
    assert "values.filter" in contract_body
    assert "values.every" not in contract_body
    assert "const dimensions = [item.length_mm,item.width_mm]" in CONTRACT_PRINT
    assert "task.carton_height_mm].filter" in MOBILE_ERP


def test_all_customer_facing_product_specification_layers_use_the_shared_rule() -> None:
    expected_tokens = {
        "app/api/orders.py": "resolved_product_specification(",
        "app/api/deliveries.py": "resolved_product_specification(",
        "app/api/requisition.py": "resolved_product_specification(",
        "app/api/incoming.py": "resolved_product_specification(",
        "app/api/finance.py": "resolved_product_specification(",
        "app/api/invoice_tasks.py": "resolved_product_specification(",
        "app/api/tianhua_pre_delivery.py": "resolved_product_specification(",
        "app/api/warehouse.py": "product_dimension_specification(",
        "app/api/mobile_erp.py": "product_dimension_specification(",
        "app/services/production_workflow.py": "resolved_product_specification(",
        "app/services/requisition_production_print.py": "resolved_product_specification(",
        "app/services/order_document_trace.py": "resolved_product_specification(",
        "app/services/contract_pdf.py": "dimension_specification(",
        "app/services/stocktake.py": "dimension_specification(",
        "app/services/warehouse_twin_dashboard.py": "dimension_specification(",
    }
    for relative_path, token in expected_tokens.items():
        source = (ROOT / relative_path).read_text(encoding="utf-8")
        assert token in source, relative_path
