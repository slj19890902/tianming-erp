from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.customer import Customer
from app.models.pdf_training import PdfOrderCustomerTemplate, PdfOrderTrainingSample
from app.core.uat_isolation import UatIsolationError, assert_uat_managed_path
from app.services.order_pdf_import import file_sha256, match_import_draft
from app.services.pdf_parse_pipeline import parse_pdf_bytes
from app.services.pdf_scoring import (
    parsed_item_value,
    parsed_top_value,
    score_sample,
    values_match,
)


GAOTAI_TEMPLATE_RULE = {
    "customer_name": "苏州高泰电子技术股份有限公司",
    "customer_type": "gaotai",
    "aliases": ["高泰电子", "苏州高泰"],
    "keywords": ["采购合同", "苏州高泰电子技术股份有限公司"],
    "order_no_labels": ["合同号/P O", "合同号", "PO"],
    "delivery_date_labels": ["交货期", "交期"],
    "item_code_rules": [
        {
            "name": "gaotai_3d_code",
            "pattern": r"^30(\d{5})$",
            "replace": r"3D\1",
            "reason": "高泰存货编码应为 3D + 5位数字，OCR 易把 D 识别成 0",
        }
    ],
    "item_columns": {
        "product_code": ["产品编号", "产品编码", "存货编码"],
        "product_name": ["名称", "产品名称"],
        "spec": ["规格"],
        "quantity": ["数量"],
        "unit": ["单位"],
        "unit_price": ["单价", "单价(含税)"],
        "amount": ["金额"],
        "delivery_date": ["交货期"],
    },
    "_source": "builtin",
}


def _json_dict(raw: str | None) -> tuple[dict, list[str]]:
    if not raw:
        return {}, []
    try:
        data = json.loads(raw)
    except (TypeError, ValueError) as error:
        return {}, [f"字段映射 JSON 无效：{error}"]
    if not isinstance(data, dict):
        return {}, ["字段映射 JSON 必须是对象。"]
    return data, []


def _template_rule(
    template: PdfOrderCustomerTemplate,
    customer: Customer | None,
) -> dict:
    payload, configuration_errors = _json_dict(template.column_map_json)
    if "field_mapping" in payload:
        field_mapping = payload.get("field_mapping")
    elif "item_field_map" in payload:
        field_mapping = payload.get("item_field_map")
    else:
        field_mapping = None
    if field_mapping is None:
        # Backward compatibility with the model's original documented shape:
        # {"product_code": 1, "quantity": 4}.  Metadata keys remain outside
        # the executable mapping.
        supported_fields = {
            "line_no", "product_code", "product_name", "spec",
            "specification", "quantity", "unit", "unit_price", "amount",
            "delivery_date", "production_notes", "reference_product_code",
        }
        field_mapping = {
            key: value
            for key, value in payload.items()
            if key in supported_fields
            and isinstance(value, (str, int))
            and not isinstance(value, bool)
        }
    elif not isinstance(field_mapping, dict):
        configuration_errors.append("field_mapping 必须是对象。")
        field_mapping = {}
    return {
        "template_id": template.id,
        "template_name": template.template_name,
        "customer_id": template.customer_id,
        "customer_name": payload.get("customer_name") or (customer.name if customer else None),
        "customer_type": payload.get("customer_type"),
        "template_customer_valid": (
            bool(customer and customer.is_active and customer.status == "active")
            if template.customer_id is not None else None
        ),
        "customer_name_pattern": template.customer_name_pattern,
        "order_no_pattern": template.order_no_pattern,
        "date_pattern": template.date_pattern,
        "item_row_pattern": template.item_row_pattern,
        "aliases": payload.get("aliases") or [],
        "keywords": payload.get("keywords") or [],
        "item_code_rules": payload.get("item_code_rules") or [],
        "item_columns": payload.get("item_columns") or {},
        "field_mapping": field_mapping,
        "configuration_errors": configuration_errors,
        "_source": "database",
    }


def _append_builtin_gaotai(rules: list[dict]) -> list[dict]:
    has_gaotai = any(
        "高泰" in " ".join(
            str(part or "")
            for part in (
                rule.get("customer_name"),
                rule.get("template_name"),
                rule.get("customer_name_pattern"),
            )
        )
        for rule in rules
    )
    if not has_gaotai:
        rules.append(dict(GAOTAI_TEMPLATE_RULE))
    return rules


