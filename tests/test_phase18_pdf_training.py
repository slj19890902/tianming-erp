"""
Phase 18 / v0.18.0: PDF 订单识别训练样本库测试。

覆盖：
- pdf_scoring 评分服务（单元测试）
- /api/pdf-training/* REST API（集成测试）
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

PYTHON = sys.executable
BASE_URL = "http://127.0.0.1:8001"
ENCODING = {"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
pytestmark = pytest.mark.usefixtures("isolated_subprocess_database")


# ---------------------------------------------------------------------------
# 评分服务单元测试（无需服务器）
# ---------------------------------------------------------------------------

class TestPdfScoringUnit:
    """app/services/pdf_scoring.py 单元测试。"""

    def setup_method(self):
        sys.path.insert(0, str(__file__.split("tests")[0]))
        from app.services.pdf_scoring import correction_candidates, score_sample
        self.correction_candidates = correction_candidates
        self.score_sample = score_sample

    def _truth(self, **extra) -> str:
        base = {
            "order_no": "THPO-2024-001",
            "customer_name": "某某公司",
            "order_date": "2024-01-15",
            "delivery_date": "2024-01-30",
            "items": [
                {
                    "line_no": 1,
                    "product_code": "P001",
                    "product_name": "产品甲",
                    "spec": "500*300*200",
                    "quantity": 100,
                    "unit_price": "12.50",
                    "amount": "1250.00",
                }
            ],
        }
        base.update(extra)
        return json.dumps(base, ensure_ascii=False)

    def test_perfect_match(self):
        truth = self._truth()
        r = self.score_sample(truth, truth)
        assert r.overall_score == 1.0, f"完全匹配应得 1.0，实际 {r.overall_score}"

    def test_no_parser_result(self):
        truth = self._truth()
        r = self.score_sample(None, truth)
        assert 0.0 <= r.overall_score < 1.0

    def test_empty_ground_truth(self):
        r = self.score_sample("{}", None)
        assert r.overall_score == 0.0
        assert r.error is not None

    def test_invalid_ground_truth_json(self):
        r = self.score_sample("{}", "not valid json")
        assert r.overall_score == 0.0
        assert "解析失败" in (r.error or "")

    def test_wrong_order_no(self):
        truth = self._truth()
        parsed = self._truth(order_no="THPO-WRONG")
        r = self.score_sample(parsed, truth)
        # order_no 权重 0.20，所以 score < 1.0
        assert r.overall_score < 1.0

    def test_items_partial_match(self):
        """明细行部分命中时，评分应介于 0 和 1 之间。"""
        truth = self._truth()
        # 明细行 quantity 错误
        parsed = json.loads(truth)
        parsed["items"][0]["quantity"] = 999
        r = self.score_sample(json.dumps(parsed), truth)
        assert 0.0 < r.overall_score < 1.0

    def test_case_insensitive_match(self):
        """归一化：大小写不影响匹配。"""
        truth = json.dumps({"order_no": "THPO-2024-ABC", "items": []})
        parsed = json.dumps({"order_no": "thpo-2024-abc", "items": []})
        r = self.score_sample(parsed, truth)
        # order_no 命中 → 有正向得分
        order_score = next(f for f in r.field_scores if f.field_path == "order_no")
        assert order_score.matched

    def test_numeric_tolerance(self):
        """金额允许 ±0.01 误差。"""
        truth = json.dumps({"order_no": "X", "items": [{"amount": "1250.00"}]})
        parsed = json.dumps({"order_no": "X", "items": [{"amount": "1249.995"}]})
        r = self.score_sample(parsed, truth)
        amt_scores = [f for f in r.field_scores if "amount" in f.field_path]
        assert all(f.matched for f in amt_scores)

    def test_field_scores_populated(self):
        truth = self._truth()
        r = self.score_sample(truth, truth)
        assert len(r.field_scores) > 0
        paths = {f.field_path for f in r.field_scores}
        assert "order_no" in paths
        assert "items[0].product_code" in paths

    def test_item_count_reported(self):
        truth = self._truth()
        r = self.score_sample(truth, truth)
        assert r.item_count_truth == 1
        assert r.item_count_parsed == 1

    def test_zero_items_truth(self):
        """ground_truth 无明细行时，items 得分逻辑不应崩溃。"""
        truth = json.dumps({"order_no": "X", "items": []})
        r = self.score_sample(truth, truth)
        assert r.overall_score >= 0.0

    def test_score_range(self):
        """评分必须在 [0, 1] 范围内。"""
        truth = self._truth()
        parsed = json.dumps({"garbage": "data"})
        r = self.score_sample(parsed, truth)
        assert 0.0 <= r.overall_score <= 1.0

    def test_preview_schema_aliases_score_and_log_only_real_differences(self):
        truth = self._truth()
        parsed = json.loads(truth)
        parsed["customer_po"] = parsed.pop("order_no")
        parsed["items"][0]["specification"] = parsed["items"][0].pop("spec")
        assert self.score_sample(json.dumps(parsed), truth).overall_score == 1.0
        parsed["items"][0]["quantity"] = 101
        differences = self.correction_candidates(json.dumps(parsed), truth)
        assert {row["field_path"] for row in differences} == {"items[0].quantity"}


# ---------------------------------------------------------------------------
# compute_stats 单元测试
# ---------------------------------------------------------------------------

class TestComputeStats:
    def setup_method(self):
        sys.path.insert(0, str(__file__.split("tests")[0]))
        from app.services.pdf_scoring import compute_stats
        self.compute_stats = compute_stats

    def _make_sample(self, score=None, status="pending", corrections=None):
        class FakeSample:
            pass
        s = FakeSample()
        s.score = score
        s.parse_status = status
        s.corrections = corrections or []
        return s

    def test_empty(self):
        stats = self.compute_stats([])
        assert stats.sample_count == 0
        assert stats.avg_score is None

    def test_labeled_count(self):
        samples = [
            self._make_sample(score=0.9, status="labeled"),
            self._make_sample(score=None, status="pending"),
            self._make_sample(score=0.7, status="reviewed"),
        ]
        stats = self.compute_stats(samples)
        assert stats.sample_count == 3
        assert stats.labeled_count == 2  # labeled + reviewed

    def test_avg_score(self):
        samples = [
            self._make_sample(score=0.8),
            self._make_sample(score=0.6),
        ]
        stats = self.compute_stats(samples)
        assert abs(stats.avg_score - 0.7) < 0.001

    def test_high_mid_low(self):
        samples = [
            self._make_sample(score=0.95),  # high
            self._make_sample(score=0.80),  # mid
            self._make_sample(score=0.50),  # low
        ]
        stats = self.compute_stats(samples)
        assert stats.high_count == 1
        assert stats.mid_count == 1
        assert stats.low_count == 1

    def test_top_error_fields(self):
        class FakeLog:
            def __init__(self, fp):
                self.field_path = fp
        s1 = self._make_sample(corrections=[FakeLog("order_no"), FakeLog("order_no")])
        s2 = self._make_sample(corrections=[FakeLog("order_no"), FakeLog("items[0].quantity")])
        stats = self.compute_stats([s1, s2])
        assert stats.top_error_fields[0][0] == "order_no"
        assert stats.top_error_fields[0][1] == 3


# ---------------------------------------------------------------------------
# REST API 集成测试（需要服务器进程）
# ---------------------------------------------------------------------------

def _run(cmd: str) -> subprocess.CompletedProcess:
    env = {**os.environ, **ENCODING}
    return subprocess.run(
        [PYTHON, "-X", "utf8", "-c", cmd],
        capture_output=True, text=True, encoding="utf-8",
        timeout=30, env=env,
        cwd=str(__file__).split("tests")[0].rstrip("/\\"),
    )


def _api(method: str, path: str, **kwargs) -> dict:
    """通过嵌入式 TestClient 调用 API（不启动真实服务器）。"""
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


@pytest.fixture(scope="module")
def admin_client():
    """返回一个带认证 cookie 的 TestClient（通过 subprocess 内嵌）。"""
    return None  # 集成测试使用 _api_auth helper


def _api_auth(method: str, path: str, payload: dict | None = None, files=None) -> dict:
    """带管理员 session 的 API 调用。"""
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

# 找第一个 admin 账号
with SessionLocal() as db:
    admin = db.query(User).filter(User.role=='admin', User.is_active==True).first()
    if not admin:
        print(json.dumps({{'status': 0, 'body': 'no admin user'}}))
        sys.exit(0)
    token = create_session_token(admin.id, auth_version=admin.auth_version)
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
    data = json.loads(result.stdout.strip())
    return data


class TestPdfTrainingApi:
    """Phase 18 REST API 集成测试。"""

    def test_stats_requires_auth(self):
        """未登录访问 /stats 应返回 401。"""
        result = _api("get", "/api/pdf-training/stats")
        assert result["status"] == 401

    def test_stats_with_admin(self):
        """管理员可访问 /stats。"""
        r = _api_auth("get", "/api/pdf-training/stats")
        assert r["status"] == 200
        body = json.loads(r["body"])
        assert "sample_count" in body
        assert "labeled_count" in body
        assert "avg_score" in body
        assert "top_error_fields" in body

    def test_batches_list(self):
        r = _api_auth("get", "/api/pdf-training/batches")
        assert r["status"] == 200
        body = json.loads(r["body"])
        assert isinstance(body, list)

    def test_create_batch(self):
        r = _api_auth("post", "/api/pdf-training/batches", payload={
            "batch_name": "测试批次 Phase18",
            "description": "单元测试自动创建",
        })
        assert r["status"] == 201, f"create batch failed: {r['body']}"
        body = json.loads(r["body"])
        assert body["batch_name"] == "测试批次 Phase18"
        assert body["sample_count"] == 0

    def test_samples_list(self):
        r = _api_auth("get", "/api/pdf-training/samples")
        assert r["status"] == 200
        body = json.loads(r["body"])
        assert isinstance(body, list)

    def test_samples_list_legacy_alias(self):
        r = _api_auth("get", "/api/pdf-training/samples/list")
        assert r["status"] == 200
        body = json.loads(r["body"])
        assert isinstance(body, list)

    def test_templates_list(self):
        r = _api_auth("get", "/api/pdf-training/templates")
        assert r["status"] == 200
        body = json.loads(r["body"])
        assert isinstance(body, list)

    def test_create_template(self):
        r = _api_auth("post", "/api/pdf-training/templates", payload={
            "template_name": "测试模板",
            "order_no_pattern": r"THPO-\d+",
            "notes": "Phase 18 自动测试",
        })
        assert r["status"] == 201, f"create template failed: {r['body']}"
        body = json.loads(r["body"])
        assert body["template_name"] == "测试模板"
        assert body["is_active"] is False
        assert body["status"] == "draft"
        return body["id"]

    def test_sample_not_found(self):
        r = _api_auth("get", "/api/pdf-training/samples/999999")
        assert r["status"] == 404

    def test_sample_invalid_id_returns_readable_message(self):
        r = _api_auth("get", "/api/pdf-training/samples/not-a-number")
        assert r["status"] == 400
        body = json.loads(r["body"])
        assert "样本ID无效" in body["detail"]

    def test_sample_legacy_detail_alias_invalid_id_returns_readable_message(self):
        r = _api_auth("get", "/api/pdf-training/samples/detail/not-a-number")
        assert r["status"] == 400
        body = json.loads(r["body"])
        assert "样本ID无效" in body["detail"]

    def test_ground_truth_requires_labeled_sample(self):
        """对不存在的样本写 ground_truth 应返回 404。"""
        r = _api_auth("put", "/api/pdf-training/samples/999999/ground-truth", payload={
            "ground_truth_json": '{"order_no":"X","items":[]}'
        })
        assert r["status"] == 404

    def test_ground_truth_invalid_json(self):
        """已创建样本写非法 JSON → 422。"""
        # 先创建一个样本（通过 DB 直接插入，避免文件上传）
        create_code = """
