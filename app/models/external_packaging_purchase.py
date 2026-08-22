from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models import Base


class ExternalPackagingPurchaseDailySequence(Base):
    __tablename__ = "external_packaging_purchase_daily_sequences"

    sequence_date: Mapped[date] = mapped_column(Date, primary_key=True)
    last_value: Mapped[int] = mapped_column(Integer, nullable=False)


class ExternalPackagingPurchaseBatch(Base):
    """One idempotent, order-level human confirmation."""

    __tablename__ = "external_packaging_purchase_batches"
    __table_args__ = (
        UniqueConstraint(
            "idempotency_key", name="uq_external_packaging_purchase_batch_key"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    sales_order_id: Mapped[int] = mapped_column(
        ForeignKey("sales_orders.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    idempotency_key: Mapped[str] = mapped_column(String(120), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    confirmed_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    confirmed_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )

    purchase_orders: Mapped[list["ExternalPackagingPurchaseOrder"]] = relationship(
        back_populates="batch",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="ExternalPackagingPurchaseOrder.id",
    )


class ExternalPackagingPurchaseOrder(Base):
    __tablename__ = "external_packaging_purchase_orders"
    __table_args__ = (
        UniqueConstraint(
            "batch_id",
            "supplier_id",
            name="uq_external_packaging_purchase_batch_supplier",
        ),
        UniqueConstraint(
            "purchase_number", name="uq_external_packaging_purchase_number"
        ),
        CheckConstraint(
            "status IN ('confirmed')", name="ck_external_packaging_purchase_status"
        ),
        CheckConstraint(
            "goods_amount >= 0", name="ck_external_packaging_purchase_goods_amount"
        ),
        CheckConstraint(
            "tax_amount >= 0", name="ck_external_packaging_purchase_tax_amount"
        ),
        CheckConstraint(
            "total_amount >= 0", name="ck_external_packaging_purchase_total_amount"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    batch_id: Mapped[int] = mapped_column(
        ForeignKey("external_packaging_purchase_batches.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    purchase_number: Mapped[str] = mapped_column(String(40), nullable=False)
    supplier_id: Mapped[int] = mapped_column(
        ForeignKey("supplier_master_records.id", ondelete="RESTRICT"), nullable=False
    )
    supplier_name_snapshot: Mapped[str] = mapped_column(String(200), nullable=False)
    supplier_business_code_snapshot: Mapped[str | None] = mapped_column(
        String(50), nullable=True
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="confirmed", server_default="confirmed"
    )
    goods_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    tax_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    total_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    confirmed_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    confirmed_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )

    batch: Mapped[ExternalPackagingPurchaseBatch] = relationship(
        back_populates="purchase_orders"
    )
    items: Mapped[list["ExternalPackagingPurchaseItem"]] = relationship(
        back_populates="purchase_order",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="ExternalPackagingPurchaseItem.id",
    )


class ExternalPackagingPurchaseItem(Base):
    __tablename__ = "external_packaging_purchase_items"
    __table_args__ = (
        UniqueConstraint(
            "purchase_order_id",
            "order_component_id",
            name="uq_external_packaging_purchase_order_component",
        ),
        CheckConstraint(
            "purchase_quantity > 0", name="ck_external_packaging_purchase_item_qty"
        ),
        CheckConstraint(
            "unit_price > 0", name="ck_external_packaging_purchase_item_price"
        ),
        CheckConstraint(
            "tax_rate >= 0 AND tax_rate <= 1",
            name="ck_external_packaging_purchase_item_tax_rate",
        ),
        CheckConstraint(
            "tax_amount_per_unit IS NULL OR tax_amount_per_unit >= 0",
            name="ck_external_packaging_purchase_item_tax_unit",
        ),
        CheckConstraint(
            "line_amount >= 0 AND tax_amount >= 0 AND total_amount >= 0",
            name="ck_external_packaging_purchase_item_amounts",
        ),
        CheckConstraint(
            "tax_mode IN ('tax_inclusive','tax_exclusive')",
            name="ck_external_packaging_purchase_item_tax_mode",
        ),
        CheckConstraint(
            "shipping_fee_mode IN ('not_provided','included','per_order','per_unit')",
            name="ck_external_packaging_purchase_item_shipping_mode",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    purchase_order_id: Mapped[int] = mapped_column(
        ForeignKey("external_packaging_purchase_orders.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    sales_order_id: Mapped[int] = mapped_column(
        ForeignKey("sales_orders.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    sales_order_item_id: Mapped[int] = mapped_column(
        ForeignKey("sales_order_items.id", ondelete="RESTRICT"), nullable=False
    )
    order_component_id: Mapped[int] = mapped_column(
        ForeignKey("sales_order_item_external_components.id", ondelete="RESTRICT"),
        nullable=False,
    )
    order_candidate_id: Mapped[int] = mapped_column(
        ForeignKey(
            "sales_order_item_external_component_candidates.id", ondelete="RESTRICT"
        ),
        nullable=False,
    )
    purpose_snapshot: Mapped[str] = mapped_column(String(200), nullable=False)
    category_code_snapshot: Mapped[str] = mapped_column(String(50), nullable=False)
    specification_summary_snapshot: Mapped[str] = mapped_column(
        String(500), nullable=False
    )
    specification_json_snapshot: Mapped[str] = mapped_column(
        Text, nullable=False, default="{}"
    )
    external_product_id_snapshot: Mapped[int] = mapped_column(Integer, nullable=False)
    external_product_version_snapshot: Mapped[int] = mapped_column(
        Integer, nullable=False
    )
    supplier_product_code_snapshot: Mapped[str] = mapped_column(
        String(100), nullable=False
    )
    product_name_snapshot: Mapped[str] = mapped_column(String(200), nullable=False)
    price_version_id: Mapped[int] = mapped_column(
        ForeignKey("external_packaging_price_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    price_version_number_snapshot: Mapped[int] = mapped_column(Integer, nullable=False)
    purchase_quantity: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    purchase_unit: Mapped[str] = mapped_column(String(20), nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    tax_mode: Mapped[str] = mapped_column(String(20), nullable=False)
    tax_rate: Mapped[Decimal] = mapped_column(Numeric(8, 6), nullable=False)
    tax_amount_per_unit: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 6), nullable=True
    )
    line_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    tax_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    total_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    tier_basis_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    moq_quantity_snapshot: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 4), nullable=True
    )
    packaging_multiple_snapshot: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 4), nullable=True
    )
    shipping_fee_mode: Mapped[str] = mapped_column(String(20), nullable=False)
    shipping_fee_snapshot: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 6), nullable=True
    )
    sample_fee_snapshot: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 6), nullable=True
    )
    plate_fee_snapshot: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 6), nullable=True
    )
    die_fee_snapshot: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 6), nullable=True
    )
    price_evidence_reference_snapshot: Mapped[str] = mapped_column(
        String(500), nullable=False
    )
    confirmed_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )

    purchase_order: Mapped[ExternalPackagingPurchaseOrder] = relationship(
        back_populates="items"
    )