def _rules_for_templates(
    db: Session,
    templates: list[PdfOrderCustomerTemplate],
) -> list[dict]:
    customer_ids = {template.customer_id for template in templates if template.customer_id is not None}
    customers = {
        customer.id: customer
        for customer in db.execute(select(Customer).where(Customer.id.in_(customer_ids))).scalars()
    } if customer_ids else {}
    return _append_builtin_gaotai(
        [_template_rule(template, customers.get(template.customer_id)) for template in templates]
    )


def load_active_pdf_template_rules(db: Session) -> list[dict]:
    """Load only lifecycle-active templates; is_active is compatibility-only."""
    rows = db.execute(
        select(PdfOrderCustomerTemplate)
        .where(PdfOrderCustomerTemplate.status == "active")
        .order_by(
            PdfOrderCustomerTemplate.customer_id,
            PdfOrderCustomerTemplate.version.desc(),
            PdfOrderCustomerTemplate.id.desc(),
        )
    ).scalars().all()
    return _rules_for_templates(db, rows)


def load_candidate_pdf_template_rules(
    db: Session,
    candidate: PdfOrderCustomerTemplate,
) -> list[dict]:
    """Return the active rule set with this candidate replacing its customer rule."""
    if candidate.customer_id is None:
        raise ValueError("候选模板必须绑定客户，才能进行激活验证")
    with db.no_autoflush:
        active = db.execute(
            select(PdfOrderCustomerTemplate)
            .where(
                PdfOrderCustomerTemplate.status == "active",
                PdfOrderCustomerTemplate.customer_id != candidate.customer_id,
            )
            .order_by(PdfOrderCustomerTemplate.customer_id, PdfOrderCustomerTemplate.version.desc())
        ).scalars().all()
        active.append(candidate)
        return _rules_for_templates(db, active)


# The required and strict lists intentionally share one source of truth.  A
# field accepted as part of gold truth must not silently disappear from the
# activation gate.
GOLD_REQUIRED_TOP_FIELDS = ("order_no", "customer_name")
GOLD_REQUIRED_ITEM_FIELDS = (
    "line_no",
    "product_code",
    "product_name",
    "spec",
    "quantity",
    "unit",
    "unit_price",
    "amount",
)
GOLD_CRITICAL_TOP_FIELDS = GOLD_REQUIRED_TOP_FIELDS
GOLD_CRITICAL_ITEM_FIELDS = GOLD_REQUIRED_ITEM_FIELDS
GOLD_ADDITIONAL_STRICT_ITEM_FIELDS = ("delivery_date",)
GOLD_STRICT_ITEM_FIELDS = (
    *GOLD_CRITICAL_ITEM_FIELDS,
    *GOLD_ADDITIONAL_STRICT_ITEM_FIELDS,
)


def _missing(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def parse_and_validate_gold_ground_truth(raw: str | None) -> dict:
    """Validate the minimum gold contract without accepting implicit defaults."""
    try:
        payload = json.loads(raw or "")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"ground_truth_json 格式错误: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("ground_truth_json 必须是 JSON 对象")
    missing_top = [
        field for field in GOLD_REQUIRED_TOP_FIELDS if _missing(payload.get(field))
    ]
    if missing_top:
        raise ValueError(f"gold ground_truth 缺少字段: {', '.join(missing_top)}")
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        raise ValueError("gold ground_truth 至少需要一条 items 明细")
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"items[{index}] 必须是对象")
        missing_fields = [
            field for field in GOLD_REQUIRED_ITEM_FIELDS if _missing(item.get(field))
        ]
        if missing_fields:
            raise ValueError(
                f"items[{index}] 缺少字段: {', '.join(missing_fields)}"
            )
    return payload


