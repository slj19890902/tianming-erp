"""add reversible warehouse-area archive facts

Revision ID: jf65v8x9z54
Revises: jc64v8x9z53
Create Date: 2026-09-01

The migration only adds archive lifecycle metadata and accepted state values.
It does not archive an existing area, disable a location, or change inventory.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "jf65v8x9z54"
down_revision: Union[str, Sequence[str], None] = "jc64v8x9z53"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_BATCH_TABLES = (
    "warehouse_areas",
    "warehouse_area_storage_policies",
)


def _suspend_sqlite_batch_triggers() -> list[str]:
    """Preserve guards that SQLite would otherwise break during table recreation."""

    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        return []
    rows = bind.execute(
        sa.text(
            "SELECT name, tbl_name, sql FROM sqlite_master "
            "WHERE type = 'trigger' AND sql IS NOT NULL"
        )
    ).all()
    definitions: list[str] = []
    for name, table_name, sql in rows:
        normalized_sql = str(sql or "").lower()
        if str(table_name or "") not in _BATCH_TABLES and not any(
            table_name in normalized_sql for table_name in _BATCH_TABLES
        ):
            continue
        definitions.append(str(sql))
        quoted_name = str(name).replace('"', '""')
        bind.execute(sa.text(f'DROP TRIGGER IF EXISTS "{quoted_name}"'))
    return definitions


def _restore_sqlite_batch_triggers(definitions: list[str]) -> None:
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        return
    for definition in definitions:
        bind.execute(sa.text(definition))


def _upgrade_schema() -> None:
    with op.batch_alter_table("warehouse_areas") as batch_op:
        batch_op.drop_constraint(
            "ck_warehouse_areas_construction_status", type_="check"
        )
        batch_op.create_check_constraint(
            "ck_warehouse_areas_construction_status",
            "construction_status IN "
            "('not_started','ledger_building','ledger_complete',"
            "'layout_building','layout_complete','enabled','archived')",
        )

    with op.batch_alter_table("warehouse_area_storage_policies") as batch_op:
        batch_op.add_column(sa.Column("archived_at", sa.DateTime(), nullable=True))
        batch_op.add_column(
            sa.Column(
                "archived_by",
                sa.Integer(),
                nullable=True,
            )
        )
        batch_op.create_foreign_key(
            "fk_warehouse_area_storage_policies_archived_by_users",
            "users",
            ["archived_by"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.add_column(
            sa.Column("archive_operation_key", sa.String(length=120), nullable=True)
        )
        batch_op.add_column(
            sa.Column("archive_request_hash", sa.String(length=64), nullable=True)
        )
        batch_op.add_column(
            sa.Column("archive_feature_snapshot_json", sa.Text(), nullable=True)
        )
        batch_op.drop_constraint(
            "ck_warehouse_area_storage_policies_status", type_="check"
        )
        batch_op.create_check_constraint(
            "ck_warehouse_area_storage_policies_status",
            "status IN ('draft','published','archived')",
        )
        batch_op.create_check_constraint(
            "ck_warehouse_area_storage_policies_archive_facts",
            "status <> 'archived' OR (archived_at IS NOT NULL "
            "AND archived_by IS NOT NULL "
            "AND length(trim(archive_operation_key)) > 0 "
            "AND length(archive_request_hash) = 64 "
            "AND length(trim(archive_feature_snapshot_json)) > 0)",
        )
        batch_op.create_unique_constraint(
            "uq_warehouse_area_storage_policies_archive_idem",
            ["archive_operation_key"],
        )

def _downgrade_schema() -> None:
    bind = op.get_bind()
    archived_policy_count = int(
        bind.execute(
            sa.text(
                "SELECT COUNT(*) FROM warehouse_area_storage_policies "
                "WHERE status = 'archived'"
            )
        ).scalar()
        or 0
    )
    archived_area_count = int(
        bind.execute(
            sa.text(
                "SELECT COUNT(*) FROM warehouse_areas "
                "WHERE construction_status = 'archived'"
            )
        ).scalar()
        or 0
    )
    if archived_policy_count or archived_area_count:
        raise RuntimeError(
            "P1-139 downgrade blocked: archived warehouse areas exist; "
            "restore the verified pre-migration backup instead."
        )

    with op.batch_alter_table("warehouse_area_storage_policies") as batch_op:
        batch_op.drop_constraint(
            "uq_warehouse_area_storage_policies_archive_idem", type_="unique"
        )
        batch_op.drop_constraint(
            "ck_warehouse_area_storage_policies_archive_facts", type_="check"
        )
        batch_op.drop_constraint(
            "ck_warehouse_area_storage_policies_status", type_="check"
        )
        batch_op.drop_constraint(
            "fk_warehouse_area_storage_policies_archived_by_users",
            type_="foreignkey",
        )
        batch_op.create_check_constraint(
            "ck_warehouse_area_storage_policies_status",
            "status IN ('draft','published')",
        )
        batch_op.drop_column("archive_feature_snapshot_json")
        batch_op.drop_column("archive_request_hash")
        batch_op.drop_column("archive_operation_key")
        batch_op.drop_column("archived_by")
        batch_op.drop_column("archived_at")

    with op.batch_alter_table("warehouse_areas") as batch_op:
        batch_op.drop_constraint(
            "ck_warehouse_areas_construction_status", type_="check"
        )
        batch_op.create_check_constraint(
            "ck_warehouse_areas_construction_status",
            "construction_status IN "
            "('not_started','ledger_building','ledger_complete',"
            "'layout_building','layout_complete','enabled')",
        )


def upgrade() -> None:
    trigger_definitions = _suspend_sqlite_batch_triggers()
    try:
        _upgrade_schema()
    finally:
        _restore_sqlite_batch_triggers(trigger_definitions)


def downgrade() -> None:
    trigger_definitions = _suspend_sqlite_batch_triggers()
    try:
        _downgrade_schema()
    finally:
        _restore_sqlite_batch_triggers(trigger_definitions)
