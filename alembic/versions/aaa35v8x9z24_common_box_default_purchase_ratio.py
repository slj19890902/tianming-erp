"""common box default external purchase quantity ratio

Revision ID: aaa35v8x9z24
Revises: zz34v8x9z23
Create Date: 2026-08-21
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "aaa35v8x9z24"
down_revision = "zz34v8x9z23"
branch_labels = None
depends_on = None


_ORDER_COLUMN = "external_packaging_default_order_quantity_basis"
_PURCHASE_COLUMN = "external_packaging_default_purchase_quantity_basis"
_CHECK_NAME = "ck_products_external_purchase_default_ratio"


def _sqlite_create_guards() -> None:
    if op.get_bind().dialect.name != "sqlite":
        return
    invalid = (
        f"((NEW.{_ORDER_COLUMN} IS NULL) <> "
        f"(NEW.{_PURCHASE_COLUMN} IS NULL)) OR "
        f"(NEW.{_ORDER_COLUMN} IS NOT NULL AND "
        f"(NEW.{_ORDER_COLUMN} <= 0 OR NEW.{_PURCHASE_COLUMN} <= 0)) OR "
        f"(NEW.supply_mode <> 'external_purchase' AND "
        f"(NEW.{_ORDER_COLUMN} IS NOT NULL OR NEW.{_PURCHASE_COLUMN} IS NOT NULL))"
    )
    op.execute(
        f"""
        CREATE TRIGGER trg_products_external_default_ratio_insert
        BEFORE INSERT ON products
        FOR EACH ROW WHEN {invalid}
        BEGIN
            SELECT RAISE(ABORT, 'invalid external purchase default ratio');
        END
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER trg_products_external_default_ratio_update
        BEFORE UPDATE OF supply_mode, {_ORDER_COLUMN}, {_PURCHASE_COLUMN} ON products
        FOR EACH ROW WHEN {invalid}
        BEGIN
            SELECT RAISE(ABORT, 'invalid external purchase default ratio');
        END
        """
    )


def _sqlite_drop_guards() -> None:
    if op.get_bind().dialect.name != "sqlite":
        return
    op.execute("DROP TRIGGER IF EXISTS trg_products_external_default_ratio_insert")
    op.execute("DROP TRIGGER IF EXISTS trg_products_external_default_ratio_update")


def upgrade() -> None:
    op.add_column(
        "products",
        sa.Column(_ORDER_COLUMN, sa.Numeric(18, 6), nullable=True),
    )
    op.add_column(
        "products",
        sa.Column(_PURCHASE_COLUMN, sa.Numeric(18, 6), nullable=True),
    )
    if op.get_bind().dialect.name == "sqlite":
        _sqlite_create_guards()
    else:
        op.create_check_constraint(
            _CHECK_NAME,
            "products",
            "((supply_mode = 'external_purchase' AND "
            f"(({_ORDER_COLUMN} IS NULL AND {_PURCHASE_COLUMN} IS NULL) OR "
            f"({_ORDER_COLUMN} > 0 AND {_PURCHASE_COLUMN} > 0))) OR "
            "(supply_mode <> 'external_purchase' AND "
            f"{_ORDER_COLUMN} IS NULL AND {_PURCHASE_COLUMN} IS NULL))",
        )


def downgrade() -> None:
    connection = op.get_bind()
    configured = int(
        connection.execute(
            sa.text(
                "SELECT COUNT(*) FROM products WHERE "
                f"{_ORDER_COLUMN} IS NOT NULL OR {_PURCHASE_COLUMN} IS NOT NULL"
            )
        ).scalar_one()
        or 0
    )
    if configured:
        raise RuntimeError(
            "cannot downgrade after common box default purchase ratios exist"
        )
    if connection.dialect.name == "sqlite":
        _sqlite_drop_guards()
    else:
        op.drop_constraint(_CHECK_NAME, "products", type_="check")
    op.drop_column("products", _PURCHASE_COLUMN)
    op.drop_column("products", _ORDER_COLUMN)
