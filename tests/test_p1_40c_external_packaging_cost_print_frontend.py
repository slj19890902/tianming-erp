from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
PRINT_PAGE = (ROOT / "static" / "external-purchase-print.html").read_text(
    encoding="utf-8"
)


def test_corner_guard_formal_meter_price_conversion_is_exact_and_not_persisted(
    tmp_path: Path,
) -> None:
    node = shutil.which("node")
    assert node, "Node.js is required for the formal-price calculation contract"
    start = INDEX.index("const calculateCornerGuardFormalCost")
    end = INDEX.index("const blankProduct", start)
    source = INDEX[start:end] + "\nconsole.log(JSON.stringify(calculateCornerGuardFormalCost(780,1000,1.10)));\n"
    target = tmp_path / "p1-40c-corner-reference.js"
    target.write_text(source, encoding="utf-8")
    result = subprocess.run(
        [node, str(target)], capture_output=True, text=True, encoding="utf-8", check=False
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "rate_per_meter": 1.1,
        "length_mm": 780,
        "quantity": 1000,
        "unit_cost": 0.858,
        "total_cost": 858,
    }
    assert "护角采购参考成本（正式报价）" in INDEX
    assert "CORNER_GUARD_REFERENCE_COST_PER_METER" not in INDEX
    assert "delete payload._corner_reference_quantity" in INDEX
    assert "external_packaging_price_versions" not in INDEX


def test_product_and_order_ui_separate_purchase_cost_from_customer_sale_price() -> None:
    assert "供应商采购成本（正式报价）" in INDEX
    assert "客户默认销售单价" in INDEX
    assert "客户销售单价" in INDEX
    assert "这是给客户的销售单价，不是供应商采购成本" in INDEX
    assert 'v-if="canViewCosts" class="sensitive-price">供应商采购成本' in INDEX
    assert "row.current_purchase_price" in INDEX
    assert "不会互相覆盖" in INDEX
    assert "EPE" in INDEX
    assert "<label>图纸</label>" in INDEX


def test_external_purchase_print_shows_supplier_specification_and_quantity() -> None:
    for label in (
        "材质",
        "孔径(mm)",
        "规格(mm)",
        "数量",
        "material",
        "aperture_mm",
        "dimensions_mm",
        "specification_summary",
        "purchase_quantity",
        "purchase_unit",
    ):
        assert label in PRINT_PAGE
    for forbidden in (
        "客户",
        "订单来源",
        "供应商产品代码",
        "供应商采购成本",
        "source_customer_name",
        "source_order_number",
        "source_item_order_number",
        "order_drawing_file_name",
        "order_drawing_version_label",
        "unit_price",
        "currency",
        "tax_rate",
        "line_amount",
        "total_amount",
        "MOQ",
        "运费",
        "纸板材质",
        "楞型",
        "开料",
        "压线",
    ):
        assert forbidden not in PRINT_PAGE
    for write_method in ('method:"POST"', 'method:"PUT"', 'method:"PATCH"', 'method:"DELETE"'):
        assert write_method not in PRINT_PAGE


def test_changed_inline_javascript_passes_node_check(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node, "Node.js is required for frontend syntax validation"
    for name, source in (("index", INDEX), ("external-purchase-print", PRINT_PAGE)):
        scripts = [
            script
            for script in re.findall(
                r"<script(?:\s[^>]*)?>(.*?)</script>", source, flags=re.DOTALL
            )
            if script.strip()
        ]
        assert scripts
        target = tmp_path / f"{name}.js"
        target.write_text("\n".join(scripts), encoding="utf-8")
        result = subprocess.run(
            [node, "--check", str(target)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        assert result.returncode == 0, result.stderr
