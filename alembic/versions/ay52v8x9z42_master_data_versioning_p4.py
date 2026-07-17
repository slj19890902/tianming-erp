"""Add append-only master-data object versioning.

Revision ID: ay52v8x9z42
Revises: ax51v8x9z41
"""

from collections.abc import Iterable

from alembic import op
import sqlalchemy as sa


revision = "ay52v8x9z42"
down_revision = "ax51v8x9z41"
branch_labels = None
depends_on = None


DOWNGRADE_BLOCKED_MESSAGE = (
    "主数据版本账本已有事实记录或当前对象版本不为 1，"
    "拒绝降级以避免历史丢失"
)
IMMUTABLE_UPDATE_TRIGGER = "trg_master_data_object_versions_immutable_update"
IMMUTABLE_DELETE_TRIGGER = "trg_master_data_object_versions_immutable_delete"
IMMUTABLE_UPDATE_MESSAGE = "master_data_object_versions is append-only; UPDATE is forbidden"
IMMUTABLE_DELETE_MESSAGE = "master_data_object_versions is append-only; DELETE is forbidden"


def _create_immutability_triggers() -> None:
    op.execute(
        f"""
        CREATE TRIGGER {IMMUTABLE_UPDATE_TRIGGER}
        BEFORE UPDATE ON master_data_object_versions
        FOR EACH ROW
        BEGIN
            SELECT RAISE(ABORT, '{IMMUTABLE_UPDATE_MESSAGE}');
        END
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER {IMMUTABLE_DELETE_TRIGGER}
        BEFORE DELETE ON master_data_object_versions
        FOR EACH ROW
        BEGIN
            SELECT RAISE(ABORT, '{IMMUTABLE_DELETE_MESSAGE}');
        END
        """
    )


def _drop_immutability_triggers() -> None:
    op.execute(f"DROP TRIGGER IF EXISTS {IMMUTABLE_UPDATE_TRIGGER}")
    op.execute(f"DROP TRIGGER IF EXISTS {IMMUTABLE_DELETE_TRIGGER}")


def _add_version_column(table_name: str, constraint_name: str) -> None:
    with op.batch_alter_table(table_name, schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "version",
                sa.Integer(),
                server_default="1",
                nullable=False,
            )
        )
        batch_op.create_check_constraint(constraint_name, "version >= 1")


def _drop_version_column(table_name: str, constraint_name: str) -> None:
    with op.batch_alter_table(table_name, schema=None) as batch_op:
        batch_op.drop_constraint(constraint_name, type_="check")
        batch_op.drop_column("version")


def upgrade() -> None:
    _add_version_column("customers", "ck_customers_version")
    _add_version_column("products", "ck_products_version")
    _add_version_column("materials", "ck_materials_version")

    op.create_table(
        "master_data_object_versions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("object_type", sa.String(20), nullable=False),
        sa.Column("object_id", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(30), nullable=False),
        sa.Column(
            "snapshot_schema_version",
            sa.Integer(),
            server_default="1",
            nullable=False,
        ),
        sa.Column("snapshot_json", sa.Text(), nullable=False),
        sa.Column("snapshot_sha256", sa.String(64), nullable=False),
        sa.Column(
            "changed_fields_json",
            sa.Text(),
            server_default="{}",
            nullable=False,
        ),
        sa.Column("restored_from_version", sa.Integer(), nullable=True),
        sa.Column("change_set_id", sa.String(36), nullable=False),
        sa.Column("operation_log_id", sa.Integer(), nullable=True),
        sa.Column("actor_user_id", sa.Integer(), nullable=True),
        sa.Column("actor_username_snapshot", sa.String(50), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("source", sa.String(100), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.func.current_timestamp(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "object_type IN ('customer', 'product', 'material')",
            name="ck_master_data_object_versions_object_type",
        ),
        sa.CheckConstraint(
            "version >= 1",
            name="ck_master_data_object_versions_version",
        ),
        sa.CheckConstraint(
            "snapshot_schema_version >= 1",
            name="ck_master_data_object_versions_snapshot_schema_version",
        ),
        sa.CheckConstraint(
            "restored_from_version IS NULL OR "
            "(restored_from_version >= 1 AND restored_from_version < version)",
            name="ck_master_data_object_versions_restored_from_version",
        ),
        sa.CheckConstraint(
            "length(snapshot_sha256) = 64",
            name="ck_master_data_object_versions_snapshot_sha256",
        ),
        sa.ForeignKeyConstraint(
            ["operation_log_id"],
            ["operation_logs.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["users.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "object_type",
            "object_id",
            "version",
            name="uq_master_data_object_versions_object_version",
        ),
    )
    op.create_index(
        "ix_master_data_object_versions_object_created",
        "master_data_object_versions",
        ["object_type", "object_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_master_data_object_versions_change_set_id",
        "master_data_object_versions",
        ["change_set_id"],
        unique=False,
    )
    op.create_index(
        "ix_master_data_object_versions_operation_log_id",
        "master_data_object_versions",
        ["operation_log_id"],
        unique=False,
    )
    op.create_index(
        "ix_master_data_object_versions_actor_user_id",
        "master_data_object_versions",
        ["actor_user_id"],
        unique=False,
    )
    _create_immutability_triggers()


def _non_initial_version_count(
    connection: sa.engine.Connection,
    table_names: Iterable[str],
) -> int:
    return sum(
        int(
            connection.execute(
                sa.text(
                    f"SELECT COUNT(*) FROM {table_name} "
                    "WHERE version IS NULL OR version <> 1"
                )
            ).scalar_one()
            or 0
        )
        for table_name in table_names
    )


def downgrade() -> None:
    # This guard intentionally runs before the first DDL statement.  A version
    # row or a bumped entity version is a business fact that cannot be safely
    # represented by the previous schema.
    connection = op.get_bind()
    ledger_count = int(
        connection.execute(
            sa.text("SELECT COUNT(*) FROM master_data_object_versions")
        ).scalar_one()
        or 0
    )
    non_initial_count = _non_initial_version_count(
        connection,
        ("customers", "products", "materials"),
    )
    if ledger_count > 0 or non_initial_count > 0:
        raise RuntimeError(DOWNGRADE_BLOCKED_MESSAGE)

    _drop_immutability_triggers()
    op.drop_table("master_data_object_versions")
    _drop_version_column("materials", "ck_materials_version")
    _drop_version_column("products", "ck_products_version")
    _drop_version_column("customers", "ck_customers_version")
