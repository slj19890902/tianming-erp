"""Independent replenishment material production; no historical backfill."""
from alembic import op
import sqlalchemy as sa
revision='fb1024rs'
down_revision='fa1023oc'
branch_labels=None
depends_on=None


def upgrade():
    bind=op.get_bind()
    if bind.dialect.name=='sqlite' and not bind.connection.driver_connection.in_transaction:
        bind.exec_driver_sql('BEGIN IMMEDIATE')
    op.create_table('stock_replenishment_production_jobs',
        sa.Column('id',sa.Integer(),primary_key=True),
        sa.Column('stock_replenishment_item_id',sa.Integer(),sa.ForeignKey('stock_replenishment_order_items.id',ondelete='RESTRICT'),nullable=False),
        sa.Column('reservation_id',sa.Integer(),sa.ForeignKey('inventory_reservations.id',ondelete='RESTRICT'),nullable=False,unique=True),
        sa.Column('product_id',sa.Integer(),sa.ForeignKey('products.id',ondelete='RESTRICT'),nullable=False),
        sa.Column('snapshot_json',sa.Text(),nullable=False),
        sa.Column('input_quantity',sa.Integer(),nullable=False),
        sa.Column('expected_output',sa.Integer(),nullable=False),
        sa.Column('actual_output',sa.Integer(),nullable=False),
        sa.Column('output_lot_id',sa.Integer(),sa.ForeignKey('inventory_lots.id',ondelete='RESTRICT'),nullable=True),
        sa.Column('status',sa.String(20),nullable=False),
        sa.Column('version',sa.Integer(),nullable=False),
        sa.Column('actor_id',sa.Integer(),sa.ForeignKey('users.id',ondelete='RESTRICT'),nullable=False),
        sa.Column('created_at',sa.DateTime(),server_default=sa.text('CURRENT_TIMESTAMP'),nullable=False),
        sa.CheckConstraint("status IN ('pending','completed','cancelled')",name='ck_replenishment_production_status'),
        sa.CheckConstraint('input_quantity > 0 AND expected_output > 0 AND actual_output >= 0 AND version > 0',name='ck_replenishment_production_quantity'))
    op.create_index('ix_stock_replenishment_production_jobs_stock_replenishment_item_id','stock_replenishment_production_jobs',['stock_replenishment_item_id'])
    # SQLite supports this nullable reference without rebuilding the old ledger.
    op.execute('ALTER TABLE scrap_production_jobs ADD COLUMN stock_replenishment_item_id INTEGER REFERENCES stock_replenishment_order_items(id) ON DELETE RESTRICT')
    op.create_index('ix_scrap_production_jobs_stock_replenishment_item_id','scrap_production_jobs',['stock_replenishment_item_id'])
    op.execute("CREATE TRIGGER scrap_replenishment_source_insert BEFORE INSERT ON scrap_production_jobs WHEN NEW.stock_replenishment_item_id IS NOT NULL AND NEW.order_item_id IS NOT NULL BEGIN SELECT RAISE(ABORT,'scrap source must be exclusive'); END")
    op.execute("CREATE TRIGGER scrap_replenishment_source_update BEFORE UPDATE OF stock_replenishment_item_id,order_item_id ON scrap_production_jobs WHEN NEW.stock_replenishment_item_id IS NOT OLD.stock_replenishment_item_id OR (NEW.stock_replenishment_item_id IS NOT NULL AND NEW.order_item_id IS NOT NULL) BEGIN SELECT RAISE(ABORT,'scrap replenishment source is immutable'); END")
    op.execute("CREATE TRIGGER replenishment_production_frozen BEFORE UPDATE OF stock_replenishment_item_id,reservation_id,product_id,snapshot_json,input_quantity,expected_output ON stock_replenishment_production_jobs BEGIN SELECT RAISE(ABORT,'replenishment production identity is immutable'); END")
    op.execute("CREATE TRIGGER replenishment_production_no_delete BEFORE DELETE ON stock_replenishment_production_jobs BEGIN SELECT RAISE(ABORT,'replenishment production history must be retained'); END")


def downgrade():
    bind=op.get_bind()
    if bind.execute(sa.text('SELECT 1 FROM stock_replenishment_production_jobs LIMIT 1')).first() or bind.execute(sa.text('SELECT 1 FROM scrap_production_jobs WHERE stock_replenishment_item_id IS NOT NULL LIMIT 1')).first():
        raise RuntimeError('Replenishment production facts exist; destructive downgrade refused')
    if bind.dialect.name=='sqlite' and not bind.connection.driver_connection.in_transaction:
        bind.exec_driver_sql('BEGIN IMMEDIATE')
    op.execute('DROP TRIGGER replenishment_production_no_delete')
    op.execute('DROP TRIGGER replenishment_production_frozen')
    op.execute('DROP TRIGGER scrap_replenishment_source_update')
    op.execute('DROP TRIGGER scrap_replenishment_source_insert')
    op.drop_index('ix_scrap_production_jobs_stock_replenishment_item_id',table_name='scrap_production_jobs')
    op.execute('ALTER TABLE scrap_production_jobs DROP COLUMN stock_replenishment_item_id')
    op.drop_table('stock_replenishment_production_jobs')
