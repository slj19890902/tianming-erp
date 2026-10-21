"""Customer catalog membership; stock stays in the original inventory ledger."""
from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column
from app.models import Base


class CustomerSharingGroup(Base):
    __tablename__ = 'customer_sharing_groups'
    __table_args__ = (CheckConstraint('version >= 1', name='ck_customer_sharing_version'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    new_products_shared: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class CustomerSharingMember(Base):
    __tablename__ = 'customer_sharing_members'
    customer_id: Mapped[int] = mapped_column(ForeignKey('customers.id', ondelete='RESTRICT'), primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey('customer_sharing_groups.id', ondelete='RESTRICT'), index=True)
    delivery_heading_customer_id: Mapped[int | None] = mapped_column(ForeignKey('customers.id', ondelete='RESTRICT'))
    delivery_address_customer_id: Mapped[int | None] = mapped_column(ForeignKey('customers.id', ondelete='RESTRICT'))
