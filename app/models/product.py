from __future__ import annotations
from app.models.dimension_type import SheetDimensionColumn

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
    false,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models import Base

if TYPE_CHECKING:
    from app.models.customer import Customer
    from app.models.material import Material
    from app.models.mold_tool import MoldTool
    from app.models.printing_plate import PrintingPlate
    from app.models.product_bom import ProductBomComponent
    from app.models.product_drawing import ProductDrawing


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (
        UniqueConstraint(
            "customer_id",
            "customer_material_code",
            "product_name",
            name="uq_products_customer_material_code_name",
        ),
        UniqueConstraint(
            "customer_id",
            "product_code",
            "product_name",
            name="uq_products_customer_product_code_name",
        ),
        CheckConstraint(
            "box_category IN ('normal', 'die_cut')",
            name="ck_products_box_category",
        ),
        CheckConstraint(
            "(default_cutting_mode IN ('一开一', '一开二', '一开三', '一开四', '一开五', '一开六') "
            "OR (substr(default_cutting_mode, 1, 2) = '一开' "
            "AND length(default_cutting_mode) BETWEEN 3 AND 20 "
            "AND CAST(substr(default_cutting_mode, 3) AS BIGINT) > 0 "
            "AND substr(default_cutting_mode, 3) = "
            "CAST(CAST(substr(default_cutting_mode, 3) AS BIGINT) AS VARCHAR)))",
            name="ck_products_default_cutting_mode",
        ),
        CheckConstraint(
            "combination_mode IN ('parent_priced_set', 'component_priced')",
            name="ck_products_combination_mode",
        ),
        CheckConstraint(
            "composite_fulfillment_mode IN ('parent_delivery', 'component_delivery')",
            name="ck_products_composite_fulfillment_mode",
        ),
        CheckConstraint(
            "printing_plate_mode IN ('no_plate', 'plate')",
            name="ck_products_printing_plate_mode",
        ),
        CheckConstraint(
            "printing_plate_mode = 'plate' OR "
            "(printing_plate_1_id IS NULL AND printing_plate_2_id IS NULL "
            "AND printing_plate_3_id IS NULL)",
            name="ck_products_no_plate_has_no_binding",
        ),
        CheckConstraint(
            "((production_label_enabled = false "
            "AND production_label_units_per_label IS NULL) OR "
            "(production_label_enabled = true "
            "AND production_label_units_per_label > 0))",
            name="ck_products_production_label_policy",
        ),
        CheckConstraint(
            "supply_mode IN ('corrugated_production','external_purchase','mixed_bom')",
            name="ck_products_supply_mode",
        ),
        CheckConstraint(
            "((supply_mode = 'external_purchase' AND external_packaging_category_code IS NOT NULL "
            "AND external_packaging_specification_json IS NOT NULL "
            "AND external_packaging_specification_summary IS NOT NULL "
            "AND external_packaging_purchase_unit IS NOT NULL "
            "AND external_packaging_candidate_snapshot_json IS NOT NULL) OR "
            "(supply_mode <> 'external_purchase' AND external_packaging_category_code IS NULL "
            "AND external_packaging_specification_json IS NULL AND external_packaging_specification_summary IS NULL "
            "AND external_packaging_purchase_unit IS NULL AND external_packaging_candidate_snapshot_json IS NULL))",
            name="ck_products_external_supply_profile",
        ),
        CheckConstraint(
            "((supply_mode = 'external_purchase' AND "
            "((external_packaging_default_order_quantity_basis IS NULL AND "
            "external_packaging_default_purchase_quantity_basis IS NULL) OR "
            "(external_packaging_default_order_quantity_basis > 0 AND "
            "external_packaging_default_purchase_quantity_basis > 0))) OR "
            "(supply_mode <> 'external_purchase' AND "
            "external_packaging_default_order_quantity_basis IS NULL AND "
            "external_packaging_default_purchase_quantity_basis IS NULL))",
            name="ck_products_external_purchase_default_ratio",
        ),
        CheckConstraint("version >= 1", name="ck_products_version"),
        Index("ix_products_customer_id", "customer_id"),
        Index("ix_products_material_id", "material_id"),
        Index("ix_products_mold_tool_id", "mold_tool_id"),
        Index("ix_products_printing_plate_1_id", "printing_plate_1_id"),
        Index("ix_products_printing_plate_2_id", "printing_plate_2_id"),
        Index("ix_products_printing_plate_3_id", "printing_plate_3_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id", ondelete="RESTRICT"),
        nullable=False,
    )
    product_code: Mapped[str] = mapped_column(String(150), nullable=False)
    customer_material_code: Mapped[str] = mapped_column(String(150), nullable=False)
    product_name: Mapped[str] = mapped_column(String(250), nullable=False)
    customer_drawing_number: Mapped[str | None] = mapped_column(String(150), nullable=True)
    customer_category: Mapped[str | None] = mapped_column(String(100), nullable=True)
    customer_model: Mapped[str | None] = mapped_column(String(500), nullable=True)
    customer_product_name: Mapped[str | None] = mapped_column(String(500), nullable=True)
    customer_drawing_display: Mapped[str | None] = mapped_column(String(250), nullable=True)
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
    supply_mode: Mapped[str] = mapped_column(
        String(30), default="corrugated_production", server_default="corrugated_production", nullable=False
    )
    external_packaging_category_code: Mapped[str | None] = mapped_column(
        String(50), nullable=True
    )
    external_packaging_specification_json: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )
    external_packaging_specification_summary: Mapped[str | None] = mapped_column(
        String(500), nullable=True
    )
    external_packaging_purchase_unit: Mapped[str | None] = mapped_column(
        String(20), nullable=True
    )
    external_packaging_candidate_snapshot_json: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )
    external_packaging_default_order_quantity_basis: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 6), nullable=True
    )
    external_packaging_default_purchase_quantity_basis: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 6), nullable=True
    )
    print_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    printing_colors: Mapped[str | None] = mapped_column(String(150), nullable=True)
    printing_plate_mode: Mapped[str] = mapped_column(
        String(20), default="no_plate", server_default="no_plate", nullable=False
    )
    printing_plate_1_id: Mapped[int | None] = mapped_column(
        ForeignKey("printing_plates.id", ondelete="SET NULL"), nullable=True
    )
    printing_plate_2_id: Mapped[int | None] = mapped_column(
        ForeignKey("printing_plates.id", ondelete="SET NULL"), nullable=True
    )
    printing_plate_3_id: Mapped[int | None] = mapped_column(
        ForeignKey("printing_plates.id", ondelete="SET NULL"), nullable=True
    )
    plate_alignment_value_mm: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 2), nullable=True
    )
    plate_mount_value_mm: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 2), nullable=True
    )
    machine_set_length_mm: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 2), nullable=True
    )
    machine_set_width_mm: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 2), nullable=True
    )
    machine_set_height_mm: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 2), nullable=True
    )
    production_process: Mapped[str | None] = mapped_column(Text, nullable=True)
    production_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    unit: Mapped[str] = mapped_column(String(20), default="只", nullable=False)
    sale_unit_price: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 6),
        nullable=True,
    )
    sale_unit_price_no_tax: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 6),
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
    # v0.19.2-B: 报料尺寸 + 压线信息（单位 mm；卡纸可保留小数，瓦楞纸板为整数）
    report_length_mm: Mapped[int | float | None] = mapped_column(SheetDimensionColumn, nullable=True)
    report_width_mm: Mapped[int | float | None] = mapped_column(SheetDimensionColumn, nullable=True)
    crease_type: Mapped[str | None] = mapped_column(String(20), nullable=True)   # 毛片 / 净料 / 压线
    crease_left_mm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    crease_middle_mm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    crease_right_mm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    report_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    base_report_length_mm: Mapped[int | float | None] = mapped_column(SheetDimensionColumn, nullable=True)
    base_report_width_mm: Mapped[int | float | None] = mapped_column(SheetDimensionColumn, nullable=True)
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
    production_label_enabled: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default=false(),
        nullable=False,
    )
    production_label_units_per_label: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )
    flap_mm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_composite: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
    )
    is_virtual_composite_parent: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default=false(),
        nullable=False,
    )
    combination_mode: Mapped[str] = mapped_column(
        String(30),
        default="parent_priced_set",
        server_default="parent_priced_set",
        nullable=False,
    )
    composite_fulfillment_mode: Mapped[str] = mapped_column(
        String(30),
        default="component_delivery",
        server_default="component_delivery",
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
    printing_plate_1: Mapped["PrintingPlate | None"] = relationship(
        foreign_keys=[printing_plate_1_id]
    )
    printing_plate_2: Mapped["PrintingPlate | None"] = relationship(
        foreign_keys=[printing_plate_2_id]
    )
    printing_plate_3: Mapped["PrintingPlate | None"] = relationship(
        foreign_keys=[printing_plate_3_id]
    )
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
