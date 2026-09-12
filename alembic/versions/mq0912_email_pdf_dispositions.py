"""Remember terminal email PDF handling without changing sales facts."""
from alembic import op
import sqlalchemy as sa

revision = 'mq0912'
down_revision = 'sprep0912'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('email_pdf_dispositions',
        sa.Column('sha256', sa.String(64), primary_key=True),
        sa.Column('attachment_id', sa.Integer(), sa.ForeignKey('email_intake_attachments.id'), nullable=False),
        sa.Column('action', sa.String(20), nullable=False),
        sa.Column('actor_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('handled_at', sa.DateTime(), nullable=False),
        sa.Column('customer_po', sa.String(200), nullable=True),
        sa.Column('order_number', sa.String(200), nullable=True),
        sa.CheckConstraint("action IN ('processed','deleted','duplicate')", name='ck_email_pdf_disposition_action'))


def downgrade():
    if op.get_bind().execute(sa.text('SELECT count(*) FROM email_pdf_dispositions')).scalar():
        raise RuntimeError('已有邮箱处理记录，禁止有损降级；请使用已验证的发布备份')
    op.drop_table('email_pdf_dispositions')
