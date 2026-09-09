"""Order-owned immutable BOM graph, with relational product identity guards."""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint, Integer, Numeric, String, Text, UniqueConstraint, func
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


class OrderBomProductionRevision(Base):
    """Append-only production amendments; original graph/material rows stay intact."""
    __tablename__ = "order_bom_production_revisions"
    __table_args__ = (
        UniqueConstraint("order_item_id", "revision", name="uq_bom_production_revision"),
        UniqueConstraint("id", "order_item_id", name="uq_bom_production_revision_identity"),
        ForeignKeyConstraint(["previous_id", "order_item_id"],
            ["order_bom_production_revisions.id", "order_bom_production_revisions.order_item_id"],
            ondelete="RESTRICT", name="fk_bom_production_previous"),
        CheckConstraint("revision > 0 AND length(content_hash) = 64", name="ck_bom_production_revision"),
        CheckConstraint("(revision = 1 AND previous_id IS NULL) OR (revision > 1 AND previous_id IS NOT NULL)",
            name="ck_bom_production_previous"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    order_item_id: Mapped[int] = mapped_column(ForeignKey("order_bom_graphs.order_item_id", ondelete="RESTRICT"), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    previous_id: Mapped[int | None] = mapped_column(Integer)
    document_json: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.current_timestamp(), nullable=False)


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


class OrderBomExternalComponent(Base):
    """Stable link between a procurement snapshot and its real frozen node."""
    __tablename__ = "order_bom_external_components"
    __table_args__ = (
        ForeignKeyConstraint(["order_item_id", "product_id"],
            ["order_bom_graph_products.order_item_id", "order_bom_graph_products.product_id"],
            ondelete="RESTRICT", name="fk_bom_external_frozen_product"),
        UniqueConstraint("order_item_id", "product_id", name="uq_bom_external_product"),
    )
    external_component_id: Mapped[int] = mapped_column(
        ForeignKey("sales_order_item_external_components.id", ondelete="RESTRICT"), primary_key=True)
    order_item_id: Mapped[int] = mapped_column(Integer, nullable=False)
    product_id: Mapped[int] = mapped_column(Integer, nullable=False)
    bom_snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("sales_order_item_bom_components.id", ondelete="RESTRICT"), nullable=False, unique=True)


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


class BomAssembly(Base):
    """Conversion provenance only; all stock balances stay in InventoryLot."""
    __tablename__ = "bom_assemblies"
    __table_args__ = (
        ForeignKeyConstraint(["order_item_id", "output_product_id"],
            ["order_bom_graph_products.order_item_id", "order_bom_graph_products.product_id"],
            ondelete="RESTRICT", name="fk_bom_assembly_frozen_product"),
        CheckConstraint("quantity >= 0 AND total_cost >= 0", name="ck_bom_assembly_positive"),
        CheckConstraint("status IN ('posted','reversed')", name="ck_bom_assembly_status"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    order_item_id: Mapped[int] = mapped_column(ForeignKey("order_bom_graphs.order_item_id", ondelete="RESTRICT"), index=True)
    output_product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="RESTRICT"), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    output_lot_id: Mapped[int | None] = mapped_column(ForeignKey("inventory_lots.id", ondelete="RESTRICT"))
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    total_cost: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    cost_detail_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="posted")
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.current_timestamp())


class BomAssemblyInput(Base):
    __tablename__ = "bom_assembly_inputs"
    __table_args__ = (
        UniqueConstraint("conversion_id", "lot_id", name="uq_bom_assembly_input"),
        CheckConstraint("quantity > 0 AND total_cost >= 0", name="ck_bom_assembly_input_positive"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    conversion_id: Mapped[int] = mapped_column(ForeignKey("bom_assemblies.id", ondelete="RESTRICT"), index=True)
    lot_id: Mapped[int] = mapped_column(ForeignKey("inventory_lots.id", ondelete="RESTRICT"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="RESTRICT"))
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    total_cost: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    consume_movement_id: Mapped[int] = mapped_column(ForeignKey("inventory_movements.id", ondelete="RESTRICT"))
    reservations_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
