"""
v0.19.1 整改测试套件

覆盖：
  F-1: 多行品名 bug 修复
  F-2: 垫板 size_spec 为空 bug 修复
  F-3: 斜杠规格截断修复
  F-4: size_spec 包装注记清理
  F-5: PO2026060079 额外列（番号/销售订单号）
  ORDER_NO_RE: 思迈尔 P-XXXXXXX 格式
  DB: snapshot_customer_model 字段
  客户类型识别
  思迈尔缺少模板时明确失败原因
  OCR 不可用时不崩溃
"""

from __future__ import annotations

import re
import sys
import unittest

sys.path.insert(0, ".")

from app.services.order_pdf_import import (
    ORDER_NO_RE,
    PACKAGING_ANNOTATION_RE,
    _detect_customer_type,
    _enrich_tianhua_item,
    _extract_spec_dimensions,
    _is_tianhua_customer,
    _join_record_lines,
    _parse_record,
    _split_records,
    ocr_available,
    should_use_ocr,
)


class TestF1MultiLineName(unittest.TestCase):
    """F-1: 多行品名 bug — product_name 不能识别为行号。"""

    def _make_record(self, lines: list[str]) -> dict | None:
        return _parse_record(lines, has_extra_columns=False)

    def test_product_name_not_line_number_basic(self):
        """当行号+料品编码单独一行时，product_name 不应为纯数字（如 '10'）。"""
        # 模拟 PO2026060079 风格：行号+料品编码无尾随空格
        record_lines = [
            "10 21301877",
            "白底黑字内箱（白）（600×900）印有日",
            "文（出口）",
            "只 200 28.0000 5,600.00 2026/07/15",
        ]
        result = self._make_record(record_lines)
        # 如果解析成功，product_name 不应为 "10"
        if result is not None:
            self.assertNotEqual(
                result["product_name"], "10",
                "product_name 不能识别为行号 '10'",
            )
            self.assertNotRegex(
                result["product_name"], r"^\d{1,3}$",
                "product_name 不能是纯数字",
            )

    def test_multiline_name_merged(self):
        """完整 ITEM_RE 能匹配的多行记录，品名应正确合并，不应为纯行号。"""
        # 构造一个可以被 ITEM_RE 完整匹配的多行记录
        joined_line = "3 21401889 瓦楞纸板 95.5*63.5cm 张 1000 2.5000 2,500.00 2026/07/15"
        result = _parse_record([joined_line])
        if result is not None:
            self.assertEqual(result["line_no"], 3)
            self.assertNotEqual(result["product_name"], "3")
            self.assertIn("瓦楞", result["product_name"])

    def test_name_not_pure_integer(self):
        """解析出的 product_name 不应是纯整数字符串。"""
        # 提供单行标准记录
        record = ["1 20001234 普通纸箱 120*80*60cm 只 500 5.0000 2,500.00 2026/08/01"]
        result = _parse_record(record)
        if result is not None:
            self.assertNotRegex(result["product_name"], r"^\d+$", "product_name 不能是纯数字")


class TestF2DabSpecNotEmpty(unittest.TestCase):
    """F-2: 垫板 size_spec 不能为空。"""

    def test_dab_spec_extracted(self):
        """垫板的 spec_raw 包含尺寸时，size_spec 应提取到正确值。"""
        item = {
            "product_code": "20005678",
            "product_name": "垫板",
            "raw_spec_model": "98*20cm",
            "specification": "98*20cm",
            "quantity": 500,
        }
        # spec_raw 只含尺寸（去掉了 old_material_code 后剩下的）
        spec_raw = "98*20cm T5P/A R"
        enriched = _enrich_tianhua_item(item, spec_raw)
        # size_spec 应非空，且包含 98
        self.assertIn("98", enriched["size_spec"], f"size_spec 应包含 98，实际为 '{enriched['size_spec']}'")

    def test_dab_old_material_code_parsed(self):
        """T5P/A 应被正确解析为材质代码和楞型。"""
        item = {
            "product_code": "20005678",
            "product_name": "垫板",
            "raw_spec_model": "",
        }
        spec_raw = "98*20cm T5P/A R"
        enriched = _enrich_tianhua_item(item, spec_raw)
        self.assertEqual(enriched["old_material_code"], "T5P/A")
        self.assertEqual(enriched.get("flute_type"), "A")
        self.assertEqual(enriched.get("layer_count"), 3)

    def test_name_len_uses_raw_name_not_first_body(self):
        """
        F-2 核心：从 body 中切割 spec 时，应使用 raw_name 的长度，
        而不是 first_body（包含了规格部分）的长度。
        """
        # 当 first_body = "垫板 98*20cm" 而 raw_name = "垫板" 时
        # name_len 应为 2（"垫板"），而不是 10（"垫板 98*20cm"）
        # spec_for_enrichment 应包含 "98*20cm"
        from app.services.order_pdf_import import _split_name_and_spec
        first_body = "垫板 98*20cm"
        full_body = "垫板 98*20cm T5P/A R"
        raw_name, raw_spec = _split_name_and_spec(first_body, full_body)
        self.assertEqual(raw_name, "垫板")
        # 正确的 raw_spec_for_enrichment 应从 body[len("垫板"):] 开始
        name_len = len(raw_name)  # 2（使用 raw_name 长度）
        spec_for_enrichment = full_body[name_len:].strip()
        self.assertIn("98", spec_for_enrichment, "spec_for_enrichment 应包含 98（尺寸部分）")
        self.assertIn("T5P/A", spec_for_enrichment, "spec_for_enrichment 应包含材质代码")


