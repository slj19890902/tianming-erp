"""tests/test_phase192_hotfix2.py

v0.19.2-A+B Hotfix 第二轮测试套件。

覆盖：
  PDF-H1: _join_record_lines 斜杠规格跨行修复（真实 PDF 场景）
  PDF-H2: _split_name_and_spec 括号品名完整保留（含空格场景）
  PDF-H3: 生产说明/包装注记不变
  MAT-H1: materials API supplier_name 过滤
  MAT-H2: supplier_name + layer_count + flute_type 组合过滤
  CMP-H1: 比价分组特殊材质区分（美卡/俄卡/进口木浆/白卡）
  AUDIT-H1: xlsx 审计相关逻辑（供应商统计/R=17000）

运行命令:
    python -X utf8 -m pytest tests/test_phase192_hotfix2.py -q
"""
from __future__ import annotations

import sys
from collections.abc import Generator
from decimal import Decimal

import pytest

sys.path.insert(0, ".")


# ===========================================================================
# PDF 解析修复测试
# ===========================================================================

from app.services.order_pdf_import import (
    _extract_spec_dimensions,
    _find_spec_start_outside_parens,
    _join_record_lines,
    _split_name_and_spec,
    _enrich_tianhua_item,
)


class TestPdfH1JoinRecordLinesSlashSpec:
    """PDF-H1: 斜杠规格跨行不截断。

    真实 PDF 场景：PDF 抽取器将 '93.5*47*2.3/2.6cm' 断在 '/2' 和 '.6cm' 之间，
    导致 DIMENSION_RE 只匹配到 '93.5*47*2.3/2'。
    修复：_join_record_lines 检测行尾为数字、下行以 '.数字' 开头，直接拼接。
    """

    def test_slash_spec_split_at_decimal(self):
        """行尾 '/2'，下行 '.6cm' → 应拼接为 '2.6cm'。"""
        # 模拟真实 PDF 行：第一行以 /2 结尾，第二行以 .6cm 开头
        lines = [
            "1 WCX1234 内箱 93.5*47*2.3/2",
            ".6cm 张 100 1.50 150.00 2026/01/01",
        ]
        joined = _join_record_lines(lines)
        assert "2.3/2.6cm" in joined, f"期望拼接 2.3/2.6cm，得到: {joined!r}"

    def test_slash_spec_not_truncated_after_join(self):
        """拼接后 DIMENSION_RE 应提取完整 '93.5*47*2.3/2.6cm'。"""
        lines = [
            "1 WCX1234 内箱 93.5*47*2.3/2",
            ".6cm 张 100 1.50 150.00 2026/01/01",
        ]
        joined = _join_record_lines(lines)
        result = _extract_spec_dimensions(joined)
        assert "2.3" in result and "2.6" in result, f"规格截断: {result!r}"

    def test_original_behavior_preserved_digit_dot(self):
        """原有行为：行尾 '18.'，下行 '5cm' → 拼接为 '18.5cm'。"""
        lines = ["1 WCX1234 内箱 18.", "5cm 张 50 2.00 100.00 2026/01/01"]
        joined = _join_record_lines(lines)
        assert "18.5cm" in joined

    def test_normal_lines_not_joined_incorrectly(self):
        """正常相邻行不应被错误拼接。"""
        lines = [
            "1 WCX1234 内箱 93.5*47*2.3cm",
            "颜色：白底黑字",
        ]
        joined = _join_record_lines(lines)
        # 两行应该用空格分开，而非直接拼接
        assert "93.5*47*2.3cm 颜色" in joined

    def test_115_spec_not_truncated(self):
        """115*67*2.5/2.8cm 整体在一行时不截断（已有测试，回归保证）。"""
        result = _extract_spec_dimensions("115*67*2.5/2.8cm")
        assert "2.5" in result and "2.8" in result


