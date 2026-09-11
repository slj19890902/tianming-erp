"""Keep one procurement identity per frozen source across structural revisions."""
from alembic import op
import sqlalchemy as sa

revision = "sj22v8x9z84"
down_revision = "si21v8x9z83"
branch_labels = None
depends_on = None


def upgrade():
    db = op.get_bind()
    invalid = db.execute(sa.text("""SELECT l.external_component_id
        FROM order_bom_external_components l
        LEFT JOIN sales_order_item_bom_components s ON s.id=l.bom_snapshot_id
        LEFT JOIN sales_order_item_external_components e ON e.id=l.external_component_id
        WHERE s.id IS NULL OR e.id IS NULL OR s.sales_order_item_id<>l.order_item_id
           OR s.component_product_id<>l.product_id OR e.sales_order_item_id<>l.order_item_id
        LIMIT 1""")).scalar()
    if invalid is not None:
        raise RuntimeError(f"外购BOM来源#{invalid}身份不一致，迁移未执行")
    op.create_index("uq_external_component_order_identity", "sales_order_item_external_components",
                    ["id", "sales_order_item_id"], unique=True)
    with op.batch_alter_table("order_bom_external_components") as batch:
        batch.drop_constraint("uq_bom_external_product", type_="unique")
        batch.create_foreign_key("fk_bom_external_source_identity", "sales_order_item_bom_components",
            ["bom_snapshot_id", "order_item_id", "product_id"],
            ["id", "sales_order_item_id", "component_product_id"], ondelete="RESTRICT")
        batch.create_foreign_key("fk_bom_external_procurement_owner", "sales_order_item_external_components",
            ["external_component_id", "order_item_id"], ["id", "sales_order_item_id"], ondelete="RESTRICT")


def downgrade():
    db = op.get_bind()
    # Stop before any DDL, including a multi-revision downgrade: revision 83
    # also forbids removing rule facts. Never partially downgrade before it.
    if db.execute(sa.text("SELECT 1 FROM order_bom_rule_revisions LIMIT 1")).scalar() is not None:
        raise RuntimeError("已有BOM规则版本事实，须恢复已验证的完整备份，不得降级删除事实")
    duplicate = db.execute(sa.text("""SELECT 1 FROM order_bom_external_components
        GROUP BY order_item_id,product_id HAVING count(*)>1 LIMIT 1""")).scalar()
    if duplicate is not None:
        raise RuntimeError("已有多个外购冻结来源，不能降级合并真实身份")
    with op.batch_alter_table("order_bom_external_components") as batch:
        batch.drop_constraint("fk_bom_external_procurement_owner", type_="foreignkey")
        batch.drop_constraint("fk_bom_external_source_identity", type_="foreignkey")
        batch.create_unique_constraint("uq_bom_external_product", ["order_item_id", "product_id"])
    op.drop_index("uq_external_component_order_identity", table_name="sales_order_item_external_components")
