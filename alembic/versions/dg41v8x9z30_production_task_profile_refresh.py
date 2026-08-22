"""freeze production task profiles and audit controlled refreshes

Revision ID: dg41v8x9z30
Revises: de39v8x9z28
Create Date: 2026-08-23
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "dg41v8x9z30"
down_revision = "de39v8x9z28"
branch_labels = None
depends_on = None


_TASK_COLUMNS = (
    sa.Column("production_box_style_snapshot", sa.String(150), nullable=True),
    sa.Column("production_needs_die_cut_snapshot", sa.Boolean(), nullable=True),
    sa.Column("production_process_snapshot", sa.Text(), nullable=True),
    sa.Column("production_notes_snapshot", sa.Text(), nullable=True),
    sa.Column("production_cutting_mode_snapshot", sa.String(30), nullable=True),
    sa.Column("production_mold_tool_id_snapshot", sa.Integer(), nullable=True),
    sa.Column("production_mold_tool_code_snapshot", sa.String(120), nullable=True),
    sa.Column("production_mold_tool_name_snapshot", sa.String(250), nullable=True),
    sa.Column("production_drawing_reference_snapshot", sa.Text(), nullable=True),
    sa.Column(
        "production_profile_source_version_snapshot", sa.Integer(), nullable=True
    ),
    sa.Column("production_profile_schema_version", sa.Integer(), nullable=True),
)


def upgrade() -> None:
    # Do not backfill legacy tasks from today's master data.  Existing pending
    # tasks are refreshed only through the explicit ADMIN preview/confirm path.
    for column in _TASK_COLUMNS:
        op.add_column("production_tasks", column)
    op.create_table(
        "production_task_profile_refreshes",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("task_id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("operator_id", sa.Integer(), nullable=True),
        sa.Column("idempotency_key", sa.String(120), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("preview_fingerprint", sa.String(64), nullable=False),
        sa.Column("expected_task_version", sa.Integer(), nullable=False),
        sa.Column("expected_source_version", sa.Integer(), nullable=False),
        sa.Column("before_task_version", sa.Integer(), nullable=False),
        sa.Column("after_task_version", sa.Integer(), nullable=False),
        sa.Column("source_version_snapshot", sa.Integer(), nullable=False),
        sa.Column("before_snapshot_json", sa.Text(), nullable=False),
        sa.Column("after_snapshot_json", sa.Text(), nullable=False),
        sa.Column("changes_json", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(request_hash) = 64 AND length(preview_fingerprint) = 64",
            name="ck_production_task_profile_refreshes_hashes",
        ),
        sa.CheckConstraint(
            "expected_task_version = before_task_version "
            "AND before_task_version >= 1 "
            "AND after_task_version = before_task_version + 1",
            name="ck_production_task_profile_refreshes_task_versions",
        ),
        sa.CheckConstraint(
            "expected_source_version = source_version_snapshot "
            "AND source_version_snapshot >= 1",
            name="ck_production_task_profile_refreshes_source_version",
        ),
        sa.ForeignKeyConstraint(
            ["operator_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["product_id"], ["products.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["task_id"], ["production_tasks.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_production_task_profile_refreshes_idempotency",
        ),
    )
    op.create_index(
        "ix_production_task_profile_refreshes_task_created",
        "production_task_profile_refreshes",
        ["task_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    connection = op.get_bind()
    refresh_count = int(
        connection.execute(
            sa.text("SELECT COUNT(*) FROM production_task_profile_refreshes")
        ).scalar_one()
        or 0
    )
    frozen_count = int(
        connection.execute(
            sa.text(
                "SELECT COUNT(*) FROM production_tasks "
                "WHERE production_profile_schema_version IS NOT NULL"
            )
        ).scalar_one()
        or 0
    )
    if refresh_count or frozen_count:
        raise RuntimeError(
            "Refusing destructive downgrade: frozen production profiles or refresh evidence exist."
        )
    op.drop_index(
        "ix_production_task_profile_refreshes_task_created",
        table_name="production_task_profile_refreshes",
    )
    op.drop_table("production_task_profile_refreshes")
    dependent_sqlite_triggers: list[tuple[str, str]] = []
    if connection.dialect.name == "sqlite":
        # SQLite rebuilds the whole table when dropping columns.  A previous
        # BOM safety trigger reads production_tasks and would otherwise become
        # temporarily invalid between DROP and RENAME.  Preserve every such
        # trigger verbatim and restore it after the table rebuild.
        dependent_sqlite_triggers = [
            (str(row.name), str(row.sql))
            for row in connection.execute(
                sa.text(
                    "SELECT name, sql FROM sqlite_master "
                    "WHERE type = 'trigger' AND sql IS NOT NULL "
                    "AND lower(sql) LIKE '%production_tasks%'"
                )
            ).mappings()
        ]
        for name, _sql in dependent_sqlite_triggers:
            op.execute(f'DROP TRIGGER IF EXISTS "{name}"')
    with op.batch_alter_table("production_tasks", recreate="always") as batch_op:
        for column in reversed(_TASK_COLUMNS):
            batch_op.drop_column(column.name)
    for _name, trigger_sql in dependent_sqlite_triggers:
        op.execute(trigger_sql)
