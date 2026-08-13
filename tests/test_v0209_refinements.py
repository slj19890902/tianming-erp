from decimal import Decimal
from pathlib import Path

from app.api.orders import _order_item_cost_totals


INDEX = Path("static/index.html").read_text(encoding="utf-8")
PRINT = Path("static/requisition-print.html").read_text(encoding="utf-8")


def test_order_cost_totals_use_actual_quantity():
    result = _order_item_cost_totals(
        quantity=500,
        unit_price=Decimal("3.92"),
        subtotal=Decimal("1960.00"),
        unit_cost=Decimal("1.72"),
    )
    assert result["sale_amount"] == "1960.00"
    assert result["total_estimated_cost"] == "860.00"
    assert result["total_estimated_gross_profit"] == "1100.00"


def test_order_edit_contains_common_box_fields_and_total_costs():
    for label in (
        "实际数量",
        "拼箱方式",
        "每箱片数",
        "报料长宽",
        "压线类型",
        "压线尺寸",
        "总预估成本",
        "总预估毛利",
    ):
        assert label in INDEX
    assert "修改材质并保存时会自动同步关联常用箱" in INDEX
    assert "同步本页其他常用箱资料" in INDEX


def test_requisition_ui_prioritizes_supplier_and_purchase_quantity():
    assert "供应商优先选择" in INDEX
    assert "requisitionSupplierCounts" in INDEX
    assert "更换供应商/材质" in INDEX
    assert "客户 / 存货编码 / 产品" in INDEX
    assert "<th>需求小片</th>" not in INDEX
    assert "purchase-qty-cell" in INDEX
    assert "pieces-per-box-cell" in INDEX
    assert "{{ m.pieces_per_box }}片" in INDEX
    assert "每箱 {{ line.source_items?.[0]?.pieces_per_box || 1 }} 片" in INDEX
    assert "m.material_display" in INDEX
    assert "line.material_display" in INDEX


def test_supplier_purchase_print_uses_legacy_columns_without_total():
    for label in ("序号", "纸板长宽（mm）", "压线(mm）", "材质/楞型", "数量", "报料备注"):
        assert label in PRINT
    assert "苏州工业园区天明纸品包装厂" in PRINT
    assert "采购单" in PRINT
    assert "存货编码 / 产品" not in PRINT
    assert "总张数" not in PRINT
    assert "row.specification" in PRINT
    assert 'class="title"' in PRINT
    assert 'class="head"' in PRINT
    assert PRINT.index('class="title"') < PRINT.index('class="head"')
    assert "data.sender?.address" in PRINT
    assert "data.sender?.phone" in PRINT


def test_supplier_purchase_preview_uses_company_sender_and_new_columns():
    for label in ("纸板长宽（mm）", "压线(mm）", "材质/楞型"):
        assert label in INDEX
    assert "modal.data.sender?.company_name" in INDEX
    assert "modal.data.sender?.address" in INDEX
    assert "modal.data.sender?.phone" in INDEX
