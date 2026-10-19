"""Add independent finished inventory label jobs without rewriting facts."""
from alembic import op
import sqlalchemy as sa

revision = 'es1019fl'
down_revision = 'er1016pa'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('finished_product_label_print_jobs',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('inventory_lot_id', sa.Integer(), sa.ForeignKey('inventory_lots.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('idempotency_key', sa.String(120), unique=True, nullable=False),
        sa.Column('request_hash', sa.String(64), nullable=False),
        sa.Column('plan_fingerprint', sa.String(64), nullable=False),
        sa.Column('payload_json', sa.Text(), nullable=False),
        sa.Column('payload_hash', sa.String(64), nullable=False),
        sa.Column('operator_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('status', sa.String(20), server_default='prepared', nullable=False),
        sa.Column('printed_confirmation_key', sa.String(120), unique=True),
        sa.Column('printed_by_user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='RESTRICT')),
        sa.Column('printed_at', sa.DateTime()),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False),
        sa.CheckConstraint("status IN ('prepared','printed')", name='ck_finished_label_status'),
        sa.CheckConstraint("length(request_hash)=64 AND length(plan_fingerprint)=64 AND length(payload_hash)=64", name='ck_finished_label_hashes'),
        sa.CheckConstraint("(status='prepared' AND printed_at IS NULL AND printed_confirmation_key IS NULL AND printed_by_user_id IS NULL) OR (status='printed' AND printed_at IS NOT NULL AND printed_confirmation_key IS NOT NULL AND printed_by_user_id IS NOT NULL)", name='ck_finished_label_lifecycle'))
    op.create_index('ix_finished_label_created', 'finished_product_label_print_jobs', ['created_at'])
    if op.get_bind().dialect.name == 'sqlite':
        op.execute("CREATE TRIGGER finished_label_no_delete BEFORE DELETE ON finished_product_label_print_jobs BEGIN SELECT RAISE(ABORT,'成品标签记录不可删除'); END")
        op.execute("""CREATE TRIGGER finished_label_frozen BEFORE UPDATE ON finished_product_label_print_jobs
        WHEN OLD.status='printed' OR NEW.id IS NOT OLD.id OR NEW.inventory_lot_id IS NOT OLD.inventory_lot_id
        OR NEW.idempotency_key IS NOT OLD.idempotency_key OR NEW.request_hash IS NOT OLD.request_hash
        OR NEW.plan_fingerprint IS NOT OLD.plan_fingerprint OR NEW.payload_json IS NOT OLD.payload_json
        OR NEW.payload_hash IS NOT OLD.payload_hash OR NEW.operator_id IS NOT OLD.operator_id OR NEW.created_at IS NOT OLD.created_at
        OR NEW.status!='printed'
        BEGIN SELECT RAISE(ABORT,'成品标签冻结内容不可改写'); END""")


def downgrade():
    if op.get_bind().execute(sa.text('SELECT count(*) FROM finished_product_label_print_jobs')).scalar_one():
        raise RuntimeError('已有成品标签打印作业，禁止有损降级')
    if op.get_bind().dialect.name == 'sqlite':
        op.execute('DROP TRIGGER finished_label_frozen')
        op.execute('DROP TRIGGER finished_label_no_delete')
    op.drop_index('ix_finished_label_created', table_name='finished_product_label_print_jobs')
    op.drop_table('finished_product_label_print_jobs')
