"""Link Tianhua pre-delivery drafts to pending deliveries.

Revision ID: aa18t6u7v8w17
Revises: 4ef11be39ad3
"""

from alembic import op
import sqlalchemy as sa


revision = "aa18t6u7v8w17"
down_revision = "4ef11be39ad3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("tianhua_pre_delivery_drafts", recreate="always") as batch_op:
        batch_op.add_column(sa.Column("delivery_id", sa.Integer()))
        batch_op.create_foreign_key(
            "fk_tianhua_pre_delivery_drafts_delivery_id",
            "sales_deliveries",
            ["delivery_id"],
            ["id"],
            ondelete="SET NULL",
        )
    op.create_index(
        "ix_tianhua_pre_delivery_drafts_delivery_id",
        "tianhua_pre_delivery_drafts",
        ["delivery_id"],
    )

    with op.batch_alter_table("tianhua_pre_delivery_draft_items", recreate="always") as batch_op:
        batch_op.add_column(sa.Column("delivery_item_id", sa.Integer()))
        batch_op.create_foreign_key(
            "fk_tianhua_pre_delivery_draft_items_delivery_item_id",
            "sales_delivery_items",
            ["delivery_item_id"],
            ["id"],
            ondelete="SET NULL",
        )
    op.create_index(
        "ix_tianhua_pre_delivery_draft_items_delivery_item_id",
        "tianhua_pre_delivery_draft_items",
        ["delivery_item_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_tianhua_pre_delivery_draft_items_delivery_item_id",
        table_name="tianhua_pre_delivery_draft_items",
    )
    with op.batch_alter_table("tianhua_pre_delivery_draft_items") as batch_op:
        batch_op.drop_constraint(
            "fk_tianhua_pre_delivery_draft_items_delivery_item_id",
            type_="foreignkey",
        )
        batch_op.drop_column("delivery_item_id")

    op.drop_index(
        "ix_tianhua_pre_delivery_drafts_delivery_id",
        table_name="tianhua_pre_delivery_drafts",
    )
    with op.batch_alter_table("tianhua_pre_delivery_drafts") as batch_op:
        batch_op.drop_constraint(
            "fk_tianhua_pre_delivery_drafts_delivery_id",
            type_="foreignkey",
        )
        batch_op.drop_column("delivery_id")
