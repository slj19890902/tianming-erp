from __future__ import annotations

from datetime import datetime
from decimal import Decimal

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
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models import Base


class ProductExternalComponentSet(Base):
    __tablename__ = "product_external_component_sets"
    __table_args__ = (
        UniqueConstraint(
            "product_id", "version", name="uq_product_external_component_set_version"
        ),
        CheckConstraint("version >= 1", name="ck_product_external_component_set_version"),
        Index(
            "uq_product_external_component_set_current",
            "product_id",
            unique=True,
            sqlite_where=text("is_current = 1"),
            postgresql_where=text("is_current = true"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    is_current: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1"
    )
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )

    components: Mapped[list["ProductExternalComponent"]] = relationship(
        back_populates="component_set",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="ProductExternalComponent.display_order",
    )


class ProductExternalComponent(Base):
    __tablename__ = "product_external_components"
    __table_args__ = (
        UniqueConstraint(
            "component_set_id",
            "display_order",
            name="uq_product_external_component_order",
        ),
        CheckConstraint(
            "quantity_per_finished_unit > 0",
            name="ck_product_external_component_quantity",
        ),
        CheckConstraint(
            "waste_rate >= 0 AND waste_rate <= 1",
            name="ck_product_external_component_waste_rate",
        ),
        CheckConstraint(
            "units_per_purchase_unit IS NULL OR units_per_purchase_unit > 0",
            name="ck_product_external_component_conversion",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    component_set_id: Mapped[int] = mapped_column(
        ForeignKey("product_external_component_sets.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    display_order: Mapped[int] = mapped_column(Integer, nullable=False)
    purpose: Mapped[str] = mapped_column(String(200), nullable=False)
    quantity_per_finished_unit: Mapped[Decimal] = mapped_column(
        Numeric(18, 6), nullable=False
    )
    waste_rate: Mapped[Decimal] = mapped_column(
        Numeric(8, 6), nullable=False, default=Decimal("0"), server_default="0"
    )
    consumption_unit: Mapped[str] = mapped_column(String(20), nullable=False)
    units_per_purchase_unit: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 6), nullable=True
    )
    conversion_basis: Mapped[str | None] = mapped_column(String(500), nullable=True)
    is_required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1"
    )
    remarks: Mapped[str | None] = mapped_column(Text, nullable=True)
    category_code: Mapped[str] = mapped_column(String(50), nullable=False)
    specification_json: Mapped[str] = mapped_column(Text, nullable=False)
    specification_summary: Mapped[str] = mapped_column(String(500), nullable=False)

    component_set: Mapped[ProductExternalComponentSet] = relationship(
        back_populates="components"
    )
    candidates: Mapped[list["ProductExternalComponentCandidate"]] = relationship(
        back_populates="component",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="ProductExternalComponentCandidate.id",
    )


class ProductExternalComponentCandidate(Base):
    __tablename__ = "product_external_component_candidates"
    __table_args__ = (
        UniqueConstraint(
            "component_id",
            "external_product_id",
            name="uq_product_external_component_candidate",
        ),
        Index(
            "uq_product_external_component_default_candidate",
            "component_id",
            unique=True,
            sqlite_where=text("is_default = 1"),
            postgresql_where=text("is_default = true"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    component_id: Mapped[int] = mapped_column(
        ForeignKey("product_external_components.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    external_product_id: Mapped[int] = mapped_column(
        ForeignKey("external_packaging_products.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    is_default: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    supplier_id_snapshot: Mapped[int] = mapped_column(Integer, nullable=False)
    supplier_name_snapshot: Mapped[str] = mapped_column(String(200), nullable=False)
    supplier_product_code_snapshot: Mapped[str] = mapped_column(
        String(100), nullable=False
    )
    product_name_snapshot: Mapped[str] = mapped_column(String(200), nullable=False)
    purchase_unit_snapshot: Mapped[str] = mapped_column(String(20), nullable=False)
    customer_scope_id_snapshot: Mapped[int | None] = mapped_column(Integer, nullable=True)
    external_product_version_snapshot: Mapped[int] = mapped_column(Integer, nullable=False)

    component: Mapped[ProductExternalComponent] = relationship(
        back_populates="candidates"
    )
    external_product = relationship("ExternalPackagingProduct")
