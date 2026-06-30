"""Tianhua mobile picking fields and delivery picker role.

Revision ID: x06r3s4t5u94
Revises: w95q2r3s4t83
"""

from alembic import op
import sqlalchemy as sa


revision = "x06r3s4t5u94"
down_revision = "w95q2r3s4t83"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("users", recreate="always") as batch_op:
        batch_op.drop_constraint("ck_users_role_valid", type_="check")
        batch_op.create_check_constraint(
            "ck_users_role_valid",
            "role IN ('admin', 'finance', 'sales', 'workshop', 'delivery_picker')",
        )

    with op.batch_alter_table(
        "tianhua_pre_delivery_draft_items",
        recreate="always",
        table_args=(
            sa.CheckConstraint("delivery_qty>=0"),
            sa.CheckConstraint(
                "mobile_pick_status IN ('pending','picked','no_stock','partial')",
                name="ck_tianhua_draft_item_mobile_pick_status",
            ),
        ),
    ) as batch_op:
        batch_op.add_column(
            sa.Column(
                "mobile_pick_status",
                sa.String(length=20),
                nullable=False,
                server_default="pending",
            )
        )
        batch_op.add_column(sa.Column("mobile_picked_qty", sa.Integer()))
        batch_op.add_column(sa.Column("mobile_pick_note", sa.String(length=500)))
        batch_op.add_column(sa.Column("mobile_picked_at", sa.DateTime()))
        batch_op.add_column(sa.Column("mobile_picked_by", sa.Integer()))
        batch_op.create_foreign_key(
            "fk_tianhua_draft_item_mobile_picked_by",
            "users",
            ["mobile_picked_by"],
            ["id"],
        )


def downgrade() -> None:
    with op.batch_alter_table(
        "tianhua_pre_delivery_draft_items",
        recreate="always",
        table_args=(sa.CheckConstraint("delivery_qty>0"),),
    ) as batch_op:
        batch_op.drop_constraint(
            "fk_tianhua_draft_item_mobile_picked_by",
            type_="foreignkey",
        )
        for column in (
            "mobile_picked_by",
            "mobile_picked_at",
            "mobile_pick_note",
            "mobile_picked_qty",
            "mobile_pick_status",
        ):
            batch_op.drop_column(column)

    with op.batch_alter_table("users", recreate="always") as batch_op:
        batch_op.drop_constraint("ck_users_role_valid", type_="check")
        batch_op.create_check_constraint(
            "ck_users_role_valid",
            "role IN ('admin', 'finance', 'sales', 'workshop')",
        )
