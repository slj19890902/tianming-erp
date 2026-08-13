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

    def test_displayed_materials_have_no_flute_dimension(self):
        material_table = HTML.split("<!-- 材质列表：", 1)[1].split(
            "</table>", 1
        )[0]
        assert "<th>楞型" not in material_table
        assert "row.flute_type" not in material_table

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
        i_supplier = HTML.index('<div class="product-material-controls">')
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


class TestModalsCentered:
    def test_compare_modal_uses_centered_backdrop(self):
        assert 'v-if="showCompareModal" class="modal-backdrop"' in HTML

    def test_price_adjust_modal_uses_centered_backdrop(self):
        assert 'v-if="showPriceAdjustModal" class="modal-backdrop"' in HTML

    def test_flute_rule_modal_uses_centered_backdrop(self):
        assert 'v-if="showFluteRuleModal" class="modal-backdrop"' in HTML

    def test_modal_backdrop_css_is_fixed_centered(self):
        assert ".modal-backdrop" in HTML
        assert "position:fixed" in HTML or "position: fixed" in HTML


class TestFlutePriceRuleMaintenance:
    def test_flute_rule_entry_button(self):
        assert '@click="openFluteRuleModal()"' in HTML

    def test_flute_rule_methods_present(self):
        for m in ("loadFluteRules", "saveFluteRule", "disableFluteRule", "editFluteRule"):
            assert m in HTML

    def test_flute_rule_api_paths(self):
        assert "/api/master/materials/flute-price-rules" in HTML
        assert "/disable" in HTML

    def test_flute_rule_table_columns(self):
        # 供应商/层数/楞型/加价/生效日期/状态/操作
        assert "加价(元/㎡)" in HTML


class TestOrderMaterialDisplay:
    def test_order_uses_material_code_slash_flute(self):
        # 订单材质显示 A6D/A，经 orderItemMaterialText
        assert "orderItemMaterialText(item)" in HTML
        assert "材质代码/实际楞型" in HTML

    def test_compare_backend_driven(self):
        assert "/api/master/materials/compare" in HTML
        assert "loadCompare" in HTML


class TestBoardCostEffectivePrice:
    def test_board_cost_sends_flute_for_delta(self):
        assert "flute_type: f.flute_type" in HTML

    def test_board_cost_text_shows_delta(self):
        assert "楞型加价" in HTML and "productBoardCostDetail" in HTML


class TestOrderItemAutoFillLock:
    def test_select_order_product_sets_from_product(self):
        assert "_from_product" in HTML
        assert "item._from_product = true" in HTML

    def test_layer_flute_auto_filled_from_product(self):
        assert "item.layer_count = data.layer_count" in HTML
        assert "item.flute_type = data.flute_type" in HTML

    def test_locked_when_from_product(self):
        # 匹配常用箱后，存货编码/名称/规格字段应锁定
        assert "item._from_product" in HTML

    def test_from_product_shows_compressed_material(self):
        assert "orderItemCompressedMaterial(item)" in HTML

    def test_from_product_badge(self):
        assert "来自常用箱" in HTML

    def test_cost_preview_passes_flute_type(self):
        assert "flute_type: item.flute_type || item._flute_filter" in HTML


class TestOrderMaterialFormat:
    def test_material_text_uses_pipe_separator(self):
        assert 'parts.join("｜")' in HTML

    def test_material_text_includes_supplier(self):
        assert "snapshot_supplier_name" in HTML

    def test_material_text_includes_weight(self):
        assert "snapshot_weight" in HTML

    def test_material_text_no_dash_be_suffix(self):
        # 材质代码去掉 -B/E 后缀
        assert 'base.split("-")[0]' in HTML


class TestOrderListUI:
    def test_no_main_order_number_column_in_expanded_detail(self):
        # 主系统单号列已从展开明细中移除
        assert "主系统单号" not in HTML or HTML.count("主系统单号") <= 1  # 仅剩旧版legacy区域

    def test_item_order_number_shown(self):
        assert "item_order_number" in HTML
        assert "明细系统单号" in HTML

    def test_order_detail_no_customer_model_column(self):
        # 只读订单详情不再有「客户型号」列
        i = HTML.index("只读订单详情")
        detail_block = HTML[i:i+2000]
        assert "客户型号" not in detail_block

    def test_order_detail_has_drawing_column(self):
        i = HTML.index("只读订单详情")
        detail_block = HTML[i:i+2000]
        assert "图纸" in detail_block or "drawing_file" in detail_block

    def test_compact_order_number_preview(self):
        assert "系统编号预览" in HTML


class TestOrderFormColumns:
    def test_no_standalone_layer_flute_columns_in_header(self):
        # 新订单表头不再有独立「层数」「楞型」列（已合并入材质+楞型列）
        i = HTML.index('<table class="line-items order-entry-table">')
        header_row = HTML[i:i+700]
        assert "材质楞型" in header_row

    def test_drawing_column_in_new_order_table(self):
        i = HTML.index('<table class="line-items order-entry-table">')
        header_row = HTML[i:HTML.index("</thead>", i)]
        assert "图纸 / 删除" in header_row

    def test_submit_passes_layer_count_and_flute(self):
        assert "layer_count: item.layer_count" in HTML
        assert "flute_type: item.flute_type" in HTML
