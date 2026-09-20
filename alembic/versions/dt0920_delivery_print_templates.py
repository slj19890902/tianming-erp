"""Add versioned delivery print templates and immutable delivery snapshots."""
from alembic import op
import sqlalchemy as sa

revision = "dt0920"
down_revision = "dc0920"
branch_labels = None
depends_on = None


DELIVERY_COLUMNS = (
    ("print_template_profile_key", sa.String(length=80)),
    ("print_template_version", sa.Integer()),
    ("print_template_payload_json", sa.Text()),
    ("print_template_payload_hash", sa.String(length=64)),
)


def upgrade():
    op.create_table(
        "delivery_print_template_revisions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("profile_key", sa.String(length=80), nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=True),
        sa.Column("stream", sa.String(length=10), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("catalog_version", sa.String(length=40), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("base_release_version", sa.Integer(), nullable=False),
        sa.Column("operation_kind", sa.String(length=20), nullable=False),
        sa.Column("operation_key", sa.String(length=120), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("source_release_version", sa.Integer(), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.func.current_timestamp(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "stream IN ('draft','release')",
            name="ck_delivery_print_template_stream",
        ),
        sa.CheckConstraint(
            "operation_kind IN ('save_draft','publish','rollback')",
            name="ck_delivery_print_template_operation_kind",
        ),
        sa.CheckConstraint(
            "version >= 1 AND base_release_version >= 0",
            name="ck_delivery_print_template_versions",
        ),
        sa.CheckConstraint(
            "length(payload_hash) = 64 AND length(request_hash) = 64",
            name="ck_delivery_print_template_hashes",
        ),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint(
            "profile_key",
            "stream",
            "version",
            name="uq_delivery_print_template_profile_stream_version",
        ),
        sa.UniqueConstraint(
            "operation_key",
            name="uq_delivery_print_template_operation_key",
        ),
    )
    op.create_index(
        "ix_delivery_print_template_profile_stream_version",
        "delivery_print_template_revisions",
        ["profile_key", "stream", "version"],
    )
    for name, column_type in DELIVERY_COLUMNS:
        op.add_column(
            "sales_deliveries",
            sa.Column(name, column_type, nullable=True),
        )


def downgrade():
    db = op.get_bind()
    if db.execute(sa.text(
        "SELECT 1 FROM sales_deliveries "
        "WHERE print_template_version IS NOT NULL LIMIT 1"
    )).first() or db.execute(sa.text(
        "SELECT 1 FROM delivery_print_template_revisions LIMIT 1"
    )).first():
        raise RuntimeError("已有送货打印模板事实，禁止删除；请使用已验证恢复备份")
    with op.batch_alter_table("sales_deliveries") as batch:
        for name, _column_type in reversed(DELIVERY_COLUMNS):
            batch.drop_column(name)
    op.drop_index(
        "ix_delivery_print_template_profile_stream_version",
        table_name="delivery_print_template_revisions",
    )
    op.drop_table("delivery_print_template_revisions")
