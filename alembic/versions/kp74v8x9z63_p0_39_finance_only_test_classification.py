"""add finance-only test-classification guard to supplier receipt facts

Revision ID: kp74v8x9z63
Revises: jo73v8x9z62
Create Date: 2026-09-03
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "kp74v8x9z63"
down_revision = "jo73v8x9z62"
branch_labels = None
depends_on = None


def _detach_dependent_sqlite_triggers() -> list[str]:
    """Preserve cross-table SQLite triggers while batch_alter rebuilds this table."""
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        return []
    rows = bind.execute(
        sa.text(
            "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' "
            "AND sql IS NOT NULL AND (tbl_name = "
            "'supplier_receipt_settlement_price_facts' OR "
            "sql LIKE '%supplier_receipt_settlement_price_facts%')"
        )
    ).all()
    for name, _ in rows:
        safe_name = str(name).replace('"', '""')
        op.execute(sa.text(f'DROP TRIGGER IF EXISTS "{safe_name}"'))
    return [str(sql) for _, sql in rows]


def _restore_sqlite_triggers(statements: list[str]) -> None:
    for statement in statements:
        op.execute(sa.text(statement))


def upgrade() -> None:
    triggers = _detach_dependent_sqlite_triggers()
    try:
        with op.batch_alter_table("supplier_receipt_settlement_price_facts") as batch:
            batch.drop_constraint("ck_supplier_receipt_price_facts_match", type_="check")
            batch.add_column(
                sa.Column(
                    "finance_only_test_classification",
                    sa.Boolean(),
                    nullable=False,
                    server_default=sa.false(),
                )
            )
            batch.create_check_constraint(
                "ck_supplier_receipt_price_facts_match",
                "match_strategy IN ('purchase_receipt_fact','stable_material_id',"
                "'supplier_unique_material_code','owner_authorized_finance_test_classification')",
            )
            batch.create_check_constraint(
                "ck_supplier_receipt_price_facts_finance_only_test",
                "finance_only_test_classification IN (0,1) AND "
                "(finance_only_test_classification = 0 OR "
                "(fact_origin = 'historical_master_adoption' AND "
                "match_strategy = 'owner_authorized_finance_test_classification'))",
            )
    except Exception:
        _restore_sqlite_triggers(triggers)
        raise
    _restore_sqlite_triggers(triggers)


def downgrade() -> None:
    bind = op.get_bind()
    count = bind.execute(
        sa.text(
            "SELECT COUNT(*) FROM supplier_receipt_settlement_price_facts "
            "WHERE finance_only_test_classification = 1"
        )
    ).scalar_one()
    if count:
        raise RuntimeError("存在财务测试归类事实，禁止降级丢失其隔离语义")
    triggers = _detach_dependent_sqlite_triggers()
    try:
        with op.batch_alter_table("supplier_receipt_settlement_price_facts") as batch:
            batch.drop_constraint("ck_supplier_receipt_price_facts_finance_only_test", type_="check")
            batch.drop_constraint("ck_supplier_receipt_price_facts_match", type_="check")
            batch.drop_column("finance_only_test_classification")
            batch.create_check_constraint(
                "ck_supplier_receipt_price_facts_match",
                "match_strategy IN ('purchase_receipt_fact','stable_material_id',"
                "'supplier_unique_material_code')",
            )
    except Exception:
        _restore_sqlite_triggers(triggers)
        raise
    _restore_sqlite_triggers(triggers)
