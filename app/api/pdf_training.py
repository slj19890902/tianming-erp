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
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.api.deps import RoleChecker, get_current_user, get_db
from app.models.audit import OperationLog
from app.models.pdf_training import (
    PdfOrderCorrectionLog,
    PdfOrderCustomerTemplate,
    PdfOrderTrainingBatch,
    PdfOrderTrainingSample,
)
from app.models.user import User
from app.services.order_pdf_import import (
    extract_text_from_pdf_bytes,
    file_sha256,
    parse_purchase_order_text,
)
from app.services.pdf_ocr import ocr_available, ocr_engine_name, ocr_pdf_bytes, should_use_ocr
from app.services.pdf_scoring import compute_stats, score_sample

router = APIRouter()

require_admin = RoleChecker(["admin"])

# 训练样本本地存储目录（使用绝对路径，避免因启动目录不同而写错位置）
# 本文件位于 app/api/pdf_training.py，parents[2] = 项目根目录
_SAMPLE_DIR = Path(__file__).resolve().parents[2] / "data" / "pdf_training_samples"


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(tz=timezone.utc).replace(tzinfo=None)


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
    created_at: datetime
    labeled_at: datetime | None

    model_config = {"from_attributes": True}


class SampleDetail(SampleSummary):
    parser_result_json: str | None
    ground_truth_json: str | None
    extracted_text: str | None
    ocr_text_raw: str | None
    notes: str | None


class GroundTruthPayload(BaseModel):
    ground_truth_json: str         # JSON 字符串
    notes: str | None = None


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
    created_at: datetime
    updated_at: datetime | None
    notes: str | None

    model_config = {"from_attributes": True}


class TemplateCreate(BaseModel):
    customer_id: int | None = None
    template_name: str
    order_no_pattern: str | None = None
    date_pattern: str | None = None
    item_row_pattern: str | None = None
    customer_name_pattern: str | None = None
    column_map_json: str | None = None
    notes: str | None = None


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
    _user: User = Depends(get_current_user),
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
    user: User = Depends(require_admin),
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
    _user: User = Depends(get_current_user),
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
    user: User = Depends(get_current_user),
):
    """上传一个 PDF，立即运行解析器，保存样本记录。

    store_pdf=True 时将 PDF 原文件保存到 data/pdf_training_samples/（仅限本地；
    该目录已被 .gitignore 排除，不会进入 Git 仓库）。
    """
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="仅接受 .pdf 文件",
        )

    content = await file.read()
    sha = file_sha256(content)

    # 重复上传检测
    existing = db.query(PdfOrderTrainingSample).filter(
        PdfOrderTrainingSample.file_sha256 == sha
    ).first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"该 PDF 已上传（样本 ID={existing.id}，文件 SHA256 重复）",
        )

    # ── 步骤1：PDF 文本提取 ──────────────────────────────────────────────
    extracted_text: str | None = None
    parser_result_json: str | None = None
    parse_result: dict | None = None
    parse_method = "failed"
    try:
        extracted_text = extract_text_from_pdf_bytes(content)
        if extracted_text and extracted_text.strip():
            try:
                parse_result = parse_purchase_order_text(extracted_text)
                parser_result_json = json.dumps(parse_result, ensure_ascii=False, default=str)
                parse_method = "text"
            except ValueError:
                # 文本提取成功但解析失败（乱码 / 格式不符）
                parse_method = "failed"
    except Exception:
        parse_method = "failed"

    # ── 步骤2：OCR 兜底（图片 PDF / 乱码 PDF）─────────────────────────
    ocr_text_raw: str | None = None
    if should_use_ocr(extracted_text, parse_result):
        ocr_text, ocr_method = ocr_pdf_bytes(content)
        if ocr_text and ocr_method not in ("ocr_unavailable", "ocr_failed"):
            ocr_text_raw = ocr_text
            # 尝试用 OCR 文本重新解析
            if parse_result is None or not parse_result.get("items"):
                try:
                    ocr_parse = parse_purchase_order_text(ocr_text)
                    parser_result_json = json.dumps(ocr_parse, ensure_ascii=False, default=str)
                    parse_result = ocr_parse
                    # 如果原文本解析也部分成功，标记为 mixed；否则纯 OCR
                    parse_method = "mixed" if extracted_text and extracted_text.strip() else ocr_method
                except ValueError:
                    parse_method = ocr_method  # OCR 了但仍解析失败
            else:
                # 已有文本解析结果，仅保存 OCR 文本供参考
                parse_method = "mixed"
        elif ocr_method == "ocr_unavailable":
            # OCR 引擎未配置，记录但不覆盖现有方法
            if parse_method == "failed":
                parse_method = "ocr_unavailable"

    # ── 步骤3：落盘 PDF 原文件 ─────────────────────────────────────────
    file_path: str | None = None
    if store_pdf:
        _SAMPLE_DIR.mkdir(parents=True, exist_ok=True)
        safe_name = f"{sha[:16]}_{Path(file.filename).stem[:40]}.pdf"
        dest = _SAMPLE_DIR / safe_name
        dest.write_bytes(content)
        file_path = str(dest)

    sample = PdfOrderTrainingSample(
        batch_id=batch_id,
        customer_id=customer_id,
        file_name=file.filename,
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
        f"上传样本: {file.filename} (sha={sha[:12]}…, method={parse_method})",
    )
    db.commit()
    db.refresh(sample)
    return sample


# ---------------------------------------------------------------------------
# 样本详情 / 标注 / 评分 / 删除
# ---------------------------------------------------------------------------

