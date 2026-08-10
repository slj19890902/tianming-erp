"""allow same customer inventory code when product names differ

Revision ID: ea09v8x9z98
Revises: dz08v8x9z97
Create Date: 2026-08-10
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "ea09v8x9z98"
down_revision: Union[str, Sequence[str], None] = "dz08v8x9z97"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

PRODUCT_TABLE = "products"
OLD_PRODUCT_CODE_CONSTRAINT = "uq_products_customer_product_code"
OLD_CUSTOMER_CODE_CONSTRAINT = "uq_products_customer_material_code"
NEW_PRODUCT_CODE_CONSTRAINT = "uq_products_customer_product_code_name"
NEW_CUSTOMER_CODE_CONSTRAINT = "uq_products_customer_material_code_name"
NAMING_CONVENTION = {"uq": "uq_%(table_name)s_%(column_0_name)s"}


def _duplicates(connection: sa.Connection, columns: str) -> list[sa.Row]:
    return list(
        connection.execute(
            sa.text(
                f"""
                SELECT customer_id, {columns}, COUNT(*) AS row_count, MIN(id) AS first_id
                FROM products
                GROUP BY customer_id, {columns}
                HAVING COUNT(*) > 1
                ORDER BY first_id
                LIMIT 20
                """
            )
        )
    )


def _render(rows: list[sa.Row]) -> str:
    return "；".join("/".join(str(value) for value in row) for row in rows)


def upgrade() -> None:
    connection = op.get_bind()
    product_duplicates = _duplicates(
        connection, "product_code, product_name"
    )
    customer_duplicates = _duplicates(
        connection, "customer_material_code, product_name"
    )
    if product_duplicates or customer_duplicates:
        raise RuntimeError(
            "已有同客户、同编码、同产品名称的重复常用箱，禁止迁移："
            + _render(product_duplicates + customer_duplicates)
        )

    with op.batch_alter_table(
        PRODUCT_TABLE,
        recreate="always",
        naming_convention=NAMING_CONVENTION,
    ) as batch:
        batch.drop_constraint(OLD_PRODUCT_CODE_CONSTRAINT, type_="unique")
        batch.drop_constraint(OLD_CUSTOMER_CODE_CONSTRAINT, type_="unique")
        batch.create_unique_constraint(
            NEW_PRODUCT_CODE_CONSTRAINT,
            ["customer_id", "product_code", "product_name"],
        )
        batch.create_unique_constraint(
            NEW_CUSTOMER_CODE_CONSTRAINT,
            ["customer_id", "customer_material_code", "product_name"],
        )


def downgrade() -> None:
    connection = op.get_bind()
    product_duplicates = _duplicates(connection, "product_code")
    customer_duplicates = _duplicates(connection, "customer_material_code")
    if product_duplicates or customer_duplicates:
        raise RuntimeError(
            "已有同客户同码、不同名称的产品，禁止破坏性降级："
            + _render(product_duplicates + customer_duplicates)
            + "。请恢复升级前完整备份。"
        )

    with op.batch_alter_table(
        PRODUCT_TABLE,
        recreate="always",
        naming_convention=NAMING_CONVENTION,
    ) as batch:
        batch.drop_constraint(NEW_PRODUCT_CODE_CONSTRAINT, type_="unique")
        batch.drop_constraint(NEW_CUSTOMER_CODE_CONSTRAINT, type_="unique")
        batch.create_unique_constraint(
            OLD_PRODUCT_CODE_CONSTRAINT,
            ["customer_id", "product_code"],
        )
        batch.create_unique_constraint(
            OLD_CUSTOMER_CODE_CONSTRAINT,
            ["customer_id", "customer_material_code"],
        )
