from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base


class ProductionTask(Base):
    __tablename__ = "production_tasks"
    __table_args__ = (
        Index(
            "uq_production_tasks_regular_order_item",
            "order_item_id",
            unique=True,
            sqlite_where=text("sales_order_item_bom_component_id IS NULL"),
            postgresql_where=text("sales_order_item_bom_component_id IS NULL"),
        ),
        Index(
            "uq_production_tasks_bom_component",
            "sales_order_item_bom_component_id",
            unique=True,
            sqlite_where=text("sales_order_item_bom_component_id IS NOT NULL"),
            postgresql_where=text("sales_order_item_bom_component_id IS NOT NULL"),
        ),
        CheckConstraint(
            "status IN ('waiting_material','pending','completed','not_required')",
            name="ck_production_tasks_status",
        ),
        CheckConstraint(
            "planned_quantity >= 0",
            name="ck_production_tasks_planned_quantity_nonnegative",
        ),
        CheckConstraint(
            "finished_coverage_snapshot >= 0",
            name="ck_production_tasks_finished_coverage_nonnegative",
        ),
        CheckConstraint("version >= 1", name="ck_production_tasks_version"),
        CheckConstraint(
            "((status IN ('waiting_material','not_required') AND planned_quantity = 0) "
            "OR (status IN ('pending','completed') AND planned_quantity > 0))",
            name="ck_production_tasks_status_quantity",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    order_item_id: Mapped[int] = mapped_column(
        ForeignKey("sales_order_items.id", ondelete="CASCADE"), nullable=False
    )
    sales_order_item_bom_component_id: Mapped[int | None] = mapped_column(
        ForeignKey("sales_order_item_bom_components.id", ondelete="SET NULL"),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(
        String(30), default="waiting_material", server_default="waiting_material", nullable=False
    )
    planned_quantity: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    finished_coverage_snapshot: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    readiness_basis: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ready_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    version: Mapped[int] = mapped_column(
        Integer, default=1, server_default="1", nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime, onupdate=func.current_timestamp(), nullable=True
    )


class ProductionCompletionBatch(Base):
    __tablename__ = "production_completion_batches"
    __table_args__ = (
        UniqueConstraint(
            "idempotency_key", name="uq_production_completion_batches_idempotency"
        ),
        CheckConstraint(
            "length(request_hash) = 64",
            name="ck_production_completion_batches_request_hash",
        ),
        CheckConstraint(
            "item_count > 0", name="ck_production_completion_batches_item_count"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    idempotency_key: Mapped[str] = mapped_column(String(120), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    item_count: Mapped[int] = mapped_column(Integer, nullable=False)
    completed_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    completed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )


class ProductionCompletion(Base):
    __tablename__ = "production_completions"
    __table_args__ = (
        Index(
            "uq_production_completions_task_active",
            "task_id",
            unique=True,
            sqlite_where=text("status = 'posted'"),
            postgresql_where=text("status = 'posted'"),
        ),
        UniqueConstraint(
            "inventory_lot_id", name="uq_production_completions_inventory_lot"
        ),
        CheckConstraint(
            "expected_version >= 1",
            name="ck_production_completions_expected_version",
        ),
        CheckConstraint(
            "quantity > 0", name="ck_production_completions_quantity"
        ),
        CheckConstraint(
            "status IN ('posted','reversed')",
            name="ck_production_completions_status",
        ),
        CheckConstraint(
            "initial_disposition IN ('direct','stock')",
            name="ck_production_completions_initial_disposition",
        ),
        CheckConstraint(
            "((initial_disposition = 'direct' AND warehouse_location_id IS NULL "
            "AND inventory_lot_id IS NULL) OR (initial_disposition = 'stock' "
            "AND warehouse_location_id IS NOT NULL))",
            name="ck_production_completions_disposition_targets",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    batch_id: Mapped[int] = mapped_column(
        ForeignKey("production_completion_batches.id", ondelete="RESTRICT"),
        nullable=False,
    )
    task_id: Mapped[int] = mapped_column(
        ForeignKey("production_tasks.id", ondelete="RESTRICT"), nullable=False
    )
    order_item_id: Mapped[int] = mapped_column(
        ForeignKey("sales_order_items.id", ondelete="RESTRICT"), nullable=False
    )
    expected_version: Mapped[int] = mapped_column(Integer, nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    initial_disposition: Mapped[str] = mapped_column(String(20), nullable=False)
    warehouse_location_id: Mapped[int | None] = mapped_column(
        ForeignKey("warehouse_locations.id", ondelete="RESTRICT"), nullable=True
    )
    inventory_lot_id: Mapped[int | None] = mapped_column(
        ForeignKey("inventory_lots.id", ondelete="RESTRICT"), nullable=True
    )
    remarks: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), default="posted", server_default="posted", nullable=False
    )
    completed_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    completed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    reversed_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    reversed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    reversal_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )


class ProductionStockTransfer(Base):
    __tablename__ = "production_stock_transfers"
    __table_args__ = (
        UniqueConstraint(
            "completion_id", name="uq_production_stock_transfers_completion"
        ),
        UniqueConstraint(
            "inventory_lot_id", name="uq_production_stock_transfers_inventory_lot"
        ),
        UniqueConstraint(
            "idempotency_key", name="uq_production_stock_transfers_idempotency"
        ),
        CheckConstraint(
            "length(request_hash) = 64",
            name="ck_production_stock_transfers_request_hash",
        ),
        CheckConstraint(
            "status IN ('posted','reversed')",
            name="ck_production_stock_transfers_status",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    completion_id: Mapped[int] = mapped_column(
        ForeignKey("production_completions.id", ondelete="RESTRICT"), nullable=False
    )
    warehouse_location_id: Mapped[int] = mapped_column(
        ForeignKey("warehouse_locations.id", ondelete="RESTRICT"), nullable=False
    )
    inventory_lot_id: Mapped[int] = mapped_column(
        ForeignKey("inventory_lots.id", ondelete="RESTRICT"), nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(120), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), default="posted", server_default="posted", nullable=False
    )
    transferred_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    transferred_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    reversed_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    reversed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    reversal_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )
