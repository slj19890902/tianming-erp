from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Date,
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

if TYPE_CHECKING:
    from app.models.order import OrderItem


class RequisitionDailySequence(Base):
    __tablename__ = "requisition_daily_sequences"

    sequence_date: Mapped[date] = mapped_column(Date, primary_key=True)
    last_value: Mapped[int] = mapped_column(Integer, nullable=False)


class Requisition(Base):
    __tablename__ = "material_requisitions"
    __table_args__ = (
        UniqueConstraint(
            "requisition_number",
            name="uq_material_requisitions_number",
        ),
        Index("ix_material_requisitions_date", "requisition_date"),
        Index("ix_material_requisitions_status", "status"),
        CheckConstraint(
            "((request_key IS NULL AND request_hash IS NULL "
            "AND request_actor_id IS NULL) OR "
            "(request_key IS NOT NULL AND length(request_hash) = 64 "
            "AND request_actor_id IS NOT NULL))",
            name="ck_material_requisitions_request_fact",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    requisition_number: Mapped[str] = mapped_column(String(40), nullable=False)
    request_key: Mapped[str | None] = mapped_column(
        String(64), nullable=True, unique=True
    )
    request_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    request_actor_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )
    requisition_date: Mapped[date] = mapped_column(Date, nullable=False)
    supplier_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    status: Mapped[str] = mapped_column(
        String(30),
        default="已报料",
        nullable=False,
    )
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.current_timestamp(),
        nullable=False,
    )

    items: Mapped[list["RequisitionItem"]] = relationship(
        back_populates="requisition",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="RequisitionItem.id",
    )