@router.get("/samples/{sample_id}", response_model=SampleDetail)
def get_sample(
    sample_id: int,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    sample = db.get(PdfOrderTrainingSample, sample_id)
    if sample is None:
        raise HTTPException(status_code=404, detail="样本不存在")
    return sample


@router.put("/samples/{sample_id}/ground-truth", response_model=SampleDetail)
def set_ground_truth(
    sample_id: int,
    payload: GroundTruthPayload,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """写入人工标注 ground_truth_json，同时自动触发评分计算。"""
    sample = db.get(PdfOrderTrainingSample, sample_id)
    if sample is None:
        raise HTTPException(status_code=404, detail="样本不存在")

    # 校验 JSON 格式
    try:
        json.loads(payload.ground_truth_json)
    except (json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"ground_truth_json 格式错误: {exc}",
        ) from exc

    sample.ground_truth_json = payload.ground_truth_json
    sample.parse_status = "labeled"
    sample.labeled_at = _now()
    sample.labeled_by = user.username
    if payload.notes:
        sample.notes = payload.notes

    # 自动评分
    sr = score_sample(sample.parser_result_json, sample.ground_truth_json)
    sample.score = sr.overall_score

    _log(
        db,
        user,
        "pdf_training.sample.label",
        f"标注样本 {sample_id}，评分={sr.overall_score:.3f}",
    )
    db.commit()
    db.refresh(sample)
    return sample


@router.post("/samples/{sample_id}/score", response_model=ScoreOut)
def compute_sample_score(
    sample_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """（重新）计算并保存评分。要求样本已有 ground_truth_json。"""
    sample = db.get(PdfOrderTrainingSample, sample_id)
    if sample is None:
        raise HTTPException(status_code=404, detail="样本不存在")
    if not sample.ground_truth_json:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="样本尚未标注 ground_truth_json，无法评分",
        )

    sr = score_sample(sample.parser_result_json, sample.ground_truth_json)
    sample.score = sr.overall_score
    _log(db, user, "pdf_training.sample.score", f"评分样本 {sample_id}={sr.overall_score:.3f}")
    db.commit()

    return ScoreOut(
        sample_id=sample_id,
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


@router.delete("/samples/{sample_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_sample(
    sample_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
):
    sample = db.get(PdfOrderTrainingSample, sample_id)
    if sample is None:
        raise HTTPException(status_code=404, detail="样本不存在")
    _log(db, user, "pdf_training.sample.delete", f"删除样本 {sample_id}: {sample.file_name}")
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


@router.post(
    "/samples/{sample_id}/corrections",
    response_model=CorrectionOut,
    status_code=status.HTTP_201_CREATED,
)
def add_correction(
    sample_id: int,
    payload: CorrectionCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """为样本新增字段纠错记录（不影响 ground_truth_json，仅记录差异）。"""
    sample = db.get(PdfOrderTrainingSample, sample_id)
    if sample is None:
        raise HTTPException(status_code=404, detail="样本不存在")
    log = PdfOrderCorrectionLog(
        sample_id=sample_id,
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
    sample_id: int,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    sample = db.get(PdfOrderTrainingSample, sample_id)
    if sample is None:
        raise HTTPException(status_code=404, detail="样本不存在")
    return (
        db.query(PdfOrderCorrectionLog)
        .filter(PdfOrderCorrectionLog.sample_id == sample_id)
        .order_by(PdfOrderCorrectionLog.corrected_at.asc())
        .all()
    )


# ---------------------------------------------------------------------------
# 客户模板
# ---------------------------------------------------------------------------

@router.get("/templates", response_model=list[TemplateOut])
def list_templates(
    customer_id: int | None = Query(None),
    active_only: bool = Query(False),
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    q = db.query(PdfOrderCustomerTemplate)
    if customer_id is not None:
        q = q.filter(PdfOrderCustomerTemplate.customer_id == customer_id)
    if active_only:
        q = q.filter(PdfOrderCustomerTemplate.is_active.is_(True))
    return q.order_by(PdfOrderCustomerTemplate.created_at.desc()).all()


@router.post(
    "/templates",
    response_model=TemplateOut,
    status_code=status.HTTP_201_CREATED,
)
def create_template(
    payload: TemplateCreate,
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
):
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
        is_active=True,
    )
    db.add(tmpl)
    _log(db, user, "pdf_training.template.create", f"新建模板: {payload.template_name}")
    db.commit()
    db.refresh(tmpl)
    return tmpl


@router.put("/templates/{template_id}", response_model=TemplateOut)
def update_template(
    template_id: int,
    payload: TemplateCreate,
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
):
    tmpl = db.get(PdfOrderCustomerTemplate, template_id)
    if tmpl is None:
        raise HTTPException(status_code=404, detail="模板不存在")
    for attr in (
        "customer_id", "template_name", "order_no_pattern", "date_pattern",
        "item_row_pattern", "customer_name_pattern", "column_map_json", "notes",
    ):
        setattr(tmpl, attr, getattr(payload, attr))
    tmpl.updated_at = _now()
    _log(db, user, "pdf_training.template.update", f"更新模板 {template_id}: {payload.template_name}")
    db.commit()
    db.refresh(tmpl)
    return tmpl


@router.delete("/templates/{template_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_template(
    template_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
):
    tmpl = db.get(PdfOrderCustomerTemplate, template_id)
    if tmpl is None:
        raise HTTPException(status_code=404, detail="模板不存在")
    _log(db, user, "pdf_training.template.delete", f"删除模板 {template_id}: {tmpl.template_name}")
    db.delete(tmpl)
    db.commit()


# ---------------------------------------------------------------------------
# OCR 状态
# ---------------------------------------------------------------------------

@router.get("/ocr-status")
def get_ocr_status(_user: User = Depends(get_current_user)):
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
    _user: User = Depends(get_current_user),
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