import sys, json
sys.path.insert(0,'.')
from app.core.database import SessionLocal
from app.models.pdf_training import PdfOrderTrainingSample
with SessionLocal() as db:
    s = PdfOrderTrainingSample(
        file_name='test.pdf',
        file_sha256='aabbcc' + '0' * 58,
        parse_method='text',
        parse_status='pending',
        parser_result_json='{}',
    )
    db.add(s)
    db.commit()
    db.refresh(s)
    print(s.id)
"""
        res = _run(create_code)
        if res.returncode != 0:
            pytest.skip(f"DB insert failed: {res.stderr[:200]}")
        sample_id = int(res.stdout.strip())

        r = _api_auth(
            "put",
            f"/api/pdf-training/samples/{sample_id}/ground-truth",
            payload={"ground_truth_json": "not valid json"},
        )
        assert r["status"] == 422

    def test_score_without_ground_truth(self):
        """没有 ground_truth_json 的样本请求评分 → 422。"""
        create_code = """
import sys, json
sys.path.insert(0,'.')
from app.core.database import SessionLocal
from app.models.pdf_training import PdfOrderTrainingSample
with SessionLocal() as db:
    s = PdfOrderTrainingSample(
        file_name='test2.pdf',
        file_sha256='ccddee' + '0' * 58,
        parse_method='text',
        parse_status='pending',
    )
    db.add(s)
    db.commit()
    db.refresh(s)
    print(s.id)
