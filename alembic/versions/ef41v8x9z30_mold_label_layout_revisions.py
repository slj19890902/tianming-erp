"""add versioned 40x80 mold-label layouts and frozen job snapshots

Revision ID: ef41v8x9z30
Revises: df40v8x9z29
Create Date: 2026-08-25
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "ef41v8x9z30"
down_revision = "df40v8x9z29"
branch_labels = None
depends_on = None


LAYOUTS = "mold_label_layout_revisions"
JOBS = "mold_label_print_jobs"


def _create_write_guards() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        for action in ("UPDATE", "DELETE"):
            op.execute(
                f"""
                CREATE TRIGGER trg_{LAYOUTS}_immutable_{action.lower()}
                BEFORE {action} ON {LAYOUTS}
                FOR EACH ROW
                BEGIN
                    SELECT RAISE(ABORT, '{LAYOUTS} rows are immutable');
                END
                """
            )
        op.execute(
            f"""
            CREATE TRIGGER trg_{JOBS}_layout_snapshot_insert
            BEFORE INSERT ON {JOBS}
            FOR EACH ROW
            WHEN NOT (
                (NEW.template_version = 'mold_40x30_v1'
                 AND NEW.label_layout_version IS NULL
                 AND NEW.label_layout_payload_json IS NULL
                 AND NEW.label_layout_payload_hash IS NULL)
                OR
                (NEW.template_version = 'mold_80x40_v1'
                 AND NEW.label_layout_version IS NOT NULL
                 AND NEW.label_layout_version >= 0
                 AND NEW.label_layout_payload_json IS NOT NULL
                 AND NEW.label_layout_payload_hash IS NOT NULL
                 AND length(NEW.label_layout_payload_hash) = 64)
            )
            BEGIN
                SELECT RAISE(ABORT, 'invalid mold label layout snapshot');
            END
            """
        )
    elif dialect == "postgresql":
        op.execute(
            """
            CREATE OR REPLACE FUNCTION p1_103_immutable_mold_label_layout()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                RAISE EXCEPTION 'mold label layout revisions are immutable';
            END;
            $$
            """
        )
        op.execute(
            f"""
            CREATE TRIGGER trg_{LAYOUTS}_immutable_write
            BEFORE UPDATE OR DELETE ON {LAYOUTS}
            FOR EACH ROW EXECUTE FUNCTION p1_103_immutable_mold_label_layout()
            """
        )
        op.execute(
            """
            CREATE OR REPLACE FUNCTION p1_103_validate_mold_label_job_layout()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF NOT (
                    (NEW.template_version = 'mold_40x30_v1'
                     AND NEW.label_layout_version IS NULL
                     AND NEW.label_layout_payload_json IS NULL
                     AND NEW.label_layout_payload_hash IS NULL)
                    OR
                    (NEW.template_version = 'mold_80x40_v1'
                     AND NEW.label_layout_version IS NOT NULL
                     AND NEW.label_layout_version >= 0
                     AND NEW.label_layout_payload_json IS NOT NULL
                     AND NEW.label_layout_payload_hash IS NOT NULL
                     AND length(NEW.label_layout_payload_hash) = 64)
                ) THEN
                    RAISE EXCEPTION 'invalid mold label layout snapshot';
                END IF;
                RETURN NEW;
            END;
            $$
            """
        )
        op.execute(
            f"""
            CREATE TRIGGER trg_{JOBS}_layout_snapshot_insert
            BEFORE INSERT ON {JOBS}
            FOR EACH ROW EXECUTE FUNCTION p1_103_validate_mold_label_job_layout()
            """
        )


def _drop_write_guards() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        op.execute(f"DROP TRIGGER IF EXISTS trg_{JOBS}_layout_snapshot_insert")
        for action in ("delete", "update"):
            op.execute(f"DROP TRIGGER IF EXISTS trg_{LAYOUTS}_immutable_{action}")
    elif dialect == "postgresql":
        op.execute(
            f"DROP TRIGGER IF EXISTS trg_{JOBS}_layout_snapshot_insert ON {JOBS}"
        )
        op.execute(
            f"DROP TRIGGER IF EXISTS trg_{LAYOUTS}_immutable_write ON {LAYOUTS}"
        )
        op.execute("DROP FUNCTION IF EXISTS p1_103_validate_mold_label_job_layout()")
        op.execute("DROP FUNCTION IF EXISTS p1_103_immutable_mold_label_layout()")


def upgrade() -> None:
    op.create_table(
        "mold_label_layout_revisions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("catalog_version", sa.String(length=40), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("operation_kind", sa.String(length=30), nullable=False),
        sa.Column("operation_key", sa.String(length=120), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("source_release_version", sa.Integer(), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "version >= 1", name="ck_mold_label_layout_revisions_version"
        ),
        sa.CheckConstraint(
            "operation_kind IN ('save_and_publish','restore_default','rollback')",
            name="ck_mold_label_layout_revisions_operation_kind",
        ),
        sa.CheckConstraint(
            "length(payload_hash) = 64 AND length(request_hash) = 64",
            name="ck_mold_label_layout_revisions_hashes",
        ),
        sa.CheckConstraint(
            "source_release_version IS NULL OR source_release_version >= 1",
            name="ck_mold_label_layout_revisions_source_version",
        ),
        sa.CheckConstraint(
            "((operation_kind = 'rollback' AND source_release_version IS NOT NULL) "
            "OR (operation_kind <> 'rollback' AND source_release_version IS NULL))",
            name="ck_mold_label_layout_revisions_source_kind",
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "operation_key", name="uq_mold_label_layout_revisions_operation_key"
        ),
        sa.UniqueConstraint(
            "version", name="uq_mold_label_layout_revisions_version"
        ),
    )
    op.create_index(
        "ix_mold_label_layout_revisions_version",
        "mold_label_layout_revisions",
        ["version"],
        unique=False,
    )
    op.add_column(
        "mold_label_print_jobs",
        sa.Column("label_layout_version", sa.Integer(), nullable=True),
    )
    op.add_column(
        "mold_label_print_jobs",
        sa.Column("label_layout_payload_json", sa.Text(), nullable=True),
    )
    op.add_column(
        "mold_label_print_jobs",
        sa.Column("label_layout_payload_hash", sa.String(length=64), nullable=True),
    )
    _create_write_guards()


def downgrade() -> None:
    connection = op.get_bind()
    revision_count = int(
        connection.execute(
            sa.text("SELECT COUNT(*) FROM mold_label_layout_revisions")
        ).scalar_one()
        or 0
    )
    frozen_job_count = int(
        connection.execute(
            sa.text(
                "SELECT COUNT(*) FROM mold_label_print_jobs "
                "WHERE label_layout_version IS NOT NULL "
                "OR label_layout_payload_json IS NOT NULL "
                "OR label_layout_payload_hash IS NOT NULL"
            )
        ).scalar_one()
        or 0
    )
    if revision_count or frozen_job_count:
        raise RuntimeError(
            "Refusing destructive downgrade: mold-label layout facts exist."
        )
    _drop_write_guards()
    op.drop_column("mold_label_print_jobs", "label_layout_payload_hash")
    op.drop_column("mold_label_print_jobs", "label_layout_payload_json")
    op.drop_column("mold_label_print_jobs", "label_layout_version")
    op.drop_index(
        "ix_mold_label_layout_revisions_version",
        table_name="mold_label_layout_revisions",
    )
    op.drop_table("mold_label_layout_revisions")
