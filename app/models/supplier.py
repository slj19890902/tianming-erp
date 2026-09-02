from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models import Base


class Supplier(Base):
    __tablename__ = "supplier_master_records"
    __table_args__ = (
        UniqueConstraint(
            "normalized_name",
            name="uq_supplier_master_records_normalized_name",
        ),
        UniqueConstraint(
            "normalized_business_code",
            name="uq_supplier_master_records_normalized_business_code",
        ),
        CheckConstraint(
            "version >= 1",
            name="ck_supplier_master_records_version",
        ),
        CheckConstraint(
            "sort_order >= 0",
            name="ck_supplier_master_records_sort_order",
        ),
        CheckConstraint(
            "settlement_day BETWEEN 1 AND 31",
            name="ck_supplier_master_records_settlement_day",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    standard_name: Mapped[str] = mapped_column(String(200), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(200), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    business_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    normalized_business_code: Mapped[str | None] = mapped_column(
        String(50),
        nullable=True,
    )
    contact_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(100), nullable=True)
    remarks: Mapped[str | None] = mapped_column(Text, nullable=True)
    sort_order: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=100,
        server_default="100",
    )
    settlement_day: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=20,
        server_default="20",
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default="1",
        index=True,
    )
    version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default="1",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        server_default=func.current_timestamp(),
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
        onupdate=func.current_timestamp(),
    )

    aliases: Mapped[list["SupplierAlias"]] = relationship(
        back_populates="supplier",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="SupplierAlias.id",
    )
    supply_categories: Mapped[list["SupplierSupplyCategory"]] = relationship(
        back_populates="supplier",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="SupplierSupplyCategory.id",
    )
    packaging_products: Mapped[list["ExternalPackagingProduct"]] = relationship(
        back_populates="supplier",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="ExternalPackagingProduct.id",
    )


class SupplierAlias(Base):
    __tablename__ = "supplier_master_aliases"
    __table_args__ = (
        UniqueConstraint(
            "normalized_alias",
            name="uq_supplier_master_aliases_normalized_alias",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    supplier_id: Mapped[int] = mapped_column(
        ForeignKey("supplier_master_records.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    alias_name: Mapped[str] = mapped_column(String(200), nullable=False)
    normalized_alias: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        server_default=func.current_timestamp(),
    )

    supplier: Mapped[Supplier] = relationship(back_populates="aliases")


class SupplierSupplyCategory(Base):
    __tablename__ = "supplier_supply_categories"
    __table_args__ = (
        UniqueConstraint(
            "supplier_id",
            "category_code",
            name="uq_supplier_supply_categories_supplier_category",
        ),
        CheckConstraint(
            "category_code IN ('corrugated_board','paper_corner_guard','coated_board','printed_folding_carton','epe_cushion','hollow_board','honeycomb_board','other_packaging')",
            name="ck_supplier_supply_categories_code",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    supplier_id: Mapped[int] = mapped_column(
        ForeignKey("supplier_master_records.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    category_code: Mapped[str] = mapped_column(String(50), nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default="1",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        server_default=func.current_timestamp(),
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
        onupdate=func.current_timestamp(),
    )

    supplier: Mapped[Supplier] = relationship(back_populates="supply_categories")


class ExternalPackagingProduct(Base):
    __tablename__ = "external_packaging_products"
    __table_args__ = (
        UniqueConstraint(
            "supplier_id",
            "normalized_supplier_product_code",
            name="uq_external_packaging_products_supplier_code",
        ),
        CheckConstraint("version >= 1", name="ck_external_packaging_products_version"),
        CheckConstraint(
            "lead_time_days IS NULL OR lead_time_days >= 0",
            name="ck_external_packaging_products_lead_time",
        ),
        CheckConstraint(
            "category_code IN ('paper_corner_guard','coated_board','printed_folding_carton','epe_cushion','hollow_board','honeycomb_board','other_packaging')",
            name="ck_external_packaging_products_category",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    supplier_id: Mapped[int] = mapped_column(
        ForeignKey("supplier_master_records.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    customer_scope_id: Mapped[int | None] = mapped_column(
        ForeignKey("customers.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    category_code: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    supplier_product_code: Mapped[str] = mapped_column(String(100), nullable=False)
    normalized_supplier_product_code: Mapped[str] = mapped_column(
        String(100), nullable=False
    )
    product_name: Mapped[str] = mapped_column(String(200), nullable=False)
    purchase_unit: Mapped[str] = mapped_column(String(20), nullable=False)
    specification_summary: Mapped[str] = mapped_column(String(500), nullable=False)
    specification_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    drawing_sample_version: Mapped[str | None] = mapped_column(String(100), nullable=True)
    lead_time_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    remarks: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1", index=True
    )
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, onupdate=func.current_timestamp()
    )

    supplier: Mapped[Supplier] = relationship(back_populates="packaging_products")
    customer_scope = relationship("Customer")
