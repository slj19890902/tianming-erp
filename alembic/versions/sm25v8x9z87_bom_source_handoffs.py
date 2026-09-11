"""Append source handoff identities without changing procurement or stock."""
from alembic import op
import sqlalchemy as sa

revision = "sm25v8x9z87"
down_revision = "sl24v8x9z86"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index("uq_bom_rule_source_identity", "order_bom_rule_sources",
        ["snapshot_id", "revision_id", "order_item_id", "product_id"], unique=True)
    op.create_table("order_bom_source_handoffs",
        sa.Column("revision_id", sa.Integer(), primary_key=True),
        sa.Column("source_snapshot_id", sa.Integer(), primary_key=True),
        sa.Column("target_snapshot_id", sa.Integer(), nullable=False),
        sa.Column("order_item_id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("source_kind", sa.String(20), nullable=False),
        sa.Column("source_basis_hash", sa.String(64), nullable=False),
        sa.Column("target_basis_hash", sa.String(64), nullable=False),
        sa.ForeignKeyConstraint(["source_snapshot_id", "order_item_id", "product_id"],
            ["sales_order_item_bom_components.id", "sales_order_item_bom_components.sales_order_item_id",
             "sales_order_item_bom_components.component_product_id"],
            ondelete="RESTRICT", name="fk_bom_handoff_source"),
        sa.ForeignKeyConstraint(["target_snapshot_id", "revision_id", "order_item_id", "product_id"],
            ["order_bom_rule_sources.snapshot_id", "order_bom_rule_sources.revision_id",
             "order_bom_rule_sources.order_item_id", "order_bom_rule_sources.product_id"],
            ondelete="RESTRICT", name="fk_bom_handoff_target"),
        sa.CheckConstraint("source_snapshot_id <> target_snapshot_id", name="ck_bom_handoff_distinct"),
        sa.CheckConstraint("source_kind IN ('manufactured', 'purchased')", name="ck_bom_handoff_kind"),
        sa.CheckConstraint("length(source_basis_hash) = 64 AND length(target_basis_hash) = 64", name="ck_bom_handoff_hash"))
    op.create_index("ix_order_bom_source_handoffs_order_item_id", "order_bom_source_handoffs", ["order_item_id"])
    for operation in ("UPDATE", "DELETE"):
        op.execute(sa.text(f"CREATE TRIGGER bom_source_handoff_no_{operation.lower()} "
            f"BEFORE {operation} ON order_bom_source_handoffs BEGIN "
            "SELECT RAISE(ABORT, 'BOM source handoff facts are immutable'); END"))


def downgrade():
    if op.get_bind().execute(sa.text("SELECT 1 FROM order_bom_source_handoffs LIMIT 1")).first():
        raise RuntimeError("已有BOM来源交接事实，禁止删除；请使用经过验证的时点备份恢复")
    op.drop_table("order_bom_source_handoffs")
    op.drop_index("uq_bom_rule_source_identity", table_name="order_bom_rule_sources")
