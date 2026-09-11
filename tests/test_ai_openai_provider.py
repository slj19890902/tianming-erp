from __future__ import annotations

from io import BytesIO
import json
from urllib.error import HTTPError, URLError

import pytest

from app.services.ai.providers import (
    OPENAI_RESPONSES_URL,
    DisabledInventoryInsightProvider,
    OpenAIInventoryInsightProvider,
    ProviderLimits,
    ProviderUnavailable,
    inventory_provider_status,
    resolve_inventory_provider,
)


API_KEY = "sk-test-openai-provider-secret-value"


def _snapshot() -> dict:
    return {
        "schema_version": "ai.inventory.insight.snapshot.v1",
        "analysis_type": "inventory_insight",
        "as_of": "2026-09-11",
        "focus": "all",
        "contains_cost_data": False,
        "summary": {"available_lots": 1},
        "data_quality": {"active_location_lots": 1},
        "evidence": [
            {
                "ref": "lot:17",
                "risk_codes": ["age_slow"],
                "quantity_available": 20,
                "unit": "只",
            }
        ],
        "limitations": ["只依据ERP已录入库存。"],
    }


def _interpretation() -> dict:
    return {
        "summary": "有一批库存库龄较长，需要现场核对。",
        "risk_groups": [
            {
                "category": "aged_inventory",
                "title": "长期库存",
                "explanation": "该批次符合ERP已计算的库龄风险。",
                "evidence_refs": ["lot:17"],
            }
        ],
        "next_checks": [
            {
                "title": "核对现场库存",
                "reason": "确认库存仍可正常使用。",
                "evidence_refs": ["lot:17"],
                "target_page": "warehouse",
            }
        ],
        "limitations": ["只依据ERP已录入库存。"],
    }


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self, limit: int) -> bytes:
        return self._payload[:limit]


def test_openai_provider_sends_bounded_tool_free_structured_request() -> None:
    captured = {}

    def opener(request, *, timeout):
        captured["url"] = request.full_url
        captured["headers"] = dict(request.header_items())
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return _FakeResponse(
            {
                "model": "gpt-5-mini-2025-08-07",
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": json.dumps(_interpretation(), ensure_ascii=False),
                            }
                        ],
                    }
                ],
                "usage": {"input_tokens": 123, "output_tokens": 45},
            }
        )

    result = OpenAIInventoryInsightProvider(
        API_KEY,
        timeout_seconds=12,
        opener=opener,
    ).generate(
        _snapshot(),
        prompt_version="ai.inventory.insight.zh-cn.v1",
        limits=ProviderLimits(),
    )

    assert captured["url"] == OPENAI_RESPONSES_URL
    assert captured["timeout"] == 12
    assert captured["headers"]["Authorization"] == f"Bearer {API_KEY}"
    body = captured["payload"]
    assert body["store"] is False
    assert "tools" not in body
    assert body["text"]["format"]["type"] == "json_schema"
    assert body["text"]["format"]["strict"] is True
    assert API_KEY not in json.dumps(body, ensure_ascii=False)
    assert result.output == _interpretation()
    assert result.model_code == "gpt-5-mini-2025-08-07"
    assert result.input_tokens == 123
    assert result.output_tokens == 45


@pytest.mark.parametrize(
    ("error", "expected_code"),
    [
        (
            HTTPError(OPENAI_RESPONSES_URL, 401, "unauthorized", {}, BytesIO()),
            "ai_credentials_rejected",
        ),
        (
            HTTPError(OPENAI_RESPONSES_URL, 429, "limited", {}, BytesIO()),
            "ai_rate_limited",
        ),
        (URLError("offline"), "ai_provider_network_error"),
        (TimeoutError(), "ai_provider_timeout"),
    ],
)
def test_openai_provider_errors_are_safe_and_mark_attempted(error, expected_code) -> None:
    def opener(_request, *, timeout):
        del timeout
        raise error

    provider = OpenAIInventoryInsightProvider(API_KEY, opener=opener)
    with pytest.raises(ProviderUnavailable) as caught:
        provider.generate(_snapshot(), prompt_version="v1", limits=ProviderLimits())
    assert caught.value.code == expected_code
    assert caught.value.request_attempted is True
    assert API_KEY not in str(caught.value)


def test_openai_status_requires_explicit_provider_and_valid_key(monkeypatch) -> None:
    monkeypatch.setenv("ERP_ENVIRONMENT", "production")
    monkeypatch.setenv("ERP_AI_INVENTORY_PROVIDER", "openai")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    missing = inventory_provider_status()
    assert missing["enabled"] is False
    assert missing["mode"] == "credentials_missing"
    assert isinstance(resolve_inventory_provider(), DisabledInventoryInsightProvider)

    monkeypatch.setenv("OPENAI_API_KEY", API_KEY)
    ready = inventory_provider_status()
    assert ready["enabled"] is True
    assert ready["provider_code"] == "openai"
    assert isinstance(resolve_inventory_provider(), OpenAIInventoryInsightProvider)


def test_openai_provider_rejects_invalid_response_without_exposing_body() -> None:
    def opener(_request, *, timeout):
        del timeout
        return _FakeResponse({"output_text": "not-json", "private": API_KEY})

    provider = OpenAIInventoryInsightProvider(API_KEY, opener=opener)
    with pytest.raises(ProviderUnavailable) as caught:
        provider.generate(_snapshot(), prompt_version="v1", limits=ProviderLimits())
    assert caught.value.code == "ai_response_invalid"
    assert caught.value.request_attempted is True
    assert API_KEY not in str(caught.value)
