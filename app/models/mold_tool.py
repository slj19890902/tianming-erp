from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

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
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models import Base

if TYPE_CHECKING:
    from app.models.product import Product
    from app.models.user import User


class MoldTool(Base):
    """Physical die/mold master and its fixed warehouse rack location."""

    __tablename__ = "mold_tools"
    __table_args__ = (
        UniqueConstraint("mold_code", name="uq_mold_tools_code"),
        Index("ix_mold_tools_active_location", "is_active", "rack_location"),
        Index("ix_mold_tools_archive_status", "archive_status", "rack_location"),
        CheckConstraint(
            "archive_status IN ('active', 'archived')",
            name="ck_mold_tools_archive_status",
        ),
        CheckConstraint(
            "repair_status IN ('normal', 'needs_repair')",
            name="ck_mold_tools_repair_status",
        ),
        CheckConstraint(
            "repair_version >= 1",
            name="ck_mold_tools_repair_version",
        ),
        CheckConstraint(
            "((archive_status = 'active' AND archived_at IS NULL "
            "AND archived_by IS NULL AND archive_reason IS NULL "
            "AND pre_archive_location IS NULL) OR "
            "(archive_status = 'archived' AND is_active = false "
            "AND archived_at IS NOT NULL AND archived_by IS NOT NULL "
            "AND archive_reason IS NOT NULL AND pre_archive_location IS NOT NULL))",
            name="ck_mold_tools_archive_state",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    mold_code: Mapped[str] = mapped_column(String(100), nullable=False)
    mold_name: Mapped[str] = mapped_column(String(200), nullable=False)
    rack_location: Mapped[str] = mapped_column(String(250), nullable=False)
    location_version: Mapped[int] = mapped_column(
        Integer,
        default=1,
        server_default="1",
        nullable=False,
    )
    repair_status: Mapped[str] = mapped_column(
        String(20), default="normal", server_default="normal", nullable=False
    )
    repair_version: Mapped[int] = mapped_column(
        Integer, default=1, server_default="1", nullable=False
    )
    last_location_confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
    )
    last_location_confirmed_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    remarks: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    archive_status: Mapped[str] = mapped_column(
        String(20), default="active", server_default="active", nullable=False
    )
    archived_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    archived_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    archive_reason: Mapped[str | None] = mapped_column(String(50), nullable=True)
    pre_archive_location: Mapped[str | None] = mapped_column(String(250), nullable=True)
    restored_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    restored_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
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

    products: Mapped[list["Product"]] = relationship(
        back_populates="mold_tool",
        passive_deletes=True,
    )
    location_movements: Mapped[list["MoldLocationMovement"]] = relationship(
        back_populates="mold_tool",
        passive_deletes=True,
        order_by="MoldLocationMovement.id",
    )
    label_print_items: Mapped[list["MoldLabelPrintJobItem"]] = relationship(
        back_populates="mold_tool",
        passive_deletes=True,
        order_by="MoldLabelPrintJobItem.id",
    )
    scan_events: Mapped[list["MoldScanEvent"]] = relationship(
        back_populates="mold_tool",
        passive_deletes=True,
        order_by="MoldScanEvent.id",
    )
    repair_events: Mapped[list["MoldRepairEvent"]] = relationship(
        back_populates="mold_tool",
        passive_deletes=True,
        order_by="MoldRepairEvent.id",
    )
    last_location_confirmer: Mapped["User | None"] = relationship(
        foreign_keys=[last_location_confirmed_by],
    )
    archiver: Mapped["User | None"] = relationship(foreign_keys=[archived_by])
    restorer: Mapped["User | None"] = relationship(foreign_keys=[restored_by])


class MoldLocationMovement(Base):
    """Immutable audit ledger for confirmed mold-location changes."""

    __tablename__ = "mold_location_movements"
    __table_args__ = (
        UniqueConstraint(
            "idempotency_key",
            name="uq_mold_location_movements_idempotency_key",
        ),
        CheckConstraint(
            "expected_version >= 1",
            name="ck_mold_location_movements_expected_version",
        ),
        CheckConstraint(
            "resulting_version = expected_version + 1",
            name="ck_mold_location_movements_resulting_version",
        ),
        CheckConstraint(
            "from_location <> to_location",
            name="ck_mold_location_movements_actual_change",
        ),
        Index(
            "ix_mold_location_movements_mold_time",
            "mold_tool_id",
            "moved_at",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    mold_tool_id: Mapped[int] = mapped_column(
        ForeignKey("mold_tools.id", ondelete="RESTRICT"),
        nullable=False,
    )
    mold_code_snapshot: Mapped[str] = mapped_column(String(100), nullable=False)
    from_location: Mapped[str] = mapped_column(String(250), nullable=False)
    to_location: Mapped[str] = mapped_column(String(250), nullable=False)
    actor_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    moved_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.current_timestamp(),
        nullable=False,
    )
    idempotency_key: Mapped[str] = mapped_column(String(120), nullable=False)
    expected_version: Mapped[int] = mapped_column(Integer, nullable=False)
    resulting_version: Mapped[int] = mapped_column(Integer, nullable=False)
    source: Mapped[str] = mapped_column(
        String(30),
        default="manual_input",
        server_default="manual_input",
        nullable=False,
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    mold_tool: Mapped["MoldTool"] = relationship(back_populates="location_movements")
    actor: Mapped["User | None"] = relationship(foreign_keys=[actor_id])


class MoldRepairEvent(Base):
    """Immutable ledger for independent mold repair-state transitions."""

    __tablename__ = "mold_repair_events"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_mold_repair_events_idempotency_key"),
        CheckConstraint(
            "before_status IN ('normal', 'needs_repair')",
            name="ck_mold_repair_events_before_status",
        ),
        CheckConstraint(
            "after_status IN ('normal', 'needs_repair')",
            name="ck_mold_repair_events_after_status",
        ),
        CheckConstraint(
            "before_status <> after_status",
            name="ck_mold_repair_events_actual_change",
        ),
        CheckConstraint(
            "expected_version >= 1",
            name="ck_mold_repair_events_expected_version",
        ),
        CheckConstraint(
            "resulting_version = expected_version + 1",
            name="ck_mold_repair_events_resulting_version",
        ),
        CheckConstraint(
            "length(request_hash) = 64",
            name="ck_mold_repair_events_request_hash",
        ),
        Index("ix_mold_repair_events_mold_time", "mold_tool_id", "occurred_at", "id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    mold_tool_id: Mapped[int] = mapped_column(
        ForeignKey("mold_tools.id", ondelete="RESTRICT"), nullable=False
    )
    mold_code_snapshot: Mapped[str] = mapped_column(String(100), nullable=False)
    before_status: Mapped[str] = mapped_column(String(20), nullable=False)
    after_status: Mapped[str] = mapped_column(String(20), nullable=False)
    actor_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    actor_username_snapshot: Mapped[str] = mapped_column(String(100), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(120), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expected_version: Mapped[int] = mapped_column(Integer, nullable=False)
    resulting_version: Mapped[int] = mapped_column(Integer, nullable=False)

    mold_tool: Mapped["MoldTool"] = relationship(back_populates="repair_events")
    actor: Mapped["User | None"] = relationship(foreign_keys=[actor_id])


class MoldScanEvent(Base):
    """Immutable proof that an operator scanned a mold for a specific task."""

    __tablename__ = "mold_scan_events"
    __table_args__ = (
        UniqueConstraint(
            "idempotency_key",
            name="uq_mold_scan_events_idempotency_key",
        ),
        CheckConstraint(
            "length(request_hash) = 64",
            name="ck_mold_scan_events_request_hash",
        ),
        CheckConstraint(
            "mold_location_version_snapshot >= 1",
            name="ck_mold_scan_events_location_version",
        ),
        CheckConstraint(
            "production_task_version_snapshot >= 1",
            name="ck_mold_scan_events_task_version",
        ),
        CheckConstraint(
            "source = 'fixed_qr'",
            name="ck_mold_scan_events_source",
        ),
        Index(
            "ix_mold_scan_events_mold_scanned_id",
            "mold_tool_id",
            "scanned_at",
            "id",
        ),
        Index(
            "ix_mold_scan_events_task_scanned_id",
            "production_task_id_snapshot",
            "scanned_at",
            "id",
        ),
        Index(
            "ix_mold_scan_events_order_scanned_id",
            "sales_order_id_snapshot",
            "scanned_at",
            "id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    mold_tool_id: Mapped[int] = mapped_column(
        ForeignKey("mold_tools.id", ondelete="RESTRICT"),
        nullable=False,
    )
    mold_code_snapshot: Mapped[str] = mapped_column(String(100), nullable=False)
    mold_location_snapshot: Mapped[str] = mapped_column(String(250), nullable=False)
    mold_location_version_snapshot: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )
    production_task_id: Mapped[int | None] = mapped_column(
        ForeignKey("production_tasks.id", ondelete="SET NULL"),
        nullable=True,
    )
    production_task_id_snapshot: Mapped[int] = mapped_column(Integer, nullable=False)
    production_task_version_snapshot: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )
    production_task_status_snapshot: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
    )
    linkage_basis_snapshot: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
    )
    sales_order_id: Mapped[int | None] = mapped_column(
        ForeignKey("sales_orders.id", ondelete="SET NULL"),
        nullable=True,
    )
    sales_order_id_snapshot: Mapped[int] = mapped_column(Integer, nullable=False)
    sales_order_number_snapshot: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )
    sales_order_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("sales_order_items.id", ondelete="SET NULL"),
        nullable=True,
    )
    sales_order_item_id_snapshot: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )
    customer_id_snapshot: Mapped[int] = mapped_column(Integer, nullable=False)
    customer_name_snapshot: Mapped[str] = mapped_column(String(200), nullable=False)
    product_code_snapshot: Mapped[str | None] = mapped_column(
        String(150),
        nullable=True,
    )
    product_name_snapshot: Mapped[str | None] = mapped_column(
        String(250),
        nullable=True,
    )
    scanned_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    scanned_by_name_snapshot: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )
    scanned_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    source: Mapped[str] = mapped_column(
        String(20),
        default="fixed_qr",
        server_default="fixed_qr",
        nullable=False,
    )
    idempotency_key: Mapped[str] = mapped_column(String(120), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    mold_tool: Mapped["MoldTool"] = relationship(back_populates="scan_events")
    scanner: Mapped["User | None"] = relationship(foreign_keys=[scanned_by])


class MoldLabelPrintJob(Base):
    """Immutable operator action recording one single or batch label print."""

    __tablename__ = "mold_label_print_jobs"
    __table_args__ = (
        UniqueConstraint(
            "idempotency_key",
            name="uq_mold_label_print_jobs_idempotency_key",
        ),
        CheckConstraint(
            "source IN ('single','batch')",
            name="ck_mold_label_print_jobs_source",
        ),
        CheckConstraint(
            "item_count >= 1 AND item_count <= 100",
            name="ck_mold_label_print_jobs_item_count",
        ),
        Index("ix_mold_label_print_jobs_printed_at", "printed_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    idempotency_key: Mapped[str] = mapped_column(String(120), nullable=False)
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    item_count: Mapped[int] = mapped_column(Integer, nullable=False)
    printed_by: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    printed_by_username: Mapped[str] = mapped_column(String(100), nullable=False)
    printed_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.current_timestamp(),
        nullable=False,
    )

    items: Mapped[list["MoldLabelPrintJobItem"]] = relationship(
        back_populates="job",
        passive_deletes=True,
        order_by="MoldLabelPrintJobItem.item_order",
    )
    printer: Mapped["User"] = relationship(foreign_keys=[printed_by])


class MoldLabelPrintJobItem(Base):
    """Immutable mold snapshot included in one label print job."""

    __tablename__ = "mold_label_print_job_items"
    __table_args__ = (
        UniqueConstraint(
            "print_job_id",
            "mold_tool_id",
            name="uq_mold_label_print_job_items_job_mold",
        ),
        UniqueConstraint(
            "print_job_id",
            "item_order",
            name="uq_mold_label_print_job_items_job_order",
        ),
        CheckConstraint(
            "item_order >= 1 AND item_order <= 100",
            name="ck_mold_label_print_job_items_order",
        ),
        Index(
            "ix_mold_label_print_job_items_mold_job",
            "mold_tool_id",
            "print_job_id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    print_job_id: Mapped[int] = mapped_column(
        ForeignKey("mold_label_print_jobs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    mold_tool_id: Mapped[int] = mapped_column(
        ForeignKey("mold_tools.id", ondelete="RESTRICT"),
        nullable=False,
    )
    item_order: Mapped[int] = mapped_column(Integer, nullable=False)
    mold_code_snapshot: Mapped[str] = mapped_column(String(100), nullable=False)
    rack_location_snapshot: Mapped[str] = mapped_column(String(250), nullable=False)

    job: Mapped["MoldLabelPrintJob"] = relationship(back_populates="items")
    mold_tool: Mapped["MoldTool"] = relationship(back_populates="label_print_items")
