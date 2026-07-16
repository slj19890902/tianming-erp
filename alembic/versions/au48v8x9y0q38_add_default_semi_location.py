"""Add the default non-floor-three semi-finished holding location."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "au48v8x9y0q38"
down_revision = "at47v7w8x9p37"
branch_labels = None
depends_on = None


LOCATION_CODE = "SF-TEMP"
LOCATION_NAME = "半成品待定位区"
DOWNGRADE_BLOCKED_MESSAGE = (
    "半成品默认库位 SF-TEMP 已被库存或物理栈板引用，禁止降级删除"
)


def upgrade() -> None:
    connection = op.get_bind()
    locations = sa.table(
        "warehouse_locations",
        sa.column("location_code", sa.String()),
        sa.column("location_name", sa.String()),
        sa.column("warehouse_type", sa.String()),
        sa.column("is_active", sa.Boolean()),
        sa.column("warehouse_floor", sa.Integer()),
        sa.column("source_version", sa.String()),
    )
    exists = connection.execute(
        sa.select(locations.c.location_code).where(
            locations.c.location_code == LOCATION_CODE
        )
    ).first()
    if exists is not None:
        return
    connection.execute(
        sa.insert(locations).values(
            location_code=LOCATION_CODE,
            location_name=LOCATION_NAME,
            warehouse_type="semi_finished",
            is_active=True,
            warehouse_floor=None,
            source_version=None,
        )
    )


def downgrade() -> None:
    connection = op.get_bind()
    referenced = connection.execute(
        sa.text(
            """
            SELECT EXISTS(
                SELECT 1 FROM inventory_lots lot
                JOIN warehouse_locations location
                  ON location.id = lot.warehouse_location_id
                WHERE location.location_code = :location_code
            )
            OR EXISTS(
                SELECT 1 FROM inventory_pallets pallet
                JOIN warehouse_locations location
                  ON location.id = pallet.location_id
                WHERE location.location_code = :location_code
            )
            """
        ),
        {"location_code": LOCATION_CODE},
    ).scalar_one()
    if referenced:
        raise RuntimeError(DOWNGRADE_BLOCKED_MESSAGE)
    connection.execute(
        sa.text(
            "DELETE FROM warehouse_locations "
            "WHERE location_code = :location_code "
            "AND warehouse_type = 'semi_finished' "
            "AND source_version IS NULL"
        ),
        {"location_code": LOCATION_CODE},
    )
