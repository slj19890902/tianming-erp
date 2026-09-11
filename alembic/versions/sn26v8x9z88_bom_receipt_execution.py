"""Keep original procurement and new receipt execution as separate identities."""
from alembic import op
import sqlalchemy as sa

revision = "sn26v8x9z88"
down_revision = "sm25v8x9z87"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("order_bom_external_receipt_executions",
        sa.Column("receipt_item_id", sa.Integer(), sa.ForeignKey("external_packaging_receipt_items.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("revision_id", sa.Integer(), nullable=False),
        sa.Column("source_snapshot_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["revision_id", "source_snapshot_id"],
            ["order_bom_source_handoffs.revision_id", "order_bom_source_handoffs.source_snapshot_id"],
            ondelete="RESTRICT", name="fk_bom_receipt_execution_handoff"))
    for operation in ("UPDATE", "DELETE"):
        op.execute(sa.text(f"CREATE TRIGGER bom_receipt_execution_no_{operation.lower()} "
            f"BEFORE {operation} ON order_bom_external_receipt_executions BEGIN "
            "SELECT RAISE(ABORT, 'BOM receipt execution facts are immutable'); END"))


def downgrade():
    if op.get_bind().execute(sa.text("SELECT 1 FROM order_bom_external_receipt_executions LIMIT 1")).first():
        raise RuntimeError("已有BOM实收执行来源，禁止删除；请使用经过验证的时点备份恢复")
    op.drop_table("order_bom_external_receipt_executions")
