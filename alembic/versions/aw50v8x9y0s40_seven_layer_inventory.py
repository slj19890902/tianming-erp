"""Expand semi-finished inventory constraints to seven-layer board.

Revision ID: aw50v8x9y0s40
Revises: av49v8x9y0r39
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "aw50v8x9y0s40"
down_revision = "av49v8x9y0r39"
branch_labels = None
depends_on = None


LAYER_CHECK_357 = "layer_count IN (3,5,7)"
FLUTE_CHECK_357 = (
    "(layer_count=3 AND flute_type IN ('A','B','E')) OR "
    "(layer_count=5 AND flute_type IN ('AB','BE')) OR "
    "(layer_count=7 AND flute_type IN ('AAA','ABC'))"
)
LAYER_CHECK_35 = "layer_count IN (3,5)"
FLUTE_CHECK_35 = (
    "(layer_count=3 AND flute_type IN ('A','B','E')) OR "
    "(layer_count=5 AND flute_type IN ('AB','BE'))"
)
DOWNGRADE_BLOCKED_MESSAGE = (
    "seven-layer semi-finished inventory exists; downgrade would violate "
    "the restored three/five-layer constraints"
)


def _replace_inventory_checks(layer_check: str, flute_check: str) -> None:
    # SQLite requires a table rebuild to replace CHECK constraints. Alembic's
    # batch operation copies rows verbatim and recreates indexes/foreign keys.
    with op.batch_alter_table(
        "semi_finished_inventory_details",
        recreate="always",
    ) as batch_op:
        batch_op.drop_constraint("ck_semi_inventory_layer", type_="check")
        batch_op.drop_constraint("ck_semi_inventory_flute", type_="check")
        batch_op.create_check_constraint("ck_semi_inventory_layer", layer_check)
        batch_op.create_check_constraint("ck_semi_inventory_flute", flute_check)


def upgrade() -> None:
    # Schema-only: existing three/five-layer rows are copied without updates.
    _replace_inventory_checks(LAYER_CHECK_357, FLUTE_CHECK_357)


def downgrade() -> None:
    connection = op.get_bind()
    incompatible_rows = connection.execute(
        sa.text(
            """
            SELECT COUNT(*)
            FROM semi_finished_inventory_details
            WHERE layer_count NOT IN (3,5)
               OR NOT (
                    (layer_count=3 AND flute_type IN ('A','B','E'))
                 OR (layer_count=5 AND flute_type IN ('AB','BE'))
               )
            """
        )
    ).scalar_one()
    if int(incompatible_rows or 0) > 0:
        raise RuntimeError(DOWNGRADE_BLOCKED_MESSAGE)

    # No business rows are rewritten or deleted; incompatible data blocks the
    # downgrade instead of being coerced into an older legal combination.
    _replace_inventory_checks(LAYER_CHECK_35, FLUTE_CHECK_35)
