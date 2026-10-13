"""Freeze ordinary order stock identity; historical orders stay unknown."""
from alembic import op
import sqlalchemy as sa

revision = "eq1013oi"
down_revision = "ep1010md"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("sales_order_items", sa.Column("stock_identity_snapshot_json", sa.Text(), nullable=True))


def downgrade():
    if op.get_bind().execute(sa.text("SELECT 1 FROM sales_order_items WHERE stock_identity_snapshot_json IS NOT NULL LIMIT 1")).first():
        raise RuntimeError("已有订单实物身份快照，禁止有损降级；请保留现库向前修复")
    if op.get_bind().dialect.name == "sqlite":
        op.execute("ALTER TABLE sales_order_items DROP COLUMN stock_identity_snapshot_json")
    else:
        op.drop_column("sales_order_items", "stock_identity_snapshot_json")
