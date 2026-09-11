"""Trace mail attachments to normal orders with duplicate protection."""
from alembic import op
import sqlalchemy as sa
revision = 'rz10v8x9z74'
down_revision = 'ry10v8x9z73'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('email_intake_order_links',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('import_key', sa.String(64), nullable=False, unique=True),
        sa.Column('payload_hash', sa.String(64), nullable=False),
        sa.Column('attachment_id', sa.Integer(), sa.ForeignKey('email_intake_attachments.id'), nullable=False),
        sa.Column('order_id', sa.Integer(), sa.ForeignKey('sales_orders.id', ondelete='SET NULL'), nullable=True),
        sa.Column('actor_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False))


def downgrade():
    if op.get_bind().execute(sa.text('SELECT COUNT(*) FROM email_intake_order_links')).scalar():
        raise RuntimeError('邮件已有正式订单来源记录，拒绝破坏性降级')
    op.drop_table('email_intake_order_links')
