"""add standard processing-time rules and wage expense categories

Revision ID: iz61v8x9z50
Revises: iy60v8x9z49
Create Date: 2026-09-01

The singleton row contains adjustable standards only.  It does not create
actual wage facts or rewrite immutable order-cost snapshots.  Updating the
SQLite category check requires a table-copy DDL operation, but every existing
cost-pool column value is copied unchanged.
"""

from __future__ import annotations

from decimal import Decimal

from alembic import op
import sqlalchemy as sa


revision = "iz61v8x9z50"
down_revision = "iy60v8x9z49"
branch_labels = None
depends_on = None


COST_TABLE = "finance_cost_pool_entries"
SNAPSHOT_TABLE = "sales_order_item_estimated_cost_snapshots"
COST_CATEGORY_CHECK = "ck_finance_cost_pool_entries_category"
OLD_COST_CATEGORIES = (
    "cost_category IN ("
    "'outsourcing','inbound_freight',"
    "'production_wages','factory_utilities','factory_rent','maintenance',"
    "'delivery_freight','sales_expense','administrative_wages',"
    "'administrative_expense','finance_expense','tax_fee','other')"
)
NEW_COST_CATEGORIES = (
    "cost_category IN ("
    "'outsourcing','inbound_freight',"
    "'production_wages','factory_utilities','factory_rent','maintenance',"
    "'delivery_freight','driver_wages','sales_expense','administrative_wages',"
    "'administrative_expense','finance_expense','finance_wages','tax_fee','other')"
)


def _replace_cost_category_check(expression: str) -> None:
    connection = op.get_bind()
    if connection.dialect.name == "sqlite":
        with op.batch_alter_table(COST_TABLE, recreate="always") as batch_op:
            batch_op.drop_constraint(COST_CATEGORY_CHECK, type_="check")
            batch_op.create_check_constraint(COST_CATEGORY_CHECK, expression)
    else:
        op.drop_constraint(COST_CATEGORY_CHECK, COST_TABLE, type_="check")
        op.create_check_constraint(COST_CATEGORY_CHECK, COST_TABLE, expression)


def _drop_snapshot_guard() -> None:
    connection = op.get_bind()
    if connection.dialect.name == "sqlite":
        for action in ("delete", "update"):
            op.execute(
                f"DROP TRIGGER IF EXISTS trg_{SNAPSHOT_TABLE}_immutable_{action}"
            )
    elif connection.dialect.name == "postgresql":
        op.execute(
            f"DROP TRIGGER IF EXISTS trg_{SNAPSHOT_TABLE}_immutable_write "
            f"ON {SNAPSHOT_TABLE}"
        )


def _create_snapshot_guard() -> None:
    connection = op.get_bind()
    if connection.dialect.name == "sqlite":
        for action in ("UPDATE", "DELETE"):
            op.execute(
                f"""
                CREATE TRIGGER trg_{SNAPSHOT_TABLE}_immutable_{action.lower()}
                BEFORE {action} ON {SNAPSHOT_TABLE}
                FOR EACH ROW BEGIN
                    SELECT RAISE(ABORT, '{SNAPSHOT_TABLE} rows are immutable');
                END
                """
            )
    elif connection.dialect.name == "postgresql":
        op.execute(
            f"""
            CREATE TRIGGER trg_{SNAPSHOT_TABLE}_immutable_write
            BEFORE UPDATE OR DELETE ON {SNAPSHOT_TABLE}
            FOR EACH ROW EXECUTE FUNCTION p1_28c1_immutable_estimated_cost_snapshot()
            """
        )


def _set_processing_cost_nullability(*, nullable: bool) -> None:
    connection = op.get_bind()
    _drop_snapshot_guard()
    recreate = "always" if connection.dialect.name == "sqlite" else "auto"
    with op.batch_alter_table(SNAPSHOT_TABLE, recreate=recreate) as batch_op:
        batch_op.alter_column(
            "processing_unit_cost",
            existing_type=sa.Numeric(18, 6),
            nullable=nullable,
        )
        batch_op.alter_column(
            "processing_total_cost",
            existing_type=sa.Numeric(18, 2),
            nullable=nullable,
        )
    _create_snapshot_guard()


