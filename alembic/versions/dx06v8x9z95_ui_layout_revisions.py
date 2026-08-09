"""Q2-02C1 append-only administrator UI layout revisions.

Revision ID: dx06v8x9z95
Revises: dw05v8x9z94
"""

from alembic import op
import sqlalchemy as sa


revision = "dx06v8x9z95"
down_revision = "dw05v8x9z94"
branch_labels = None
depends_on = None

TABLE = "ui_layout_revisions"


def _create_immutable_guard() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        for action in ("UPDATE", "DELETE"):
            op.execute(
                f"""
                CREATE TRIGGER trg_{TABLE}_immutable_{action.lower()}
                BEFORE {action} ON {TABLE}
                FOR EACH ROW BEGIN
                    SELECT RAISE(ABORT, '{TABLE} rows are immutable');
                END
                """
            )
    elif dialect == "postgresql":
        op.execute(
            """
            CREATE OR REPLACE FUNCTION q2_02c1_immutable_ui_layout_revision()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                RAISE EXCEPTION 'ui layout revisions are immutable';
            END;
            $$
            """
        )
        op.execute(
            f"""CREATE TRIGGER trg_{TABLE}_immutable_write
            BEFORE UPDATE OR DELETE ON {TABLE}
            FOR EACH ROW EXECUTE FUNCTION q2_02c1_immutable_ui_layout_revision()"""
        )


def _drop_immutable_guard() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        for action in ("delete", "update"):
            op.execute(f"DROP TRIGGER IF EXISTS trg_{TABLE}_immutable_{action}")
    elif dialect == "postgresql":
        op.execute(f"DROP TRIGGER IF EXISTS trg_{TABLE}_immutable_write ON {TABLE}")
        op.execute("DROP FUNCTION IF EXISTS q2_02c1_immutable_ui_layout_revision()")


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("role_code", sa.String(20), nullable=False),
        sa.Column("display_mode", sa.String(20), nullable=False),
        sa.Column("stream", sa.String(10), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("catalog_version", sa.String(40), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("base_release_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("operation_kind", sa.String(30), nullable=False),
        sa.Column("operation_key", sa.String(64), nullable=True),
        sa.Column("request_fingerprint", sa.String(64), nullable=True),
        sa.Column("source_release_version", sa.Integer(), nullable=True),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
        sa.UniqueConstraint("role_code", "display_mode", "stream", "version", name="uq_ui_layout_revision_stream_version"),
        sa.UniqueConstraint("operation_key", name="uq_ui_layout_revision_operation_key"),
        sa.CheckConstraint("role_code IN ('admin','finance','sales','workshop','boss')", name="ck_ui_layout_revision_role"),
        sa.CheckConstraint("display_mode IN ('standard','large','mobile')", name="ck_ui_layout_revision_display_mode"),
        sa.CheckConstraint("stream IN ('draft','release')", name="ck_ui_layout_revision_stream"),
        sa.CheckConstraint("version > 0", name="ck_ui_layout_revision_version"),
        sa.CheckConstraint("base_release_version >= 0", name="ck_ui_layout_revision_base_release_version"),
    )
    op.create_index(
        "ix_ui_layout_revisions_profile_stream_version",
        TABLE,
        ["role_code", "display_mode", "stream", "version"],
    )
    _create_immutable_guard()


def downgrade() -> None:
    count = int(op.get_bind().execute(sa.text(f"SELECT COUNT(*) FROM {TABLE}")).scalar_one() or 0)
    if count:
        raise RuntimeError("Q2-02C1 已存在界面布局版本，禁止破坏性降级；请恢复升级前备份")
    _drop_immutable_guard()
    op.drop_index("ix_ui_layout_revisions_profile_stream_version", table_name=TABLE)
    op.drop_table(TABLE)