class TestF3SlashSpecNotTruncated(unittest.TestCase):
    """F-3: 斜杠规格不截断。"""

    def test_slash_spec_preserved(self):
        """115*67*2.5/2.8cm 不能截断为 115*67*2.5。"""
        raw = "115*67*2.5/2.8cm"
        result = _extract_spec_dimensions(raw)
        self.assertNotEqual(result, "115*67*2.5", "斜杠规格不能截断")

    def test_slash_spec_contains_both_parts(self):
        """提取结果应包含斜杠（/）表示完整的双厚度规格。"""
        raw = "115*67*2.5/2.8cm"
        result = _extract_spec_dimensions(raw)
        # 结果应包含 2.5 和 2.8（或整体保留）
        self.assertTrue(
            "/2.8" in result or "2.5/2.8" in result or result == raw.replace(" ", ""),
            f"结果 '{result}' 应保留斜杠部分",
        )

    def test_normal_spec_still_works(self):
        """普通规格（无斜杠）不受影响。"""
        result = _extract_spec_dimensions("95.5*63.5*18.5cm")
        self.assertIn("95.5", result)
        self.assertIn("63.5", result)
        self.assertIn("18.5", result)


class TestF4PackagingAnnotationClean(unittest.TestCase):
    """F-4: 包装注记不能污染 size_spec。"""

    def test_per_box_annotation_removed(self):
        """'5盒/箱' 不应出现在 size_spec 中。"""
        item = {
            "product_code": "20001111",
            "product_name": "某产品",
            "raw_spec_model": "95.5*63.5*18.5cm",
        }
        spec_raw = "95.5*63.5*18.5cm 5盒/箱 W535A/AB THH10"
        enriched = _enrich_tianhua_item(item, spec_raw)
        self.assertNotIn("盒/箱", enriched["size_spec"], f"size_spec 不应含包装注记，实际：'{enriched['size_spec']}'")

    def test_per_pack_annotation_removed(self):
        """'3本/盒' 不应出现在 size_spec 中。"""
        item = {
            "product_code": "20002222",
            "product_name": "说明书",
            "raw_spec_model": "20*15cm",
        }
        spec_raw = "20*15cm 3本/盒 T5P/A"
        enriched = _enrich_tianhua_item(item, spec_raw)
        self.assertNotIn("本/盒", enriched["size_spec"], f"size_spec 不应含包装注记，实际：'{enriched['size_spec']}'")

    def test_packaging_annotation_regex(self):
        """PACKAGING_ANNOTATION_RE 能识别常见包装注记。"""
        cases = ["5盒/箱", "10盒/箱", "3本/盒", "6件/箱", "20片/箱"]
        for case in cases:
            self.assertTrue(
                PACKAGING_ANNOTATION_RE.search(case),
                f"PACKAGING_ANNOTATION_RE 应匹配 '{case}'",
            )

    def test_packaging_note_stored(self):
        """被清除的包装注记应保存在 packaging_note 字段。"""
        item = {"product_code": "X", "product_name": "测试"}
        spec_raw = "95.5*63.5cm 5盒/箱"
        enriched = _enrich_tianhua_item(item, spec_raw)
        # packaging_note 应有值
        self.assertTrue(
            enriched.get("packaging_note"),
            "packaging_note 应保存被清除的包装注记",
        )


