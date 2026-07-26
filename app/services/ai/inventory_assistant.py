from __future__ import annotations

from copy import deepcopy
from datetime import date
import hashlib
import json
import re
from typing import Literal

from app.services.ai.providers import (
    InventoryInsightProvider,
    ProviderLimits,
)


InventoryFocus = Literal[
    "all",
    "aged_inventory",
    "demand_coverage",
    "cost_missing",
]

SNAPSHOT_SCHEMA_VERSION = "ai.inventory.insight.snapshot.v1"
PROMPT_VERSION = "ai.inventory.insight.zh-cn.v1"
MAX_EVIDENCE = 50
ALLOWED_FOCUS = frozenset(
    {"all", "aged_inventory", "demand_coverage", "cost_missing"}
)
ALLOWED_CATEGORIES = frozenset(
    {"aged_inventory", "demand_coverage", "cost_missing", "data_quality"}
)
ALLOWED_TARGET_PAGES = frozenset({"warehouse", "orders", "requisition"})
AGE_REASON_CODES = frozenset(
    {
        "age_slow",
        "age_attention",
        "age_handling",
        "age_cleanup",
        "no_demand_180",
    }
)
DEMAND_REASON_CODES = frozenset(
    {
        "finished_stock_can_cover_order",
        "finished_stock_exceeds_open_demand",
        "semi_stock_may_cover_demand",
    }
)
COST_REASON_CODES = frozenset({"cost_pending"})
DATA_QUALITY_REASON_CODES = frozenset(
    {
        "location_unavailable",
        "stock_date_unknown",
        "stock_date_estimated",
        "semi_product_assignment_missing",
    }
)
ALLOWED_REASON_CODES = (
    AGE_REASON_CODES
    | DEMAND_REASON_CODES
    | COST_REASON_CODES
    | DATA_QUALITY_REASON_CODES
)
_FORBIDDEN_OUTPUT = re.compile(
    r"(?:https?://|<\s*/?\s*[a-z][^>]*>|"
    r"\b(?:select|insert|update|delete|drop|alter|truncate)\b)",
    flags=re.IGNORECASE,
)


class InventoryAssistantError(ValueError):
    """A safe, user-readable AI snapshot or output contract failure."""


def _allowed_dict(value: object, fields: frozenset[str]) -> dict:
    if not isinstance(value, dict):
        return {}
    return {field: deepcopy(value[field]) for field in fields if field in value}


def _as_date_string(value: object) -> str:
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return value
    raise InventoryAssistantError("库存分析日期无效")


def _focus_codes(focus: str, *, include_cost: bool) -> frozenset[str]:
    if focus == "aged_inventory":
        return AGE_REASON_CODES
    if focus == "demand_coverage":
        return DEMAND_REASON_CODES
    if focus == "cost_missing":
        if not include_cost:
            raise InventoryAssistantError("当前账号无成本权限，不能生成成本缺失解读")
        return COST_REASON_CODES
    return ALLOWED_REASON_CODES if include_cost else ALLOWED_REASON_CODES - COST_REASON_CODES


