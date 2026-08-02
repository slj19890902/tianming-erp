"""add formal floor-one delivery staging location

Revision ID: de87v8x9z76
Revises: dd86v8x9z75
Create Date: 2026-08-02
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "de87v8x9z76"
down_revision = "dd86v8x9z75"
branch_labels = None
depends_on = None

FLOOR_CODE = "F1"
FLOOR_NAME = "一楼"
FLOOR_NUMBER = 1
AREA_CODE = "DISPATCH"
AREA_NAME = "待送区"
LOCATION_CODE = "F1-DISPATCH-01"
LOCATION_NAME = "一楼待送区"
SOURCE_VERSION = "P1-25C"
SEED_REMARK = "P1-25C 自动建立：生产完工直接待送整批暂存"


def _one(connection, sql: str, **params):
    return connection.execute(sa.text(sql), params).mappings().one_or_none()


def _drop_sqlite_triggers_referencing(connection, table_name: str) -> list[str]:
    if connection.dialect.name != "sqlite":
        return []
    rows = connection.execute(
        sa.text(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type='trigger' AND sql IS NOT NULL AND sql LIKE :needle"
        ),
        {"needle": f"%{table_name}%"},
    ).mappings().all()
    definitions: list[str] = []
    for row in rows:
        name = str(row["name"])
        if not name.replace("_", "").isalnum():
            raise RuntimeError(f"检测到无法安全处理的 SQLite 触发器名称：{name}")
        definitions.append(str(row["sql"]))
        connection.exec_driver_sql(f'DROP TRIGGER IF EXISTS "{name}"')
    return definitions


def _restore_sqlite_triggers(connection, definitions: list[str]) -> None:
    for definition in definitions:
        connection.exec_driver_sql(definition)


def upgrade() -> None:
    connection = op.get_bind()

    floor_by_number = _one(
        connection,
        "SELECT * FROM warehouse_floors WHERE floor_number=:number",
        number=FLOOR_NUMBER,
    )
    floor_by_code = _one(
        connection,
        "SELECT * FROM warehouse_floors WHERE floor_code=:code",
        code=FLOOR_CODE,
    )
    if floor_by_number and floor_by_code and floor_by_number["id"] != floor_by_code["id"]:
        raise RuntimeError("一楼编号与 F1 编码分别指向不同楼层，拒绝自动建立待送区")
    floor = floor_by_number or floor_by_code
    if floor is None:
        connection.execute(
            sa.text(
                "INSERT INTO warehouse_floors "
                "(floor_code,floor_name,floor_number,construction_status,remarks) "
                "VALUES (:code,:name,:number,'enabled',:remarks)"
            ),
            {
                "code": FLOOR_CODE,
                "name": FLOOR_NAME,
                "number": FLOOR_NUMBER,
                "remarks": SEED_REMARK,
            },
        )
        floor = _one(
            connection,
            "SELECT * FROM warehouse_floors WHERE floor_number=:number",
            number=FLOOR_NUMBER,
        )
    elif floor["floor_code"] != FLOOR_CODE or floor["construction_status"] != "enabled":
        raise RuntimeError("现有一楼台账不是已启用的 F1，拒绝自动改写")
    assert floor is not None

    area = _one(
        connection,
        "SELECT * FROM warehouse_areas WHERE floor_id=:floor_id AND area_code=:code",
        floor_id=floor["id"],
        code=AREA_CODE,
    )
    if area is None:
        connection.execute(
            sa.text(
                "INSERT INTO warehouse_areas "
                "(floor_id,area_code,area_name,planned_location_count,"
                "planned_pallet_capacity,construction_status,remarks) "
                "VALUES (:floor_id,:code,:name,1,0,'enabled',:remarks)"
            ),
            {
                "floor_id": floor["id"],
                "code": AREA_CODE,
                "name": AREA_NAME,
                "remarks": SEED_REMARK,
            },
        )
    elif area["construction_status"] != "enabled":
        raise RuntimeError("现有一楼待送区尚未启用，拒绝自动改写")

    location = _one(
        connection,
        "SELECT * FROM warehouse_locations WHERE location_code=:code",
        code=LOCATION_CODE,
    )
    if location is None:
        connection.execute(
            sa.text(
                "INSERT INTO warehouse_locations "
                "(location_code,location_name,warehouse_type,is_active,remarks,"
                "warehouse_floor,area_code,storage_type,sort_order,is_temporary,"
                "source_version,placement_status) "
                "VALUES (:code,:name,'finished',1,:remarks,1,:area,'temporary_aisle',"
                "1,1,:source,'placed')"
            ),
            {
                "code": LOCATION_CODE,
                "name": LOCATION_NAME,
                "remarks": SEED_REMARK,
                "area": AREA_CODE,
                "source": SOURCE_VERSION,
            },
        )
    elif not (
        location["warehouse_type"] in {"finished", "shared"}
        and bool(location["is_active"])
        and location["warehouse_floor"] == FLOOR_NUMBER
        and str(location["area_code"] or "").upper() == AREA_CODE
        and location["placement_status"] in {None, "placed"}
    ):
        raise RuntimeError("现有 F1-DISPATCH-01 与正式待送区定义冲突，拒绝自动改写")

    inventory_trigger_sql = _drop_sqlite_triggers_referencing(
        connection, "inventory_lots"
    )
    with op.batch_alter_table("inventory_lots") as batch:
        batch.drop_constraint("ck_inventory_lots_source_type", type_="check")
        batch.create_check_constraint(
            "ck_inventory_lots_source_type",
            "source_type IN ('manual','production_completion','production_surplus',"
            "'purchase_surplus','stocktake','transfer','replenishment')",
        )
    _restore_sqlite_triggers(connection, inventory_trigger_sql)

    with op.batch_alter_table("production_completions") as batch:
        batch.drop_constraint("ck_production_completions_disposition_targets", type_="check")
        batch.create_check_constraint(
            "ck_production_completions_disposition_targets",
            "((initial_disposition = 'direct' AND stock_quantity = 0 "
            "AND ((warehouse_location_id IS NULL AND inventory_lot_id IS NULL) "
            "OR warehouse_location_id IS NOT NULL)) "
            "OR (initial_disposition = 'stock' AND warehouse_location_id IS NOT NULL "
            "AND direct_delivery_quantity = 0) "
            "OR (initial_disposition = 'split' AND warehouse_location_id IS NOT NULL "
            "AND direct_delivery_quantity > 0 AND stock_quantity > 0))",
        )


def downgrade() -> None:
    connection = op.get_bind()
    staged_facts = int(
        connection.execute(
            sa.text(
                "SELECT COUNT(*) FROM inventory_lots "
                "WHERE source_type='production_completion'"
            )
        ).scalar()
        or 0
    )
    staged_direct = int(
        connection.execute(
            sa.text(
                "SELECT COUNT(*) FROM production_completions "
                "WHERE initial_disposition='direct' AND warehouse_location_id IS NOT NULL"
            )
        ).scalar()
        or 0
    )
    if staged_facts or staged_direct:
        raise RuntimeError("已有一楼待送区生产完工事实，拒绝破坏性降级")

    with op.batch_alter_table("production_completions") as batch:
        batch.drop_constraint("ck_production_completions_disposition_targets", type_="check")
        batch.create_check_constraint(
            "ck_production_completions_disposition_targets",
            "((initial_disposition = 'direct' AND warehouse_location_id IS NULL "
            "AND inventory_lot_id IS NULL AND stock_quantity = 0) "
            "OR (initial_disposition = 'stock' AND warehouse_location_id IS NOT NULL "
            "AND direct_delivery_quantity = 0) "
            "OR (initial_disposition = 'split' AND warehouse_location_id IS NOT NULL "
            "AND direct_delivery_quantity > 0 AND stock_quantity > 0))",
        )
    inventory_trigger_sql = _drop_sqlite_triggers_referencing(
        connection, "inventory_lots"
    )
    with op.batch_alter_table("inventory_lots") as batch:
        batch.drop_constraint("ck_inventory_lots_source_type", type_="check")
        batch.create_check_constraint(
            "ck_inventory_lots_source_type",
            "source_type IN ('manual','production_surplus','purchase_surplus',"
            "'stocktake','transfer','replenishment')",
        )
    _restore_sqlite_triggers(connection, inventory_trigger_sql)

    location = _one(
        connection,
        "SELECT * FROM warehouse_locations WHERE location_code=:code",
        code=LOCATION_CODE,
    )
    if location and location["source_version"] == SOURCE_VERSION:
        connection.execute(
            sa.text("DELETE FROM warehouse_locations WHERE id=:id"),
            {"id": location["id"]},
        )

    floor = _one(
        connection,
        "SELECT * FROM warehouse_floors WHERE floor_number=:number",
        number=FLOOR_NUMBER,
    )
    if floor:
        area = _one(
            connection,
            "SELECT * FROM warehouse_areas WHERE floor_id=:floor_id AND area_code=:code",
            floor_id=floor["id"],
            code=AREA_CODE,
        )
        if area and area["remarks"] == SEED_REMARK:
            remaining = int(
                connection.execute(
                    sa.text(
                        "SELECT COUNT(*) FROM warehouse_locations "
                        "WHERE warehouse_floor=:number AND UPPER(area_code)=:code"
                    ),
                    {"number": FLOOR_NUMBER, "code": AREA_CODE},
                ).scalar()
                or 0
            )
            if remaining == 0:
                connection.execute(
                    sa.text("DELETE FROM warehouse_areas WHERE id=:id"),
                    {"id": area["id"]},
                )
        if floor["remarks"] == SEED_REMARK:
            remaining_areas = int(
                connection.execute(
                    sa.text("SELECT COUNT(*) FROM warehouse_areas WHERE floor_id=:id"),
                    {"id": floor["id"]},
                ).scalar()
                or 0
            )
            if remaining_areas == 0:
                connection.execute(
                    sa.text("DELETE FROM warehouse_floors WHERE id=:id"),
                    {"id": floor["id"]},
                )
