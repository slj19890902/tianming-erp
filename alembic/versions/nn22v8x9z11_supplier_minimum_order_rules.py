"""add supplier minimum order rules

Revision ID: nn22v8x9z11
Revises: mm21v8x9z10
Create Date: 2026-08-14
"""

from alembic import op
import sqlalchemy as sa


revision = "nn22v8x9z11"
down_revision = "mm21v8x9z10"
branch_labels = None
depends_on = None


TABLE = "supplier_minimum_order_rules"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("supplier_id", sa.Integer(), nullable=False),
        sa.Column("supplier_name_snapshot", sa.String(200), nullable=False),
        sa.Column("scope_type", sa.String(20), nullable=False),
        sa.Column("scope_value", sa.String(200), nullable=True),
        sa.Column("scope_label_snapshot", sa.String(200), nullable=False),
        sa.Column("minimum_quantity", sa.Numeric(18, 4), nullable=False),
        sa.Column("unit", sa.String(30), nullable=False),
        sa.Column("merge_allowed", sa.Boolean(), nullable=False),
        sa.Column("merge_window_days", sa.Integer(), nullable=True),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("source", sa.String(30), nullable=False, server_default="manual_confirmation"),
        sa.Column("evidence_reference", sa.Text(), nullable=False),
        sa.Column("confirmed_by_user_id", sa.Integer(), nullable=True),
        sa.Column("confirmed_by_username_snapshot", sa.String(100), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
        sa.Column("created_by_user_id", sa.Integer(), nullable=True),
        sa.Column("updated_by_user_id", sa.Integer(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["supplier_id"], ["supplier_master_records.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["confirmed_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["updated_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.CheckConstraint("scope_type IN ('supplier','customer','material','flute')", name="ck_supplier_minimum_order_rules_scope_type"),
        sa.CheckConstraint("unit IN ('sheets','square_meters','meters','amount','group_total')", name="ck_supplier_minimum_order_rules_unit"),
        sa.CheckConstraint("status IN ('active','inactive')", name="ck_supplier_minimum_order_rules_status"),
        sa.CheckConstraint("source = 'manual_confirmation'", name="ck_supplier_minimum_order_rules_source"),
        sa.CheckConstraint("minimum_quantity > 0", name="ck_supplier_minimum_order_rules_minimum_quantity"),
        sa.CheckConstraint("(merge_allowed AND merge_window_days BETWEEN 1 AND 365) OR ((NOT merge_allowed) AND merge_window_days IS NULL)", name="ck_supplier_minimum_order_rules_merge_window"),
        sa.CheckConstraint("effective_to IS NULL OR effective_to >= effective_from", name="ck_supplier_minimum_order_rules_effective_dates"),
        sa.CheckConstraint("version >= 1", name="ck_supplier_minimum_order_rules_version"),
    )
    op.create_index("ix_supplier_minimum_order_rules_supplier", TABLE, ["supplier_id"])
    op.create_index("ix_supplier_minimum_order_rules_scope", TABLE, ["scope_type", "scope_value"])
    op.create_index("ix_supplier_minimum_order_rules_status_dates", TABLE, ["status", "effective_from", "effective_to"])


def downgrade() -> None:
    count = int(op.get_bind().execute(sa.text(f"SELECT COUNT(*) FROM {TABLE}")).scalar() or 0)
    if count:
        raise RuntimeError("P1-11D1 已存在供应商 MOQ 规则事实，拒绝破坏性降级；请恢复升级前完整备份。")
    op.drop_index("ix_supplier_minimum_order_rules_status_dates", table_name=TABLE)
    op.drop_index("ix_supplier_minimum_order_rules_scope", table_name=TABLE)
    op.drop_index("ix_supplier_minimum_order_rules_supplier", table_name=TABLE)
    op.drop_table(TABLE)
