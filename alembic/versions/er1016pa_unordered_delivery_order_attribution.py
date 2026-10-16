"""Record an order fulfilled by an existing unordered dispatched delivery."""

from alembic import op
import sqlalchemy as sa

revision = "er1016pa"
down_revision = "eq1015sa"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "unordered_delivery_order_attributions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("delivery_item_id", sa.Integer(), sa.ForeignKey("sales_delivery_items.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("order_item_id", sa.Integer(), sa.ForeignKey("sales_order_items.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("actor_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("operation_key", sa.String(120), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False),
        sa.CheckConstraint("quantity > 0", name="ck_unordered_delivery_order_attribution_quantity"),
        sa.UniqueConstraint("delivery_item_id", name="uq_unordered_delivery_order_attribution_line"),
        sa.UniqueConstraint("operation_key", name="uq_unordered_delivery_order_attribution_operation"),
    )
    op.create_index(
        "ix_unordered_delivery_order_attributions_order_item_id",
        "unordered_delivery_order_attributions",
        ["order_item_id"],
    )
    if op.get_bind().dialect.name == "sqlite":
        for action in ("UPDATE", "DELETE"):
            op.execute(
                f"CREATE TRIGGER unordered_delivery_order_attributions_no_{action.lower()} "
                f"BEFORE {action} ON unordered_delivery_order_attributions "
                "BEGIN SELECT RAISE(ABORT, '已核实订单送货归属不可直接改写'); END"
            )


def downgrade():
    count = op.get_bind().execute(
        sa.text("SELECT count(*) FROM unordered_delivery_order_attributions")
    ).scalar_one()
    if count:
        raise RuntimeError("已有正式送货归属更正，不能有损降级")
    if op.get_bind().dialect.name == "sqlite":
        for action in ("UPDATE", "DELETE"):
            op.execute(
                f"DROP TRIGGER unordered_delivery_order_attributions_no_{action.lower()}"
            )
    op.drop_index(
        "ix_unordered_delivery_order_attributions_order_item_id",
        table_name="unordered_delivery_order_attributions",
    )
    op.drop_table("unordered_delivery_order_attributions")
