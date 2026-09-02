"""add simplified recurring finance and acceptance notes

Revision ID: jl70v8x9z59
Revises: jk69v8x9z58
Create Date: 2026-09-02
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "jl70v8x9z59"
down_revision = "jk69v8x9z58"
branch_labels = None
depends_on = None


def _assert_safe_downgrade() -> None:
    bind = op.get_bind()
    used = []
    for table in (
        "finance_recurring_rules",
        "finance_utility_readings",
        "finance_acceptance_notes",
    ):
        count = int(bind.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar_one() or 0)
        if count:
            used.append(table)
    generated = int(
        bind.execute(
            sa.text(
                "SELECT COUNT(*) FROM finance_cost_pool_entries "
                "WHERE source_type IN ('recurring_rule','utility_reading')"
            )
        ).scalar_one()
        or 0
    )
    if generated:
        used.append("finance_cost_pool_entries(recurring/utility)")
    acceptance_payments = int(
        bind.execute(
            sa.text(
                "SELECT COUNT(*) FROM supplier_monthly_payments "
                "WHERE payment_method = 'acceptance' OR acceptance_note_id IS NOT NULL"
            )
        ).scalar_one()
        or 0
    )
    if acceptance_payments:
        used.append("supplier_monthly_payments(acceptance)")
    if used:
        raise RuntimeError(
            "P1-144 simplified finance downgrade blocked: facts would be lost: "
            + ", ".join(used)
        )


def upgrade() -> None:
    op.create_table(
        "finance_recurring_rules",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("rule_type", sa.String(30), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("role_name", sa.String(80), nullable=True),
        sa.Column("cost_center_id", sa.Integer(), nullable=False),
        sa.Column("cost_category", sa.String(40), nullable=False),
        sa.Column("start_month", sa.String(7), nullable=False),
        sa.Column("end_month", sa.String(7), nullable=True),
        sa.Column("base_amount", sa.Numeric(14, 2), nullable=False, server_default="0"),
        sa.Column("allowance_amount", sa.Numeric(14, 2), nullable=False, server_default="0"),
        sa.Column(
            "employer_social_amount", sa.Numeric(14, 2), nullable=False, server_default="0"
        ),
        sa.Column("deduction_amount", sa.Numeric(14, 2), nullable=False, server_default="0"),
        sa.Column("annual_amount", sa.Numeric(14, 2), nullable=False, server_default="0"),
        sa.Column("monthly_amount", sa.Numeric(14, 2), nullable=False, server_default="0"),
        sa.Column("due_day", sa.Integer(), nullable=False, server_default="20"),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.Column("updated_by", sa.Integer(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "rule_type IN ('employee_wage','annual_rent','fixed_monthly')",
            name="ck_finance_recurring_rules_type",
        ),
        sa.CheckConstraint(
            "cost_category IN ('production_wages','factory_rent',"
            "'administrative_expense','finance_expense')",
            name="ck_finance_recurring_rules_category",
        ),
        sa.CheckConstraint(
            "start_month GLOB '[0-9][0-9][0-9][0-9]-[0-1][0-9]' "
            "AND CAST(substr(start_month, 6, 2) AS INTEGER) BETWEEN 1 AND 12",
            name="ck_finance_recurring_rules_start_month",
        ),
        sa.CheckConstraint(
            "end_month IS NULL OR ("
            "end_month GLOB '[0-9][0-9][0-9][0-9]-[0-1][0-9]' "
            "AND CAST(substr(end_month, 6, 2) AS INTEGER) BETWEEN 1 AND 12 "
            "AND end_month >= start_month)",
            name="ck_finance_recurring_rules_end_month",
        ),
        sa.CheckConstraint(
            "base_amount >= 0 AND allowance_amount >= 0 "
            "AND employer_social_amount >= 0 AND deduction_amount >= 0 "
            "AND annual_amount >= 0 AND monthly_amount >= 0",
            name="ck_finance_recurring_rules_non_negative",
        ),
        sa.CheckConstraint(
            "(rule_type = 'employee_wage' "
            "AND base_amount + allowance_amount + employer_social_amount "
            "- deduction_amount > 0) OR "
            "(rule_type = 'annual_rent' AND annual_amount > 0) OR "
            "(rule_type = 'fixed_monthly' AND monthly_amount > 0)",
            name="ck_finance_recurring_rules_amount",
        ),
        sa.CheckConstraint(
            "due_day BETWEEN 1 AND 28", name="ck_finance_recurring_rules_due_day"
        ),
        sa.CheckConstraint("version >= 1", name="ck_finance_recurring_rules_version"),
        sa.ForeignKeyConstraint(
            ["cost_center_id"], ["finance_cost_centers.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"], ondelete="SET NULL"),
    )
    op.create_index(
        "ix_finance_recurring_rules_active_period",
        "finance_recurring_rules",
        ["is_active", "start_month", "end_month"],
    )
    op.create_index(
        "ix_finance_recurring_rules_type",
        "finance_recurring_rules",
        ["rule_type", "is_active"],
    )

    with op.batch_alter_table("finance_cost_pool_entries") as batch:
        batch.drop_constraint("ck_finance_cost_pool_entries_source_type", type_="check")
        batch.create_check_constraint(
            "ck_finance_cost_pool_entries_source_type",
            "source_type IN ('manual','excel_import','finance_payable',"
            "'recurring_rule','utility_reading')",
        )

    op.create_table(
        "finance_utility_readings",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("cost_month", sa.String(7), nullable=False),
        sa.Column("utility_type", sa.String(20), nullable=False),
        sa.Column("cost_center_id", sa.Integer(), nullable=False),
        sa.Column("previous_reading", sa.Numeric(18, 3), nullable=False),
        sa.Column("current_reading", sa.Numeric(18, 3), nullable=False),
        sa.Column("usage_quantity", sa.Numeric(18, 3), nullable=False),
        sa.Column("unit_price", sa.Numeric(14, 4), nullable=False),
        sa.Column("calculated_amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("invoice_number", sa.String(120), nullable=True),
        sa.Column("invoice_date", sa.Date(), nullable=True),
        sa.Column("invoice_amount", sa.Numeric(14, 2), nullable=True),
        sa.Column("paid_amount", sa.Numeric(14, 2), nullable=False, server_default="0"),
        sa.Column("payment_date", sa.Date(), nullable=True),
        sa.Column("cost_pool_entry_id", sa.Integer(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.Column("updated_by", sa.Integer(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "utility_type IN ('water','electricity')",
            name="ck_finance_utility_readings_type",
        ),
        sa.CheckConstraint(
            "cost_month GLOB '[0-9][0-9][0-9][0-9]-[0-1][0-9]' "
            "AND CAST(substr(cost_month, 6, 2) AS INTEGER) BETWEEN 1 AND 12",
            name="ck_finance_utility_readings_month",
        ),
        sa.CheckConstraint(
            "previous_reading >= 0 AND current_reading >= previous_reading "
            "AND abs(usage_quantity - (current_reading - previous_reading)) < 0.0005",
            name="ck_finance_utility_readings_meter",
        ),
        sa.CheckConstraint(
            "unit_price >= 0 AND calculated_amount >= 0 "
            "AND (invoice_amount IS NULL OR invoice_amount >= 0) "
            "AND paid_amount >= 0",
            name="ck_finance_utility_readings_amounts",
        ),
        sa.CheckConstraint("version >= 1", name="ck_finance_utility_readings_version"),
        sa.ForeignKeyConstraint(
            ["cost_center_id"], ["finance_cost_centers.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["cost_pool_entry_id"], ["finance_cost_pool_entries.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint(
            "cost_month", "utility_type", name="uq_finance_utility_readings_month_type"
        ),
        sa.UniqueConstraint(
            "cost_pool_entry_id", name="uq_finance_utility_readings_cost_entry"
        ),
    )
    op.create_index(
        "ix_finance_utility_readings_month",
        "finance_utility_readings",
        ["cost_month", "utility_type"],
    )

    op.create_table(
        "finance_acceptance_notes",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("bill_number", sa.String(120), nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("customer_name_snapshot", sa.String(200), nullable=False),
        sa.Column("customer_statement_id", sa.Integer(), nullable=True),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("received_date", sa.Date(), nullable=False),
        sa.Column("maturity_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="held"),
        sa.Column("supplier_id", sa.Integer(), nullable=True),
        sa.Column("supplier_name_snapshot", sa.String(200), nullable=True),
        sa.Column("supplier_statement_id", sa.Integer(), nullable=True),
        sa.Column("supplier_payment_id", sa.Integer(), nullable=True),
        sa.Column("endorsed_date", sa.Date(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.Column("updated_by", sa.Integer(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "status IN ('held','endorsed','matured','returned','voided')",
            name="ck_finance_acceptance_notes_status",
        ),
        sa.CheckConstraint("amount > 0", name="ck_finance_acceptance_notes_amount"),
        sa.CheckConstraint(
            "maturity_date >= received_date", name="ck_finance_acceptance_notes_dates"
        ),
        sa.CheckConstraint("version >= 1", name="ck_finance_acceptance_notes_version"),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["customer_statement_id"], ["finance_statements.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["supplier_id"], ["supplier_master_records.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["supplier_statement_id"],
            ["supplier_monthly_statements.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["supplier_payment_id"], ["supplier_monthly_payments.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("bill_number", name="uq_finance_acceptance_notes_bill_number"),
        sa.UniqueConstraint(
            "supplier_payment_id", name="uq_finance_acceptance_notes_supplier_payment"
        ),
    )
    op.create_index(
        "ix_finance_acceptance_notes_status_maturity",
        "finance_acceptance_notes",
        ["status", "maturity_date"],
    )
    op.create_index(
        "ix_finance_acceptance_notes_customer",
        "finance_acceptance_notes",
        ["customer_id", "received_date"],
    )

    with op.batch_alter_table("supplier_monthly_payments") as batch:
        batch.add_column(
            sa.Column("payment_method", sa.String(20), nullable=False, server_default="bank")
        )
        batch.add_column(sa.Column("acceptance_note_id", sa.Integer(), nullable=True))
        batch.create_check_constraint(
            "ck_supplier_monthly_payments_method",
            "payment_method IN ('bank','acceptance')",
        )
        batch.create_check_constraint(
            "ck_supplier_monthly_payments_acceptance_link",
            "(payment_method = 'bank' AND acceptance_note_id IS NULL) OR "
            "(payment_method = 'acceptance' AND acceptance_note_id IS NOT NULL)",
        )
        batch.create_foreign_key(
            "fk_supplier_monthly_payments_acceptance_note",
            "finance_acceptance_notes",
            ["acceptance_note_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.create_unique_constraint(
            "uq_supplier_monthly_payments_acceptance", ["acceptance_note_id"]
        )


def downgrade() -> None:
    _assert_safe_downgrade()
    with op.batch_alter_table("supplier_monthly_payments") as batch:
        batch.drop_constraint("uq_supplier_monthly_payments_acceptance", type_="unique")
        batch.drop_constraint(
            "fk_supplier_monthly_payments_acceptance_note", type_="foreignkey"
        )
        batch.drop_constraint(
            "ck_supplier_monthly_payments_acceptance_link", type_="check"
        )
        batch.drop_constraint("ck_supplier_monthly_payments_method", type_="check")
        batch.drop_column("acceptance_note_id")
        batch.drop_column("payment_method")

    op.drop_index(
        "ix_finance_acceptance_notes_customer", table_name="finance_acceptance_notes"
    )
    op.drop_index(
        "ix_finance_acceptance_notes_status_maturity",
        table_name="finance_acceptance_notes",
    )
    op.drop_table("finance_acceptance_notes")

    op.drop_index(
        "ix_finance_utility_readings_month", table_name="finance_utility_readings"
    )
    op.drop_table("finance_utility_readings")

    with op.batch_alter_table("finance_cost_pool_entries") as batch:
        batch.drop_constraint("ck_finance_cost_pool_entries_source_type", type_="check")
        batch.create_check_constraint(
            "ck_finance_cost_pool_entries_source_type",
            "source_type IN ('manual','excel_import','finance_payable')",
        )

    op.drop_index(
        "ix_finance_recurring_rules_type", table_name="finance_recurring_rules"
    )
    op.drop_index(
        "ix_finance_recurring_rules_active_period",
        table_name="finance_recurring_rules",
    )
    op.drop_table("finance_recurring_rules")
