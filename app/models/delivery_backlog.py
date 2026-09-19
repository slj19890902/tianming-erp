"""Persistent notice tracking linked to existing order debt, not a second order."""
from datetime import date,datetime
from sqlalchemy import CheckConstraint,Date,DateTime,ForeignKey,Index,Integer,String,Text,UniqueConstraint,func
from sqlalchemy.orm import Mapped,mapped_column
from app.models import Base


class DeliveryBacklog(Base):
    __tablename__='delivery_backlogs'
    __table_args__=(
        CheckConstraint("status IN ('active','cancelled') AND version > 0 AND original_quantity > 0 AND target_quantity >= original_quantity",name='ck_delivery_backlog'),
        Index('ix_delivery_backlog_scope','customer_id','status','due_date','created_at'),
    )
    id:Mapped[int]=mapped_column(primary_key=True)
    customer_id:Mapped[int]=mapped_column(ForeignKey('customers.id',ondelete='RESTRICT'))
    product_id:Mapped[int]=mapped_column(ForeignKey('products.id',ondelete='RESTRICT'))
    order_item_id:Mapped[int]=mapped_column(ForeignKey('sales_order_items.id',ondelete='RESTRICT'),unique=True)
    stock_code:Mapped[str]=mapped_column(String(100))
    product_name:Mapped[str]=mapped_column(String(250))
    customer_order_no:Mapped[str|None]=mapped_column(String(150))
    due_date:Mapped[date|None]=mapped_column(Date)
    original_quantity:Mapped[int]=mapped_column(Integer)
    target_quantity:Mapped[int]=mapped_column(Integer)
    reason:Mapped[str]=mapped_column(String(500))
    status:Mapped[str]=mapped_column(String(20),default='active',server_default='active')
    version:Mapped[int]=mapped_column(Integer,default=1,server_default='1')
    cancelled_reason:Mapped[str|None]=mapped_column(String(500))
    cancelled_by:Mapped[int|None]=mapped_column(ForeignKey('users.id',ondelete='RESTRICT'))
    cancelled_at:Mapped[datetime|None]=mapped_column(DateTime)
    created_by:Mapped[int|None]=mapped_column(ForeignKey('users.id',ondelete='RESTRICT'))
    created_at:Mapped[datetime]=mapped_column(DateTime,server_default=func.current_timestamp())


class DeliveryBacklogSource(Base):
    __tablename__='delivery_backlog_sources'
    __table_args__=(CheckConstraint('requested_quantity > 0',name='ck_backlog_source_quantity'),)
    id:Mapped[int]=mapped_column(primary_key=True)
    backlog_id:Mapped[int]=mapped_column(ForeignKey('delivery_backlogs.id',ondelete='RESTRICT'),index=True)
    import_item_id:Mapped[int]=mapped_column(ForeignKey('tianhua_pre_delivery_import_items.id',ondelete='RESTRICT'),unique=True)
    requested_quantity:Mapped[int]=mapped_column(Integer)
    snapshot_json:Mapped[str]=mapped_column(Text)
    created_at:Mapped[datetime]=mapped_column(DateTime,server_default=func.current_timestamp())


class DeliveryBacklogFulfillment(Base):
    __tablename__='delivery_backlog_fulfillments'
    __table_args__=(
        UniqueConstraint('backlog_id','delivery_item_id','dispatched_at',name='uq_backlog_dispatch'),
        CheckConstraint("quantity > 0 AND status IN ('posted','reversed')",name='ck_backlog_fulfillment'),
    )
    id:Mapped[int]=mapped_column(primary_key=True)
    backlog_id:Mapped[int]=mapped_column(ForeignKey('delivery_backlogs.id',ondelete='RESTRICT'),index=True)
    delivery_item_id:Mapped[int]=mapped_column(ForeignKey('sales_delivery_items.id',ondelete='RESTRICT'),index=True)
    quantity:Mapped[int]=mapped_column(Integer)
    dispatched_at:Mapped[datetime]=mapped_column(DateTime)
    status:Mapped[str]=mapped_column(String(20),default='posted',server_default='posted')
    reversed_at:Mapped[datetime|None]=mapped_column(DateTime)
    created_by:Mapped[int|None]=mapped_column(ForeignKey('users.id',ondelete='RESTRICT'))
