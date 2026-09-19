"""Frozen raw-board procurement decisions; inventory remains in the existing ledger."""
from datetime import datetime
from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, Numeric, UniqueConstraint, func, text
from decimal import Decimal
from sqlalchemy.orm import Mapped, mapped_column
from app.models import Base


class RawPurchasePlan(Base):
    __tablename__ = 'raw_purchase_plans'
    __table_args__ = (CheckConstraint("status IN ('active','voided')",name='ck_raw_plan_status'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    stock_item_id: Mapped[int] = mapped_column(ForeignKey('stock_replenishment_order_items.id',ondelete='RESTRICT'),unique=True)
    supplier_order_id: Mapped[int] = mapped_column(ForeignKey('supplier_requisition_orders.id',ondelete='RESTRICT'))
    customer_id: Mapped[int] = mapped_column(ForeignKey('customers.id',ondelete='RESTRICT'))
    operation_key: Mapped[str] = mapped_column(String(64),unique=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    snapshot_json: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20),default='active',server_default='active')
    created_by: Mapped[int] = mapped_column(ForeignKey('users.id',ondelete='RESTRICT'))
    created_at: Mapped[datetime] = mapped_column(DateTime,server_default=func.current_timestamp())


class RawPurchaseDemand(Base):
    __tablename__ = 'raw_purchase_demands'
    __table_args__ = (
        CheckConstraint("status IN ('active','voided')",name='ck_raw_demand_status'),
        CheckConstraint("raw_quantity > 0 AND piece_quantity > 0 AND yield_factor > 0 AND piece_quantity = raw_quantity * yield_factor",name='ck_raw_demand_quantity'),
        Index('uq_raw_active_demand','source_key',unique=True,sqlite_where=text("status = 'active'"),postgresql_where=text("status = 'active'")),
        Index('ix_raw_demand_order','order_item_id','status'),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey('raw_purchase_plans.id',ondelete='RESTRICT'))
    order_item_id: Mapped[int] = mapped_column(ForeignKey('sales_order_items.id',ondelete='RESTRICT'))
    product_id: Mapped[int] = mapped_column(ForeignKey('products.id',ondelete='RESTRICT'))
    requirement_id: Mapped[int] = mapped_column(ForeignKey('order_item_semi_requirements.id',ondelete='RESTRICT'))
    source_key: Mapped[str] = mapped_column(String(100))
    snapshot_json: Mapped[str] = mapped_column(Text)
    raw_quantity: Mapped[int] = mapped_column(Integer)
    piece_quantity: Mapped[int] = mapped_column(Integer)

    yield_factor: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20),default='active',server_default='active')


class RawPurchaseReceiptAllocation(Base):
    __tablename__ = 'raw_purchase_receipt_allocations'
    __table_args__ = (
        CheckConstraint('raw_quantity > 0 AND piece_quantity > 0 AND raw_offset >= 0 AND total_cost >= 0',name='ck_raw_receipt_quantity'),
        Index('uq_raw_receipt_demand','receipt_item_id','demand_id',unique=True),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    demand_id: Mapped[int] = mapped_column(ForeignKey('raw_purchase_demands.id',ondelete='RESTRICT'))
    receipt_item_id: Mapped[int] = mapped_column(ForeignKey('incoming_receipt_items.id',ondelete='RESTRICT'))
    reservation_id: Mapped[int] = mapped_column(ForeignKey('inventory_reservations.id',ondelete='RESTRICT'),unique=True)
    raw_quantity: Mapped[int] = mapped_column(Integer)
    piece_quantity: Mapped[int] = mapped_column(Integer)
    raw_offset: Mapped[int] = mapped_column(Integer)
    total_cost: Mapped[Decimal] = mapped_column(Numeric(20,4))


class RawPurchaseDeliveryCostPortion(Base):
    """Typed source portions of the existing delivery cost fact, no second ledger."""
    __tablename__='raw_purchase_delivery_cost_portions'
    __table_args__=(
        UniqueConstraint('fact_id','ordinal',name='uq_raw_delivery_portion'),
        CheckConstraint('ordinal >= 0 AND full_output_cost >= 0 AND charged_cost >= 0 AND tax_rate >= 0 AND tax_rate <= 1',name='ck_raw_delivery_cost'),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    fact_id: Mapped[int] = mapped_column(ForeignKey('finance_delivery_graph_cost_facts.id',ondelete='RESTRICT'),index=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    raw_receipt_allocation_id: Mapped[int] = mapped_column(ForeignKey('raw_purchase_receipt_allocations.id',ondelete='RESTRICT'))
    full_output_cost: Mapped[Decimal] = mapped_column(Numeric(20,6))
    charged_cost: Mapped[Decimal] = mapped_column(Numeric(20,6))
    tax_included: Mapped[bool]
    tax_rate: Mapped[Decimal] = mapped_column(Numeric(8,6))
    purchase_receipt_fact_id=None
    purpose_allocation_id=None
    external_receipt_item_id=None
