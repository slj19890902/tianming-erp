from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class ProviderUnavailable(RuntimeError):
    """Raised when AI interpretation is intentionally unavailable."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


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
