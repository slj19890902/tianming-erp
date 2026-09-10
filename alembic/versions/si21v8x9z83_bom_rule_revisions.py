"""Append immutable structural BOM revisions; no existing fact is rewritten."""
from alembic import op
import sqlalchemy as sa

revision = "si21v8x9z83"
down_revision = "sh20v8x9z82"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index("uq_bom_snapshot_order_product_identity", "sales_order_item_bom_components",
                    ["id", "sales_order_item_id", "component_product_id"], unique=True)
    op.create_table("order_bom_rule_revisions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("order_item_id", sa.Integer(), sa.ForeignKey("order_bom_graphs.order_item_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("previous_id", sa.Integer()),
        sa.Column("previous_revision", sa.Integer()),
        sa.Column("production_revision_before", sa.Integer(), nullable=False),
        sa.Column("order_quantity", sa.Integer(), nullable=False),
        sa.Column("delivered_before", sa.Integer(), nullable=False),
        sa.Column("document_json", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("review_hash", sa.String(64), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(120), nullable=False, unique=True),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False),
        sa.UniqueConstraint("order_item_id", "revision", name="uq_bom_rule_revision"),
        sa.UniqueConstraint("id", "order_item_id", name="uq_bom_rule_revision_order"),
        sa.UniqueConstraint("id", "order_item_id", "revision", name="uq_bom_rule_revision_chain"),
        sa.ForeignKeyConstraint(["previous_id", "order_item_id", "previous_revision"],
            ["order_bom_rule_revisions.id", "order_bom_rule_revisions.order_item_id", "order_bom_rule_revisions.revision"],
            ondelete="RESTRICT", name="fk_bom_rule_previous"),
        sa.CheckConstraint("revision > 0 AND production_revision_before >= 0", name="ck_bom_rule_revision_number"),
        sa.CheckConstraint("(revision = 1 AND previous_id IS NULL AND previous_revision IS NULL) OR "
            "(revision > 1 AND previous_id IS NOT NULL AND previous_revision IS NOT NULL AND previous_revision = revision - 1)", name="ck_bom_rule_previous"),
        sa.CheckConstraint("order_quantity > 0 AND delivered_before >= 0 AND delivered_before < order_quantity", name="ck_bom_rule_quantity"),
        sa.CheckConstraint("length(content_hash) = 64 AND length(request_hash) = 64 AND length(review_hash) = 64", name="ck_bom_rule_hashes"),
        sa.CheckConstraint("length(trim(idempotency_key)) > 0", name="ck_bom_rule_key"))
    op.create_table("order_bom_rule_products",
        sa.Column("revision_id", sa.Integer(), primary_key=True),
        sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("order_item_id", sa.Integer(), nullable=False),
        sa.Column("product_version", sa.Integer(), nullable=False),
        sa.UniqueConstraint("revision_id", "product_id", "order_item_id", name="uq_bom_rule_product_order"),
        sa.ForeignKeyConstraint(["revision_id", "order_item_id"],
            ["order_bom_rule_revisions.id", "order_bom_rule_revisions.order_item_id"], ondelete="RESTRICT", name="fk_bom_rule_product_revision"),
        sa.CheckConstraint("product_version > 0", name="ck_bom_rule_product_version"))
    op.create_table("order_bom_rule_sources",
        sa.Column("snapshot_id", sa.Integer(), primary_key=True),
        sa.Column("revision_id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("order_item_id", sa.Integer(), nullable=False),
        sa.UniqueConstraint("revision_id", "product_id", name="uq_bom_rule_source_product"),
        sa.ForeignKeyConstraint(["revision_id", "product_id", "order_item_id"],
            ["order_bom_rule_products.revision_id", "order_bom_rule_products.product_id", "order_bom_rule_products.order_item_id"],
            ondelete="RESTRICT", name="fk_bom_rule_source_product"),
        sa.ForeignKeyConstraint(["snapshot_id", "order_item_id", "product_id"],
            ["sales_order_item_bom_components.id", "sales_order_item_bom_components.sales_order_item_id",
             "sales_order_item_bom_components.component_product_id"],
            ondelete="RESTRICT", name="fk_bom_rule_source_order"))
    op.create_index("ix_order_bom_rule_sources_revision_id", "order_bom_rule_sources", ["revision_id"])
    op.create_index("ix_order_bom_rule_sources_order_item_id", "order_bom_rule_sources", ["order_item_id"])


def downgrade():
    bind = op.get_bind()
    for table in ("order_bom_rule_revisions", "order_bom_rule_products", "order_bom_rule_sources"):
        if bind.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first():
            raise RuntimeError("已有BOM规则版本事实，禁止删除；请使用经过验证的时点备份恢复")
    op.drop_table("order_bom_rule_sources")
    op.drop_table("order_bom_rule_products")
    op.drop_table("order_bom_rule_revisions")
    op.drop_index("uq_bom_snapshot_order_product_identity", table_name="sales_order_item_bom_components")