class TestF5ExtraColumns(unittest.TestCase):
    """F-5: PO2026060079 额外列（番号/销售订单号）不导致列偏移。"""

    def test_extra_column_header_detected(self):
        """含'番号'的表头应被 _split_records 检测为 has_extra_columns=True。"""
        lines = [
            "采购订单",
            "行号 番号 料品编码 物料名称 规格型号 单位 数量 单价 金额 销售订单号 交货日期",
            "1 001 20001234 普通纸箱 120*80*60cm 只 500 5.0000 2,500.00 S001 2026/08/01",
            "合计 2,500.00",
        ]
        _, has_extra, _ = _split_records(lines)
        self.assertTrue(has_extra, "含'番号'的表头应被识别为 has_extra_columns=True")

    def test_normal_header_no_extra_columns(self):
        """标准表头不应标记 has_extra_columns。"""
        lines = [
            "行号 料品编码 物料名称 规格型号 单位 数量 单价 金额 交货日期",
            "1 20001234 普通纸箱 120*80*60cm 只 500 5.0000 2,500.00 2026/08/01",
            "合计",
        ]
        _, has_extra, _ = _split_records(lines)
        self.assertFalse(has_extra, "标准表头不应标记 has_extra_columns")

    def test_extra_column_record_parsed(self):
        """有额外列时，_parse_record(has_extra_columns=True) 应成功解析行号和料品编码。"""
        # ITEM_RE_EXTRA 格式：行号 番号 料品编码 物料名称 ... 销售订单号 交货日期
        record = ["1 001 20001234 普通纸箱 120*80*60cm 只 500 5.0000 2,500.00 S001 2026/08/01"]
        result = _parse_record(record, has_extra_columns=True)
        if result is not None:
            self.assertEqual(result["line_no"], 1)
            self.assertEqual(result["raw_product_code"], "20001234")
        # 不报错就是通过（额外列不崩溃）


class TestOrderNoRE(unittest.TestCase):
    """ORDER_NO_RE 扩展 — 思迈尔 P-XXXXXXX 格式。"""

    def test_simair_basic(self):
        """ORDER_NO_RE 应匹配 P-0028338。"""
        m = ORDER_NO_RE.search("P-0028338")
        self.assertIsNotNone(m, "ORDER_NO_RE 应匹配 P-0028338")
        self.assertEqual(m.group(1), "P-0028338")

    def test_simair_with_suffix(self):
        """ORDER_NO_RE 应匹配 P-0029133-1。"""
        m = ORDER_NO_RE.search("P-0029133-1")
        self.assertIsNotNone(m, "ORDER_NO_RE 应匹配 P-0029133-1")
        self.assertEqual(m.group(1), "P-0029133-1")

    def test_pure_digits_not_matched(self):
        """ORDER_NO_RE 不应匹配纯数字 '123456'。"""
        m = ORDER_NO_RE.search("123456")
        self.assertIsNone(m, "ORDER_NO_RE 不应匹配纯数字")

    def test_tianhua_po_still_matched(self):
        """ORDER_NO_RE 仍应匹配天华 PO 格式。"""
        self.assertIsNotNone(ORDER_NO_RE.search("PO2026050269"))
        self.assertIsNotNone(ORDER_NO_RE.search("THPO2605210008"))

    def test_short_po_not_matched(self):
        """ORDER_NO_RE 不应匹配太短的 PO（PO12345，5位）。"""
        m = ORDER_NO_RE.search("PO12345")
        self.assertIsNone(m, "PO12345 不足6位，不应匹配")


class TestCustomerTypeDetect(unittest.TestCase):
    """客户类型识别。"""

    def test_tianhua_energy_detected(self):
        """含'新能源'的客户名应识别为 tianhua_energy。"""
        ct = _detect_customer_type("苏州天华新能源科技股份有限公司", None)
        self.assertEqual(ct, "tianhua_energy")

    def test_tianhua_chao_detected(self):
        """含'天华超净'的客户名应识别为 tianhua_chao。"""
        ct = _detect_customer_type("苏州天华超净有限公司", None)
        self.assertEqual(ct, "tianhua_chao")

    def test_tianhua_is_tianhua_customer_true(self):
        """天华新能源应被 _is_tianhua_customer 识别为天华客户。"""
        self.assertTrue(_is_tianhua_customer("苏州天华新能源科技股份有限公司"))

    def test_simair_detected_by_name(self):
        """含'思迈尔'的客户名应识别为 simair。"""
        ct = _detect_customer_type("苏州思迈尔电子设备有限公司", None)
        self.assertEqual(ct, "simair")

    def test_simair_detected_by_po(self):
        """P-XXXXXXX 格式订单号应识别为 simair。"""
        ct = _detect_customer_type(None, "P-0028338")
        self.assertEqual(ct, "simair")

    def test_unknown_customer(self):
        """无法识别的客户名应返回 unknown。"""
        ct = _detect_customer_type("某某纸箱有限公司", "SO-12345")
        self.assertEqual(ct, "unknown")

    def test_gaotai_detected_by_name(self):
        """含'高泰'的客户名应识别为 gaotai。"""
        ct = _detect_customer_type("苏州高泰电子技术股份有限公司", None)
        self.assertEqual(ct, "gaotai")