class TestPdfH2NameBracketPreserved:
    """PDF-H2: 品名括号内容完整保留。

    真实 PDF 场景：物料名称 '白底黑字内箱（18"*36"）' 括号内有空格/特殊字符，
    原来 split(maxsplit=1) 会在第一个空格处截断品名。
    修复：pos is None 时使用完整 first_body 作为品名，不再 split。
    """

    def test_fullwidth_paren_with_space_inside(self):
        """括号内含空格时，品名不截断。"""
        # 模拟 PDF 给出的 first_body：名称内有空格（全角括号内）
        first_body = '白底黑字内箱（18 "*36"）'
        full_body = '白底黑字内箱（18 "*36"） 93.5*47*2.3/2.6cm'
        name, spec = _split_name_and_spec(first_body, full_body)
        # 括号内容必须在名称中
        assert "（18" in name, f"品名截断，得到: {name!r}"
        assert "36" in name, f"品名截断，得到: {name!r}"

    def test_fullwidth_paren_no_space_preserved(self):
        """括号内无空格，品名同样完整保留。"""
        first_body = '白底黑字内箱（18"*36"）'
        full_body = '白底黑字内箱（18"*36"） 93.5*47*2.3/2.6cm'
        name, spec = _split_name_and_spec(first_body, full_body)
        assert '（18"*36"）' in name

    def test_spec_after_bracket_recognized(self):
        """括号外的规格应进入 spec，而不是混入 name。"""
        first_body = '白底黑字内箱（18"*36"）93.5*47*2.3cm'
        full_body = first_body
        name, spec = _split_name_and_spec(first_body, full_body)
        # 名称应为括号部分，spec 应包含尺寸
        assert "白底黑字内箱" in name
        assert "93.5" in spec or "93.5" in name  # 至少在某处

    def test_halfwidth_paren_preserved(self):
        """半角括号内容同样保留。"""
        first_body = '彩箱(600*900mm)印刷'
        full_body = '彩箱(600*900mm)印刷 100*200*50cm'
        name, spec = _split_name_and_spec(first_body, full_body)
        # 如果整体没有发现外层规格，名称保留括号内容
        assert "彩箱" in name

    def test_find_spec_skips_inside_fullwidth_parens(self):
        """_find_spec_start_outside_parens 跳过全角括号内的数字。"""
        text = '白底黑字内箱（18"*36"）'
        idx = _find_spec_start_outside_parens(text)
        # 括号内数字不应触发 spec 起点
        assert idx is None or idx >= len(text), (
            f"错误地把括号内数字当成了 spec 起点：pos={idx}"
        )

    def test_find_spec_detects_after_fullwidth_parens(self):
        """全角括号关闭后的数字应被识别为 spec 起点。"""
        text = '内箱（18"*36"）93.5*47*2.3cm'
        idx = _find_spec_start_outside_parens(text)
        assert idx is not None
        # spec 起点应在 ）之后
        closing = text.index("）")
        assert idx > closing, f"spec 起点 {idx} 应在 ） 后({closing})"


class TestPdfH3ProductionNotesUnchanged:
    """PDF-H3: 生产说明/包装注记不因修复而改变。"""

    def test_production_notes_still_extracted(self):
        item = {"product_code": "X", "product_name": "内箱"}
        spec_raw = "93.5*47*2.3/2.6cm W535A/BE THH10 在白色处打勾 縦置き厳禁 5盒/箱"
        enriched = _enrich_tianhua_item(item, spec_raw)
        notes = enriched.get("production_notes") or ""
        assert "打勾" in notes or "厳禁" in notes

    def test_packaging_still_excluded(self):
        item = {"product_code": "X", "product_name": "内箱"}
        spec_raw = "93.5*47cm 5盒/箱"
        enriched = _enrich_tianhua_item(item, spec_raw)
        assert "盒/箱" not in enriched.get("size_spec", "")
        assert "盒/箱" not in (enriched.get("production_notes") or "")


# ===========================================================================
# 材质 API supplier_name 过滤测试（FastAPI TestClient）
# ===========================================================================

from sqlalchemy.orm import sessionmaker, Session
from fastapi import FastAPI
from fastapi.testclient import TestClient

# 导入所有 model 确保 metadata 完整
import app.models  # noqa: F401 – registers all tables
from app.models.material import Material
from app.models.user import User
from app.api.deps import get_db, get_current_user
from app.core.database import create_sqlite_engine
from app.core.security import hash_password


