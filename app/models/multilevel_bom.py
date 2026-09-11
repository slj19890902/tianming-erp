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


class OrderBomExecutionCutover(Base):
    """Explicit legacy/current boundary; does not replace historical facts."""
    __tablename__ = "order_bom_execution_cutovers"
    __table_args__ = (
        CheckConstraint("order_quantity > 0 AND delivered_before >= 0 AND delivered_before < order_quantity",
            name="ck_bom_cutover_quantities"),
        CheckConstraint("length(basis_hash) = 64 AND length(request_hash) = 64",
            name="ck_bom_cutover_hashes"),
        CheckConstraint("length(trim(idempotency_key)) > 0", name="ck_bom_cutover_key"),
    )
    order_item_id: Mapped[int] = mapped_column(
        ForeignKey("order_bom_graphs.order_item_id", ondelete="RESTRICT"), primary_key=True)
    order_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    delivered_before: Mapped[int] = mapped_column(Integer, nullable=False)
    basis_json: Mapped[str] = mapped_column(Text, nullable=False)
    basis_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.current_timestamp(), nullable=False)


class OrderBomCutoverSource(Base):
    """Both epochs keep original snapshot IDs and same-order foreign keys."""
    __tablename__ = "order_bom_cutover_sources"
    __table_args__ = (
        ForeignKeyConstraint(["snapshot_id", "order_item_id"],
            ["sales_order_item_bom_components.id", "sales_order_item_bom_components.sales_order_item_id"],
            ondelete="RESTRICT", name="fk_bom_cutover_source_order"),
        CheckConstraint("role IN ('history','current')", name="ck_bom_cutover_source_role"),
    )
    snapshot_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_item_id: Mapped[int] = mapped_column(
        ForeignKey("order_bom_execution_cutovers.order_item_id", ondelete="RESTRICT"), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(20), nullable=False)


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


class OrderBomRuleRevision(Base):
    """Append-only structural rules; original graph and sources remain intact."""
    __tablename__ = "order_bom_rule_revisions"
    __table_args__ = (
        UniqueConstraint("order_item_id", "revision", name="uq_bom_rule_revision"),
        UniqueConstraint("id", "order_item_id", name="uq_bom_rule_revision_order"),
        UniqueConstraint("id", "order_item_id", "revision", name="uq_bom_rule_revision_chain"),
        ForeignKeyConstraint(["previous_id", "order_item_id", "previous_revision"],
            ["order_bom_rule_revisions.id", "order_bom_rule_revisions.order_item_id", "order_bom_rule_revisions.revision"],
            ondelete="RESTRICT", name="fk_bom_rule_previous"),
        CheckConstraint("revision > 0 AND production_revision_before >= 0", name="ck_bom_rule_revision_number"),
        CheckConstraint("(revision = 1 AND previous_id IS NULL AND previous_revision IS NULL) OR "
            "(revision > 1 AND previous_id IS NOT NULL AND previous_revision IS NOT NULL AND previous_revision = revision - 1)",
            name="ck_bom_rule_previous"),
        CheckConstraint("order_quantity > 0 AND delivered_before >= 0 AND delivered_before < order_quantity",
            name="ck_bom_rule_quantity"),
        CheckConstraint("length(content_hash) = 64 AND length(request_hash) = 64 AND length(review_hash) = 64",
            name="ck_bom_rule_hashes"),
        CheckConstraint("length(trim(idempotency_key)) > 0", name="ck_bom_rule_key"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    order_item_id: Mapped[int] = mapped_column(
        ForeignKey("order_bom_graphs.order_item_id", ondelete="RESTRICT"), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    previous_id: Mapped[int | None] = mapped_column(Integer)
    previous_revision: Mapped[int | None] = mapped_column(Integer)
    production_revision_before: Mapped[int] = mapped_column(Integer, nullable=False)
    order_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    delivered_before: Mapped[int] = mapped_column(Integer, nullable=False)
    document_json: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    review_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.current_timestamp(), nullable=False)


class OrderBomRuleProduct(Base):
    __tablename__ = "order_bom_rule_products"
    __table_args__ = (
        UniqueConstraint("revision_id", "product_id", "order_item_id", name="uq_bom_rule_product_order"),
        ForeignKeyConstraint(["revision_id", "order_item_id"],
            ["order_bom_rule_revisions.id", "order_bom_rule_revisions.order_item_id"],
            ondelete="RESTRICT", name="fk_bom_rule_product_revision"),
        CheckConstraint("product_version > 0", name="ck_bom_rule_product_version"),
    )
    revision_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="RESTRICT"), primary_key=True)
    order_item_id: Mapped[int] = mapped_column(Integer, nullable=False)
    product_version: Mapped[int] = mapped_column(Integer, nullable=False)


class OrderBomRuleSource(Base):
    __tablename__ = "order_bom_rule_sources"
    __table_args__ = (
        UniqueConstraint("revision_id", "product_id", name="uq_bom_rule_source_product"),
        UniqueConstraint("snapshot_id", "revision_id", "order_item_id", "product_id", name="uq_bom_rule_source_identity"),
        ForeignKeyConstraint(["revision_id", "product_id", "order_item_id"],
            ["order_bom_rule_products.revision_id", "order_bom_rule_products.product_id", "order_bom_rule_products.order_item_id"],
            ondelete="RESTRICT", name="fk_bom_rule_source_product"),
        ForeignKeyConstraint(["snapshot_id", "order_item_id", "product_id"],
            ["sales_order_item_bom_components.id", "sales_order_item_bom_components.sales_order_item_id",
             "sales_order_item_bom_components.component_product_id"],
            ondelete="RESTRICT", name="fk_bom_rule_source_order"),
    )
    snapshot_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    revision_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    product_id: Mapped[int] = mapped_column(Integer, nullable=False)
    order_item_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)


