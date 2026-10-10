"""Freeze replenishment production identity without modifying existing purchases."""
from alembic import op
import sqlalchemy as sa

revision = 'eo1010pi'
down_revision = 'en1009hp'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('stock_replenishment_order_items', sa.Column('production_snapshot_json', sa.Text(), nullable=True))
    if op.get_bind().dialect.name == 'sqlite':
        op.execute("""CREATE TRIGGER stock_purchase_identity_immutable
            BEFORE UPDATE OF production_snapshot_json ON stock_replenishment_order_items
            WHEN OLD.production_snapshot_json IS NOT NULL AND NEW.production_snapshot_json IS NOT OLD.production_snapshot_json
            BEGIN SELECT RAISE(ABORT, 'purchase production identity is immutable'); END""")


def downgrade():
    if op.get_bind().execute(sa.text('SELECT 1 FROM stock_replenishment_order_items WHERE production_snapshot_json IS NOT NULL LIMIT 1')).first():
        raise RuntimeError('存在报料冻结加工身份，禁止有损降级；请保留现库向前修复')
    if op.get_bind().dialect.name == 'sqlite':
        op.execute('DROP TRIGGER stock_purchase_identity_immutable')
        op.execute('ALTER TABLE stock_replenishment_order_items DROP COLUMN production_snapshot_json')
    else:
        op.drop_column('stock_replenishment_order_items', 'production_snapshot_json')
