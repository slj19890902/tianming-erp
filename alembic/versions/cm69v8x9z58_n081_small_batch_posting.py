"""N081 production-ready small-batch inventory onboarding posting.

Revision ID: cm69v8x9z58
Revises: ck67v8x9z56
Create Date: 2026-07-24
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "cm69v8x9z58"
down_revision = "ck67v8x9z56"
branch_labels = None
depends_on = None


POSTING_TABLE = "inventory_onboarding_postings"
ONBOARDING_LOT_INDEX = "uq_inventory_lots_onboarding_line_source"


def _assert_source_rows_unique(connection: sa.Connection) -> None:
    duplicate = connection.execute(
        sa.text(
            """
            SELECT source_ref_id, COUNT(*)
            FROM inventory_lots
            WHERE source_ref_type = 'inventory_onboarding_line'
              AND source_ref_id IS NOT NULL
            GROUP BY source_ref_id
            HAVING COUNT(*) > 1
            LIMIT 1
            """
        )
    ).first()
    if duplicate is not None:
        raise RuntimeError(
            "duplicate inventory onboarding source lots must be resolved "
            "before upgrade"
        )


def _assert_posting_facts_absent(connection: sa.Connection) -> None:
    posting_count = int(
        connection.execute(
            sa.text(f"SELECT COUNT(*) FROM {POSTING_TABLE}")
        ).scalar_one()
    )
    source_lot_count = int(
        connection.execute(
            sa.text(
                """
                SELECT COUNT(*)
                FROM inventory_lots
                WHERE source_ref_type = 'inventory_onboarding_line'
                """
            )
        ).scalar_one()
    )
    if posting_count or source_lot_count:
        raise RuntimeError(
            "cannot downgrade while formal onboarding posting facts exist"
        )


def _create_sqlite_guards(connection: sa.Connection) -> None:
    connection.execute(
        sa.text(
            """
            CREATE TRIGGER trg_inventory_lots_onboarding_source_insert_guard
            BEFORE INSERT ON inventory_lots
            WHEN NEW.source_ref_type = 'inventory_onboarding_line'
              AND NOT EXISTS (
                    SELECT 1
                    FROM inventory_onboarding_lines AS source
                    JOIN inventory_onboarding_batches AS batch
                      ON batch.id = source.batch_id
                    WHERE source.id = NEW.source_ref_id
                      AND batch.status = 'submitted'
                      AND source.action_decision = 'create_new'
                      AND source.match_status = 'ready'
                      AND source.inventory_type = NEW.inventory_type
                      AND source.location_id = NEW.warehouse_location_id
                      AND source.quantity = NEW.quantity_available
                      AND source.unit = NEW.unit
                      AND NEW.source_type = 'stocktake'
                      AND NEW.quantity_available > 0
                      AND NEW.quantity_reserved = 0
                      AND NEW.quantity_consumed = 0
                      AND NEW.quantity_damaged = 0
                      AND NEW.quantity_scrapped = 0
                      AND NEW.status = 'active'
                )
            BEGIN
                SELECT RAISE(
                    ABORT,
                    'onboarding inventory lot requires one eligible source line'
                );
            END
            """
        )
    )
    connection.execute(
        sa.text(
            """
            CREATE TRIGGER trg_inventory_lots_onboarding_source_update_guard
            BEFORE UPDATE ON inventory_lots
            WHEN (
                    OLD.source_ref_type = 'inventory_onboarding_line'
                    OR NEW.source_ref_type = 'inventory_onboarding_line'
                 )
              AND (
                    NEW.source_ref_type IS NOT OLD.source_ref_type
                    OR NEW.source_ref_id IS NOT OLD.source_ref_id
                  )
            BEGIN
                SELECT RAISE(
                    ABORT,
                    'onboarding inventory lot source is immutable'
                );
            END
            """
        )
    )
    connection.execute(
        sa.text(
            """
            CREATE TRIGGER trg_inventory_lots_onboarding_source_delete_guard
            BEFORE DELETE ON inventory_lots
            WHEN OLD.source_ref_type = 'inventory_onboarding_line'
            BEGIN
                SELECT RAISE(
                    ABORT,
                    'onboarding inventory lot audit cannot be deleted'
                );
            END
            """
        )
    )
    connection.execute(
        sa.text(
            """
            CREATE TRIGGER trg_inventory_onboarding_postings_insert_guard
            BEFORE INSERT ON inventory_onboarding_postings
            WHEN NOT EXISTS (
                    SELECT 1
                    FROM inventory_onboarding_batches AS batch
                    WHERE batch.id = NEW.onboarding_batch_id
                      AND batch.status = 'submitted'
                      AND batch.version = NEW.onboarding_batch_version
                      AND batch.dry_run_fingerprint
                            = NEW.onboarding_batch_fingerprint
                 )
              OR NEW.line_count <> (
                    SELECT COUNT(*)
                    FROM inventory_onboarding_lines AS source
                    WHERE source.batch_id = NEW.onboarding_batch_id
                      AND source.action_decision = 'create_new'
                      AND source.match_status = 'ready'
                 )
              OR NEW.line_count <> (
                    SELECT COUNT(*)
                    FROM inventory_lots AS lot
                    JOIN inventory_onboarding_lines AS source
                      ON source.id = lot.source_ref_id
                    WHERE source.batch_id = NEW.onboarding_batch_id
                      AND lot.source_ref_type = 'inventory_onboarding_line'
                      AND source.action_decision = 'create_new'
                      AND source.match_status = 'ready'
                 )
            BEGIN
                SELECT RAISE(
                    ABORT,
                    'posting receipt requires one complete formal batch'
                );
            END
            """
        )
    )
    connection.execute(
        sa.text(
            """
            CREATE TRIGGER trg_inventory_onboarding_postings_update_guard
            BEFORE UPDATE ON inventory_onboarding_postings
            BEGIN
                SELECT RAISE(
                    ABORT,
                    'inventory onboarding posting receipt is immutable'
                );
            END
            """
        )
    )
    connection.execute(
        sa.text(
            """
            CREATE TRIGGER trg_inventory_onboarding_postings_delete_guard
            BEFORE DELETE ON inventory_onboarding_postings
            BEGIN
                SELECT RAISE(
                    ABORT,
                    'inventory onboarding posting receipt is immutable'
                );
            END
            """
        )
    )


def _drop_sqlite_guards(connection: sa.Connection) -> None:
    for name in (
        "trg_inventory_onboarding_postings_delete_guard",
        "trg_inventory_onboarding_postings_update_guard",
        "trg_inventory_onboarding_postings_insert_guard",
        "trg_inventory_lots_onboarding_source_delete_guard",
        "trg_inventory_lots_onboarding_source_update_guard",
        "trg_inventory_lots_onboarding_source_insert_guard",
    ):
        connection.execute(sa.text(f"DROP TRIGGER IF EXISTS {name}"))


def upgrade() -> None:
    connection = op.get_bind()
    _assert_source_rows_unique(connection)
    op.create_table(
        POSTING_TABLE,
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("posting_number", sa.String(length=50), nullable=False),
        sa.Column("onboarding_batch_id", sa.Integer(), nullable=False),
        sa.Column("onboarding_batch_version", sa.Integer(), nullable=False),
        sa.Column(
            "onboarding_batch_fingerprint",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column("posting_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=100), nullable=False),
        sa.Column("floor_snapshot", sa.String(length=20), nullable=True),
        sa.Column("area_code_snapshot", sa.String(length=30), nullable=False),
        sa.Column("line_count", sa.Integer(), nullable=False),
        sa.Column("finished_line_count", sa.Integer(), nullable=False),
        sa.Column("semi_finished_line_count", sa.Integer(), nullable=False),
        sa.Column("evidence_json", sa.JSON(), nullable=False),
        sa.Column("posted_by", sa.Integer(), nullable=False),
        sa.Column("posted_at", sa.DateTime(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "onboarding_batch_version >= 1",
            name="ck_inventory_onboarding_postings_batch_version",
        ),
        sa.CheckConstraint(
            "length(onboarding_batch_fingerprint) = 64 "
            "AND length(posting_fingerprint) = 64",
            name="ck_inventory_onboarding_postings_fingerprints",
        ),
        sa.CheckConstraint(
            "length(trim(area_code_snapshot)) > 0",
            name="ck_inventory_onboarding_postings_area",
        ),
        sa.CheckConstraint(
            "line_count > 0 AND line_count <= 100",
            name="ck_inventory_onboarding_postings_line_count",
        ),
        sa.CheckConstraint(
            "finished_line_count >= 0 "
            "AND semi_finished_line_count >= 0 "
            "AND finished_line_count + semi_finished_line_count = line_count",
            name="ck_inventory_onboarding_postings_type_counts",
        ),
        sa.ForeignKeyConstraint(
            ["onboarding_batch_id"],
            ["inventory_onboarding_batches.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["posted_by"],
            ["users.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "posting_number",
            name="uq_inventory_onboarding_postings_number",
        ),
        sa.UniqueConstraint(
            "onboarding_batch_id",
            name="uq_inventory_onboarding_postings_batch",
        ),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_inventory_onboarding_postings_idempotency",
        ),
    )
    op.create_index(
        "ix_inventory_onboarding_postings_posted_at",
        POSTING_TABLE,
        ["posted_at"],
        unique=False,
    )
    op.create_index(
        ONBOARDING_LOT_INDEX,
        "inventory_lots",
        ["source_ref_type", "source_ref_id"],
        unique=True,
        sqlite_where=sa.text(
            "source_ref_type = 'inventory_onboarding_line' "
            "AND source_ref_id IS NOT NULL"
        ),
        postgresql_where=sa.text(
            "source_ref_type = 'inventory_onboarding_line' "
            "AND source_ref_id IS NOT NULL"
        ),
    )
    if connection.dialect.name == "sqlite":
        _create_sqlite_guards(connection)


def downgrade() -> None:
    connection = op.get_bind()
    _assert_posting_facts_absent(connection)
    if connection.dialect.name == "sqlite":
        _drop_sqlite_guards(connection)
    op.drop_index(
        ONBOARDING_LOT_INDEX,
        table_name="inventory_lots",
    )
    op.drop_index(
        "ix_inventory_onboarding_postings_posted_at",
        table_name=POSTING_TABLE,
    )
    op.drop_table(POSTING_TABLE)
