"""Immutable multi-level order BOM snapshot storage. No business backfill."""
from alembic import op
import sqlalchemy as sa

revision = "ru09v8x9z69"
down_revision = "rt09v8x9z68"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("order_bom_graphs",
        sa.Column("order_item_id", sa.Integer(), sa.ForeignKey("sales_order_items.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("root_product_id", sa.Integer(), sa.ForeignKey("products.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("customer_id", sa.Integer(), sa.ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("document_json", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False),
        sa.CheckConstraint("schema_version > 0", name="ck_order_bom_graph_schema"),
        sa.CheckConstraint("length(content_hash) = 64", name="ck_order_bom_graph_hash"))
    op.create_table("order_bom_graph_products",
        sa.Column("order_item_id", sa.Integer(), sa.ForeignKey("order_bom_graphs.order_item_id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("product_version", sa.Integer(), nullable=False),
        sa.CheckConstraint("product_version > 0", name="ck_order_bom_graph_product_version"))


def downgrade():
    for table in ("order_bom_graph_products", "order_bom_graphs"):
        if op.get_bind().execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first():
            raise RuntimeError("订单BOM已有冻结记录，禁止删除；请使用经验证的发布前备份回滚")
    op.drop_table("order_bom_graph_products")
    op.drop_table("order_bom_graphs")