def build_inventory_snapshot(
    insights: dict,
    *,
    focus: InventoryFocus = "all",
    include_cost: bool,
    max_evidence: int = MAX_EVIDENCE,
) -> dict:
    """Create a bounded local-only snapshot from deterministic ERP facts."""

    if focus not in ALLOWED_FOCUS:
        raise InventoryAssistantError("库存分析关注方向无效")
    if max_evidence < 1 or max_evidence > MAX_EVIDENCE:
        raise InventoryAssistantError(f"库存分析最多允许 {MAX_EVIDENCE} 条依据")
    if not isinstance(insights, dict):
        raise InventoryAssistantError("库存分析数据无效")

    as_of = _as_date_string(insights.get("as_of"))
    relevant_codes = _focus_codes(focus, include_cost=include_cost)
    source_items = insights.get("action_items")
    if not isinstance(source_items, list):
        source_items = []

    evidence: list[dict] = []
    seen_refs: set[str] = set()
    relevant_item_count = 0
    for source_item in source_items:
        if not isinstance(source_item, dict):
            continue
        lot_id = source_item.get("lot_id")
        if not isinstance(lot_id, int) or lot_id <= 0:
            continue
        ref = f"lot:{lot_id}"
        if ref in seen_refs:
            continue
        source_reasons = source_item.get("reasons")
        if not isinstance(source_reasons, list):
            source_reasons = []
        risk_codes = [
            reason.get("code")
            for reason in source_reasons
            if isinstance(reason, dict)
            and reason.get("code") in relevant_codes
        ]
        risk_codes = list(dict.fromkeys(risk_codes))
        if not risk_codes:
            continue
        relevant_item_count += 1
        if len(evidence) >= max_evidence:
            continue

        item = _allowed_dict(
            source_item,
            frozenset(
                {
                    "priority",
                    "lot_number",
                    "inventory_type",
                    "status",
                    "location_code",
                    "quantity_available",
                    "unit",
                    "age_days",
                    "age_basis",
                    "last_movement_at",
                    "movement_stagnant_days",
                    "covered_demand_quantity",
                    "uncovered_demand_quantity",
                    "coverage_percent",
                    "coverage_basis",
                }
            ),
        )
        item["ref"] = ref
        item["risk_codes"] = risk_codes
        item["detail"] = _allowed_dict(
            source_item.get("detail"),
            frozenset(
                {
                    "customer_name",
                    "inventory_code",
                    "name",
                    "assigned_product_count",
                    "binding_scope",
                    "deduction_eligibility",
                }
            ),
        )
        item["demand"] = _allowed_dict(
            source_item.get("demand"),
            frozenset({"demand_30", "demand_90", "demand_180", "open_demand"}),
        )
        if include_cost:
            item["cost"] = _allowed_dict(
                source_item,
                frozenset(
                    {
                        "cost_status",
                        "estimated_unit_cost",
                        "estimated_value",
                    }
                ),
            )
        evidence.append(item)
        seen_refs.add(ref)

    summary_fields = {
        "recorded_lots",
        "available_lots",
        "finished_available",
        "semi_finished_available",
        "total_reserved",
        "total_damaged",
        "total_scrapped",
    }
    quality_fields = {
        "active_location_lots",
        "exact_stock_date_lots",
        "estimated_stock_date_lots",
        "unknown_stock_date_lots",
    }
    if include_cost:
        summary_fields.update({"actual_inventory_value", "estimated_inventory_value"})
        quality_fields.update(
            {
                "cost_ready_lots",
                "missing_cost_lots",
                "cost_coverage_percent",
                "snapshot_estimate_coverage",
                "current_quote_coverage",
                "product_reference_coverage",
                "actual_cost_supported",
                "actual_cost_message",
            }
        )

    limitations = [
        "本次解读只依据已录入 ERP 的库存，不代表现场尚未盘点的库存。",
        "AI 只解释和整理，不会修改订单、报料、库存、生产、送货或财务数据。",
    ]
    if include_cost:
        limitations.append("金额只代表后端可估算部分，不是全部实际现金成本。")
    else:
        limitations.append("当前分析不包含任何成本、价格或金额数据。")

    snapshot = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "analysis_type": "inventory_insight",
        "as_of": as_of,
        "focus": focus,
        "contains_cost_data": include_cost,
        "summary": _allowed_dict(insights.get("summary"), frozenset(summary_fields)),
        "data_quality": _allowed_dict(
            insights.get("data_quality"), frozenset(quality_fields)
        ),
        "evidence": evidence,
        "evidence_limit": max_evidence,
        "truncated": relevant_item_count > len(evidence),
        "limitations": limitations,
    }
    return snapshot


