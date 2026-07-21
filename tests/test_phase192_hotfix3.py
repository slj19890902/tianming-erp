"""tests/test_phase192_hotfix3.py

v0.19.2-A+B Hotfix 第三轮测试套件（下一轮）。

覆盖：
  RPDF: 真实 PDF 多页回归（data/pdf_training_samples/PO2026060675.pdf，5 页）
  STD : 常用箱优先（_apply_standard_product / _material_label）
  KW  : 材质字典 keyword 搜索（list_materials keyword 参数）
  AUD : 新版 xlsx 审计报告产物存在

运行命令:
    python -X utf8 -m pytest tests/test_phase192_hotfix3.py -q
"""
from __future__ import annotations

import os
import sys
from datetime import datetime

import pytest

sys.path.insert(0, ".")

import app.models  # noqa: F401 – registers all tables
from app.models.material import Material
from app.models.product import Product


# ===========================================================================
# 真实 PDF 多页回归（PO2026060675.pdf）
# ===========================================================================

REAL_PDF = r"data/pdf_training_samples/PO2026060675.pdf"


@pytest.mark.skipif(not os.path.exists(REAL_PDF), reason="真实 PDF 样本不存在")
class TestRealPdfRegression:
    """RPDF: 以真实 PDF 建立回归 fixture，固化已验证字段。

    业务背景：该订单为天华超净 5 页采购单，旧版只识别首页 6 条，
    且 170 行品名缺括号、规格 .6cm 被截断。本轮修复后应识别全部 39 条。
    """

    @pytest.fixture(scope="class")
    def parsed(self):
        from app.services.order_pdf_import import (
            extract_text_from_pdf_bytes,
            parse_purchase_order_text,
        )
        data = open(REAL_PDF, "rb").read()
        text = extract_text_from_pdf_bytes(data)
        return parse_purchase_order_text(text)

    @pytest.fixture(scope="class")
    def by_line(self, parsed):
        return {it.get("line_no"): it for it in parsed["items"]}

    def test_five_pages(self):
        import io
        import pypdf
        data = open(REAL_PDF, "rb").read()
        assert len(pypdf.PdfReader(io.BytesIO(data)).pages) == 5

    def test_total_item_count(self, parsed):
        assert len(parsed["items"]) == 39, (
            f"应识别全部 39 条明细（多页修复），实际 {len(parsed['items'])}"
        )

    def test_customer_po_and_delivery(self, parsed):
        assert parsed["customer_po"] == "PO2026060675"
        assert parsed["delivery_date"] == "2026-07-02"
        assert "天华" in (parsed["customer_name"] or "")

    def test_all_pages_lines_present(self, by_line):
        # 行号覆盖第 1~5 页（10~390，步进 10）
        for ln in range(10, 400, 10):
            assert ln in by_line, f"行号 {ln} 缺失（多页/跨页明细未带入）"

    def test_row_170_name_bracket_preserved(self, by_line):
        assert by_line[170]["product_name"] == "白底黑字内箱（18”*36”）"

    def test_row_170_size_not_truncated(self, by_line):
        assert by_line[170]["size_spec"] == "93.5*47*2.3/2.6cm"

    def test_row_170_old_material_be(self, by_line):
        assert by_line[170]["old_material_code"] == "W535A/BE"

    def test_row_170_customer_model(self, by_line):
        assert by_line[170]["customer_model"] == "THH10"

    def test_row_170_production_notes(self, by_line):
        notes = by_line[170].get("production_notes") or ""
        assert "在白色处打钩" in notes
        assert "縦置き厳禁" in notes
        # 跨行抽取的空格已清理
        assert "在白色处 打钩" not in notes

    def test_row_200_size_not_truncated(self, by_line):
        assert by_line[200]["size_spec"] == "93.5*47*2.3/2.6cm"

    def test_row_350_size(self, by_line):
        assert by_line[350]["size_spec"] == "93.5*62*2.3/2.6cm"

    def test_row_370_size(self, by_line):
        assert by_line[370]["size_spec"] == "93.5*62*2.3/2.6cm"

    def test_row_30_size(self, by_line):
        assert by_line[30]["size_spec"] == "115*67*2.5/2.8cm"

    def test_row_10_old_material_with_leading_digit(self, by_line):
        # 旧正则要求字母开头，9CCC9/AB 会丢失；本轮放宽允许数字开头但要求含字母
        assert by_line[10]["old_material_code"] == "9CCC9/AB"

    def test_row_10_layer_flute_from_material_code(self, by_line):
        assert by_line[10]["flute_type"] == "AB"
        assert by_line[10]["layer_count"] == 5

    def test_row_10_production_notes(self, by_line):
        assert "黑色印刷" in (by_line[10].get("production_notes") or "")

    def test_packaging_qty_not_in_notes_or_spec(self, by_line):
        # 包装数（盒/箱）不得混入生产说明或规格
        for it in by_line.values():
            assert "盒/箱" not in (it.get("size_spec") or "")
            assert "盒/箱" not in (it.get("production_notes") or "")

    def test_per_page_recognition(self, by_line):
        # 每页 10 条（行号 ÷ 10 落在 1..39），共 39 行号互不重复
        assert len(by_line) == 39
        assert max(by_line) == 390 and min(by_line) == 10


