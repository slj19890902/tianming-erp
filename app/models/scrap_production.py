"""Occasional off-ledger scrap work; output uses the existing stock ledger."""
from datetime import date, datetime
from sqlalchemy import CheckConstraint, Date, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column
from app.models import Base


class ScrapProductionJob(Base):
    __tablename__='scrap_production_jobs'
    __table_args__=(CheckConstraint("status IN ('pending','completed','cancelled')",name='ck_scrap_production_status'),
        CheckConstraint('quantity>0 AND version>=1',name='ck_scrap_production_quantity'))
    id:Mapped[int]=mapped_column(primary_key=True)
    customer_id:Mapped[int]=mapped_column(ForeignKey('customers.id',ondelete='RESTRICT'),index=True)
    product_id:Mapped[int]=mapped_column(ForeignKey('products.id',ondelete='RESTRICT'))
    order_item_id:Mapped[int|None]=mapped_column(ForeignKey('sales_order_items.id',ondelete='RESTRICT'),index=True)
    quantity:Mapped[int]=mapped_column(Integer)
    location_id:Mapped[int]=mapped_column(ForeignKey('warehouse_locations.id',ondelete='RESTRICT'))
    stock_date:Mapped[date]=mapped_column(Date)
    status:Mapped[str]=mapped_column(String(20),default='pending')
    snapshot_json:Mapped[str]=mapped_column(Text)
    output_lot_id:Mapped[int|None]=mapped_column(ForeignKey('inventory_lots.id',ondelete='RESTRICT'))
    version:Mapped[int]=mapped_column(Integer,default=1)
    actor_id:Mapped[int]=mapped_column(ForeignKey('users.id',ondelete='RESTRICT'))
    created_at:Mapped[datetime]=mapped_column(DateTime,server_default=func.current_timestamp())