def inventory_snapshot_hash(snapshot: dict) -> str:
    canonical = json.dumps(
        snapshot,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _validate_text(value: object, *, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InventoryAssistantError(f"AI 输出缺少 {field}")
    text = value.strip()
    if len(text) > maximum:
        raise InventoryAssistantError(f"AI 输出 {field} 过长")
    if _FORBIDDEN_OUTPUT.search(text):
        raise InventoryAssistantError(f"AI 输出 {field} 包含禁止内容")
    return text


def _validate_refs(
    value: object,
    *,
    field: str,
    allowed_refs: frozenset[str],
) -> list[str]:
    if not isinstance(value, list) or not value:
        raise InventoryAssistantError(f"AI 输出 {field} 缺少库存依据")
    refs = []
    for ref in value:
        if not isinstance(ref, str) or ref not in allowed_refs:
            raise InventoryAssistantError(f"AI 输出 {field} 引用了不存在的库存依据")
        refs.append(ref)
    return list(dict.fromkeys(refs))


def validate_inventory_interpretation(
    output: dict,
    *,
    snapshot: dict,
    limits: ProviderLimits | None = None,
) -> dict:
    """Validate model output before any result can be rendered as successful."""

    limits = limits or ProviderLimits()
    if not isinstance(output, dict):
        raise InventoryAssistantError("AI 输出结构无效")
    allowed_refs = frozenset(
        item["ref"]
        for item in snapshot.get("evidence", [])
        if isinstance(item, dict) and isinstance(item.get("ref"), str)
    )
    summary = _validate_text(output.get("summary"), field="summary", maximum=300)

    source_groups = output.get("risk_groups")
    if not isinstance(source_groups, list):
        raise InventoryAssistantError("AI 输出 risk_groups 无效")
    if len(source_groups) > limits.max_risk_groups:
        raise InventoryAssistantError("AI 风险分组数量超限")
    risk_groups = []
    for source_group in source_groups:
        if not isinstance(source_group, dict):
            raise InventoryAssistantError("AI 风险分组结构无效")
        category = source_group.get("category")
        if category not in ALLOWED_CATEGORIES:
            raise InventoryAssistantError("AI 风险类别不在白名单")
        if category == "cost_missing" and not snapshot.get("contains_cost_data"):
            raise InventoryAssistantError("无成本权限时禁止返回成本结论")
        risk_groups.append(
            {
                "category": category,
                "title": _validate_text(
                    source_group.get("title"), field="risk title", maximum=80
                ),
                "explanation": _validate_text(
                    source_group.get("explanation"),
                    field="risk explanation",
                    maximum=400,
                ),
                "evidence_refs": _validate_refs(
                    source_group.get("evidence_refs"),
                    field="risk evidence",
                    allowed_refs=allowed_refs,
                ),
            }
        )

    source_checks = output.get("next_checks")
    if not isinstance(source_checks, list):
        raise InventoryAssistantError("AI 输出 next_checks 无效")
    if len(source_checks) > limits.max_next_checks:
        raise InventoryAssistantError("AI 人工核对建议数量超限")
    next_checks = []
    for source_check in source_checks:
        if not isinstance(source_check, dict):
            raise InventoryAssistantError("AI 人工核对建议结构无效")
        target_page = source_check.get("target_page")
        if target_page not in ALLOWED_TARGET_PAGES:
            raise InventoryAssistantError("AI 目标页面不在白名单")
        next_checks.append(
            {
                "title": _validate_text(
                    source_check.get("title"), field="check title", maximum=80
                ),
                "reason": _validate_text(
                    source_check.get("reason"), field="check reason", maximum=400
                ),
                "evidence_refs": _validate_refs(
                    source_check.get("evidence_refs"),
                    field="check evidence",
                    allowed_refs=allowed_refs,
                ),
                "target_page": target_page,
            }
        )

    source_limitations = output.get("limitations")
    if not isinstance(source_limitations, list) or len(source_limitations) > 6:
        raise InventoryAssistantError("AI 局限说明结构无效")
    limitations = [
        _validate_text(item, field="limitation", maximum=300)
        for item in source_limitations
    ]
    validated = {
        "summary": summary,
        "risk_groups": risk_groups,
        "next_checks": next_checks,
        "limitations": limitations,
    }
    encoded = json.dumps(validated, ensure_ascii=False, separators=(",", ":"))
    if len(encoded) > limits.max_output_characters:
        raise InventoryAssistantError("AI 输出总长度超限")
    return validated


def generate_inventory_interpretation(
    snapshot: dict,
    *,
    provider: InventoryInsightProvider,
    limits: ProviderLimits | None = None,
) -> dict:
    """Generate and validate a read-only interpretation without ERP tools."""

    limits = limits or ProviderLimits()
    provider_result = provider.generate(
        deepcopy(snapshot),
        prompt_version=PROMPT_VERSION,
        limits=limits,
    )
    validated = validate_inventory_interpretation(
        provider_result.output,
        snapshot=snapshot,
        limits=limits,
    )
    return {
        "status": "success",
        "provider_code": provider_result.provider_code,
        "model_code": provider_result.model_code,
        "prompt_version": PROMPT_VERSION,
        "input_hash": inventory_snapshot_hash(snapshot),
        "input_tokens": provider_result.input_tokens,
        "output_tokens": provider_result.output_tokens,
        "interpretation": validated,
    }
