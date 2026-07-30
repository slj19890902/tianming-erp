"""split A3 BOM requisition and semi-finished sources by cover/base

Revision ID: cy81v8x9z70
Revises: cw79v8x9z68
Create Date: 2026-07-29
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "cy81v8x9z70"
down_revision: Union[str, Sequence[str], None] = "cw79v8x9z68"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


SOURCE_TABLE = "requisition_item_bom_sources"
SEMI_REQUIREMENT_TABLE = "order_item_semi_requirements"
SOURCE_RULE_TRIGGER = "trg_requisition_item_bom_sources_rule_immutable"
SOURCE_RULE_FUNCTION = "n039_immutable_requisition_item_bom_source_rule"
SOURCE_COMPONENT_INDEX = (
    "ix_requisition_item_bom_sources_snapshot_component"
)
SOURCE_ACTIVE_UNIQUE = (
    "uq_requisition_item_bom_sources_active_physical_source"
)
SOURCE_UNIQUE = "uq_requisition_item_bom_sources_item_snapshot"
SEMI_BOM_UNIQUE = "uq_order_item_semi_requirements_bom_component"


def _drop_source_rule_guard() -> None:
    connection = op.get_bind()
    if connection.dialect.name == "sqlite":
        op.execute(f"DROP TRIGGER IF EXISTS {SOURCE_RULE_TRIGGER}")
    elif connection.dialect.name == "postgresql":
        op.execute(
            f"DROP TRIGGER IF EXISTS {SOURCE_RULE_TRIGGER} ON {SOURCE_TABLE}"
        )
        op.execute(f"DROP FUNCTION IF EXISTS {SOURCE_RULE_FUNCTION}()")


def _create_source_rule_guard(*, include_component_type: bool) -> None:
    connection = op.get_bind()
    if connection.dialect.name == "sqlite":
        protected_columns = "calculation_rule_version, demand_basis"
        if include_component_type:
            protected_columns += ", component_type"
        op.execute(
            f"""
            CREATE TRIGGER {SOURCE_RULE_TRIGGER}
            BEFORE UPDATE OF {protected_columns} ON {SOURCE_TABLE}
            FOR EACH ROW
            BEGIN
                SELECT RAISE(
                    ABORT,
                    'requisition_item_bom_sources calculation basis is immutable'
                );
            END
            """
        )
    elif connection.dialect.name == "postgresql":
        component_guard = (
            "\n                   OR NEW.component_type IS DISTINCT FROM OLD.component_type"
            if include_component_type
            else ""
        )
        op.execute(
            f"""
            CREATE OR REPLACE FUNCTION {SOURCE_RULE_FUNCTION}()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF NEW.calculation_rule_version
                       IS DISTINCT FROM OLD.calculation_rule_version
                   OR NEW.demand_basis IS DISTINCT FROM OLD.demand_basis
                   {component_guard}
                THEN
                    RAISE EXCEPTION
                        'requisition_item_bom_sources calculation basis is immutable';
                END IF;
                RETURN NEW;
            END;
            $$
            """
        )
        op.execute(
            f"""
            CREATE TRIGGER {SOURCE_RULE_TRIGGER}
            BEFORE UPDATE ON {SOURCE_TABLE}
            FOR EACH ROW EXECUTE FUNCTION {SOURCE_RULE_FUNCTION}()
            """
        )


def upgrade() -> None:
    # SQLite reports non-transactional DDL.  Detect legacy duplicate active
    # sources before changing a single column, constraint, trigger, or index.
    # The cv78 schema has no component_type, so every historical row is the
    # legacy `whole` physical source for this preflight.
    connection = op.get_bind()
    duplicate_legacy_active_sources = int(
        connection.execute(
            sa.text(
                f"""
                SELECT COUNT(*) FROM (
                    SELECT
                        source.sales_order_item_bom_component_id
                      FROM {SOURCE_TABLE} AS source
                      JOIN material_requisition_items AS requisition_item
                        ON requisition_item.id = source.requisition_item_id
                     WHERE lower(requisition_item.status) NOT IN (
                        'cancelled','canceled','voided','withdrawn','invalid',
                        '已取消','已作废','已撤回'
                     )
                     GROUP BY source.sales_order_item_bom_component_id
                    HAVING COUNT(*) > 1
                ) AS duplicate_legacy_active_sources
                """
            )
        ).scalar_one()
        or 0
    )
    if duplicate_legacy_active_sources:
        raise RuntimeError(
            "历史组合 BOM 存在重复活动物理料来源，升级已在任何 DDL 前停止；"
            "请先在隔离副本核对并修正重复报料事实。"
        )

    _drop_source_rule_guard()
    with op.batch_alter_table(SOURCE_TABLE) as batch:
        batch.drop_constraint(SOURCE_UNIQUE, type_="unique")
        batch.add_column(
            sa.Column(
                "component_type",
                sa.String(length=20),
                nullable=False,
                server_default="whole",
            )
        )
        batch.add_column(
            sa.Column(
                "active_guard",
                sa.Integer(),
                nullable=True,
                server_default="1",
            )
        )
        batch.create_check_constraint(
            "ck_requisition_item_bom_sources_component_type",
            "component_type IN ('whole','cover','base')",
        )
        batch.create_check_constraint(
            "ck_requisition_item_bom_sources_active_guard",
            "active_guard IS NULL OR active_guard = 1",
        )
        batch.create_unique_constraint(
            SOURCE_UNIQUE,
            [
                "requisition_item_id",
                "sales_order_item_bom_component_id",
                "component_type",
            ],
        )
    with op.batch_alter_table(SOURCE_TABLE) as batch:
        batch.alter_column(
            "component_type",
            existing_type=sa.String(length=20),
            server_default=None,
        )
    op.create_index(
        SOURCE_COMPONENT_INDEX,
        SOURCE_TABLE,
        ["sales_order_item_bom_component_id", "component_type"],
    )
    op.execute(
        sa.text(f"UPDATE {SOURCE_TABLE} SET active_guard = NULL")
    )
    op.execute(
        sa.text(
            f"""
            UPDATE {SOURCE_TABLE}
               SET active_guard = 1
             WHERE EXISTS (
                SELECT 1
                  FROM material_requisition_items AS requisition_item
                 WHERE requisition_item.id =
                       {SOURCE_TABLE}.requisition_item_id
                   AND lower(requisition_item.status) NOT IN (
                       'cancelled','canceled','voided','withdrawn','invalid',
                       '已取消','已作废','已撤回'
                   )
             )
            """
        )
    )
    duplicate_active_sources = int(
        op.get_bind()
        .execute(
            sa.text(
                f"""
                SELECT COUNT(*) FROM (
                    SELECT sales_order_item_bom_component_id, component_type
                      FROM {SOURCE_TABLE}
                     WHERE active_guard = 1
                     GROUP BY sales_order_item_bom_component_id, component_type
                    HAVING COUNT(*) > 1
                ) AS duplicate_active_physical_sources
                """
            )
        )
        .scalar_one()
        or 0
    )
    if duplicate_active_sources:
        raise RuntimeError(
            "历史组合 BOM 存在重复活动物理料来源，禁止创建并发唯一门禁；"
            "请先在隔离副本核对并修正重复报料事实。"
        )
    op.create_index(
        SOURCE_ACTIVE_UNIQUE,
        SOURCE_TABLE,
        ["sales_order_item_bom_component_id", "component_type"],
        unique=True,
        sqlite_where=sa.text("active_guard = 1"),
        postgresql_where=sa.text("active_guard = 1"),
    )
    _create_source_rule_guard(include_component_type=True)

    # One A3 BOM snapshot can now own independent cover and base requirements.
    # Historical rows retain their existing `whole` component value.
    op.drop_index(SEMI_BOM_UNIQUE, table_name=SEMI_REQUIREMENT_TABLE)
    op.create_index(
        SEMI_BOM_UNIQUE,
        SEMI_REQUIREMENT_TABLE,
        ["sales_order_item_bom_component_id", "component_type"],
        unique=True,
        sqlite_where=sa.text(
            "sales_order_item_bom_component_id IS NOT NULL"
        ),
        postgresql_where=sa.text(
            "sales_order_item_bom_component_id IS NOT NULL"
        ),
    )


def downgrade() -> None:
    connection = op.get_bind()
    new_source_facts = int(
        connection.execute(
            sa.text(
                f"SELECT COUNT(*) FROM {SOURCE_TABLE} "
                "WHERE component_type <> 'whole'"
            )
        ).scalar_one()
        or 0
    )
    new_semi_facts = int(
        connection.execute(
            sa.text(
                "SELECT COUNT(*) FROM ("
                f"SELECT sales_order_item_bom_component_id FROM {SEMI_REQUIREMENT_TABLE} "
                "WHERE sales_order_item_bom_component_id IS NOT NULL "
                "GROUP BY sales_order_item_bom_component_id "
                "HAVING COUNT(*) > 1"
                ") AS duplicated_bom_requirements"
            )
        ).scalar_one()
        or 0
    )
    if new_source_facts or new_semi_facts:
        raise RuntimeError(
            "已有组合 A3 盖片/底片报料或半成品需求事实，禁止破坏性降级；"
            "请恢复 cw79 升级前完整数据库备份。"
        )

    op.drop_index(SEMI_BOM_UNIQUE, table_name=SEMI_REQUIREMENT_TABLE)
    op.create_index(
        SEMI_BOM_UNIQUE,
        SEMI_REQUIREMENT_TABLE,
        ["sales_order_item_bom_component_id"],
        unique=True,
        sqlite_where=sa.text(
            "sales_order_item_bom_component_id IS NOT NULL"
        ),
        postgresql_where=sa.text(
            "sales_order_item_bom_component_id IS NOT NULL"
        ),
    )

    _drop_source_rule_guard()
    op.drop_index(SOURCE_ACTIVE_UNIQUE, table_name=SOURCE_TABLE)
    op.drop_index(SOURCE_COMPONENT_INDEX, table_name=SOURCE_TABLE)
    with op.batch_alter_table(SOURCE_TABLE) as batch:
        batch.drop_constraint(SOURCE_UNIQUE, type_="unique")
        batch.drop_constraint(
            "ck_requisition_item_bom_sources_component_type",
            type_="check",
        )
        batch.drop_constraint(
            "ck_requisition_item_bom_sources_active_guard",
            type_="check",
        )
        batch.drop_column("active_guard")
        batch.drop_column("component_type")
        batch.create_unique_constraint(
            SOURCE_UNIQUE,
            ["requisition_item_id", "sales_order_item_bom_component_id"],
        )
    _create_source_rule_guard(include_component_type=False)
