"""Freeze raw procurement specs separately from final manufacturing demand."""
from alembic import op
import sqlalchemy as sa

revision='rp0919'
down_revision='up0919'
branch_labels=None
depends_on=None


def upgrade():
    op.create_table('raw_purchase_plans',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('stock_item_id', sa.Integer(), nullable=False),
    sa.Column('supplier_order_id', sa.Integer(), nullable=False),
    sa.Column('customer_id', sa.Integer(), nullable=False),
    sa.Column('operation_key', sa.String(length=64), nullable=False),
    sa.Column('request_hash', sa.String(length=64), nullable=False),
    sa.Column('snapshot_json', sa.Text(), nullable=False),
    sa.Column('status', sa.String(length=20), server_default='active', nullable=False),
    sa.Column('created_by', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
    sa.CheckConstraint("status IN ('active','voided')", name='ck_raw_plan_status'),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['customer_id'], ['customers.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['stock_item_id'], ['stock_replenishment_order_items.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['supplier_order_id'], ['supplier_requisition_orders.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('operation_key'),
    sa.UniqueConstraint('stock_item_id')
    )
    op.create_table('raw_purchase_demands',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('plan_id', sa.Integer(), nullable=False),
    sa.Column('order_item_id', sa.Integer(), nullable=False),
    sa.Column('product_id', sa.Integer(), nullable=False),
    sa.Column('requirement_id', sa.Integer(), nullable=False),
    sa.Column('source_key', sa.String(length=100), nullable=False),
    sa.Column('snapshot_json', sa.Text(), nullable=False),
    sa.Column('raw_quantity', sa.Integer(), nullable=False),
    sa.Column('piece_quantity', sa.Integer(), nullable=False),
    sa.Column('yield_factor', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(length=20), server_default='active', nullable=False),
    sa.CheckConstraint("status IN ('active','voided')", name='ck_raw_demand_status'),
    sa.CheckConstraint('raw_quantity > 0 AND piece_quantity > 0 AND yield_factor > 0 AND piece_quantity = raw_quantity * yield_factor', name='ck_raw_demand_quantity'),
    sa.ForeignKeyConstraint(['order_item_id'], ['sales_order_items.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['plan_id'], ['raw_purchase_plans.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['product_id'], ['products.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['requirement_id'], ['order_item_semi_requirements.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_raw_demand_order', 'raw_purchase_demands', ['order_item_id', 'status'], unique=False)
    op.create_index('uq_raw_active_demand', 'raw_purchase_demands', ['source_key'], unique=True, sqlite_where=sa.text("status = 'active'"), postgresql_where=sa.text("status = 'active'"))
    op.create_table('raw_purchase_receipt_allocations',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('demand_id', sa.Integer(), nullable=False),
    sa.Column('receipt_item_id', sa.Integer(), nullable=False),
    sa.Column('reservation_id', sa.Integer(), nullable=False),
    sa.Column('raw_quantity', sa.Integer(), nullable=False),
    sa.Column('piece_quantity', sa.Integer(), nullable=False),
    sa.Column('raw_offset', sa.Integer(), nullable=False),
    sa.Column('total_cost', sa.Numeric(precision=20, scale=4), nullable=False),
    sa.CheckConstraint('raw_quantity > 0 AND piece_quantity > 0 AND raw_offset >= 0 AND total_cost >= 0', name='ck_raw_receipt_quantity'),
    sa.ForeignKeyConstraint(['demand_id'], ['raw_purchase_demands.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['receipt_item_id'], ['incoming_receipt_items.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['reservation_id'], ['inventory_reservations.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('reservation_id')
    )
    op.create_index('uq_raw_receipt_demand', 'raw_purchase_receipt_allocations', ['receipt_item_id', 'demand_id'], unique=True)
    op.create_table('raw_purchase_delivery_cost_portions',
        sa.Column('id',sa.Integer(),primary_key=True),
        sa.Column('fact_id',sa.Integer(),sa.ForeignKey('finance_delivery_graph_cost_facts.id',ondelete='RESTRICT'),nullable=False),
        sa.Column('ordinal',sa.Integer(),nullable=False),
        sa.Column('raw_receipt_allocation_id',sa.Integer(),sa.ForeignKey('raw_purchase_receipt_allocations.id',ondelete='RESTRICT'),nullable=False),
        sa.Column('full_output_cost',sa.Numeric(20,6),nullable=False),
        sa.Column('charged_cost',sa.Numeric(20,6),nullable=False),
        sa.Column('tax_included',sa.Boolean(),nullable=False),
        sa.Column('tax_rate',sa.Numeric(8,6),nullable=False),
        sa.UniqueConstraint('fact_id','ordinal',name='uq_raw_delivery_portion'),
        sa.CheckConstraint('ordinal >= 0 AND full_output_cost >= 0 AND charged_cost >= 0 AND tax_rate >= 0 AND tax_rate <= 1',name='ck_raw_delivery_cost'))
    op.create_index('ix_raw_purchase_delivery_cost_portions_fact_id','raw_purchase_delivery_cost_portions',['fact_id'])
    if op.get_bind().dialect.name == "sqlite":
        op.execute("CREATE TRIGGER raw_purchase_plans_immutable BEFORE UPDATE OF id, stock_item_id, supplier_order_id, customer_id, operation_key, request_hash, snapshot_json, created_by, created_at ON raw_purchase_plans BEGIN SELECT RAISE(ABORT,'raw purchase source is immutable'); END")
        op.execute("CREATE TRIGGER raw_purchase_plans_no_delete BEFORE DELETE ON raw_purchase_plans BEGIN SELECT RAISE(ABORT,'raw purchase history cannot be deleted'); END")
        op.execute("CREATE TRIGGER raw_purchase_demands_immutable BEFORE UPDATE OF id, plan_id, order_item_id, product_id, requirement_id, source_key, snapshot_json, raw_quantity, piece_quantity, yield_factor ON raw_purchase_demands BEGIN SELECT RAISE(ABORT,'raw purchase source is immutable'); END")
        op.execute("CREATE TRIGGER raw_purchase_demands_no_delete BEFORE DELETE ON raw_purchase_demands BEGIN SELECT RAISE(ABORT,'raw purchase history cannot be deleted'); END")
        op.execute("CREATE TRIGGER raw_purchase_receipt_allocations_immutable BEFORE UPDATE OF id, demand_id, receipt_item_id, reservation_id, raw_quantity, piece_quantity, raw_offset, total_cost ON raw_purchase_receipt_allocations BEGIN SELECT RAISE(ABORT,'raw purchase source is immutable'); END")
        op.execute("CREATE TRIGGER raw_purchase_receipt_allocations_no_delete BEFORE DELETE ON raw_purchase_receipt_allocations BEGIN SELECT RAISE(ABORT,'raw purchase history cannot be deleted'); END")
        op.execute("CREATE TRIGGER raw_purchase_delivery_cost_portions_immutable BEFORE UPDATE ON raw_purchase_delivery_cost_portions BEGIN SELECT RAISE(ABORT,'raw purchase source is immutable'); END")
        op.execute("CREATE TRIGGER raw_purchase_delivery_cost_portions_no_delete BEFORE DELETE ON raw_purchase_delivery_cost_portions BEGIN SELECT RAISE(ABORT,'raw purchase history cannot be deleted'); END")


def downgrade():
    db=op.get_bind()
    if db.execute(sa.text("SELECT 1 FROM raw_purchase_plans LIMIT 1")).first():
        raise RuntimeError("已有原片采购或分配事实，禁止删除历史；请使用已验证恢复备份")
    if db.execute(sa.text("SELECT 1 FROM raw_purchase_demands LIMIT 1")).first():
        raise RuntimeError("已有原片采购或分配事实，禁止删除历史；请使用已验证恢复备份")
    if db.execute(sa.text("SELECT 1 FROM raw_purchase_receipt_allocations LIMIT 1")).first():
        raise RuntimeError("已有原片采购或分配事实，禁止删除历史；请使用已验证恢复备份")
    if db.execute(sa.text("SELECT 1 FROM raw_purchase_delivery_cost_portions LIMIT 1")).first():
        raise RuntimeError("已有原片发货成本事实，禁止删除历史")
    if db.dialect.name == "sqlite":
        op.execute("DROP TRIGGER raw_purchase_plans_immutable")
        op.execute("DROP TRIGGER raw_purchase_plans_no_delete")
        op.execute("DROP TRIGGER raw_purchase_demands_immutable")
        op.execute("DROP TRIGGER raw_purchase_demands_no_delete")
        op.execute("DROP TRIGGER raw_purchase_receipt_allocations_immutable")
        op.execute("DROP TRIGGER raw_purchase_receipt_allocations_no_delete")
        op.execute("DROP TRIGGER raw_purchase_delivery_cost_portions_immutable")
        op.execute("DROP TRIGGER raw_purchase_delivery_cost_portions_no_delete")
    op.drop_table('raw_purchase_delivery_cost_portions')
    op.drop_table("raw_purchase_receipt_allocations")
    op.drop_table("raw_purchase_demands")
    op.drop_table("raw_purchase_plans")
