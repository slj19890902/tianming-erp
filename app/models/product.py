from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models import Base

if TYPE_CHECKING:
    from app.models.customer import Customer
    from app.models.material import Material
    from app.models.mold_tool import MoldTool
    from app.models.product_bom import ProductBomComponent
    from app.models.product_drawing import ProductDrawing


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (
        UniqueConstraint(
            "customer_id",
            "customer_material_code",
            name="uq_products_customer_material_code",
        ),
        UniqueConstraint(
            "customer_id",
            "product_code",
            name="uq_products_customer_product_code",
        ),
        CheckConstraint(
            "box_category IN ('normal', 'die_cut')",
            name="ck_products_box_category",
        ),
        CheckConstraint(
            "default_cutting_mode IN ('一开一', '一开二', '一开三', '一开四', '一开五')",
            name="ck_products_default_cutting_mode",
        ),
        CheckConstraint(
            "combination_mode IN ('parent_priced_set', 'component_priced')",
            name="ck_products_combination_mode",
        ),
        CheckConstraint("version >= 1", name="ck_products_version"),
        Index("ix_products_customer_id", "customer_id"),
        Index("ix_products_material_id", "material_id"),
        Index("ix_products_mold_tool_id", "mold_tool_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id", ondelete="RESTRICT"),
        nullable=False,
    )
    product_code: Mapped[str] = mapped_column(String(150), nullable=False)
    customer_material_code: Mapped[str] = mapped_column(String(150), nullable=False)
    product_name: Mapped[str] = mapped_column(String(250), nullable=False)
    material_id: Mapped[int | None] = mapped_column(
        ForeignKey("materials.id", ondelete="SET NULL"),
        nullable=True,
    )
    mold_tool_id: Mapped[int | None] = mapped_column(
        ForeignKey("mold_tools.id", ondelete="SET NULL"),
        nullable=True,
    )
    legacy_material_text: Mapped[str | None] = mapped_column(String(250), nullable=True)
    length_mm: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    width_mm: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    height_mm: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    box_category: Mapped[str] = mapped_column(
        String(20),
        default="normal",
        nullable=False,
    )
    box_style: Mapped[str | None] = mapped_column(String(150), nullable=True)
    print_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    printing_colors: Mapped[str | None] = mapped_column(String(150), nullable=True)
    production_process: Mapped[str | None] = mapped_column(Text, nullable=True)
    unit: Mapped[str] = mapped_column(String(20), default="只", nullable=False)
    sale_unit_price: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 4),
        nullable=True,
    )
    sale_unit_price_no_tax: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 4),
        nullable=True,
    )
    cost_unit_price: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 4),
        nullable=True,
    )
    board_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
    suggested_price: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 4),
        nullable=True,
    )
    default_cardboard_length: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 2),
        nullable=True,
    )
    default_cardboard_width: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 2),
        nullable=True,
    )
    default_score_lines: Mapped[str | None] = mapped_column(
        String(250),
        nullable=True,
    )
    default_material_code: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
    )
    die_cut_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    remark: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 批量规范客户料号前的旧值快照（可追溯，不得删除）
    legacy_customer_material_code: Mapped[str | None] = mapped_column(
        String(150), nullable=True
    )
    # Phase 17: 楞型相关字段（直接存储，不依赖 material_id）
    flute_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    layer_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    surface_paper_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # 楞型批量识别前的原始文本快照
    legacy_flute_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    # v0.19.2-B: 报料尺寸 + 压线信息（单位 mm，整数）
    report_length_mm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    report_width_mm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    crease_type: Mapped[str | None] = mapped_column(String(20), nullable=True)   # 毛片 / 净料 / 压线
    crease_left_mm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    crease_middle_mm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    crease_right_mm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    report_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    base_report_length_mm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    base_report_width_mm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    base_crease_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    base_crease_left_mm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    base_crease_middle_mm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    base_crease_right_mm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    base_report_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    splice_mode: Mapped[str | None] = mapped_column(String(20), nullable=True)
    pieces_per_box: Mapped[int | None] = mapped_column(Integer, nullable=True)
    default_cutting_mode: Mapped[str] = mapped_column(
        String(20),
        default="一开一",
        server_default="一开一",
        nullable=False,
    )
    flap_mm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_composite: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
    )
    combination_mode: Mapped[str] = mapped_column(
        String(30),
        default="parent_priced_set",
        server_default="parent_priced_set",
        nullable=False,
    )
    is_internal_component: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    manual_modified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    manual_modified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
        index=True,
    )
    deleted_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    purged_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
        index=True,
    )
    version: Mapped[int] = mapped_column(
        Integer,
        default=1,
        server_default="1",
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.current_timestamp(),
        nullable=False,
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        onupdate=func.current_timestamp(),
        nullable=True,
    )

    customer: Mapped["Customer"] = relationship(back_populates="products")
    material: Mapped["Material | None"] = relationship(back_populates="products")
    mold_tool: Mapped["MoldTool | None"] = relationship(back_populates="products")
    bom_components: Mapped[list["ProductBomComponent"]] = relationship(
        "ProductBomComponent",
        foreign_keys="ProductBomComponent.parent_product_id",
        back_populates="parent_product",
        passive_deletes=True,
        order_by="ProductBomComponent.display_order",
    )
    drawings: Mapped[list["ProductDrawing"]] = relationship(
        back_populates="product",
        order_by=(
            "desc(ProductDrawing.uploaded_at), "
            "desc(ProductDrawing.id)"
        ),
    )
