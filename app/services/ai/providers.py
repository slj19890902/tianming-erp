from __future__ import annotations

from dataclasses import dataclass
import json
import os
import socket
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class ProviderUnavailable(RuntimeError):
    """Raised when AI interpretation is intentionally unavailable."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        request_attempted: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.request_attempted = request_attempted


@dataclass(frozen=True, slots=True)
class ProviderLimits:
    max_risk_groups: int = 4
    max_next_checks: int = 8
    max_output_characters: int = 8_000


@dataclass(frozen=True, slots=True)
class ProviderResult:
    provider_code: str
    model_code: str
    output: dict
    input_tokens: int = 0
    output_tokens: int = 0


class InventoryInsightProvider(Protocol):
    provider_code: str
    model_code: str

    def generate(
        self,
        snapshot: dict,
        *,
        prompt_version: str,
        limits: ProviderLimits,
    ) -> ProviderResult:
        """Interpret a sanitized snapshot without access to ERP tools."""


class DisabledInventoryInsightProvider:
    provider_code = "disabled"
    model_code = "disabled"

    def generate(
        self,
        snapshot: dict,
        *,
        prompt_version: str,
        limits: ProviderLimits,
    ) -> ProviderResult:
        del snapshot, prompt_version, limits
        raise ProviderUnavailable(
            "ai_inventory_disabled",
            "AI 经营解读尚未启用，原库存经营看板仍可正常使用。",
        )


OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
DEFAULT_OPENAI_MODEL = "gpt-5-mini"


def _valid_openai_key(value: str) -> bool:
    return value.startswith("sk-") and len(value) >= 20 and not any(
        character.isspace() for character in value
    )


def _openai_timeout_seconds() -> float:
    try:
        value = float(os.getenv("ERP_AI_INVENTORY_TIMEOUT_SECONDS", "30"))
    except ValueError:
        return 30.0
    return min(60.0, max(5.0, value))


def _openai_output_schema() -> dict:
    evidence_refs = {
        "type": "array",
        "minItems": 1,
        "maxItems": 10,
        "uniqueItems": True,
        "items": {"type": "string", "pattern": r"^lot:[1-9][0-9]*$"},
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["summary", "risk_groups", "next_checks", "limitations"],
        "properties": {
            "summary": {"type": "string", "maxLength": 300},
            "risk_groups": {
                "type": "array",
                "maxItems": 4,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "category",
                        "title",
                        "explanation",
                        "evidence_refs",
                    ],
                    "properties": {
                        "category": {
                            "type": "string",
                            "enum": [
                                "aged_inventory",
                                "demand_coverage",
                                "cost_missing",
                                "data_quality",
                            ],
                        },
                        "title": {"type": "string", "maxLength": 80},
                        "explanation": {"type": "string", "maxLength": 400},
                        "evidence_refs": evidence_refs,
                    },
                },
            },
            "next_checks": {
                "type": "array",
                "maxItems": 8,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["title", "reason", "evidence_refs", "target_page"],
                    "properties": {
                        "title": {"type": "string", "maxLength": 80},
                        "reason": {"type": "string", "maxLength": 400},
                        "evidence_refs": evidence_refs,
                        "target_page": {
                            "type": "string",
                            "enum": ["warehouse", "orders", "requisition"],
                        },
                    },
                },
            },
            "limitations": {
                "type": "array",
                "maxItems": 6,
                "items": {"type": "string", "maxLength": 300},
            },
        },
    }


def _response_output_text(payload: dict) -> str:
    direct = payload.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct
    parts: list[str] = []
    for item in payload.get("output") or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for content in item.get("content") or []:
            if not isinstance(content, dict) or content.get("type") != "output_text":
                continue
            value = content.get("text")
            if isinstance(value, str):
                parts.append(value)
    return "".join(parts)


class OpenAIInventoryInsightProvider:
    provider_code = "openai"

    def __init__(
        self,
        api_key: str,
        *,
        model_code: str = DEFAULT_OPENAI_MODEL,
        timeout_seconds: float = 30.0,
        opener=urlopen,
    ) -> None:
        if not _valid_openai_key(api_key):
            raise ProviderUnavailable(
                "ai_credentials_invalid",
                "AI 密钥无效，请在天明ERP助手中重新设置。",
            )
        if not model_code or len(model_code) > 80 or not all(
            character.isalnum() or character in "-._:" for character in model_code
        ):
            raise ProviderUnavailable(
                "ai_model_invalid",
                "AI 模型配置无效，请检查安装助手配置。",
            )
        self._api_key = api_key
        self.model_code = model_code
        self._timeout_seconds = min(60.0, max(5.0, float(timeout_seconds)))
        self._opener = opener

    def generate(
        self,
        snapshot: dict,
        *,
        prompt_version: str,
        limits: ProviderLimits,
    ) -> ProviderResult:
        request_payload = {
            "model": self.model_code,
            "store": False,
            "max_output_tokens": 2500,
            "instructions": (
                "你是天明包装ERP的只读库存经营解读助手。只能解释输入JSON中的确定性事实，"
                "不得猜测库存、成本、客户或业务状态，不得提出自动写入、删除、抵扣或建单动作。"
                "每项结论必须引用输入中存在的lot证据；输出简明中文并严格符合JSON Schema。"
            ),
            "input": (
                f"prompt_version={prompt_version}\n"
                "以下是ERP已脱敏且有权限范围限制的库存快照：\n"
                + json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
            ),
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "tianming_inventory_insight",
                    "strict": True,
                    "schema": _openai_output_schema(),
                }
            },
        }
        request = Request(
            OPENAI_RESPONSES_URL,
            data=json.dumps(request_payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
                "User-Agent": "TianmingERP-InventoryAssistant/1.0",
            },
            method="POST",
        )
        try:
            with self._opener(request, timeout=self._timeout_seconds) as response:
                raw = response.read(262_145)
        except HTTPError as error:
            if error.code in {401, 403}:
                raise ProviderUnavailable(
                    "ai_credentials_rejected",
                    "AI 密钥或项目权限不可用，请在天明ERP助手中重新设置。",
                    request_attempted=True,
                ) from error
            if error.code == 429:
                raise ProviderUnavailable(
                    "ai_rate_limited",
                    "AI 当前繁忙或项目额度不足，请稍后重试。",
                    request_attempted=True,
                ) from error
            raise ProviderUnavailable(
                "ai_provider_http_error",
                "AI 服务暂时不可用，原库存经营看板仍可正常使用。",
                request_attempted=True,
            ) from error
        except (TimeoutError, socket.timeout) as error:
            raise ProviderUnavailable(
                "ai_provider_timeout",
                "AI 请求超时，原库存经营看板仍可正常使用。",
                request_attempted=True,
            ) from error
        except (URLError, OSError) as error:
            raise ProviderUnavailable(
                "ai_provider_network_error",
                "AI 网络连接失败，原库存经营看板仍可正常使用。",
                request_attempted=True,
            ) from error
        if len(raw) > 262_144:
            raise ProviderUnavailable(
                "ai_response_too_large",
                "AI 返回内容过大，已停止处理。",
                request_attempted=True,
            )
        try:
            response_payload = json.loads(raw.decode("utf-8"))
            output = json.loads(_response_output_text(response_payload))
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as error:
            raise ProviderUnavailable(
                "ai_response_invalid",
                "AI 返回格式无效，原库存经营看板仍可正常使用。",
                request_attempted=True,
            ) from error
        usage = response_payload.get("usage") or {}
        return ProviderResult(
            provider_code=self.provider_code,
            model_code=str(response_payload.get("model") or self.model_code),
            output=output,
            input_tokens=max(0, int(usage.get("input_tokens") or 0)),
            output_tokens=max(0, int(usage.get("output_tokens") or 0)),
        )


_CATEGORY_DEFINITIONS = (
    (
        "aged_inventory",
        frozenset(
            {
                "age_slow",
                "age_attention",
                "age_handling",
                "age_cleanup",
                "no_demand_180",
            }
        ),
        "长期积压待核对",
        "先核对长期未流转库存是否仍有订单需求或现场用途。",
    ),
    (
        "demand_coverage",
        frozenset(
            {
                "finished_stock_can_cover_order",
                "finished_stock_exceeds_open_demand",
                "semi_stock_may_cover_demand",
            }
        ),
        "订单需求覆盖待核对",
        "先核对现有库存能否按当前订单和客户范围优先使用。",
    ),
    (
        "data_quality",
        frozenset(
            {
                "location_unavailable",
                "stock_date_unknown",
                "stock_date_estimated",
                "semi_product_assignment_missing",
            }
        ),
        "库存资料待补齐",
        "先补齐库位、入库日期或产品绑定，再决定后续处理。",
    ),
    (
        "cost_missing",
        frozenset({"cost_pending"}),
        "成本资料待补齐",
        "当前仅能核对数量事实，需先补齐成本依据。",
    ),
)


class MockInventoryInsightProvider:
    """Deterministic no-network provider for tests and isolated UAT."""

    provider_code = "mock"
    model_code = "deterministic-v1"

    def generate(
        self,
        snapshot: dict,
        *,
        prompt_version: str,
        limits: ProviderLimits,
    ) -> ProviderResult:
        del prompt_version
        evidence = snapshot.get("evidence")
        if not isinstance(evidence, list):
            evidence = []

        grouped: list[dict] = []
        grouped_refs: set[str] = set()
        for category, codes, title, explanation in _CATEGORY_DEFINITIONS:
            if category == "cost_missing" and not snapshot.get("contains_cost_data"):
                continue
            refs = [
                item["ref"]
                for item in evidence
                if isinstance(item, dict)
                and isinstance(item.get("ref"), str)
                and codes.intersection(item.get("risk_codes") or [])
            ]
            refs = list(dict.fromkeys(refs))[:10]
            if not refs:
                continue
            grouped.append(
                {
                    "category": category,
                    "title": title,
                    "explanation": explanation,
                    "evidence_refs": refs,
                }
            )
            grouped_refs.update(refs)
            if len(grouped) >= limits.max_risk_groups:
                break

        next_checks = []
        for item in evidence:
            if not isinstance(item, dict) or item.get("ref") not in grouped_refs:
                continue
            next_checks.append(
                {
                    "title": "打开库存批次核对现场事实",
                    "reason": "依据 ERP 已计算的库存、库龄、需求和库位事实人工判断。",
                    "evidence_refs": [item["ref"]],
                    "target_page": "warehouse",
                }
            )
            if len(next_checks) >= limits.max_next_checks:
                break

        output = {
            "summary": (
                "本次只读解读已按 ERP 确定性结果整理；"
                "所有处理动作仍需在原业务页面人工确认。"
            ),
            "risk_groups": grouped,
            "next_checks": next_checks,
            "limitations": list(snapshot.get("limitations") or []),
        }
        return ProviderResult(
            provider_code=self.provider_code,
            model_code=self.model_code,
            output=output,
        )


def inventory_provider_status() -> dict[str, object]:
    """Return credential-free provider status safe for authenticated users."""

    environment = os.getenv("ERP_ENVIRONMENT", "development").strip().lower()
    configured = (
        os.getenv("ERP_AI_INVENTORY_PROVIDER", "disabled").strip().lower()
        or "disabled"
    )
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    model_code = (
        os.getenv("ERP_AI_INVENTORY_MODEL", DEFAULT_OPENAI_MODEL).strip()
        or DEFAULT_OPENAI_MODEL
    )
    if configured == "openai":
        if _valid_openai_key(api_key):
            return {
                "enabled": True,
                "provider_code": "openai",
                "model_code": model_code,
                "mode": "openai_responses",
                "message": "AI 库存经营解读已启用。",
            }
        return {
            "enabled": False,
            "provider_code": "disabled",
            "model_code": "disabled",
            "mode": "credentials_missing",
            "message": "尚未设置有效AI密钥，请在天明ERP助手中设置。",
        }
    mock_enabled = environment in {"development", "test"} and configured == "mock"
    if mock_enabled:
        return {
            "enabled": True,
            "provider_code": "mock",
            "model_code": "deterministic-v1",
            "mode": "isolated_mock",
            "message": "当前为隔离 Mock 解读，不连接外部模型。",
        }
    return {
        "enabled": False,
        "provider_code": "disabled",
        "model_code": "disabled",
        "mode": "disabled",
        "message": "AI 经营解读尚未启用，原库存经营看板仍可正常使用。",
    }


def resolve_inventory_provider() -> InventoryInsightProvider:
    status = inventory_provider_status()
    if status["enabled"] and status["provider_code"] == "openai":
        return OpenAIInventoryInsightProvider(
            os.getenv("OPENAI_API_KEY", "").strip(),
            model_code=str(status["model_code"]),
            timeout_seconds=_openai_timeout_seconds(),
        )
    if status["enabled"] and status["provider_code"] == "mock":
        return MockInventoryInsightProvider()
    return DisabledInventoryInsightProvider()
