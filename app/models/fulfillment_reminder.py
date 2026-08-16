from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
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
    true,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base


class FulfillmentReminder(Base):
    """Internal-only customer fulfillment reminder originating from a receipt."""

    __tablename__ = "fulfillment_reminders"
    __table_args__ = (
        CheckConstraint(
            "scope_type IN ('receipt','customer','product')",
            name="ck_fulfillment_reminders_scope_type",
        ),
        CheckConstraint(
            "reminder_type IN "
            "('replenishment','delivery_attention','production_attention','other')",
            name="ck_fulfillment_reminders_type",
        ),
        CheckConstraint(
            "cadence IN ('one_time','continuous')",
            name="ck_fulfillment_reminders_cadence",
        ),
        CheckConstraint(
            "status IN ('active','resolved','cancelled')",
            name="ck_fulfillment_reminders_status",
        ),
        CheckConstraint(
            "suggested_quantity IS NULL OR suggested_quantity > 0",
            name="ck_fulfillment_reminders_suggested_quantity",
        ),
        CheckConstraint(
            "version >= 1",
            name="ck_fulfillment_reminders_version",
        ),
        CheckConstraint(
            "(scope_type = 'product' AND product_id_snapshot IS NOT NULL "
            "AND product_code_snapshot IS NOT NULL AND product_name_snapshot IS NOT NULL) "
            "OR (scope_type <> 'product' AND product_id_snapshot IS NULL "
            "AND product_id IS NULL AND product_code_snapshot IS NULL "
            "AND product_name_snapshot IS NULL)",
            name="ck_fulfillment_reminders_product_scope",
        ),
        Index(
            "ix_fulfillment_reminders_customer_status_due_id",
            "customer_id",
            "status",
            "remind_on",
            "id",
        ),
        Index(
            "ix_fulfillment_reminders_product_status_due_id",
            "product_id_snapshot",
            "status",
            "remind_on",
            "id",
        ),
        Index(
            "ix_fulfillment_reminders_source_receipt",
            "source_return_receipt_id_snapshot",
            "status",
            "id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    source_return_receipt_id: Mapped[int | None] = mapped_column(
        ForeignKey("finance_return_receipts.id", ondelete="SET NULL"),
        nullable=True,
    )
    source_return_receipt_id_snapshot: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )
    source_delivery_id_snapshot: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )
    source_delivery_number_snapshot: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
    )
    source_received_date_snapshot: Mapped[date] = mapped_column(
        Date,
        nullable=False,
    )
    source_valid: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default=true(),
        nullable=False,
    )
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id", ondelete="RESTRICT"),
        nullable=False,
    )
    customer_name_snapshot: Mapped[str] = mapped_column(
        String(200),
        nullable=False,
    )
    product_id: Mapped[int | None] = mapped_column(
        ForeignKey("products.id", ondelete="SET NULL"),
        nullable=True,
    )
    product_id_snapshot: Mapped[int | None] = mapped_column(Integer, nullable=True)
    product_code_snapshot: Mapped[str | None] = mapped_column(
        String(150),
        nullable=True,
    )
    product_name_snapshot: Mapped[str | None] = mapped_column(
        String(250),
        nullable=True,
    )
    scope_type: Mapped[str] = mapped_column(String(20), nullable=False)
    reminder_type: Mapped[str] = mapped_column(String(30), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    suggested_quantity: Mapped[Decimal | None] = mapped_column(
        Numeric(14, 3),
        nullable=True,
    )
    cadence: Mapped[str] = mapped_column(String(20), nullable=False)
    remind_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(
        String(20),
        default="active",
        server_default="active",
        nullable=False,
    )
    version: Mapped[int] = mapped_column(
        Integer,
        default=1,
        server_default="1",
        nullable=False,
    )
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_by_name_snapshot: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )
    updated_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    resolved_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    cancelled_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.current_timestamp(),
        nullable=False,
    )
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class FulfillmentReminderMutation(Base):
    """Durable replay receipt for reminder writes and receipt/reminder bundles."""

    __tablename__ = "fulfillment_reminder_mutations"
    __table_args__ = (
        UniqueConstraint(
            "idempotency_key",
            name="uq_fulfillment_reminder_mutations_key",
        ),
        CheckConstraint(
            "action IN "
            "('create','update','resolve','cancel','receipt_create_bundle')",
            name="ck_fulfillment_reminder_mutations_action",
        ),
        Index(
            "ix_fulfillment_reminder_mutations_reminder_created",
            "reminder_id",
            "created_at",
            "id",
        ),
        Index(
            "ix_fulfillment_reminder_mutations_receipt_created",
            "return_receipt_id_snapshot",
            "created_at",
            "id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    reminder_id: Mapped[int | None] = mapped_column(
        ForeignKey("fulfillment_reminders.id", ondelete="SET NULL"),
        nullable=True,
    )
    return_receipt_id_snapshot: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )
    idempotency_key: Mapped[str] = mapped_column(String(120), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(30), nullable=False)
    actor_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    actor_name_snapshot: Mapped[str] = mapped_column(String(100), nullable=False)
    response_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.current_timestamp(),
        nullable=False,
    )