# ===========================================================================
# 常用箱优先（_apply_standard_product / _material_label）
# ===========================================================================

class TestStandardProductPriority:
    """STD: 唯一命中常用箱后，标准字段以常用箱为准，订单事实字段保留 PDF。"""

    def _product(self):
        mat = Material(
            code="D4B",
            supplier_name="苏州嘉林亿",
            layer_count=3,
            flute_type="B",
            basis_weight_description="130g/100g/100g",
            quote_price=1.21,
        )
        prod = Product(
            product_code="P-STD-1",
            product_name="标准内箱",
            length_mm=300,
            width_mm=200,
            height_mm=150,
            production_process="标准工艺",
            print_content="标准印刷",
        )
        prod.material = mat
        prod.material_id = 99
        return prod, mat

    def test_standard_fields_override_pdf(self):
        from app.services.order_pdf_import import _apply_standard_product
        prod, mat = self._product()
        item = {
            "product_name": "PDF识别内箱",
            "size_spec": "999*999*999cm",
            "old_material_code": "X9X/AB",
            "quantity": 500,
            "unit_price": "1.88",
            "customer_po": "PO123",
        }
        _apply_standard_product(item, prod)
        # 标准字段被常用箱覆盖
        assert item["product_name"] == "标准内箱"
        assert item["size_spec"] == "300×200×150mm"
        assert item["matched_material_id"] == 99
        assert item["flute_type"] == "B"
        assert item["layer_count"] == 3
        assert item["material_supplier_name"] == "苏州嘉林亿"
        assert item["material_weight_structure"] == "130/100/100"
        # 订单事实字段保留 PDF
        assert item["quantity"] == 500
        assert item["unit_price"] == "1.88"
        assert item["customer_po"] == "PO123"

    def test_standard_match_comparison_info(self):
        from app.services.order_pdf_import import _apply_standard_product
        prod, mat = self._product()
        item = {
            "product_name": "PDF识别内箱",
            "size_spec": "999*999*999cm",
            "old_material_code": "X9X/AB",
        }
        _apply_standard_product(item, prod)
        sm = item["standard_match"]
        assert sm["matched"] is True
        assert sm["product_id"] == prod.id
        assert sm["size_differs"] is True  # PDF 与常用箱尺寸不同
        assert sm["pdf_material_code"] == "X9X/AB"
        assert "D4B" in sm["standard_material_label"]
        assert item["product_manual_modified"] is False
        assert sm["manual_modified"] is False

    def test_standard_match_exposes_common_box_manual_modified_state(self):
        from app.services.order_pdf_import import _apply_standard_product
        prod, _mat = self._product()
        prod.manual_modified = True
        prod.manual_modified_at = datetime(2026, 7, 21, 8, 30)
        item = {"product_name": "PDF识别内箱", "size_spec": "300*200*150mm"}
        _apply_standard_product(item, prod)
        assert item["product_manual_modified"] is True
        assert item["product_manual_modified_at"] == "2026-07-21T08:30:00"
        assert item["standard_match"]["manual_modified"] is True

    def test_material_label_format(self):
        from app.services.order_pdf_import import _material_label
        _, mat = self._product()
        label = _material_label(mat)
        assert label == "D4B｜苏州嘉林亿｜130/100/100｜¥1.21"

    def test_material_label_none(self):
        from app.services.order_pdf_import import _material_label
        assert _material_label(None) == ""

    def test_no_reverse_modify_standard(self):
        """覆盖只作用于草稿 item，不改 Product/Material 本身。"""
        from app.services.order_pdf_import import _apply_standard_product
        prod, mat = self._product()
        item = {"product_name": "PDF内箱", "size_spec": "1*1*1cm"}
        _apply_standard_product(item, prod)
        assert prod.product_name == "标准内箱"  # 未被反向修改
        assert mat.quote_price == 1.21


