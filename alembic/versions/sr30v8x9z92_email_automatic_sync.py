"""Email intake automatic polling settings; no order facts are created."""
from alembic import op
import sqlalchemy as sa

revision = 'sr30v8x9z92'
down_revision = 'sq29v8x9z91'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('email_intake_settings') as batch:
        batch.add_column(sa.Column('automatic_enabled', sa.Boolean(), nullable=False, server_default=sa.false()))
        batch.add_column(sa.Column('sync_interval_minutes', sa.Integer(), nullable=False, server_default='5'))
        batch.add_column(sa.Column('last_sync_started_at', sa.DateTime(), nullable=True))
        batch.add_column(sa.Column('last_sync_completed_at', sa.DateTime(), nullable=True))
        batch.add_column(sa.Column('last_sync_status', sa.String(30), nullable=False, server_default='never'))
        batch.add_column(sa.Column('last_sync_received', sa.Integer(), nullable=False, server_default='0'))
        batch.add_column(sa.Column('last_sync_remaining', sa.Integer(), nullable=False, server_default='0'))
        batch.add_column(sa.Column('last_sync_error', sa.String(500), nullable=True))
        batch.create_check_constraint('ck_email_intake_sync_interval', 'sync_interval_minutes BETWEEN 1 AND 60')
        batch.create_check_constraint('ck_email_intake_sync_status', "last_sync_status IN ('never','running','success','failed')")
        batch.create_check_constraint('ck_email_intake_sync_counts', 'last_sync_received >= 0 AND last_sync_remaining >= 0')


def downgrade():
    connection = op.get_bind()
    active = connection.execute(sa.text(
        "SELECT COUNT(*) FROM email_intake_settings WHERE automatic_enabled = 1 "
        "OR last_sync_status <> 'never' OR last_sync_started_at IS NOT NULL "
        "OR last_sync_completed_at IS NOT NULL OR last_sync_received <> 0 "
        "OR last_sync_remaining <> 0 OR last_sync_error IS NOT NULL"
    )).scalar()
    if active:
        raise RuntimeError('邮箱自动读取已有配置或运行事实，禁止降级；请使用已验证备份恢复')
    with op.batch_alter_table('email_intake_settings') as batch:
        batch.drop_constraint('ck_email_intake_sync_counts', type_='check')
        batch.drop_constraint('ck_email_intake_sync_status', type_='check')
        batch.drop_constraint('ck_email_intake_sync_interval', type_='check')
        for name in ('last_sync_error', 'last_sync_remaining', 'last_sync_received',
                     'last_sync_status', 'last_sync_completed_at', 'last_sync_started_at',
                     'sync_interval_minutes', 'automatic_enabled'):
            batch.drop_column(name)