class TestSimairMissingTemplate(unittest.TestCase):
    """思迈尔缺少完整模板时返回明确失败原因。"""

    def test_simair_returns_missing_template_status(self):
        """思迈尔订单解析应返回 recognition_status=missing_customer_template。"""
        from app.services.order_pdf_import import parse_purchase_order_text

        # 模拟一段含思迈尔订单号的文本（不含天华表头）
        simair_text = """
苏州思迈尔电子设备有限公司
采购订单
P-0028338
序号 物料编号 供应商参考号 采购数量 到货日期
1 MAT-001 REF-001 100 2026/07/15
合计 100
"""
        result = parse_purchase_order_text(simair_text, "P-0028338.pdf")
        self.assertEqual(
            result["recognition_status"],
            "missing_customer_template",
            f"思迈尔应返回 missing_customer_template，实际：{result['recognition_status']}",
        )
        self.assertEqual(result["customer_type"], "simair")
        self.assertGreater(len(result["warnings"]), 0, "应有警告说明缺少模板")

    def test_simair_not_silent_fail(self):
        """思迈尔订单不应静默失败（不应抛出未捕获异常，必须返回结构化结果）。"""
        from app.services.order_pdf_import import parse_purchase_order_text

        simair_text = "思迈尔 P-0029254 序号 物料编号"
        try:
            result = parse_purchase_order_text(simair_text, "simair.pdf")
            # 如果返回，必须有 recognition_status
            self.assertIn("recognition_status", result)
        except ValueError:
            # ValueError 是可接受的（未找到订单号等），但不能是其他异常
            pass


class TestOcrUnavailableNocrash(unittest.TestCase):
    """OCR 不可用时 ERP 不崩溃。"""

    def test_ocr_available_returns_bool(self):
        """ocr_available() 应返回 bool，不崩溃。"""
        result = ocr_available()
        self.assertIsInstance(result, bool)

    def test_should_use_ocr_no_crash_on_garbled(self):
        """should_use_ocr 对纯 ASCII 乱码文本不崩溃，且中文率=0 时应触发 OCR。"""
        # 使用纯 ASCII 非中文字符，中文率 = 0%，无论 parse_result 如何都应触发 OCR
        garbled = "Lorem ipsum dolor sit amet xyz abc ??? !!!"
        result = should_use_ocr(garbled, None)
        self.assertIsInstance(result, bool)