# ===========================================================================
# 材质字典 keyword 搜索（list_materials keyword 参数）
# ===========================================================================

from sqlalchemy.orm import sessionmaker
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.models.user import User
from app.api.deps import get_db, get_current_user
from app.core.database import create_sqlite_engine
from app.core.security import hash_password


def _make_kw_client():
    import pathlib
    import tempfile
    tmp = pathlib.Path(tempfile.mkdtemp()) / "kw_test.sqlite3"
    engine = create_sqlite_engine(tmp)
    from app.models import Base
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(
        bind=engine, autoflush=False, autocommit=False, expire_on_commit=False
    )
    with SessionLocal() as db:
        db.add(User(
            username="admin", password_hash=hash_password("x"), role="admin",
            real_name="管理员", must_change_password=False, is_active=True,
        ))
        db.add_all([
            Material(code="D4B", supplier_name="苏州嘉林亿", layer_count=3,
                     flute_type="B", quote_price=1.21,
                     basis_weight_description="130g/100g/100g"),
            Material(code="A4B", supplier_name="昆山鸣朋", layer_count=3,
                     flute_type="B", quote_price=1.44,
                     basis_weight_description="100g/100g/100g"),
            Material(code="A414B", supplier_name="昆山鸣朋", layer_count=5,
                     flute_type="AB", quote_price=2.15,
                     basis_weight_description="100g/100g/60g/100g/100g"),
            Material(code="X4A", supplier_name="苏州佳丰", layer_count=3,
                     flute_type="A", quote_price=2.11,
                     basis_weight_description="230g进口俄卡"),
        ])
        db.commit()

    app = FastAPI()
    cache = {}

    def odb():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    def ouser():
        if not cache:
            with SessionLocal() as db:
                cache["u"] = db.query(User).filter_by(username="admin").first()
        return cache["u"]

    app.dependency_overrides[get_db] = odb
    app.dependency_overrides[get_current_user] = ouser
    from app.api.materials import router
    app.include_router(router, prefix="/api/master/materials")
    return TestClient(app)


@pytest.fixture(scope="module")
def kw_client():
    return _make_kw_client()


class TestKeywordSearch:
    """KW: keyword 在 supplier+layer+flute 过滤之后再匹配。"""

    def test_keyword_by_code(self, kw_client):
        r = kw_client.get("/api/master/materials?keyword=D4B")
        assert r.status_code == 200
        codes = [i["code"] for i in r.json()["items"]]
        assert "D4B" in codes and "A4B" not in codes

    def test_keyword_partial_code(self, kw_client):
        r = kw_client.get("/api/master/materials?keyword=A41")
        codes = [i["code"] for i in r.json()["items"]]
        assert codes == ["A414B"], f"A41 应只命中 A414B，得 {codes}"

    def test_keyword_by_supplier(self, kw_client):
        r = kw_client.get("/api/master/materials?keyword=鸣朋")
        items = r.json()["items"]
        assert items and all("鸣朋" in i["supplier_name"] for i in items)

    def test_keyword_by_weight(self, kw_client):
        r = kw_client.get("/api/master/materials?keyword=130")
        codes = [i["code"] for i in r.json()["items"]]
        assert "D4B" in codes

    def test_keyword_by_price(self, kw_client):
        r = kw_client.get("/api/master/materials?keyword=2.11")
        codes = [i["code"] for i in r.json()["items"]]
        assert "X4A" in codes

    def test_keyword_with_layer_filter(self, kw_client):
        """keyword 不绕过层数过滤：layer=3 + A4 只剩三层结果。"""
        r = kw_client.get("/api/master/materials?layer_count=3&keyword=A4")
        codes = [i["code"] for i in r.json()["items"]]
        assert "A4B" in codes
        assert "A414B" not in codes, "五层 A414B 不应在 layer=3 结果中"

    def test_keyword_no_match_empty(self, kw_client):
        r = kw_client.get("/api/master/materials?keyword=完全不存在ZZZ")
        assert r.json()["items"] == []

    def test_keyword_paper_type(self, kw_client):
        r = kw_client.get("/api/master/materials?keyword=俄卡")
        codes = [i["code"] for i in r.json()["items"]]
        assert codes == ["X4A"]


