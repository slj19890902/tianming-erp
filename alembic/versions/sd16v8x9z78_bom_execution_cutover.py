"""Explicit legacy/current BOM boundary. No orders or stock are converted."""
from alembic import op
import sqlalchemy as sa

revision = "sd16v8x9z78"
down_revision = "sc15v8x9z77"
branch_labels = None
depends_on = None


def upgrade():
    # A new index, not a table rebuild: every original snapshot/FK stays intact.
    op.create_index("uq_bom_snapshot_order_identity", "sales_order_item_bom_components",
                    ["id", "sales_order_item_id"], unique=True)
    op.create_table("order_bom_execution_cutovers",
        sa.Column("order_item_id", sa.Integer(),
            sa.ForeignKey("order_bom_graphs.order_item_id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("order_quantity", sa.Integer(), nullable=False),
        sa.Column("delivered_before", sa.Integer(), nullable=False),
        sa.Column("basis_json", sa.Text(), nullable=False),
        sa.Column("basis_hash", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(120), unique=True, nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False),
        sa.CheckConstraint("order_quantity > 0 AND delivered_before >= 0 AND delivered_before < order_quantity",
            name="ck_bom_cutover_quantities"),
        sa.CheckConstraint("length(basis_hash) = 64 AND length(request_hash) = 64", name="ck_bom_cutover_hashes"),
        sa.CheckConstraint("length(trim(idempotency_key)) > 0", name="ck_bom_cutover_key"))
    op.create_table("order_bom_cutover_sources",
        sa.Column("snapshot_id", sa.Integer(), primary_key=True),
        sa.Column("order_item_id", sa.Integer(),
            sa.ForeignKey("order_bom_execution_cutovers.order_item_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("role", sa.String(20), nullable=False),
        sa.ForeignKeyConstraint(["snapshot_id", "order_item_id"],
            ["sales_order_item_bom_components.id", "sales_order_item_bom_components.sales_order_item_id"],
            ondelete="RESTRICT", name="fk_bom_cutover_source_order"),
        sa.CheckConstraint("role IN ('history','current')", name="ck_bom_cutover_source_role"))
    op.create_index("ix_order_bom_cutover_sources_order_item_id", "order_bom_cutover_sources", ["order_item_id"])


def downgrade():
    db = op.get_bind()
    if (db.execute(sa.text("SELECT 1 FROM order_bom_execution_cutovers LIMIT 1")).first()
            or db.execute(sa.text("SELECT 1 FROM order_bom_cutover_sources LIMIT 1")).first()):
        raise RuntimeError("已有BOM执行转换记录，禁止降级；请使用已验证备份回退")
    op.drop_table("order_bom_cutover_sources")
    op.drop_table("order_bom_execution_cutovers")
    op.drop_index("uq_bom_snapshot_order_identity", table_name="sales_order_item_bom_components")
