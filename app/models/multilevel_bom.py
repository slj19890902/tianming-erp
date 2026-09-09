"""Order-owned immutable BOM graph, with relational product identity guards."""

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base


class OrderBomGraph(Base):
    __tablename__ = "order_bom_graphs"
    __table_args__ = (
        CheckConstraint("schema_version > 0", name="ck_order_bom_graph_schema"),
        CheckConstraint("length(content_hash) = 64", name="ck_order_bom_graph_hash"),
    )
    order_item_id: Mapped[int] = mapped_column(
        ForeignKey("sales_order_items.id", ondelete="RESTRICT"), primary_key=True)
    root_product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"), nullable=False)
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    document_json: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False)


class OrderBomGraphProduct(Base):
    __tablename__ = "order_bom_graph_products"
    __table_args__ = (
        CheckConstraint("product_version > 0", name="ck_order_bom_graph_product_version"),
    )
    order_item_id: Mapped[int] = mapped_column(
        ForeignKey("order_bom_graphs.order_item_id", ondelete="RESTRICT"), primary_key=True)
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"), primary_key=True)
    product_version: Mapped[int] = mapped_column(Integer, nullable=False)


class ProductBomProfile(Base):
    """Explicit physical source; absent rows retain the legacy contract."""
    __tablename__ = "product_bom_profiles"
    __table_args__ = (
        CheckConstraint("source IN ('manufactured','purchased','assembled')", name="ck_product_bom_profile_source"),
    )
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"), primary_key=True)
    source: Mapped[str] = mapped_column(String(20), nullable=False)


class ProductBomInventoryRelation(Base):
    """Meaning of an existing real BOM edge, not a parallel recipe."""
    __tablename__ = "product_bom_inventory_relations"
    __table_args__ = (
        CheckConstraint("relation IN ('assembly','accompany')", name="ck_product_bom_inventory_relation"),
    )
    bom_component_id: Mapped[int] = mapped_column(
        ForeignKey("product_bom_components.id", ondelete="CASCADE"), primary_key=True)
    relation: Mapped[str] = mapped_column(String(20), nullable=False)
