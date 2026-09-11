"""Mail intake staging; no existing order or inventory facts changed."""
from alembic import op
import sqlalchemy as sa

revision = 'ry10v8x9z73'
down_revision = 'rx10v8x9z72'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('email_intake_settings',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('encrypted_secret', sa.Text(), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False))
    op.create_table('email_intake_messages',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('mailbox_key', sa.String(200), nullable=False),
        sa.Column('uid_validity', sa.String(40), nullable=False),
        sa.Column('uid', sa.Integer(), nullable=False),
        sa.Column('message_id', sa.String(1000), nullable=False),
        sa.Column('subject', sa.String(1000), nullable=False),
        sa.Column('sender', sa.String(1000), nullable=False),
        sa.Column('received', sa.String(100), nullable=False),
        sa.Column('body', sa.Text(), nullable=False),
        sa.Column('status', sa.String(30), nullable=False),
        sa.Column('notice', sa.Text(), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.UniqueConstraint('mailbox_key', 'uid_validity', 'uid', name='uq_email_intake_uid'))
    op.create_table('email_intake_attachments',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('message_id', sa.Integer(), sa.ForeignKey('email_intake_messages.id'), nullable=False),
        sa.Column('part_number', sa.Integer(), nullable=False),
        sa.Column('filename', sa.String(240), nullable=False),
        sa.Column('sha256', sa.String(64), nullable=False),
        sa.Column('content', sa.LargeBinary(), nullable=False),
        sa.Column('duplicate_of', sa.Integer(), sa.ForeignKey('email_intake_attachments.id'), nullable=True),
        sa.UniqueConstraint('message_id', 'part_number', name='uq_email_intake_part'))
    op.create_index('ix_email_intake_attachments_sha256', 'email_intake_attachments', ['sha256'])


def downgrade():
    connection = op.get_bind()
    for table in ('email_intake_attachments', 'email_intake_messages', 'email_intake_settings'):
        if connection.execute(sa.text('SELECT COUNT(*) FROM ' + table)).scalar():
            raise RuntimeError('邮箱收单已有设置或邮件事实，禁止破坏性降级；请使用已验证备份恢复')
    op.drop_index('ix_email_intake_attachments_sha256', table_name='email_intake_attachments')
    for table in ('email_intake_attachments', 'email_intake_messages', 'email_intake_settings'):
        op.drop_table(table)
