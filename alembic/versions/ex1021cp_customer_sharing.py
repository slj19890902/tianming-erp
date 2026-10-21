"""Explicit customer catalog sharing, no historical backfill."""
from alembic import op
import sqlalchemy as sa

revision = 'ex1021cp'
down_revision = 'ew1020em'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('products',sa.Column('production_variants',sa.JSON(none_as_null=True),nullable=True))
    op.create_table('customer_sharing_groups',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('name', sa.String(200), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('new_products_shared', sa.Boolean(), nullable=False),
        sa.CheckConstraint('version >= 1', name='ck_customer_sharing_version'))
    op.create_table('customer_sharing_members',
        sa.Column('customer_id', sa.Integer(), sa.ForeignKey('customers.id', ondelete='RESTRICT'), primary_key=True),
        sa.Column('group_id', sa.Integer(), sa.ForeignKey('customer_sharing_groups.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('delivery_heading_customer_id', sa.Integer(), sa.ForeignKey('customers.id', ondelete='RESTRICT')),
        sa.Column('delivery_address_customer_id', sa.Integer(), sa.ForeignKey('customers.id', ondelete='RESTRICT')))
    op.create_index('ix_customer_sharing_members_group_id', 'customer_sharing_members', ['group_id'])
    op.create_table('scrap_production_jobs',
        sa.Column('id',sa.Integer(),primary_key=True),
        sa.Column('customer_id',sa.Integer(),sa.ForeignKey('customers.id',ondelete='RESTRICT'),nullable=False),
        sa.Column('product_id',sa.Integer(),sa.ForeignKey('products.id',ondelete='RESTRICT'),nullable=False),
        sa.Column('order_item_id',sa.Integer(),sa.ForeignKey('sales_order_items.id',ondelete='RESTRICT')),
        sa.Column('quantity',sa.Integer(),nullable=False),
        sa.Column('location_id',sa.Integer(),sa.ForeignKey('warehouse_locations.id',ondelete='RESTRICT'),nullable=False),
        sa.Column('stock_date',sa.Date(),nullable=False),sa.Column('status',sa.String(20),nullable=False),
        sa.Column('snapshot_json',sa.Text(),nullable=False),
        sa.Column('output_lot_id',sa.Integer(),sa.ForeignKey('inventory_lots.id',ondelete='RESTRICT')),
        sa.Column('version',sa.Integer(),nullable=False),
        sa.Column('actor_id',sa.Integer(),sa.ForeignKey('users.id',ondelete='RESTRICT'),nullable=False),
        sa.Column('created_at',sa.DateTime(),server_default=sa.func.current_timestamp(),nullable=False),
        sa.CheckConstraint("status IN ('pending','completed','cancelled')",name='ck_scrap_production_status'),
        sa.CheckConstraint('quantity>0 AND version>=1',name='ck_scrap_production_quantity'))
    op.create_index('ix_scrap_production_jobs_customer_id','scrap_production_jobs',['customer_id'])
    op.create_index('ix_scrap_production_jobs_order_item_id','scrap_production_jobs',['order_item_id'])


def downgrade():
    for table in ('customer_sharing_groups', 'customer_sharing_members','scrap_production_jobs'):
        if op.get_bind().execute(sa.text('SELECT 1 FROM '+table+' LIMIT 1')).first():
            raise RuntimeError('已有客户共用设置，禁止有损降级；保留现库向前修复')
    if op.get_bind().execute(sa.text('SELECT 1 FROM products WHERE production_variants IS NOT NULL LIMIT 1')).first():
        raise RuntimeError('已有多模具方案，禁止有损降级；保留现库向前修复')
    op.drop_table('scrap_production_jobs')
    op.drop_table('customer_sharing_members')
    op.drop_table('customer_sharing_groups')
    op.drop_column('products','production_variants')
