"""Typed links from a supplier purchase line to its existing receipt ledger."""
from datetime import datetime
from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column
from app.models import Base


class ProcurementSourceLink(Base):
    __tablename__ = "procurement_source_links"
    __table_args__ = (
        CheckConstraint("(stock_replenishment_item_id IS NOT NULL AND material_requisition_item_id IS NULL) OR "
            "(stock_replenishment_item_id IS NULL AND material_requisition_item_id IS NOT NULL)",
            name="ck_procurement_source_kind"),
        CheckConstraint("status IN ('active','voided')", name="ck_procurement_source_status"),
        CheckConstraint("source_quantity > 0", name="ck_procurement_source_quantity"),
        Index("uq_procurement_active_stock", "stock_replenishment_item_id", unique=True,
            sqlite_where=text("status = 'active'"), postgresql_where=text("status = 'active'")),
        Index("uq_procurement_active_material", "material_requisition_item_id", unique=True,
            sqlite_where=text("status = 'active'"), postgresql_where=text("status = 'active'")),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    supplier_item_id: Mapped[int] = mapped_column(ForeignKey("supplier_requisition_order_items.id", ondelete="RESTRICT"), unique=True)
    stock_replenishment_item_id: Mapped[int | None] = mapped_column(ForeignKey("stock_replenishment_order_items.id", ondelete="RESTRICT"))
    material_requisition_item_id: Mapped[int | None] = mapped_column(ForeignKey("material_requisition_items.id", ondelete="RESTRICT"))
    source_quantity: Mapped[int] = mapped_column(Integer)
    source_snapshot_json: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="active", server_default="active")
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.current_timestamp())
