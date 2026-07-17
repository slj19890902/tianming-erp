"""
Phase 18 / v0.18.0: PDF 订单识别评分服务。

将解析器输出（parser_result_json）与人工标注（ground_truth_json）逐字段比对，
计算识别准确率评分（0.0–1.0）并生成字段级报告。

评分规则
--------
顶层字段（各占权重）：
  order_no         0.20
  customer_name    0.10
  order_date       0.10
  delivery_date    0.05
  items            0.55  （按行数平均后逐行评分）

行级字段（每行内权重）：
  product_code     0.25
  product_name     0.15
  spec             0.15
  quantity         0.20
  unit_price       0.15
  amount           0.10
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# 配置：权重
# ---------------------------------------------------------------------------

TOP_WEIGHTS: dict[str, float] = {
    "order_no": 0.20,
    "customer_name": 0.10,
    "order_date": 0.10,
    "delivery_date": 0.05,
    "items": 0.55,
}

ITEM_WEIGHTS: dict[str, float] = {
    "product_code": 0.25,
    "product_name": 0.15,
    "spec": 0.15,
    "quantity": 0.20,
    "unit_price": 0.15,
    "amount": 0.10,
}


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class FieldScore:
    field_path: str
    truth_value: str | None
    parsed_value: str | None
    matched: bool
    weight: float
    weighted_score: float  # weight * (1 if matched else 0)


@dataclass
class ScoringResult:
    overall_score: float            # 0.0–1.0
    field_scores: list[FieldScore] = field(default_factory=list)
    item_count_truth: int = 0
    item_count_parsed: int = 0
    error: str | None = None        # 解析 JSON 失败时填入说明


# ---------------------------------------------------------------------------
# 字符串归一化（用于宽松比对）
# ---------------------------------------------------------------------------

def _normalize(value: Any) -> str:
    """将任意值转为比对用的归一化字符串（去空白、大小写统一）。"""
    if value is None:
        return ""
    text = str(value)
    # 去掉所有空白、破折号、星号
    text = re.sub(r"[\s\-\*×xX,，]", "", text)
    return text.casefold()


def _values_match(truth: Any, parsed: Any) -> bool:
    """宽松比对：归一化后相等即算命中。数字比对允许 ±0.01 误差。"""
    if truth is None and parsed is None:
        return True
    nt = _normalize(truth)
    np = _normalize(parsed)
    if nt == np:
        return True
    # 尝试数值比对（金额、数量）
    try:
        ft = float(nt.replace(",", ""))
        fp = float(np.replace(",", ""))
        return abs(ft - fp) < 0.011
    except ValueError:
        pass
    return False


def values_match(truth: Any, parsed: Any) -> bool:
    """Public comparison used by lifecycle evidence checks and scoring alike."""
    return _values_match(truth, parsed)


def _parsed_top_value(parsed: dict, field_name: str) -> Any:
    """Keep the training schema tolerant of order-preview field names."""
    if field_name == "order_no":
        return parsed.get("order_no") or parsed.get("customer_po")
    return parsed.get(field_name)


def parsed_top_value(parsed: dict, field_name: str) -> Any:
    """Public parsed-value alias contract shared with lifecycle validation."""
    return _parsed_top_value(parsed, field_name)


def _parsed_item_value(parsed_row: dict, field_name: str) -> Any:
    if field_name == "spec":
        return (
            parsed_row.get("spec")
            or parsed_row.get("specification")
            or parsed_row.get("raw_spec_model")
        )
    return parsed_row.get(field_name)


def parsed_item_value(parsed_row: dict, field_name: str) -> Any:
    """Public item alias contract (notably spec/specification/raw_spec_model)."""
    return _parsed_item_value(parsed_row, field_name)


# ---------------------------------------------------------------------------
# 核心评分
# ---------------------------------------------------------------------------

def _score_items(
    truth_items: list[dict],
    parsed_items: list[dict],
) -> tuple[float, list[FieldScore]]:
    """对明细行逐行逐字段评分，返回 (0–1 均分, 字段得分列表)。"""
    scores: list[FieldScore] = []
    if not truth_items:
        return (1.0 if not parsed_items else 0.0), scores

    # 按行数对齐（多余的解析行或缺失的行计为 0 分）
    row_scores: list[float] = []
    for idx, truth_row in enumerate(truth_items):
        parsed_row = parsed_items[idx] if idx < len(parsed_items) else {}
        row_total_weight = 0.0
        row_weighted_score = 0.0
        for fname, w in ITEM_WEIGHTS.items():
            tv = truth_row.get(fname)
            pv = _parsed_item_value(parsed_row, fname)
            matched = _values_match(tv, pv)
            ws = w if matched else 0.0
            scores.append(
                FieldScore(
                    field_path=f"items[{idx}].{fname}",
                    truth_value=str(tv) if tv is not None else None,
                    parsed_value=str(pv) if pv is not None else None,
                    matched=matched,
                    weight=w,
                    weighted_score=ws,
                )
            )
            row_total_weight += w
            row_weighted_score += ws
        row_scores.append(
            row_weighted_score / row_total_weight if row_total_weight else 0.0
        )

    return sum(row_scores) / len(row_scores), scores


def score_sample(
    parser_result_json: str | None,
    ground_truth_json: str | None,
) -> ScoringResult:
    """
    主入口：对单条样本评分。

    Parameters
    ----------
    parser_result_json : 解析器输出 JSON 字符串
    ground_truth_json  : 人工标注 JSON 字符串

    Returns
    -------
    ScoringResult  含 overall_score（0.0–1.0）和字段级明细
    """
    if not ground_truth_json:
        return ScoringResult(
            overall_score=0.0,
            error="ground_truth_json 为空，无法评分",
        )

    try:
        truth: dict = json.loads(ground_truth_json)
    except (json.JSONDecodeError, ValueError) as exc:
        return ScoringResult(
            overall_score=0.0,
            error=f"ground_truth_json 解析失败: {exc}",
        )

    try:
        parsed: dict = json.loads(parser_result_json or "{}")
    except (json.JSONDecodeError, ValueError):
        parsed = {}

    all_field_scores: list[FieldScore] = []
    total_weight = 0.0
    total_weighted_score = 0.0

    # 顶层字段（非 items）
    for fname, w in TOP_WEIGHTS.items():
        if fname == "items":
            continue
        tv = truth.get(fname)
        pv = _parsed_top_value(parsed, fname)
        matched = _values_match(tv, pv)
        ws = w if matched else 0.0
        all_field_scores.append(
            FieldScore(
                field_path=fname,
                truth_value=str(tv) if tv is not None else None,
                parsed_value=str(pv) if pv is not None else None,
                matched=matched,
                weight=w,
                weighted_score=ws,
            )
        )
        total_weight += w
        total_weighted_score += ws

    # 明细行
    truth_items: list[dict] = truth.get("items") or []
    parsed_items: list[dict] = parsed.get("items") or []
    items_weight = TOP_WEIGHTS["items"]
    item_score, item_field_scores = _score_items(truth_items, parsed_items)
    all_field_scores.extend(item_field_scores)
    total_weight += items_weight
    total_weighted_score += items_weight * item_score

    overall = total_weighted_score / total_weight if total_weight else 0.0
    return ScoringResult(
        overall_score=round(overall, 4),
        field_scores=all_field_scores,
        item_count_truth=len(truth_items),
        item_count_parsed=len(parsed_items),
    )


def correction_candidates(
    parser_result_json: str | None,
    ground_truth_json: str,
) -> list[dict[str, str | None]]:
    """Return reviewed parser differences for the training library only.

    This is deliberately a pure calculation: callers decide whether to persist
    the resulting correction logs, and it never creates an Order.
    """
    result = score_sample(parser_result_json, ground_truth_json)
    candidates = [
        {
            "field_path": field.field_path,
            "parser_value": field.parsed_value,
            "corrected_value": field.truth_value,
        }
        for field in result.field_scores
        if not field.matched and _normalize(field.truth_value)
    ]
    try:
        truth = json.loads(ground_truth_json)
    except (json.JSONDecodeError, ValueError):
        return candidates
    try:
        parsed = json.loads(parser_result_json or "{}")
    except (json.JSONDecodeError, ValueError):
        parsed = {}

    truth_items = truth.get("items") or []
    parsed_items = parsed.get("items") or []
    if len(truth_items) != len(parsed_items):
        candidates.append(
            {
                "field_path": "items.length",
                "parser_value": str(len(parsed_items)),
                "corrected_value": str(len(truth_items)),
            }
        )
    for index, truth_item in enumerate(truth_items):
        truth_unit = truth_item.get("unit")
        if not _normalize(truth_unit):
            continue
        parsed_unit = parsed_items[index].get("unit") if index < len(parsed_items) else None
        if not _values_match(truth_unit, parsed_unit):
            candidates.append(
                {
                    "field_path": f"items[{index}].unit",
                    "parser_value": str(parsed_unit) if parsed_unit is not None else None,
                    "corrected_value": str(truth_unit),
                }
            )
    return candidates


# ---------------------------------------------------------------------------
# 统计汇总（供管理看板）
# ---------------------------------------------------------------------------

@dataclass
class ScoreStats:
    sample_count: int = 0
    labeled_count: int = 0
    avg_score: float | None = None
    high_count: int = 0   # score >= 0.9
    mid_count: int = 0    # 0.7 <= score < 0.9
    low_count: int = 0    # score < 0.7
    # 高频出错字段 top-5（field_path → 出错次数）
    top_error_fields: list[tuple[str, int]] = field(default_factory=list)


def compute_stats(samples: list) -> ScoreStats:
    """
    从 ORM 样本列表计算汇总统计。

    Parameters
    ----------
    samples : list of PdfOrderTrainingSample ORM 对象
    """
    from collections import Counter

    stats = ScoreStats(sample_count=len(samples))
    scored = [s for s in samples if s.score is not None]
    stats.labeled_count = len([s for s in samples if s.parse_status != "pending"])

    if scored:
        scores = [s.score for s in scored]
        stats.avg_score = round(sum(scores) / len(scores), 4)
        stats.high_count = sum(1 for sc in scores if sc >= 0.9)
        stats.mid_count = sum(1 for sc in scores if 0.7 <= sc < 0.9)
        stats.low_count = sum(1 for sc in scores if sc < 0.7)

    # 统计高频出错字段（从 correction_logs 聚合）
    error_counter: Counter[str] = Counter()
    for s in samples:
        for log in (s.corrections or []):
            error_counter[log.field_path] += 1
    stats.top_error_fields = error_counter.most_common(5)

    return stats
