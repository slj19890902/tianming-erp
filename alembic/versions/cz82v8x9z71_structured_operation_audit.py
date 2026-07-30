"""add structured, append-only operation audit envelope

Revision ID: cz82v8x9z71
Revises: cy81v8x9z70
Create Date: 2026-07-29
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "cz82v8x9z71"
down_revision: Union[str, Sequence[str], None] = "cy81v8x9z70"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


TABLE = "operation_logs"
SQLITE_UPDATE_TRIGGER = "trg_operation_logs_immutable_update"
SQLITE_DELETE_TRIGGER = "trg_operation_logs_immutable_delete"
POSTGRES_TRIGGER = "trg_operation_logs_immutable"
POSTGRES_FUNCTION = "q1_02_operation_logs_immutable"
IMMUTABLE_MESSAGE = (
    "operation_logs is append-only; UPDATE and DELETE are forbidden"
)
DOWNGRADE_BLOCKED_MESSAGE = (
    "操作审计中已存在 Q1-02 结构化事实，禁止破坏性降级；"
    "请恢复 cz82 升级前完整数据库备份。"
)

NEW_COLUMNS: tuple[sa.Column, ...] = (
    sa.Column("event_category", sa.String(length=30), nullable=True),
    sa.Column("result", sa.String(length=20), nullable=True),
    sa.Column("source", sa.String(length=30), nullable=True),
    sa.Column("module_code", sa.String(length=50), nullable=True),
    sa.Column("action_code", sa.String(length=80), nullable=True),
    sa.Column("actor_user_id_snapshot", sa.Integer(), nullable=True),
    sa.Column("operator_name_snapshot", sa.String(length=100), nullable=True),
    sa.Column("object_ref", sa.String(length=200), nullable=True),
    sa.Column("customer_id_snapshot", sa.Integer(), nullable=True),
    sa.Column("customer_name_snapshot", sa.String(length=200), nullable=True),
    sa.Column("request_id", sa.String(length=64), nullable=True),
    sa.Column("batch_id", sa.String(length=64), nullable=True),
    sa.Column("schema_version", sa.Integer(), nullable=True),
)

INDEXES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "ix_operation_logs_category_created_id",
        ("event_category", "created_at", "id"),
    ),
    (
        "ix_operation_logs_operator_created_id",
        ("actor_user_id_snapshot", "created_at", "id"),
    ),
    (
        "ix_operation_logs_module_action_created_id",
        ("module_code", "action_code", "created_at", "id"),
    ),
    (
        "ix_operation_logs_object_ref_created_id",
        ("object_ref", "created_at", "id"),
    ),
    (
        "ix_operation_logs_customer_created_id",
        ("customer_id_snapshot", "created_at", "id"),
    ),
    (
        "ix_operation_logs_result_source_created_id",
        ("result", "source", "created_at", "id"),
    ),
    ("ix_operation_logs_request_id", ("request_id",)),
    ("ix_operation_logs_batch_id", ("batch_id",)),
    ("ix_operation_logs_schema_version", ("schema_version",)),
)

_SQLITE_PROTECTED_COLUMNS = (
    "id",
    "action",
    "resource",
    "details",
    "ip_address",
    "created_at",
    "username",
    "role",
    "entity_type",
    "entity_id",
    "description",
    "user_agent",
    "extra_json",
    *(column.name for column in NEW_COLUMNS),
)


def _drop_immutability_guards() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        op.execute(sa.text(f"DROP TRIGGER IF EXISTS {SQLITE_DELETE_TRIGGER}"))
        op.execute(sa.text(f"DROP TRIGGER IF EXISTS {SQLITE_UPDATE_TRIGGER}"))
    elif dialect == "postgresql":
        op.execute(
            sa.text(
                f"DROP TRIGGER IF EXISTS {POSTGRES_TRIGGER} ON {TABLE}"
            )
        )
        op.execute(sa.text(f"DROP FUNCTION IF EXISTS {POSTGRES_FUNCTION}()"))


def _create_immutability_guards() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        unchanged_columns = "\n                   AND ".join(
            f"NEW.{column} IS OLD.{column}"
            for column in _SQLITE_PROTECTED_COLUMNS
        )
        op.execute(
            sa.text(
                f"""
                CREATE TRIGGER {SQLITE_UPDATE_TRIGGER}
                BEFORE UPDATE ON {TABLE}
                FOR EACH ROW
                WHEN NOT (
                    OLD.user_id IS NOT NULL
                    AND NEW.user_id IS NULL
                    AND {unchanged_columns}
                )
                BEGIN
                    SELECT RAISE(ABORT, '{IMMUTABLE_MESSAGE}');
                END
                """
            )
        )
        op.execute(
            sa.text(
                f"""
                CREATE TRIGGER {SQLITE_DELETE_TRIGGER}
                BEFORE DELETE ON {TABLE}
                FOR EACH ROW
                BEGIN
                    SELECT RAISE(ABORT, '{IMMUTABLE_MESSAGE}');
                END
                """
            )
        )
    elif dialect == "postgresql":
        op.execute(
            sa.text(
                f"""
                CREATE OR REPLACE FUNCTION {POSTGRES_FUNCTION}()
                RETURNS trigger AS $$
                BEGIN
                    IF TG_OP = 'UPDATE'
                       AND OLD.user_id IS NOT NULL
                       AND NEW.user_id IS NULL
                       AND (to_jsonb(NEW) - 'user_id')
                           = (to_jsonb(OLD) - 'user_id')
                    THEN
                        RETURN NEW;
                    END IF;
                    RAISE EXCEPTION '{IMMUTABLE_MESSAGE}';
                END;
                $$ LANGUAGE plpgsql
                """
            )
        )
        op.execute(
            sa.text(
                f"""
                CREATE TRIGGER {POSTGRES_TRIGGER}
                BEFORE UPDATE OR DELETE ON {TABLE}
                FOR EACH ROW EXECUTE FUNCTION {POSTGRES_FUNCTION}()
                """
            )
        )


def upgrade() -> None:
    # All columns are nullable and have no default.  Existing rows are marked
    # only with the safe fact that they predate Q1-02.  We deliberately do not
    # infer a business/security category, module, action code, operator,
    # customer or successful outcome.
    for column in NEW_COLUMNS:
        op.add_column(TABLE, column)

    op.execute(
        sa.text(
            f"""
            UPDATE {TABLE}
               SET schema_version = 0,
                   source = 'legacy',
                   result = 'legacy'
             WHERE schema_version IS NULL
            """
        )
    )

    for index_name, columns in INDEXES:
        op.create_index(index_name, TABLE, list(columns), unique=False)

    _create_immutability_guards()


def downgrade() -> None:
    connection = op.get_bind()
    # This guard is deliberately the first operation.  SQLite DDL is not
    # reliably transactional, so a blocked downgrade must leave every column,
    # index and trigger intact.
    structured_fact_count = int(
        connection.execute(
            sa.text(
                f"SELECT COUNT(*) FROM {TABLE} "
                "WHERE schema_version = 1"
            )
        ).scalar_one()
        or 0
    )
    if structured_fact_count:
        raise RuntimeError(DOWNGRADE_BLOCKED_MESSAGE)

    _drop_immutability_guards()
    for index_name, _columns in reversed(INDEXES):
        op.drop_index(index_name, table_name=TABLE)
    with op.batch_alter_table(TABLE, schema=None) as batch_op:
        for column in reversed(NEW_COLUMNS):
            batch_op.drop_column(column.name)