class OrderBomSourceHandoff(Base):
    """Explicit old-to-new source identity; supplier and receipt facts stay put."""
    __tablename__ = "order_bom_source_handoffs"
    __table_args__ = (
        ForeignKeyConstraint(["source_snapshot_id", "order_item_id", "product_id"],
            ["sales_order_item_bom_components.id", "sales_order_item_bom_components.sales_order_item_id",
             "sales_order_item_bom_components.component_product_id"],
            ondelete="RESTRICT", name="fk_bom_handoff_source"),
        ForeignKeyConstraint(["target_snapshot_id", "revision_id", "order_item_id", "product_id"],
            ["order_bom_rule_sources.snapshot_id", "order_bom_rule_sources.revision_id",
             "order_bom_rule_sources.order_item_id", "order_bom_rule_sources.product_id"],
            ondelete="RESTRICT", name="fk_bom_handoff_target"),
        CheckConstraint("source_snapshot_id <> target_snapshot_id", name="ck_bom_handoff_distinct"),
        CheckConstraint("source_kind IN ('manufactured', 'purchased')", name="ck_bom_handoff_kind"),
        CheckConstraint("length(source_basis_hash) = 64 AND length(target_basis_hash) = 64", name="ck_bom_handoff_hash"),
    )
    revision_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_snapshot_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    target_snapshot_id: Mapped[int] = mapped_column(Integer, nullable=False)
    order_item_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    product_id: Mapped[int] = mapped_column(Integer, nullable=False)
    source_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    source_basis_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    target_basis_hash: Mapped[str] = mapped_column(String(64), nullable=False)


class OrderBomExternalComponent(Base):
    """Stable link between a procurement snapshot and its real frozen node."""
    __tablename__ = "order_bom_external_components"
    __table_args__ = (
        ForeignKeyConstraint(["order_item_id", "product_id"],
            ["order_bom_graph_products.order_item_id", "order_bom_graph_products.product_id"],
            ondelete="RESTRICT", name="fk_bom_external_frozen_product"),
        ForeignKeyConstraint(["bom_snapshot_id", "order_item_id", "product_id"],
            ["sales_order_item_bom_components.id", "sales_order_item_bom_components.sales_order_item_id",
             "sales_order_item_bom_components.component_product_id"],
            ondelete="RESTRICT", name="fk_bom_external_source_identity"),
        ForeignKeyConstraint(["external_component_id", "order_item_id"],
            ["sales_order_item_external_components.id", "sales_order_item_external_components.sales_order_item_id"],
            ondelete="RESTRICT", name="fk_bom_external_procurement_owner"),
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
        CheckConstraint("source IN ('manufactured','purchased','assembled','separate')", name="ck_product_bom_profile_source"),
        CheckConstraint("(material_mode IS NULL AND delivery_mode IS NULL AND source <> 'separate') OR "
                        "(material_mode = 'expand_children' AND material_mode IS NOT NULL "
                        "AND delivery_mode IS NOT NULL AND delivery_mode IN ('parent','components') "
                        "AND NOT (source = 'assembled' AND delivery_mode = 'components'))",
                        name="ck_product_bom_profile_modes"),
    )
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"), primary_key=True)
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    material_mode: Mapped[str | None] = mapped_column(String(30))
    delivery_mode: Mapped[str | None] = mapped_column(String(20))


class ProductBomInventoryRelation(Base):
    """Meaning of an existing real BOM edge, not a parallel recipe."""
    __tablename__ = "product_bom_inventory_relations"
    __table_args__ = (
        CheckConstraint("relation IN ('assembly','accompany')", name="ck_product_bom_inventory_relation"),
    )
    bom_component_id: Mapped[int] = mapped_column(
        ForeignKey("product_bom_components.id", ondelete="CASCADE"), primary_key=True)
    relation: Mapped[str] = mapped_column(String(20), nullable=False)


class BomBodyInventoryDetail(Base):
    """Identity of unassembled manufactured bodies; balances remain in InventoryLot."""
    __tablename__ = "bom_body_inventory_details"
    __table_args__ = (
        ForeignKeyConstraint(["inventory_lot_id", "inventory_type"],
            ["inventory_lots.id", "inventory_lots.inventory_type"],
            ondelete="RESTRICT", name="fk_bom_body_lot_type"),
        ForeignKeyConstraint(["order_item_id", "product_id"],
            ["order_bom_graph_products.order_item_id", "order_bom_graph_products.product_id"],
            ondelete="RESTRICT", name="fk_bom_body_frozen_product"),
        CheckConstraint("inventory_type = 'assembly_body'", name="ck_bom_body_type"),
    )
    inventory_lot_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    inventory_type: Mapped[str] = mapped_column(String(30), nullable=False,
        default="assembly_body", server_default="assembly_body")
    order_item_id: Mapped[int] = mapped_column(Integer, nullable=False)
    product_id: Mapped[int] = mapped_column(Integer, nullable=False)
    production_completion_id: Mapped[int] = mapped_column(
        ForeignKey("production_completions.id", ondelete="RESTRICT"), nullable=False, index=True)


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
