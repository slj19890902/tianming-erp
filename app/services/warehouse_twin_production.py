from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3
from uuid import uuid4


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_FACTORY_TWIN_DATABASE_PATH = (
    PROJECT_ROOT / "factory_twin" / "data" / "factory_twin.sqlite3"
)
SOURCE_TYPE = "erp_production_task"


class WarehouseTwinProductionError(RuntimeError):
    def __init__(self, message: str, status_code: int = 409):
        super().__init__(message)
        self.status_code = status_code


def factory_twin_database_path() -> Path:
    raw = os.getenv("ERP_FACTORY_TWIN_DATABASE_PATH", "").strip()
    path = Path(raw) if raw else DEFAULT_FACTORY_TWIN_DATABASE_PATH
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve(strict=False)


def _connect(path: Path, *, write: bool) -> sqlite3.Connection:
    if not path.is_file():
        raise WarehouseTwinProductionError("隔离数字孪生数据库不存在", 503)
    mode = "rw" if write else "ro"
    connection = sqlite3.connect(
        f"file:{path.as_posix()}?mode={mode}", uri=True, timeout=5
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    if not write:
        connection.execute("PRAGMA query_only=ON")
    table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='twin_production_projection_mappings'"
    ).fetchone()
    if table is None:
        connection.close()
        raise WarehouseTwinProductionError("隔离地图库尚未初始化生产任务定位表", 503)
    return connection


def _target_catalog(floor: dict) -> dict[tuple[str, str], tuple[str, str]]:
    catalog: dict[tuple[str, str], tuple[str, str]] = {}
    for pallet in floor.get("pallets") or []:
        catalog[("pallet", str(pallet["id"]))] = (
            str(pallet.get("pallet_code") or ""),
            str(pallet.get("name") or "标准栈板"),
        )
    for feature in floor.get("features") or []:
        if feature.get("feature_kind") == "zone":
            catalog[("zone", str(feature["id"]))] = (
                str(feature.get("feature_code") or ""),
                str(feature.get("name") or "区域"),
            )
    return catalog


def _mapping_payload(
    row: sqlite3.Row,
    catalog: dict[tuple[str, str], tuple[str, str]],
) -> dict:
    target = catalog.get((str(row["target_kind"]), str(row["target_id"])))
    return {
        "id": str(row["id"]),
        "source_task_id": int(row["source_task_id"]),
        "source_type": str(row["source_type"]),
        "target_kind": str(row["target_kind"]),
        "target_id": str(row["target_id"]),
        "target_code": target[0] if target else None,
        "target_name": target[1] if target else None,
        "target_missing": target is None,
        "confirmed_at": str(row["confirmed_at"]),
        "version": int(row["version"]),
    }


def list_production_projection_mappings(
    layout_id: str,
    floor: dict,
    *,
    path: Path | None = None,
) -> list[dict]:
    database = path or factory_twin_database_path()
    with _connect(database, write=False) as connection:
        rows = connection.execute(
            "SELECT id, source_task_id, source_type, target_kind, target_id, "
            "confirmed_at, version FROM twin_production_projection_mappings "
            "WHERE layout_id=? ORDER BY source_task_id",
            (layout_id,),
        ).fetchall()
    catalog = _target_catalog(floor)
    return [_mapping_payload(row, catalog) for row in rows]


