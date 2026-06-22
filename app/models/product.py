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
        Index("ix_products_customer_id", "customer_id"),
        Index("ix_products_material_id", "material_id"),
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
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
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
    drawings: Mapped[list["ProductDrawing"]] = relationship(
        back_populates="product",
        order_by=(
            "desc(ProductDrawing.uploaded_at), "
            "desc(ProductDrawing.id)"
        ),
    )
