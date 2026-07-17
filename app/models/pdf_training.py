"""
Phase 18 / v0.18.0: PDF 订单识别训练样本库数据模型。

四张表：
- pdf_order_training_batches   : 批次（一次上传多个 PDF 为一批）
- pdf_order_training_samples   : 单个 PDF 样本（含解析结果 + 人工标注）
- pdf_order_customer_templates : 客户级正则模板
- pdf_order_correction_logs    : 字段级纠错历史
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models import Base


# ---------------------------------------------------------------------------
# 1. 批次表
# ---------------------------------------------------------------------------

class PdfOrderTrainingBatch(Base):
    """PDF 训练批次。每次人工上传一组 PDF 样本时创建一条批次记录。"""

    __tablename__ = "pdf_order_training_batches"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    batch_name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 反向关系
    samples: Mapped[list["PdfOrderTrainingSample"]] = relationship(
        "PdfOrderTrainingSample",
        back_populates="batch",
        lazy="select",
    )

    @property
    def sample_count(self) -> int:
        return len(self.samples)


# ---------------------------------------------------------------------------
# 2. 样本表
# ---------------------------------------------------------------------------

class PdfOrderTrainingSample(Base):
    """单个 PDF 样本记录。

    parse_status 生命周期:
      pending   → 已上传，尚未人工标注
      labeled   → 已标注 ground_truth_json
      reviewed  → 已复核（高质量标注）

    ground_truth_json / parser_result_json 结构（示例）::

        {
          "order_no": "THPO-2024-001",
          "customer_name": "某某公司",
          "order_date": "2024-01-15",
          "delivery_date": "2024-01-30",
          "items": [
            {
              "line_no": 1,
              "product_code": "P001",
              "product_name": "产品名称",
              "spec": "500*300*200",
              "quantity": 100,
              "unit": "件",
              "unit_price": "12.50",
              "amount": "1250.00",
              "delivery_date": "2024-01-30"
            }
          ]
        }
    """

    __tablename__ = "pdf_order_training_samples"
    __table_args__ = (
        CheckConstraint(
            "parse_status IN ('pending', 'labeled', 'reviewed')",
            name="ck_pdf_sample_parse_status",
        ),
        CheckConstraint(
            "parse_method IN ('text', 'ocr', 'failed', 'unknown', 'mixed', "
            "'ocr_easyocr', 'ocr_tesseract', 'ocr_unavailable', 'ocr_failed')",
            name="ck_pdf_sample_parse_method",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # 批次关联
    batch_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("pdf_order_training_batches.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    batch: Mapped["PdfOrderTrainingBatch | None"] = relationship(
        "PdfOrderTrainingBatch", back_populates="samples"
    )

    # 客户关联（可选）
    customer_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("customers.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # 文件信息
    file_name: Mapped[str] = mapped_column(String(500), nullable=False)
    file_sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # 本地存储路径（相对 data/pdf_training_samples/），不含机密
    file_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    # 解析结果（JSON 字符串）
    parser_result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 人工标注的正确结果（JSON 字符串）
    ground_truth_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 解析质量评分 0.0–1.0
    score: Mapped[float | None] = mapped_column(Float, nullable=True)

    # 状态
    parse_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending", index=True
    )
    # 解析方式
    parse_method: Mapped[str] = mapped_column(
        String(20), nullable=False, default="unknown"
    )

    # Gold review is deliberately independent from the legacy parse_status.
    # Existing "reviewed" samples are migrated to pending, never implicitly
    # trusted as activation evidence.
    gold_review_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending", index=True
    )
    gold_reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    gold_reviewed_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    gold_review_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 原始提取文本（PDF 文本层，供调试）
    extracted_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    # OCR 识别结果文本（Phase 19 新增；图片 PDF / 乱码 PDF 适用）
    ocr_text_raw: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 时间戳
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )
    labeled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    labeled_by: Mapped[str | None] = mapped_column(String(100), nullable=True)

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 反向关系
    corrections: Mapped[list["PdfOrderCorrectionLog"]] = relationship(
        "PdfOrderCorrectionLog",
        back_populates="sample",
        lazy="select",
    )


# ---------------------------------------------------------------------------
# 3. 客户级模板表
# ---------------------------------------------------------------------------

class PdfOrderCustomerTemplate(Base):
    """客户级 PDF 解析模板。

    为特定客户存储定制化的正则表达式模式，覆盖全局默认规则。
    同一客户可有多个版本模板。status 是唯一的生命周期真相；is_active
    保留为兼容旧调用方的镜像，所有新写入必须同步两者。
    """

    __tablename__ = "pdf_order_customer_templates"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'active', 'retired')",
            name="ck_pdf_template_status",
        ),
        CheckConstraint("version >= 1", name="ck_pdf_template_version"),
        UniqueConstraint(
            "customer_id", "version", name="uq_pdf_templates_customer_version"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    customer_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("customers.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    template_name: Mapped[str] = mapped_column(String(200), nullable=False)

    # 正则模式（均可为 None，表示沿用全局默认）
    order_no_pattern: Mapped[str | None] = mapped_column(Text, nullable=True)
    date_pattern: Mapped[str | None] = mapped_column(Text, nullable=True)
    item_row_pattern: Mapped[str | None] = mapped_column(Text, nullable=True)
    customer_name_pattern: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 字段捕获组映射。正式格式为
    # {"field_mapping":{"product_code":"sku","quantity":4}}；同时兼容
    # 旧顶层列号格式 {"product_code":1,"quantity":4}。
    column_map_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    is_active: Mapped[bool] = mapped_column(default=False, nullable=False)

    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    supersedes_template_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("pdf_order_customer_templates.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(100), nullable=True)

    activated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    activated_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    activation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    retired_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    retired_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    retired_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


# ---------------------------------------------------------------------------
# 4. 字段纠错历史
# ---------------------------------------------------------------------------

class PdfOrderCorrectionLog(Base):
    """字段级纠错历史。

    每次人工将某字段的解析值改为正确值，记录一条。
    支持统计高频出错字段、生成改进优先级报告。
    """

    __tablename__ = "pdf_order_correction_logs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    sample_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("pdf_order_training_samples.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    sample: Mapped["PdfOrderTrainingSample"] = relationship(
        "PdfOrderTrainingSample", back_populates="corrections"
    )

    # 字段路径，如 "order_no" / "items[0].product_code"
    field_path: Mapped[str] = mapped_column(String(200), nullable=False)
    # 解析器输出值（字符串化）
    parser_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 人工纠正后的正确值
    corrected_value: Mapped[str | None] = mapped_column(Text, nullable=True)

    corrected_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    corrected_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