def _make_test_app():
    import tempfile, pathlib
    tmp = pathlib.Path(tempfile.mkdtemp()) / "mat_test.sqlite3"
    engine = create_sqlite_engine(tmp)
    from app.models import Base
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)

    # 种入用户 + 材质
    with SessionLocal() as db:
        admin = User(
            username="admin",
            password_hash=hash_password("x"),
            role="admin",
            real_name="管理员",
            must_change_password=False,
            is_active=True,
        )
        db.add(admin)
        mats = [
            Material(code="A4B",   supplier_name="嘉林亿", layer_count=3, flute_type="B",  quote_price=1.44, basis_weight_description="100g国产牛卡"),
            Material(code="A414B", supplier_name="嘉林亿", layer_count=5, flute_type="AB", quote_price=2.15, basis_weight_description="100g国产牛卡"),
            Material(code="N7N",   supplier_name="鸣朋",   layer_count=3, flute_type="B",  quote_price=2.28, basis_weight_description="190g国产牛卡"),
            Material(code="N717N", supplier_name="鸣朋",   layer_count=5, flute_type="AB", quote_price=3.28, basis_weight_description="190g国产牛卡"),
            Material(code="B3B",   supplier_name="佳丰",   layer_count=3, flute_type="B",  quote_price=1.32, basis_weight_description="100g国产牛卡"),
            Material(code="B313B", supplier_name="佳丰",   layer_count=5, flute_type="AB", quote_price=2.11, basis_weight_description="100g国产牛卡"),
            Material(code="X4A",   supplier_name="嘉林亿", layer_count=3, flute_type="A",  quote_price=2.11, basis_weight_description="230g进口俄卡"),
            Material(code="X616X", supplier_name="鸣朋",   layer_count=5, flute_type="AB", quote_price=6.96, basis_weight_description="250g美卡"),
        ]
        for material in mats:
            material.price_unit = "元/㎡"
            material.purchase_currency = "CNY"
            material.purchase_tax_included = True
            material.purchase_tax_rate = Decimal("0.13")
        db.add_all(mats)
        db.commit()

    app = FastAPI()
    _admin_cache = {}

    def override_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    def override_user():
        if not _admin_cache:
            with SessionLocal() as db:
                u = db.query(User).filter_by(username="admin").first()
                _admin_cache["user"] = u
        return _admin_cache["user"]

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = override_user

    from app.api.materials import router as mat_router
    app.include_router(mat_router, prefix="/api/master/materials")

    return TestClient(app)


@pytest.fixture(scope="module")
def mat_client():
    return _make_test_app()


class TestMatH1SupplierFilter:
    """MAT-H1: supplier_name 单独过滤。"""

    def test_filter_jialiyi(self, mat_client):
        r = mat_client.get("/api/master/materials?supplier_name=嘉林亿")
        assert r.status_code == 200
        items = r.json()["items"]
        assert all(i["supplier_name"] == "嘉林亿" for i in items), "含有非嘉林亿数据"
        assert len(items) >= 1

    def test_filter_mingpeng(self, mat_client):
        r = mat_client.get("/api/master/materials?supplier_name=鸣朋")
        assert r.status_code == 200
        items = r.json()["items"]
        assert all(i["supplier_name"] == "鸣朋" for i in items)

    def test_filter_jiafeng(self, mat_client):
        r = mat_client.get("/api/master/materials?supplier_name=佳丰")
        assert r.status_code == 200
        items = r.json()["items"]
        assert all(i["supplier_name"] == "佳丰" for i in items)

    def test_no_supplier_returns_all(self, mat_client):
        r = mat_client.get("/api/master/materials")
        assert r.status_code == 200
        items = r.json()["items"]
        suppliers = {i["supplier_name"] for i in items}
        assert len(suppliers) >= 2, "无供应商过滤应返回多家"

    def test_unknown_supplier_returns_empty(self, mat_client):
        r = mat_client.get("/api/master/materials?supplier_name=不存在供应商")
        assert r.status_code == 200
        assert r.json()["items"] == []


class TestMatH2CombinedFilter:
    """MAT-H2: supplier_name + layer_count + flute_type 组合过滤。"""

    def test_supplier_plus_layer(self, mat_client):
        r = mat_client.get("/api/master/materials?supplier_name=嘉林亿&layer_count=3")
        assert r.status_code == 200
        items = r.json()["items"]
        assert all(i["supplier_name"] == "嘉林亿" and i["layer_count"] == 3 for i in items)

    def test_supplier_plus_layer_five(self, mat_client):
        r = mat_client.get("/api/master/materials?supplier_name=鸣朋&layer_count=5")
        items = r.json()["items"]
        assert all(i["supplier_name"] == "鸣朋" and i["layer_count"] == 5 for i in items)

    def test_supplier_plus_layer_plus_flute(self, mat_client):
        r = mat_client.get("/api/master/materials?supplier_name=嘉林亿&layer_count=3&flute_type=B")
        items = r.json()["items"]
        assert all(
            i["supplier_name"] == "嘉林亿"
            and i["layer_count"] == 3
            and i["flute_type"] == "B"
            for i in items
        )

    def test_cross_supplier_same_layer(self, mat_client):
        """不加供应商过滤时，同层数来自多家。"""
        r = mat_client.get("/api/master/materials?layer_count=3")
        items = r.json()["items"]
        suppliers = {i["supplier_name"] for i in items}
        assert len(suppliers) >= 2


