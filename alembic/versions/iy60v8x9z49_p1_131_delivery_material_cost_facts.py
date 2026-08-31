"""freeze actual direct-material cost at delivery source events

Revision ID: iy60v8x9z49
Revises: ix59v8x9z48
Create Date: 2026-09-01

This migration is structural only.  Historical deliveries are not silently
backfilled because an old inventory estimate must never be promoted to an
actual accounting cost without a verified purchase-receipt lineage.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "iy60v8x9z49"
down_revision = "ix59v8x9z48"
branch_labels = None
depends_on = None


TABLE = "finance_delivery_material_cost_facts"
POSTGRES_GUARD_FUNCTION = "p1_131_delivery_material_cost_fact_immutable"


def _create_immutable_guard() -> None:
    connection = op.get_bind()
    if connection.dialect.name == "sqlite":
        for action in ("UPDATE", "DELETE"):
            op.execute(
                f"""
                CREATE TRIGGER trg_{TABLE}_immutable_{action.lower()}
                BEFORE {action} ON {TABLE}
                FOR EACH ROW
                BEGIN
                    SELECT RAISE(
                        ABORT,
                        'finance delivery material cost facts are immutable'
                    );
                END
                """
            )
    elif connection.dialect.name == "postgresql":
        op.execute(
            f"""
            CREATE FUNCTION {POSTGRES_GUARD_FUNCTION}() RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION
                    'finance delivery material cost facts are immutable';
            END;
            $$ LANGUAGE plpgsql;

            CREATE TRIGGER trg_{TABLE}_immutable_write
            BEFORE UPDATE OR DELETE ON {TABLE}
            FOR EACH ROW EXECUTE FUNCTION {POSTGRES_GUARD_FUNCTION}();
            """
        )


def _drop_immutable_guard() -> None:
    connection = op.get_bind()
    if connection.dialect.name == "sqlite":
        for action in ("delete", "update"):
            op.execute(f"DROP TRIGGER IF EXISTS trg_{TABLE}_immutable_{action}")
    elif connection.dialect.name == "postgresql":
        op.execute(f"DROP TRIGGER IF EXISTS trg_{TABLE}_immutable_write ON {TABLE}")
        op.execute(f"DROP FUNCTION IF EXISTS {POSTGRES_GUARD_FUNCTION}()")


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("source_kind", sa.String(length=40), nullable=False),
        sa.Column("snapshot_version", sa.Integer(), nullable=False),
        sa.Column("delivery_id", sa.Integer(), nullable=False),
        sa.Column("delivery_item_id", sa.Integer(), nullable=False),
        sa.Column("delivery_inventory_allocation_id", sa.Integer(), nullable=True),
        sa.Column(
            "unordered_finished_delivery_allocation_id",
            sa.Integer(),
            nullable=True,
        ),
        sa.Column(
            "bom_component_direct_delivery_allocation_id",
            sa.Integer(),
            nullable=True,
        ),
        sa.Column("inventory_lot_id", sa.Integer(), nullable=True),
        sa.Column("production_completion_id", sa.Integer(), nullable=True),
        sa.Column(
            "incoming_receipt_purpose_allocation_id",
            sa.Integer(),
            nullable=False,
        ),
        sa.Column("purchase_receipt_fact_id", sa.Integer(), nullable=False),
        sa.Column("consumed_quantity", sa.Integer(), nullable=False),
        sa.Column("quantity_unit_snapshot", sa.String(length=20), nullable=False),
        sa.Column("unit_material_cost", sa.Numeric(20, 6), nullable=False),
        sa.Column("total_material_cost", sa.Numeric(20, 6), nullable=False),
        sa.Column("currency_snapshot", sa.String(length=3), nullable=False),
        sa.Column("tax_included_snapshot", sa.Boolean(), nullable=False),
        sa.Column("tax_rate_snapshot", sa.Numeric(8, 6), nullable=False),
        sa.Column("source_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.CheckConstraint(
            "source_kind IN ("
            "'inventory_allocation','unordered_inventory_allocation',"
            "'bom_direct_completion')",
            name="ck_finance_delivery_material_cost_facts_source_kind",
        ),
        sa.CheckConstraint(
            "((source_kind = 'inventory_allocation' "
            "AND delivery_inventory_allocation_id IS NOT NULL "
            "AND unordered_finished_delivery_allocation_id IS NULL "
            "AND bom_component_direct_delivery_allocation_id IS NULL "
            "AND inventory_lot_id IS NOT NULL) OR "
            "(source_kind = 'unordered_inventory_allocation' "
            "AND delivery_inventory_allocation_id IS NULL "
            "AND unordered_finished_delivery_allocation_id IS NOT NULL "
            "AND bom_component_direct_delivery_allocation_id IS NULL "
            "AND inventory_lot_id IS NOT NULL) OR "
            "(source_kind = 'bom_direct_completion' "
            "AND delivery_inventory_allocation_id IS NULL "
            "AND unordered_finished_delivery_allocation_id IS NULL "
            "AND bom_component_direct_delivery_allocation_id IS NOT NULL "
            "AND inventory_lot_id IS NULL "
            "AND production_completion_id IS NOT NULL))",
            name="ck_finance_delivery_material_cost_facts_source_identity",
        ),
        sa.CheckConstraint(
            "snapshot_version >= 1 AND consumed_quantity > 0",
            name="ck_finance_delivery_material_cost_facts_quantity_version",
        ),
        sa.CheckConstraint(
            "unit_material_cost > 0 AND total_material_cost > 0",
            name="ck_finance_delivery_material_cost_facts_cost",
        ),
        sa.CheckConstraint(
            "length(trim(quantity_unit_snapshot)) > 0 "
            "AND length(trim(currency_snapshot)) = 3 "
            "AND length(source_fingerprint) = 64",
            name="ck_finance_delivery_material_cost_facts_frozen_text",
        ),
        sa.CheckConstraint(
            "tax_rate_snapshot >= 0 AND tax_rate_snapshot <= 1",
            name="ck_finance_delivery_material_cost_facts_tax_rate",
        ),
        sa.ForeignKeyConstraint(
            ["delivery_id"], ["sales_deliveries.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["delivery_item_id"], ["sales_delivery_items.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["delivery_inventory_allocation_id"],
            ["delivery_inventory_allocations.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["unordered_finished_delivery_allocation_id"],
            ["unordered_finished_delivery_allocations.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["bom_component_direct_delivery_allocation_id"],
            ["bom_component_direct_delivery_allocations.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["inventory_lot_id"], ["inventory_lots.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["production_completion_id"],
            ["production_completions.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["incoming_receipt_purpose_allocation_id"],
            ["incoming_receipt_purpose_allocations.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["purchase_receipt_fact_id"],
            ["purchase_receipt_facts.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint(
            "delivery_inventory_allocation_id",
            "snapshot_version",
            name="uq_finance_delivery_material_cost_facts_inventory_version",
        ),
        sa.UniqueConstraint(
            "unordered_finished_delivery_allocation_id",
            "snapshot_version",
            name="uq_finance_delivery_material_cost_facts_unordered_version",
        ),
        sa.UniqueConstraint(
            "bom_component_direct_delivery_allocation_id",
            "snapshot_version",
            name="uq_finance_delivery_material_cost_facts_bom_version",
        ),
        sa.UniqueConstraint(
            "source_fingerprint",
            name="uq_finance_delivery_material_cost_facts_fingerprint",
        ),
    )
    op.create_index(
        "ix_finance_delivery_material_cost_facts_delivery_item",
        TABLE,
        ["delivery_item_id", "source_kind"],
    )
    op.create_index(
        "ix_finance_delivery_material_cost_facts_delivery",
        TABLE,
        ["delivery_id"],
    )
    op.create_index(
        "ix_finance_delivery_material_cost_facts_receipt_fact",
        TABLE,
        ["purchase_receipt_fact_id"],
    )
    _create_immutable_guard()


def downgrade() -> None:
    count = int(
        op.get_bind().execute(sa.text(f"SELECT COUNT(*) FROM {TABLE}")).scalar_one()
        or 0
    )
    if count:
        raise RuntimeError(
            "P1-131 downgrade blocked: frozen delivery material cost facts would be lost"
        )
    _drop_immutable_guard()
    op.drop_index(
        "ix_finance_delivery_material_cost_facts_receipt_fact", table_name=TABLE
    )
    op.drop_index(
        "ix_finance_delivery_material_cost_facts_delivery", table_name=TABLE
    )
    op.drop_index(
        "ix_finance_delivery_material_cost_facts_delivery_item", table_name=TABLE
    )
    op.drop_table(TABLE)