"""
        res = _run(create_code)
        if res.returncode != 0:
            pytest.skip(f"DB insert failed: {res.stderr[:200]}")
        sample_id = int(res.stdout.strip())

        r = _api_auth("post", f"/api/pdf-training/samples/{sample_id}/score")
        assert r["status"] == 422

    def test_score_with_ground_truth(self):
        """有 ground_truth_json 的样本评分应返回 200 + overall_score。"""
        truth = json.dumps({
            "order_no": "THPO-TEST",
            "customer_name": "测试客户",
            "items": [{"product_code": "P999", "quantity": 10}],
        })
        create_code = f"""
import sys, json
sys.path.insert(0,'.')
from app.core.database import SessionLocal
from app.models.pdf_training import PdfOrderTrainingSample
with SessionLocal() as db:
    s = PdfOrderTrainingSample(
        file_name='test3.pdf',
        file_sha256='eeff00' + '0' * 58,
        parse_method='text',
        parse_status='pending',
        parser_result_json={truth!r},
        ground_truth_json={truth!r},
    )
    db.add(s)
    db.commit()
    db.refresh(s)
    print(s.id)
"""
        res = _run(create_code)
        if res.returncode != 0:
            pytest.skip(f"DB insert failed: {res.stderr[:200]}")
        sample_id = int(res.stdout.strip())

        r = _api_auth("post", f"/api/pdf-training/samples/{sample_id}/score")
        assert r["status"] == 200, f"score failed: {r['body']}"
        body = json.loads(r["body"])
        assert "overall_score" in body
        assert 0.0 <= body["overall_score"] <= 1.0

    def test_delete_sample_admin(self):
        """管理员可以删除样本。"""
        create_code = """
import sys
sys.path.insert(0,'.')
from app.core.database import SessionLocal
from app.models.pdf_training import PdfOrderTrainingSample
with SessionLocal() as db:
    s = PdfOrderTrainingSample(
        file_name='delete_me.pdf',
        file_sha256='ffaa11' + '0' * 58,
        parse_method='unknown',
        parse_status='pending',
    )
    db.add(s)
    db.commit()
    db.refresh(s)
    print(s.id)
"""
        res = _run(create_code)
        if res.returncode != 0:
            pytest.skip(f"DB insert failed: {res.stderr[:200]}")
        sample_id = int(res.stdout.strip())

        r = _api_auth("delete", f"/api/pdf-training/samples/{sample_id}")
        assert r["status"] == 204, f"delete failed: {r['body']}"

        # 确认已删除
        r2 = _api_auth("get", f"/api/pdf-training/samples/{sample_id}")
        assert r2["status"] == 404
