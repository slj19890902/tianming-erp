from __future__ import annotations

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.models.warehouse_inventory import WarehouseFloor


def claim_warehouse_floor_projection(
    db: Session,
    *,
    floor_number: int,
) -> bool:
    """Acquire the persistent floor mutex shared by spatial and business writes."""

    result = db.execute(
        update(WarehouseFloor)
        .where(WarehouseFloor.floor_number == int(floor_number))
        .values(
            construction_status=WarehouseFloor.construction_status,
            updated_at=WarehouseFloor.updated_at,
        )
        .execution_options(synchronize_session=False)
    )
    return result.rowcount == 1


def claim_warehouse_floor_projection_by_id(
    db: Session,
    *,
    floor_id: int,
) -> bool:
    """Acquire the same persistent floor mutex by stable primary key.

    Floor-master edits can change both the human code and floor number, so
    those writes must claim the immutable row identity before re-reading and
    validating the editable identities.
    """

    result = db.execute(
        update(WarehouseFloor)
        .where(WarehouseFloor.id == int(floor_id))
        .values(
            construction_status=WarehouseFloor.construction_status,
            updated_at=WarehouseFloor.updated_at,
        )
        .execution_options(synchronize_session=False)
    )
    return result.rowcount == 1
