"""add database idempotency facts for manual finance mutations

Revision ID: go50v8x9z39
Revises: gn49v8x9z38
Create Date: 2026-08-27
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "go50v8x9z39"
down_revision = "gn49v8x9z38"
branch_labels = None
depends_on = None


_LEDGER_INSERT_TRIGGER = "trg_finance_statements_ledger_version_insert"
_LEDGER_UPDATE_TRIGGER = "trg_finance_statements_ledger_version_update"
_LEDGER_CHECK = "ck_finance_statements_ledger_version"


def _create_ledger_guards() -> None:
    if op.get_bind().dialect.name == "sqlite":
        op.execute(
            f"""
            CREATE TRIGGER {_LEDGER_INSERT_TRIGGER}
            BEFORE INSERT ON finance_statements
            FOR EACH ROW WHEN NEW.ledger_version IS NULL
                              OR NEW.ledger_version < 1
            BEGIN
                SELECT RAISE(ABORT, 'invalid statement ledger version');
            END
            """
        )
        op.execute(
            f"""
            CREATE TRIGGER {_LEDGER_UPDATE_TRIGGER}
            BEFORE UPDATE OF ledger_version ON finance_statements
            FOR EACH ROW WHEN NEW.ledger_version IS NULL
                              OR NEW.ledger_version < 1
            BEGIN
                SELECT RAISE(ABORT, 'invalid statement ledger version');
            END
            """
        )
        return
    op.create_check_constraint(
        _LEDGER_CHECK,
        "finance_statements",
        "ledger_version >= 1",
    )


def _drop_ledger_guards() -> None:
    if op.get_bind().dialect.name == "sqlite":
        op.execute(f"DROP TRIGGER IF EXISTS {_LEDGER_INSERT_TRIGGER}")
        op.execute(f"DROP TRIGGER IF EXISTS {_LEDGER_UPDATE_TRIGGER}")
        return
    op.drop_constraint(
        _LEDGER_CHECK,
        "finance_statements",
        type_="check",
    )


def upgrade() -> None:
    op.add_column(
        "finance_statements",
        sa.Column(
            "ledger_version",
            sa.Integer(),
            server_default=sa.text("1"),
            nullable=False,
        )
    )
    _create_ledger_guards()
    op.create_table(
        "finance_manual_mutations",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("idempotency_key", sa.String(length=120), nullable=False),
        sa.Column("mutation_type", sa.String(length=40), nullable=False),
        sa.Column("statement_id", sa.Integer(), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("actor_id", sa.Integer(), nullable=False),
        sa.Column("response_json", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "mutation_type IN "
            "('register_invoice', 'settle_statement', 'register_invoice_task')",
            name="ck_finance_manual_mutations_type",
        ),
        sa.CheckConstraint(
            "length(trim(idempotency_key)) >= 8",
            name="ck_finance_manual_mutations_key",
        ),
        sa.CheckConstraint(
            "(mutation_type = 'register_invoice_task' "
            "AND lower(trim(idempotency_key)) LIKE 'system:invoice-task-result:%') "
            "OR (mutation_type != 'register_invoice_task' "
            "AND lower(trim(idempotency_key)) NOT LIKE 'system:%')",
            name="ck_finance_manual_mutations_key_namespace",
        ),
        sa.CheckConstraint(
            "length(request_hash) = 64",
            name="ck_finance_manual_mutations_request_hash",
        ),
        sa.CheckConstraint(
            "length(response_json) >= 2",
            name="ck_finance_manual_mutations_response_json",
        ),
        sa.ForeignKeyConstraint(
            ["actor_id"],
            ["users.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["statement_id"],
            ["finance_statements.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_finance_manual_mutations_idempotency_key",
        ),
    )
    op.create_index(
        "ix_finance_manual_mutations_statement_id",
        "finance_manual_mutations",
        ["statement_id"],
        unique=False,
    )


def downgrade() -> None:
    connection = op.get_bind()
    fact_count = int(
        connection.scalar(
            sa.text("SELECT COUNT(*) FROM finance_manual_mutations")
        )
        or 0
    )
    if fact_count:
        raise RuntimeError(
            "cannot downgrade manual finance idempotency after mutation facts exist; "
            "restore the verified pre-upgrade database backup instead"
        )
    changed_ledger_count = int(
        connection.scalar(
            sa.text(
                "SELECT COUNT(*) FROM finance_statements "
                "WHERE ledger_version IS NULL OR ledger_version <> 1"
            )
        )
        or 0
    )
    if changed_ledger_count:
        raise RuntimeError(
            "cannot downgrade statement ledger version after ledger mutations; "
            "restore the verified pre-upgrade database backup instead"
        )
    op.drop_index(
        "ix_finance_manual_mutations_statement_id",
        table_name="finance_manual_mutations",
    )
    op.drop_table("finance_manual_mutations")
    _drop_ledger_guards()
    op.drop_column("finance_statements", "ledger_version")