def upgrade() -> None:
    op.create_table(
        "processing_cost_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("working_hours_per_day", sa.Numeric(8, 4), nullable=False),
        sa.Column("working_days_per_month", sa.Numeric(8, 4), nullable=False),
        sa.Column("default_printer", sa.String(10), nullable=False),
        sa.Column(
            "new_printer_normal_sheets_per_minute", sa.Numeric(12, 4), nullable=False
        ),
        sa.Column(
            "new_printer_max_sheets_per_minute", sa.Numeric(12, 4), nullable=False
        ),
        sa.Column(
            "old_printer_normal_sheets_per_minute", sa.Numeric(12, 4), nullable=False
        ),
        sa.Column(
            "old_printer_max_sheets_per_minute", sa.Numeric(12, 4), nullable=False
        ),
        sa.Column("printing_setup_minutes", sa.Numeric(12, 4), nullable=False),
        sa.Column("printing_crew_size", sa.Integer(), nullable=False),
        sa.Column("colors_per_pass", sa.Integer(), nullable=False),
        sa.Column("die_setup_minutes", sa.Numeric(12, 4), nullable=False),
        sa.Column(
            "small_die_normal_pieces_per_minute", sa.Numeric(12, 4), nullable=False
        ),
        sa.Column(
            "small_die_max_pieces_per_minute", sa.Numeric(12, 4), nullable=False
        ),
        sa.Column("small_die_crew_size", sa.Integer(), nullable=False),
        sa.Column("small_complex_die_crew_size", sa.Integer(), nullable=False),
        sa.Column("large_die_pieces_per_minute", sa.Numeric(12, 4), nullable=False),
        sa.Column("large_die_crew_size", sa.Integer(), nullable=False),
        sa.Column("oversize_die_seconds_per_piece", sa.Numeric(12, 4), nullable=False),
        sa.Column("oversize_die_crew_size", sa.Integer(), nullable=False),
        sa.Column(
            "joining_normal_pieces_per_second", sa.Numeric(12, 4), nullable=False
        ),
        sa.Column(
            "joining_max_pieces_per_second", sa.Numeric(12, 4), nullable=False
        ),
        sa.Column("double_splice_seconds_per_piece", sa.Numeric(12, 4), nullable=False),
        sa.Column("joining_crew_size", sa.Integer(), nullable=False),
        sa.Column("average_worker_monthly_salary", sa.Numeric(14, 2), nullable=True),
        sa.Column(
            "average_worker_monthly_social_cost",
            sa.Numeric(14, 2),
            nullable=False,
            server_default="0",
        ),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("updated_by", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint("id = 1", name="ck_processing_cost_settings_singleton"),
        sa.CheckConstraint("version >= 1", name="ck_processing_cost_settings_version"),
        sa.CheckConstraint(
            "default_printer IN ('new','old')",
            name="ck_processing_cost_settings_default_printer",
        ),
        sa.CheckConstraint(
            "working_hours_per_day > 0 AND working_hours_per_day <= 24",
            name="ck_processing_cost_settings_working_hours",
        ),
        sa.CheckConstraint(
            "working_days_per_month > 0 AND working_days_per_month <= 31",
            name="ck_processing_cost_settings_working_days",
        ),
        sa.CheckConstraint(
            "new_printer_normal_sheets_per_minute > 0 "
            "AND new_printer_max_sheets_per_minute >= new_printer_normal_sheets_per_minute",
            name="ck_processing_cost_settings_new_printer_speed",
        ),
        sa.CheckConstraint(
            "old_printer_normal_sheets_per_minute > 0 "
            "AND old_printer_max_sheets_per_minute >= old_printer_normal_sheets_per_minute",
            name="ck_processing_cost_settings_old_printer_speed",
        ),
        sa.CheckConstraint(
            "printing_setup_minutes >= 0 AND printing_setup_minutes <= 1440",
            name="ck_processing_cost_settings_printing_setup",
        ),
        sa.CheckConstraint(
            "printing_crew_size > 0 AND printing_crew_size <= 100",
            name="ck_processing_cost_settings_printing_crew",
        ),
        sa.CheckConstraint(
            "colors_per_pass > 0 AND colors_per_pass <= 10",
            name="ck_processing_cost_settings_colors_per_pass",
        ),
        sa.CheckConstraint(
            "die_setup_minutes >= 0 AND die_setup_minutes <= 1440",
            name="ck_processing_cost_settings_die_setup",
        ),
        sa.CheckConstraint(
            "small_die_normal_pieces_per_minute > 0 "
            "AND small_die_max_pieces_per_minute >= small_die_normal_pieces_per_minute",
            name="ck_processing_cost_settings_small_die_speed",
        ),
        sa.CheckConstraint(
            "small_die_crew_size > 0 AND small_die_crew_size <= 100 "
            "AND small_complex_die_crew_size > 0 AND small_complex_die_crew_size <= 100 "
            "AND large_die_crew_size > 0 AND large_die_crew_size <= 100 "
            "AND oversize_die_crew_size > 0 AND oversize_die_crew_size <= 100",
            name="ck_processing_cost_settings_die_crews",
        ),
        sa.CheckConstraint(
            "large_die_pieces_per_minute > 0 "
            "AND oversize_die_seconds_per_piece > 0",
            name="ck_processing_cost_settings_large_die_speeds",
        ),
        sa.CheckConstraint(
            "joining_normal_pieces_per_second > 0 "
            "AND joining_max_pieces_per_second >= joining_normal_pieces_per_second",
            name="ck_processing_cost_settings_joining_speed",
        ),
        sa.CheckConstraint(
            "double_splice_seconds_per_piece > 0",
            name="ck_processing_cost_settings_double_splice_speed",
        ),
        sa.CheckConstraint(
            "joining_crew_size > 0 AND joining_crew_size <= 100",
            name="ck_processing_cost_settings_joining_crew",
        ),
        sa.CheckConstraint(
            "average_worker_monthly_salary IS NULL "
            "OR average_worker_monthly_salary > 0",
            name="ck_processing_cost_settings_salary",
        ),
        sa.CheckConstraint(
            "average_worker_monthly_social_cost >= 0",
            name="ck_processing_cost_settings_social_cost",
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"], ondelete="SET NULL"),
    )
    op.execute(
        sa.text(
            """
            INSERT INTO processing_cost_settings (
                id, working_hours_per_day, working_days_per_month, default_printer,
                new_printer_normal_sheets_per_minute,
                new_printer_max_sheets_per_minute,
                old_printer_normal_sheets_per_minute,
                old_printer_max_sheets_per_minute,
                printing_setup_minutes, printing_crew_size, colors_per_pass,
                die_setup_minutes, small_die_normal_pieces_per_minute,
                small_die_max_pieces_per_minute, small_die_crew_size,
                small_complex_die_crew_size, large_die_pieces_per_minute,
                large_die_crew_size, oversize_die_seconds_per_piece,
                oversize_die_crew_size, joining_normal_pieces_per_second,
                joining_max_pieces_per_second, double_splice_seconds_per_piece,
                joining_crew_size, average_worker_monthly_salary,
                average_worker_monthly_social_cost, version
            ) VALUES (
                1, 8, 26, 'new', 90, 120, 30, 60, 30, 2, 2,
                30, 60, 120, 2, 3, 50, 2, 120, 2, 1, 3, 30, 2,
                NULL, 0, 1
            )
            """
        )
    )

    op.create_table(
        "product_processing_profiles",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("printer_mode", sa.String(10), nullable=False, server_default="auto"),
        sa.Column("die_cut_mode", sa.String(20), nullable=False, server_default="auto"),
        sa.Column("assembly_worker_days_per_1000", sa.Numeric(14, 6), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("updated_by", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "printer_mode IN ('auto','new','old','none')",
            name="ck_product_processing_profiles_printer_mode",
        ),
        sa.CheckConstraint(
            "die_cut_mode IN "
            "('auto','none','small_normal','small_complex','large','oversize')",
            name="ck_product_processing_profiles_die_cut_mode",
        ),
        sa.CheckConstraint(
            "assembly_worker_days_per_1000 IS NULL "
            "OR assembly_worker_days_per_1000 > 0",
            name="ck_product_processing_profiles_assembly_days",
        ),
        sa.CheckConstraint(
            "version >= 1", name="ck_product_processing_profiles_version"
        ),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint(
            "product_id", name="uq_product_processing_profiles_product"
        ),
    )
    op.create_index(
        "ix_product_processing_profiles_product_id",
        "product_processing_profiles",
        ["product_id"],
    )

    _set_processing_cost_nullability(nullable=True)
    _replace_cost_category_check(NEW_COST_CATEGORIES)


def downgrade() -> None:
    connection = op.get_bind()
    null_processing_snapshots = int(
        connection.execute(
            sa.text(
                f"SELECT COUNT(*) FROM {SNAPSHOT_TABLE} "
                "WHERE processing_unit_cost IS NULL OR processing_total_cost IS NULL"
            )
        ).scalar_one()
    )
    if null_processing_snapshots:
        raise RuntimeError(
            "P1-131 downgrade blocked: incomplete processing-cost snapshots exist."
        )
    profile_count = int(
        connection.execute(
            sa.text("SELECT COUNT(*) FROM product_processing_profiles")
        ).scalar_one()
    )
    if profile_count:
        raise RuntimeError(
            "P1-131 downgrade blocked: product processing profiles already exist."
        )
    settings = connection.execute(
        sa.text(
            """
            SELECT * FROM processing_cost_settings WHERE id = 1
            """
        )
    ).mappings().one_or_none()
    numeric_defaults = {
        "working_hours_per_day": Decimal("8"),
        "working_days_per_month": Decimal("26"),
        "new_printer_normal_sheets_per_minute": Decimal("90"),
        "new_printer_max_sheets_per_minute": Decimal("120"),
        "old_printer_normal_sheets_per_minute": Decimal("30"),
        "old_printer_max_sheets_per_minute": Decimal("60"),
        "printing_setup_minutes": Decimal("30"),
        "die_setup_minutes": Decimal("30"),
        "small_die_normal_pieces_per_minute": Decimal("60"),
        "small_die_max_pieces_per_minute": Decimal("120"),
        "large_die_pieces_per_minute": Decimal("50"),
        "oversize_die_seconds_per_piece": Decimal("120"),
        "joining_normal_pieces_per_second": Decimal("1"),
        "joining_max_pieces_per_second": Decimal("3"),
        "double_splice_seconds_per_piece": Decimal("30"),
        "average_worker_monthly_social_cost": Decimal("0"),
    }
    scalar_defaults = {
        "default_printer": "new",
        "printing_crew_size": 2,
        "colors_per_pass": 2,
        "small_die_crew_size": 2,
        "small_complex_die_crew_size": 3,
        "large_die_crew_size": 2,
        "oversize_die_crew_size": 2,
        "joining_crew_size": 2,
        "version": 1,
    }
    settings_changed = settings is None
    if settings is not None:
        settings_changed = any(
            Decimal(str(settings[name])) != expected
            for name, expected in numeric_defaults.items()
        ) or any(settings[name] != expected for name, expected in scalar_defaults.items())
        settings_changed = settings_changed or any(
            settings[name] is not None
            for name in (
                "average_worker_monthly_salary",
                "created_by",
                "updated_by",
                "updated_at",
            )
        )
    if settings_changed:
        raise RuntimeError(
            "P1-131 downgrade blocked: processing settings differ from the seeded default."
        )
    new_category_count = int(
        connection.execute(
            sa.text(
                "SELECT COUNT(*) FROM finance_cost_pool_entries "
                "WHERE cost_category IN ('driver_wages','finance_wages')"
            )
        ).scalar_one()
    )
    if new_category_count:
        raise RuntimeError(
            "P1-131 downgrade blocked: driver or finance wage entries already exist."
        )
    _replace_cost_category_check(OLD_COST_CATEGORIES)
    _set_processing_cost_nullability(nullable=False)
    op.drop_index(
        "ix_product_processing_profiles_product_id",
        table_name="product_processing_profiles",
    )
    op.drop_table("product_processing_profiles")
    op.drop_table("processing_cost_settings")
