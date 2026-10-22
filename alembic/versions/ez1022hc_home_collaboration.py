"""Personal memos and shared file versions; no historical backfill."""
from alembic import op
import sqlalchemy as sa

revision = "ez1022hc"
down_revision = "ey1021se"
branch_labels = None
depends_on = None


def user_column(name):
    return sa.Column(name, sa.Integer(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)


def upgrade():
    op.create_table("home_memos",
        sa.Column("id", sa.String(36), primary_key=True), user_column("owner_id"),
        sa.Column("text", sa.String(600), nullable=False),
        sa.Column("completed", sa.Boolean(), nullable=False), sa.Column("deleted", sa.Boolean(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False), sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("version > 0", name="ck_home_memo_version"))
    op.create_index("ix_home_memo_owner_state", "home_memos", ["owner_id", "deleted", "completed", "updated_at"])
    op.create_table("shared_files",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("name", sa.String(255), nullable=False),
        user_column("owner_id"), user_column("updated_by"),
        sa.Column("version", sa.Integer(), nullable=False), sa.Column("content_version", sa.Integer(), nullable=False),
        sa.Column("deleted", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False), sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("version > 0", name="ck_shared_file_version"))
    op.create_index("ix_shared_file_deleted_updated", "shared_files", ["deleted", "updated_at"])
    op.create_table("shared_file_versions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("file_id", sa.String(36), sa.ForeignKey("shared_files.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False), sa.Column("reference", sa.String(255), nullable=False, unique=True),
        sa.Column("original_name", sa.String(255), nullable=False), sa.Column("content_type", sa.String(120), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False), sa.Column("sha256", sa.String(64), nullable=False),
        user_column("actor_id"), sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("file_id", "version", name="uq_shared_file_version"),
        sa.CheckConstraint("version > 0 AND size > 0", name="ck_shared_file_content"))
    op.create_table("home_collaboration_mutations",
        sa.Column("id", sa.Integer(), primary_key=True), user_column("actor_id"),
        sa.Column("request_key", sa.String(80), nullable=False), sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("response_json", sa.Text(), nullable=False), sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("actor_id", "request_key", name="uq_home_collaboration_request"))
    if op.get_bind().dialect.name == "sqlite":
        for table in ("shared_file_versions", "home_collaboration_mutations"):
            for action in ("UPDATE", "DELETE"):
                op.execute(f"CREATE TRIGGER {table}_no_{action.lower()} BEFORE {action} ON {table} "
                           "BEGIN SELECT RAISE(ABORT, 'shared file and request history is immutable'); END")


def downgrade():
    tables = ("home_memos", "shared_files", "shared_file_versions", "home_collaboration_mutations")
    for table in tables:
        if op.get_bind().execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first():
            raise RuntimeError("已有待办或共享文件事实，禁止有损降级；保留数据库向前修复")
    if op.get_bind().dialect.name == "sqlite":
        for table in ("shared_file_versions", "home_collaboration_mutations"):
            for action in ("update", "delete"):
                op.execute(f"DROP TRIGGER {table}_no_{action}")
    for table in reversed(tables):
        op.drop_table(table)
