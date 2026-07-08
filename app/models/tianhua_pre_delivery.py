from __future__ import annotations

from datetime import date, datetime
from sqlalchemy import Boolean, CheckConstraint, Date, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.models import Base


class TianhuaPreDeliveryImportBatch(Base):
    __tablename__ = "tianhua_pre_delivery_import_batches"
    __table_args__ = (CheckConstraint("status IN ('preprocessed','draft_created','failed')", name="ck_tianhua_pre_delivery_batch_status"), UniqueConstraint("batch_number"))
    id: Mapped[int] = mapped_column(primary_key=True)
    batch_number: Mapped[str] = mapped_column(String(50), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), nullable=False)
    customer_name: Mapped[str] = mapped_column(String(200), nullable=False)
    pre_delivery_date: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(30), default="preprocessed", nullable=False)
    total_rows: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.current_timestamp(), nullable=False)
    items: Mapped[list["TianhuaPreDeliveryImportItem"]] = relationship(cascade="all, delete-orphan", order_by="TianhuaPreDeliveryImportItem.row_no")


class TianhuaPreDeliveryImportItem(Base):
    __tablename__ = "tianhua_pre_delivery_import_items"
    __table_args__ = (CheckConstraint("status IN ('ok','duplicate_warning','qty_mismatch','stock_shortage','not_matched','ocr_failed')", name="ck_tianhua_pre_delivery_item_status"), UniqueConstraint("batch_id", "row_no"), Index("ix_tianhua_import_batch", "batch_id"))
    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("tianhua_pre_delivery_import_batches.id", ondelete="CASCADE"), nullable=False)
    row_no: Mapped[int] = mapped_column(Integer, nullable=False)
    raw_text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    stock_code: Mapped[str | None] = mapped_column(String(30))
    image_qty: Mapped[int | None] = mapped_column(Integer)
    image_order_no: Mapped[str | None] = mapped_column(String(150))
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id"))
    product_name: Mapped[str | None] = mapped_column(String(250))
    order_item_id: Mapped[int | None] = mapped_column(ForeignKey("sales_order_items.id"))
    order_id: Mapped[int | None] = mapped_column(ForeignKey("sales_orders.id"))
    order_number: Mapped[str | None] = mapped_column(String(64))
    customer_order_no: Mapped[str | None] = mapped_column(String(150))
    match_reason: Mapped[str | None] = mapped_column(Text)
    match_score: Mapped[int | None] = mapped_column(Integer)
    candidate_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    system_pending_qty: Mapped[int | None] = mapped_column(Integer)
    available_qty: Mapped[int | None] = mapped_column(Integer)
    suggested_qty: Mapped[int | None] = mapped_column(Integer)
    final_delivery_qty: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(30), default="ocr_failed", nullable=False)
    warning: Mapped[str | None] = mapped_column(Text)
    selected: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.current_timestamp(), nullable=False)


class TianhuaPreDeliveryDraft(Base):
    __tablename__ = "tianhua_pre_delivery_drafts"
    __table_args__ = (CheckConstraint("status='draft'"), UniqueConstraint("batch_id"), UniqueConstraint("draft_number"))
    id: Mapped[int] = mapped_column(primary_key=True)
    draft_number: Mapped[str] = mapped_column(String(50), nullable=False)
    batch_id: Mapped[int] = mapped_column(ForeignKey("tianhua_pre_delivery_import_batches.id"), nullable=False)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), nullable=False)
    delivery_id: Mapped[int | None] = mapped_column(ForeignKey("sales_deliveries.id", ondelete="SET NULL"))
    status: Mapped[str] = mapped_column(String(20), default="draft", nullable=False)
    remark: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.current_timestamp(), nullable=False)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime)
    items: Mapped[list["TianhuaPreDeliveryDraftItem"]] = relationship(cascade="all, delete-orphan", order_by="TianhuaPreDeliveryDraftItem.row_no")


class TianhuaPreDeliveryDraftItem(Base):
    __tablename__ = "tianhua_pre_delivery_draft_items"
    __table_args__ = (
        CheckConstraint("delivery_qty>=0"),
        CheckConstraint(
            "mobile_pick_status IN ('pending','picked','no_stock','partial')",
            name="ck_tianhua_draft_item_mobile_pick_status",
        ),
        UniqueConstraint("draft_id", "import_item_id"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    draft_id: Mapped[int] = mapped_column(ForeignKey("tianhua_pre_delivery_drafts.id", ondelete="CASCADE"), nullable=False)
    import_item_id: Mapped[int] = mapped_column(ForeignKey("tianhua_pre_delivery_import_items.id"), nullable=False)
    row_no: Mapped[int] = mapped_column(Integer, nullable=False)
    stock_code: Mapped[str] = mapped_column(String(30), nullable=False)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), nullable=False)
    order_item_id: Mapped[int] = mapped_column(ForeignKey("sales_order_items.id"), nullable=False)
    order_id: Mapped[int] = mapped_column(ForeignKey("sales_orders.id"), nullable=False)
    order_number: Mapped[str] = mapped_column(String(64), nullable=False)
    customer_order_no: Mapped[str | None] = mapped_column(String(150))
    delivery_item_id: Mapped[int | None] = mapped_column(ForeignKey("sales_delivery_items.id", ondelete="SET NULL"))
    delivery_qty: Mapped[int] = mapped_column(Integer, nullable=False)
    warning: Mapped[str | None] = mapped_column(Text)
    mobile_pick_status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    mobile_picked_qty: Mapped[int | None] = mapped_column(Integer)
    mobile_pick_note: Mapped[str | None] = mapped_column(String(500))
    mobile_picked_at: Mapped[datetime | None] = mapped_column(DateTime)
    mobile_picked_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.current_timestamp(), nullable=False)
