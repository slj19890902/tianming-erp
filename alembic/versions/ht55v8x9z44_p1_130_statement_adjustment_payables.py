"""add statement dispute adjustment and simple payable register

Revision ID: ht55v8x9z44
Revises: gs54v8x9z43
Create Date: 2026-08-30
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "ht55v8x9z44"
down_revision = "gs54v8x9z43"
branch_labels = None
depends_on = None


_RECEIPT_RESOLUTION_INSERT_TRIGGER = (
    "trg_finance_receipt_resolution_action_insert"
)
_RECEIPT_RESOLUTION_UPDATE_TRIGGER = (
    "trg_finance_receipt_resolution_action_update"
)


def _restore_receipt_resolution_guards() -> None:
    if op.get_bind().dialect.name != "sqlite":
        return
    op.execute(f"DROP TRIGGER IF EXISTS {_RECEIPT_RESOLUTION_UPDATE_TRIGGER}")
    op.execute(f"DROP TRIGGER IF EXISTS {_RECEIPT_RESOLUTION_INSERT_TRIGGER}")
    op.execute(
        f"""
        CREATE TRIGGER {_RECEIPT_RESOLUTION_INSERT_TRIGGER}
        BEFORE INSERT ON finance_return_receipt_items
        FOR EACH ROW
        WHEN NEW.resolution_action IS NOT NULL
         AND NEW.resolution_action NOT IN
             ('continue_delivery','accept_short','accept_over')
        BEGIN
            SELECT RAISE(ABORT, 'invalid finance receipt resolution_action');
        END
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER {_RECEIPT_RESOLUTION_UPDATE_TRIGGER}
        BEFORE UPDATE OF resolution_action ON finance_return_receipt_items
        FOR EACH ROW
        WHEN NEW.resolution_action IS NOT NULL
         AND NEW.resolution_action NOT IN
             ('continue_delivery','accept_short','accept_over')
        BEGIN
            SELECT RAISE(ABORT, 'invalid finance receipt resolution_action');
        END
        """
    )


def _assert_safe_downgrade() -> None:
    bind = op.get_bind()
    fact_checks = (
        ("finance_settlement_entities", "SELECT COUNT(*) FROM finance_settlement_entities"),
        ("finance_statement_adjustments", "SELECT COUNT(*) FROM finance_statement_adjustments"),
        ("finance_payables", "SELECT COUNT(*) FROM finance_payables"),
        (
            "customer settlement mappings",
            "SELECT COUNT(*) FROM customer_invoice_profiles WHERE settlement_entity_id IS NOT NULL",
        ),
        (
            "statement settlement snapshots",
            "SELECT COUNT(*) FROM finance_statements "
            "WHERE settlement_entity_id IS NOT NULL "
            "OR settlement_name_snapshot IS NOT NULL "
            "OR settlement_customer_ids_snapshot_json IS NOT NULL "
            "OR statement_cycle_start_day_snapshot IS NOT NULL",
        ),
        (
            "statement source customer snapshots",
            "SELECT COUNT(*) FROM finance_statement_items WHERE source_customer_id IS NOT NULL",
        ),
        (
            "receipt reconciliation overrides",
            "SELECT COUNT(*) FROM finance_return_receipt_items "
            "WHERE reconciliation_month_override IS NOT NULL "
            "OR reconciliation_override_reason IS NOT NULL "
            "OR reconciliation_overridden_by IS NOT NULL "
            "OR reconciliation_overridden_at IS NOT NULL",
        ),
    )
    used = [
        label
        for label, query in fact_checks
        if int(bind.execute(sa.text(query)).scalar_one() or 0) > 0
    ]
    if used:
        raise RuntimeError(
            "P1-130 downgrade blocked: finance facts would be lost: "
            + ", ".join(used)
        )


def upgrade() -> None:
    op.create_table(
        "finance_settlement_entities",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("entity_code", sa.String(40), nullable=False),
        sa.Column("entity_name", sa.String(200), nullable=False),
        sa.Column("short_name", sa.String(50), nullable=True),
        sa.Column("tax_no", sa.String(100), nullable=True),
        sa.Column("invoice_address", sa.Text(), nullable=True),
        sa.Column("invoice_phone", sa.String(100), nullable=True),
        sa.Column("bank_name", sa.String(200), nullable=True),
        sa.Column("bank_account", sa.String(200), nullable=True),
        sa.Column("default_seller_id", sa.Integer(), nullable=True),
        sa.Column("statement_cycle_start_day", sa.Integer(), nullable=False, server_default="20"),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("confirmation_status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("confirmed_by", sa.Integer(), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint("version >= 1", name="ck_finance_settlement_entities_version"),
        sa.CheckConstraint("confirmation_status IN ('pending','confirmed')", name="ck_finance_settlement_entities_confirmation_status"),
        sa.CheckConstraint("statement_cycle_start_day BETWEEN 1 AND 28", name="ck_finance_settlement_entities_cycle_day"),
        sa.ForeignKeyConstraint(["default_seller_id"], ["invoice_seller_entities.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["confirmed_by"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("entity_code", name="uq_finance_settlement_entities_code"),
    )
    with op.batch_alter_table("customer_invoice_profiles") as batch:
        batch.add_column(sa.Column("settlement_entity_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_customer_invoice_profiles_settlement_entity",
            "finance_settlement_entities",
            ["settlement_entity_id"],
            ["id"],
            ondelete="RESTRICT",
        )
    with op.batch_alter_table("finance_statements") as batch:
        batch.add_column(sa.Column("settlement_entity_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("settlement_name_snapshot", sa.String(200), nullable=True))
        batch.add_column(
            sa.Column("settlement_customer_ids_snapshot_json", sa.Text(), nullable=True)
        )
        batch.add_column(
            sa.Column("statement_cycle_start_day_snapshot", sa.Integer(), nullable=True)
        )
        batch.create_foreign_key(
            "fk_finance_statements_settlement_entity",
            "finance_settlement_entities",
            ["settlement_entity_id"],
            ["id"],
            ondelete="RESTRICT",
        )
    with op.batch_alter_table("finance_statement_items") as batch:
        batch.add_column(sa.Column("source_customer_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_finance_statement_items_source_customer",
            "customers",
            ["source_customer_id"],
            ["id"],
            ondelete="RESTRICT",
        )
    with op.batch_alter_table("finance_return_receipt_items") as batch:
        batch.add_column(sa.Column("reconciliation_month_override", sa.String(7), nullable=True))
        batch.add_column(sa.Column("reconciliation_override_reason", sa.Text(), nullable=True))
        batch.add_column(sa.Column("reconciliation_overridden_by", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("reconciliation_overridden_at", sa.DateTime(), nullable=True))
        batch.create_foreign_key(
            "fk_receipt_items_reconciliation_overridden_by_users",
            "users",
            ["reconciliation_overridden_by"],
            ["id"],
            ondelete="SET NULL",
        )

    op.create_table(
        "finance_statement_adjustments",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("statement_id", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(40), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("before_version", sa.Integer(), nullable=False),
        sa.Column("after_version", sa.Integer(), nullable=False),
        sa.Column("details_json", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.CheckConstraint(
            "before_version >= 1", name="ck_statement_adjustments_before_version"
        ),
        sa.CheckConstraint(
            "after_version > before_version", name="ck_statement_adjustments_after_version"
        ),
        sa.ForeignKeyConstraint(
            ["statement_id"], ["finance_statements.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
    )
    op.create_index(
        "ix_statement_adjustments_statement",
        "finance_statement_adjustments",
        ["statement_id", "id"],
    )

    op.create_table(
        "finance_payables",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("supplier_id", sa.Integer(), nullable=True),
        sa.Column("counterparty_name", sa.String(200), nullable=False),
        sa.Column("category", sa.String(30), nullable=False),
        sa.Column("document_number", sa.String(100), nullable=True),
        sa.Column("document_date", sa.Date(), nullable=False),
        sa.Column("due_date", sa.Date(), nullable=True),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="draft"),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.String(120), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("confirmed_by", sa.Integer(), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(), nullable=True),
        sa.Column("paid_by", sa.Integer(), nullable=True),
        sa.Column("paid_at", sa.DateTime(), nullable=True),
        sa.Column("voided_by", sa.Integer(), nullable=True),
        sa.Column("voided_at", sa.DateTime(), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "category IN ('material','outsourcing','freight','utilities','rent','wages','maintenance','tax_fee','other')",
            name="ck_finance_payables_category",
        ),
        sa.CheckConstraint(
            "status IN ('draft','confirmed','paid','voided')",
            name="ck_finance_payables_status",
        ),
        sa.CheckConstraint("amount > 0", name="ck_finance_payables_amount_positive"),
        sa.CheckConstraint("version >= 1", name="ck_finance_payables_version"),
        sa.ForeignKeyConstraint(
            ["supplier_id"], ["supplier_master_records.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["confirmed_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["paid_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["voided_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("idempotency_key", name="uq_finance_payables_idempotency"),
    )
    op.create_index("ix_finance_payables_document_date", "finance_payables", ["document_date"])
    op.create_index("ix_finance_payables_due_status", "finance_payables", ["due_date", "status"])
    op.create_index("ix_finance_payables_supplier", "finance_payables", ["supplier_id", "status"])


def downgrade() -> None:
    _assert_safe_downgrade()
    op.drop_index("ix_finance_payables_supplier", table_name="finance_payables")
    op.drop_index("ix_finance_payables_due_status", table_name="finance_payables")
    op.drop_index("ix_finance_payables_document_date", table_name="finance_payables")
    op.drop_table("finance_payables")
    op.drop_index(
        "ix_statement_adjustments_statement",
        table_name="finance_statement_adjustments",
    )
    op.drop_table("finance_statement_adjustments")
    with op.batch_alter_table("finance_return_receipt_items") as batch:
        batch.drop_constraint(
            "fk_receipt_items_reconciliation_overridden_by_users",
            type_="foreignkey",
        )
        batch.drop_column("reconciliation_overridden_at")
        batch.drop_column("reconciliation_overridden_by")
        batch.drop_column("reconciliation_override_reason")
        batch.drop_column("reconciliation_month_override")
    with op.batch_alter_table("finance_statement_items") as batch:
        batch.drop_constraint(
            "fk_finance_statement_items_source_customer", type_="foreignkey"
        )
        batch.drop_column("source_customer_id")
    with op.batch_alter_table("finance_statements") as batch:
        batch.drop_constraint(
            "fk_finance_statements_settlement_entity", type_="foreignkey"
        )
        batch.drop_column("statement_cycle_start_day_snapshot")
        batch.drop_column("settlement_customer_ids_snapshot_json")
        batch.drop_column("settlement_name_snapshot")
        batch.drop_column("settlement_entity_id")
    with op.batch_alter_table("customer_invoice_profiles") as batch:
        batch.drop_constraint(
            "fk_customer_invoice_profiles_settlement_entity", type_="foreignkey"
        )
        batch.drop_column("settlement_entity_id")
    op.drop_table("finance_settlement_entities")
    # The SQLite batch downgrade above recreates finance_return_receipt_items
    # and therefore removes its N005 table-bound triggers.  gs54 still owns
    # those guards, so a true round-trip must put them back before returning.
    _restore_receipt_resolution_guards()
