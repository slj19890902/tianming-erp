"""scope material code uniqueness to supplier

Revision ID: dl94v8x9z83
Revises: dk93v8x9z82
Create Date: 2026-08-06
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "dl94v8x9z83"
down_revision: Union[str, Sequence[str], None] = "dk93v8x9z82"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

MATERIAL_TABLE = "materials"
LEGACY_CODE_INDEX = "ix_materials_code"
LEGACY_CODE_CONSTRAINT = "uq_materials_code"
SUPPLIER_CODE_CONSTRAINT = "uq_materials_supplier_code"
NAMING_CONVENTION = {
    "uq": "uq_%(table_name)s_%(column_0_name)s",
}


def _duplicate_supplier_codes(connection: sa.Connection) -> list[sa.Row]:
    return list(
        connection.execute(
            sa.text(
                """
                SELECT
                    lower(trim(coalesce(supplier_name, ''))) AS supplier_key,
                    upper(trim(code)) AS code_key,
                    COUNT(*) AS row_count,
                    MIN(id) AS first_id
                FROM materials
                GROUP BY
                    lower(trim(coalesce(supplier_name, ''))),
                    upper(trim(code))
                HAVING COUNT(*) > 1
                ORDER BY first_id
                LIMIT 20
                """
            )
        )
    )


def _global_duplicate_codes(connection: sa.Connection) -> list[sa.Row]:
    return list(
        connection.execute(
            sa.text(
                """
                SELECT
                    upper(trim(code)) AS code_key,
                    COUNT(*) AS row_count,
                    MIN(id) AS first_id
                FROM materials
                GROUP BY upper(trim(code))
                HAVING COUNT(*) > 1
                ORDER BY first_id
                LIMIT 20
                """
            )
        )
    )


def _render_duplicates(rows: list[sa.Row]) -> str:
    return "；".join(
        "/".join(str(value) for value in row)
        for row in rows
    )


def upgrade() -> None:
    connection = op.get_bind()
    duplicates = _duplicate_supplier_codes(connection)
    if duplicates:
        raise RuntimeError(
            "同一供应商已存在重复材质代码，禁止迁移："
            + _render_duplicates(duplicates)
        )

    # SQLite 上原始 UNIQUE(code) 没有显式名称；naming_convention 在
    # batch 反射时为它补上稳定名称，才能安全替换而不触碰业务行。
    with op.batch_alter_table(
        MATERIAL_TABLE,
        recreate="always",
        naming_convention=NAMING_CONVENTION,
    ) as batch:
        batch.drop_index(LEGACY_CODE_INDEX)
        batch.drop_constraint(LEGACY_CODE_CONSTRAINT, type_="unique")
        batch.create_unique_constraint(
            SUPPLIER_CODE_CONSTRAINT,
            ["supplier_name", "code"],
        )
        batch.create_index(LEGACY_CODE_INDEX, ["code"], unique=False)


def downgrade() -> None:
    connection = op.get_bind()
    duplicates = _global_duplicate_codes(connection)
    if duplicates:
        raise RuntimeError(
            "已有跨供应商同码材质，禁止破坏性降级："
            + _render_duplicates(duplicates)
            + "。请恢复升级前完整备份。"
        )

    with op.batch_alter_table(
        MATERIAL_TABLE,
        recreate="always",
        naming_convention=NAMING_CONVENTION,
    ) as batch:
        batch.drop_index(LEGACY_CODE_INDEX)
        batch.drop_constraint(SUPPLIER_CODE_CONSTRAINT, type_="unique")
        batch.create_unique_constraint(LEGACY_CODE_CONSTRAINT, ["code"])
        batch.create_index(LEGACY_CODE_INDEX, ["code"], unique=True)
