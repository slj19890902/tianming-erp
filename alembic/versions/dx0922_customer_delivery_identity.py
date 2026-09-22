"""Customer document identity and immutable order/delivery snapshots; no backfill."""
from alembic import op
import sqlalchemy as sa

revision='dx0922'
down_revision='dw0922'
branch_labels=None
depends_on=None

FIELDS=(('customer_drawing_number',150),('customer_category',100),('customer_model',500),
        ('customer_product_name',500),('customer_drawing_display',250))


def upgrade():
    for name,size in FIELDS:
        op.add_column('products',sa.Column(name,sa.String(size),nullable=True))
    for table in ('sales_order_items','sales_delivery_items'):
        op.add_column(table,sa.Column('customer_document_snapshot_json',sa.Text(),nullable=True))


def downgrade():
    db=op.get_bind()
    if db.execute(sa.text("SELECT 1 FROM delivery_print_template_revisions WHERE catalog_version='delivery-print-v2' LIMIT 1")).first():
        raise RuntimeError('已有客户专用模板版本，禁止有损降级；保留数据库向前修复')
    for table,names in [('products',[name for name,_ in FIELDS]),
                        ('sales_order_items',['customer_document_snapshot_json']),
                        ('sales_delivery_items',['customer_document_snapshot_json'])]:
        if db.execute(sa.text('SELECT 1 FROM '+table+' WHERE '+
                             ' OR '.join(name+' IS NOT NULL' for name in names)+' LIMIT 1')).first():
            raise RuntimeError('已有客户送货资料或冻结快照，禁止有损降级；保留数据库向前修复')
    # Native DROP COLUMN preserves unrelated table constraints, indexes and triggers.
    for table in ('sales_delivery_items','sales_order_items'):
        op.execute(sa.text('ALTER TABLE '+table+' DROP COLUMN customer_document_snapshot_json'))
    for name,_ in reversed(FIELDS):
        op.execute(sa.text('ALTER TABLE products DROP COLUMN '+name))
