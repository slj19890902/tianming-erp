"""Explicit customer catalog sharing, no historical backfill."""
from alembic import op
import sqlalchemy as sa

revision = 'ex1021cp'
down_revision = 'ew1020em'
branch_labels = None
depends_on = None


def upgrade():
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


def downgrade():
    for table in ('customer_sharing_groups', 'customer_sharing_members'):
        if op.get_bind().execute(sa.text('SELECT 1 FROM '+table+' LIMIT 1')).first():
            raise RuntimeError('已有客户共用设置，禁止有损降级；保留现库向前修复')
    op.drop_table('customer_sharing_members')
    op.drop_table('customer_sharing_groups')
