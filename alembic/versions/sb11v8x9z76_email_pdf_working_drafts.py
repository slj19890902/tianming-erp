"""Recover an operator's PDF edits without creating an order."""
from alembic import op
import sqlalchemy as sa
revision = 'sb11v8x9z76'
down_revision = 'rz10v8x9z74'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('email_pdf_working_drafts',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('attachment_id', sa.Integer(), sa.ForeignKey('email_intake_attachments.id'), nullable=False),
        sa.Column('actor_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('content_json', sa.Text(), nullable=False),
        sa.Column('content_hash', sa.String(64), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('saved_at', sa.String(40), nullable=False),
        sa.UniqueConstraint('attachment_id', 'actor_id', name='uq_email_pdf_actor_draft'))


def downgrade():
    if op.get_bind().execute(sa.text('SELECT COUNT(*) FROM email_pdf_working_drafts')).scalar():
        raise RuntimeError('已有已保存PDF核对草稿，拒绝破坏性降级')
    op.drop_table('email_pdf_working_drafts')
