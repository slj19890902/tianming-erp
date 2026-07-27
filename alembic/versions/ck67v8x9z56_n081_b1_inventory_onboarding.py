"""N081-B1 private inventory-onboarding drafts and immutable submission.

Revision ID: ck67v8x9z56
Revises: cj66v8x9z55
Create Date: 2026-07-23
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "ck67v8x9z56"
down_revision = "cj66v8x9z55"
branch_labels = None
depends_on = None


BATCH_TABLE = "inventory_onboarding_batches"
LINE_TABLE = "inventory_onboarding_lines"


def _create_sqlite_guards(connection: sa.Connection) -> None:
    connection.execute(
        sa.text(
            """
            CREATE TRIGGER trg_inventory_onboarding_batches_insert_guard
            BEFORE INSERT ON inventory_onboarding_batches
            WHEN NEW.status <> 'draft'
            BEGIN
                SELECT RAISE(
                    ABORT,
                    'inventory onboarding batch must be created as draft'
                );
            END
            """
        )
    )
    connection.execute(
        sa.text(
            """
            CREATE TRIGGER trg_inventory_onboarding_batches_source_immutable
            BEFORE UPDATE ON inventory_onboarding_batches
            WHEN NEW.id IS NOT OLD.id
              OR NEW.batch_number IS NOT OLD.batch_number
              OR NEW.source_file_reference IS NOT OLD.source_file_reference
              OR NEW.source_file_sha256 IS NOT OLD.source_file_sha256
              OR NEW.source_original_filename IS NOT OLD.source_original_filename
              OR NEW.source_content_type IS NOT OLD.source_content_type
              OR NEW.source_size IS NOT OLD.source_size
              OR NEW.source_format IS NOT OLD.source_format
              OR NEW.source_encoding IS NOT OLD.source_encoding
              OR NEW.created_by IS NOT OLD.created_by
              OR NEW.created_at IS NOT OLD.created_at
            BEGIN
                SELECT RAISE(
                    ABORT,
                    'inventory onboarding source file facts are immutable'
                );
            END
            """
        )
    )
    connection.execute(
        sa.text(
            """
            CREATE TRIGGER trg_inventory_onboarding_batches_update_guard
            BEFORE UPDATE ON inventory_onboarding_batches
            WHEN OLD.status = 'submitted'
              OR NEW.version <> OLD.version + 1
              OR NEW.status NOT IN ('draft', 'submitted')
              OR (
                  OLD.status = 'draft'
                  AND NEW.status = 'submitted'
                  AND (
                      NEW.dry_run_fingerprint IS NULL
                      OR NEW.dry_run_summary_json IS NULL
                      OR NEW.dry_run_by IS NULL
                      OR NEW.dry_run_at IS NULL
                      OR NEW.submit_idempotency_key IS NULL
                      OR NEW.submitted_by IS NULL
                      OR NEW.submitted_at IS NULL
                  )
              )
            BEGIN
                SELECT RAISE(
                    ABORT,
                    'invalid inventory onboarding transition or immutable update'
                );
            END
            """
        )
    )
    connection.execute(
        sa.text(
            """
            CREATE TRIGGER trg_inventory_onboarding_batches_delete_guard
            BEFORE DELETE ON inventory_onboarding_batches
            WHEN OLD.status = 'submitted'
            BEGIN
                SELECT RAISE(
                    ABORT,
                    'submitted inventory onboarding batch is immutable'
                );
            END
            """
        )
    )
    connection.execute(
        sa.text(
            """
            CREATE TRIGGER trg_inventory_onboarding_lines_insert_guard
            BEFORE INSERT ON inventory_onboarding_lines
            WHEN NOT EXISTS (
                SELECT 1
                FROM inventory_onboarding_batches
                WHERE id = NEW.batch_id AND status = 'draft'
            )
            BEGIN
                SELECT RAISE(
                    ABORT,
                    'inventory onboarding lines require a draft batch'
                );
            END
            """
        )
    )
    connection.execute(
        sa.text(
            """
            CREATE TRIGGER trg_inventory_onboarding_lines_update_guard
            BEFORE UPDATE ON inventory_onboarding_lines
            WHEN NOT EXISTS (
                    SELECT 1
                    FROM inventory_onboarding_batches
                    WHERE id = OLD.batch_id AND status = 'draft'
                 )
              OR NOT EXISTS (
                    SELECT 1
                    FROM inventory_onboarding_batches
                    WHERE id = NEW.batch_id AND status = 'draft'
                 )
              OR NEW.id IS NOT OLD.id
              OR NEW.batch_id IS NOT OLD.batch_id
              OR NEW.source_sheet_name IS NOT OLD.source_sheet_name
              OR NEW.source_row_number IS NOT OLD.source_row_number
              OR NEW.source_row_hash IS NOT OLD.source_row_hash
              OR NEW.raw_row_text IS NOT OLD.raw_row_text
              OR NEW.original_values_json IS NOT OLD.original_values_json
              OR NEW.created_at IS NOT OLD.created_at
              OR NEW.version <> OLD.version + 1
            BEGIN
                SELECT RAISE(
                    ABORT,
                    'inventory onboarding source row or submitted line is immutable'
                );
            END
            """
        )
    )
    connection.execute(
        sa.text(
            """
            CREATE TRIGGER trg_inventory_onboarding_lines_delete_guard
            BEFORE DELETE ON inventory_onboarding_lines
            WHEN NOT EXISTS (
                SELECT 1
                FROM inventory_onboarding_batches
                WHERE id = OLD.batch_id AND status = 'draft'
            )
            BEGIN
                SELECT RAISE(
                    ABORT,
                    'submitted inventory onboarding line is immutable'
                );
            END
            """
        )
    )


def _drop_sqlite_guards(connection: sa.Connection) -> None:
    for trigger in (
        "trg_inventory_onboarding_lines_delete_guard",
        "trg_inventory_onboarding_lines_update_guard",
        "trg_inventory_onboarding_lines_insert_guard",
        "trg_inventory_onboarding_batches_delete_guard",
        "trg_inventory_onboarding_batches_update_guard",
        "trg_inventory_onboarding_batches_source_immutable",
        "trg_inventory_onboarding_batches_insert_guard",
    ):
        connection.execute(sa.text(f"DROP TRIGGER IF EXISTS {trigger}"))


def _assert_onboarding_tables_empty(connection: sa.Connection) -> None:
    line_count = int(
        connection.execute(
            sa.text("SELECT COUNT(*) FROM inventory_onboarding_lines")
        ).scalar_one()
    )
    batch_count = int(
        connection.execute(
            sa.text("SELECT COUNT(*) FROM inventory_onboarding_batches")
        ).scalar_one()
    )
    if line_count or batch_count:
        raise RuntimeError(
            "N081-B1 inventory-onboarding facts exist; downgrade is "
            "fail-closed. Restore the verified pre-upgrade backup instead "
            "of discarding source files, original rows, or submitted drafts."
        )


def upgrade() -> None:
    op.create_table(
        BATCH_TABLE,
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("batch_number", sa.String(length=50), nullable=False),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default=sa.text("'draft'"),
        ),
        sa.Column(
            "version",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column("source_file_reference", sa.Text(), nullable=False),
        sa.Column("source_file_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "source_original_filename", sa.String(length=255), nullable=False
        ),
        sa.Column("source_content_type", sa.String(length=150), nullable=False),
        sa.Column("source_size", sa.Integer(), nullable=False),
        sa.Column("source_format", sa.String(length=10), nullable=False),
        sa.Column("source_encoding", sa.String(length=20), nullable=True),
        sa.Column("resolved_floor", sa.Integer(), nullable=True),
        sa.Column("resolved_area_code", sa.String(length=30), nullable=True),
        sa.Column("dry_run_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("dry_run_summary_json", sa.JSON(), nullable=True),
        sa.Column("dry_run_by", sa.Integer(), nullable=True),
        sa.Column("dry_run_at", sa.DateTime(), nullable=True),
        sa.Column("submit_idempotency_key", sa.String(length=120), nullable=True),
        sa.Column("submitted_by", sa.Integer(), nullable=True),
        sa.Column("submitted_at", sa.DateTime(), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.Column("updated_by", sa.Integer(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "status IN ('draft','submitted')",
            name="ck_inventory_onboarding_batches_status",
        ),
        sa.CheckConstraint(
            "version >= 1",
            name="ck_inventory_onboarding_batches_version",
        ),
        sa.CheckConstraint(
            "source_size > 0",
            name="ck_inventory_onboarding_batches_source_size",
        ),
        sa.CheckConstraint(
            "length(source_file_sha256) = 64",
            name="ck_inventory_onboarding_batches_source_sha256",
        ),
        sa.CheckConstraint(
            "source_format IN ('csv','xlsx')",
            name="ck_inventory_onboarding_batches_source_format",
        ),
        sa.CheckConstraint(
            "(source_format = 'xlsx' AND source_encoding IS NULL) OR "
            "(source_format = 'csv' AND "
            "source_encoding IN ('utf-8','utf-8-sig','gb18030'))",
            name="ck_inventory_onboarding_batches_source_encoding",
        ),
        sa.CheckConstraint(
            "(dry_run_fingerprint IS NULL AND dry_run_summary_json IS NULL "
            "AND dry_run_by IS NULL AND dry_run_at IS NULL) OR "
            "(dry_run_fingerprint IS NOT NULL "
            "AND length(dry_run_fingerprint) = 64 "
            "AND dry_run_summary_json IS NOT NULL "
            "AND dry_run_by IS NOT NULL AND dry_run_at IS NOT NULL)",
            name="ck_inventory_onboarding_batches_dry_run_state",
        ),
        sa.CheckConstraint(
            "(status = 'draft' AND submit_idempotency_key IS NULL "
            "AND submitted_by IS NULL AND submitted_at IS NULL) OR "
            "(status = 'submitted' AND submit_idempotency_key IS NOT NULL "
            "AND submitted_by IS NOT NULL AND submitted_at IS NOT NULL "
            "AND dry_run_fingerprint IS NOT NULL)",
            name="ck_inventory_onboarding_batches_submit_state",
        ),
        sa.ForeignKeyConstraint(
            ["dry_run_by"], ["users.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["submitted_by"], ["users.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["updated_by"], ["users.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "batch_number",
            name="uq_inventory_onboarding_batches_number",
        ),
        sa.UniqueConstraint(
            "source_file_reference",
            name="uq_inventory_onboarding_batches_source_reference",
        ),
        sa.UniqueConstraint(
            "source_file_sha256",
            name="uq_inventory_onboarding_batches_source_sha256",
        ),
        sa.UniqueConstraint(
            "submit_idempotency_key",
            name="uq_inventory_onboarding_batches_submit_idempotency",
        ),
    )
    op.create_index(
        "ix_inventory_onboarding_batches_status_created",
        BATCH_TABLE,
        ["status", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_inventory_onboarding_batches_area_status",
        BATCH_TABLE,
        ["resolved_area_code", "status"],
        unique=False,
    )

    op.create_table(
        LINE_TABLE,
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("batch_id", sa.Integer(), nullable=False),
        sa.Column("source_sheet_name", sa.String(length=100), nullable=False),
        sa.Column("source_row_number", sa.Integer(), nullable=False),
        sa.Column("source_row_hash", sa.String(length=64), nullable=False),
        sa.Column("raw_row_text", sa.Text(), nullable=True),
        sa.Column("original_values_json", sa.JSON(), nullable=False),
        sa.Column("stocktake_date", sa.Date(), nullable=True),
        sa.Column("stocktaker_name", sa.String(length=100), nullable=True),
        sa.Column("inventory_type", sa.String(length=30), nullable=True),
        sa.Column("ownership_type", sa.String(length=30), nullable=True),
        sa.Column("location_id", sa.Integer(), nullable=True),
        sa.Column("location_code_snapshot", sa.String(length=50), nullable=True),
        sa.Column("floor_snapshot", sa.Integer(), nullable=True),
        sa.Column("warehouse_name_snapshot", sa.String(length=100), nullable=True),
        sa.Column("area_code_snapshot", sa.String(length=30), nullable=True),
        sa.Column("pallet_code", sa.String(length=100), nullable=True),
        sa.Column("customer_id", sa.Integer(), nullable=True),
        sa.Column("customer_code_snapshot", sa.String(length=100), nullable=True),
        sa.Column("customer_name_snapshot", sa.String(length=200), nullable=True),
        sa.Column("product_id", sa.Integer(), nullable=True),
        sa.Column("inventory_code_snapshot", sa.String(length=150), nullable=True),
        sa.Column("product_name_snapshot", sa.String(length=250), nullable=True),
        sa.Column("material_id", sa.Integer(), nullable=True),
        sa.Column("material_code_snapshot", sa.String(length=100), nullable=True),
        sa.Column("quantity", sa.Integer(), nullable=True),
        sa.Column("unit", sa.String(length=20), nullable=True),
        sa.Column(
            "source_type",
            sa.String(length=30),
            nullable=False,
            server_default=sa.text("'stocktake'"),
        ),
        sa.Column("stock_date", sa.Date(), nullable=True),
        sa.Column("stock_date_accuracy", sa.String(length=20), nullable=True),
        sa.Column("stock_date_original_text", sa.Text(), nullable=True),
        sa.Column("supplier_name", sa.String(length=200), nullable=True),
        sa.Column("layer_count", sa.Integer(), nullable=True),
        sa.Column("flute_type", sa.String(length=20), nullable=True),
        sa.Column("board_length_mm", sa.Integer(), nullable=True),
        sa.Column("board_width_mm", sa.Integer(), nullable=True),
        sa.Column("sheet_type", sa.String(length=30), nullable=True),
        sa.Column("component_type", sa.String(length=20), nullable=True),
        sa.Column("pieces_per_box", sa.Integer(), nullable=True),
        sa.Column("stock_yield_per_sheet", sa.Integer(), nullable=True),
        sa.Column("crease_type", sa.String(length=20), nullable=True),
        sa.Column("crease_left_mm", sa.Integer(), nullable=True),
        sa.Column("crease_middle_mm", sa.Integer(), nullable=True),
        sa.Column("crease_right_mm", sa.Integer(), nullable=True),
        sa.Column("cutting_note", sa.Text(), nullable=True),
        sa.Column("remarks", sa.Text(), nullable=True),
        sa.Column(
            "action_decision",
            sa.String(length=40),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column("existing_lot_id", sa.Integer(), nullable=True),
        sa.Column("existing_pallet_id", sa.Integer(), nullable=True),
        sa.Column(
            "match_status",
            sa.String(length=20),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column(
            "error_codes_json",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
        sa.Column(
            "warning_codes_json",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
        sa.Column(
            "match_evidence_json",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column(
            "version",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column("updated_by", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "version >= 1",
            name="ck_inventory_onboarding_lines_version",
        ),
        sa.CheckConstraint(
            "source_row_number > 0",
            name="ck_inventory_onboarding_lines_source_row",
        ),
        sa.CheckConstraint(
            "length(source_row_hash) = 64",
            name="ck_inventory_onboarding_lines_source_hash",
        ),
        sa.CheckConstraint(
            "inventory_type IS NULL OR "
            "inventory_type IN ('finished','semi_finished')",
            name="ck_inventory_onboarding_lines_inventory_type",
        ),
        sa.CheckConstraint(
            "ownership_type IS NULL OR "
            "ownership_type IN ('customer_specific','general')",
            name="ck_inventory_onboarding_lines_ownership",
        ),
        sa.CheckConstraint(
            "quantity IS NULL OR quantity > 0",
            name="ck_inventory_onboarding_lines_quantity",
        ),
        sa.CheckConstraint(
            "unit IS NULL OR unit IN ('boxes','sheets')",
            name="ck_inventory_onboarding_lines_unit",
        ),
        sa.CheckConstraint(
            "inventory_type IS NULL OR unit IS NULL OR "
            "(inventory_type = 'finished' AND unit = 'boxes') OR "
            "(inventory_type = 'semi_finished' AND unit = 'sheets')",
            name="ck_inventory_onboarding_lines_type_unit",
        ),
        sa.CheckConstraint(
            "source_type = 'stocktake'",
            name="ck_inventory_onboarding_lines_source_type",
        ),
        sa.CheckConstraint(
            "stock_date_accuracy IS NULL OR "
            "stock_date_accuracy IN ('exact','estimated','unknown')",
            name="ck_inventory_onboarding_lines_date_accuracy",
        ),
        sa.CheckConstraint(
            "layer_count IS NULL OR layer_count IN (3,5,7)",
            name="ck_inventory_onboarding_lines_layer_count",
        ),
        sa.CheckConstraint(
            "board_length_mm IS NULL OR board_length_mm > 0",
            name="ck_inventory_onboarding_lines_board_length",
        ),
        sa.CheckConstraint(
            "board_width_mm IS NULL OR board_width_mm > 0",
            name="ck_inventory_onboarding_lines_board_width",
        ),
        sa.CheckConstraint(
            "sheet_type IS NULL OR "
            "sheet_type IN ('raw_board','net_sheet','creased_sheet')",
            name="ck_inventory_onboarding_lines_sheet_type",
        ),
        sa.CheckConstraint(
            "component_type IS NULL OR "
            "component_type IN ('whole','cover','base')",
            name="ck_inventory_onboarding_lines_component",
        ),
        sa.CheckConstraint(
            "pieces_per_box IS NULL OR pieces_per_box > 0",
            name="ck_inventory_onboarding_lines_pieces_per_box",
        ),
        sa.CheckConstraint(
            "stock_yield_per_sheet IS NULL OR stock_yield_per_sheet > 0",
            name="ck_inventory_onboarding_lines_stock_yield",
        ),
        sa.CheckConstraint(
            "action_decision IN ("
            "'pending','create_new','route_n035','route_semi_adjust',"
            "'route_snapshot_conversion','exclude')",
            name="ck_inventory_onboarding_lines_action",
        ),
        sa.CheckConstraint(
            "match_status IN ('pending','ready','blocked','routed','excluded')",
            name="ck_inventory_onboarding_lines_match_status",
        ),
        sa.CheckConstraint(
            "(action_decision NOT IN ('route_n035','route_semi_adjust') "
            "OR existing_lot_id IS NOT NULL) AND "
            "(action_decision != 'route_snapshot_conversion' "
            "OR existing_pallet_id IS NOT NULL)",
            name="ck_inventory_onboarding_lines_route_target",
        ),
        sa.ForeignKeyConstraint(
            ["batch_id"],
            ["inventory_onboarding_batches.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["location_id"], ["warehouse_locations.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["customer_id"], ["customers.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["product_id"], ["products.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["material_id"], ["materials.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["existing_lot_id"], ["inventory_lots.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["existing_pallet_id"],
            ["inventory_pallets.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by"], ["users.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "batch_id",
            "source_sheet_name",
            "source_row_number",
            name="uq_inventory_onboarding_lines_source_row",
        ),
    )
    op.create_index(
        "ix_inventory_onboarding_lines_batch_match",
        LINE_TABLE,
        ["batch_id", "match_status"],
        unique=False,
    )
    op.create_index(
        "ix_inventory_onboarding_lines_batch_action",
        LINE_TABLE,
        ["batch_id", "action_decision"],
        unique=False,
    )
    op.create_index(
        "ix_inventory_onboarding_lines_location",
        LINE_TABLE,
        ["location_id"],
        unique=False,
    )
    op.create_index(
        "ix_inventory_onboarding_lines_customer_product",
        LINE_TABLE,
        ["customer_id", "product_id"],
        unique=False,
    )

    connection = op.get_bind()
    if connection.dialect.name == "sqlite":
        _create_sqlite_guards(connection)


def downgrade() -> None:
    connection = op.get_bind()
    _assert_onboarding_tables_empty(connection)
    if connection.dialect.name == "sqlite":
        _drop_sqlite_guards(connection)
    op.drop_index(
        "ix_inventory_onboarding_lines_customer_product",
        table_name=LINE_TABLE,
    )
    op.drop_index(
        "ix_inventory_onboarding_lines_location",
        table_name=LINE_TABLE,
    )
    op.drop_index(
        "ix_inventory_onboarding_lines_batch_action",
        table_name=LINE_TABLE,
    )
    op.drop_index(
        "ix_inventory_onboarding_lines_batch_match",
        table_name=LINE_TABLE,
    )
    op.drop_table(LINE_TABLE)
    op.drop_index(
        "ix_inventory_onboarding_batches_area_status",
        table_name=BATCH_TABLE,
    )
    op.drop_index(
        "ix_inventory_onboarding_batches_status_created",
        table_name=BATCH_TABLE,
    )
    op.drop_table(BATCH_TABLE)
