"""add lightweight management cost centres and cost pool

Revision ID: iw58v8x9z47
Revises: iv57v8x9z46
Create Date: 2026-08-31
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "iw58v8x9z47"
down_revision = "iv57v8x9z46"
branch_labels = None
depends_on = None


DEFAULT_CENTERS = (
    ("PROD", "生产成本", "production"),
    ("WH_DELIVERY", "仓储配送", "warehouse_delivery"),
    ("SALES", "销售费用", "sales"),
    ("ADMIN", "管理费用", "administration"),
    ("FINANCE", "财务费用", "finance"),
    ("UNALLOCATED", "待分类", "unallocated"),
)


def _assert_safe_downgrade() -> None:
    bind = op.get_bind()
    entry_count = int(
        bind.execute(sa.text("SELECT COUNT(*) FROM finance_cost_pool_entries")).scalar_one()
        or 0
    )
    if entry_count:
        raise RuntimeError(
            "P1-131 downgrade blocked: finance cost pool facts would be lost"
        )

    expected = {code: (name, center_type) for code, name, center_type in DEFAULT_CENTERS}
    rows = bind.execute(
        sa.text(
            "SELECT code, name, center_type, is_active, version, created_by, updated_at "
            "FROM finance_cost_centers"
        )
    ).mappings()
    changed = []
    seen = set()
    for row in rows:
        code = str(row["code"])
        seen.add(code)
        if (
            code not in expected
            or expected[code] != (row["name"], row["center_type"])
            or not bool(row["is_active"])
            or int(row["version"] or 0) != 1
            or row["created_by"] is not None
            or row["updated_at"] is not None
        ):
            changed.append(code)
    if seen != set(expected) or changed:
        raise RuntimeError(
            "P1-131 downgrade blocked: finance cost centre configuration would be lost"
        )


def upgrade() -> None:
    op.create_table(
        "finance_cost_centers",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("code", sa.String(30), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("center_type", sa.String(30), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "center_type IN "
            "('production','warehouse_delivery','sales','administration','finance','unallocated')",
            name="ck_finance_cost_centers_type",
        ),
        sa.CheckConstraint(
            "length(trim(code)) BETWEEN 2 AND 30",
            name="ck_finance_cost_centers_code_not_blank",
        ),
        sa.CheckConstraint(
            "length(trim(name)) BETWEEN 1 AND 100",
            name="ck_finance_cost_centers_name_not_blank",
        ),
        sa.CheckConstraint("version >= 1", name="ck_finance_cost_centers_version"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("code", name="uq_finance_cost_centers_code"),
    )
    op.create_index(
        "ix_finance_cost_centers_active",
        "finance_cost_centers",
        ["is_active", "center_type"],
    )
    centers = sa.table(
        "finance_cost_centers",
        sa.column("code", sa.String),
        sa.column("name", sa.String),
        sa.column("center_type", sa.String),
        sa.column("is_active", sa.Boolean),
        sa.column("version", sa.Integer),
    )
    op.bulk_insert(
        centers,
        [
            {
                "code": code,
                "name": name,
                "center_type": center_type,
                "is_active": True,
                "version": 1,
            }
            for code, name, center_type in DEFAULT_CENTERS
        ],
    )

    op.create_table(
        "finance_cost_pool_entries",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("cost_month", sa.String(7), nullable=False),
        sa.Column("document_date", sa.Date(), nullable=False),
        sa.Column("cost_center_id", sa.Integer(), nullable=False),
        sa.Column("cost_center_code_snapshot", sa.String(30), nullable=False),
        sa.Column("cost_center_name_snapshot", sa.String(100), nullable=False),
        sa.Column("cost_center_type_snapshot", sa.String(30), nullable=False),
        sa.Column("cost_category", sa.String(40), nullable=False),
        sa.Column("accounting_class", sa.String(30), nullable=False),
        sa.Column(
            "allocation_basis",
            sa.String(30),
            nullable=False,
            server_default="unallocated",
        ),
        sa.Column("description", sa.String(300), nullable=False),
        sa.Column("counterparty_name", sa.String(200), nullable=True),
        sa.Column("document_number", sa.String(100), nullable=True),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("tax_amount", sa.Numeric(14, 2), nullable=False, server_default="0.00"),
        sa.Column("source_type", sa.String(30), nullable=False, server_default="manual"),
        sa.Column("source_reference", sa.String(200), nullable=False),
        sa.Column("source_fingerprint", sa.String(96), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="draft"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("confirmed_by", sa.Integer(), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(), nullable=True),
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
            "cost_month GLOB '[0-9][0-9][0-9][0-9]-[0-1][0-9]' "
            "AND CAST(substr(cost_month, 6, 2) AS INTEGER) BETWEEN 1 AND 12",
            name="ck_finance_cost_pool_entries_month_shape",
        ),
        sa.CheckConstraint(
            "cost_category IN ("
            "'outsourcing','inbound_freight',"
            "'production_wages','factory_utilities','factory_rent','maintenance',"
            "'delivery_freight','sales_expense','administrative_wages',"
            "'administrative_expense','finance_expense','tax_fee','other')",
            name="ck_finance_cost_pool_entries_category",
        ),
        sa.CheckConstraint(
            "accounting_class IN ('manufacturing','selling','administrative','finance','excluded')",
            name="ck_finance_cost_pool_entries_class",
        ),
        sa.CheckConstraint(
            "allocation_basis IN ("
            "'unallocated','direct','completion_area','production_quantity',"
            "'machine_hours','delivery_quantity','manual')",
            name="ck_finance_cost_pool_entries_allocation_basis",
        ),
        sa.CheckConstraint(
            "source_type IN ('manual','excel_import','finance_payable')",
            name="ck_finance_cost_pool_entries_source_type",
        ),
        sa.CheckConstraint(
            "status IN ('draft','confirmed','voided')",
            name="ck_finance_cost_pool_entries_status",
        ),
        sa.CheckConstraint(
            "(status = 'draft' AND confirmed_at IS NULL AND voided_at IS NULL) OR "
            "(status = 'confirmed' AND confirmed_at IS NOT NULL AND voided_at IS NULL) OR "
            "(status = 'voided' AND voided_at IS NOT NULL)",
            name="ck_finance_cost_pool_entries_status_timestamps",
        ),
        sa.CheckConstraint(
            "length(trim(description)) BETWEEN 1 AND 300",
            name="ck_finance_cost_pool_entries_description_not_blank",
        ),
        sa.CheckConstraint(
            "length(trim(source_reference)) BETWEEN 1 AND 200",
            name="ck_finance_cost_pool_entries_source_reference_not_blank",
        ),
        sa.CheckConstraint(
            "source_fingerprint IS NULL OR length(trim(source_fingerprint)) > 0",
            name="ck_finance_cost_pool_entries_source_fingerprint_not_blank",
        ),
        sa.CheckConstraint(
            "amount > 0 AND amount <= 999999999999.99",
            name="ck_finance_cost_pool_entries_amount",
        ),
        sa.CheckConstraint(
            "tax_amount >= 0 AND tax_amount <= amount "
            "AND tax_amount <= 999999999999.99",
            name="ck_finance_cost_pool_entries_tax_amount",
        ),
        sa.CheckConstraint("version >= 1", name="ck_finance_cost_pool_entries_version"),
        sa.ForeignKeyConstraint(
            ["cost_center_id"], ["finance_cost_centers.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["confirmed_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["voided_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint(
            "source_fingerprint",
            name="uq_finance_cost_pool_entries_source_fingerprint",
        ),
    )
    op.create_index(
        "ix_finance_cost_pool_entries_month_status",
        "finance_cost_pool_entries",
        ["cost_month", "status"],
    )
    op.create_index(
        "ix_finance_cost_pool_entries_center_month",
        "finance_cost_pool_entries",
        ["cost_center_id", "cost_month"],
    )
    op.create_index(
        "ix_finance_cost_pool_entries_category_month",
        "finance_cost_pool_entries",
        ["cost_category", "cost_month"],
    )
    op.create_index(
        "ix_finance_cost_pool_entries_source",
        "finance_cost_pool_entries",
        ["source_type", "source_reference"],
    )


def downgrade() -> None:
    _assert_safe_downgrade()
    op.drop_index(
        "ix_finance_cost_pool_entries_source",
        table_name="finance_cost_pool_entries",
    )
    op.drop_index(
        "ix_finance_cost_pool_entries_category_month",
        table_name="finance_cost_pool_entries",
    )
    op.drop_index(
        "ix_finance_cost_pool_entries_center_month",
        table_name="finance_cost_pool_entries",
    )
    op.drop_index(
        "ix_finance_cost_pool_entries_month_status",
        table_name="finance_cost_pool_entries",
    )
    op.drop_table("finance_cost_pool_entries")
    op.drop_index("ix_finance_cost_centers_active", table_name="finance_cost_centers")
    op.drop_table("finance_cost_centers")
