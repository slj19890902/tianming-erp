"""Preserve source identities when stock/BOM demand shares a supplier purchase."""
from alembic import op
import sqlalchemy as sa

revision = "up0919"
down_revision = "sc0916"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("stock_replenishment_orders", sa.Column("request_hash", sa.String(64), nullable=True))
    op.create_table("procurement_source_links",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("supplier_item_id", sa.Integer(), sa.ForeignKey("supplier_requisition_order_items.id", ondelete="RESTRICT"), nullable=False, unique=True),
        sa.Column("stock_replenishment_item_id", sa.Integer(), sa.ForeignKey("stock_replenishment_order_items.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("material_requisition_item_id", sa.Integer(), sa.ForeignKey("material_requisition_items.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("source_quantity", sa.Integer(), nullable=False),
        sa.Column("source_snapshot_json", sa.Text(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
        sa.CheckConstraint("(stock_replenishment_item_id IS NOT NULL AND material_requisition_item_id IS NULL) OR (stock_replenishment_item_id IS NULL AND material_requisition_item_id IS NOT NULL)", name="ck_procurement_source_kind"),
        sa.CheckConstraint("status IN ('active','voided')", name="ck_procurement_source_status"),
        sa.CheckConstraint("source_quantity > 0", name="ck_procurement_source_quantity"),
    )
    for label, column in [("stock", "stock_replenishment_item_id"), ("material", "material_requisition_item_id")]:
        op.create_index("uq_procurement_active_" + label, "procurement_source_links", [column], unique=True,
            sqlite_where=sa.text("status = 'active'"), postgresql_where=sa.text("status = 'active'"))
    if op.get_bind().dialect.name == "sqlite":
        op.execute("CREATE TRIGGER procurement_source_identity_immutable BEFORE UPDATE OF supplier_item_id, stock_replenishment_item_id, material_requisition_item_id, source_quantity, source_snapshot_json, created_by ON procurement_source_links BEGIN SELECT RAISE(ABORT,'purchase source identity is immutable'); END")
        op.execute("CREATE TRIGGER procurement_source_no_delete BEFORE DELETE ON procurement_source_links BEGIN SELECT RAISE(ABORT,'purchase source history cannot be deleted'); END")


def downgrade():
    db = op.get_bind()
    if db.execute(sa.text("SELECT 1 FROM procurement_source_links LIMIT 1")).first() or db.execute(sa.text("SELECT 1 FROM stock_replenishment_orders WHERE request_hash IS NOT NULL LIMIT 1")).first():
        raise RuntimeError("已有统一采购来源或补库请求事实，禁止删除；请使用已验证备份恢复")
    if db.dialect.name == "sqlite":
        op.execute("DROP TRIGGER procurement_source_identity_immutable")
        op.execute("DROP TRIGGER procurement_source_no_delete")
    op.drop_table("procurement_source_links")
    op.drop_column("stock_replenishment_orders", "request_hash")
