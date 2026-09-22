"""Managed V2 drawing drafts, immutable releases and task references."""
from alembic import op
import sqlalchemy as sa

revision = "rt21v8x9z68"
down_revision = "rs08v8x9z67"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("drawing_designs",
        sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("template_key", sa.String(50), nullable=False),
        sa.Column("parameters_json", sa.Text(), nullable=False),
        sa.Column("print_objects_json", sa.Text(), nullable=False),
        sa.Column("paper_color", sa.String(20), nullable=False),
        sa.Column("thickness_mm", sa.String(30)),
        sa.Column("thickness_source", sa.String(160)),
        sa.Column("thickness_approximate", sa.Integer(), nullable=False),
        sa.Column("customer_number", sa.String(160)),
        sa.Column("customer_revision", sa.String(50)),
        sa.Column("internal_number", sa.String(30), unique=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("source_product_version", sa.Integer(), nullable=False),
        sa.Column("last_save_key", sa.String(100)),
        sa.Column("last_save_hash", sa.String(64)),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False),
        sa.CheckConstraint("version >= 1", name="ck_drawing_design_version"))
    op.create_table("drawing_number_sequences",
        sa.Column("business_date", sa.String(8), primary_key=True),
        sa.Column("last_number", sa.Integer(), nullable=False))
    op.create_table("drawing_releases",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("customer_id", sa.Integer(), sa.ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("revision", sa.String(50), nullable=False),
        sa.Column("external_number", sa.String(160), nullable=False),
        sa.Column("internal_number", sa.String(30), nullable=False),
        sa.Column("idempotency_key", sa.String(100), nullable=False),
        sa.Column("design_version", sa.Integer(), nullable=False),
        sa.Column("product_version", sa.Integer(), nullable=False),
        sa.Column("template_key", sa.String(50), nullable=False),
        sa.Column("manifest_json", sa.Text(), nullable=False),
        sa.Column("pdf_reference", sa.Text(), nullable=False),
        sa.Column("pdf_sha256", sa.String(64), nullable=False),
        sa.Column("mold_tool_id", sa.Integer(), sa.ForeignKey("mold_tools.id", ondelete="RESTRICT")),
        sa.Column("published_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("published_at", sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False),
        sa.UniqueConstraint("product_id", "external_number", "revision", name="uq_drawing_release_product_revision"),
        sa.UniqueConstraint("product_id", "design_version", "product_version", name="uq_drawing_release_source_version"),
        sa.UniqueConstraint("product_id", "idempotency_key", name="uq_drawing_release_idempotency"))
    op.create_index("ix_drawing_release_customer_product", "drawing_releases", ["customer_id", "product_id"])
    op.create_table("production_task_drawings",
        sa.Column("task_id", sa.Integer(), sa.ForeignKey("production_tasks.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("release_id", sa.Integer(), sa.ForeignKey("drawing_releases.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("bound_at", sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False),
        sa.Column("bound_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL")))
    op.execute("CREATE TRIGGER drawing_releases_no_update BEFORE UPDATE ON drawing_releases BEGIN SELECT RAISE(ABORT, 'drawing release is immutable'); END")
    op.execute("CREATE TRIGGER drawing_releases_no_delete BEFORE DELETE ON drawing_releases BEGIN SELECT RAISE(ABORT, 'drawing release is immutable'); END")
    op.execute("CREATE TRIGGER production_task_drawings_no_update BEFORE UPDATE ON production_task_drawings BEGIN SELECT RAISE(ABORT, 'task drawing binding is immutable'); END")
    op.execute("CREATE TRIGGER production_task_drawings_no_delete BEFORE DELETE ON production_task_drawings BEGIN SELECT RAISE(ABORT, 'task drawing binding is immutable'); END")
    op.create_table("production_task_drawing_adoptions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("task_id", sa.Integer(), sa.ForeignKey("production_tasks.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("old_release_id", sa.Integer(), sa.ForeignKey("drawing_releases.id", ondelete="RESTRICT")),
        sa.Column("new_release_id", sa.Integer(), sa.ForeignKey("drawing_releases.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("legacy_reference", sa.Text()),
        sa.Column("expected_task_version", sa.Integer(), nullable=False),
        sa.Column("resulting_task_version", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(100), unique=True, nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("confirmed_not_issued", sa.Integer(), nullable=False),
        sa.Column("adopted_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("adopted_at", sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False),
        sa.UniqueConstraint("task_id", "resulting_task_version", name="uq_task_drawing_adoption_version"),
        sa.CheckConstraint("expected_task_version >= 1 AND resulting_task_version = expected_task_version + 1", name="ck_task_drawing_adoption_version"),
        sa.CheckConstraint("confirmed_not_issued = 1", name="ck_task_drawing_adoption_confirmation"))
    op.create_index("ix_task_drawing_adoption_task_id", "production_task_drawing_adoptions", ["task_id", "id"])
    for operation in ("update", "delete"):
        op.execute(f"CREATE TRIGGER task_drawing_adoptions_no_{operation} BEFORE {operation.upper()} ON production_task_drawing_adoptions BEGIN SELECT RAISE(ABORT, 'task drawing adoption is immutable'); END")


def downgrade():
    bind = op.get_bind()
    for name in ("production_task_drawing_adoptions", "production_task_drawings", "drawing_releases", "drawing_designs", "drawing_number_sequences"):
        if bind.execute(sa.text(f"SELECT 1 FROM {name} LIMIT 1")).first():
            raise RuntimeError("图纸V2已有事实，须用经验证的升级前备份恢复")
    op.execute("DROP TRIGGER task_drawing_adoptions_no_delete")
    op.execute("DROP TRIGGER task_drawing_adoptions_no_update")
    op.drop_index("ix_task_drawing_adoption_task_id", table_name="production_task_drawing_adoptions")
    op.drop_table("production_task_drawing_adoptions")
    op.execute("DROP TRIGGER production_task_drawings_no_delete")
    op.execute("DROP TRIGGER production_task_drawings_no_update")
    op.execute("DROP TRIGGER drawing_releases_no_delete")
    op.execute("DROP TRIGGER drawing_releases_no_update")
    op.drop_table("production_task_drawings")
    op.drop_index("ix_drawing_release_customer_product", table_name="drawing_releases")
    op.drop_table("drawing_releases")
    op.drop_table("drawing_number_sequences")
    op.drop_table("drawing_designs")
