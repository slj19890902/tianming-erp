"""Add versioned, immutable mold-location movement confirmation.

Revision ID: bb55v8x9z45
Revises: ba54v8x9z44
"""

from alembic import op
import sqlalchemy as sa


revision = "bb55v8x9z45"
down_revision = "ba54v8x9z44"
branch_labels = None
depends_on = None


IMMUTABLE_UPDATE_TRIGGER = "trg_mold_location_movements_immutable_update"
IMMUTABLE_DELETE_TRIGGER = "trg_mold_location_movements_immutable_delete"
POSTGRES_IMMUTABLE_TRIGGER = "trg_mold_location_movements_immutable"
POSTGRES_IMMUTABLE_FUNCTION = "n022_immutable_mold_location_movement"
LAST_CONFIRMER_FK = "fk_mold_tools_last_location_confirmed_by_users"


def _create_immutable_guards() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        op.execute(
            sa.text(
                f"""
                CREATE TRIGGER {IMMUTABLE_UPDATE_TRIGGER}
                BEFORE UPDATE ON mold_location_movements
                BEGIN
                    SELECT RAISE(ABORT, 'mold_location_movements rows are immutable');
                END
                """
            )
        )
        op.execute(
            sa.text(
                f"""
                CREATE TRIGGER {IMMUTABLE_DELETE_TRIGGER}
                BEFORE DELETE ON mold_location_movements
                BEGIN
                    SELECT RAISE(ABORT, 'mold_location_movements rows are immutable');
                END
                """
            )
        )
    elif dialect == "postgresql":
        op.execute(
            sa.text(
                f"""
                CREATE OR REPLACE FUNCTION {POSTGRES_IMMUTABLE_FUNCTION}()
                RETURNS trigger AS $$
                BEGIN
                    RAISE EXCEPTION 'mold_location_movements rows are immutable';
                END;
                $$ LANGUAGE plpgsql
                """
            )
        )
        op.execute(
            sa.text(
                f"""
                CREATE TRIGGER {POSTGRES_IMMUTABLE_TRIGGER}
                BEFORE UPDATE OR DELETE ON mold_location_movements
                FOR EACH ROW EXECUTE FUNCTION {POSTGRES_IMMUTABLE_FUNCTION}()
                """
            )
        )


def _drop_immutable_guards() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        op.execute(sa.text(f"DROP TRIGGER IF EXISTS {IMMUTABLE_DELETE_TRIGGER}"))
        op.execute(sa.text(f"DROP TRIGGER IF EXISTS {IMMUTABLE_UPDATE_TRIGGER}"))
    elif dialect == "postgresql":
        op.execute(
            sa.text(
                f"DROP TRIGGER IF EXISTS {POSTGRES_IMMUTABLE_TRIGGER} "
                "ON mold_location_movements"
            )
        )
        op.execute(
            sa.text(f"DROP FUNCTION IF EXISTS {POSTGRES_IMMUTABLE_FUNCTION}()")
        )


def upgrade() -> None:
    with op.batch_alter_table("mold_tools", recreate="always") as batch_op:
        batch_op.add_column(
            sa.Column(
                "location_version",
                sa.Integer(),
                nullable=False,
                server_default="1",
            )
        )
        batch_op.add_column(
            sa.Column("last_location_confirmed_at", sa.DateTime(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("last_location_confirmed_by", sa.Integer(), nullable=True)
        )
        batch_op.create_foreign_key(
            LAST_CONFIRMER_FK,
            "users",
            ["last_location_confirmed_by"],
            ["id"],
            ondelete="SET NULL",
        )

    op.create_table(
        "mold_location_movements",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("mold_tool_id", sa.Integer(), nullable=False),
        sa.Column("mold_code_snapshot", sa.String(length=100), nullable=False),
        sa.Column("from_location", sa.String(length=250), nullable=False),
        sa.Column("to_location", sa.String(length=250), nullable=False),
        sa.Column("actor_id", sa.Integer(), nullable=True),
        sa.Column(
            "moved_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("idempotency_key", sa.String(length=120), nullable=False),
        sa.Column("expected_version", sa.Integer(), nullable=False),
        sa.Column("resulting_version", sa.Integer(), nullable=False),
        sa.Column(
            "source",
            sa.String(length=30),
            nullable=False,
            server_default="manual_input",
        ),
        sa.Column("note", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "expected_version >= 1",
            name="ck_mold_location_movements_expected_version",
        ),
        sa.CheckConstraint(
            "resulting_version = expected_version + 1",
            name="ck_mold_location_movements_resulting_version",
        ),
        sa.CheckConstraint(
            "from_location <> to_location",
            name="ck_mold_location_movements_actual_change",
        ),
        sa.ForeignKeyConstraint(
            ["mold_tool_id"],
            ["mold_tools.id"],
            name="fk_mold_location_movements_mold_tool",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["actor_id"],
            ["users.id"],
            name="fk_mold_location_movements_actor",
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_mold_location_movements_idempotency_key",
        ),
    )
    op.create_index(
        "ix_mold_location_movements_mold_time",
        "mold_location_movements",
        ["mold_tool_id", "moved_at"],
    )
    _create_immutable_guards()


def downgrade() -> None:
    connection = op.get_bind()
    movement_count = connection.execute(
        sa.text("SELECT COUNT(*) FROM mold_location_movements")
    ).scalar_one()
    changed_mold_count = connection.execute(
        sa.text(
            """
            SELECT COUNT(*)
            FROM mold_tools
            WHERE location_version <> 1
               OR last_location_confirmed_at IS NOT NULL
               OR last_location_confirmed_by IS NOT NULL
            """
        )
    ).scalar_one()
    if movement_count or changed_mold_count:
        raise RuntimeError(
            "模具位置移动事实或确认版本已经产生，禁止破坏性降级；"
            "请停止服务并恢复 bb55 升级前的完整数据库备份。"
        )

    _drop_immutable_guards()
    op.drop_index(
        "ix_mold_location_movements_mold_time",
        table_name="mold_location_movements",
    )
    op.drop_table("mold_location_movements")

    with op.batch_alter_table("mold_tools", recreate="always") as batch_op:
        batch_op.drop_constraint(LAST_CONFIRMER_FK, type_="foreignkey")
        batch_op.drop_column("last_location_confirmed_by")
        batch_op.drop_column("last_location_confirmed_at")
        batch_op.drop_column("location_version")