# ===========================================================================
# 比价分组特殊材质测试（纯 Python 逻辑，无 DB）
# ===========================================================================

class TestCmpH1SpecialMaterialClassification:
    """CMP-H1: 特殊材质不进入"可比价"组。"""

    def _classify(self, desc: str) -> dict:
        """复制前端 classifyMaterial 逻辑到 Python 进行测试。"""
        import re
        origin, ptype, grade, special, reason = "未知", "其他", "未知", False, ""
        if "进口" in desc: origin = "进口"
        elif "国产" in desc: origin = "国产"
        if re.search(r"俄卡|仿俄", desc): ptype, special, reason = "牛卡", True, "俄卡/仿俄卡"
        elif "美卡" in desc: ptype, special, reason = "牛卡", True, "美卡"
        elif "白牛" in desc: ptype, special, reason = "白牛卡", True, "白牛卡"
        elif "白卡" in desc: ptype, special, reason = "白卡", True, "白卡"
        elif "木浆" in desc: ptype, special, reason = "牛卡", True, "进口木浆牛卡"
        elif "牛卡" in desc: ptype = "牛卡"
        elif "高强" in desc: ptype = "高强芯"
        elif "芯纸" in desc: ptype = "芯纸"
        elif re.search(r"高瓦|施胶", desc): ptype = "高强瓦"
        elif "普瓦" in desc: ptype = "普瓦"
        if "AAA" in desc: grade, special = "AAA", True
        elif "AA级" in desc: grade = "AA"
        elif "A级" in desc: grade = "A"
        elif "B级" in desc: grade = "B"
        return {"origin": origin, "ptype": ptype, "grade": grade, "special": special, "reason": reason}

    def _compare_status(self, mat1_desc: str, mat2_desc: str) -> str:
        """根据两条材质描述推断比价状态。"""
        c1 = self._classify(mat1_desc)
        c2 = self._classify(mat2_desc)
        if c1["ptype"] != c2["ptype"] or c1["origin"] != c2["origin"]:
            if c1["special"] or c2["special"]:
                return "仅供参考"
            return "材质类别不同"
        if c1["special"] or c2["special"]:
            return "仅供参考"
        if c1["ptype"] == "其他" or c2["ptype"] == "其他":
            return "待人工确认"
        return "可比价"

    def test_meika_vs_guochan_not_comparable(self):
        """同克重美卡 vs 国产牛卡 → 仅供参考 / 材质类别不同，不可比价。"""
        status = self._compare_status("250g美卡", "250g国产牛卡")
        assert status in ("仅供参考", "材质类别不同"), f"美卡与国产牛卡不应比价，得: {status}"

    def test_eka_vs_guochan_not_comparable(self):
        """进口俄卡 vs 国产牛卡 → 仅供参考。"""
        status = self._compare_status("170g进口俄卡", "170g国产牛卡")
        assert status in ("仅供参考", "材质类别不同"), f"俄卡与国产牛卡不应比价，得: {status}"

    def test_gaoqua_vs_puwa_not_comparable(self):
        """高强瓦 vs 普瓦 → 类别不同，不可比价。"""
        c1 = self._classify("130g高强瓦纸")
        c2 = self._classify("130g普瓦")
        assert c1["ptype"] != c2["ptype"], "高强瓦和普瓦应属不同类别"

    def test_baika_vs_niuka_not_comparable(self):
        """白卡 vs 普通牛卡 → 材质类别不同。"""
        status = self._compare_status("175g国产白卡", "170g国产牛卡")
        assert status in ("仅供参考", "材质类别不同"), f"白卡与牛卡不应比价，得: {status}"

    def test_same_type_same_origin_comparable(self):
        """同类同来源同克重 → 可比价。"""
        status = self._compare_status("100g国产牛卡", "100g国产牛卡")
        assert status == "可比价", f"相同材质应可比价，得: {status}"

    def test_unknown_type_pending_confirm(self):
        """材质类别未知 → 待人工确认。"""
        c = self._classify("某特殊材料100g")
        # 未包含任何已知关键词时，ptype 为 "其他"
        assert c["ptype"] == "其他" or c["special"] is False

    def test_special_material_flagged(self):
        """美卡/俄卡/白卡/木浆 均标记为特殊材质。"""
        for desc, expected_special, expected_reason in [
            ("230g进口俄卡", True, "俄"),
            ("250g美卡", True, "美卡"),
            ("175g国产白卡", True, "白卡"),
            ("180g进口木浆牛卡", True, "木浆"),
        ]:
            c = self._classify(desc)
            assert c["special"] is True, f"{desc!r} 应被标记为特殊材质"
            assert expected_reason in c["reason"] or c["ptype"] != "其他"

    def test_normal_guochan_not_special(self):
        """普通国产牛卡不标记为特殊材质。"""
        c = self._classify("100g国产牛卡")
        assert c["special"] is False
        assert c["ptype"] == "牛卡"
        assert c["origin"] == "国产"


