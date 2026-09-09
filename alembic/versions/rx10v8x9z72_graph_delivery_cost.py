"""Multi-source delivery cost evidence. No historical backfill."""
from alembic import op
import sqlalchemy as sa

revision = "rx10v8x9z72"
down_revision = "rw09v8x9z71"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("finance_delivery_graph_cost_facts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("delivery_item_id", sa.Integer(), sa.ForeignKey("sales_delivery_items.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("delivery_inventory_allocation_id", sa.Integer(), sa.ForeignKey("delivery_inventory_allocations.id", ondelete="RESTRICT")),
        sa.Column("unordered_allocation_id", sa.Integer(), sa.ForeignKey("unordered_finished_delivery_allocations.id", ondelete="RESTRICT")),
        sa.Column("inventory_lot_id", sa.Integer(), sa.ForeignKey("inventory_lots.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("consume_movement_id", sa.Integer(), sa.ForeignKey("inventory_movements.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("snapshot_version", sa.Integer(), nullable=False),
        sa.Column("source_quantity", sa.Integer(), nullable=False),
        sa.Column("source_offset", sa.Integer(), nullable=False),
        sa.Column("consumed_quantity", sa.Integer(), nullable=False),
        sa.Column("total_cost", sa.Numeric(20,6), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("source_fingerprint", sa.String(64), nullable=False),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False),
        sa.CheckConstraint("(delivery_inventory_allocation_id IS NOT NULL AND unordered_allocation_id IS NULL) OR (delivery_inventory_allocation_id IS NULL AND unordered_allocation_id IS NOT NULL)", name="ck_graph_cost_source"),
        sa.CheckConstraint("snapshot_version > 0 AND consumed_quantity > 0 AND source_offset >= 0 AND source_quantity >= source_offset + consumed_quantity AND total_cost >= 0", name="ck_graph_cost_quantity"),
        sa.CheckConstraint("length(currency) = 3 AND length(source_fingerprint) = 64", name="ck_graph_cost_text"),
        sa.UniqueConstraint("delivery_inventory_allocation_id", "snapshot_version", name="uq_graph_cost_inventory_version"),
        sa.UniqueConstraint("unordered_allocation_id", "snapshot_version", name="uq_graph_cost_unordered_version"))
    op.create_index("ix_finance_delivery_graph_cost_facts_delivery_item_id", "finance_delivery_graph_cost_facts", ["delivery_item_id"])
    op.create_table("finance_delivery_graph_cost_portions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("fact_id", sa.Integer(), sa.ForeignKey("finance_delivery_graph_cost_facts.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("purchase_receipt_fact_id", sa.Integer(), sa.ForeignKey("purchase_receipt_facts.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("purpose_allocation_id", sa.Integer(), sa.ForeignKey("incoming_receipt_purpose_allocations.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("full_output_cost", sa.Numeric(20,6), nullable=False),
        sa.Column("charged_cost", sa.Numeric(20,6), nullable=False),
        sa.Column("tax_included", sa.Boolean(), nullable=False),
        sa.Column("tax_rate", sa.Numeric(8,6), nullable=False),
        sa.UniqueConstraint("fact_id", "ordinal", name="uq_graph_cost_portion"),
        sa.CheckConstraint("ordinal >= 0 AND full_output_cost >= 0 AND charged_cost >= 0 AND tax_rate >= 0 AND tax_rate <= 1", name="ck_graph_cost_portion_amount"))
    op.create_index("ix_finance_delivery_graph_cost_portions_fact_id", "finance_delivery_graph_cost_portions", ["fact_id"])


def downgrade():
    bind = op.get_bind()
    for table in ("finance_delivery_graph_cost_portions", "finance_delivery_graph_cost_facts"):
        if bind.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first():
            raise RuntimeError("已有多来源发货成本事实，禁止删除；请使用已验证备份回退")
    op.drop_table("finance_delivery_graph_cost_portions")
    op.drop_table("finance_delivery_graph_cost_facts")
