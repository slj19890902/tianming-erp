"""Fixed shelf configuration and packaging; no layout or inventory backfill."""
from alembic import op
import sqlalchemy as sa

revision = "rs08v8x9z67"
down_revision = "rr08v8x9z67"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("warehouse_shelf_profiles",
        sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("units_per_bundle", sa.Integer(), nullable=True),
        sa.Column("staging_location_id", sa.Integer(), sa.ForeignKey("warehouse_locations.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.CheckConstraint("units_per_bundle IS NULL OR units_per_bundle > 0", name="ck_shelf_profile_bundle"),
        sa.CheckConstraint("version > 0", name="ck_shelf_profile_version"))
    op.create_table("warehouse_shelf_bindings",
        sa.Column("location_id", sa.Integer(), sa.ForeignKey("warehouse_locations.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("product_id", sa.Integer(), sa.ForeignKey("warehouse_shelf_profiles.product_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False), sa.Column("capacity", sa.Integer(), nullable=True),
        sa.UniqueConstraint("product_id", "priority", name="uq_shelf_product_priority"),
        sa.CheckConstraint("priority >= 0", name="ck_shelf_binding_priority"),
        sa.CheckConstraint("capacity IS NULL OR capacity > 0", name="ck_shelf_binding_capacity"))
    op.create_index("ix_warehouse_shelf_bindings_product_id", "warehouse_shelf_bindings", ["product_id"])
    op.create_table("warehouse_shelf_lot_states",
        sa.Column("lot_id", sa.Integer(), sa.ForeignKey("inventory_lots.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("units_per_bundle", sa.Integer(), nullable=True),
        sa.Column("target_location_id", sa.Integer(), sa.ForeignKey("warehouse_locations.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("staged_delivery_item_id", sa.Integer(), sa.ForeignKey("sales_delivery_items.id", ondelete="RESTRICT"), nullable=True),
        sa.CheckConstraint("units_per_bundle IS NULL OR units_per_bundle > 0", name="ck_shelf_lot_bundle"))
    op.create_index("ix_warehouse_shelf_lot_states_target_location_id", "warehouse_shelf_lot_states", ["target_location_id"])
    op.create_index("ix_warehouse_shelf_lot_states_staged_delivery_item_id", "warehouse_shelf_lot_states", ["staged_delivery_item_id"])
    op.create_table("warehouse_shelf_mutations",
        sa.Column("idempotency_key", sa.String(100), primary_key=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("result_json", sa.Text(), nullable=False))


def downgrade():
    tables = ("warehouse_shelf_mutations", "warehouse_shelf_lot_states", "warehouse_shelf_bindings", "warehouse_shelf_profiles")
    bind = op.get_bind()
    for name in tables:
        if bind.execute(sa.text(f"SELECT 1 FROM {name} LIMIT 1")).first():
            raise RuntimeError("Fixed shelf facts exist; restore a verified pre-release backup instead of deleting facts")
    for name in tables:
        op.drop_table(name)
