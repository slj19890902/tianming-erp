"""Freeze customer and physical quantities without reinterpreting old deliveries."""
from alembic import op
import sqlalchemy as sa

revision = 'eb0926dq'
down_revision = 'ea0926'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('sales_delivery_items', sa.Column('quantity_contract_json', sa.Text(), nullable=True))
    op.add_column('stock_replenishment_order_items', sa.Column('quantity_contract_json', sa.Text(), nullable=True))
    for table, name in (
        ('inventory_reservations', 'ck_reservation_requirement_denominator'),
        ('delivery_inventory_allocations', 'ck_delivery_allocation_requirement_denominator'),
    ):
        op.add_column(table, sa.Column('requirement_quantity_denominator', sa.Integer(),
            sa.CheckConstraint('requirement_quantity_denominator > 0', name=name),
            nullable=False, server_default='1'))


def downgrade():
    if op.get_bind().execute(sa.text(
        'SELECT 1 FROM stock_replenishment_order_items WHERE quantity_contract_json IS NOT NULL LIMIT 1'
    )).first():
        raise RuntimeError('已有补库实物需求快照，禁止有损降级；保留数据库向前修复')
    for table in ('inventory_reservations', 'delivery_inventory_allocations'):
        if op.get_bind().execute(sa.text(
            f'SELECT 1 FROM {table} WHERE requirement_quantity_denominator <> 1 LIMIT 1'
        )).first():
            raise RuntimeError('已有精确分数需求数量，禁止有损降级；保留数据库向前修复')
    if op.get_bind().execute(sa.text(
        'SELECT 1 FROM sales_delivery_items WHERE quantity_contract_json IS NOT NULL LIMIT 1'
    )).first():
        raise RuntimeError('已有客户/实物数量快照，禁止有损降级；保留数据库向前修复')
    op.execute(sa.text('ALTER TABLE sales_delivery_items DROP COLUMN quantity_contract_json'))
    op.execute(sa.text('ALTER TABLE stock_replenishment_order_items DROP COLUMN quantity_contract_json'))
    for table in ('delivery_inventory_allocations', 'inventory_reservations'):
        op.execute(sa.text(f'ALTER TABLE {table} DROP COLUMN requirement_quantity_denominator'))
