"""
Phase 18 / v0.18.0: PDF 订单识别训练样本库 REST API。

端点（均挂载在 /api/pdf-training/ 前缀下）：

批次管理
  GET    /batches                   → 批次列表
  POST   /batches                   → 新建批次

样本管理
  GET    /samples                   → 样本列表（分页 + 过滤）
  POST   /samples/upload            → 上传 PDF（运行解析 + 存样本）
  GET    /samples/{id}              → 样本详情
  PUT    /samples/{id}/ground-truth → 写入人工标注
  POST   /samples/{id}/score        → 计算并保存评分
  DELETE /samples/{id}              → 删除样本（仅管理员）

客户模板
  GET    /templates                 → 模板列表
  POST   /templates                 → 创建模板
  PUT    /templates/{id}            → 更新模板
  DELETE /templates/{id}            → 删除模板

统计
  GET    /stats                     → 全局统计（样本数、标注数、平均分、高频出错字段）
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from pydantic import BaseModel, field_serializer
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import PermissionChecker, get_db
from app.core.time_contract import utc_naive_to_api, utc_now_naive
from app.core.uat_isolation import UatIsolationError, assert_uat_managed_path
from app.models.audit import OperationLog
from app.models.customer import Customer
from app.models.pdf_training import (
    PdfOrderCorrectionLog,
    PdfOrderCustomerTemplate,
    PdfOrderTrainingBatch,
    PdfOrderTrainingSample,
)
from app.models.user import User
from app.services.order_pdf_import import (
    TEMPLATE_CAPTURE_GROUP_MAX,
    TEMPLATE_ITEM_FIELDS,
    TEMPLATE_PATTERN_MAX_LENGTH,
    file_sha256,
    match_import_draft,
    template_pattern_safety_error,
)
from app.services.pdf_customer_templates import (
    activation_dry_run,
    load_active_pdf_template_rules,
    parse_and_validate_gold_ground_truth,
    read_and_verify_sample_pdf,
)
from app.services.pdf_ocr import ocr_available, ocr_engine_name
from app.services.pdf_parse_pipeline import PdfParsePipelineError, parse_pdf_bytes
from app.services.pdf_scoring import compute_stats, correction_candidates, score_sample
from app.services.secure_uploads import (
    PDF_POLICY,
    UploadValidationError,
    read_validated_upload,
)

router = APIRouter()

LEARNING_DRAFT_KIND = "approved_gold_rule_draft"
LEARNING_GENERATOR_VERSION = 1

require_pdf_training_view = PermissionChecker("pdf_training.view")
require_pdf_training_manage = PermissionChecker("pdf_training.manage")

# 训练样本本地存储目录（使用绝对路径，避免因启动目录不同而写错位置）
# 本文件位于 app/api/pdf_training.py，parents[2] = 项目根目录
_SAMPLE_DIR = Path(
    os.getenv(
        "ERP_PDF_TRAINING_DIR",
        str(Path(__file__).resolve().parents[2] / "data" / "pdf_training_samples"),
    )
).resolve(strict=False)


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return utc_now_naive()


def _log(db: Session, user: User, action: str, detail: str) -> None:
    # action max 30 chars → use short verb; resource=entity; description=detail
    short_action = action.split(".")[-1][:30]          # e.g. "create" / "upload" / "delete"
    resource = action[:100]                             # full dotted path as resource
    db.add(
        OperationLog(
            user_id=user.id,
            username=user.username,
            action=short_action,
            resource=resource,
            description=detail,
        )
    )


def _parse_sample_id(sample_id: int | str | None) -> int:
    raw = "" if sample_id is None else str(sample_id).strip()
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="样本ID不能为空，请刷新样本列表后重试。",
        )
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"样本ID无效：{raw}。请从样本列表重新进入详情。",
        ) from exc
    if value <= 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="样本ID必须是大于 0 的整数。",
        )
    return value


def _get_sample_or_404(
    db: Session,
    sample_id: int | str | None,
) -> tuple[PdfOrderTrainingSample, int]:
    safe_sample_id = _parse_sample_id(sample_id)
    sample = db.get(PdfOrderTrainingSample, safe_sample_id)
    if sample is None:
        raise HTTPException(status_code=404, detail="样本不存在")
    return sample, safe_sample_id


def _parse_pdf_sample_content(
    db: Session,
    content: bytes,
    source_name: str,
    customer_id: int | None = None,
) -> dict:
    template_rules = load_active_pdf_template_rules(db)
    try:
        outcome = parse_pdf_bytes(content, source_name, template_rules)
        extracted_text = outcome.extracted_text
        ocr_text_raw = outcome.ocr_text_raw
        draft = (
            match_import_draft(db, outcome.draft, customer_id=customer_id)
            if customer_id is not None
            else outcome.draft
        )
        parser_result_json = json.dumps(draft, ensure_ascii=False, default=str)
        parse_method = outcome.parse_method
        text_quality = outcome.text_quality
    except PdfParsePipelineError as error:
        extracted_text = error.extracted_text
        ocr_text_raw = error.ocr_text_raw
        parser_result_json = None
        parse_method = error.parse_method
        text_quality = error.text_quality

    return {
        "extracted_text": extracted_text,
        "ocr_text_raw": ocr_text_raw,
        "parser_result_json": parser_result_json,
        "parse_method": parse_method,
        "text_quality": text_quality,
    }


def _refresh_stored_sample_parse(
    db: Session,
    sample: PdfOrderTrainingSample,
    content: bytes | None = None,
) -> None:
    source = content if content is not None else read_and_verify_sample_pdf(sample)
    parse_payload = _parse_pdf_sample_content(
        db,
        source,
        sample.file_name or "sample.pdf",
        customer_id=sample.customer_id,
    )
    sample.extracted_text = parse_payload["extracted_text"]
    sample.ocr_text_raw = parse_payload["ocr_text_raw"]
    sample.parser_result_json = parse_payload["parser_result_json"]
    sample.parse_method = parse_payload["parse_method"]
    sample.score = (
        score_sample(sample.parser_result_json, sample.ground_truth_json).overall_score
        if sample.ground_truth_json
        else None
    )


# ---------------------------------------------------------------------------
# Sample source persistence
# ---------------------------------------------------------------------------

def _ensure_sample_pdf(
    sample: PdfOrderTrainingSample,
    content: bytes,
    source_name: str,
) -> bool:
    """Persist the immutable source PDF once, repairing a missing/stale local copy."""
    expected_sha = sample.file_sha256
    if sample.file_path:
        try:
            current_path = assert_uat_managed_path(
                sample.file_path,
                "ERP_PDF_TRAINING_DIR",
                label="PDF training sample",
            )
            if current_path.is_file() and file_sha256(current_path.read_bytes()) == expected_sha:
                return False
        except (OSError, UatIsolationError):
            pass

    _SAMPLE_DIR.mkdir(parents=True, exist_ok=True)
    del source_name
    destination = _SAMPLE_DIR / f"{uuid4().hex}.pdf"
    try:
        already_saved = destination.is_file() and file_sha256(destination.read_bytes()) == expected_sha
    except OSError:
        already_saved = False
    if not already_saved:
        destination.write_bytes(content)
    new_path = str(destination)
    path_changed = sample.file_path != new_path
    sample.file_path = new_path
    return path_changed or not already_saved


# ---------------------------------------------------------------------------
# Pydantic schema
# ---------------------------------------------------------------------------

class BatchOut(BaseModel):
    id: int
    batch_name: str
    description: str | None
    created_by: str | None
    created_at: datetime
    sample_count: int

    model_config = {"from_attributes": True}

    @field_serializer("created_at")
    def serialize_created_at(self, value: datetime) -> str:
        return utc_naive_to_api(value)


class BatchCreate(BaseModel):
    batch_name: str
    description: str | None = None
    notes: str | None = None


class SampleSummary(BaseModel):
    id: int
    batch_id: int | None
    customer_id: int | None
    file_name: str
    file_sha256: str
    parse_status: str
    parse_method: str
    score: float | None
    gold_review_status: str
    gold_reviewed_at: datetime | None
    gold_reviewed_by: str | None
    created_at: datetime
    labeled_at: datetime | None

    model_config = {"from_attributes": True}

    @field_serializer("created_at", "labeled_at", "gold_reviewed_at")
    def serialize_timestamps(self, value: datetime | None) -> str | None:
        return utc_naive_to_api(value) if value else None


class SampleDetail(SampleSummary):
    parser_result_json: str | None
    ground_truth_json: str | None
    extracted_text: str | None
    ocr_text_raw: str | None
    notes: str | None
    gold_review_note: str | None
    learning_draft: dict | None = None
    learning_replay: dict | None = None
    learning_message: str | None = None


class GroundTruthPayload(BaseModel):
    ground_truth_json: str         # JSON 字符串
    notes: str | None = None


class GoldReviewPayload(BaseModel):
    status: Literal["approved", "rejected"]
    note: str | None = None


class TemplateOut(BaseModel):
    id: int
    customer_id: int | None
    template_name: str
    order_no_pattern: str | None
    date_pattern: str | None
    item_row_pattern: str | None
    customer_name_pattern: str | None
    column_map_json: str | None
    is_active: bool
    status: str
    version: int
    supersedes_template_id: int | None
    created_at: datetime
    updated_at: datetime | None
    updated_by: str | None
    activated_at: datetime | None
    activated_by: str | None
    activation_reason: str | None
    evidence_json: str | None
    retired_at: datetime | None
    retired_by: str | None
    retired_reason: str | None
    notes: str | None

    model_config = {"from_attributes": True}

    @field_serializer("created_at", "updated_at", "activated_at", "retired_at")
    def serialize_timestamps(self, value: datetime | None) -> str | None:
        return utc_naive_to_api(value) if value else None


class TemplateCreate(BaseModel):
    customer_id: int | None = None
    template_name: str
    order_no_pattern: str | None = None
    date_pattern: str | None = None
    item_row_pattern: str | None = None
    customer_name_pattern: str | None = None
    column_map_json: str | None = None
    notes: str | None = None


class TemplateClonePayload(BaseModel):
    template_name: str | None = None
    notes: str | None = None


class TemplateActivationPayload(BaseModel):
    reason: str | None = None


class TemplateRetirePayload(BaseModel):
    reason: str | None = None


def _apply_ground_truth(
    db: Session,
    sample: PdfOrderTrainingSample,
    sample_id: int,
    payload: GroundTruthPayload,
    user: User,
) -> bool:
    """Apply the shared annotation workflow and return whether it changed the sample."""
    try:
        json.loads(payload.ground_truth_json)
    except (json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"ground_truth_json 格式错误: {exc}",
        ) from exc

    ground_truth_changed = sample.ground_truth_json != payload.ground_truth_json
    notes_changed = payload.notes is not None and sample.notes != payload.notes
    if not ground_truth_changed and not notes_changed:
        return False

    sample.ground_truth_json = payload.ground_truth_json
    sample.parse_status = "labeled"
    sample.labeled_at = _now()
    sample.labeled_by = user.username
    if payload.notes is not None:
        sample.notes = payload.notes

    if ground_truth_changed:
        sample.gold_review_status = "pending"
        sample.gold_reviewed_at = None
        sample.gold_reviewed_by = None
        sample.gold_review_note = None
        _invalidate_learning_replay(db, sample.customer_id, user)

    score_result = score_sample(sample.parser_result_json, sample.ground_truth_json)
    sample.score = score_result.overall_score
    auto_note = "ground_truth_form_auto_diff"
    db.query(PdfOrderCorrectionLog).filter(
        PdfOrderCorrectionLog.sample_id == sample_id,
        PdfOrderCorrectionLog.note == auto_note,
    ).delete(synchronize_session=False)
    for difference in correction_candidates(
        sample.parser_result_json,
        sample.ground_truth_json,
    ):
        db.add(
            PdfOrderCorrectionLog(
                sample_id=sample_id,
                field_path=str(difference["field_path"]),
                parser_value=difference["parser_value"],
                corrected_value=difference["corrected_value"],
                corrected_by=user.username,
                note=auto_note,
            )
        )

    _log(
        db,
        user,
        "pdf_training.sample.label",
        f"标注样本 {sample_id}，评分={score_result.overall_score:.3f}",
    )
    return True


def _validate_template_rule_payload(payload: TemplateCreate) -> None:
    compiled_item_pattern: re.Pattern | None = None
    for field_name, label in (
        ("customer_name_pattern", "客户名称规则"),
        ("order_no_pattern", "订单号规则"),
        ("date_pattern", "日期规则"),
        ("item_row_pattern", "明细行规则"),
    ):
        pattern = str(getattr(payload, field_name) or "").strip()
        if not pattern:
            continue
        if len(pattern) > TEMPLATE_PATTERN_MAX_LENGTH:
            raise HTTPException(status_code=422, detail=f"{label}过长，请缩短后再保存。")
        try:
            compiled = re.compile(pattern, re.IGNORECASE | re.MULTILINE)
        except (RecursionError, re.error) as error:
            raise HTTPException(status_code=422, detail=f"{label}无效：{error}") from error
        if compiled.groups > TEMPLATE_CAPTURE_GROUP_MAX:
            raise HTTPException(
                status_code=422,
                detail=f"{label}捕获组超过 {TEMPLATE_CAPTURE_GROUP_MAX} 个。",
            )
        safety_error = template_pattern_safety_error(pattern)
        if safety_error:
            raise HTTPException(
                status_code=422,
                detail=f"{label}存在不安全正则结构：{safety_error}。",
            )
        if field_name == "item_row_pattern":
            compiled_item_pattern = compiled

    raw_mapping = str(payload.column_map_json or "").strip()
    if not raw_mapping:
        return
    if len(raw_mapping.encode("utf-8")) > 20_000:
        raise HTTPException(status_code=422, detail="字段映射 JSON 过大，请精简后再保存。")
    try:
        mapping_payload = json.loads(raw_mapping)
    except (TypeError, ValueError) as error:
        raise HTTPException(status_code=422, detail=f"字段映射 JSON 无效：{error}") from error
    if not isinstance(mapping_payload, dict):
        raise HTTPException(status_code=422, detail="字段映射 JSON 必须是对象。")

    if "field_mapping" in mapping_payload:
        field_mapping = mapping_payload.get("field_mapping")
    elif "item_field_map" in mapping_payload:
        field_mapping = mapping_payload.get("item_field_map")
    else:
        supported = {*TEMPLATE_ITEM_FIELDS, "specification", "production_notes", "reference_product_code"}
        field_mapping = {
            key: value for key, value in mapping_payload.items() if key in supported
        }
    if not isinstance(field_mapping, dict):
        raise HTTPException(status_code=422, detail="field_mapping 必须是对象。")
    supported = {*TEMPLATE_ITEM_FIELDS, "specification", "production_notes", "reference_product_code"}
    for field_name, selector in field_mapping.items():
        if field_name not in supported:
            raise HTTPException(status_code=422, detail=f"字段映射不支持字段：{field_name}")
        if not isinstance(selector, (str, int)) or isinstance(selector, bool):
            raise HTTPException(status_code=422, detail=f"字段 {field_name} 的捕获组必须是名称或序号。")
        selector_text = str(selector).strip()
        if not selector_text:
            raise HTTPException(status_code=422, detail=f"字段 {field_name} 的捕获组不能为空。")
        if compiled_item_pattern is None:
            continue
        if selector_text.isdigit():
            group_index = int(selector_text)
            if group_index < 1 or group_index > compiled_item_pattern.groups:
                raise HTTPException(
                    status_code=422,
                    detail=f"字段 {field_name} 指向不存在的捕获组 {group_index}。",
                )
        elif selector_text not in compiled_item_pattern.groupindex:
            raise HTTPException(
                status_code=422,
                detail=f"字段 {field_name} 指向不存在的命名组 {selector_text}。",
            )


class ScoreOut(BaseModel):
    sample_id: int
    overall_score: float
    item_count_truth: int
    item_count_parsed: int
    error: str | None
    field_scores: list[dict]


class StatsOut(BaseModel):
    sample_count: int
    labeled_count: int
    avg_score: float | None
    high_count: int
    mid_count: int
    low_count: int
    top_error_fields: list[list]


# ---------------------------------------------------------------------------
# 批次
# ---------------------------------------------------------------------------

@router.get("/batches", response_model=list[BatchOut])
def list_batches(
    db: Session = Depends(get_db),
    _user: User = Depends(require_pdf_training_view),
):
    batches = (
        db.query(PdfOrderTrainingBatch)
        .order_by(PdfOrderTrainingBatch.created_at.desc())
        .all()
    )
    result = []
    for b in batches:
        cnt = db.query(func.count(PdfOrderTrainingSample.id)).filter(
            PdfOrderTrainingSample.batch_id == b.id
        ).scalar() or 0
        result.append(
            BatchOut(
                id=b.id,
                batch_name=b.batch_name,
                description=b.description,
                created_by=b.created_by,
                created_at=b.created_at,
                sample_count=cnt,
            )
        )
    return result


@router.post("/batches", response_model=BatchOut, status_code=status.HTTP_201_CREATED)
def create_batch(
    payload: BatchCreate,
    db: Session = Depends(get_db),
    user: User = Depends(require_pdf_training_manage),
):
    batch = PdfOrderTrainingBatch(
        batch_name=payload.batch_name,
        description=payload.description,
        notes=payload.notes,
        created_by=user.username,
    )
    db.add(batch)
    _log(db, user, "pdf_training.batch.create", f"新建批次: {payload.batch_name}")
    db.commit()
    db.refresh(batch)
    return BatchOut(
        id=batch.id,
        batch_name=batch.batch_name,
        description=batch.description,
        created_by=batch.created_by,
        created_at=batch.created_at,
        sample_count=0,
    )


# ---------------------------------------------------------------------------
# 样本列表 + 上传
# ---------------------------------------------------------------------------

@router.get("/samples", response_model=list[SampleSummary])
def list_samples(
    batch_id: int | None = Query(None),
    parse_status: str | None = Query(None),
    customer_id: int | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    _user: User = Depends(require_pdf_training_view),
):
    q = db.query(PdfOrderTrainingSample)
    if batch_id is not None:
        q = q.filter(PdfOrderTrainingSample.batch_id == batch_id)
    if parse_status:
        q = q.filter(PdfOrderTrainingSample.parse_status == parse_status)
    if customer_id is not None:
        q = q.filter(PdfOrderTrainingSample.customer_id == customer_id)
    samples = (
        q.order_by(PdfOrderTrainingSample.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return samples


@router.get("/samples/list", response_model=list[SampleSummary])
def list_samples_legacy(
    batch_id: int | None = Query(None),
    parse_status: str | None = Query(None),
    customer_id: int | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    _user: User = Depends(require_pdf_training_view),
):
    return list_samples(batch_id, parse_status, customer_id, limit, offset, db, _user)


@router.post(
    "/samples/upload",
    response_model=SampleSummary,
    status_code=status.HTTP_201_CREATED,
)
async def upload_sample(
    file: UploadFile = File(...),
    batch_id: int | None = Form(None),
    customer_id: int | None = Form(None),
    notes: str | None = Form(None),
    store_pdf: bool = Form(False),
    db: Session = Depends(get_db),
    user: User = Depends(require_pdf_training_manage),
):
    """上传一个 PDF，立即运行解析器，保存样本记录。

    store_pdf=True 时将 PDF 原文件保存到 data/pdf_training_samples/（仅限本地；
    该目录已被 .gitignore 排除，不会进入 Git 仓库）。
    """
    try:
        upload = await read_validated_upload(file, PDF_POLICY)
    except UploadValidationError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    content = upload.content
    sha = upload.sha256

    # 重复上传检测
    existing = db.query(PdfOrderTrainingSample).filter(
        PdfOrderTrainingSample.file_sha256 == sha
    ).first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"该 PDF 已上传（样本 ID={existing.id}，文件 SHA256 重复）",
        )

    parse_payload = _parse_pdf_sample_content(
        db,
        content,
        upload.original_filename,
        customer_id=customer_id,
    )
    extracted_text = parse_payload["extracted_text"]
    ocr_text_raw = parse_payload["ocr_text_raw"]
    parser_result_json = parse_payload["parser_result_json"]
    parse_method = parse_payload["parse_method"]

    # ── 步骤3：落盘 PDF 原文件 ─────────────────────────────────────────
    file_path: str | None = None
    if store_pdf:
        _SAMPLE_DIR.mkdir(parents=True, exist_ok=True)
        safe_name = f"{uuid4().hex}.pdf"
        dest = _SAMPLE_DIR / safe_name
        dest.write_bytes(content)
        file_path = str(dest)

    sample = PdfOrderTrainingSample(
        batch_id=batch_id,
        customer_id=customer_id,
        file_name=upload.original_filename,
        file_sha256=sha,
        file_path=file_path,
        parser_result_json=parser_result_json,
        extracted_text=extracted_text,
        ocr_text_raw=ocr_text_raw,
        parse_method=parse_method,
        parse_status="pending",
        notes=notes,
    )
    db.add(sample)
    _log(
        db,
        user,
        "pdf_training.sample.upload",
        f"上传样本: {upload.original_filename} (sha={sha[:12]}…, method={parse_method})",
    )
    db.commit()
    db.refresh(sample)
    return sample


# ---------------------------------------------------------------------------
# 样本详情 / 标注 / 评分 / 删除
# ---------------------------------------------------------------------------

@router.post(
    "/samples/submit-correction",
    response_model=SampleDetail,
)
async def submit_correction_sample(
    file: UploadFile = File(...),
    ground_truth_json: str = Form(...),
    customer_id: int | None = Form(None),
    notes: str | None = Form(None),
    db: Session = Depends(get_db),
    user: User = Depends(require_pdf_training_manage),
):
    """保存人工纠正样本；不会创建正式订单或启用客户模板。"""
    try:
        upload = await read_validated_upload(file, PDF_POLICY)
    except UploadValidationError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    if customer_id is not None and db.get(Customer, customer_id) is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="所选客户不存在，请刷新后重新选择。",
        )

    content = upload.content
    sha = upload.sha256
    sample = db.query(PdfOrderTrainingSample).filter(
        PdfOrderTrainingSample.file_sha256 == sha
    ).first()
    created = sample is None
    customer_changed = False
    if sample is not None:
        if (
            sample.customer_id is not None
            and customer_id is not None
            and sample.customer_id != customer_id
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="该 PDF 已绑定其他客户，不能覆盖。",
            )
        if sample.customer_id is None and customer_id is not None:
            sample.customer_id = customer_id
            customer_changed = True
    else:
        parse_payload = _parse_pdf_sample_content(
            db,
            content,
            upload.original_filename,
            customer_id=customer_id,
        )
        sample = PdfOrderTrainingSample(
            customer_id=customer_id,
            file_name=upload.original_filename,
            file_sha256=sha,
            parser_result_json=parse_payload["parser_result_json"],
            extracted_text=parse_payload["extracted_text"],
            ocr_text_raw=parse_payload["ocr_text_raw"],
            parse_method=parse_payload["parse_method"],
            parse_status="pending",
        )
        db.add(sample)
        db.flush()

    if not created:
        parse_payload = _parse_pdf_sample_content(
            db,
            content,
            upload.original_filename,
            customer_id=sample.customer_id,
        )
        sample.parser_result_json = parse_payload["parser_result_json"]
        sample.extracted_text = parse_payload["extracted_text"]
        sample.ocr_text_raw = parse_payload["ocr_text_raw"]
        sample.parse_method = parse_payload["parse_method"]

    file_changed = _ensure_sample_pdf(sample, content, upload.original_filename)
    labeled = _apply_ground_truth(
        db,
        sample,
        sample.id,
        GroundTruthPayload(ground_truth_json=ground_truth_json, notes=notes),
        user,
    )
    if created or customer_changed or file_changed or labeled:
        db.commit()
        db.refresh(sample)
    return _attach_sample_learning_state(db, sample)


@router.get("/samples/detail/{sample_id}", response_model=SampleDetail)
def get_sample_legacy_detail(
    sample_id: str,
    db: Session = Depends(get_db),
    _user: User = Depends(require_pdf_training_view),
):
    sample, _ = _get_sample_or_404(db, sample_id)
    return _attach_sample_learning_state(db, sample)


@router.get("/samples/{sample_id}", response_model=SampleDetail)
def get_sample(
    sample_id: str,
    db: Session = Depends(get_db),
    _user: User = Depends(require_pdf_training_view),
):
    sample, _ = _get_sample_or_404(db, sample_id)
    return _attach_sample_learning_state(db, sample)


@router.put("/samples/{sample_id}/ground-truth", response_model=SampleDetail)
def set_ground_truth(
    sample_id: str,
    payload: GroundTruthPayload,
    db: Session = Depends(get_db),
    user: User = Depends(require_pdf_training_manage),
):
    """写入人工标注，并通过统一纠错流程重新评分。"""
    sample, safe_sample_id = _get_sample_or_404(db, sample_id)
    changed = _apply_ground_truth(db, sample, safe_sample_id, payload, user)
    if changed:
        db.commit()
        db.refresh(sample)
    return _attach_sample_learning_state(db, sample)


@router.post("/samples/{sample_id}/gold-review", response_model=SampleDetail)
def review_gold_sample(
    sample_id: str,
    payload: GoldReviewPayload,
    db: Session = Depends(get_db),
    user: User = Depends(require_pdf_training_manage),
):
    """Approve/reject a sample for activation evidence, fail-closed on approval."""
    sample, safe_sample_id = _get_sample_or_404(db, sample_id)
    if payload.status == "approved":
        if sample.customer_id is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="批准金样本前必须绑定客户，才能生成同客户规则草稿并回放。",
            )
        try:
            parse_and_validate_gold_ground_truth(sample.ground_truth_json)
            content = read_and_verify_sample_pdf(sample)
            _refresh_stored_sample_parse(db, sample, content)
        except (ValueError, OSError) as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"不能批准金样本: {exc}",
            ) from exc

    changed = (
        sample.gold_review_status != payload.status
        or sample.gold_review_note != payload.note
        or sample.parse_status != "reviewed"
    )
    if changed:
        sample.gold_review_status = payload.status
        sample.gold_reviewed_at = _now()
        sample.gold_reviewed_by = user.username
        sample.gold_review_note = payload.note
        sample.parse_status = "reviewed"
        _log(
            db,
            user,
            "pdf_training.sample.gold_review",
            f"金样本复核 {safe_sample_id}: {payload.status}",
        )
    if payload.status == "approved":
        db.flush()
        learning_draft = _ensure_learning_draft(db, sample, user)
        _persist_template_replay(db, learning_draft, user)
    elif sample.customer_id is not None:
        _invalidate_learning_replay(db, sample.customer_id, user)
    db.commit()
    db.refresh(sample)
    return _attach_sample_learning_state(db, sample)


@router.post("/samples/{sample_id}/learning-loop", response_model=SampleDetail)
def run_sample_learning_loop(
    sample_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_pdf_training_manage),
):
    """Idempotently create/refresh a rule draft and persist its gold replay."""
    sample, _ = _get_sample_or_404(db, sample_id)
    if sample.gold_review_status != "approved" or sample.parse_status != "reviewed":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="只有已批准的金样本才能生成规则草稿并回放。",
        )
    if sample.customer_id is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="金样本未绑定客户，不能生成同客户规则草稿。",
        )
    try:
        _refresh_stored_sample_parse(db, sample)
    except (ValueError, OSError) as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"原始 PDF 校验失败，不能刷新学习闭环：{exc}",
        ) from exc
    draft = _ensure_learning_draft(db, sample, user)
    _persist_template_replay(db, draft, user)
    db.commit()
    db.refresh(sample)
    return _attach_sample_learning_state(db, sample)


@router.post("/samples/{sample_id}/score", response_model=ScoreOut)
def compute_sample_score(
    sample_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_pdf_training_manage),
):
    """（重新）计算并保存评分。要求样本已有 ground_truth_json。"""
    sample, safe_sample_id = _get_sample_or_404(db, sample_id)
    if not sample.ground_truth_json:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="样本尚未标注 ground_truth_json，无法评分",
        )

    sr = score_sample(sample.parser_result_json, sample.ground_truth_json)
    sample.score = sr.overall_score
    _log(db, user, "pdf_training.sample.score", f"评分样本 {safe_sample_id}={sr.overall_score:.3f}")
    db.commit()

    return ScoreOut(
        sample_id=safe_sample_id,
        overall_score=sr.overall_score,
        item_count_truth=sr.item_count_truth,
        item_count_parsed=sr.item_count_parsed,
        error=sr.error,
        field_scores=[
            {
                "field_path": fs.field_path,
                "truth_value": fs.truth_value,
                "parsed_value": fs.parsed_value,
                "matched": fs.matched,
                "weight": fs.weight,
                "weighted_score": fs.weighted_score,
            }
            for fs in sr.field_scores
        ],
    )


@router.post("/samples/{sample_id}/reparse", response_model=SampleDetail)
def reparse_sample(
    sample_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_pdf_training_manage),
):
    sample, safe_sample_id = _get_sample_or_404(db, sample_id)
    if not sample.file_path:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="样本原始 PDF 文件不存在，无法重新解析。",
        )
    try:
        content = read_and_verify_sample_pdf(sample)
    except (ValueError, OSError) as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"样本原始 PDF 校验失败，拒绝重新解析：{exc}",
        ) from exc

    _refresh_stored_sample_parse(db, sample, content)
    _invalidate_learning_replay(db, sample.customer_id, user)
    _log(db, user, "pdf_training.sample.reparse", f"重新解析样本 {safe_sample_id}: {sample.file_name}")
    db.commit()
    db.refresh(sample)
    return _attach_sample_learning_state(db, sample)


@router.delete("/samples/{sample_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_sample(
    sample_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_pdf_training_manage),
):
    sample, safe_sample_id = _get_sample_or_404(db, sample_id)
    _invalidate_learning_replay(db, sample.customer_id, user)
    _log(db, user, "pdf_training.sample.delete", f"删除样本 {safe_sample_id}: {sample.file_name}")
    db.delete(sample)
    db.commit()


# ---------------------------------------------------------------------------
# 纠错记录
# ---------------------------------------------------------------------------

class CorrectionCreate(BaseModel):
    field_path: str
    parser_value: str | None = None
    corrected_value: str | None = None
    note: str | None = None


class CorrectionOut(BaseModel):
    id: int
    sample_id: int
    field_path: str
    parser_value: str | None
    corrected_value: str | None
    corrected_by: str | None
    corrected_at: datetime
    note: str | None

    model_config = {"from_attributes": True}

    @field_serializer("corrected_at")
    def serialize_corrected_at(self, value: datetime) -> str:
        return utc_naive_to_api(value)


@router.post(
    "/samples/{sample_id}/corrections",
    response_model=CorrectionOut,
    status_code=status.HTTP_201_CREATED,
)
def add_correction(
    sample_id: str,
    payload: CorrectionCreate,
    db: Session = Depends(get_db),
    user: User = Depends(require_pdf_training_manage),
):
    """为样本新增字段纠错记录（不影响 ground_truth_json，仅记录差异）。"""
    _sample, safe_sample_id = _get_sample_or_404(db, sample_id)
    log = PdfOrderCorrectionLog(
        sample_id=safe_sample_id,
        field_path=payload.field_path,
        parser_value=payload.parser_value,
        corrected_value=payload.corrected_value,
        corrected_by=user.username,
        corrected_at=_now(),
        note=payload.note,
    )
    db.add(log)
    db.commit()
    db.refresh(log)
    return log


@router.get("/samples/{sample_id}/corrections", response_model=list[CorrectionOut])
def list_corrections(
    sample_id: str,
    db: Session = Depends(get_db),
    _user: User = Depends(require_pdf_training_view),
):
    _sample, safe_sample_id = _get_sample_or_404(db, sample_id)
    return (
        db.query(PdfOrderCorrectionLog)
        .filter(PdfOrderCorrectionLog.sample_id == safe_sample_id)
        .order_by(PdfOrderCorrectionLog.corrected_at.asc())
        .all()
    )


# ---------------------------------------------------------------------------
# 客户模板
# ---------------------------------------------------------------------------

def _template_next_version(db: Session, customer_id: int | None) -> int:
    statement = select(func.coalesce(func.max(PdfOrderCustomerTemplate.version), 0) + 1)
    if customer_id is None:
        statement = statement.where(PdfOrderCustomerTemplate.customer_id.is_(None))
    else:
        statement = statement.where(PdfOrderCustomerTemplate.customer_id == customer_id)
    return int(db.execute(statement).scalar_one())


def _get_template_or_404(db: Session, template_id: int) -> PdfOrderCustomerTemplate:
    template = db.get(PdfOrderCustomerTemplate, template_id)
    if template is None:
        raise HTTPException(status_code=404, detail="模板不存在")
    return template


def _require_draft(template: PdfOrderCustomerTemplate, action: str) -> None:
    if template.status != "draft":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"{template.status} 模板不能{action}；只有 draft 可操作",
        )


def _commit_template_change(db: Session, detail: str) -> None:
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"模板生命周期约束拒绝该操作: {detail}",
        ) from exc


def _json_object(raw: str | None) -> dict:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _learning_metadata(template: PdfOrderCustomerTemplate) -> dict | None:
    metadata = _json_object(template.column_map_json).get("learning")
    if not isinstance(metadata, dict) or metadata.get("kind") != LEARNING_DRAFT_KIND:
        return None
    return metadata


def _approved_gold_samples(db: Session, customer_id: int) -> list[PdfOrderTrainingSample]:
    return db.execute(
        select(PdfOrderTrainingSample)
        .where(
            PdfOrderTrainingSample.customer_id == customer_id,
            PdfOrderTrainingSample.parse_status == "reviewed",
            PdfOrderTrainingSample.gold_review_status == "approved",
        )
        .order_by(PdfOrderTrainingSample.id)
    ).scalars().all()


def _gold_learning_signature(samples: list[PdfOrderTrainingSample]) -> list[dict]:
    return [
        {
            "sample_id": sample.id,
            "file_sha256": sample.file_sha256,
            "ground_truth_sha256": file_sha256(
                (sample.ground_truth_json or "").encode("utf-8")
            ),
        }
        for sample in samples
    ]


def _learning_correction_fields(samples: list[PdfOrderTrainingSample]) -> list[str]:
    fields: set[str] = set()
    for sample in samples:
        if not sample.ground_truth_json:
            continue
        fields.update(
            str(row["field_path"])
            for row in correction_candidates(
                sample.parser_result_json,
                sample.ground_truth_json,
            )
            if row.get("field_path")
        )
    return sorted(fields)


def _learning_template_summary(template: PdfOrderCustomerTemplate) -> dict:
    metadata = _learning_metadata(template) or {}
    return {
        "id": template.id,
        "customer_id": template.customer_id,
        "template_name": template.template_name,
        "version": template.version,
        "status": template.status,
        "supersedes_template_id": template.supersedes_template_id,
        "sample_ids": metadata.get("sample_ids") or [],
        "sample_count": len(metadata.get("sample_ids") or []),
        "correction_fields": metadata.get("correction_fields") or [],
        "generated_at": metadata.get("generated_at"),
    }


def _find_learning_templates(
    db: Session,
    customer_id: int,
    *,
    statuses: tuple[str, ...] = ("draft", "active"),
) -> list[PdfOrderCustomerTemplate]:
    rows = db.execute(
        select(PdfOrderCustomerTemplate)
        .where(
            PdfOrderCustomerTemplate.customer_id == customer_id,
            PdfOrderCustomerTemplate.status.in_(statuses),
        )
        .order_by(
            PdfOrderCustomerTemplate.version.desc(),
            PdfOrderCustomerTemplate.id.desc(),
        )
    ).scalars().all()
    return [row for row in rows if _learning_metadata(row) is not None]


def _invalidate_learning_replay(
    db: Session,
    customer_id: int | None,
    user: User,
) -> None:
    if customer_id is None:
        return
    invalidated: list[int] = []
    for template in _find_learning_templates(db, customer_id, statuses=("draft",)):
        if template.evidence_json is None:
            continue
        template.evidence_json = None
        template.updated_at = _now()
        template.updated_by = user.username
        invalidated.append(template.id)
    if invalidated:
        _log(
            db,
            user,
            "pdf_training.template.invalidate",
            f"金样本变化，规则草稿回放失效: {invalidated}",
        )


def _ensure_learning_draft(
    db: Session,
    source_sample: PdfOrderTrainingSample,
    user: User,
) -> PdfOrderCustomerTemplate:
    customer_id = source_sample.customer_id
    if customer_id is None:
        raise HTTPException(status_code=422, detail="样本未绑定客户，不能生成规则草稿。")
    customer = db.get(Customer, customer_id)
    if customer is None:
        raise HTTPException(status_code=422, detail="样本绑定的客户不存在，不能生成规则草稿。")

    approved_samples = _approved_gold_samples(db, customer_id)
    signatures = _gold_learning_signature(approved_samples)
    sample_ids = [row["sample_id"] for row in signatures]
    correction_fields = _learning_correction_fields(approved_samples)
    active = db.execute(
        select(PdfOrderCustomerTemplate)
        .where(
            PdfOrderCustomerTemplate.customer_id == customer_id,
            PdfOrderCustomerTemplate.status == "active",
        )
        .order_by(PdfOrderCustomerTemplate.version.desc())
    ).scalars().first()

    active_metadata = _learning_metadata(active) if active is not None else None
    if active_metadata and active_metadata.get("gold_signatures") == signatures:
        return active

    drafts = _find_learning_templates(db, customer_id, statuses=("draft",))
    draft = next(
        (
            row for row in drafts
            if row.supersedes_template_id == (active.id if active is not None else None)
        ),
        None,
    )
    source_template_id = active.id if active is not None else None
    metadata = {
        "kind": LEARNING_DRAFT_KIND,
        "generator_version": LEARNING_GENERATOR_VERSION,
        "source_template_id": source_template_id,
        "sample_ids": sample_ids,
        "gold_signatures": signatures,
        "correction_fields": correction_fields,
        "parser_output_layer": "order_preview_matched",
    }

    if draft is None:
        configuration = _json_object(active.column_map_json) if active is not None else {
            "customer_name": customer.name,
            "aliases": [customer.name],
        }
        metadata["generated_at"] = _now().isoformat(timespec="seconds") + "Z"
        metadata["generated_by"] = user.username
        configuration["learning"] = metadata
        version = _template_next_version(db, customer_id)
        draft = PdfOrderCustomerTemplate(
            customer_id=customer_id,
            template_name=f"自动学习草稿-{customer.name}-v{version}"[:200],
            order_no_pattern=active.order_no_pattern if active is not None else None,
            date_pattern=active.date_pattern if active is not None else None,
            item_row_pattern=active.item_row_pattern if active is not None else None,
            customer_name_pattern=(
                active.customer_name_pattern if active is not None else re.escape(customer.name)
            ),
            column_map_json=json.dumps(configuration, ensure_ascii=False, sort_keys=True),
            notes="由已批准金样本自动生成；必须回放通过并由人工一键启用。",
            created_by=user.username,
            is_active=False,
            status="draft",
            version=version,
            supersedes_template_id=source_template_id,
        )
        db.add(draft)
        db.flush()
        _log(
            db,
            user,
            "pdf_training.template.auto_draft",
            f"金样本 {source_sample.id} 生成规则草稿 {draft.id}",
        )
        return draft

    configuration = _json_object(draft.column_map_json)
    previous = configuration.get("learning") if isinstance(configuration.get("learning"), dict) else {}
    comparable_previous = {
        key: previous.get(key)
        for key in (
            "kind", "generator_version", "source_template_id", "sample_ids",
            "gold_signatures", "correction_fields", "parser_output_layer",
        )
    }
    if comparable_previous != metadata:
        metadata["generated_at"] = _now().isoformat(timespec="seconds") + "Z"
        metadata["generated_by"] = user.username
        configuration["learning"] = metadata
        draft.column_map_json = json.dumps(configuration, ensure_ascii=False, sort_keys=True)
        draft.evidence_json = None
        draft.updated_at = _now()
        draft.updated_by = user.username
        _log(
            db,
            user,
            "pdf_training.template.auto_refresh",
            f"金样本集合刷新规则草稿 {draft.id}: {sample_ids}",
        )
    return draft


def _persist_template_replay(
    db: Session,
    template: PdfOrderCustomerTemplate,
    user: User,
) -> dict:
    if template.status != "draft":
        return _json_object(template.evidence_json)
    evidence = activation_dry_run(db, template)
    evidence["replayed_at"] = _now().isoformat(timespec="seconds") + "Z"
    evidence["replayed_by"] = user.username
    template.evidence_json = json.dumps(evidence, ensure_ascii=False, sort_keys=True)
    template.updated_at = _now()
    template.updated_by = user.username
    _log(
        db,
        user,
        "pdf_training.template.replay",
        f"回放规则草稿 {template.id}: can_activate={evidence.get('can_activate')}",
    )
    return evidence


def _attach_sample_learning_state(
    db: Session,
    sample: PdfOrderTrainingSample,
) -> PdfOrderTrainingSample:
    learning_template = None
    if sample.customer_id is not None:
        for candidate in _find_learning_templates(db, sample.customer_id):
            metadata = _learning_metadata(candidate) or {}
            if sample.id in (metadata.get("sample_ids") or []):
                learning_template = candidate
                break
    learning_draft = (
        _learning_template_summary(learning_template)
        if learning_template is not None
        else None
    )
    learning_replay = (
        _json_object(learning_template.evidence_json)
        if learning_template is not None and learning_template.evidence_json
        else None
    )
    if sample.customer_id is None:
        message = "请先绑定客户，再批准为金样本。"
    elif sample.gold_review_status != "approved":
        message = "提交改进后请先完成金样本复核。"
    elif learning_template is None:
        message = "金样本已批准，可生成规则草稿并回放。"
    elif learning_template.status == "active":
        message = "该金样本对应的规则版本已启用。"
    elif learning_replay and learning_replay.get("can_activate"):
        message = "金样本回放已通过，可以一键启用。"
    else:
        reasons = (learning_replay or {}).get("reasons") or []
        message = "规则草稿已生成；" + ("；".join(reasons) if reasons else "请执行金样本回放。")
    setattr(sample, "learning_draft", learning_draft)
    setattr(sample, "learning_replay", learning_replay)
    setattr(sample, "learning_message", message)
    return sample


@router.get("/templates", response_model=list[TemplateOut])
def list_templates(
    customer_id: int | None = Query(None),
    active_only: bool = Query(False),
    db: Session = Depends(get_db),
    _user: User = Depends(require_pdf_training_view),
):
    q = db.query(PdfOrderCustomerTemplate)
    if customer_id is not None:
        q = q.filter(PdfOrderCustomerTemplate.customer_id == customer_id)
    if active_only:
        q = q.filter(PdfOrderCustomerTemplate.status == "active")
    return q.order_by(
        PdfOrderCustomerTemplate.customer_id,
        PdfOrderCustomerTemplate.version.desc(),
        PdfOrderCustomerTemplate.id.desc(),
    ).all()


@router.post(
    "/templates",
    response_model=TemplateOut,
    status_code=status.HTTP_201_CREATED,
)
def create_template(
    payload: TemplateCreate,
    db: Session = Depends(get_db),
    user: User = Depends(require_pdf_training_manage),
):
    """Create a non-routable draft.  Activation is evidence-gated separately."""
    _validate_template_rule_payload(payload)
    tmpl = PdfOrderCustomerTemplate(
        customer_id=payload.customer_id,
        template_name=payload.template_name,
        order_no_pattern=payload.order_no_pattern,
        date_pattern=payload.date_pattern,
        item_row_pattern=payload.item_row_pattern,
        customer_name_pattern=payload.customer_name_pattern,
        column_map_json=payload.column_map_json,
        notes=payload.notes,
        created_by=user.username,
        is_active=False,
        status="draft",
        version=_template_next_version(db, payload.customer_id),
    )
    db.add(tmpl)
    _log(db, user, "pdf_training.template.create", f"新建 draft 模板: {payload.template_name}")
    _commit_template_change(db, "版本号已存在")
    db.refresh(tmpl)
    return tmpl


@router.put("/templates/{template_id}", response_model=TemplateOut)
def update_template(
    template_id: int,
    payload: TemplateCreate,
    db: Session = Depends(get_db),
    user: User = Depends(require_pdf_training_manage),
):
    tmpl = _get_template_or_404(db, template_id)
    _require_draft(tmpl, "原地编辑")
    _validate_template_rule_payload(payload)
    if tmpl.customer_id != payload.customer_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="模板创建后不可更换客户；请 clone-draft 后维护新客户版本",
        )
    editable = (
        "template_name", "order_no_pattern", "date_pattern", "item_row_pattern",
        "customer_name_pattern", "column_map_json", "notes",
    )
    if all(getattr(tmpl, attr) == getattr(payload, attr) for attr in editable):
        return tmpl
    for attr in editable:
        setattr(tmpl, attr, getattr(payload, attr))
    tmpl.evidence_json = None
    tmpl.updated_at = _now()
    tmpl.updated_by = user.username
    tmpl.is_active = False
    _log(db, user, "pdf_training.template.update", f"更新 draft 模板 {template_id}: {payload.template_name}")
    _commit_template_change(db, "草稿更新冲突")
    db.refresh(tmpl)
    return tmpl


@router.post("/templates/{template_id}/clone-draft", response_model=TemplateOut, status_code=status.HTTP_201_CREATED)
def clone_template_draft(
    template_id: int,
    payload: TemplateClonePayload,
    db: Session = Depends(get_db),
    user: User = Depends(require_pdf_training_manage),
):
    source = _get_template_or_404(db, template_id)
    clone = PdfOrderCustomerTemplate(
        customer_id=source.customer_id,
        template_name=payload.template_name or f"{source.template_name} v{source.version + 1}",
        order_no_pattern=source.order_no_pattern,
        date_pattern=source.date_pattern,
        item_row_pattern=source.item_row_pattern,
        customer_name_pattern=source.customer_name_pattern,
        column_map_json=source.column_map_json,
        notes=source.notes if payload.notes is None else payload.notes,
        created_by=user.username,
        is_active=False,
        status="draft",
        version=_template_next_version(db, source.customer_id),
        supersedes_template_id=source.id,
    )
    db.add(clone)
    _log(db, user, "pdf_training.template.clone", f"从模板 {source.id} 创建 draft 版本")
    _commit_template_change(db, "并发创建了相同客户版本")
    db.refresh(clone)
    return clone


@router.post("/templates/{template_id}/activation-dry-run")
def template_activation_dry_run(
    template_id: int,
    db: Session = Depends(get_db),
    _user: User = Depends(require_pdf_training_manage),
):
    return activation_dry_run(db, _get_template_or_404(db, template_id))


@router.post("/templates/{template_id}/replay")
def replay_template_gold_samples(
    template_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_pdf_training_manage),
):
    template = _get_template_or_404(db, template_id)
    _require_draft(template, "回放金样本")
    evidence = _persist_template_replay(db, template, user)
    db.commit()
    return evidence


@router.post("/templates/{template_id}/activate", response_model=TemplateOut)
def activate_template(
    template_id: int,
    payload: TemplateActivationPayload,
    db: Session = Depends(get_db),
    user: User = Depends(require_pdf_training_manage),
):
    candidate = _get_template_or_404(db, template_id)
    _require_draft(candidate, "激活")
    reason = (payload.reason or "").strip() or "激活PDF订单模板（系统记录）"
    evidence = activation_dry_run(db, candidate)
    if not evidence["can_activate"]:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"message": "金样本 dry-run 未通过，模板未激活", "evidence": evidence},
        )

    active = db.execute(
        select(PdfOrderCustomerTemplate)
        .where(
            PdfOrderCustomerTemplate.customer_id == candidate.customer_id,
            PdfOrderCustomerTemplate.status == "active",
        )
        .with_for_update()
    ).scalars().all()
    now = _now()
    for old in active:
        old.status = "retired"
        old.is_active = False
        old.retired_at = now
        old.retired_by = user.username
        old.retired_reason = f"被模板 {candidate.id} 激活替换: {reason}"
        old.updated_at = now
        old.updated_by = user.username
    if active:
        try:
            db.flush()
        except IntegrityError as exc:
            db.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="模板生命周期约束拒绝退役旧 active 模板",
            ) from exc
    candidate.status = "active"
    candidate.is_active = True
    candidate.activated_at = now
    candidate.activated_by = user.username
    candidate.activation_reason = reason
    candidate.evidence_json = json.dumps(evidence, ensure_ascii=False, sort_keys=True)
    candidate.updated_at = now
    candidate.updated_by = user.username
    _log(db, user, "pdf_training.template.activate", f"激活模板 {candidate.id}: {reason}")
    _commit_template_change(db, "同客户已经有 active 模板")
    db.refresh(candidate)
    return candidate


@router.post("/templates/{template_id}/retire", response_model=TemplateOut)
def retire_template(
    template_id: int,
    payload: TemplateRetirePayload,
    db: Session = Depends(get_db),
    user: User = Depends(require_pdf_training_manage),
):
    template = _get_template_or_404(db, template_id)
    if template.status != "active":
        raise HTTPException(status_code=409, detail="只有 active 模板可以退役")
    reason = (payload.reason or "").strip() or "退役PDF订单模板（系统记录）"
    now = _now()
    template.status = "retired"
    template.is_active = False
    template.retired_at = now
    template.retired_by = user.username
    template.retired_reason = reason
    template.updated_at = now
    template.updated_by = user.username
    _log(db, user, "pdf_training.template.retire", f"退役模板 {template.id}: {reason}")
    _commit_template_change(db, "模板退役冲突")
    db.refresh(template)
    return template


@router.delete("/templates/{template_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_template(
    template_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_pdf_training_manage),
):
    tmpl = _get_template_or_404(db, template_id)
    _require_draft(tmpl, "物理删除")
    has_successor = db.execute(
        select(PdfOrderCustomerTemplate.id).where(
            PdfOrderCustomerTemplate.supersedes_template_id == tmpl.id
        )
    ).first()
    if has_successor:
        raise HTTPException(status_code=409, detail="已有后继版本引用该模板，不能物理删除")
    _log(db, user, "pdf_training.template.delete", f"删除 draft 模板 {template_id}: {tmpl.template_name}")
    db.delete(tmpl)
    _commit_template_change(db, "模板已被其他版本引用")


# ---------------------------------------------------------------------------
# OCR 状态
# ---------------------------------------------------------------------------

@router.get("/ocr-status")
def get_ocr_status(_user: User = Depends(require_pdf_training_view)):
    """返回当前服务器 OCR 能力状态。"""
    return {
        "available": ocr_available(),
        "engine": ocr_engine_name(),
        "message": (
            "OCR 就绪" if ocr_available()
            else "OCR 未配置：请安装 pymupdf + easyocr 或 tesseract"
        ),
    }


# ---------------------------------------------------------------------------
# 全局统计
# ---------------------------------------------------------------------------

@router.get("/stats", response_model=StatsOut)
def get_stats(
    db: Session = Depends(get_db),
    _user: User = Depends(require_pdf_training_view),
):
    samples = db.query(PdfOrderTrainingSample).all()
    stats = compute_stats(samples)
    return StatsOut(
        sample_count=stats.sample_count,
        labeled_count=stats.labeled_count,
        avg_score=stats.avg_score,
        high_count=stats.high_count,
        mid_count=stats.mid_count,
        low_count=stats.low_count,
        top_error_fields=[[f, c] for f, c in stats.top_error_fields],
    )