# ===========================================================================
# 材质分页加载（page_size<=200 保护，修复 "Input should be <= 200" 422）
# ===========================================================================

class TestMaterialPageSizeGuardFrontend:
    """LIMIT-FE: 前端不再请求 page_size>200。"""

    INDEX = r"static/index.html"

    @pytest.fixture(scope="class")
    def html(self):
        return open(self.INDEX, encoding="utf-8").read()

    def test_no_oversized_page_size(self, html):
        assert "page_size: 1000" not in html
        assert "page_size: 500" not in html
        assert "page_size:1000" not in html
        assert "page_size:500" not in html

    def test_uses_paginated_helper(self, html):
        assert "fetchAllMaterials" in html
        assert "PAGE_SIZE = 200" in html

    def test_loadmaterials_calls_helper(self, html):
        assert "this.fetchAllMaterials(" in html


class TestMaterialPageSizeGuardApi:
    """LIMIT-API: 后端 page_size 上限 200，分页可取全部。"""

    def _client_with_n(self, n):
        import pathlib
        import tempfile
        tmp = pathlib.Path(tempfile.mkdtemp()) / f"lim_{n}.sqlite3"
        engine = create_sqlite_engine(tmp)
        from app.models import Base
        Base.metadata.create_all(engine)
        SessionLocal = sessionmaker(
            bind=engine, autoflush=False, autocommit=False, expire_on_commit=False
        )
        with SessionLocal() as db:
            db.add(User(
                username="admin", password_hash=hash_password("x"), role="admin",
                real_name="管理员", must_change_password=False, is_active=True,
            ))
            db.add_all([
                Material(code=f"M{i:04d}", supplier_name="苏州嘉林亿",
                         layer_count=3, flute_type="B", quote_price=1.0 + i / 100,
                         basis_weight_description="100g/100g/100g")
                for i in range(n)
            ])
            db.commit()
        app = FastAPI()
        cache = {}

        def odb():
            db = SessionLocal()
            try:
                yield db
            finally:
                db.close()

        def ouser():
            if not cache:
                with SessionLocal() as db:
                    cache["u"] = db.query(User).filter_by(username="admin").first()
            return cache["u"]

        app.dependency_overrides[get_db] = odb
        app.dependency_overrides[get_current_user] = ouser
        from app.api.materials import router
        app.include_router(router, prefix="/api/master/materials")
        return TestClient(app)

    def test_page_size_over_200_rejected(self):
        client = self._client_with_n(5)
        r = client.get("/api/master/materials?page_size=1000")
        assert r.status_code == 422, "page_size>200 应被后端拒绝（合理保护，不放宽到 9999）"

    def test_page_size_200_ok(self):
        client = self._client_with_n(5)
        r = client.get("/api/master/materials?page_size=200")
        assert r.status_code == 200

    def test_paginate_over_200_materials(self):
        """材质总数 250 时，按 page_size=200 翻页应取回全部 250 条。"""
        n = 250
        client = self._client_with_n(n)
        collected = []
        page = 1
        while True:
            r = client.get(f"/api/master/materials?page={page}&page_size=200")
            assert r.status_code == 200
            data = r.json()
            items = data["items"]
            collected.extend(items)
            assert len(items) <= 200, "单页不得超过 200"
            if len(items) < 200 or len(collected) >= data["total"]:
                break
            page += 1
        assert data["total"] == n
        assert len(collected) == n
        assert len({c["code"] for c in collected}) == n, "翻页应无重复/缺漏"


# ===========================================================================
# 新版 xlsx 审计报告产物
# ===========================================================================

class TestAuditReportArtifact:
    """AUD: 新版数据集审计报告已生成且含关键结论。"""

    REPORT = r"docs/material_reports/MATERIAL_MASTER_DATASET_REAUDIT_V0192_NEXT.md"

    def test_report_exists(self):
        assert os.path.exists(self.REPORT), "新版审计报告应已生成"

    def test_report_key_conclusions(self):
        content = open(self.REPORT, encoding="utf-8").read()
        assert "昆山鸣朋" in content
        assert "苏州佳丰" in content
        assert "苏州嘉林亿" in content
        assert "七层" in content  # 无七层结论
        assert "俄卡" in content  # 特殊材质识别
        assert "379" in content  # 合计材质数
