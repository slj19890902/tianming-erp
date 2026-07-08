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
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models import Base


class XinzhenCartonMarkingOrder(Base):
    __tablename__ = "xinzhen_carton_marking_orders"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending_receipt', 'needs_review')",
            name="ck_xinzhen_orders_status",
        ),
        UniqueConstraint(
            "order_number",
            name="uq_xinzhen_orders_order_number",
        ),
        Index("ix_xinzhen_orders_customer_id", "customer_id"),
        Index("ix_xinzhen_orders_external_po_no", "external_po_no"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    order_number: Mapped[str] = mapped_column(String(50), nullable=False)
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id", ondelete="RESTRICT"),
        nullable=False,
    )
    external_po_no: Mapped[str | None] = mapped_column(String(150), nullable=True)
    source_file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    header_total_carton_qty: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )
    total_carton_qty: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    layout_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        default="needs_review",
    )
    resin_template_id: Mapped[int | None] = mapped_column(
        ForeignKey("xinzhen_resin_templates.id", ondelete="SET NULL"),
        nullable=True,
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

    import_batches: Mapped[list["XinzhenCartonMarkingImportBatch"]] = relationship(
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="XinzhenCartonMarkingImportBatch.id",
    )
    layouts: Mapped[list["XinzhenPrintLayout"]] = relationship(
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="XinzhenPrintLayout.sort_order",
    )
    changeover_steps: Mapped[list["XinzhenPrintChangeoverStep"]] = relationship(
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="XinzhenPrintChangeoverStep.step_no",
    )


class XinzhenCartonMarkingImportBatch(Base):
    __tablename__ = "xinzhen_carton_marking_import_batches"
    __table_args__ = (
        CheckConstraint(
            "import_status IN ('ready', 'needs_review')",
            name="ck_xinzhen_import_batches_status",
        ),
        UniqueConstraint(
            "batch_number",
            name="uq_xinzhen_import_batches_batch_number",
        ),
        Index("ix_xinzhen_import_batches_customer_id", "customer_id"),
        Index("ix_xinzhen_import_batches_order_id", "order_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    batch_number: Mapped[str] = mapped_column(String(50), nullable=False)
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id", ondelete="RESTRICT"),
        nullable=False,
    )
    order_id: Mapped[int] = mapped_column(
        ForeignKey("xinzhen_carton_marking_orders.id", ondelete="CASCADE"),
        nullable=False,
    )
    source_file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    source_file_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    parsed_layout_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    header_total_carton_qty: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )
    layout_total_carton_qty: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    mismatch_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    missing_common_box_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )
    missing_rubber_block_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )
    generated_changeover_step_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )
    import_status: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        default="needs_review",
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

    layouts: Mapped[list["XinzhenPrintLayout"]] = relationship(
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="XinzhenPrintLayout.sort_order",
    )


class XinzhenResinTemplate(Base):
    __tablename__ = "xinzhen_resin_templates"
    __table_args__ = (
        UniqueConstraint(
            "customer_id",
            "template_code",
            name="uq_xinzhen_resin_templates_customer_code",
        ),
        Index("ix_xinzhen_resin_templates_customer_id", "customer_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id", ondelete="RESTRICT"),
        nullable=False,
    )
    template_code: Mapped[str] = mapped_column(String(80), nullable=False)
    template_name: Mapped[str] = mapped_column(String(200), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.current_timestamp(),
        nullable=False,
    )

    slots: Mapped[list["XinzhenResinTemplateSlot"]] = relationship(
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="XinzhenResinTemplateSlot.display_order",
    )


