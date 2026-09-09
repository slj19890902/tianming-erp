"""Multi-level conversion provenance; no stock or historical backfill."""
from alembic import op
import sqlalchemy as sa

revision = "rw09v8x9z71"
down_revision = "rv09v8x9z70"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("bom_assemblies",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("order_item_id", sa.Integer(), sa.ForeignKey("order_bom_graphs.order_item_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("output_product_id", sa.Integer(), sa.ForeignKey("products.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("idempotency_key", sa.String(120), unique=True, nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("output_lot_id", sa.Integer(), sa.ForeignKey("inventory_lots.id", ondelete="RESTRICT")),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("total_cost", sa.Numeric(18, 4), nullable=False),
        sa.Column("cost_detail_json", sa.Text(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False),
        sa.ForeignKeyConstraint(["order_item_id", "output_product_id"],
            ["order_bom_graph_products.order_item_id", "order_bom_graph_products.product_id"],
            ondelete="RESTRICT", name="fk_bom_assembly_frozen_product"),
        sa.CheckConstraint("quantity >= 0 AND total_cost >= 0", name="ck_bom_assembly_positive"),
        sa.CheckConstraint("status IN ('posted','reversed')", name="ck_bom_assembly_status"))
    op.create_index("ix_bom_assemblies_order_item_id", "bom_assemblies", ["order_item_id"])
    op.create_index("ix_bom_assemblies_output_product_id", "bom_assemblies", ["output_product_id"])
    op.create_table("bom_assembly_inputs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("conversion_id", sa.Integer(), sa.ForeignKey("bom_assemblies.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("lot_id", sa.Integer(), sa.ForeignKey("inventory_lots.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("total_cost", sa.Numeric(18, 4), nullable=False),
        sa.Column("consume_movement_id", sa.Integer(), sa.ForeignKey("inventory_movements.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("reservations_json", sa.Text(), nullable=False),
        sa.UniqueConstraint("conversion_id", "lot_id", name="uq_bom_assembly_input"),
        sa.CheckConstraint("quantity > 0 AND total_cost >= 0", name="ck_bom_assembly_input_positive"))
    op.create_index("ix_bom_assembly_inputs_conversion_id", "bom_assembly_inputs", ["conversion_id"])
    op.create_index("ix_bom_assembly_inputs_lot_id", "bom_assembly_inputs", ["lot_id"])


def downgrade():
    for table in ("bom_assembly_inputs", "bom_assemblies"):
        if op.get_bind().execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first():
            raise RuntimeError("已有多级组装流水，禁止删除；使用已验证发布前备份回退")
    op.drop_table("bom_assembly_inputs")
    op.drop_table("bom_assemblies")
