"""Persist sender allowlists without changing existing received mail."""
from alembic import op
import sqlalchemy as sa

revision = 'ss31v8x9z93'
down_revision = 'sr30v8x9z92'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('email_intake_settings', sa.Column('sender_addresses_json', sa.Text(), nullable=True))


def downgrade():
    if op.get_bind().execute(sa.text('SELECT count(*) FROM email_intake_settings WHERE sender_addresses_json IS NOT NULL')).scalar():
        raise RuntimeError('已有发件人筛选配置，禁止降级删除；请使用已验证备份恢复')
    with op.batch_alter_table('email_intake_settings') as batch:
        batch.drop_column('sender_addresses_json')
