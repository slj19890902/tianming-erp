"""Separate unassembled body identity from finished product stock."""
from alembic import op
import sqlalchemy as sa

revision = "sc15v8x9z77"
down_revision = "sb14v8x9z76"
branch_labels = None
depends_on = None


def _alter_lot_type(upgrade):
    connection = op.get_bind()
    triggers = []
    if connection.dialect.name == "sqlite":
        if connection.exec_driver_sql("PRAGMA foreign_key_check").first():
            raise RuntimeError("库存迁移前外键不完整，停止迁移")
        triggers = connection.execute(sa.text(
            "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND sql LIKE '%inventory_lots%'"
        )).all()
        # Batch table reconstruction must retain every old integrity trigger.
        for name, definition in triggers:
            if not name.replace("_", "").isalnum() or not definition:
                raise RuntimeError("库存触发器定义无法安全保留")
        for name, _ in triggers:
            connection.exec_driver_sql(f'DROP TRIGGER "{name}"')
    with op.batch_alter_table("inventory_lots") as batch:
        batch.drop_constraint("ck_inventory_lots_type", type_="check")
        batch.create_check_constraint("ck_inventory_lots_type",
            "inventory_type IN ('finished','semi_finished','assembly_body')" if upgrade else
            "inventory_type IN ('finished','semi_finished')")
        if upgrade:
            batch.create_unique_constraint("uq_inventory_lot_type_identity", ["id", "inventory_type"])
        else:
            batch.drop_constraint("uq_inventory_lot_type_identity", type_="unique")
    for _, definition in triggers:
        connection.exec_driver_sql(definition)
    if connection.dialect.name == "sqlite" and connection.exec_driver_sql("PRAGMA foreign_key_check").first():
        raise RuntimeError("库存迁移后外键检查失败，请保留现场并恢复验证备份")


def upgrade():
    _alter_lot_type(True)
    op.create_table("bom_body_inventory_details",
        sa.Column("inventory_lot_id", sa.Integer(), primary_key=True),
        sa.Column("inventory_type", sa.String(30), nullable=False, server_default="assembly_body"),
        sa.Column("order_item_id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("production_completion_id", sa.Integer(),
            sa.ForeignKey("production_completions.id", ondelete="RESTRICT"), nullable=False),
        sa.ForeignKeyConstraint(["inventory_lot_id", "inventory_type"],
            ["inventory_lots.id", "inventory_lots.inventory_type"],
            ondelete="RESTRICT", name="fk_bom_body_lot_type"),
        sa.ForeignKeyConstraint(["order_item_id", "product_id"],
            ["order_bom_graph_products.order_item_id", "order_bom_graph_products.product_id"],
            ondelete="RESTRICT", name="fk_bom_body_frozen_product"),
        sa.CheckConstraint("inventory_type = 'assembly_body'", name="ck_bom_body_type"))
    op.create_index("ix_bom_body_inventory_details_production_completion_id",
                    "bom_body_inventory_details", ["production_completion_id"])


def downgrade():
    db = op.get_bind()
    if (db.execute(sa.text("SELECT 1 FROM bom_body_inventory_details LIMIT 1")).first()
            or db.execute(sa.text("SELECT 1 FROM inventory_lots WHERE inventory_type='assembly_body' LIMIT 1")).first()):
        raise RuntimeError("已有本体库存记录，禁止降级；请使用已验证备份回退")
    op.drop_table("bom_body_inventory_details")
    _alter_lot_type(False)
