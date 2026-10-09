"""Approved cross-customer BOM roles; no historical business backfill."""
from alembic import op
import sqlalchemy as sa

revision='em1009bs'
down_revision='el1009cp'
branch_labels=depends_on=None


def upgrade():
    op.create_table('shared_bom_members',
        sa.Column('product_id',sa.Integer(),sa.ForeignKey('shared_finished_members.product_id',ondelete='RESTRICT'),primary_key=True),
        sa.Column('role',sa.String(20),nullable=False),
        sa.Column('contract_json',sa.Text(),nullable=False))
    for action in ('UPDATE','DELETE'):
        op.execute(f"CREATE TRIGGER shared_bom_members_no_{action.lower()} BEFORE {action} ON shared_bom_members "
                   "BEGIN SELECT RAISE(ABORT, 'shared BOM proof is immutable'); END")


def downgrade():
    if op.get_bind().execute(sa.text('SELECT 1 FROM shared_bom_members LIMIT 1')).first():
        raise RuntimeError('已有BOM共用事实，禁止有损降级；保留现库向前修复')
    for action in ('update','delete'):
        op.execute(f'DROP TRIGGER shared_bom_members_no_{action}')
    op.drop_table('shared_bom_members')
