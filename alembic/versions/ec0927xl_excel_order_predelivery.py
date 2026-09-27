"""Add Excel order source trace and neutral pre-delivery source facts."""

from alembic import op
import sqlalchemy as sa


revision = "ec0927xl"
down_revision = "eb0926dq"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("order_import_sources") as batch:
        batch.drop_constraint("ck_order_import_sources_kind", type_="check")
        batch.create_check_constraint(
            "ck_order_import_sources_kind",
            "source_kind IN ('pdf_upload', 'email_attachment', 'excel_upload')",
        )
    with op.batch_alter_table("order_import_source_lines") as batch:
        batch.add_column(sa.Column("source_sheet", sa.String(length=120), nullable=True))
        batch.add_column(sa.Column("source_row", sa.Integer(), nullable=True))
        batch.create_check_constraint(
            "ck_order_import_source_lines_source_row",
            "source_row IS NULL OR source_row >= 1",
        )

    with op.batch_alter_table("tianhua_pre_delivery_import_batches") as batch:
        batch.add_column(sa.Column("source_type", sa.String(length=30), nullable=False, server_default="tianhua_image"))
        batch.add_column(sa.Column("source_format", sa.String(length=20), nullable=True))
        batch.add_column(sa.Column("source_hash", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("business_fingerprint", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("parser_version", sa.String(length=80), nullable=True))
        batch.add_column(sa.Column("source_name", sa.String(length=255), nullable=True))
        batch.create_check_constraint(
            "ck_tianhua_pre_delivery_batch_source_type",
            "source_type IN ('tianhua_image', 'excel_upload')",
        )
    op.create_index(
        "uq_tianhua_pre_delivery_excel_source_hash",
        "tianhua_pre_delivery_import_batches",
        ["source_type", "source_hash"],
        unique=True,
    )
    with op.batch_alter_table("tianhua_pre_delivery_import_items") as batch:
        batch.add_column(sa.Column("source_sheet", sa.String(length=120), nullable=True))
        batch.add_column(sa.Column("source_row", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("source_no", sa.String(length=120), nullable=True))
        batch.add_column(sa.Column("source_payload_json", sa.Text(), nullable=True))
    with op.batch_alter_table("delivery_pick_tasks") as batch:
        batch.add_column(sa.Column("route_snapshot_json", sa.Text(), nullable=True))
        batch.add_column(sa.Column("route_snapshot_version", sa.String(length=64), nullable=True))

    op.create_table(
        "pre_delivery_source_allocations",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("import_item_id", sa.Integer(), nullable=False),
        sa.Column("order_item_id", sa.Integer(), nullable=False),
        sa.Column("allocated_qty", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False),
        sa.CheckConstraint("allocated_qty > 0", name="ck_pre_delivery_source_allocations_qty"),
        sa.ForeignKeyConstraint(["import_item_id"], ["tianhua_pre_delivery_import_items.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["order_item_id"], ["sales_order_items.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("import_item_id", "order_item_id", name="uq_pre_delivery_source_allocation"),
    )
    op.create_index("ix_pre_delivery_source_allocations_item", "pre_delivery_source_allocations", ["import_item_id"])


def downgrade() -> None:
    connection = op.get_bind()
    if connection.execute(sa.text("SELECT 1 FROM delivery_pick_tasks WHERE route_snapshot_json IS NOT NULL LIMIT 1")).first():
        raise RuntimeError("已有拿货路线快照，禁止降级；请使用已验证恢复备份")
    if connection.execute(sa.text("SELECT 1 FROM pre_delivery_source_allocations LIMIT 1")).first():
        raise RuntimeError("已有预送货分配事实，禁止降级；请使用已验证恢复备份")
    if connection.execute(sa.text("SELECT 1 FROM order_import_sources WHERE source_kind='excel_upload' LIMIT 1")).first():
        raise RuntimeError("已有 Excel 订单来源事实，禁止降级；请使用已验证恢复备份")
    op.drop_index("ix_pre_delivery_source_allocations_item", table_name="pre_delivery_source_allocations")
    op.drop_table("pre_delivery_source_allocations")
    with op.batch_alter_table("delivery_pick_tasks") as batch:
        batch.drop_column("route_snapshot_version")
        batch.drop_column("route_snapshot_json")
    with op.batch_alter_table("tianhua_pre_delivery_import_items") as batch:
        batch.drop_column("source_payload_json")
        batch.drop_column("source_no")
        batch.drop_column("source_row")
        batch.drop_column("source_sheet")
    op.drop_index("uq_tianhua_pre_delivery_excel_source_hash", table_name="tianhua_pre_delivery_import_batches")
    with op.batch_alter_table("tianhua_pre_delivery_import_batches") as batch:
        batch.drop_constraint("ck_tianhua_pre_delivery_batch_source_type", type_="check")
        batch.drop_column("source_name")
        batch.drop_column("parser_version")
        batch.drop_column("business_fingerprint")
        batch.drop_column("source_hash")
        batch.drop_column("source_format")
        batch.drop_column("source_type")
    with op.batch_alter_table("order_import_source_lines") as batch:
        batch.drop_constraint("ck_order_import_source_lines_source_row", type_="check")
        batch.drop_column("source_row")
        batch.drop_column("source_sheet")
    with op.batch_alter_table("order_import_sources") as batch:
        batch.drop_constraint("ck_order_import_sources_kind", type_="check")
        batch.create_check_constraint(
            "ck_order_import_sources_kind",
            "source_kind IN ('pdf_upload', 'email_attachment')",
        )
