"""add FIN-001 manual invoice task closure

Revision ID: dc85v8x9z74
Revises: db84v8x9z73
Create Date: 2026-08-01

The migration creates structure only.  It never guesses seller or customer tax
facts and keeps all existing invoices as ``legacy_manual`` records.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "dc85v8x9z74"
down_revision: Union[str, Sequence[str], None] = "db84v8x9z73"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "invoice_seller_entities",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("seller_code", sa.String(40), nullable=False),
        sa.Column("seller_name", sa.String(200), nullable=False),
        sa.Column("tax_no", sa.String(100), nullable=True),
        sa.Column("address", sa.Text(), nullable=True),
        sa.Column("phone", sa.String(100), nullable=True),
        sa.Column("bank_name", sa.String(200), nullable=True),
        sa.Column("bank_account", sa.String(200), nullable=True),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("effective_from", sa.Date(), nullable=True),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column("confirmation_status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("confirmed_by", sa.Integer(), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint("version >= 1", name="ck_invoice_sellers_version"),
        sa.CheckConstraint("confirmation_status IN ('pending','confirmed')", name="ck_invoice_sellers_confirmation_status"),
        sa.ForeignKeyConstraint(["confirmed_by"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("seller_code", name="uq_invoice_seller_entities_code"),
    )
    op.create_table(
        "customer_invoice_profiles",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("invoice_title", sa.String(200), nullable=True),
        sa.Column("tax_no", sa.String(100), nullable=True),
        sa.Column("invoice_address", sa.Text(), nullable=True),
        sa.Column("invoice_phone", sa.String(100), nullable=True),
        sa.Column("bank_name", sa.String(200), nullable=True),
        sa.Column("bank_account", sa.String(200), nullable=True),
        sa.Column("default_seller_id", sa.Integer(), nullable=True),
        sa.Column("invoice_type", sa.String(40), nullable=False, server_default="digital_vat_special"),
        sa.Column("price_tax_mode", sa.String(30), nullable=False, server_default="tax_inclusive"),
        sa.Column("default_tax_rate", sa.Numeric(6, 4), nullable=True),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("confirmation_status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("confirmed_by", sa.Integer(), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint("version >= 1", name="ck_customer_invoice_profiles_version"),
        sa.CheckConstraint("confirmation_status IN ('pending','confirmed')", name="ck_customer_invoice_profiles_confirmation_status"),
        sa.CheckConstraint("invoice_type = 'digital_vat_special'", name="ck_customer_invoice_profiles_invoice_type"),
        sa.CheckConstraint("price_tax_mode IN ('tax_inclusive','tax_exclusive')", name="ck_customer_invoice_profiles_price_tax_mode"),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["default_seller_id"], ["invoice_seller_entities.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["confirmed_by"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("customer_id", name="uq_customer_invoice_profiles_customer"),
    )
    op.create_table(
        "customer_invoice_item_rules",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=True),
        sa.Column("project_name", sa.String(200), nullable=True),
        sa.Column("tax_classification_code", sa.String(80), nullable=True),
        sa.Column("unit", sa.String(40), nullable=True),
        sa.Column("tax_rate", sa.Numeric(6, 4), nullable=True),
        sa.Column("spec_source", sa.String(40), nullable=False, server_default="product_snapshot"),
        sa.Column("quantity_source", sa.String(50), nullable=False, server_default="statement_received_quantity"),
        sa.Column("amount_source", sa.String(50), nullable=False, server_default="statement_receivable_amount"),
        sa.Column("fill_unit_price", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("effective_from", sa.Date(), nullable=True),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column("confirmation_status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("confirmed_by", sa.Integer(), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint("version >= 1", name="ck_customer_invoice_item_rules_version"),
        sa.CheckConstraint("confirmation_status IN ('pending','confirmed')", name="ck_customer_invoice_item_rules_confirmation_status"),
        sa.CheckConstraint("spec_source IN ('product_snapshot','blank')", name="ck_customer_invoice_item_rules_spec_source"),
        sa.CheckConstraint("quantity_source = 'statement_received_quantity'", name="ck_customer_invoice_item_rules_quantity_source"),
        sa.CheckConstraint("amount_source = 'statement_receivable_amount'", name="ck_customer_invoice_item_rules_amount_source"),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["confirmed_by"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("customer_id", "product_id", "version", name="uq_customer_invoice_item_rules_version"),
    )
    op.create_index("ix_customer_invoice_item_rules_customer", "customer_invoice_item_rules", ["customer_id"])

    op.add_column("finance_statements", sa.Column("confirmation_status", sa.String(20), nullable=False, server_default="draft"))
    op.add_column("finance_statements", sa.Column("version", sa.Integer(), nullable=False, server_default="1"))
    op.add_column(
        "finance_statements",
        sa.Column(
            "confirmed_by",
            sa.Integer(),
            nullable=True,
        ),
    )
    op.add_column("finance_statements", sa.Column("confirmed_at", sa.DateTime(), nullable=True))
    op.execute("""
        CREATE TRIGGER trg_finance_statements_confirmation_insert
        BEFORE INSERT ON finance_statements
        WHEN NEW.confirmation_status NOT IN ('draft','confirmed','cancelled')
          OR NEW.version < 1
          OR (NEW.confirmed_by IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM users WHERE id = NEW.confirmed_by))
        BEGIN SELECT RAISE(ABORT, 'invalid statement confirmation'); END
    """)
    op.execute("""
        CREATE TRIGGER trg_finance_statements_confirmation_update
        BEFORE UPDATE OF confirmation_status, version ON finance_statements
        WHEN NEW.confirmation_status NOT IN ('draft','confirmed','cancelled')
          OR NEW.version < 1
          OR (NEW.confirmed_by IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM users WHERE id = NEW.confirmed_by))
        BEGIN SELECT RAISE(ABORT, 'invalid statement confirmation'); END
    """)

    op.create_table(
        "finance_invoice_tasks",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("task_number", sa.String(60), nullable=False),
        sa.Column("statement_id", sa.Integer(), nullable=False),
        sa.Column("statement_version", sa.Integer(), nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("seller_entity_id", sa.Integer(), nullable=False),
        sa.Column("buyer_snapshot_json", sa.Text(), nullable=False),
        sa.Column("seller_snapshot_json", sa.Text(), nullable=False),
        sa.Column("invoice_type", sa.String(40), nullable=False),
        sa.Column("price_tax_mode", sa.String(30), nullable=False),
        sa.Column("net_amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("tax_amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("total_amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="draft"),
        sa.Column("rule_version", sa.Integer(), nullable=False),
        sa.Column("source_snapshot_hash", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(160), nullable=False),
        sa.Column("export_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_export_hash", sa.String(64), nullable=True),
        sa.Column("last_exported_at", sa.DateTime(), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("confirmed_by", sa.Integer(), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(), nullable=True),
        sa.Column("voided_by", sa.Integer(), nullable=True),
        sa.Column("voided_at", sa.DateTime(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint("status IN ('draft','ready','exported','issued','failed','voided')", name="ck_finance_invoice_tasks_status"),
        sa.CheckConstraint("version >= 1", name="ck_finance_invoice_tasks_version"),
        sa.ForeignKeyConstraint(["statement_id"], ["finance_statements.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["seller_entity_id"], ["invoice_seller_entities.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["confirmed_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["voided_by"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("task_number", name="uq_finance_invoice_tasks_number"),
        sa.UniqueConstraint("idempotency_key", name="uq_finance_invoice_tasks_idempotency"),
    )
    op.create_index("ix_finance_invoice_tasks_statement", "finance_invoice_tasks", ["statement_id"])
    op.create_index("ix_finance_invoice_tasks_customer_status", "finance_invoice_tasks", ["customer_id", "status"])
    op.create_table(
        "finance_invoice_task_items",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("task_id", sa.Integer(), nullable=False),
        sa.Column("sequence_no", sa.Integer(), nullable=False),
        sa.Column("statement_item_id", sa.Integer(), nullable=False),
        sa.Column("product_code_snapshot", sa.String(100), nullable=True),
        sa.Column("product_name_snapshot", sa.String(200), nullable=True),
        sa.Column("project_name", sa.String(200), nullable=False),
        sa.Column("tax_classification_code", sa.String(80), nullable=False),
        sa.Column("specification", sa.String(200), nullable=True),
        sa.Column("unit", sa.String(40), nullable=False),
        sa.Column("quantity", sa.Numeric(14, 4), nullable=False),
        sa.Column("unit_price", sa.Numeric(14, 6), nullable=True),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("tax_rate", sa.Numeric(6, 4), nullable=False),
        sa.Column("tax_amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("rule_version", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["finance_invoice_tasks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["statement_item_id"], ["finance_statement_items.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("task_id", "sequence_no", name="uq_invoice_task_items_sequence"),
        sa.UniqueConstraint("task_id", "statement_item_id", name="uq_invoice_task_items_statement_item"),
    )
    op.create_index("ix_finance_invoice_task_items_task", "finance_invoice_task_items", ["task_id"])
    op.create_table(
        "customer_invoice_seller_changes",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("old_seller_id", sa.Integer(), nullable=True),
        sa.Column("new_seller_id", sa.Integer(), nullable=False),
        sa.Column("change_type", sa.String(20), nullable=False),
        sa.Column("statement_id", sa.Integer(), nullable=True),
        sa.Column("invoice_task_id", sa.Integer(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint("change_type IN ('temporary','permanent')", name="ck_customer_invoice_seller_changes_type"),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["old_seller_id"], ["invoice_seller_entities.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["new_seller_id"], ["invoice_seller_entities.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["statement_id"], ["finance_statements.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["invoice_task_id"], ["finance_invoice_tasks.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_customer_invoice_seller_changes_customer", "customer_invoice_seller_changes", ["customer_id"])
    op.create_table(
        "finance_invoice_attachments",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("task_id", sa.Integer(), nullable=False),
        sa.Column("attachment_type", sa.String(20), nullable=False),
        sa.Column("original_name", sa.String(255), nullable=False),
        sa.Column("stored_name", sa.String(255), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("file_size", sa.Integer(), nullable=False),
        sa.Column("source_attachment_id", sa.Integer(), nullable=True),
        sa.Column("uploaded_by", sa.Integer(), nullable=True),
        sa.Column("uploaded_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint("attachment_type IN ('original','organized')", name="ck_finance_invoice_attachments_type"),
        sa.ForeignKeyConstraint(["task_id"], ["finance_invoice_tasks.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["source_attachment_id"], ["finance_invoice_attachments.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["uploaded_by"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("task_id", "attachment_type", "content_hash", name="uq_finance_invoice_attachments_hash"),
    )
    op.create_index("ix_finance_invoice_attachments_task", "finance_invoice_attachments", ["task_id"])

    op.add_column(
        "finance_invoices",
        sa.Column(
            "invoice_task_id",
            sa.Integer(),
            nullable=True,
        ),
    )
    op.add_column(
        "finance_invoices",
        sa.Column(
            "seller_entity_id",
            sa.Integer(),
            nullable=True,
        ),
    )
    op.add_column("finance_invoices", sa.Column("net_amount", sa.Numeric(14, 2), nullable=True))
    op.add_column("finance_invoices", sa.Column("tax_amount", sa.Numeric(14, 2), nullable=True))
    op.add_column("finance_invoices", sa.Column("total_amount", sa.Numeric(14, 2), nullable=True))
    op.add_column("finance_invoices", sa.Column("invoice_status", sa.String(20), nullable=False, server_default="issued"))
    op.add_column("finance_invoices", sa.Column("source", sa.String(30), nullable=False, server_default="legacy_manual"))
    op.add_column("finance_invoices", sa.Column("failure_reason", sa.Text(), nullable=True))
    op.add_column(
        "finance_invoices",
        sa.Column(
            "confirmed_by",
            sa.Integer(),
            nullable=True,
        ),
    )
    op.add_column("finance_invoices", sa.Column("confirmed_at", sa.DateTime(), nullable=True))
    op.create_index("uq_finance_invoices_invoice_task", "finance_invoices", ["invoice_task_id"], unique=True)
    op.execute("""
        CREATE TRIGGER trg_finance_invoices_task_state_insert
        BEFORE INSERT ON finance_invoices
        WHEN NEW.invoice_status NOT IN ('issued','voided')
          OR NEW.source NOT IN ('legacy_manual','invoice_task')
          OR (NEW.invoice_task_id IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM finance_invoice_tasks WHERE id = NEW.invoice_task_id))
          OR (NEW.seller_entity_id IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM invoice_seller_entities WHERE id = NEW.seller_entity_id))
          OR (NEW.confirmed_by IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM users WHERE id = NEW.confirmed_by))
        BEGIN SELECT RAISE(ABORT, 'invalid invoice task state'); END
    """)
    op.execute("""
        CREATE TRIGGER trg_finance_invoices_task_state_update
        BEFORE UPDATE OF invoice_status, source ON finance_invoices
        WHEN NEW.invoice_status NOT IN ('issued','voided')
          OR NEW.source NOT IN ('legacy_manual','invoice_task')
          OR (NEW.invoice_task_id IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM finance_invoice_tasks WHERE id = NEW.invoice_task_id))
          OR (NEW.seller_entity_id IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM invoice_seller_entities WHERE id = NEW.seller_entity_id))
          OR (NEW.confirmed_by IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM users WHERE id = NEW.confirmed_by))
        BEGIN SELECT RAISE(ABORT, 'invalid invoice task state'); END
    """)


def downgrade() -> None:
    connection = op.get_bind()
    checks = (
        "invoice_seller_entities",
        "customer_invoice_profiles",
        "customer_invoice_item_rules",
        "finance_invoice_tasks",
        "finance_invoice_task_items",
        "customer_invoice_seller_changes",
        "finance_invoice_attachments",
    )
    for table in checks:
        if connection.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first() is not None:
            raise RuntimeError("存在开票档案、任务或附件事实，禁止破坏性降级；请保留当前数据库并恢复迁移前完整备份。")
    if connection.execute(sa.text("SELECT 1 FROM finance_statements WHERE confirmation_status <> 'draft' OR version <> 1 LIMIT 1")).first() is not None:
        raise RuntimeError("存在已确认或已变更版本的对账单，禁止破坏性降级。")
    if connection.execute(sa.text("SELECT 1 FROM finance_invoices WHERE source <> 'legacy_manual' OR invoice_task_id IS NOT NULL LIMIT 1")).first() is not None:
        raise RuntimeError("存在任务来源发票，禁止破坏性降级。")

    op.execute("DROP TRIGGER IF EXISTS trg_finance_invoices_task_state_update")
    op.execute("DROP TRIGGER IF EXISTS trg_finance_invoices_task_state_insert")
    op.drop_index("uq_finance_invoices_invoice_task", table_name="finance_invoices")
    for column in (
        "confirmed_at", "confirmed_by", "failure_reason", "source",
        "invoice_status", "total_amount", "tax_amount", "net_amount",
        "seller_entity_id", "invoice_task_id",
    ):
        op.drop_column("finance_invoices", column)
    op.drop_index("ix_finance_invoice_attachments_task", table_name="finance_invoice_attachments")
    op.drop_table("finance_invoice_attachments")
    op.drop_index("ix_customer_invoice_seller_changes_customer", table_name="customer_invoice_seller_changes")
    op.drop_table("customer_invoice_seller_changes")
    op.drop_index("ix_finance_invoice_task_items_task", table_name="finance_invoice_task_items")
    op.drop_table("finance_invoice_task_items")
    op.drop_index("ix_finance_invoice_tasks_customer_status", table_name="finance_invoice_tasks")
    op.drop_index("ix_finance_invoice_tasks_statement", table_name="finance_invoice_tasks")
    op.drop_table("finance_invoice_tasks")
    op.execute("DROP TRIGGER IF EXISTS trg_finance_statements_confirmation_update")
    op.execute("DROP TRIGGER IF EXISTS trg_finance_statements_confirmation_insert")
    for column in ("confirmed_at", "confirmed_by", "version", "confirmation_status"):
        op.drop_column("finance_statements", column)
    op.drop_index("ix_customer_invoice_item_rules_customer", table_name="customer_invoice_item_rules")
    op.drop_table("customer_invoice_item_rules")
    op.drop_table("customer_invoice_profiles")
    op.drop_table("invoice_seller_entities")
