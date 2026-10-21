"""Separate supplier edge requirements from crease processing, without backfill."""
from alembic import op
import sqlalchemy as sa

revision = 'ey1021se'
down_revision = 'ex1021cp'
branch_labels = None
depends_on = None
COLUMNS = {
    'products': ('supplier_edge_type', 'base_supplier_edge_type'),
    'stock_replenishment_order_items': ('supplier_edge_type',),
    'supplier_requisition_order_items': ('supplier_edge_type',),
}


def upgrade():
    for table, columns in COLUMNS.items():
        for name in columns:
            op.add_column(table, sa.Column(name, sa.String(20), nullable=True))
    for table in ('stock_replenishment_order_items','supplier_requisition_order_items'):
        op.execute(f"CREATE TRIGGER {table}_supplier_edge_immutable BEFORE UPDATE OF supplier_edge_type ON {table} "
            "WHEN OLD.supplier_edge_type IS NOT NEW.supplier_edge_type "
            "BEGIN SELECT RAISE(ABORT, 'supplier edge snapshot is immutable'); END")


def downgrade():
    if op.get_bind().execute(sa.text("SELECT 1 FROM inventory_reservations WHERE "
            "json_valid(cut_plan_json) AND json_extract(cut_plan_json,'$.full_sheet_output_v1')=1 LIMIT 1")).first():
        raise RuntimeError('已有整张裁切产出合同，禁止有损降级；保留数据库向前修复')
    for table, columns in COLUMNS.items():
        if op.get_bind().execute(sa.text('SELECT 1 FROM '+table+' WHERE '+
                ' OR '.join(name+' IS NOT NULL' for name in columns)+' LIMIT 1')).first():
            raise RuntimeError('已有供应商毛净事实，禁止有损降级；保留数据库向前修复')
    for table in ('stock_replenishment_order_items','supplier_requisition_order_items'):
        op.execute(f'DROP TRIGGER {table}_supplier_edge_immutable')
    for table, columns in reversed(list(COLUMNS.items())):
        for name in reversed(columns):
            op.drop_column(table, name)
