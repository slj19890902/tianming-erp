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
