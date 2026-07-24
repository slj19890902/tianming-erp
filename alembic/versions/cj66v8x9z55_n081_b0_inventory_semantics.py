"""N081-B0 inventory source, ownership, and stock-date semantics.

Revision ID: cj66v8x9z55
Revises: cp72v8x9z61
Create Date: 2026-07-23
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "cj66v8x9z55"
down_revision = "cp72v8x9z61"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # SQLite can add these columns in place.  Do not batch-rebuild the formal
    # inventory table merely to add B0 metadata.
    op.add_column(
        "inventory_lots",
        sa.Column(
            "stock_date_accuracy",
            sa.String(length=20),
            sa.CheckConstraint(
                "stock_date_accuracy IN ('exact','estimated','unknown')",
                name="ck_inventory_lots_stock_date_accuracy",
            ),
            nullable=False,
            server_default=sa.text("'exact'"),
        ),
    )
    op.add_column(
        "inventory_lots",
        sa.Column("stock_date_original_text", sa.Text(), nullable=True),
    )

    # A legacy date value has no recorded provenance, so migration must not
    # silently promote it to an exact fact.
    op.execute(
        sa.text(
            "UPDATE inventory_lots "
            "SET stock_date_accuracy = 'unknown' "
        )
    )
    if op.get_bind().dialect.name == "sqlite":
        # Existing legacy rows intentionally remain unknown + NULL.  Any new
        # deliberate unknown fact must carry source text or the explicit
        # service marker, so downgrade can distinguish it from the backfill.
        op.execute(
            """
            CREATE TRIGGER trg_inventory_lots_unknown_date_source_insert
            BEFORE INSERT ON inventory_lots
            FOR EACH ROW
            WHEN NEW.stock_date_accuracy = 'unknown'
             AND TRIM(COALESCE(NEW.stock_date_original_text, '')) = ''
            BEGIN
                SELECT RAISE(
                    ABORT,
                    'new unknown stock date requires original text'
                );
            END
            """
        )
        op.execute(
            """
            CREATE TRIGGER trg_inventory_lots_unknown_date_source_update
            BEFORE UPDATE OF stock_date, stock_date_accuracy,
                stock_date_original_text
            ON inventory_lots
            FOR EACH ROW
            WHEN NEW.stock_date_accuracy = 'unknown'
             AND TRIM(COALESCE(NEW.stock_date_original_text, '')) = ''
            BEGIN
                SELECT RAISE(
                    ABORT,
                    'new unknown stock date requires original text'
                );
            END
            """
        )


def downgrade() -> None:
    connection = op.get_bind()
    fact_count = int(
        connection.execute(
            sa.text(
                "SELECT COUNT(*) FROM inventory_lots "
                "WHERE stock_date_accuracy != 'unknown' "
                "OR stock_date_original_text IS NOT NULL"
            )
        ).scalar_one()
    )
    if fact_count:
        raise RuntimeError(
            "N081-B0 stock-date facts exist; restore the verified pre-upgrade "
            "backup instead of dropping date accuracy or original text"
        )

    if connection.dialect.name == "sqlite":
        op.execute(
            "DROP TRIGGER IF EXISTS "
            "trg_inventory_lots_unknown_date_source_update"
        )
        op.execute(
            "DROP TRIGGER IF EXISTS "
            "trg_inventory_lots_unknown_date_source_insert"
        )
    op.drop_column("inventory_lots", "stock_date_original_text")
    op.drop_column("inventory_lots", "stock_date_accuracy")
