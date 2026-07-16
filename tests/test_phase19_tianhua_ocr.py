"""
Phase 19: 天华解析优化 + PDF OCR 支持 测试。

覆盖：
- 括号内容保留（不误把 (24*36) 当规格起始）
- 天华型号提取 (THH10 / THB8 等)
- 旧材质代码解析 + 楞型推断
- display_product_name 拼接
- size_spec 不含旧料号/型号
- pdf_ocr.py 单元测试
- /api/pdf-training/ocr-status 端点
- ocr_text_raw 字段存储与返回
- 扩展 parse_method 约束验证
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

PYTHON = sys.executable
ENCODING = {"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
pytestmark = pytest.mark.usefixtures("isolated_subprocess_database")


def _run(cmd: str) -> subprocess.CompletedProcess:
    env = {**os.environ, **ENCODING}
    return subprocess.run(
        [PYTHON, "-X", "utf8", "-c", cmd],
        capture_output=True, text=True, encoding="utf-8",
        timeout=30, env=env,
        cwd=str(__file__).split("tests")[0].rstrip("/\\"),
    )


def _api(method: str, path: str, **kwargs) -> dict:
    code = f"""
import sys; sys.path.insert(0,'.')
import json
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app, raise_server_exceptions=False)
resp = client.{method}('{path}', {', '.join(f'{k}={v!r}' for k, v in kwargs.items())})
print(json.dumps({{'status': resp.status_code, 'body': resp.text}}))
"""
    result = _run(code)
    if result.returncode != 0:
        raise RuntimeError(result.stderr[:500])
    return json.loads(result.stdout.strip())


def _api_auth(method: str, path: str, payload: dict | None = None) -> dict:
    payload_json = json.dumps(payload) if payload else "None"
    code = f"""
import sys, json
sys.path.insert(0,'.')
from fastapi.testclient import TestClient
from app.main import app
from app.core.database import SessionLocal
from app.models.user import User
from app.core.security import create_session_token
from app.core.config import settings

client = TestClient(app, raise_server_exceptions=False)

with SessionLocal() as db:
    admin = db.query(User).filter(User.role=='admin', User.is_active==True).first()
    if not admin:
        print(json.dumps({{'status': 0, 'body': 'no admin user'}}))
        sys.exit(0)
    token = create_session_token(admin.id)
    cookie_name = settings.session_cookie_name

client.cookies.set(cookie_name, token)
payload = {payload_json}
if payload is not None:
    resp = client.{method}('{path}', json=payload)
else:
    resp = client.{method}('{path}')
