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
    ordered_quantity_snapshot: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    material_received_quantity: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    material_input_quantity: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    output_factor: Mapped[int] = mapped_column(
        Integer, default=1, server_default="1", nullable=False
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
            "uq_production_completions_task_primary_active",
            "task_id",
            unique=True,
            sqlite_where=text("status = 'posted' AND completion_type = 'primary'"),
            postgresql_where=text("status = 'posted' AND completion_type = 'primary'"),
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
            "initial_disposition IN ('direct','stock','split')",
            name="ck_production_completions_initial_disposition",
        ),
        CheckConstraint(
            "((initial_disposition = 'direct' AND warehouse_location_id IS NULL "
            "AND inventory_lot_id IS NULL AND stock_quantity = 0) "
            "OR (initial_disposition = 'stock' AND warehouse_location_id IS NOT NULL "
            "AND direct_delivery_quantity = 0) "
            "OR (initial_disposition = 'split' AND warehouse_location_id IS NOT NULL "
            "AND direct_delivery_quantity > 0 AND stock_quantity > 0))",
            name="ck_production_completions_disposition_targets",
        ),
        CheckConstraint(
            "completion_type IN ('primary','supplemental')",
            name="ck_production_completions_type",
        ),
        CheckConstraint(
            "material_input_quantity > 0 AND planned_output_quantity > 0 "
            "AND actual_output_quantity > 0 AND defective_quantity >= 0",
            name="ck_production_completions_output_quantities",
        ),
        CheckConstraint(
            "quantity = actual_output_quantity "
            "AND direct_delivery_quantity + stock_quantity = actual_output_quantity "
            "AND order_reserved_quantity >= 0 "
            "AND order_reserved_quantity <= actual_output_quantity "
            "AND surplus_finished_quantity = actual_output_quantity - order_reserved_quantity",
            name="ck_production_completions_quantity_conservation",
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
    completion_type: Mapped[str] = mapped_column(
        String(20), default="primary", server_default="primary", nullable=False
    )
    material_input_quantity: Mapped[int] = mapped_column(
        Integer,
        default=lambda context: int(context.get_current_parameters().get("quantity") or 0),
        nullable=False,
    )
    planned_output_quantity: Mapped[int] = mapped_column(
        Integer,
        default=lambda context: int(context.get_current_parameters().get("quantity") or 0),
        nullable=False,
    )
    actual_output_quantity: Mapped[int] = mapped_column(
        Integer,
        default=lambda context: int(context.get_current_parameters().get("quantity") or 0),
        nullable=False,
    )
    defective_quantity: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    order_reserved_quantity: Mapped[int] = mapped_column(
        Integer,
        default=lambda context: int(context.get_current_parameters().get("quantity") or 0),
        server_default="0",
        nullable=False,
    )
    direct_delivery_quantity: Mapped[int] = mapped_column(
        Integer,
        default=lambda context: (
            int(context.get_current_parameters().get("quantity") or 0)
            if context.get_current_parameters().get("initial_disposition") == "direct"
            else 0
        ),
        server_default="0",
        nullable=False,
    )
    stock_quantity: Mapped[int] = mapped_column(
        Integer,
        default=lambda context: (
            int(context.get_current_parameters().get("quantity") or 0)
            if context.get_current_parameters().get("initial_disposition") == "stock"
            else 0
        ),
        server_default="0",
        nullable=False,
    )
    surplus_finished_quantity: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
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


class ProductionCompletionMaterialUsage(Base):
    """Immutable per-reservation material facts captured at production completion."""

    __tablename__ = "production_completion_material_usages"
    __table_args__ = (
        UniqueConstraint(
            "completion_id",
            "reservation_id",
            name="uq_production_completion_material_usages_completion_reservation",
        ),
        UniqueConstraint(
            "consume_movement_id",
            name="uq_production_completion_material_usages_consume_movement",
        ),
        UniqueConstraint(
            "release_movement_id",
            name="uq_production_completion_material_usages_release_movement",
        ),
        Index(
            "ix_production_completion_material_usages_task",
            "task_id",
            "status",
        ),
        Index(
            "ix_production_completion_material_usages_reservation",
            "reservation_id",
            "status",
        ),
        Index(
            "ix_production_completion_material_usages_lot",
            "inventory_lot_id",
            "status",
        ),
        CheckConstraint(
            "assigned_stock_quantity > 0 "
            "AND actual_consumed_stock_quantity >= 0 "
            "AND returned_intact_stock_quantity >= 0 "
            "AND damaged_stock_quantity >= 0 "
            "AND offcut_stock_quantity >= 0 "
            "AND remaining_reserved_stock_quantity >= 0",
            name="ck_production_completion_material_usages_nonnegative",
        ),
        CheckConstraint(
            "assigned_stock_quantity = actual_consumed_stock_quantity "
            "+ returned_intact_stock_quantity + damaged_stock_quantity "
            "+ offcut_stock_quantity + remaining_reserved_stock_quantity",
            name="ck_production_completion_material_usages_conservation",
        ),
        CheckConstraint(
            "remaining_reserved_stock_quantity = 0",
            name="ck_production_completion_material_usages_no_remaining",
        ),
        CheckConstraint(
            "expected_lot_version >= 1 "
            "AND result_lot_version > expected_lot_version "
            "AND credited_requirement_quantity >= 0 "
            "AND credited_requirement_quantity <= assigned_stock_quantity * yield_factor "
            "AND actual_credited_requirement_quantity >= 0 "
            "AND actual_credited_requirement_quantity <= credited_requirement_quantity "
            "AND actual_credited_requirement_quantity "
            "<= actual_consumed_stock_quantity * yield_factor "
            "AND yield_factor >= 1",
            name="ck_production_completion_material_usages_credits",
        ),
        CheckConstraint(
            "actual_consumed_stock_quantity = assigned_stock_quantity "
            "OR variance_reason_code IN ('intact_return','damaged','offcut','mixed')",
            name="ck_production_completion_material_usages_variance_reason",
        ),
        CheckConstraint(
            "((actual_consumed_stock_quantity + damaged_stock_quantity "
            "+ offcut_stock_quantity = 0 AND consume_movement_id IS NULL) "
            "OR (actual_consumed_stock_quantity + damaged_stock_quantity "
            "+ offcut_stock_quantity > 0 AND consume_movement_id IS NOT NULL)) "
            "AND ((returned_intact_stock_quantity = 0 AND release_movement_id IS NULL) "
            "OR (returned_intact_stock_quantity > 0 AND release_movement_id IS NOT NULL))",
            name="ck_production_completion_material_usages_movements",
        ),
        CheckConstraint(
            "status IN ('posted','reversed')",
            name="ck_production_completion_material_usages_status",
        ),
        CheckConstraint(
            "return_status IN ('not_applicable','released','reversed')",
            name="ck_production_completion_material_usages_return_status",
        ),
        CheckConstraint(
            "(returned_intact_stock_quantity = 0 "
            "AND return_confirmed IS FALSE "
            "AND return_status = 'not_applicable' "
            "AND return_confirmed_by IS NULL "
            "AND return_confirmed_at IS NULL) "
            "OR (returned_intact_stock_quantity > 0 "
            "AND return_confirmed IS TRUE "
            "AND return_status IN ('released','reversed') "
            "AND return_confirmed_by IS NOT NULL "
            "AND return_confirmed_at IS NOT NULL)",
            name="ck_production_completion_material_usages_return_confirmation",
        ),
        CheckConstraint(
            "(status = 'posted' AND reversed_at IS NULL "
            "AND reversed_by IS NULL AND reversal_reason IS NULL) "
            "OR (status = 'reversed' AND reversed_at IS NOT NULL "
            "AND reversal_reason IS NOT NULL)",
            name="ck_production_completion_material_usages_reversal",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    completion_id: Mapped[int] = mapped_column(
        ForeignKey("production_completions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    task_id: Mapped[int] = mapped_column(
        ForeignKey("production_tasks.id", ondelete="RESTRICT"),
        nullable=False,
    )
    order_item_id: Mapped[int] = mapped_column(
        ForeignKey("sales_order_items.id", ondelete="RESTRICT"),
        nullable=False,
    )
    reservation_id: Mapped[int] = mapped_column(
        ForeignKey("inventory_reservations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    inventory_lot_id: Mapped[int] = mapped_column(
        ForeignKey("inventory_lots.id", ondelete="RESTRICT"),
        nullable=False,
    )
    expected_lot_version: Mapped[int] = mapped_column(Integer, nullable=False)
    result_lot_version: Mapped[int] = mapped_column(Integer, nullable=False)
    assigned_stock_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    actual_consumed_stock_quantity: Mapped[int] = mapped_column(
        Integer, nullable=False
    )
    returned_intact_stock_quantity: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    damaged_stock_quantity: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    offcut_stock_quantity: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    remaining_reserved_stock_quantity: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    credited_requirement_quantity: Mapped[int] = mapped_column(
        Integer, nullable=False
    )
    actual_credited_requirement_quantity: Mapped[int] = mapped_column(
        Integer, nullable=False
    )
    yield_factor: Mapped[int] = mapped_column(Integer, nullable=False)
    variance_reason_code: Mapped[str | None] = mapped_column(
        String(50), nullable=True
    )
    variance_reason_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    return_confirmed: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0", nullable=False
    )
    return_confirmed_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    return_confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    return_status: Mapped[str] = mapped_column(
        String(30), default="not_applicable", server_default="not_applicable", nullable=False
    )
    consume_movement_id: Mapped[int | None] = mapped_column(
        ForeignKey("inventory_movements.id", ondelete="RESTRICT"),
        nullable=True,
    )
    release_movement_id: Mapped[int | None] = mapped_column(
        ForeignKey("inventory_movements.id", ondelete="RESTRICT"),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(
        String(20), default="posted", server_default="posted", nullable=False
    )
    operator_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )
    reversed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    reversed_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    reversal_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


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