class RequisitionItem(Base):
    sheet_cutting_snapshot: Mapped[dict | None] = mapped_column(JSON(none_as_null=True), nullable=True)
    __tablename__ = "material_requisition_items"
    __table_args__ = (
        Index(
            "ix_material_requisition_items_requisition_id",
            "requisition_id",
        ),
        Index(
            "ix_material_requisition_items_order_item_id",
            "order_item_id",
        ),
        CheckConstraint(
            "purpose_contract_status IN ('legacy_unset','frozen')",
            name="ck_material_requisition_items_purpose_contract_status",
        ),
        CheckConstraint(
            "version >= 1",
            name="ck_material_requisition_items_version",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    requisition_id: Mapped[int] = mapped_column(
        ForeignKey("material_requisitions.id", ondelete="CASCADE"),
        nullable=False,
    )
    order_item_id: Mapped[int] = mapped_column(
        ForeignKey("sales_order_items.id", ondelete="RESTRICT"),
        nullable=False,
    )
    inventory_deducted_qty: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
    )
    requisition_qty: Mapped[int] = mapped_column(Integer, nullable=False)
    cardboard_len: Mapped[Decimal] = mapped_column(
        Numeric(12, 2),
        nullable=False,
    )
    cardboard_width: Mapped[Decimal] = mapped_column(
        Numeric(12, 2),
        nullable=False,
    )
    pieces_per_box: Mapped[int | None] = mapped_column(Integer, nullable=True)
    required_piece_qty: Mapped[int | None] = mapped_column(Integer, nullable=True)
    special_process: Mapped[str] = mapped_column(
        String(30),
        default="无",
        nullable=False,
    )
    material_snapshot: Mapped[str | None] = mapped_column(
        String(250),
        nullable=True,
    )
    product_code_snapshot: Mapped[str | None] = mapped_column(
        String(150),
        nullable=True,
    )
    product_name_snapshot: Mapped[str] = mapped_column(
        String(250),
        nullable=False,
    )
    specification_snapshot: Mapped[str | None] = mapped_column(
        String(150),
        nullable=True,
    )
    remark: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(30),
        default="有效",
        nullable=False,
    )
    purpose_contract_status: Mapped[str] = mapped_column(
        String(20),
        default="legacy_unset",
        server_default="legacy_unset",
        nullable=False,
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

    requisition: Mapped["Requisition"] = relationship(back_populates="items")
    order_item: Mapped["OrderItem"] = relationship()


class RequisitionHold(Base):
    """A queue-only pause for one still-pending order item.

    This record deliberately does not change ``OrderItem.requisition_status``.
    It preserves the existing "unreported" business gate while allowing the
    pending-requisition query to exclude a separately governed waiting item.
    """

    __tablename__ = "requisition_holds"
    __table_args__ = (
        CheckConstraint(
            "release_mode IN ('previous_batch_completed', 'expected_date')",
            name="ck_requisition_holds_release_mode",
        ),
        CheckConstraint(
            "status IN ('active', 'released', 'invalidated')",
            name="ck_requisition_holds_status",
        ),
        CheckConstraint(
            "status <> 'active' OR ((release_mode = 'previous_batch_completed' "
            "AND previous_order_item_id_snapshot IS NOT NULL "
            "AND expected_requisition_date IS NULL) "
            "OR (release_mode = 'expected_date' "
            "AND previous_order_item_id_snapshot IS NULL "
            "AND expected_requisition_date IS NOT NULL))",
            name="ck_requisition_holds_one_release_condition",
        ),
        CheckConstraint(
            "previous_order_item_id IS NULL "
            "OR previous_order_item_id <> order_item_id",
            name="ck_requisition_holds_previous_not_self",
        ),
        CheckConstraint("version >= 1", name="ck_requisition_holds_version"),
        # SQLite supports this partial unique index.  It allows closed audit
        # rows while preventing two simultaneous queue holds for one item.
        Index(
            "uq_requisition_holds_active_order_item",
            "order_item_id",
            unique=True,
            sqlite_where=text("status = 'active'"),
        ),
        Index("ix_requisition_holds_previous_order_item", "previous_order_item_id"),
        Index(
            "ix_requisition_holds_active_expected_date",
            "status",
            "expected_requisition_date",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # Keep the operational link while the order item exists.  A later order
    # item deletion sets it to NULL and a database trigger invalidates the
    # hold; immutable snapshots retain the audit trail without a ghost queue
    # row or a delete-blocking foreign key.
    order_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("sales_order_items.id", ondelete="SET NULL"),
        nullable=True,
    )
    order_item_id_snapshot: Mapped[int] = mapped_column(Integer, nullable=False)
    customer_id_snapshot: Mapped[int | None] = mapped_column(Integer, nullable=True)
    customer_name_snapshot: Mapped[str] = mapped_column(String(200), nullable=False)
    order_number_snapshot: Mapped[str] = mapped_column(String(64), nullable=False)
    order_item_sequence_snapshot: Mapped[int | None] = mapped_column(Integer, nullable=True)
    product_code_snapshot: Mapped[str | None] = mapped_column(String(150), nullable=True)
    product_name_snapshot: Mapped[str] = mapped_column(String(250), nullable=False)
    specification_snapshot: Mapped[str | None] = mapped_column(String(150), nullable=True)
    quantity_snapshot: Mapped[int] = mapped_column(Integer, nullable=False)
    release_mode: Mapped[str] = mapped_column(String(40), nullable=False)
    previous_order_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("sales_order_items.id", ondelete="SET NULL"),
        nullable=True,
    )
    previous_order_item_id_snapshot: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    expected_requisition_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), default="active", server_default="active", nullable=False
    )
    version: Mapped[int] = mapped_column(
        Integer, default=1, server_default="1", nullable=False
    )
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    updated_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    released_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    released_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    release_source: Mapped[str | None] = mapped_column(String(40), nullable=True)
    release_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime, onupdate=func.current_timestamp(), nullable=True
    )

    order_item: Mapped["OrderItem"] = relationship(
        back_populates="requisition_holds", foreign_keys=[order_item_id]
    )
    previous_order_item: Mapped["OrderItem | None"] = relationship(
        foreign_keys=[previous_order_item_id]
    )
