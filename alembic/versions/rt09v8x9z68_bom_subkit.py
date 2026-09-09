"""Independent sub-kit definitions and conversion lineage. No business backfill."""
from alembic import op
import sqlalchemy as sa

revision = "rt09v8x9z68"
down_revision = "rs08v8x9z67"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("product_subkits",
        sa.Column("parent_product_id", sa.Integer(), sa.ForeignKey("products.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("kit_product_id", sa.Integer(), sa.ForeignKey("products.id", ondelete="RESTRICT"), nullable=False, unique=True),
        sa.Column("kits_per_parent", sa.Integer(), nullable=False),
        sa.Column("recipe_json", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.CheckConstraint("parent_product_id <> kit_product_id", name="ck_subkit_distinct"),
        sa.CheckConstraint("kits_per_parent > 0 AND version > 0", name="ck_subkit_positive"))
    op.create_table("order_subkits",
        sa.Column("order_item_id", sa.Integer(), sa.ForeignKey("sales_order_items.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("kit_product_id", sa.Integer(), sa.ForeignKey("products.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("kits_per_parent", sa.Integer(), nullable=False),
        sa.Column("definition_version", sa.Integer(), nullable=False),
        sa.Column("recipe_json", sa.Text(), nullable=False),
        sa.Column("kit_name_snapshot", sa.String(250), nullable=False),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False),
        sa.CheckConstraint("kits_per_parent > 0 AND definition_version > 0", name="ck_order_subkit_positive"))
    op.create_table("subkit_conversions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("order_item_id", sa.Integer(), sa.ForeignKey("order_subkits.order_item_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("idempotency_key", sa.String(120), nullable=False, unique=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("output_lot_id", sa.Integer(), sa.ForeignKey("inventory_lots.id", ondelete="RESTRICT")),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("total_cost", sa.Numeric(18, 4), nullable=False),
        sa.Column("cost_detail_json", sa.Text(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False),
        sa.CheckConstraint("quantity >= 0 AND total_cost >= 0", name="ck_subkit_conversion_positive"),
        sa.CheckConstraint("status IN ('posted','reversed')", name="ck_subkit_conversion_status"))
    op.create_index("ix_subkit_conversions_order_item_id", "subkit_conversions", ["order_item_id"])
    op.create_table("subkit_conversion_inputs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("conversion_id", sa.Integer(), sa.ForeignKey("subkit_conversions.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("lot_id", sa.Integer(), sa.ForeignKey("inventory_lots.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("total_cost", sa.Numeric(18, 4), nullable=False),
        sa.Column("consume_movement_id", sa.Integer(), sa.ForeignKey("inventory_movements.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("reservations_json", sa.Text(), nullable=False),
        sa.UniqueConstraint("conversion_id", "lot_id", name="uq_subkit_conversion_input"),
        sa.CheckConstraint("quantity > 0 AND total_cost >= 0", name="ck_subkit_input_positive"))
    op.create_index("ix_subkit_conversion_inputs_conversion_id", "subkit_conversion_inputs", ["conversion_id"])
    op.create_index("ix_subkit_conversion_inputs_lot_id", "subkit_conversion_inputs", ["lot_id"])
    op.create_table("subkit_receipt_outputs",
        sa.Column("allocation_id", sa.Integer(), sa.ForeignKey("incoming_receipt_purpose_allocations.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("order_item_id", sa.Integer(), sa.ForeignKey("order_subkits.order_item_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("lot_id", sa.Integer(), sa.ForeignKey("inventory_lots.id", ondelete="RESTRICT"), nullable=False, unique=True),
        sa.Column("quantity", sa.Integer(), nullable=False), sa.Column("total_cost", sa.Numeric(18,4), nullable=False),
        sa.Column("reversed", sa.Boolean(), nullable=False),
        sa.CheckConstraint("quantity > 0 AND total_cost >= 0", name="ck_subkit_receipt_positive"))
    op.create_index("ix_subkit_receipt_outputs_order_item_id", "subkit_receipt_outputs", ["order_item_id"])
    op.create_table("subkit_delivery_allocations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("delivery_item_id", sa.Integer(), sa.ForeignKey("sales_delivery_items.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("lot_id", sa.Integer(), sa.ForeignKey("inventory_lots.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False), sa.Column("total_cost", sa.Numeric(18,4), nullable=False),
        sa.Column("operation_key", sa.String(120), nullable=False),
        sa.Column("cost_detail_json", sa.Text(), nullable=False),
        sa.Column("consume_movement_id", sa.Integer(), sa.ForeignKey("inventory_movements.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("reversed", sa.Boolean(), nullable=False),
        sa.UniqueConstraint("delivery_item_id", "operation_key", "lot_id", name="uq_subkit_delivery_lot_operation"),
        sa.CheckConstraint("quantity > 0 AND total_cost >= 0", name="ck_subkit_delivery_positive"))
    op.create_index("ix_subkit_delivery_allocations_delivery_item_id", "subkit_delivery_allocations", ["delivery_item_id"])


def downgrade():
    tables = ("subkit_delivery_allocations", "subkit_receipt_outputs", "subkit_conversion_inputs", "subkit_conversions", "order_subkits", "product_subkits")
    for table in tables:
        if op.get_bind().execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first():
            raise RuntimeError("子套件已有业务记录，禁止删除；请使用经验证的升级前备份回滚")
    for table in tables:
        op.drop_table(table)
