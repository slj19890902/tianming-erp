"""Allow explicit document-based historical receipt prices without backfilling.

Revision ID: rq07v8x9z66
Revises: rp06v8x9z65
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "rq07v8x9z66"
down_revision = "rp06v8x9z65"
branch_labels = None
depends_on = None

TABLE = "supplier_receipt_settlement_price_facts"
ORIGIN = "historical_document_confirmation"
OLD_ADOPTION_CHECK = (
    "((fact_origin = 'receipt_frozen' AND adoption_reason IS NULL "
    "AND adoption_evidence_reference IS NULL) OR "
    "(fact_origin = 'historical_master_adoption' AND adoption_reason IS NOT NULL "
    "AND adoption_reason = '2026-09-02 老板确认采用当前主数据' "
    "AND adoption_evidence_reference IS NOT NULL "
    "AND length(trim(adoption_evidence_reference)) > 0))"
)
NEW_ADOPTION_CHECK = (
    OLD_ADOPTION_CHECK[:-1] + " OR (fact_origin = 'historical_document_confirmation' "
    "AND adoption_reason IS NOT NULL AND length(trim(adoption_reason)) > 0 "
    "AND adoption_evidence_reference IS NOT NULL "
    "AND length(trim(adoption_evidence_reference)) > 0))"
)


def _change_checks(*, include_document: bool) -> None:
    bind = op.get_bind()
    triggers: list[str] = []
    if bind.dialect.name == "sqlite":
        rows = bind.execute(sa.text(
            "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' AND sql IS NOT NULL "
            "AND (tbl_name = :table OR sql LIKE :reference)"
        ), {"table": TABLE, "reference": f"%{TABLE}%"}).all()
        for name, sql in rows:
            safe_name = str(name).replace('"', '""')
            op.execute(sa.text(f'DROP TRIGGER IF EXISTS "{safe_name}"'))
            triggers.append(str(sql))
    try:
        with op.batch_alter_table(TABLE) as batch:
            batch.drop_constraint("ck_supplier_receipt_price_facts_origin", type_="check")
            batch.drop_constraint("ck_supplier_receipt_price_facts_adoption", type_="check")
            origins = "'receipt_frozen','historical_master_adoption'"
            if include_document:
                origins += ",'historical_document_confirmation'"
            batch.create_check_constraint("ck_supplier_receipt_price_facts_origin", f"fact_origin IN ({origins})")
            batch.create_check_constraint(
                "ck_supplier_receipt_price_facts_adoption",
                NEW_ADOPTION_CHECK if include_document else OLD_ADOPTION_CHECK,
            )
    finally:
        for sql in triggers:
            op.execute(sa.text(sql))


def upgrade() -> None:
    _change_checks(include_document=True)


def downgrade() -> None:
    count = op.get_bind().execute(
        sa.text(f"SELECT COUNT(*) FROM {TABLE} WHERE fact_origin = :origin"),
        {"origin": ORIGIN},
    ).scalar_one()
    if count:
        raise RuntimeError("存在历史凭据确认价格事实，禁止降级删除或改变其来源语义")
    _change_checks(include_document=False)