class ExternalPackagingPurchaseCancellation(Base):
    """Append-only invalidation fact for an unreceived purchase document."""

    __tablename__ = "external_packaging_purchase_cancellations"
    __table_args__ = (
        UniqueConstraint(
            "purchase_order_id",
            name="uq_external_packaging_purchase_cancellation_order",
        ),
        CheckConstraint(
            "source IN ('order_workflow_rollback','order_status_cancelled','order_status_dead','authorized_data_repair','manual_purchase_cancel')",
            name="ck_external_packaging_purchase_cancellation_source",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    purchase_order_id: Mapped[int] = mapped_column(
        ForeignKey("external_packaging_purchase_orders.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    source: Mapped[str] = mapped_column(String(40), nullable=False)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    cancelled_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    cancelled_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )


class ExternalPackagingPurchasePurgeAuthorization(Base):
    """Short-lived database gate for deleting a fully cancelled purchase batch.

    Normal purchase/cancellation facts remain immutable.  A row can only be
    inserted when every purchase order in the batch is cancelled and no receipt
    fact exists; it is removed automatically with the batch in the same
    transaction.
    """

    __tablename__ = "external_packaging_purchase_purge_authorizations"

    batch_id: Mapped[int] = mapped_column(
        ForeignKey("external_packaging_purchase_batches.id", ondelete="CASCADE"),
        primary_key=True,
    )
    authorized_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    authorized_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )


class ExternalPackagingReceipt(Base):
    """One append-only external-packaging receiving event."""

    __tablename__ = "external_packaging_receipts"
    __table_args__ = (
        UniqueConstraint(
            "receipt_number", name="uq_external_packaging_receipt_number"
        ),
        UniqueConstraint(
            "idempotency_key", name="uq_external_packaging_receipt_key"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    purchase_order_id: Mapped[int] = mapped_column(
        ForeignKey("external_packaging_purchase_orders.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    receipt_number: Mapped[str] = mapped_column(String(60), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(120), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    received_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    received_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )

    items: Mapped[list["ExternalPackagingReceiptItem"]] = relationship(
        back_populates="receipt",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="ExternalPackagingReceiptItem.id",
    )


class ExternalPackagingReceiptItem(Base):
    """Original-unit quantity received for one frozen purchase line."""

    __tablename__ = "external_packaging_receipt_items"
    __table_args__ = (
        UniqueConstraint(
            "receipt_id",
            "purchase_item_id",
            name="uq_external_packaging_receipt_purchase_item",
        ),
        CheckConstraint(
            "received_quantity > 0",
            name="ck_external_packaging_receipt_item_quantity",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    receipt_id: Mapped[int] = mapped_column(
        ForeignKey("external_packaging_receipts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    purchase_item_id: Mapped[int] = mapped_column(
        ForeignKey("external_packaging_purchase_items.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    received_quantity: Mapped[Decimal] = mapped_column(
        Numeric(18, 6), nullable=False
    )
    purchase_unit_snapshot: Mapped[str] = mapped_column(String(20), nullable=False)

    receipt: Mapped[ExternalPackagingReceipt] = relationship(back_populates="items")
