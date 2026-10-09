"""Explicit shared stock inbound policies and append-only management receipts."""
from alembic import op
import sqlalchemy as sa

revision = "ej1009sm"
down_revision = "ei1009ss"
branch_labels = depends_on = None


def upgrade():
    op.create_table("shared_finished_policies",
        sa.Column("group_id", sa.Integer(), sa.ForeignKey("shared_finished_groups.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("auto_enroll", sa.Boolean(), nullable=False))
    op.create_table("shared_finished_mutations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("operation_key", sa.String(100), nullable=False, unique=True),
        sa.Column("group_id", sa.Integer(), sa.ForeignKey("shared_finished_groups.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("request_json", sa.Text(), nullable=False),
        sa.Column("result_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False))
    op.create_index("ix_shared_finished_mutations_group_id", "shared_finished_mutations", ["group_id"])
    op.create_table("shared_finished_order_bases",
        sa.Column("order_item_id", sa.Integer(), sa.ForeignKey("sales_order_items.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("group_id", sa.Integer(), sa.ForeignKey("shared_finished_groups.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("group_version", sa.Integer(), nullable=False),
        sa.Column("member_identity_json", sa.Text(), nullable=False),
        sa.Column("product_basis_json", sa.Text(), nullable=False),
        sa.Column("order_identity_json", sa.Text(), nullable=False))
    for action in ("UPDATE", "DELETE"):
        op.execute(f"""CREATE TRIGGER shared_finished_mutations_no_{action.lower()}
            BEFORE {action} ON shared_finished_mutations BEGIN
            SELECT RAISE(ABORT, 'shared finished management receipts are immutable'); END""")


def downgrade():
    for name in ("shared_finished_mutations", "shared_finished_policies", "shared_finished_order_bases"):
        if op.get_bind().execute(sa.text(f'SELECT 1 FROM "{name}" LIMIT 1')).first():
            raise RuntimeError("已有共用维护或自动入组事实，禁止有损降级；请保留数据库并向前修复")
    for action in ("update", "delete"):
        op.execute(f"DROP TRIGGER shared_finished_mutations_no_{action}")
    op.drop_table("shared_finished_mutations")
    op.drop_table("shared_finished_policies")
    op.drop_table("shared_finished_order_bases")
