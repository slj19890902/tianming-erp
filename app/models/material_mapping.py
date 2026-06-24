from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, DateTime, Integer, Numeric, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base


class MaterialCodeMappingCandidate(Base):
    """供应商材质代码映射候选表。

    保存 cowork CSV 提供的旧代码 → 嘉林亿新代码映射，以及人工审批状态。
    不直接写入 products 或 materials；高可信且 approved 后由服务层写入。
    """

    __tablename__ = "material_code_mapping_candidates"
    __table_args__ = (
        CheckConstraint(
            "confidence_level IN ('高可信', '中可信', '低可信')",
            name="ck_mat_mapping_confidence_level",
        ),
        CheckConstraint(
            "review_status IN ('pending', 'approved', 'rejected')",
            name="ck_mat_mapping_review_status",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # 原始代码
    old_code: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    new_code: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # 供应商信息
    new_supplier: Mapped[str | None] = mapped_column(String(100), nullable=True)   # 嘉林亿
    old_supplier: Mapped[str | None] = mapped_column(String(100), nullable=True)   # 鸣朋/佳丰

    # 材质结构
    layer_count: Mapped[str | None] = mapped_column(String(20), nullable=True)     # 例如 "5层"
    weight_structure: Mapped[str | None] = mapped_column(String(500), nullable=True)  # 逐层克重

    # 价格
    new_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
    old_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)

    # 可信度
    confidence_label: Mapped[str | None] = mapped_column(String(300), nullable=True)
    confidence_level: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        index=True,
        default="低可信",
    )

    # CSV 来源追踪
    source_file: Mapped[str | None] = mapped_column(String(500), nullable=True)
    source_row_number: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.current_timestamp(),
        nullable=False,
    )

    # 审批状态
    review_status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="pending",
        index=True,
    )
    review_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 命中产品数（只读汇总，在 API 层计算，不存库）
    # —— 不建模为字段，在查询时 JOIN 计算
