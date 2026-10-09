"""Explicit finished-stock interchangeability without moving quantity facts.

Revision ID: ei1009ss
Revises: eh1009qr
"""
from alembic import op
import sqlalchemy as sa

revision = "ei1009ss"
down_revision = "eh1009qr"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("shared_finished_groups",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("operation_key", sa.String(100), nullable=False, unique=True),
        sa.Column("request_json", sa.Text(), nullable=False),
        sa.Column("evidence", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("actor_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="RESTRICT")),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()))
    op.create_table("shared_finished_members",
        sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("group_id", sa.Integer(), sa.ForeignKey("shared_finished_groups.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("customer_id", sa.Integer(), sa.ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("identity_json", sa.Text(), nullable=False),
        sa.Column("product_basis_json", sa.Text(), nullable=False))
    op.create_index("ix_shared_finished_members_group_id", "shared_finished_members", ["group_id"])
    op.create_index("ix_shared_finished_members_customer_id", "shared_finished_members", ["customer_id"])
    op.create_table("shared_finished_lots",
        sa.Column("lot_id", sa.Integer(), sa.ForeignKey("inventory_lots.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("group_id", sa.Integer(), sa.ForeignKey("shared_finished_groups.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("identity_json", sa.Text(), nullable=False),
        sa.Column("source_lot_id", sa.Integer(), sa.ForeignKey("inventory_lots.id", ondelete="RESTRICT")))
    op.create_index("ix_shared_finished_lots_group_id", "shared_finished_lots", ["group_id"])
    op.create_table("shared_finished_reservations",
        sa.Column("reservation_id", sa.Integer(), sa.ForeignKey("inventory_reservations.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("group_id", sa.Integer(), sa.ForeignKey("shared_finished_groups.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("customer_id", sa.Integer(), sa.ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("lot_identity_json", sa.Text(), nullable=False),
        sa.Column("product_basis_json", sa.Text(), nullable=False))


def downgrade():
    names = ("shared_finished_reservations", "shared_finished_lots", "shared_finished_members", "shared_finished_groups")
    for name in names:
        if op.get_bind().execute(sa.text(f'SELECT 1 FROM "{name}" LIMIT 1')).first():
            raise RuntimeError("已有成品共用事实，禁止有损降级；请保留数据库并前向修复")
    for name in names:
        op.drop_table(name)
