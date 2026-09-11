"""Link external procurement to real BOM identities; no historical backfill."""
from alembic import op
import sqlalchemy as sa

revision = 'ry11v8x9z73'
down_revision = 'rx11v8x9z72'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('order_bom_external_components',
        sa.Column('external_component_id', sa.Integer(), sa.ForeignKey('sales_order_item_external_components.id', ondelete='RESTRICT'), primary_key=True),
        sa.Column('order_item_id', sa.Integer(), nullable=False),
        sa.Column('product_id', sa.Integer(), nullable=False),
        sa.Column('bom_snapshot_id', sa.Integer(), sa.ForeignKey('sales_order_item_bom_components.id', ondelete='RESTRICT'), nullable=False, unique=True),
        sa.ForeignKeyConstraint(['order_item_id', 'product_id'], ['order_bom_graph_products.order_item_id', 'order_bom_graph_products.product_id'], ondelete='RESTRICT', name='fk_bom_external_frozen_product'),
        sa.UniqueConstraint('order_item_id', 'product_id', name='uq_bom_external_product'))


def downgrade():
    if op.get_bind().execute(sa.text('SELECT 1 FROM order_bom_external_components LIMIT 1')).first():
        raise RuntimeError('已有真实BOM外购关联，禁止删除；请使用已验证备份回退')
    op.drop_table('order_bom_external_components')
