from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base


class SupplierFlutePriceRule(Base):
    """供应商楞型加价规则（v0.19.2-B）。

    供应商报价单里的「平方报价」是基础价（B/E 同价）。某些楞型需在基础价上加价：
      苏州嘉林亿 3层 A瓦 +0.04 元/㎡；昆山鸣朋/苏州佳丰 3层 A瓦 +0.05 元/㎡。
    常用箱成本参考、订单预估成本、比价统一调用此规则：
      最终材料平方价 = 基础平方报价 + price_delta。

    主键唯一性靠 (supplier_name, layer_count, flute_type, is_active) 业务约束，
    取值时取最新生效、启用的一条。
    """

    __tablename__ = "supplier_flute_price_rules"
    __table_args__ = (
        Index(
            "ix_sfpr_supplier_layer_flute",
            "supplier_name",
            "layer_count",
            "flute_type",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    supplier_name: Mapped[str] = mapped_column(String(200), nullable=False)
    layer_count: Mapped[int] = mapped_column(Integer, nullable=False)
    flute_type: Mapped[str] = mapped_column(String(20), nullable=False)
    price_delta: Mapped[Decimal] = mapped_column(
        Numeric(12, 4), default=Decimal("0"), nullable=False
    )
    effective_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    remark: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime, onupdate=func.current_timestamp(), nullable=True
    )