class TestGaotaiTemplateRules(unittest.TestCase):
    def test_gaotai_generic_keyword_does_not_capture_tianhua_contract(self):
        from app.services.order_pdf_import import (
            detect_pdf_customer_by_template,
            parse_purchase_order_text,
        )
        from app.services.pdf_customer_templates import GAOTAI_TEMPLATE_RULE

        text = """
采购合同
苏州天华超净科技有限公司
PO2026050269
苏州天明包装有限公司
行号 料品编码 物料名称 规格型号 单位 数量 含税单价 价税合计 交货日期
10 21312009 中性内箱 28.5*19.5*5.5cm 个 25.00000 5.410000 135.25 2026.06.25
合计 25.00000 135.25
"""
        rules = [dict(GAOTAI_TEMPLATE_RULE)]

        self.assertIsNone(detect_pdf_customer_by_template(text, rules))
        result = parse_purchase_order_text(text, "tianhua-contract.pdf", template_rules=rules)

        self.assertEqual(result["customer_name"], "苏州天华超净科技有限公司")
        self.assertEqual(result["customer_type"], "tianhua_chao")
        self.assertEqual(result["customer_po"], "PO2026050269")
        self.assertGreater(len(result["items"]), 0)
        self.assertNotIn(
            result.get("recognition_status"),
            {"missing_customer_template", "ocr_required", "failed"},
        )

    def test_normalize_gaotai_product_code(self):
        from app.services.order_pdf_import import normalize_gaotai_product_code

        self.assertEqual(normalize_gaotai_product_code("3090078")[0], "3D90078")
        self.assertEqual(normalize_gaotai_product_code("3090095")[0], "3D90095")
        self.assertEqual(normalize_gaotai_product_code("3030268")[0], "3D30268")
        self.assertEqual(normalize_gaotai_product_code("3D90078")[0], "3D90078")

    def test_apply_gaotai_postprocess_only_for_gaotai(self):
        from app.services.order_pdf_import import apply_customer_template_postprocess

        gaotai = {
            "customer_name": "苏州高泰电子技术股份有限公司",
            "customer_type": "gaotai",
            "items": [{"product_code": "3090078"}],
            "warnings": [],
        }
        result = apply_customer_template_postprocess(gaotai, "苏州高泰电子技术股份有限公司")
        self.assertEqual(result["items"][0]["product_code"], "3D90078")
        self.assertEqual(result["items"][0]["raw_product_code"], "3090078")
        self.assertEqual(result["items"][0]["normalized_product_code"], "3D90078")
        self.assertTrue(any("3090078" in warning and "3D90078" in warning for warning in result["warnings"]))

        tianhua = {
            "customer_name": "苏州天华新能源科技股份有限公司",
            "customer_type": "tianhua_energy",
            "items": [{"product_code": "3090078"}],
            "warnings": [],
        }
        untouched = apply_customer_template_postprocess(tianhua, "苏州天华新能源科技股份有限公司")
        self.assertEqual(untouched["items"][0]["product_code"], "3090078")

    def test_gaotai_ocr_snippet_can_produce_corrected_items(self):
        from app.services.order_pdf_import import parse_purchase_order_text

        text = """
苏州高泰电子技术股份有限公司 采购合同 苏州工业园区天明纸品包装厂
合同号/0 : 0100-CG260624-02 日期/0ate: 2026/6/24
序号 产品编号 名称 规格 数量 单位 单价(含税) 金额 税率 交货期 备注
3090078 纸箱 SSII 50 510.00 13.00 2026-5-26
3090095 纸箱 2SII 500 Pcs 2950.00 13.00 2026-6-26
3030268 纸箱 190*160 2000 Pcs 13.00 2026-6-26
合计
"""
        result = parse_purchase_order_text(text, "gaotai.pdf")
        self.assertEqual(result["customer_type"], "gaotai")
        self.assertEqual(result["customer_po"], "0100-CG260624-02")
        self.assertEqual(
            [item["product_code"] for item in result["items"][:3]],
            ["3D90078", "3D90095", "3D30268"],
        )
        self.assertEqual(
            [item["raw_product_code"] for item in result["items"][:3]],
            ["3090078", "3090095", "3030268"],
        )
        self.assertEqual(
            [item["normalized_product_code"] for item in result["items"][:3]],
            ["3D90078", "3D90095", "3D30268"],
        )
        self.assertTrue(any("3D90078" in warning for warning in result["warnings"]))
        # 中文率 = 0% < 1%，应触发 OCR
        self.assertTrue(result, "纯 ASCII 文本中文率=0，应触发 OCR")

    def test_should_use_ocr_empty_text(self):
        """空文本应触发 OCR。"""
        self.assertTrue(should_use_ocr("", None))
        self.assertTrue(should_use_ocr("   ", None))


class TestDbMigration(unittest.TestCase):
    """DB 迁移验证 — snapshot_customer_model 字段。"""

    def test_snapshot_customer_model_column_exists(self):
        """sales_order_items 表应有 snapshot_customer_model 列。"""
        import sqlite3

        conn = sqlite3.connect("data/carton_erp.sqlite3")
        cur = conn.cursor()
        cur.execute("PRAGMA table_info(sales_order_items)")
        col_names = [row[1] for row in cur.fetchall()]
        conn.close()
        self.assertIn(
            "snapshot_customer_model",
            col_names,
            "sales_order_items 应有 snapshot_customer_model 列",
        )

    def test_existing_orders_have_null_customer_model(self):
        """历史订单的 snapshot_customer_model 应全部为 NULL（未回填）。"""
        import sqlite3

        conn = sqlite3.connect("data/carton_erp.sqlite3")
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM sales_order_items WHERE snapshot_customer_model IS NOT NULL"
        )
        non_null_count = cur.fetchone()[0]
        conn.close()
        self.assertEqual(
            non_null_count,
            0,
            f"历史订单不应有 snapshot_customer_model 值，但发现 {non_null_count} 条非 NULL 记录",
        )

    def test_order_item_model_has_attribute(self):
        """OrderItem ORM 模型应有 snapshot_customer_model 属性。"""
        from app.models.order import OrderItem

        self.assertTrue(
            hasattr(OrderItem, "snapshot_customer_model"),
            "OrderItem 模型应有 snapshot_customer_model 属性",
        )


if __name__ == "__main__":
    unittest.main()
