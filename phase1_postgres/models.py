from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
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
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def bigint_pk():
    return mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class LegacyMixin:
    legacy_source: Mapped[str | None] = mapped_column(String(50), nullable=True)
    legacy_id: Mapped[str | None] = mapped_column(String(100), nullable=True)


class User(TimestampMixin, Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(
            "role IN ('admin', 'clerk', 'sales', 'workshop', 'delivery', 'finance', 'readonly')",
            name="ck_users_role",
        ),
    )

    id: Mapped[int] = bigint_pk()
    username: Mapped[str] = mapped_column(String(80), nullable=False, unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    real_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    role: Mapped[str] = mapped_column(String(30), nullable=False, default="readonly")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    must_change_password: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class Customer(TimestampMixin, LegacyMixin, Base):
    __tablename__ = "customers"
    __table_args__ = (
        UniqueConstraint("legacy_source", "legacy_id", name="uq_customers_legacy"),
        CheckConstraint("delivery_method IN ('自提', '配送', '物流')", name="ck_customers_delivery_method"),
    )

    id: Mapped[int] = bigint_pk()
    customer_number: Mapped[int | None] = mapped_column(Integer, nullable=True, unique=True)
    customer_code: Mapped[str] = mapped_column(String(80), nullable=False, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    short_name: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    contact_person: Mapped[str | None] = mapped_column(String(100), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(100), nullable=True)
    mobile: Mapped[str | None] = mapped_column(String(100), nullable=True)
    address: Mapped[str | None] = mapped_column(String(500), nullable=True)
    payment_term_days: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    delivery_method: Mapped[str] = mapped_column(String(20), nullable=False, default="配送")
    default_tax_rate: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False, default=Decimal("0.13"))
    invoice_title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    tax_number: Mapped[str | None] = mapped_column(String(80), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    products: Mapped[list[Product]] = relationship(back_populates="customer", passive_deletes=True)
    orders: Mapped[list[SalesOrder]] = relationship(back_populates="customer", passive_deletes=True)


class Supplier(TimestampMixin, LegacyMixin, Base):
    __tablename__ = "suppliers"
    __table_args__ = (UniqueConstraint("legacy_source", "legacy_id", name="uq_suppliers_legacy"),)

    id: Mapped[int] = bigint_pk()
    supplier_code: Mapped[str] = mapped_column(String(80), nullable=False, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    contact_person: Mapped[str | None] = mapped_column(String(100), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(100), nullable=True)
    address: Mapped[str | None] = mapped_column(String(500), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class FluteType(TimestampMixin, LegacyMixin, Base):
    __tablename__ = "flute_types"
    __table_args__ = (UniqueConstraint("legacy_source", "legacy_id", name="uq_flute_types_legacy"),)

    id: Mapped[int] = bigint_pk()
    code: Mapped[str] = mapped_column(String(50), nullable=False, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    add_width_mm: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False, default=Decimal("0"))
    basis_weight_gsm: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    freight_rate: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
    loss_rate: Mapped[Decimal] = mapped_column(Numeric(6, 4), nullable=False, default=Decimal("0"))
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    materials: Mapped[list[Material]] = relationship(back_populates="flute_type", passive_deletes=True)


class Material(TimestampMixin, LegacyMixin, Base):
    __tablename__ = "materials"
    __table_args__ = (UniqueConstraint("legacy_source", "legacy_id", name="uq_materials_legacy"),)

    id: Mapped[int] = bigint_pk()
    code: Mapped[str] = mapped_column(String(100), nullable=False, unique=True, index=True)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    paper_composition: Mapped[str | None] = mapped_column(String(500), nullable=True)
    basis_weight_description: Mapped[str | None] = mapped_column(String(255), nullable=True)
    layer_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    flute_type_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("flute_types.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    default_supplier_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("suppliers.id", ondelete="SET NULL"), nullable=True, index=True
    )
    customer_square_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
    supplier_square_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    flute_type: Mapped[FluteType | None] = relationship(back_populates="materials")
    default_supplier: Mapped[Supplier | None] = relationship()


class BoxTypeRule(TimestampMixin, LegacyMixin, Base):
    __tablename__ = "box_type_rules"
    __table_args__ = (
        UniqueConstraint("legacy_source", "legacy_id", name="uq_box_type_rules_legacy"),
        UniqueConstraint("code", name="uq_box_type_rules_code"),
    )

    id: Mapped[int] = bigint_pk()
    code: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    unit: Mapped[str] = mapped_column(String(20), nullable=False, default="mm")
    is_die_cut: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    glue_flap_mm: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False, default=Decimal("0"))
    length_formula_mm: Mapped[str] = mapped_column(String(500), nullable=False)
    width_formula_mm: Mapped[str] = mapped_column(String(500), nullable=False)
    single_flap_length_formula_mm: Mapped[str | None] = mapped_column(String(500), nullable=True)
    double_flap_length_formula_mm: Mapped[str | None] = mapped_column(String(500), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    score_line_rules: Mapped[list[ScoreLineRule]] = relationship(back_populates="box_type_rule", passive_deletes=True)
    products: Mapped[list[Product]] = relationship(back_populates="box_type_rule", passive_deletes=True)


class ScoreLineRule(TimestampMixin, LegacyMixin, Base):
    __tablename__ = "score_line_rules"
    __table_args__ = (
        UniqueConstraint("box_type_rule_id", "flute_type_id", name="uq_score_line_box_flute"),
        UniqueConstraint("legacy_source", "legacy_id", name="uq_score_line_rules_legacy"),
    )

    id: Mapped[int] = bigint_pk()
    box_type_rule_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("box_type_rules.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    flute_type_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("flute_types.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    inner_formula: Mapped[str] = mapped_column(String(500), nullable=False)
    outer_formula: Mapped[str] = mapped_column(String(500), nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    box_type_rule: Mapped[BoxTypeRule] = relationship(back_populates="score_line_rules")
    flute_type: Mapped[FluteType] = relationship()


class CuttingWidthRule(TimestampMixin, LegacyMixin, Base):
    __tablename__ = "cutting_width_rules"
    __table_args__ = (
        CheckConstraint("start_width_mm <= end_width_mm", name="ck_cutting_width_range"),
        UniqueConstraint("legacy_source", "legacy_id", name="uq_cutting_width_rules_legacy"),
    )

    id: Mapped[int] = bigint_pk()
    use_width_mm: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    start_width_mm: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    end_width_mm: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    unit: Mapped[str] = mapped_column(String(20), nullable=False, default="mm")
    inch_value: Mapped[Decimal | None] = mapped_column(Numeric(10, 3), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class Product(TimestampMixin, LegacyMixin, Base):
    __tablename__ = "products"
    __table_args__ = (
        UniqueConstraint("customer_id", "product_code", name="uq_products_customer_product_code"),
        UniqueConstraint("customer_id", "customer_material_code", name="uq_products_customer_material_code"),
        UniqueConstraint("legacy_source", "legacy_id", name="uq_products_legacy"),
        CheckConstraint("box_category IN ('normal', 'die_cut')", name="ck_products_box_category"),
    )

    id: Mapped[int] = bigint_pk()
    customer_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    product_code: Mapped[str] = mapped_column(String(120), nullable=False)
    customer_material_code: Mapped[str] = mapped_column(String(120), nullable=False)
    product_name: Mapped[str] = mapped_column(String(255), nullable=False)
    material_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("materials.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    default_material_text: Mapped[str | None] = mapped_column(String(255), nullable=True)
    flute_type_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("flute_types.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    box_type_rule_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("box_type_rules.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    length_mm: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    width_mm: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    height_mm: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    box_category: Mapped[str] = mapped_column(String(30), nullable=False, default="normal")
    box_style: Mapped[str | None] = mapped_column(String(120), nullable=True)
    print_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    production_process: Mapped[str | None] = mapped_column(String(255), nullable=True)
    default_score_line: Mapped[str | None] = mapped_column(String(255), nullable=True)
    default_cardboard_length_mm: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    default_cardboard_width_mm: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    default_pieces_per_sheet: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    default_unit_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
    historical_search_key: Mapped[str | None] = mapped_column(String(500), nullable=True, index=True)
    historical_style_no: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    historical_material_code: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    customer: Mapped[Customer] = relationship(back_populates="products")
    material: Mapped[Material | None] = relationship()
    flute_type: Mapped[FluteType | None] = relationship()
    box_type_rule: Mapped[BoxTypeRule | None] = relationship(back_populates="products")
    order_items: Mapped[list[OrderItem]] = relationship(back_populates="product", passive_deletes=True)


class SalesOrder(TimestampMixin, LegacyMixin, Base):
    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint("order_number", name="uq_orders_order_number"),
        UniqueConstraint("legacy_source", "legacy_id", name="uq_orders_legacy"),
        CheckConstraint(
            "status IN ('draft', 'ordered', 'requisition_pending', 'requisitioned', 'material_received', "
            "'production', 'ready_to_deliver', 'partially_delivered', 'delivered', 'receipt_pending', "
            "'receipt_confirmed', 'statemented', 'invoiced', 'paid', 'completed', 'cancelled')",
            name="ck_orders_status",
        ),
        CheckConstraint("payment_status IN ('unsettled', 'partial', 'settled')", name="ck_orders_payment_status"),
    )

    id: Mapped[int] = bigint_pk()
    order_number: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    customer_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    customer_po: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    order_date: Mapped[date] = mapped_column(Date, nullable=False)
    delivery_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="ordered", index=True)
    payment_status: Mapped[str] = mapped_column(String(30), nullable=False, default="unsettled")
    total_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=Decimal("0"))
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    customer: Mapped[Customer] = relationship(back_populates="orders")
    items: Mapped[list[OrderItem]] = relationship(
        back_populates="order",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class OrderItem(TimestampMixin, LegacyMixin, Base):
    __tablename__ = "order_items"
    __table_args__ = (
        UniqueConstraint("legacy_source", "legacy_id", name="uq_order_items_legacy"),
        CheckConstraint("material_status IN ('pending', 'received')", name="ck_order_items_material_status"),
        CheckConstraint("requisition_status IN ('未报料', '已报料', '已入库')", name="ck_order_items_requisition_status"),
        Index("ix_order_items_status_dates", "material_status", "requisition_status"),
    )

    id: Mapped[int] = bigint_pk()
    order_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("orders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    product_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("products.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    subtotal: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)

    snapshot_product_code: Mapped[str | None] = mapped_column(String(120), nullable=True)
    snapshot_product_name: Mapped[str] = mapped_column(String(255), nullable=False)
    snapshot_spec: Mapped[str | None] = mapped_column(String(255), nullable=True)
    snapshot_material: Mapped[str | None] = mapped_column(String(255), nullable=True)
    snapshot_flute_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    snapshot_score_line: Mapped[str | None] = mapped_column(String(255), nullable=True)
    snapshot_cardboard_length_mm: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    snapshot_cardboard_width_mm: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    snapshot_pieces_per_sheet: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    material_status: Mapped[str] = mapped_column(String(30), nullable=False, default="pending")
    material_received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    material_received_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    inventory_deducted_qty: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    requisition_qty: Mapped[int | None] = mapped_column(Integer, nullable=True)
    requisition_status: Mapped[str] = mapped_column(String(20), nullable=False, default="未报料")
    requisition_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    special_process: Mapped[str] = mapped_column(String(30), nullable=False, default="无")
    production_source: Mapped[str] = mapped_column(String(30), nullable=False, default="ORDER_REQUISITION")

    delivered_quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_force_closed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    order: Mapped[SalesOrder] = relationship(back_populates="items")
    product: Mapped[Product | None] = relationship(back_populates="order_items")
    receiver: Mapped[User | None] = relationship()
    requisition_items: Mapped[list[RequisitionItem]] = relationship(back_populates="order_item", passive_deletes=True)


class Requisition(TimestampMixin, Base):
    __tablename__ = "requisitions"
    __table_args__ = (
        UniqueConstraint("requisition_number", name="uq_requisitions_number"),
    )

    id: Mapped[int] = bigint_pk()
    requisition_number: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    supplier_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    requisition_date: Mapped[date] = mapped_column(Date, nullable=False, server_default=func.current_date())
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="PENDING")
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    items: Mapped[list[RequisitionItem]] = relationship(
        back_populates="requisition",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class RequisitionItem(TimestampMixin, Base):
    __tablename__ = "requisition_items"

    id: Mapped[int] = bigint_pk()
    requisition_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("requisitions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    order_item_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("order_items.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    requisition_qty: Mapped[int] = mapped_column(Integer, nullable=False)
    received_qty: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="PENDING")
    supplier_delivery_no: Mapped[str | None] = mapped_column(String(120), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    requisition: Mapped[Requisition] = relationship(back_populates="items")
    order_item: Mapped[OrderItem] = relationship(back_populates="requisition_items")
    receive_logs: Mapped[list[WmsReceiveLog]] = relationship(
        back_populates="requisition_item",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class WmsReceiveLog(TimestampMixin, Base):
    __tablename__ = "wms_receive_logs"

    id: Mapped[int] = bigint_pk()
    requisition_item_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("requisition_items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    actual_receive_qty: Mapped[int] = mapped_column(Integer, nullable=False)
    received_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    requisition_item: Mapped[RequisitionItem] = relationship(back_populates="receive_logs")


class DeliveryNote(TimestampMixin, Base):
    __tablename__ = "delivery_notes"
    __table_args__ = (UniqueConstraint("delivery_number", name="uq_delivery_notes_number"),)

    id: Mapped[int] = bigint_pk()
    delivery_number: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    customer_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    delivery_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    vehicle_number: Mapped[str | None] = mapped_column(String(80), nullable=True)
    driver_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="DELIVERED_WAIT_RECEIPT", index=True)
    total_quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    customer: Mapped[Customer] = relationship()
    items: Mapped[list[DeliveryItem]] = relationship(
        back_populates="delivery",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    receipts: Mapped[list[ReturnReceipt]] = relationship(back_populates="delivery", passive_deletes=True)


class DeliveryItem(TimestampMixin, Base):
    __tablename__ = "delivery_items"

    id: Mapped[int] = bigint_pk()
    delivery_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("delivery_notes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    order_item_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("order_items.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    delivery_qty: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price_snapshot: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    amount_snapshot: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    remark: Mapped[str | None] = mapped_column(Text, nullable=True)

    delivery: Mapped[DeliveryNote] = relationship(back_populates="items")
    order_item: Mapped[OrderItem] = relationship()
    receipt_items: Mapped[list[ReturnReceiptItem]] = relationship(back_populates="delivery_item", passive_deletes=True)


class ReturnReceipt(TimestampMixin, Base):
    __tablename__ = "return_receipts"

    id: Mapped[int] = bigint_pk()
    delivery_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("delivery_notes.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    actual_received_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    signed_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="SIGNED_WAIT_OWNER_REVIEW", index=True)
    owner_reviewed_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    owner_reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    delivery: Mapped[DeliveryNote] = relationship(back_populates="receipts")
    items: Mapped[list[ReturnReceiptItem]] = relationship(
        back_populates="receipt",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class ReturnReceiptItem(TimestampMixin, Base):
    __tablename__ = "return_receipt_items"

    id: Mapped[int] = bigint_pk()
    receipt_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("return_receipts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    delivery_item_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("delivery_items.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    actual_signed_qty: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price_snapshot: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    difference_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    receipt: Mapped[ReturnReceipt] = relationship(back_populates="items")
    delivery_item: Mapped[DeliveryItem] = relationship(back_populates="receipt_items")
    statement_items: Mapped[list[StatementItem]] = relationship(back_populates="receipt_item", passive_deletes=True)


class Statement(TimestampMixin, Base):
    __tablename__ = "statements"
    __table_args__ = (UniqueConstraint("statement_number", name="uq_statements_number"),)

    id: Mapped[int] = bigint_pk()
    statement_number: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    customer_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    statement_month: Mapped[str] = mapped_column(String(7), nullable=False, index=True)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    total_quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=Decimal("0.00"))
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="DRAFT")
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    customer: Mapped[Customer] = relationship()
    items: Mapped[list[StatementItem]] = relationship(
        back_populates="statement",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class StatementItem(TimestampMixin, Base):
    __tablename__ = "statement_items"
    __table_args__ = (UniqueConstraint("receipt_item_id", name="uq_statement_items_receipt_item"),)

    id: Mapped[int] = bigint_pk()
    statement_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("statements.id", ondelete="CASCADE"), nullable=False, index=True
    )
    receipt_item_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("return_receipt_items.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    delivery_date: Mapped[date] = mapped_column(Date, nullable=False)
    delivery_number: Mapped[str] = mapped_column(String(80), nullable=False)
    order_number: Mapped[str] = mapped_column(String(80), nullable=False)
    customer_po: Mapped[str | None] = mapped_column(String(120), nullable=True)
    product_code: Mapped[str | None] = mapped_column(String(120), nullable=True)
    product_name: Mapped[str] = mapped_column(String(255), nullable=False)
    spec: Mapped[str | None] = mapped_column(String(255), nullable=True)
    actual_signed_qty: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price_snapshot: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    remark: Mapped[str | None] = mapped_column(Text, nullable=True)

    statement: Mapped[Statement] = relationship(back_populates="items")
    receipt_item: Mapped[ReturnReceiptItem] = relationship(back_populates="statement_items")
