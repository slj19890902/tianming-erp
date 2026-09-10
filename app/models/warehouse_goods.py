"""Operator-confirmed applicability; physical stock and historical receipts stay intact."""
from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from app.models import Base


class WarehouseGoodsProfile(Base):
    __tablename__ = "warehouse_goods_profiles"
    lot_id: Mapped[int] = mapped_column(ForeignKey("inventory_lots.id", ondelete="RESTRICT"), primary_key=True)
    data_json: Mapped[str] = mapped_column(Text)


class WarehouseGoodsMutation(Base):
    __tablename__ = "warehouse_goods_mutations"
    id: Mapped[int] = mapped_column(primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(100), unique=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    lot_id: Mapped[int] = mapped_column(ForeignKey("inventory_lots.id", ondelete="RESTRICT"), index=True)
    response_json: Mapped[str] = mapped_column(Text)