class XinzhenResinTemplateSlot(Base):
    __tablename__ = "xinzhen_resin_template_slots"
    __table_args__ = (
        UniqueConstraint(
            "template_id",
            "slot_code",
            name="uq_xinzhen_resin_template_slots_template_slot_code",
        ),
        Index("ix_xinzhen_resin_template_slots_template_id", "template_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    template_id: Mapped[int] = mapped_column(
        ForeignKey("xinzhen_resin_templates.id", ondelete="CASCADE"),
        nullable=False,
    )
    slot_code: Mapped[str] = mapped_column(String(20), nullable=False)
    slot_name: Mapped[str] = mapped_column(String(120), nullable=False)
    field_name: Mapped[str] = mapped_column(String(80), nullable=False)
    block_type: Mapped[str] = mapped_column(String(40), nullable=False)
    display_order: Mapped[int] = mapped_column(Integer, nullable=False)
    is_variable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class XinzhenRubberTypeBlock(Base):
    __tablename__ = "xinzhen_rubber_type_blocks"
    __table_args__ = (
        Index(
            "ix_xinzhen_rubber_type_blocks_customer_type_text",
            "customer_id",
            "block_type",
            "block_text",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id", ondelete="RESTRICT"),
        nullable=False,
    )
    block_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    block_text: Mapped[str] = mapped_column(String(255), nullable=False)
    block_type: Mapped[str] = mapped_column(String(40), nullable=False)
    storage_location_text: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.current_timestamp(),
        nullable=False,
    )


class XinzhenCommonBoxRule(Base):
    __tablename__ = "xinzhen_common_box_rules"
    __table_args__ = (
        Index("ix_xinzhen_common_box_rules_customer_id", "customer_id"),
        Index(
            "ix_xinzhen_common_box_rules_customer_size_units",
            "customer_id",
            "product_size",
            "units_per_carton",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id", ondelete="RESTRICT"),
        nullable=False,
    )
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"),
        nullable=False,
    )
    dimension_hint_text: Mapped[str | None] = mapped_column(String(80), nullable=True)
    product_size: Mapped[str | None] = mapped_column(String(80), nullable=True)
    units_per_carton: Mapped[int | None] = mapped_column(Integer, nullable=True)
    vendor_style_no: Mapped[str | None] = mapped_column(String(80), nullable=True)
    color: Mapped[str | None] = mapped_column(String(80), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.current_timestamp(),
        nullable=False,
    )


class XinzhenPrintLayout(Base):
    __tablename__ = "xinzhen_print_layouts"
    __table_args__ = (
        CheckConstraint(
            "box_match_status IN ('matched', 'missing')",
            name="ck_xinzhen_print_layouts_box_match_status",
        ),
        CheckConstraint(
            "parse_status IN ('ok', 'needs_review')",
            name="ck_xinzhen_print_layouts_parse_status",
        ),
        CheckConstraint(
            "print_status IN ('pending', 'printing', 'completed')",
            name="ck_xinzhen_print_layouts_print_status",
        ),
        UniqueConstraint(
            "import_batch_id",
            "layout_code",
            name="uq_xinzhen_print_layouts_batch_layout_code",
        ),
        Index("ix_xinzhen_print_layouts_order_sort", "order_id", "sort_order"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    import_batch_id: Mapped[int] = mapped_column(
        ForeignKey("xinzhen_carton_marking_import_batches.id", ondelete="CASCADE"),
        nullable=False,
    )
    order_id: Mapped[int] = mapped_column(
        ForeignKey("xinzhen_carton_marking_orders.id", ondelete="CASCADE"),
        nullable=False,
    )
    layout_code: Mapped[str] = mapped_column(String(20), nullable=False)
    source_file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    source_sheet_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    source_block_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    external_po_no: Mapped[str | None] = mapped_column(String(150), nullable=True)
    customer_mark_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    vendor_style_no: Mapped[str | None] = mapped_column(String(120), nullable=True)
    color: Mapped[str | None] = mapped_column(String(120), nullable=True)
    product_size: Mapped[str | None] = mapped_column(String(120), nullable=True)
    units_per_carton: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_units: Mapped[int | None] = mapped_column(Integer, nullable=True)
    carton_qty: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    carton_no_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    carton_no_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    carton_no_range_text: Mapped[str | None] = mapped_column(String(255), nullable=True)
    carton_qty_formula_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    resin_template_id: Mapped[int] = mapped_column(
        ForeignKey("xinzhen_resin_templates.id", ondelete="RESTRICT"),
        nullable=False,
    )
    common_box_id: Mapped[int | None] = mapped_column(
        ForeignKey("products.id", ondelete="SET NULL"),
        nullable=True,
    )
    box_match_status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="missing",
    )
    box_match_strategy: Mapped[str | None] = mapped_column(String(40), nullable=True)
    common_box_dimension: Mapped[str | None] = mapped_column(String(80), nullable=True)
    print_content_snapshot: Mapped[str | None] = mapped_column(Text, nullable=True)
    layout_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    print_status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="pending",
    )
    parse_status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="ok",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.current_timestamp(),
        nullable=False,
    )

    slot_values: Mapped[list["XinzhenPrintLayoutSlotValue"]] = relationship(
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="XinzhenPrintLayoutSlotValue.display_order",
    )