def read_and_verify_sample_pdf(sample: PdfOrderTrainingSample) -> bytes:
    """Read an immutable sample source only after checking its recorded digest."""
    if not sample.file_path:
        raise ValueError("样本未保留原始 PDF，不能作为金样本")
    try:
        path = assert_uat_managed_path(
            sample.file_path,
            "ERP_PDF_TRAINING_DIR",
            label="PDF training sample",
        )
    except UatIsolationError as error:
        raise ValueError("样本原始 PDF 不属于当前隔离 UAT") from error
    if not path.is_file():
        raise ValueError("样本原始 PDF 文件不存在")
    content = path.read_bytes()
    if file_sha256(content) != sample.file_sha256:
        raise ValueError("样本原始 PDF 的 SHA-256 与记录不一致")
    return content


def _uses_legacy_ordinal_line_numbers(truth: dict, parsed: dict) -> bool:
    """Recognize labels saved by the old UI that rewrote source rows to 1..N.

    The compatibility applies only when every product code still matches in
    order and the parsed source line numbers are unique.  New labels preserve
    the real PDF line number and therefore do not need this alias.
    """

    truth_items = truth.get("items") or []
    parsed_items = parsed.get("items") or []
    if not truth_items or len(truth_items) != len(parsed_items):
        return False
    try:
        truth_lines = [int(item.get("line_no")) for item in truth_items]
        parsed_lines = [int(item.get("line_no")) for item in parsed_items]
    except (TypeError, ValueError):
        return False
    return (
        truth_lines == list(range(1, len(truth_items) + 1))
        and parsed_lines != truth_lines
        and len(set(parsed_lines)) == len(parsed_lines)
        and all(
            values_match(
                truth_item.get("product_code"),
                parsed_item_value(parsed_item, "product_code"),
            )
            for truth_item, parsed_item in zip(truth_items, parsed_items, strict=True)
        )
    )


def _strict_gold_matches(truth: dict, parsed: dict) -> dict[str, bool]:
    checks: dict[str, bool] = {
        field: values_match(
            truth.get(field), parsed_top_value(parsed, field)
        )
        for field in GOLD_CRITICAL_TOP_FIELDS
    }
    truth_items = truth.get("items") or []
    parsed_items = parsed.get("items") or []
    legacy_ordinal_lines = _uses_legacy_ordinal_line_numbers(truth, parsed)
    checks["items.count"] = len(truth_items) == len(parsed_items)
    for index, truth_item in enumerate(truth_items):
        parsed_item = parsed_items[index] if index < len(parsed_items) else {}
        for field in GOLD_CRITICAL_ITEM_FIELDS:
            if field == "line_no" and legacy_ordinal_lines:
                checks[f"items[{index}].{field}"] = True
                continue
            checks[f"items[{index}].{field}"] = values_match(
                truth_item.get(field), parsed_item_value(parsed_item, field)
            )
        for field in GOLD_ADDITIONAL_STRICT_ITEM_FIELDS:
            if _missing(truth_item.get(field)):
                continue
            checks[f"items[{index}].{field}"] = values_match(
                truth_item.get(field), parsed_item_value(parsed_item, field)
            )
    return checks