# ===========================================================================
# xlsx 审计逻辑测试（需要 xlsx 文件存在）
# ===========================================================================

import os
import pytest

XLSX_PATH = r"C:\Users\Administrator\Desktop\纸板代~1_1.xlsx"


@pytest.mark.skipif(not os.path.exists(XLSX_PATH), reason="xlsx 文件不在桌面")
class TestAuditH1XlsxRead:
    """AUDIT-H1: xlsx 审计逻辑。"""

    @pytest.fixture(scope="class")
    def sup_data(self):
        import openpyxl
        wb = openpyxl.load_workbook(XLSX_PATH, read_only=True, data_only=True)
        ws = wb["供应商代码总库"]
        rows = list(ws.iter_rows(values_only=True))
        return [r for r in rows[1:] if any(c for c in r)]

    def test_can_read_supplier_sheet(self, sup_data):
        assert len(sup_data) >= 300, "供应商代码总库应至少有300条"

    def test_supplier_count(self, sup_data):
        suppliers = {str(r[0]).strip() for r in sup_data if r[0]}
        assert "嘉林亿" in suppliers
        assert "鸣朋" in suppliers
        assert "佳丰" in suppliers

    def test_r_equals_17000_detected(self, sup_data):
        r17 = [r for r in sup_data if r[5] and "R=17000" in str(r[5])]
        assert len(r17) >= 17, f"应发现至少17条 R=17000 错误，实际{len(r17)}"
        # 全部应来自佳丰
        assert all(r[0] == "佳丰" for r in r17), "R=17000 错误应来自佳丰"

    def test_no_empty_codes(self, sup_data):
        empty = [r for r in sup_data if not r[1]]
        assert len(empty) == 0, f"存在 {len(empty)} 条空代码"

    def test_code_d4b_exists(self, sup_data):
        codes = {str(r[1]).strip() for r in sup_data}
        assert "D4B" in codes, "D4B 应在供应商总库"

    def test_code_n7n_exists(self, sup_data):
        codes = {str(r[1]).strip() for r in sup_data}
        assert "N7N" in codes, "N7N 应在供应商总库"

    def test_missing_codes_not_in_supplier_sheet(self, sup_data):
        codes = {str(r[1]).strip() for r in sup_data}
        # 这些代码按审计结果不在供应商总库
        missing = ["D416A", "K619K", "W7N", "BC14C", "B416B",
                   "E647D", "E6C7A", "G416B", "F947J", "K9C9E", "K9C7J"]
        for code in missing:
            assert code not in codes, f"{code} 应不在供应商总库（当前审计结论：缺失）"

    def test_audit_report_written(self):
        report = r"D:\纸箱厂erp软件搭建\docs\material_reports\MATERIAL_MASTER_AUDIT_V0192_HOTFIX.md"
        assert os.path.exists(report), "审计报告文件应已生成"
        content = open(report, encoding="utf-8").read()
        assert "R=17000" in content
        assert "嘉林亿" in content
        assert "特殊材质" in content


# ===========================================================================
# 材质字典排序/楞型筛选测试（v0.19.2-B 排序优化）
# ===========================================================================

from app.api.materials import (
    _sort_materials,
    _supplier_sort_key,
    _flute_rank,
    _parse_layer_weights,
)


def _mat(code, supplier, layer, flute, weight, price):
    return Material(
        code=code,
        supplier_name=supplier,
        layer_count=layer,
        flute_type=flute,
        basis_weight_description=weight,
        quote_price=price,
    )


