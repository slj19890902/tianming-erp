"""Add lossless decimal companions, without rewriting historical sale facts."""
from alembic import op
import sqlalchemy as sa

revision = 'ew1020em'
down_revision = 'ev1020hr'
branch_labels = None
depends_on = None

COLUMNS = {
    'sales_orders': ('total_amount_exact',),
    'sales_order_items': ('unit_price_exact', 'subtotal_exact'),
    'finance_statements': ('total_receivable_exact',),
    'finance_statement_items': ('unit_price_snapshot_exact', 'receivable_amount_exact'),
}


def upgrade():
    for table, columns in COLUMNS.items():
        for name in columns:
            op.add_column(table, sa.Column(name, sa.Text(), nullable=True))


def downgrade():
    for table, columns in COLUMNS.items():
        if op.get_bind().execute(sa.text('SELECT 1 FROM ' + table + ' WHERE ' +
                ' OR '.join(name + ' IS NOT NULL' for name in columns) + ' LIMIT 1')).first():
            raise RuntimeError('已有精确销售金额事实，禁止丢失金额降级；保留当前数据库向前修复')
    for table, columns in reversed(list(COLUMNS.items())):
        for name in reversed(columns):
            op.drop_column(table, name)
