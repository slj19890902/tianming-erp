"""Remaining-only legacy accompanying contracts; no historical backfill."""
from alembic import op
import sqlalchemy as sa

revision = 'la0915'
down_revision = 'mr0914'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('legacy_accompany_contracts',
        sa.Column('order_item_id', sa.Integer(), sa.ForeignKey('sales_order_items.id', ondelete='RESTRICT'), primary_key=True),
        sa.Column('document_json', sa.Text(), nullable=False),
        sa.Column('content_hash', sa.String(64), nullable=False),
        sa.Column('created_by', sa.Integer(), sa.ForeignKey('users.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False),
        sa.CheckConstraint('length(content_hash) = 64', name='ck_legacy_accompany_hash'))
    if op.get_bind().dialect.name == 'sqlite':
        for action in ('UPDATE', 'DELETE'):
            op.execute(f"CREATE TRIGGER legacy_accompany_no_{action.lower()} BEFORE {action} ON legacy_accompany_contracts BEGIN SELECT RAISE(ABORT, 'legacy accompanying contract is immutable'); END")


def downgrade():
    if op.get_bind().execute(sa.text('SELECT 1 FROM legacy_accompany_contracts LIMIT 1')).first():
        raise RuntimeError('已有旧单随货执行事实，禁止降级删除；请使用验证过的备份恢复')
    op.drop_table('legacy_accompany_contracts')
