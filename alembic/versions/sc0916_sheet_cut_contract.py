"""Freeze allocation-specific cutting evidence; no historical inference."""
from alembic import op
import sqlalchemy as sa

revision = 'sc0916'
down_revision = 'cc0915'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('inventory_reservations', sa.Column('cut_plan_json', sa.Text(), nullable=True))
    if op.get_bind().dialect.name == 'sqlite':
        op.execute("CREATE TRIGGER reservation_cut_plan_immutable BEFORE UPDATE OF cut_plan_json, yield_factor ON inventory_reservations WHEN OLD.cut_plan_json IS NOT NULL AND (NEW.cut_plan_json IS NOT OLD.cut_plan_json OR NEW.yield_factor IS NOT OLD.yield_factor) BEGIN SELECT RAISE(ABORT,'frozen cutting contract is immutable'); END")


def downgrade():
    if op.get_bind().execute(sa.text('SELECT 1 FROM inventory_reservations WHERE cut_plan_json IS NOT NULL LIMIT 1')).first():
        raise RuntimeError('已有裁切分配事实，禁止删除；请使用已验证备份恢复')
    if op.get_bind().dialect.name == 'sqlite':
        op.execute('DROP TRIGGER reservation_cut_plan_immutable')
    op.drop_column('inventory_reservations', 'cut_plan_json')
