"""Delivery-only customer PO override; never backfill upstream business facts."""
from alembic import op
import sqlalchemy as sa
revision = "rr08v8x9z67"
down_revision = "rq07v8x9z66"
branch_labels = None
depends_on = None

def upgrade():
    op.add_column("sales_delivery_items", sa.Column("customer_po_snapshot", sa.String(200), nullable=True))

def downgrade():
    bind = op.get_bind()
    if bind.scalar(sa.text("SELECT COUNT(*) FROM sales_delivery_items WHERE customer_po_snapshot IS NOT NULL")):
        raise RuntimeError("送货单已有独立客户单号，禁止删除；请保留数据并回退代码。")
    op.drop_column("sales_delivery_items", "customer_po_snapshot")
