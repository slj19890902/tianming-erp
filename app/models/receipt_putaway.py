"""Receipt routing preferences, not another inventory ledger."""
from sqlalchemy import CheckConstraint, ForeignKey, Integer
from sqlalchemy.orm import Mapped, mapped_column
from app.models import Base


class ProductStoragePreference(Base):
    __tablename__ = "product_storage_preferences"
    __table_args__ = (CheckConstraint("version > 0", name="ck_product_storage_version"),)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="RESTRICT"), primary_key=True)
    area_id: Mapped[int | None] = mapped_column(ForeignKey("warehouse_areas.id", ondelete="RESTRICT"))
    location_id: Mapped[int | None] = mapped_column(ForeignKey("warehouse_locations.id", ondelete="RESTRICT"))
    # A row with no destination is an explicit cancellation: never auto-remember again.
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class ReceiptStagingArea(Base):
    __tablename__ = "receipt_staging_areas"
    area_id: Mapped[int] = mapped_column(ForeignKey("warehouse_areas.id", ondelete="RESTRICT"), primary_key=True)
