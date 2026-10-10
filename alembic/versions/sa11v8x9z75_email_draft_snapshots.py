"""Immutable mail spreadsheet interpretation snapshots."""
from alembic import op
import sqlalchemy as sa
revision = 'sa11v8x9z75'
down_revision = 'rz10v8x9z74'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('email_intake_drafts',
        sa.Column('id',sa.Integer(),primary_key=True),
        sa.Column('parse_key',sa.String(64),nullable=False,unique=True),
        sa.Column('attachment_id',sa.Integer(),sa.ForeignKey('email_intake_attachments.id'),nullable=False),
        sa.Column('customer_id',sa.Integer(),sa.ForeignKey('customers.id'),nullable=False),
        sa.Column('actor_id',sa.Integer(),sa.ForeignKey('users.id'),nullable=False),
        sa.Column('config_json',sa.Text(),nullable=False),
        sa.Column('draft_json',sa.Text(),nullable=False),
        sa.Column('created_at',sa.DateTime(),server_default=sa.func.now(),nullable=False))


def downgrade():
    if op.get_bind().execute(sa.text('SELECT COUNT(*) FROM email_intake_drafts')).scalar():
        raise RuntimeError('已有邮件识别快照，拒绝破坏性降级')
    op.drop_table('email_intake_drafts')
