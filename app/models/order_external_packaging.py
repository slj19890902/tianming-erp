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


class SalesOrderItemExternalComponent(Base):
    """Immutable external-packaging facts frozen when an order item is created."""

    __tablename__ = "sales_order_item_external_components"
    __table_args__ = (
        UniqueConstraint(
            "sales_order_item_id",
            "source_component_id",
            name="uq_sales_order_item_external_component_source",
        ),
        UniqueConstraint(
            "sales_order_item_id",
            "display_order",
            name="uq_sales_order_item_external_component_order",
        ),
        CheckConstraint(
            "source_kind IN ('bound_component','direct_product')",
            name="ck_sales_order_item_external_component_source_kind",
        ),
        CheckConstraint(
            "((source_kind = 'bound_component' AND source_component_set_id IS NOT NULL "
            "AND source_component_id IS NOT NULL) OR "
            "(source_kind = 'direct_product' AND source_component_set_id IS NULL "
            "AND source_component_id IS NULL))",
            name="ck_sales_order_item_external_component_source",
        ),
        CheckConstraint(
            "source_component_set_version >= 1",
            name="ck_sales_order_item_external_component_set_version",
        ),
        CheckConstraint(
            "quantity_per_finished_unit > 0",
            name="ck_sales_order_item_external_component_quantity",
        ),
        CheckConstraint(
            "waste_rate >= 0 AND waste_rate <= 1",
            name="ck_sales_order_item_external_component_waste_rate",
        ),
        CheckConstraint(
            "units_per_purchase_unit IS NULL OR units_per_purchase_unit > 0",
            name="ck_sales_order_item_external_component_conversion",
        ),
        Index(
            "ix_sales_order_item_external_components_order_item_id",
            "sales_order_item_id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    sales_order_item_id: Mapped[int] = mapped_column(
        ForeignKey("sales_order_items.id", ondelete="CASCADE"), nullable=False
    )
    source_kind: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        default="bound_component",
        server_default="bound_component",
    )
    source_component_set_id: Mapped[int | None] = mapped_column(
        ForeignKey("product_external_component_sets.id", ondelete="RESTRICT"),
        nullable=True,
    )
    source_component_id: Mapped[int | None] = mapped_column(
        ForeignKey("product_external_components.id", ondelete="RESTRICT"),
        nullable=True,
    )
    source_component_set_version: Mapped[int] = mapped_column(Integer, nullable=False)
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
        Boolean, nullable=False, default=True, server_default=text("1")
    )
    remarks: Mapped[str | None] = mapped_column(Text, nullable=True)
    category_code: Mapped[str] = mapped_column(String(50), nullable=False)
    specification_json: Mapped[str] = mapped_column(Text, nullable=False)
    specification_summary: Mapped[str] = mapped_column(String(500), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )

    candidates: Mapped[list["SalesOrderItemExternalComponentCandidate"]] = relationship(
        back_populates="component",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="SalesOrderItemExternalComponentCandidate.id",
    )


class SalesOrderItemExternalComponentCandidate(Base):
    __tablename__ = "sales_order_item_external_component_candidates"
    __table_args__ = (
        UniqueConstraint(
            "order_component_id",
            "source_candidate_id",
            name="uq_sales_order_item_external_candidate_source",
        ),
        Index(
            "ix_sales_order_item_external_candidates_component_id",
            "order_component_id",
        ),
        Index(
            "uq_sales_order_item_external_candidate_default",
            "order_component_id",
            unique=True,
            sqlite_where=text("is_default = 1"),
            postgresql_where=text("is_default = true"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    order_component_id: Mapped[int] = mapped_column(
        ForeignKey("sales_order_item_external_components.id", ondelete="CASCADE"),
        nullable=False,
    )
    source_candidate_id: Mapped[int | None] = mapped_column(
        ForeignKey("product_external_component_candidates.id", ondelete="RESTRICT"),
        nullable=True,
    )
    external_product_id_snapshot: Mapped[int] = mapped_column(Integer, nullable=False)
    is_default: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
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

    component: Mapped[SalesOrderItemExternalComponent] = relationship(
        back_populates="candidates"
    )
