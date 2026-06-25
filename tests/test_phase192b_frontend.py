"""Frontend guard tests for v0.19.2-B material-dictionary UI + 三层A瓦 fix.

These assert against static/index.html so the agreed UI rules don't regress:
  * 材质字典 no longer renders a 楞型 filter tab row / 楞型 column
  * 材质代码 column uses real code (materialBaseCode no longer returns paper_composition)
  * 克重结构 prefers paper_composition (纸种明细)
  * material-code dropdown filters by supplier+layer only (no flute filter)
  * 常用箱 form field order: 材质供应商 → 层数 → 楞型 → 材质代码
  * 供应商调价 / 价格历史 entry points exist
"""
from pathlib import Path

import pytest

HTML = (Path(__file__).resolve().parent.parent / "static" / "index.html").read_text(
    encoding="utf-8"
)


class TestMaterialDictionaryUI:
    def test_table_header_order_no_flute_column(self):
        # 材质字典表头：供应商/层数/材质代码/克重结构/平方报价/报价日期/操作
        assert "<th>供应商</th><th>层数</th><th>材质代码</th><th>克重结构</th>" in HTML

    def test_no_flute_filter_tabs_in_dictionary(self):
        # 材质字典工具栏不再有 materialFluteTabs 楞型筛选按钮
        assert "v-for=\"ft in materialFluteTabs\"" not in HTML

    def test_material_base_code_returns_real_code_not_paper(self):
        # materialBaseCode 不再 fallback 到 paper_composition
        assert "if (row.paper_composition) return row.paper_composition;" not in HTML

    def test_weight_structure_prefers_paper_composition(self):
        assert "const paper = (row.paper_composition || \"\").trim();" in HTML

    def test_displayed_materials_no_flute_filter(self):
        assert "材质字典不再按楞型拆分/过滤" in HTML

    def test_supplier_price_adjust_button(self):
        assert "@click=\"openPriceAdjust()\"" in HTML

    def test_price_history_button(self):
        assert "@click=\"openPriceHistory(row)\"" in HTML


class TestMaterialDropdownSupplierLayerOnly:
    def test_dropdown_filter_ignores_flute(self):
        # filteredMaterialOptions 不再按 fluteMatches 过滤材质代码下拉
        assert "材质代码只按 供应商 + 层数 过滤" in HTML
        assert "if (fluteType && !this.fluteMatches(m.flute_type, fluteType)) return false;" not in HTML


class TestProductFormFieldOrder:
    def test_field_order_supplier_layer_flute_code(self):
        i_supplier = HTML.index("材质选择顺序：材质供应商 → 层数 → 楞型 → 材质代码")
        block = HTML[i_supplier : i_supplier + 4000]
        p_supplier = block.index(">材质供应商<")
        p_layer = block.index(">层数<")
        p_flute = block.index(">楞型<")
        p_code = block.index("材质（代码")
        assert p_supplier < p_layer < p_flute < p_code

    def test_three_layer_flute_options_include_a(self):
        # 三层楞型必须含 A/B/E
        assert "A瓦（三层）" in HTML and "B瓦（三层）" in HTML and "E瓦（三层）" in HTML

    def test_order_item_three_layer_flute_has_a(self):
        assert 'if (lc === 3) return ["B", "A", "E"];' in HTML