print(json.dumps({{'status': resp.status_code, 'body': resp.text}}))
"""
    result = _run(code)
    if result.returncode != 0:
        raise RuntimeError(result.stderr[:500])
    return json.loads(result.stdout.strip())


# ---------------------------------------------------------------------------
# 天华解析单元测试
# ---------------------------------------------------------------------------

class TestTianhuaParserUnit:
    """天华 PDF 解析专项单元测试（order_pdf_import.py）。"""

    def setup_method(self):
        sys.path.insert(0, str(__file__).split("tests")[0])
        from app.services.order_pdf_import import (
            _enrich_tianhua_item,
            _find_spec_start_outside_parens,
            _is_tianhua_customer,
            _parse_old_material_code,
            _split_name_and_spec,
        )
        self._enrich = _enrich_tianhua_item
        self._find_spec = _find_spec_start_outside_parens
        self._is_tianhua = _is_tianhua_customer
        self._parse_mat = _parse_old_material_code
        self._split = _split_name_and_spec

    # --- 括号保留 ---

    def test_bracket_name_preserved_in_split(self):
        """产品名 '白底蓝字外箱(24*36)' 括号内容不被截断。"""
        text = "白底蓝字外箱(24*36) 95.5*63.5*18.5cm"
        name, spec = self._split(text, text)
        assert "(24*36)" in name or "24*36" in name, \
            f"括号内容丢失，name={name!r}"

    def test_spec_start_is_outside_parens(self):
        """paren-depth 追踪：括号外的数字才算规格起始。"""
        text = "白底蓝字外箱(24*36) 95.5*63.5*18.5cm"
        pos = self._find_spec(text)
        assert pos is not None
        bracket_pos = text.index("24")  # 括号内 24 的位置
        assert pos > bracket_pos, \
            f"规格起始位置 {pos} 不应在括号内 '24' 位置（{bracket_pos}）之前"

    def test_no_paren_name_splits_correctly(self):
        """普通名称（无括号）仍能正确拆分规格。"""
        text = "白卡外箱 300*200*150mm"
        name, spec = self._split(text, text)
        assert "白卡外箱" in name
        assert "300" in spec

    # --- 天华型号提取 ---

    def test_extract_thh10_model(self):
        item = {"product_code": "21301055", "product_name": "白底蓝字外箱(24*36)"}
        enriched = self._enrich(item, "95.5*63.5*18.5cm W535A/AB THH10")
        assert enriched["customer_model"] == "THH10"

    def test_extract_thb8_model(self):
        item = {"product_code": "21301060", "product_name": "牛卡外箱"}
        enriched = self._enrich(item, "60*40*30cm T5P/A THB8")
        assert enriched["customer_model"] == "THB8"

    def test_no_th_model_for_liner(self):
        """衬板等配件无 TH 型号，customer_model 为空。"""
        item = {"product_code": "21301099", "product_name": "衬板"}
        enriched = self._enrich(item, "95.5*63.5 W535A/AB")
        assert enriched.get("customer_model", "") == ""

    def test_display_name_with_model(self):
        """display_product_name = product_name + customer_model。"""
        item = {"product_code": "X", "product_name": "白底蓝字外箱(24*36)"}
        enriched = self._enrich(item, "95.5*63.5*18.5cm W535A/AB THH10")
        assert enriched["display_product_name"] == "白底蓝字外箱(24*36) THH10"

    def test_display_name_no_model(self):
        """无型号时 display_product_name 等于 product_name，不多空格。"""
        item = {"product_code": "X", "product_name": "衬板"}
        enriched = self._enrich(item, "95.5*63.5 W535A/AB")
        assert enriched["display_product_name"] == "衬板"

    # --- 旧材质代码 + 楞型推断 ---

    def test_w535a_ab_gives_ab_5layer_white(self):
        r = self._parse_mat("W535A/AB")
        assert r["flute_type"] == "AB"
        assert r["layer_count"] == 5
        assert r.get("surface_paper_type") == "white"

    def test_t5p_a_gives_a_3layer(self):
        r = self._parse_mat("T5P/A")
        assert r["flute_type"] == "A"
        assert r["layer_count"] == 3

    def test_a535t_ab_gives_ab_5layer(self):
        r = self._parse_mat("A535T/AB")
        assert r["flute_type"] == "AB"
        assert r["layer_count"] == 5

    def test_be_suffix_gives_be_5layer(self):
        r = self._parse_mat("W535A/BE")
        assert r["flute_type"] == "BE"
        assert r["layer_count"] == 5

    def test_b_suffix_gives_b_3layer(self):
        r = self._parse_mat("W535B/B")
        assert r["flute_type"] == "B"
        assert r["layer_count"] == 3

    def test_none_code_returns_empty(self):
        assert self._parse_mat(None) == {}

    def test_enrich_captures_old_material_code(self):
        item = {"product_code": "21301055", "product_name": "外箱"}
        enriched = self._enrich(item, "95.5*63.5*18.5cm W535A/AB THH10")
        assert enriched["old_material_code"] == "W535A/AB"

    def test_enrich_flute_from_material_code(self):
        item = {"product_code": "X", "product_name": "外箱"}
        enriched = self._enrich(item, "60*40*30 T5P/A")
        assert enriched.get("flute_type") == "A"
        assert enriched.get("layer_count") == 3

    # --- size_spec 清洁度 ---

    def test_size_spec_excludes_material_code(self):
        item = {"product_code": "X", "product_name": "外箱"}
        enriched = self._enrich(item, "95.5*63.5*18.5cm W535A/AB THH10")
        size_spec = enriched.get("size_spec", "")
        assert "W535A" not in size_spec, \
            f"size_spec 不应含旧料号 W535A: {size_spec!r}"

    def test_size_spec_excludes_th_model(self):
        item = {"product_code": "X", "product_name": "外箱"}
        enriched = self._enrich(item, "95.5*63.5*18.5cm W535A/AB THH10")
        size_spec = enriched.get("size_spec", "")
        assert "THH10" not in size_spec, \
            f"size_spec 不应含天华型号 THH10: {size_spec!r}"

    # --- 天华客户判断 ---

    def test_is_tianhua_by_name(self):
        assert self._is_tianhua("天华超净技术股份有限公司") is True

    def test_is_tianhua_canmax(self):
        assert self._is_tianhua("canmax科技") is True

    def test_is_tianhua_thpo_code(self):
        assert self._is_tianhua("THPO客户") is True

    def test_not_tianhua(self):
        assert self._is_tianhua("普通纸箱客户") is False

    def test_not_tianhua_none(self):
        assert self._is_tianhua(None) is False


# ---------------------------------------------------------------------------
# PDF OCR 单元测试
# ---------------------------------------------------------------------------

class TestPdfOcrUnit:
    """pdf_ocr.py 单元测试（不实际调用 EasyOCR）。"""

    def setup_method(self):
        sys.path.insert(0, str(__file__).split("tests")[0])
        from app.services.pdf_ocr import (
            ocr_available,
            ocr_engine_name,
            should_use_ocr,
        )
        self.ocr_available = ocr_available
        self.ocr_engine_name = ocr_engine_name
        self.should_use_ocr = should_use_ocr

    def test_ocr_available_is_bool(self):
        assert isinstance(self.ocr_available(), bool)

    def test_ocr_engine_name_is_string(self):
        name = self.ocr_engine_name()
        assert isinstance(name, str) and len(name) > 0

    def test_should_use_ocr_empty_string(self):
        assert self.should_use_ocr("", None) is True

    def test_should_use_ocr_none(self):
        assert self.should_use_ocr(None, None) is True

    def test_should_not_ocr_good_chinese_text(self):
        """文本质量好（大量中文）且解析成功时，不触发 OCR。"""
        good = "采购订单 天华超净技术股份有限公司 外箱 瓦楞纸箱 数量 交期 " * 10
        parse_ok = {"order_no": "THPO-001", "items": [{"line_no": 1}]}
        assert self.should_use_ocr(good, parse_ok) is False

    def test_should_use_ocr_on_parse_fail(self):
        result = {"_error": "parse_failed"}
        assert self.should_use_ocr("some text", result) is True

    def test_should_use_ocr_garbled_low_chinese(self):
        garbled = "ABCDEFGHIJKLMNOP 1234567 ######## xyzxyz"
        assert self.should_use_ocr(garbled, None) is True

    def test_should_use_ocr_custom_font_mojibake_even_if_parser_found_items(self):
        garbled = "䈑㕁亾㔘慷峏㖍㛇䦶慹桅孉ẟ⋽ỵ" * 10
        accidental_parse = {"items": [{"product_code": "CPN084557"}]}
        assert self.should_use_ocr(garbled, accidental_parse) is True

    def test_ocr_pdf_bytes_graceful_no_crash(self):
        """用 4 bytes 的假 PDF 调用 ocr_pdf_bytes，不崩溃，返回合法 method 值。"""
        from app.services.pdf_ocr import ocr_pdf_bytes
        text, method = ocr_pdf_bytes(b"%PDF")
        valid_methods = {
            "ocr_easyocr", "ocr_tesseract",
            "ocr_unavailable", "ocr_failed",
        }
        assert method in valid_methods, f"method={method!r} 不在合法集合内"


# ---------------------------------------------------------------------------
# Phase 19 API 集成测试
# ---------------------------------------------------------------------------

class TestPhase19Api:
    """OCR 状态端点 + ocr_text_raw 字段验证。"""

    def test_ocr_status_ok(self):
        r = _api_auth("get", "/api/pdf-training/ocr-status")
        assert r["status"] == 200, f"ocr-status failed: {r['body']}"
        body = json.loads(r["body"])
        assert "available" in body
        assert "engine" in body
        assert "message" in body
        assert isinstance(body["available"], bool)

    def test_ocr_status_requires_auth(self):
        r = _api("get", "/api/pdf-training/ocr-status")
        assert r["status"] == 401

    def test_sample_returns_ocr_text_raw(self):
        """样本详情应包含 ocr_text_raw 字段。"""
        create_code = (
            "import sys, json\n"
            "sys.path.insert(0,'.')\n"
            "from app.core.database import SessionLocal\n"
            "from app.models.pdf_training import PdfOrderTrainingSample\n"
            "with SessionLocal() as db:\n"
            "    s = PdfOrderTrainingSample(\n"
            "        file_name='ocr_test_p19.pdf',\n"
            "        file_sha256='ocr19test' + '0' * 55,\n"
            "        parse_method='ocr_easyocr',\n"
            "        parse_status='pending',\n"
            "        extracted_text='',\n"
            "        ocr_text_raw='天华超净 采购订单 THPO-2026-001',\n"
            "    )\n"
            "    db.add(s)\n"
            "    db.commit()\n"
            "    db.refresh(s)\n"
            "    print(s.id)\n"
        )
        res = _run(create_code)
        if res.returncode != 0:
            pytest.skip(f"DB insert failed: {res.stderr[:200]}")
        sample_id = int(res.stdout.strip())

        r = _api_auth("get", f"/api/pdf-training/samples/{sample_id}")
        assert r["status"] == 200
        body = json.loads(r["body"])
        assert "ocr_text_raw" in body, "响应缺少 ocr_text_raw"
        assert body["ocr_text_raw"] == "天华超净 采购订单 THPO-2026-001"
        assert body["parse_method"] == "ocr_easyocr"

    def test_ocr_unavailable_parse_method_accepted(self):
        """parse_method='ocr_unavailable' DB 约束应接受。"""
        code = (
            "import sys\n"
            "sys.path.insert(0,'.')\n"
            "from app.core.database import SessionLocal\n"
            "from app.models.pdf_training import PdfOrderTrainingSample\n"
            "with SessionLocal() as db:\n"
            "    s = PdfOrderTrainingSample(\n"
            "        file_name='ocr_unavail.pdf',\n"
            "        file_sha256='unavail99' + '0' * 55,\n"
            "        parse_method='ocr_unavailable',\n"
            "        parse_status='pending',\n"
            "    )\n"
            "    db.add(s)\n"
            "    db.commit()\n"
            "    print('ok')\n"
        )
        res = _run(code)
        assert res.returncode == 0, f"insert failed: {res.stderr[:200]}"
        assert "ok" in res.stdout

    def test_all_new_parse_methods_accepted(self):
        """扩展 parse_method 值均被 DB 约束接受。"""
        methods = [
            "ocr_easyocr", "ocr_tesseract", "mixed",
            "ocr_failed", "ocr_unavailable",
        ]
        for i, method in enumerate(methods):
            sha = f"meth{i:02d}phase19" + "0" * 54
            code = (
                "import sys\n"
                "sys.path.insert(0,'.')\n"
                "from app.core.database import SessionLocal\n"
                "from app.models.pdf_training import PdfOrderTrainingSample\n"
                "with SessionLocal() as db:\n"
                f"    s = PdfOrderTrainingSample(\n"
                f"        file_name='method_{method}.pdf',\n"
                f"        file_sha256='{sha}',\n"
                f"        parse_method='{method}',\n"
                "        parse_status='pending',\n"
                "    )\n"
                "    db.add(s)\n"
                "    db.commit()\n"
                "    print('ok')\n"
            )
            res = _run(code)
            assert res.returncode == 0, \
                f"parse_method={method!r} 约束拒绝: {res.stderr[:200]}"
