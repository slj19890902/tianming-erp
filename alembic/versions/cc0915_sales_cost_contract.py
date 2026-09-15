"""Sales unit and delivery valuation contract; no historical fact inference."""
from alembic import op
import sqlalchemy as sa
revision='cc0915'
down_revision='la0915'
branch_labels=None
depends_on=None

def upgrade():
    op.add_column('sales_order_items',sa.Column('sales_unit_snapshot',sa.String(20),nullable=True))
    op.add_column('sales_delivery_items',sa.Column('sales_contract_json',sa.Text(),nullable=True))
    if op.get_bind().dialect.name == 'sqlite':
        op.execute("CREATE TRIGGER delivery_sales_contract_immutable BEFORE UPDATE OF sales_contract_json ON sales_delivery_items WHEN OLD.sales_contract_json IS NOT NULL AND NEW.sales_contract_json IS NOT OLD.sales_contract_json BEGIN SELECT RAISE(ABORT,'frozen sales contract is immutable'); END")

def downgrade():
    bind=op.get_bind()
    if bind.execute(sa.text('SELECT 1 FROM sales_order_items WHERE sales_unit_snapshot IS NOT NULL LIMIT 1')).first() or bind.execute(sa.text('SELECT 1 FROM sales_delivery_items WHERE sales_contract_json IS NOT NULL LIMIT 1')).first():
        raise RuntimeError('已有销售快照，禁止删除事实降级；请用已验证备份恢复')
    if bind.dialect.name == 'sqlite':
        op.execute('DROP TRIGGER delivery_sales_contract_immutable')
    op.drop_column('sales_delivery_items','sales_contract_json')
    op.drop_column('sales_order_items','sales_unit_snapshot')