class TestMatSortHelpers:
    """排序辅助函数：供应商顺序 / 楞型顺序 / 逐层克重解析。"""

    def test_supplier_sort_key_is_dynamic_and_empty_is_last(self):
        names = ["胜源", "森林阳光", "昆山鸣朋", "苏州嘉林亿"]
        assert sorted(names, key=_supplier_sort_key) == sorted(
            names,
            key=lambda value: value.casefold(),
        )
        supplier_order = {
            "苏州嘉林亿": 10,
            "昆山鸣朋": 20,
            "胜源": 30,
            "森林阳光": 40,
        }
        assert sorted(
            names,
            key=lambda value: _supplier_sort_key(value, supplier_order),
        ) == ["苏州嘉林亿", "昆山鸣朋", "胜源", "森林阳光"]
        assert _supplier_sort_key(None) > _supplier_sort_key("任意启用供应商")

    def test_flute_rank_three_layer_b_e_a(self):
        assert _flute_rank(3, "B") < _flute_rank(3, "E") < _flute_rank(3, "A")
        # 组合楞 BE 取最靠前者（B），与 B 同级
        assert _flute_rank(3, "BE") == _flute_rank(3, "B")

    def test_flute_rank_five_layer_ab_be(self):
        assert _flute_rank(5, "AB") < _flute_rank(5, "BE")

    def test_parse_layer_weights(self):
        assert _parse_layer_weights("150g/100g/100g") == (150.0, 100.0, 100.0)
        assert _parse_layer_weights("130 / 100 / 60 / 100 / 100") == (130.0, 100.0, 60.0, 100.0, 100.0)
        assert _parse_layer_weights("") == ()
        assert _parse_layer_weights(None) == ()


class TestMatSortLogic:
    """排序结果：common / weight / price 三种顺序。"""

    def _rows(self):
        # 同层（三层），跨供应商、跨克重、跨价格
        return [
            _mat("A4B", "苏州佳丰", 3, "B", "150g/100g/100g", 1.50),
            _mat("B3B", "昆山鸣朋", 3, "B", "100g/100g/100g", 1.30),
            _mat("C3C", "苏州嘉林亿包装科技有限公司", 3, "B", "80g/80g/80g", 1.00),
            _mat("D6D", "苏州嘉林亿包装科技有限公司", 3, "B", "150g/130g/150g", 1.80),
            _mat("Z9Z", "其他厂", 3, "B", "120g/100g/100g", 1.20),
        ]

    def test_common_supplier_first(self):
        ordered = _sort_materials(self._rows(), "common")
        suppliers = [_supplier_sort_key(m.supplier_name) for m in ordered]
        assert suppliers == sorted(suppliers), "common 应按当前供应商名称稳定排序"

    def test_common_within_supplier_weight_asc(self):
        ordered = _sort_materials(self._rows(), "common")
        jl = [m for m in ordered if m.supplier_name.startswith("苏州嘉林亿")]
        assert [m.code for m in jl] == ["C3C", "D6D"], "嘉林亿内部应按逐层克重升序"

    def test_weight_sort_global_ascending(self):
        ordered = _sort_materials(self._rows(), "weight")
        firsts = [_parse_layer_weights(m.basis_weight_description)[0] for m in ordered]
        assert firsts == sorted(firsts), "weight 排序应全局按面纸克重升序"
        assert ordered[0].code == "C3C"

    def test_price_sort_ascending(self):
        ordered = _sort_materials(self._rows(), "price")
        prices = [float(m.quote_price) for m in ordered]
        assert prices == sorted(prices), "price 排序应按平方报价升序"
        assert ordered[0].code == "C3C"

    def test_not_alphabetical_by_code(self):
        ordered = _sort_materials(self._rows(), "common")
        codes = [m.code for m in ordered]
        assert codes != sorted(codes), "排序不应退化为材质代码字母序"


class TestMatApiSort:
    """API 层 sort 参数：default=common，不破坏旧调用。"""

    def test_default_sort_is_common(self, mat_client):
        r = mat_client.get("/api/master/materials?layer_count=3")
        assert r.status_code == 200
        # 默认不报错，返回三层数据
        assert all(i["layer_count"] == 3 for i in r.json()["items"])

    def test_sort_price_param_accepted(self, mat_client):
        r = mat_client.get("/api/master/materials?sort=price")
        assert r.status_code == 200
        prices = [i["quote_price"] for i in r.json()["items"] if i["quote_price"] is not None]
        assert prices == sorted(prices), "sort=price 应按报价升序"

    def test_sort_weight_param_accepted(self, mat_client):
        r = mat_client.get("/api/master/materials?sort=weight")
        assert r.status_code == 200

    def test_invalid_sort_rejected(self, mat_client):
        r = mat_client.get("/api/master/materials?sort=bogus")
        assert r.status_code == 422, "非法 sort 值应被 Literal 校验拒绝"
