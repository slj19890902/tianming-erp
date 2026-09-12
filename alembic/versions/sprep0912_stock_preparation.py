"""Stock preparation plans; existing receipts are projected without backfill."""
from alembic import op
import sqlalchemy as sa
revision = "sprep0912"
down_revision = "rp0912"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("stock_preparation_jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("receipt_item_id", sa.Integer(), sa.ForeignKey("incoming_receipt_items.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("reservation_id", sa.Integer(), sa.ForeignKey("inventory_reservations.id", ondelete="RESTRICT"), nullable=False, unique=True),
        sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("product_snapshot", sa.Text(), nullable=False),
        sa.Column("input_quantity", sa.Integer(), nullable=False),
        sa.Column("expected_output", sa.Integer(), nullable=False),
        sa.Column("actual_output", sa.Integer(), nullable=False),
        sa.Column("output_lot_id", sa.Integer(), sa.ForeignKey("inventory_lots.id", ondelete="RESTRICT")),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
        sa.CheckConstraint("status IN ('pending','completed','cancelled')", name="ck_stock_prep_status"),
        sa.CheckConstraint("input_quantity > 0 AND expected_output > 0 AND actual_output >= 0", name="ck_stock_prep_qty"))
    op.create_index("ix_stock_preparation_jobs_receipt_item_id", "stock_preparation_jobs", ["receipt_item_id"])
    op.create_table("stock_preparation_commands",
        sa.Column("operation_key", sa.String(80), primary_key=True),
        sa.Column("receipt_item_id", sa.Integer(), sa.ForeignKey("incoming_receipt_items.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("request_json", sa.Text(), nullable=False),
        sa.Column("result_json", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()))
    op.create_index("ix_stock_preparation_commands_receipt_item_id", "stock_preparation_commands", ["receipt_item_id"])
    for action in ("UPDATE", "DELETE"):
        op.execute(f"CREATE TRIGGER stock_preparation_commands_no_{action.lower()} BEFORE {action} ON stock_preparation_commands BEGIN SELECT RAISE(ABORT, '备库安排流水不可修改或删除'); END")


def downgrade():
    for table in ("stock_preparation_commands", "stock_preparation_jobs"):
        if op.get_bind().execute(sa.text(f"SELECT count(*) FROM {table}")).scalar():
            raise RuntimeError("已有备库生产安排事实，禁止降级删除；请使用验证备份恢复")
    op.drop_table("stock_preparation_commands")
    op.drop_table("stock_preparation_jobs")