def _activation_input_fingerprint(
    candidate: PdfOrderCustomerTemplate,
    samples: list[PdfOrderTrainingSample],
) -> str:
    payload = {
        "template": {
            "id": candidate.id,
            "customer_id": candidate.customer_id,
            "version": candidate.version,
            "status": candidate.status,
            "order_no_pattern": candidate.order_no_pattern,
            "date_pattern": candidate.date_pattern,
            "item_row_pattern": candidate.item_row_pattern,
            "customer_name_pattern": candidate.customer_name_pattern,
            "column_map_json": candidate.column_map_json,
        },
        "samples": [
            {
                "id": sample.id,
                "file_sha256": sample.file_sha256,
                "ground_truth_sha256": hashlib.sha256(
                    (sample.ground_truth_json or "").encode("utf-8")
                ).hexdigest(),
                "gold_review_status": sample.gold_review_status,
            }
            for sample in samples
        ],
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def activation_dry_run(
    db: Session,
    candidate: PdfOrderCustomerTemplate,
) -> dict:
    """Read-only, fail-closed activation evidence for one draft template.

    The function intentionally never adds, flushes, commits, or logs.  The
    no_autoflush scope also prevents unrelated pending ORM state being flushed
    during its queries.
    """
    evidence: dict[str, Any] = {
        "template_id": candidate.id,
        "customer_id": candidate.customer_id,
        "template_version": candidate.version,
        "parser_output_layer": "order_preview_matched",
        "input_fingerprint": None,
        "sample_count": 0,
        "sample_results": [],
        "average_score": None,
        "reasons": [],
        "can_activate": False,
    }
    if candidate.status != "draft":
        evidence["reasons"].append("只有 draft 模板可以进行激活验证")
    if candidate.customer_id is None:
        evidence["reasons"].append("模板未绑定客户，不能进行金样本验证")
    if evidence["reasons"]:
        return _finalize_evidence(evidence)

    with db.no_autoflush:
        samples = db.execute(
            select(PdfOrderTrainingSample)
            .where(
                PdfOrderTrainingSample.customer_id == candidate.customer_id,
                PdfOrderTrainingSample.parse_status == "reviewed",
                PdfOrderTrainingSample.gold_review_status == "approved",
            )
            .order_by(PdfOrderTrainingSample.id)
        ).scalars().all()
        rules = load_candidate_pdf_template_rules(db, candidate)

    evidence["sample_count"] = len(samples)
    evidence["input_fingerprint"] = _activation_input_fingerprint(candidate, samples)
    if len(samples) < 3:
        evidence["reasons"].append("至少需要 3 份同客户已批准金样本")

    scores: list[float] = []
    for sample in samples:
        sample_evidence: dict[str, Any] = {
            "sample_id": sample.id,
            "file_sha256": sample.file_sha256,
            "ok": False,
            "reasons": [],
            "strict_checks": {},
            "legacy_ordinal_line_numbers": False,
            "score": None,
        }
        try:
            truth = parse_and_validate_gold_ground_truth(sample.ground_truth_json)
            content = read_and_verify_sample_pdf(sample)
            outcome = parse_pdf_bytes(content, sample.file_name, rules)
            parsed = match_import_draft(
                db,
                outcome.draft,
                customer_id=candidate.customer_id,
            )
            route = parsed.get("customer_route") or {}
            if not (
                route.get("status") == "locked"
                and route.get("template_customer_id") == candidate.customer_id
            ):
                sample_evidence["reasons"].append("客户路由未锁定到候选模板客户")
            if (
                outcome.parse_method in {"ocr_unavailable", "ocr_failed", "failed"}
                or outcome.ocr_method in {"ocr_unavailable", "ocr_failed"}
            ):
                sample_evidence["reasons"].append(
                    f"OCR/解析不可用: {outcome.ocr_method or outcome.parse_method}"
                )
            score = score_sample(json.dumps(parsed, ensure_ascii=False, default=str), sample.ground_truth_json)
            sample_evidence["score"] = score.overall_score
            if score.error:
                sample_evidence["reasons"].append(score.error)
            elif score.overall_score < 0.90:
                sample_evidence["reasons"].append("样本评分低于 0.90")
            strict_checks = _strict_gold_matches(truth, parsed)
            sample_evidence["strict_checks"] = strict_checks
            sample_evidence["legacy_ordinal_line_numbers"] = (
                _uses_legacy_ordinal_line_numbers(truth, parsed)
            )
            failed_checks = [name for name, passed in strict_checks.items() if not passed]
            if failed_checks:
                sample_evidence["reasons"].append(
                    "关键字段未 100% 命中: " + ", ".join(failed_checks)
                )
            if not sample_evidence["reasons"]:
                sample_evidence["ok"] = True
            scores.append(score.overall_score)
        except Exception as exc:
            sample_evidence["reasons"].append(str(exc))
        evidence["sample_results"].append(sample_evidence)

    if scores:
        evidence["average_score"] = round(sum(scores) / len(scores), 4)
        if evidence["average_score"] < 0.95:
            evidence["reasons"].append("金样本平均评分低于 0.95")
    else:
        evidence["reasons"].append("没有可计算评分的金样本")
    if any(not result["ok"] for result in evidence["sample_results"]):
        evidence["reasons"].append("至少一份金样本未通过只读验证")
    evidence["can_activate"] = not evidence["reasons"]
    return _finalize_evidence(evidence)


def _finalize_evidence(evidence: dict) -> dict:
    canonical = json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    evidence["evidence_hash"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return evidence