def build_production_projection(
    *,
    floor: dict,
    tasks: list[dict],
    task_dates: dict[int, dict[str, str | None]] | None = None,
    path: Path | None = None,
) -> dict:
    layout_id = str(floor["layout_id"])
    mappings = list_production_projection_mappings(layout_id, floor, path=path)
    by_task_id = {item["source_task_id"]: item for item in mappings}
    dates = task_dates or {}
    items = []
    for task in tasks:
        task_id = int(task["id"])
        source_dates = dates.get(task_id, {})
        items.append(
            {
                "source_task_id": task_id,
                "source_version": int(task.get("version") or 1),
                "order_id": int(task["order_id"]),
                "order_number": str(
                    task.get("item_order_number") or task.get("order_number") or ""
                ),
                "customer_name": str(task.get("customer_name") or ""),
                "product_code": str(task.get("product_code") or ""),
                "product_name": str(task.get("product_name") or ""),
                "specification": str(task.get("specification") or ""),
                "status": "pending",
                "planned_quantity": int(task.get("planned_quantity") or 0),
                "production_quantity_unit": str(
                    task.get("production_quantity_unit") or "sets"
                ),
                "task_updated_at": source_dates.get("task_updated_at"),
                "delivery_date": source_dates.get("delivery_date"),
                "source_state": "current",
                "mapping": by_task_id.get(task_id),
            }
        )
    current_ids = {item["source_task_id"] for item in items}
    stale = [
        {**mapping, "source_state": "not_pending_or_missing"}
        for mapping in mappings
        if mapping["source_task_id"] not in current_ids
    ]
    return {
        "available": True,
        "source_label": "ERP当前账号可见的真实待生产任务",
        "source_read_only": True,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "error": None,
        "layout_id": layout_id,
        "floor_code": str(floor["floor_code"]),
        "items": items,
        "stale_mappings": stale,
        "write_boundary": "仅保存隔离地图库定位关系，不修改ERP订单、生产、库存或流水",
    }


def save_production_projection_mapping(
    *,
    floor: dict,
    source_task_id: int,
    target_kind: str,
    target_id: str,
    expected_version: int | None,
    path: Path | None = None,
) -> dict:
    if str(floor.get("floor_code", "")).upper() != "1F":
        raise WarehouseTwinProductionError("当前阶段只允许定位一楼生产周转任务", 422)
    catalog = _target_catalog(floor)
    target_key = (target_kind, target_id)
    if target_key not in catalog:
        raise WarehouseTwinProductionError("所选栈板或区域不属于当前一楼布局", 422)
    database = path or factory_twin_database_path()
    now = datetime.now(timezone.utc).isoformat()
    with _connect(database, write=True) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT id, source_task_id, source_type, target_kind, target_id, "
            "confirmed_at, version FROM twin_production_projection_mappings "
            "WHERE layout_id=? AND source_task_id=?",
            (str(floor["layout_id"]), source_task_id),
        ).fetchone()
        if row is None:
            if expected_version is not None:
                raise WarehouseTwinProductionError("任务尚未定位，请刷新后重试")
            mapping_id = str(uuid4())
            connection.execute(
                "INSERT INTO twin_production_projection_mappings "
                "(id, layout_id, source_task_id, source_type, target_kind, target_id, "
                "confirmed_at, version, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    mapping_id,
                    str(floor["layout_id"]),
                    source_task_id,
                    SOURCE_TYPE,
                    target_kind,
                    target_id,
                    now,
                    1,
                    now,
                    now,
                ),
            )
        else:
            current_version = int(row["version"])
            if expected_version != current_version:
                raise WarehouseTwinProductionError("任务定位已被更新，请刷新后重试")
            mapping_id = str(row["id"])
            connection.execute(
                "UPDATE twin_production_projection_mappings "
                "SET target_kind=?, target_id=?, confirmed_at=?, version=?, updated_at=? "
                "WHERE id=?",
                (
                    target_kind,
                    target_id,
                    now,
                    current_version + 1,
                    now,
                    mapping_id,
                ),
            )
        connection.commit()
        saved = connection.execute(
            "SELECT id, source_task_id, source_type, target_kind, target_id, "
            "confirmed_at, version FROM twin_production_projection_mappings WHERE id=?",
            (mapping_id,),
        ).fetchone()
    assert saved is not None
    return _mapping_payload(saved, catalog)


def delete_production_projection_mapping(
    *,
    layout_id: str,
    source_task_id: int,
    expected_version: int,
    path: Path | None = None,
) -> None:
    database = path or factory_twin_database_path()
    with _connect(database, write=True) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT id, version FROM twin_production_projection_mappings "
            "WHERE layout_id=? AND source_task_id=?",
            (layout_id, source_task_id),
        ).fetchone()
        if row is None:
            raise WarehouseTwinProductionError("任务定位不存在", 404)
        if int(row["version"]) != expected_version:
            raise WarehouseTwinProductionError("任务定位已被更新，请刷新后重试")
        connection.execute(
            "DELETE FROM twin_production_projection_mappings WHERE id=?",
            (str(row["id"]),),
        )
        connection.commit()
