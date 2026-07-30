"""add queue-only requisition holds

Revision ID: db84v8x9z73
Revises: da83v8x9z72
Create Date: 2026-07-30

Q1-04 is linearly attached after the Q1-03 unordered finished-goods delivery
revision.  A hold only changes the pending-requisition queue; it does not
create a supplier order, incoming receipt, production, delivery, or inventory
fact.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "db84v8x9z73"
down_revision: Union[str, Sequence[str], None] = "da83v8x9z72"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


DOWNGRADE_BLOCKED_MESSAGE = (
    "存在报料暂缓/等候记录，禁止破坏性降级；"
    "请保留当前数据库并恢复迁移前完整备份。"
)


def upgrade() -> None:
    op.create_table(
        "requisition_holds",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("order_item_id", sa.Integer(), nullable=True),
        sa.Column("order_item_id_snapshot", sa.Integer(), nullable=False),
        sa.Column("customer_id_snapshot", sa.Integer(), nullable=True),
        sa.Column("customer_name_snapshot", sa.String(length=200), nullable=False),
        sa.Column("order_number_snapshot", sa.String(length=64), nullable=False),
        sa.Column("order_item_sequence_snapshot", sa.Integer(), nullable=True),
        sa.Column("product_code_snapshot", sa.String(length=150), nullable=True),
        sa.Column("product_name_snapshot", sa.String(length=250), nullable=False),
        sa.Column("specification_snapshot", sa.String(length=150), nullable=True),
        sa.Column("quantity_snapshot", sa.Integer(), nullable=False),
        sa.Column("release_mode", sa.String(length=40), nullable=False),
        sa.Column("previous_order_item_id", sa.Integer(), nullable=True),
        sa.Column("previous_order_item_id_snapshot", sa.Integer(), nullable=True),
        sa.Column("expected_requisition_date", sa.Date(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="active"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("updated_by", sa.Integer(), nullable=True),
        sa.Column("released_by", sa.Integer(), nullable=True),
        sa.Column("released_at", sa.DateTime(), nullable=True),
        sa.Column("release_source", sa.String(length=40), nullable=True),
        sa.Column("release_note", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "release_mode IN ('previous_batch_completed', 'expected_date')",
            name="ck_requisition_holds_release_mode",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'released', 'invalidated')",
            name="ck_requisition_holds_status",
        ),
        sa.CheckConstraint(
            "status <> 'active' OR ((release_mode = 'previous_batch_completed' "
            "AND previous_order_item_id_snapshot IS NOT NULL "
            "AND expected_requisition_date IS NULL) "
            "OR (release_mode = 'expected_date' "
            "AND previous_order_item_id_snapshot IS NULL "
            "AND expected_requisition_date IS NOT NULL))",
            name="ck_requisition_holds_one_release_condition",
        ),
        sa.CheckConstraint(
            "previous_order_item_id IS NULL "
            "OR previous_order_item_id <> order_item_id",
            name="ck_requisition_holds_previous_not_self",
        ),
        sa.CheckConstraint("version >= 1", name="ck_requisition_holds_version"),
        sa.ForeignKeyConstraint(
            ["order_item_id"], ["sales_order_items.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["previous_order_item_id"],
            ["sales_order_items.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["released_by"], ["users.id"], ondelete="SET NULL"),
    )
    op.create_index(
        "uq_requisition_holds_active_order_item",
        "requisition_holds",
        ["order_item_id"],
        unique=True,
        sqlite_where=sa.text("status = 'active'"),
    )
    op.create_index(
        "ix_requisition_holds_previous_order_item",
        "requisition_holds",
        ["previous_order_item_id"],
    )
    op.create_index(
        "ix_requisition_holds_active_expected_date",
        "requisition_holds",
        ["status", "expected_requisition_date"],
    )
    op.execute(
        """
        CREATE TRIGGER trg_requisition_holds_invalidate_deleted_order_item
        AFTER UPDATE OF order_item_id ON requisition_holds
        WHEN OLD.order_item_id IS NOT NULL
          AND NEW.order_item_id IS NULL
          AND NEW.status = 'active'
        BEGIN
            UPDATE requisition_holds
            SET status = 'invalidated',
                release_source = 'order_item_deleted',
                released_at = COALESCE(released_at, CURRENT_TIMESTAMP),
                version = version + 1,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = NEW.id;
        END
        """
    )


def downgrade() -> None:
    connection = op.get_bind()
    has_facts = connection.execute(
        sa.text("SELECT 1 FROM requisition_holds LIMIT 1")
    ).first()
    if has_facts is not None:
        raise RuntimeError(DOWNGRADE_BLOCKED_MESSAGE)
    op.execute("DROP TRIGGER IF EXISTS trg_requisition_holds_invalidate_deleted_order_item")
    op.drop_index(
        "ix_requisition_holds_active_expected_date",
        table_name="requisition_holds",
    )
    op.drop_index(
        "ix_requisition_holds_previous_order_item",
        table_name="requisition_holds",
    )
    op.drop_index(
        "uq_requisition_holds_active_order_item",
        table_name="requisition_holds",
    )
    op.drop_table("requisition_holds")
