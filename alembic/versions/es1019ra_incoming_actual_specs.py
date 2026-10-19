"""Freeze actual receipt material, sheet geometry and cutting without rewriting purchases."""
from alembic import op
import sqlalchemy as sa
revision = 'es1019ra'
down_revision = 'er1016pa'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('incoming_receipt_actual_specs',
        sa.Column('receipt_item_id', sa.Integer(), sa.ForeignKey('incoming_receipt_items.id', ondelete='RESTRICT'), primary_key=True),
        sa.Column('material_id', sa.Integer(), sa.ForeignKey('materials.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('actor_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('request_json', sa.Text(), nullable=False),
        sa.Column('actual_json', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False))
    if op.get_bind().dialect.name == 'sqlite':
        for action in ('UPDATE', 'DELETE'):
            op.execute(f"CREATE TRIGGER incoming_actual_specs_no_{action.lower()} BEFORE {action} ON incoming_receipt_actual_specs BEGIN SELECT RAISE(ABORT, '实际收料合同不可改写，请沿原收料撤销流程处理'); END")


def downgrade():
    if op.get_bind().execute(sa.text('SELECT count(*) FROM incoming_receipt_actual_specs')).scalar_one():
        raise RuntimeError('已有实际收料规格，不能有损降级')
    op.drop_table('incoming_receipt_actual_specs')
