"""Quotation versions and durable commands; preserve all existing business fields."""
from alembic import op
import sqlalchemy as sa

revision = "eh1009qr"
down_revision = "eg1008sc"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("quotation_orders", sa.Column("version", sa.Integer(), nullable=False, server_default="1"))
    op.create_table(
        "quotation_mutations",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("actor_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("customer_id", sa.Integer(), sa.ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("quotation_id", sa.Integer(), sa.ForeignKey("quotation_orders.id", ondelete="RESTRICT")),
        sa.Column("action", sa.String(24), nullable=False),
        sa.Column("idempotency_key", sa.String(120), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("response_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
        sa.UniqueConstraint("actor_id", "idempotency_key", name="uq_quotation_mutation_actor_key"),
    )


def downgrade():
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT 1 FROM quotation_mutations LIMIT 1")).first() or bind.execute(
        sa.text("SELECT 1 FROM quotation_orders WHERE version <> 1 LIMIT 1")
    ).first():
        raise RuntimeError("已有报价提交或版本事实，禁止有损降级；请使用兼容新结构的前向修复")
    op.drop_table("quotation_mutations")
    op.drop_column("quotation_orders", "version")
