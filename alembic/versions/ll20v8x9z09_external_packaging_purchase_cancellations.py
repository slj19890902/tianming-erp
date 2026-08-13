"""append-only external-packaging purchase cancellation facts

Revision ID: ll20v8x9z09
Revises: kk19v8x9z08
Create Date: 2026-08-13
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "ll20v8x9z09"
down_revision = "kk19v8x9z08"
branch_labels = None
depends_on = None


TABLE = "external_packaging_purchase_cancellations"


def _create_immutable_guards() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        for action in ("UPDATE", "DELETE"):
            op.execute(
                f"""
                CREATE TRIGGER trg_{TABLE}_immutable_{action.lower()}
                BEFORE {action} ON {TABLE}
                FOR EACH ROW
                BEGIN
                    SELECT RAISE(ABORT, '{TABLE} rows are immutable');
                END
                """
            )
    elif dialect == "postgresql":
        op.execute(
            """
            CREATE OR REPLACE FUNCTION p1_54_immutable_external_purchase_cancellation()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                RAISE EXCEPTION 'external packaging purchase cancellation facts are immutable';
            END;
            $$
            """
        )
        op.execute(
            f"""
            CREATE TRIGGER trg_{TABLE}_immutable_write
            BEFORE UPDATE OR DELETE ON {TABLE}
            FOR EACH ROW EXECUTE FUNCTION p1_54_immutable_external_purchase_cancellation()
            """
        )


def _drop_immutable_guards() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        for action in ("delete", "update"):
            op.execute(f"DROP TRIGGER IF EXISTS trg_{TABLE}_immutable_{action}")
    elif dialect == "postgresql":
        op.execute(f"DROP TRIGGER IF EXISTS trg_{TABLE}_immutable_write ON {TABLE}")
        op.execute(
            "DROP FUNCTION IF EXISTS p1_54_immutable_external_purchase_cancellation()"
        )


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("purchase_order_id", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(40), nullable=False),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column("cancelled_by", sa.Integer(), nullable=True),
        sa.Column(
            "cancelled_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.ForeignKeyConstraint(
            ["purchase_order_id"],
            ["external_packaging_purchase_orders.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["cancelled_by"], ["users.id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint(
            "purchase_order_id",
            name="uq_external_packaging_purchase_cancellation_order",
        ),
        sa.CheckConstraint(
            "source IN ('order_workflow_rollback','order_status_cancelled','order_status_dead','authorized_data_repair')",
            name="ck_external_packaging_purchase_cancellation_source",
        ),
    )
    op.create_index(
        "ix_external_packaging_purchase_cancellations_purchase_order_id",
        TABLE,
        ["purchase_order_id"],
    )
    _create_immutable_guards()


def downgrade() -> None:
    count = int(
        op.get_bind().execute(
            sa.text(f"SELECT COUNT(*) FROM {TABLE}")
        ).scalar_one()
        or 0
    )
    if count:
        raise RuntimeError(
            "P1-54 已存在正式外购包材采购作废事实，拒绝破坏性降级；请恢复升级前完整备份。"
        )
    _drop_immutable_guards()
    op.drop_index(
        "ix_external_packaging_purchase_cancellations_purchase_order_id",
        table_name=TABLE,
    )
    op.drop_table(TABLE)