class XinzhenPrintLayoutSlotValue(Base):
    __tablename__ = "xinzhen_print_layout_slot_values"
    __table_args__ = (
        CheckConstraint(
            "match_status IN ('matched', 'missing')",
            name="ck_xinzhen_print_layout_slot_values_match_status",
        ),
        UniqueConstraint(
            "print_layout_id",
            "slot_code",
            name="uq_xinzhen_print_layout_slot_values_layout_slot_code",
        ),
        Index(
            "ix_xinzhen_print_layout_slot_values_layout_id",
            "print_layout_id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    print_layout_id: Mapped[int] = mapped_column(
        ForeignKey("xinzhen_print_layouts.id", ondelete="CASCADE"),
        nullable=False,
    )
    slot_code: Mapped[str] = mapped_column(String(20), nullable=False)
    slot_name: Mapped[str] = mapped_column(String(120), nullable=False)
    field_name: Mapped[str] = mapped_column(String(80), nullable=False)
    block_type: Mapped[str] = mapped_column(String(40), nullable=False)
    display_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    slot_value: Mapped[str | None] = mapped_column(String(255), nullable=True)
    rubber_block_id: Mapped[int | None] = mapped_column(
        ForeignKey("xinzhen_rubber_type_blocks.id", ondelete="SET NULL"),
        nullable=True,
    )
    rubber_block_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    rubber_block_storage_location: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )
    match_status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="missing",
    )


class XinzhenPrintChangeoverStep(Base):
    __tablename__ = "xinzhen_print_changeover_steps"
    __table_args__ = (
        CheckConstraint(
            "status IN ('ready', 'needs_review', 'no_change')",
            name="ck_xinzhen_print_changeover_steps_status",
        ),
        UniqueConstraint(
            "order_id",
            "step_no",
            name="uq_xinzhen_print_changeover_steps_order_step_no",
        ),
        Index("ix_xinzhen_print_changeover_steps_order_id", "order_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(
        ForeignKey("xinzhen_carton_marking_orders.id", ondelete="CASCADE"),
        nullable=False,
    )
    from_layout_id: Mapped[int] = mapped_column(
        ForeignKey("xinzhen_print_layouts.id", ondelete="CASCADE"),
        nullable=False,
    )
    to_layout_id: Mapped[int] = mapped_column(
        ForeignKey("xinzhen_print_layouts.id", ondelete="CASCADE"),
        nullable=False,
    )
    step_no: Mapped[int] = mapped_column(Integer, nullable=False)
    changed_slot_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="no_change",
    )

    items: Mapped[list["XinzhenPrintChangeoverStepItem"]] = relationship(
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="XinzhenPrintChangeoverStepItem.id",
    )


class XinzhenPrintChangeoverStepItem(Base):
    __tablename__ = "xinzhen_print_changeover_step_items"
    __table_args__ = (
        CheckConstraint(
            "confirm_status IN ('needs_change', 'missing')",
            name="ck_xinzhen_print_changeover_step_items_confirm_status",
        ),
        Index(
            "ix_xinzhen_print_changeover_step_items_step_id",
            "changeover_step_id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    changeover_step_id: Mapped[int] = mapped_column(
        ForeignKey("xinzhen_print_changeover_steps.id", ondelete="CASCADE"),
        nullable=False,
    )
    slot_code: Mapped[str] = mapped_column(String(20), nullable=False)
    slot_name: Mapped[str] = mapped_column(String(120), nullable=False)
    old_value: Mapped[str | None] = mapped_column(String(255), nullable=True)
    new_value: Mapped[str | None] = mapped_column(String(255), nullable=True)
    rubber_block_id: Mapped[int | None] = mapped_column(
        ForeignKey("xinzhen_rubber_type_blocks.id", ondelete="SET NULL"),
        nullable=True,
    )
    rubber_block_storage_location: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )
    confirm_status: Mapped[str] = mapped_column(String(20), nullable=False)
