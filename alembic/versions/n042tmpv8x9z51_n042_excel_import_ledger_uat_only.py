"""N042 temporary Excel import ledger for isolated UAT only.

Revision ID: n042tmpv8x9z51
Revises: ce61v8x9z50
Create Date: 2026-07-20

DO NOT MERGE THIS REVISION INTO THE MAIN INTEGRATION LINE.
After N041 is integrated, assign a new revision ID and change down_revision to
``df62v8x9z51`` before the feature is considered mergeable.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "n042tmpv8x9z51"
down_revision: Union[str, Sequence[str], None] = "ce61v8x9z50"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


BATCH_TABLE = "excel_order_import_batches"
ROW_TABLE = "excel_order_import_rows"
CONVERSION_TABLE = "excel_order_import_conversions"
LEDGER_TABLES = (BATCH_TABLE, ROW_TABLE, CONVERSION_TABLE)
GUARD_FUNCTION = "n042_immutable_excel_order_import_ledger"


def _create_immutable_guards() -> None:
    connection = op.get_bind()
    if connection.dialect.name == "sqlite":
        for table in LEDGER_TABLES:
            op.execute(
                f"""
                CREATE TRIGGER trg_{table}_no_update
                BEFORE UPDATE ON {table}
                FOR EACH ROW
                BEGIN
                    SELECT RAISE(ABORT, 'N042 Excel import ledger is immutable');
                END
                """
            )
            op.execute(
                f"""
                CREATE TRIGGER trg_{table}_no_delete
                BEFORE DELETE ON {table}
                FOR EACH ROW
                BEGIN
                    SELECT RAISE(ABORT, 'N042 Excel import ledger is immutable');
                END
                """
            )
    elif connection.dialect.name == "postgresql":
        op.execute(
            f"""
            CREATE FUNCTION {GUARD_FUNCTION}()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                RAISE EXCEPTION 'N042 Excel import ledger is immutable';
            END;
            $$
            """
        )
        for table in LEDGER_TABLES:
            for action in ("UPDATE", "DELETE"):
                op.execute(
                    f"""
                    CREATE TRIGGER trg_{table}_no_{action.lower()}
                    BEFORE {action} ON {table}
                    FOR EACH ROW EXECUTE FUNCTION {GUARD_FUNCTION}()
                    """
                )


def _drop_immutable_guards() -> None:
    connection = op.get_bind()
    if connection.dialect.name == "sqlite":
        for table in LEDGER_TABLES:
            op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_no_update")
            op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_no_delete")
    elif connection.dialect.name == "postgresql":
        for table in LEDGER_TABLES:
            op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_no_update ON {table}")
            op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_no_delete ON {table}")
        op.execute(f"DROP FUNCTION IF EXISTS {GUARD_FUNCTION}()")


def _assert_empty_before_downgrade() -> None:
    connection = op.get_bind()
    total_expression = " + ".join(
        f"(SELECT COUNT(*) FROM {table})" for table in LEDGER_TABLES
    )
    count = connection.execute(
        sa.text(f"SELECT {total_expression}")
    ).scalar_one()
    if int(count or 0) > 0:
        raise RuntimeError(
            "N042 Excel 导入台账已经产生不可变事实，禁止破坏性降级；"
            "请停止服务并恢复迁移前已校验的完整数据库备份。"
        )


def upgrade() -> None:
    op.create_table(
        BATCH_TABLE,
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("customer_code_snapshot", sa.String(50), nullable=True),
        sa.Column("customer_name_snapshot", sa.String(200), nullable=False),
        sa.Column("source_type", sa.String(80), nullable=False),
        sa.Column("source_filename", sa.String(255), nullable=False),
        sa.Column("source_sha256", sa.String(64), nullable=False),
        sa.Column("parser_version", sa.String(80), nullable=False),
        sa.Column("worksheet_name", sa.String(255), nullable=False),
        sa.Column("normalized_source_hash", sa.String(64), nullable=False),
        sa.Column("normalized_source_json", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.func.current_timestamp(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "customer_id",
            "source_type",
            "source_sha256",
            name="uq_excel_order_import_batches_source",
        ),
    )
    op.create_index(
        "ix_excel_order_import_batches_customer_created",
        BATCH_TABLE,
        ["customer_id", "created_at"],
    )

    op.create_table(
        ROW_TABLE,
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("batch_id", sa.Integer(), nullable=False),
        sa.Column("source_row_number", sa.Integer(), nullable=False),
        sa.Column("source_row_hash", sa.String(64), nullable=False),
        sa.Column("normalized_row_json", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.func.current_timestamp(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "batch_id",
            "source_row_number",
            name="uq_excel_order_import_rows_batch_row",
        ),
    )
    op.create_index("ix_excel_order_import_rows_batch", ROW_TABLE, ["batch_id"])

    op.create_table(
        CONVERSION_TABLE,
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("batch_id", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(120), nullable=False),
        sa.Column("normalized_order_hash", sa.String(64), nullable=False),
        sa.Column("order_id", sa.Integer(), nullable=False),
        sa.Column("operator_id", sa.Integer(), nullable=True),
        sa.Column("confirmation_token_id", sa.String(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.func.current_timestamp(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "batch_id", name="uq_excel_order_import_conversions_batch"
        ),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_excel_order_import_conversions_idempotency",
        ),
    )
    op.create_index(
        "ix_excel_order_import_conversions_order",
        CONVERSION_TABLE,
        ["order_id"],
    )
    _create_immutable_guards()


def downgrade() -> None:
    _assert_empty_before_downgrade()
    _drop_immutable_guards()
    op.drop_index(
        "ix_excel_order_import_conversions_order",
        table_name=CONVERSION_TABLE,
    )
    op.drop_table(CONVERSION_TABLE)
    op.drop_index("ix_excel_order_import_rows_batch", table_name=ROW_TABLE)
    op.drop_table(ROW_TABLE)
    op.drop_index(
        "ix_excel_order_import_batches_customer_created",
        table_name=BATCH_TABLE,
    )
    op.drop_table(BATCH_TABLE)
