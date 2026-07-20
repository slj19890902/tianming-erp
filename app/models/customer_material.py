from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base


class CustomerMaterialCandidate(Base):
    __tablename__ = "customer_material_candidates"
    __table_args__ = (
        CheckConstraint(
            "manual_priority >= 0",
            name="ck_customer_material_candidates_priority",
        ),
        UniqueConstraint(
            "customer_id",
            "normalized_original_material_code",
            "normalized_supplier_name",
            "actual_material_code_snapshot",
            name="uq_customer_material_candidates_business_key",
        ),
        Index(
            "ix_customer_material_candidates_lookup",
            "customer_id",
            "normalized_original_material_code",
            "is_active",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    original_material_code: Mapped[str] = mapped_column(String(250), nullable=False)
    normalized_original_material_code: Mapped[str] = mapped_column(
        String(250), nullable=False
    )
    supplier_name: Mapped[str] = mapped_column(String(200), nullable=False)
    normalized_supplier_name: Mapped[str] = mapped_column(String(200), nullable=False)
    actual_material_id: Mapped[int | None] = mapped_column(
        ForeignKey("materials.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    actual_material_code_snapshot: Mapped[str] = mapped_column(
        String(100), nullable=False
    )
    manual_priority: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="1", nullable=False
    )
    source: Mapped[str] = mapped_column(
        String(50), default="manual", server_default="manual", nullable=False
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    updated_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime, onupdate=func.current_timestamp(), nullable=True
    )


class CustomerMaterialSelectionHistory(Base):
    __tablename__ = "customer_material_selection_history"
    __table_args__ = (
        Index(
            "ix_customer_material_selection_history_lookup",
            "customer_id",
            "normalized_original_material_code_snapshot",
            "selected_at",
        ),
        Index(
            "ix_customer_material_selection_history_order_item",
            "order_item_id",
            "selected_at",
        ),
        Index(
            "ix_customer_material_selection_history_product",
            "product_id",
            "selected_at",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    order_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("sales_order_items.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    product_id: Mapped[int | None] = mapped_column(
        ForeignKey("products.id", ondelete="SET NULL"), nullable=True
    )
    candidate_id: Mapped[int | None] = mapped_column(
        ForeignKey("customer_material_candidates.id", ondelete="SET NULL"),
        nullable=True,
    )
    original_material_code_snapshot: Mapped[str | None] = mapped_column(
        String(250), nullable=True
    )
    normalized_original_material_code_snapshot: Mapped[str | None] = mapped_column(
        String(250), nullable=True
    )
    original_material_confidence: Mapped[str] = mapped_column(
        String(30), default="frozen", server_default="frozen", nullable=False
    )
    selected_material_id: Mapped[int | None] = mapped_column(
        ForeignKey("materials.id", ondelete="SET NULL"), nullable=True
    )
    selected_material_code_snapshot: Mapped[str] = mapped_column(
        String(100), nullable=False
    )
    selected_supplier_name_snapshot: Mapped[str | None] = mapped_column(
        String(200), nullable=True
    )
    layer_count_snapshot: Mapped[int | None] = mapped_column(Integer, nullable=True)
    flute_type_snapshot: Mapped[str | None] = mapped_column(String(50), nullable=True)
    source_type: Mapped[str] = mapped_column(
        String(50), default="manual", server_default="manual", nullable=False
    )
    source_reference: Mapped[str | None] = mapped_column(String(250), nullable=True)
    selection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    sync_product: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0", nullable=False
    )
    selected_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    selected_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )
