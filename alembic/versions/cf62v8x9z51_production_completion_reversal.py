"""add audited production completion reversal

Revision ID: cf62v8x9z51
Revises: ce61v8x9z50
Create Date: 2026-07-21
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "cf62v8x9z51"
down_revision: Union[str, Sequence[str], None] = "ce61v8x9z50"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _create_reversed_guards() -> None:
    dialect = op.get_bind().dialect.name
    tables = ("production_completions", "production_stock_transfers")
    if dialect == "sqlite":
        for table in tables:
            op.execute(
                f"""
                CREATE TRIGGER trg_{table}_reversed_no_update
                BEFORE UPDATE ON {table}
                FOR EACH ROW WHEN OLD.status = 'reversed'
                BEGIN
                    SELECT RAISE(ABORT, 'reversed production audit rows are immutable');
                END
                """
            )
            op.execute(
                f"""
                CREATE TRIGGER trg_{table}_reversed_no_delete
                BEFORE DELETE ON {table}
                FOR EACH ROW WHEN OLD.status = 'reversed'
                BEGIN
                    SELECT RAISE(ABORT, 'reversed production audit rows cannot be deleted');
                END
                """
            )
        return
    if dialect == "postgresql":
        op.execute(
            """
            CREATE FUNCTION protect_reversed_production_audit() RETURNS trigger AS $$
            BEGIN
                IF OLD.status = 'reversed' THEN
                    RAISE EXCEPTION 'reversed production audit rows are immutable';
                END IF;
                RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
            END;
            $$ LANGUAGE plpgsql
            """
        )
        for table in tables:
            op.execute(
                f"CREATE TRIGGER trg_{table}_reversed_no_update "
                f"BEFORE UPDATE ON {table} FOR EACH ROW "
                "EXECUTE FUNCTION protect_reversed_production_audit()"
            )
            op.execute(
                f"CREATE TRIGGER trg_{table}_reversed_no_delete "
                f"BEFORE DELETE ON {table} FOR EACH ROW "
                "EXECUTE FUNCTION protect_reversed_production_audit()"
            )


def _drop_reversed_guards() -> None:
    dialect = op.get_bind().dialect.name
    for table in ("production_completions", "production_stock_transfers"):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_reversed_no_update")
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_reversed_no_delete")
    if dialect == "postgresql":
        op.execute("DROP FUNCTION IF EXISTS protect_reversed_production_audit()")


def upgrade() -> None:
    with op.batch_alter_table("production_completions") as batch:
        batch.add_column(
            sa.Column("status", sa.String(length=20), server_default="posted", nullable=False)
        )
        batch.add_column(sa.Column("reversed_by", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("reversed_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("reversal_reason", sa.Text(), nullable=True))
        batch.create_foreign_key(
            "fk_production_completions_reversed_by_users",
            "users",
            ["reversed_by"],
            ["id"],
            ondelete="SET NULL",
        )
        batch.create_check_constraint(
            "ck_production_completions_status",
            "status IN ('posted','reversed')",
        )
    op.drop_index("uq_production_completions_task", table_name="production_completions")
    op.create_index(
        "uq_production_completions_task_active",
        "production_completions",
        ["task_id"],
        unique=True,
        sqlite_where=sa.text("status = 'posted'"),
        postgresql_where=sa.text("status = 'posted'"),
    )

    with op.batch_alter_table("production_stock_transfers") as batch:
        batch.add_column(
            sa.Column("status", sa.String(length=20), server_default="posted", nullable=False)
        )
        batch.add_column(sa.Column("reversed_by", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("reversed_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("reversal_reason", sa.Text(), nullable=True))
        batch.create_foreign_key(
            "fk_production_stock_transfers_reversed_by_users",
            "users",
            ["reversed_by"],
            ["id"],
            ondelete="SET NULL",
        )
        batch.create_check_constraint(
            "ck_production_stock_transfers_status",
            "status IN ('posted','reversed')",
        )
    _create_reversed_guards()


def downgrade() -> None:
    connection = op.get_bind()
    reversed_count = int(
        connection.execute(
            sa.text(
                "SELECT (SELECT COUNT(*) FROM production_completions WHERE status='reversed') + "
                "(SELECT COUNT(*) FROM production_stock_transfers WHERE status='reversed')"
            )
        ).scalar_one()
    )
    if reversed_count:
        raise RuntimeError(
            "已存在生产完工或转库存撤销审计，禁止破坏性降级；请恢复升级前完整备份。"
        )
    _drop_reversed_guards()
    with op.batch_alter_table("production_stock_transfers") as batch:
        batch.drop_constraint("ck_production_stock_transfers_status", type_="check")
        batch.drop_constraint("fk_production_stock_transfers_reversed_by_users", type_="foreignkey")
        for column in ("reversal_reason", "reversed_at", "reversed_by", "status"):
            batch.drop_column(column)
    op.drop_index("uq_production_completions_task_active", table_name="production_completions")
    op.create_index(
        "uq_production_completions_task",
        "production_completions",
        ["task_id"],
        unique=True,
    )
    with op.batch_alter_table("production_completions") as batch:
        batch.drop_constraint("ck_production_completions_status", type_="check")
        batch.drop_constraint("fk_production_completions_reversed_by_users", type_="foreignkey")
        for column in ("reversal_reason", "reversed_at", "reversed_by", "status"):
            batch.drop_column(column)
