"""Normalize the one verified legacy current pallet item into an inventory lot.

This is deliberately a data migration service, not a warehouse operation.  Its
preflight is tied to the one audited legacy fact and it refuses to guess an
order, inbound date, or a replacement physical position.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from hashlib import sha256
import json
from typing import Any

import sqlalchemy as sa


MIGRATION_KEY = "p1-108-legacy-pallet-item-2-lot-normalization"
SNAPSHOT_TABLE = "warehouse_current_map_migration_snapshots"
PALLET_ITEM_ID = 2
PALLET_CODE = "PLT-3F-20260716-B63DB93F"
LOCATION_CODE = "A1-L01"
QUANTITY = 86
CUSTOMER_ID = 5
PRODUCT_ID = 51
INVENTORY_CODE = "21301090"
LOT_NUMBER = "LEGACY-PLTITEM-2"
MOVEMENT_NUMBER = "MIG-P1-108-PLTITEM-2"
IDEMPOTENCY_KEY = "p1-108:legacy-pallet-item:2:manual-in"
REASON = "P1-108 legacy pallet item normalization"
MIGRATED_AT = "2026-08-27 00:00:00"


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat(sep=" ") if isinstance(value, datetime) else value.isoformat()
    return value


def _rows(connection: sa.Connection, statement: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    return [
        {str(key): _json_value(value) for key, value in row.items()}
        for row in connection.execute(sa.text(statement), params or {}).mappings().all()
    ]


def _scalar(connection: sa.Connection, statement: str, params: dict[str, Any] | None = None) -> int:
    return int(connection.execute(sa.text(statement), params or {}).scalar_one() or 0)


def _table_exists(connection: sa.Connection, table_name: str) -> bool:
    return bool(sa.inspect(connection).has_table(table_name))


def _target_rows(connection: sa.Connection) -> dict[str, list[dict[str, Any]]]:
    params = {"item_id": PALLET_ITEM_ID, "pallet_code": PALLET_CODE, "lot_number": LOT_NUMBER}
    return {
        "pallet_item": _rows(connection, "SELECT * FROM inventory_pallet_items WHERE id=:item_id", params),
        "pallet": _rows(connection, "SELECT p.* FROM inventory_pallets p JOIN inventory_pallet_items i ON i.pallet_id=p.id WHERE i.id=:item_id", params),
        "location": _rows(connection, "SELECT l.* FROM warehouse_locations l JOIN inventory_pallets p ON p.location_id=l.id JOIN inventory_pallet_items i ON i.pallet_id=p.id WHERE i.id=:item_id", params),
        "occupancy": _rows(connection, "SELECT o.*,s.id AS slot_id,s.location_id AS slot_location_id,s.slot_sequence,s.status AS slot_status FROM warehouse_ground_occupancies o JOIN warehouse_ground_occupancy_slots s ON s.occupancy_id=o.id JOIN inventory_pallet_items i ON i.pallet_id=o.pallet_id WHERE i.id=:item_id AND s.status='active' ORDER BY s.id", params),
        "normalized_lot": _rows(connection, "SELECT * FROM inventory_lots WHERE lot_number=:lot_number", params),
        "normalized_detail": _rows(connection, "SELECT d.* FROM finished_goods_inventory_details d JOIN inventory_lots x ON x.id=d.inventory_lot_id WHERE x.lot_number=:lot_number", params),
        "normalized_movement": _rows(connection, "SELECT m.* FROM inventory_movements m WHERE m.movement_number=:movement_number", {"movement_number": MOVEMENT_NUMBER}),
        "same_location_lots": _rows(connection, "SELECT x.* FROM inventory_lots x JOIN inventory_pallet_items i ON i.id=:item_id JOIN inventory_pallets p ON p.id=i.pallet_id WHERE x.warehouse_location_id=p.location_id AND x.quantity_available+x.quantity_reserved+x.quantity_damaged>0 ORDER BY x.id", params),
    }


def _fingerprint(connection: sa.Connection) -> str:
    document = _target_rows(connection)
    encoded = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(encoded.encode("utf-8")).hexdigest()


def _legacy_row(connection: sa.Connection) -> dict[str, Any] | None:
    return connection.execute(
        sa.text(
            """
            SELECT i.*,p.pallet_code,p.status AS pallet_status,p.is_current,
                   p.location_id,l.location_code,l.source_version,l.is_active,l.placement_status
            FROM inventory_pallet_items i
            JOIN inventory_pallets p ON p.id=i.pallet_id
            JOIN warehouse_locations l ON l.id=p.location_id
            WHERE i.id=:item_id
            """
        ),
        {"item_id": PALLET_ITEM_ID},
    ).mappings().one_or_none()


def _preflight(connection: sa.Connection) -> dict[str, Any] | None:
    if not _table_exists(connection, SNAPSHOT_TABLE):
        raise RuntimeError("P1-108 preflight failed: current-map snapshot table is missing.")
    row = _legacy_row(connection)
    if row is None:
        facts = _scalar(connection, "SELECT COUNT(*) FROM inventory_pallet_items")
        if facts == 0:
            return None
        raise RuntimeError("P1-108 preflight failed: audited legacy pallet item 2 is missing.")
    expected = (
        row["inventory_lot_id"] is None
        and row["pallet_code"] == PALLET_CODE
        and row["pallet_status"] == "active"
        and int(row["is_current"] or 0) == 1
        and row["location_code"] == LOCATION_CODE
        and row["source_version"] == "CURRENT_MAP"
        and int(row["is_active"] or 0) == 1
        and row["placement_status"] == "placed"
        and int(row["customer_id"] or 0) == CUSTOMER_ID
        and int(row["product_id"] or 0) == PRODUCT_ID
        and str(row["inventory_code"] or "") == INVENTORY_CODE
        and row["item_type"] == "finished"
        and str(row["unit"] or "") == "boxes"
        and row["match_status"] == "matched"
        and int(row["quantity"] or 0) == QUANTITY
    )
    if not expected:
        raise RuntimeError("P1-108 preflight failed: legacy pallet item identity or physical fact drifted.")
    occupancy = connection.execute(sa.text("SELECT primary_location_id,capacity_quantity,status FROM warehouse_ground_occupancies WHERE pallet_id=:pallet_id AND status='active'"), {"pallet_id": row["pallet_id"]}).mappings().one_or_none()
    if occupancy is None or int(occupancy["primary_location_id"] or 0) != int(row["location_id"]) or int(occupancy["capacity_quantity"] or 0) != 98:
        raise RuntimeError("P1-108 preflight failed: legacy pallet no longer has exactly one active ground occupancy.")
    if _scalar(connection, "SELECT COUNT(*) FROM warehouse_ground_occupancy_slots s JOIN warehouse_ground_occupancies o ON o.id=s.occupancy_id WHERE o.pallet_id=:pallet_id AND o.status='active' AND s.status='active'", {"pallet_id": row["pallet_id"]}) != 1:
        raise RuntimeError("P1-108 preflight failed: legacy pallet active ground slot drifted.")
    if _scalar(connection, "SELECT COUNT(*) FROM inventory_lots WHERE id=1 AND warehouse_location_id=:location_id AND inventory_type='finished' AND quantity_available=12", {"location_id": row["location_id"]}) != 1:
        raise RuntimeError("P1-108 preflight failed: healthy same-location inventory evidence is missing.")
    if _scalar(connection, "SELECT COUNT(*) FROM finished_goods_inventory_details WHERE inventory_lot_id=1 AND product_id=44") != 1:
        raise RuntimeError("P1-108 preflight failed: healthy same-location finished-lot identity drifted.")
    if _scalar(connection, "SELECT COUNT(*) FROM inventory_lots WHERE source_ref_type='legacy_pallet_item' AND source_ref_id=:item_id", {"item_id": PALLET_ITEM_ID}):
        raise RuntimeError("P1-108 preflight failed: legacy pallet item already has a normalized-lot source.")
    if not row["customer_name_snapshot"] or not row["product_name"]:
        raise RuntimeError("P1-108 preflight failed: legacy customer or product snapshot is missing.")
    return dict(row)


def _existing_snapshot(connection: sa.Connection) -> dict[str, Any] | None:
    row = connection.execute(sa.text(f"SELECT payload_json,post_fingerprint FROM {SNAPSHOT_TABLE} WHERE migration_key=:key"), {"key": MIGRATION_KEY}).mappings().one_or_none()
    return dict(row) if row else None


def upgrade_legacy_pallet_item_lot(connection: sa.Connection) -> bool:
    existing = _existing_snapshot(connection)
    if existing is not None:
        if _fingerprint(connection) != existing["post_fingerprint"]:
            raise RuntimeError("P1-108 existing migration snapshot does not match current target facts.")
        return False
    row = _preflight(connection)
    if row is None:
        return False
    payload = {"migration_key": MIGRATION_KEY, "pre_fingerprint": _fingerprint(connection), "target": _target_rows(connection)}
    connection.execute(sa.text(f"INSERT INTO {SNAPSHOT_TABLE} (migration_key,payload_json,post_fingerprint,created_at) VALUES (:key,:payload,'PENDING',:created_at)"), {"key": MIGRATION_KEY, "payload": json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")), "created_at": MIGRATED_AT})
    stock_date = str(row["created_at"])[0:10]
    connection.execute(sa.text("""
        INSERT INTO inventory_lots (lot_number,inventory_type,warehouse_location_id,quantity_available,quantity_reserved,quantity_consumed,quantity_damaged,quantity_scrapped,unit,status,source_type,source_ref_type,source_ref_id,stock_date,stock_date_accuracy,stock_date_original_text,last_movement_at,version,remarks,created_by,created_at)
        VALUES (:lot_number,'finished',:location_id,:quantity,0,0,0,0,'boxes','active','manual','legacy_pallet_item',:item_id,:stock_date,'unknown',:date_note,:created_at,1,:remarks,:created_by,:created_at)
        """), {"lot_number": LOT_NUMBER, "location_id": row["location_id"], "quantity": QUANTITY, "item_id": PALLET_ITEM_ID, "stock_date": stock_date, "date_note": "Legacy pallet item had no recorded inbound date; created_at is a non-business placeholder.", "created_at": MIGRATED_AT, "remarks": "Normalized from legacy pallet item 2; historical inbound date unknown.", "created_by": row["created_by"]})
    lot_id = _scalar(connection, "SELECT id FROM inventory_lots WHERE lot_number=:lot_number", {"lot_number": LOT_NUMBER})
    connection.execute(sa.text("""
        INSERT INTO finished_goods_inventory_details (inventory_lot_id,owner_customer_id,owner_customer_name_snapshot,is_general,product_id,inventory_code_snapshot,product_name_snapshot)
        VALUES (:lot_id,:customer_id,:customer_name,0,:product_id,:inventory_code,:product_name)
        """), {"lot_id": lot_id, "customer_id": CUSTOMER_ID, "customer_name": row["customer_name_snapshot"], "product_id": PRODUCT_ID, "inventory_code": row["inventory_code"], "product_name": row["product_name"]})
    connection.execute(sa.text("""
        INSERT INTO inventory_movements (movement_number,inventory_lot_id,movement_type,quantity,unit,before_available,after_available,before_reserved,after_reserved,before_consumed,after_consumed,before_damaged,after_damaged,before_scrapped,after_scrapped,reason,remarks,operator_id,idempotency_key,created_at)
        VALUES (:number,:lot_id,'manual_in',:quantity,'boxes',0,:quantity,0,0,0,0,0,0,0,0,:reason,:remarks,:operator_id,:idempotency_key,:created_at)
        """), {"number": MOVEMENT_NUMBER, "lot_id": lot_id, "quantity": QUANTITY, "reason": REASON, "remarks": "Creates traceable lot for legacy pallet item 2 without changing physical pallet or occupancy.", "operator_id": None, "idempotency_key": IDEMPOTENCY_KEY, "created_at": MIGRATED_AT})
    connection.execute(sa.text("UPDATE inventory_pallet_items SET inventory_lot_id=:lot_id WHERE id=:item_id AND inventory_lot_id IS NULL"), {"lot_id": lot_id, "item_id": PALLET_ITEM_ID})
    if _scalar(connection, "SELECT COUNT(*) FROM inventory_pallet_items WHERE id=:item_id AND inventory_lot_id=:lot_id", {"item_id": PALLET_ITEM_ID, "lot_id": lot_id}) != 1:
        raise RuntimeError("P1-108 post validation failed: legacy pallet item was not uniquely bound to normalized lot.")
    if _scalar(connection, "SELECT COUNT(*) FROM inventory_lots WHERE id=:lot_id AND quantity_available=:quantity AND source_type='manual' AND source_ref_type='legacy_pallet_item' AND source_ref_id=:item_id AND stock_date_accuracy='unknown'", {"lot_id": lot_id, "quantity": QUANTITY, "item_id": PALLET_ITEM_ID}) != 1:
        raise RuntimeError("P1-108 post validation failed: normalized lot facts are incomplete.")
    if _scalar(connection, "SELECT COUNT(*) FROM finished_goods_inventory_details WHERE inventory_lot_id=:lot_id AND owner_customer_id=:customer_id AND product_id=:product_id AND inventory_code_snapshot=:inventory_code", {"lot_id": lot_id, "customer_id": CUSTOMER_ID, "product_id": PRODUCT_ID, "inventory_code": INVENTORY_CODE}) != 1:
        raise RuntimeError("P1-108 post validation failed: normalized finished-detail facts are incomplete.")
    if _scalar(connection, "SELECT COUNT(*) FROM inventory_movements WHERE inventory_lot_id=:lot_id AND movement_number=:number AND movement_type='manual_in' AND quantity=:quantity AND before_available=0 AND after_available=:quantity AND reason=:reason", {"lot_id": lot_id, "number": MOVEMENT_NUMBER, "quantity": QUANTITY, "reason": REASON}) != 1:
        raise RuntimeError("P1-108 post validation failed: normalized manual-in trace is incomplete.")
    if _scalar(connection, "SELECT COUNT(*) FROM inventory_pallet_items i JOIN inventory_pallets p ON p.id=i.pallet_id WHERE p.is_current=1 AND p.status='active' AND i.quantity>0 AND i.inventory_lot_id IS NULL") != 0:
        raise RuntimeError("P1-108 post validation failed: positive current pallet items remain without lot identity.")
    post_fingerprint = _fingerprint(connection)
    connection.execute(sa.text(f"UPDATE {SNAPSHOT_TABLE} SET post_fingerprint=:fingerprint WHERE migration_key=:key"), {"fingerprint": post_fingerprint, "key": MIGRATION_KEY})
    return True


def downgrade_legacy_pallet_item_lot(connection: sa.Connection) -> bool:
    if not _table_exists(connection, SNAPSHOT_TABLE):
        return False
    row = _existing_snapshot(connection)
    if row is None:
        return False
    if _fingerprint(connection) != row["post_fingerprint"]:
        raise RuntimeError("Refusing P1-108 downgrade: normalized lot or its physical target facts changed after migration.")
    payload = json.loads(str(row["payload_json"]))
    lot_id = _scalar(connection, "SELECT id FROM inventory_lots WHERE lot_number=:lot_number", {"lot_number": LOT_NUMBER})
    connection.execute(sa.text("DELETE FROM inventory_movements WHERE movement_number=:number"), {"number": MOVEMENT_NUMBER})
    connection.execute(sa.text("DELETE FROM finished_goods_inventory_details WHERE inventory_lot_id=:lot_id"), {"lot_id": lot_id})
    connection.execute(sa.text("UPDATE inventory_pallet_items SET inventory_lot_id=NULL WHERE id=:item_id AND inventory_lot_id=:lot_id"), {"item_id": PALLET_ITEM_ID, "lot_id": lot_id})
    connection.execute(sa.text("DELETE FROM inventory_lots WHERE id=:lot_id"), {"lot_id": lot_id})
    connection.execute(sa.text(f"DELETE FROM {SNAPSHOT_TABLE} WHERE migration_key=:key"), {"key": MIGRATION_KEY})
    if _fingerprint(connection) != payload["pre_fingerprint"]:
        raise RuntimeError("P1-108 downgrade did not restore the exact pre-migration target state.")
    return True
