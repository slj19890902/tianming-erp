"""append-only mold label print jobs

Revision ID: mm21v8x9z10
Revises: ll20v8x9z09
Create Date: 2026-08-13
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "mm21v8x9z10"
down_revision = "ll20v8x9z09"
branch_labels = None
depends_on = None


JOBS = "mold_label_print_jobs"
ITEMS = "mold_label_print_job_items"


def _create_immutable_guards() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        for table in (JOBS, ITEMS):
            for action in ("UPDATE", "DELETE"):
                op.execute(
                    f"""
                    CREATE TRIGGER trg_{table}_immutable_{action.lower()}
                    BEFORE {action} ON {table}
                    FOR EACH ROW
                    BEGIN
                        SELECT RAISE(ABORT, '{table} rows are immutable');
                    END
                    """
                )
    elif dialect == "postgresql":
        op.execute(
            """
            CREATE OR REPLACE FUNCTION p1_58_immutable_mold_label_print_fact()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                RAISE EXCEPTION 'mold label print facts are immutable';
            END;
            $$
            """
        )
        for table in (JOBS, ITEMS):
            op.execute(
                f"""
                CREATE TRIGGER trg_{table}_immutable_write
                BEFORE UPDATE OR DELETE ON {table}
                FOR EACH ROW EXECUTE FUNCTION p1_58_immutable_mold_label_print_fact()
                """
            )


def _drop_immutable_guards() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        for table in (ITEMS, JOBS):
            for action in ("delete", "update"):
                op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_immutable_{action}")
    elif dialect == "postgresql":
        for table in (ITEMS, JOBS):
            op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_immutable_write ON {table}")
        op.execute("DROP FUNCTION IF EXISTS p1_58_immutable_mold_label_print_fact()")


def upgrade() -> None:
    op.create_table(
        JOBS,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("idempotency_key", sa.String(120), nullable=False),
        sa.Column("source", sa.String(20), nullable=False),
        sa.Column("item_count", sa.Integer(), nullable=False),
        sa.Column("printed_by", sa.Integer(), nullable=False),
        sa.Column("printed_by_username", sa.String(100), nullable=False),
        sa.Column(
            "printed_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.ForeignKeyConstraint(["printed_by"], ["users.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_mold_label_print_jobs_idempotency_key",
        ),
        sa.CheckConstraint(
            "source IN ('single','batch')",
            name="ck_mold_label_print_jobs_source",
        ),
        sa.CheckConstraint(
            "item_count >= 1 AND item_count <= 100",
            name="ck_mold_label_print_jobs_item_count",
        ),
    )
    op.create_index(
        "ix_mold_label_print_jobs_printed_at",
        JOBS,
        ["printed_at"],
    )
    op.create_table(
        ITEMS,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("print_job_id", sa.Integer(), nullable=False),
        sa.Column("mold_tool_id", sa.Integer(), nullable=False),
        sa.Column("item_order", sa.Integer(), nullable=False),
        sa.Column("mold_code_snapshot", sa.String(100), nullable=False),
        sa.Column("rack_location_snapshot", sa.String(250), nullable=False),
        sa.ForeignKeyConstraint(["print_job_id"], [f"{JOBS}.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["mold_tool_id"], ["mold_tools.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint(
            "print_job_id",
            "mold_tool_id",
            name="uq_mold_label_print_job_items_job_mold",
        ),
        sa.UniqueConstraint(
            "print_job_id",
            "item_order",
            name="uq_mold_label_print_job_items_job_order",
        ),
        sa.CheckConstraint(
            "item_order >= 1 AND item_order <= 100",
            name="ck_mold_label_print_job_items_order",
        ),
    )
    op.create_index(
        "ix_mold_label_print_job_items_mold_job",
        ITEMS,
        ["mold_tool_id", "print_job_id"],
    )
    _create_immutable_guards()


def downgrade() -> None:
    count = int(op.get_bind().execute(sa.text(f"SELECT COUNT(*) FROM {JOBS}")).scalar_one() or 0)
    if count:
        raise RuntimeError(
            "P1-58 已存在正式模具标签打印事实，拒绝破坏性降级；请恢复升级前完整备份。"
        )
    _drop_immutable_guards()
    op.drop_index("ix_mold_label_print_job_items_mold_job", table_name=ITEMS)
    op.drop_table(ITEMS)
    op.drop_index("ix_mold_label_print_jobs_printed_at", table_name=JOBS)
    op.drop_table(JOBS)
