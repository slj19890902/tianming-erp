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
    from app.models.customer import Customer
    from app.models.product import Product
    from app.models.user import User


class MoldTool(Base):
    """Physical die/mold master and its fixed warehouse rack location."""

    __tablename__ = "mold_tools"
    __table_args__ = (
        UniqueConstraint("mold_code", name="uq_mold_tools_code"),
        Index("uq_mold_tools_delete_key", "delete_idempotency_key", unique=True),
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
            "version >= 1",
            name="ck_mold_tools_version",
        ),
        CheckConstraint(
            "identity_status IN ('legacy_unset', 'frozen')",
            name="ck_mold_tools_identity_status",
        ),
        CheckConstraint(
            "((identity_status = 'legacy_unset' AND label_name IS NULL) OR "
            "(identity_status = 'frozen' AND label_name IS NOT NULL "
            "AND length(trim(label_name)) > 0))",
            name="ck_mold_tools_identity_fields",
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
    label_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    chinese_short_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    label_overrides_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    identity_status: Mapped[str] = mapped_column(
        String(20), default="legacy_unset", server_default="legacy_unset", nullable=False
    )
    version: Mapped[int] = mapped_column(
        Integer, default=1, server_default="1", nullable=False
    )
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
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    deleted_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    delete_idempotency_key: Mapped[str | None] = mapped_column(String(120), nullable=True)
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
    customer_links: Mapped[list["MoldToolCustomer"]] = relationship(
        back_populates="mold_tool",
        passive_deletes=True,
        order_by="MoldToolCustomer.id",
    )
    master_mutations: Mapped[list["MoldMasterMutation"]] = relationship(
        back_populates="mold_tool",
        passive_deletes=True,
        order_by="MoldMasterMutation.id",
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


class MoldToolCustomer(Base):
    """A formal customer association for one physical mold body."""

    __tablename__ = "mold_tool_customers"
    __table_args__ = (
        UniqueConstraint(
            "mold_tool_id",
            "customer_id",
            name="uq_mold_tool_customers_mold_customer",
        ),
        UniqueConstraint(
            "mold_tool_id",
            "display_order",
            name="uq_mold_tool_customers_mold_display_order",
        ),
        CheckConstraint(
            "display_order IS NULL OR display_order IN (1, 2)",
            name="ck_mold_tool_customers_display_order",
        ),
        Index("ix_mold_tool_customers_customer_mold", "customer_id", "mold_tool_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    mold_tool_id: Mapped[int] = mapped_column(
        ForeignKey("mold_tools.id", ondelete="RESTRICT"), nullable=False
    )
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False
    )
    display_order: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )

    mold_tool: Mapped["MoldTool"] = relationship(back_populates="customer_links")
    customer: Mapped["Customer"] = relationship()
    creator: Mapped["User | None"] = relationship(foreign_keys=[created_by])


class MoldMasterMutation(Base):
    """Immutable idempotency and audit fact for mold identity edits."""

    __tablename__ = "mold_master_mutations"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_mold_master_mutations_key"),
        CheckConstraint(
            "action IN ('create', 'update')",
            name="ck_mold_master_mutations_action",
        ),
        CheckConstraint(
            "length(request_hash) = 64",
            name="ck_mold_master_mutations_request_hash",
        ),
        CheckConstraint(
            "result_version >= 1",
            name="ck_mold_master_mutations_result_version",
        ),
        Index("ix_mold_master_mutations_mold_time", "mold_tool_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    mold_tool_id: Mapped[int] = mapped_column(
        ForeignKey("mold_tools.id", ondelete="RESTRICT"), nullable=False
    )
    action: Mapped[str] = mapped_column(String(20), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(120), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    result_version: Mapped[int] = mapped_column(Integer, nullable=False)
    result_snapshot_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )

    mold_tool: Mapped["MoldTool"] = relationship(back_populates="master_mutations")
    actor: Mapped["User"] = relationship(foreign_keys=[actor_id])


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


class MoldLabelLayoutRevision(Base):
    """Append-only released layout for the physical 40x80 mold label."""

    __tablename__ = "mold_label_layout_revisions"
    __table_args__ = (
        UniqueConstraint("version", name="uq_mold_label_layout_revisions_version"),
        UniqueConstraint(
            "operation_key", name="uq_mold_label_layout_revisions_operation_key"
        ),
        CheckConstraint(
            "version >= 1", name="ck_mold_label_layout_revisions_version"
        ),
        CheckConstraint(
            "operation_kind IN ('save_and_publish','restore_default','rollback')",
            name="ck_mold_label_layout_revisions_operation_kind",
        ),
        CheckConstraint(
            "length(payload_hash) = 64 AND length(request_hash) = 64",
            name="ck_mold_label_layout_revisions_hashes",
        ),
        CheckConstraint(
            "source_release_version IS NULL OR source_release_version >= 1",
            name="ck_mold_label_layout_revisions_source_version",
        ),
        CheckConstraint(
            "((operation_kind = 'rollback' AND source_release_version IS NOT NULL) "
            "OR (operation_kind <> 'rollback' AND source_release_version IS NULL))",
            name="ck_mold_label_layout_revisions_source_kind",
        ),
        Index("ix_mold_label_layout_revisions_version", "version"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    catalog_version: Mapped[str] = mapped_column(String(40), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    operation_kind: Mapped[str] = mapped_column(String(30), nullable=False)
    operation_key: Mapped[str] = mapped_column(String(120), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_release_version: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )


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
        CheckConstraint(
            "template_version IN ('mold_40x30_v1','mold_80x40_v1')",
            name="ck_mold_label_print_jobs_template_version",
        ),
        Index("ix_mold_label_print_jobs_printed_at", "printed_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    idempotency_key: Mapped[str] = mapped_column(String(120), nullable=False)
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    item_count: Mapped[int] = mapped_column(Integer, nullable=False)
    template_version: Mapped[str] = mapped_column(
        String(30),
        default="mold_40x30_v1",
        server_default="mold_40x30_v1",
        nullable=False,
    )
    label_layout_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    label_layout_payload_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    label_layout_payload_hash: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
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
