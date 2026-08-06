"""Seed isolated 1F pallet/turnover visualization examples.

Dry-run is the default. ``--apply`` requires an explicit confirmation, creates
and verifies a full SQLite backup, then inserts only clearly marked simulated
pallets into the factory-twin candidate database. It never reads or writes ERP
inventory, orders, production records, warehouse locations, or movements.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sqlite3

from sqlalchemy import select

from factory_twin.backend.database import create_database_engine, create_session_factory
from factory_twin.backend.layout_rules import _bounds_intersect, _points_bounds, _rect_bounds
from factory_twin.backend.models import Layout, PalletPlacement
from factory_twin.backend.pallet_layout import validate_pallet_position


LAYOUT_ID = "756bcfa7-74d8-48f2-ba64-a4a23c9984ae"
CONFIRMATION = "SEED_ISOLATED_1F_TURNOVER"
PALLET_WIDTH_MM = 1200.0
PALLET_DEPTH_MM = 1000.0
PALLET_HEIGHT_MM = 150.0

SEEDS = [
    {
        "pallet_code": "SIM-1F-PAL-001",
        "name": "模拟·空栈板回收",
        "visual_status": "empty",
        "status_note": "模拟·空栈板待回收或再次使用",
        "zones": ["ZONE-1F-TEMP-002", "ZONE-1F-RAW-003"],
    },
    {
        "pallet_code": "SIM-1F-PAL-002",
        "name": "模拟·纸板待上机",
        "visual_status": "waiting",
        "status_note": "模拟·纸板进入现场后等待生产",
        "zones": ["ZONE-1F-RAW-005", "ZONE-1F-RAW-003"],
    },
    {
        "pallet_code": "SIM-1F-PAL-003",
        "name": "模拟·辅料待生产",
        "visual_status": "waiting",
        "status_note": "模拟·待生产周转，不代表正式库存",
        "zones": ["ZONE-1F-RAW-003", "ZONE-1F-RAW-005"],
    },
    {
        "pallet_code": "SIM-1F-PAL-004",
        "name": "模拟·印刷后周转",
        "visual_status": "in_process",
        "status_note": "模拟·印刷后等待下一工序",
        "zones": ["ZONE-1F-TEMP-001", "ZONE-1F-SEMI-001"],
    },
    {
        "pallet_code": "SIM-1F-PAL-005",
        "name": "模拟·模切后周转",
        "visual_status": "in_process",
        "status_note": "模拟·模切后等待下一工序",
        "zones": ["ZONE-1F-TEMP-001", "ZONE-1F-SEMI-001"],
    },
    {
        "pallet_code": "SIM-1F-PAL-006",
        "name": "模拟·完工待搬运",
        "visual_status": "completed",
        "status_note": "模拟·完工后等待转运",
        "zones": ["ZONE-1F-FIN-001", "ZONE-1F-FIN-003"],
    },
    {
        "pallet_code": "SIM-1F-PAL-007",
        "name": "模拟·异常复核",
        "visual_status": "abnormal",
        "status_note": "模拟·异常暂存并等待人工复核",
        "zones": ["ZONE-1F-TEMP-003", "ZONE-1F-SEMI-002"],
    },
]


def _verified_backup(source_path: Path, target_path: Path) -> dict:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(f"file:{source_path.resolve().as_posix()}?mode=ro", uri=True)
    target = sqlite3.connect(target_path)
    try:
        source.backup(target)
        quick_check = target.execute("PRAGMA quick_check").fetchone()[0]
        source_tables = source.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table'"
        ).fetchone()[0]
        target_tables = target.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table'"
        ).fetchone()[0]
        if quick_check != "ok" or source_tables != target_tables:
            raise RuntimeError("一楼周转可视化写入前备份校验失败")
        return {
            "path": str(target_path.resolve()),
            "quick_check": quick_check,
            "table_count": target_tables,
            "size_bytes": target_path.stat().st_size,
        }
    finally:
        source.close()
        target.close()


def _candidate_centers(zone, step_mm: float = 200.0):
    left, bottom, right, top = _points_bounds(zone.points_json)
    half_width = PALLET_WIDTH_MM / 2
    half_depth = PALLET_DEPTH_MM / 2
    margin = 60.0
    x = left + half_width + margin
    while x <= right - half_width - margin + 1e-6:
        y = bottom + half_depth + margin
        while y <= top - half_depth - margin + 1e-6:
            yield round(x, 3), round(y, 3)
            y += step_mm
        x += step_mm


def _build_plan(layout: Layout) -> list[dict]:
    zones = {feature.feature_code: feature for feature in layout.features}
    existing = {pallet.pallet_code: pallet for pallet in layout.pallets}
    reserved: list[tuple[float, float, float, float]] = []
    plan: list[dict] = []

    for seed in SEEDS:
        current = existing.get(seed["pallet_code"])
        if current is not None:
            if not current.is_simulated:
                raise RuntimeError(f"{current.pallet_code} 已被非模拟栈板占用")
            plan.append({
                "action": "existing",
                "pallet_code": current.pallet_code,
                "zone_id": current.zone_id,
                "x_mm": current.x_mm,
                "y_mm": current.y_mm,
                "visual_status": current.visual_status,
            })
            reserved.append(
                _rect_bounds(
                    current.x_mm,
                    current.y_mm,
                    current.width_mm,
                    current.depth_mm,
                    current.rotation_deg,
                )
            )
            continue

        selected = None
        for zone_code in seed["zones"]:
            zone = zones.get(zone_code)
            if zone is None or zone.storage_mode != "floor":
                continue
            for x_mm, y_mm in _candidate_centers(zone):
                rect = _rect_bounds(
                    x_mm, y_mm, PALLET_WIDTH_MM, PALLET_DEPTH_MM, 0
                )
                if any(_bounds_intersect(rect, item) for item in reserved):
                    continue
                try:
                    actual_zone = validate_pallet_position(
                        layout,
                        x_mm=x_mm,
                        y_mm=y_mm,
                        width_mm=PALLET_WIDTH_MM,
                        depth_mm=PALLET_DEPTH_MM,
                        rotation_deg=0,
                    )
                except ValueError:
                    continue
                if actual_zone.id != zone.id:
                    continue
                selected = (zone, x_mm, y_mm, rect)
                break
            if selected is not None:
                break
        if selected is None:
            raise RuntimeError(f"未找到 {seed['pallet_code']} 的安全落点")
        zone, x_mm, y_mm, rect = selected
        reserved.append(rect)
        plan.append({
            "action": "create",
            "pallet_code": seed["pallet_code"],
            "name": seed["name"],
            "visual_status": seed["visual_status"],
            "status_note": seed["status_note"],
            "zone_id": zone.id,
            "zone_code": zone.feature_code,
            "x_mm": x_mm,
            "y_mm": y_mm,
        })
    return plan


def run(database: Path, apply: bool, confirmation: str, backup_dir: Path) -> dict:
    database = database.resolve()
    if database.name != "factory_twin.sqlite3" or "factory_twin" not in database.as_posix():
        raise RuntimeError("只允许使用隔离的 factory_twin.sqlite3 候选数据库")
    if not database.is_file():
        raise FileNotFoundError(database)
    engine = create_database_engine(f"sqlite+pysqlite:///{database.as_posix()}")
    session_factory = create_session_factory(engine)
    backup = None
    try:
        with session_factory() as session:
            layout = session.scalar(select(Layout).where(Layout.id == LAYOUT_ID))
            if layout is None or layout.floor_code.upper() != "1F":
                raise RuntimeError("一楼候选布局不存在")
            plan = _build_plan(layout)
            result = {
                "mode": "apply" if apply else "dry-run",
                "database": str(database),
                "layout_id": layout.id,
                "seed_count": len(SEEDS),
                "create_count": sum(item["action"] == "create" for item in plan),
                "existing_count": sum(item["action"] == "existing" for item in plan),
                "formal_erp_inventory_touched": False,
                "plan": plan,
            }
            if not apply:
                return result
            if confirmation != CONFIRMATION:
                raise RuntimeError(f"必须显式提供 --confirmation {CONFIRMATION}")
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            backup = _verified_backup(
                database,
                backup_dir.resolve() / f"before-1f-turnover-visuals-{stamp}.sqlite3",
            )
            for item in plan:
                if item["action"] != "create":
                    continue
                session.add(PalletPlacement(
                    layout_id=layout.id,
                    pallet_code=item["pallet_code"],
                    name=item["name"],
                    zone_id=item["zone_id"],
                    x_mm=item["x_mm"],
                    y_mm=item["y_mm"],
                    z_mm=0,
                    width_mm=PALLET_WIDTH_MM,
                    depth_mm=PALLET_DEPTH_MM,
                    height_mm=PALLET_HEIGHT_MM,
                    rotation_deg=0,
                    color="#b7793f",
                    visual_status=item["visual_status"],
                    status_note=item["status_note"],
                    is_simulated=True,
                ))
            session.commit()
            session.expire_all()
            after = _build_plan(layout)
            if any(item["action"] != "existing" for item in after):
                raise RuntimeError("一楼周转模拟栈板写入后未达到幂等状态")
            result["backup"] = backup
            result["after"] = after
            return result
    finally:
        engine.dispose()


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="添加一楼隔离模拟栈板与生产周转状态")
    value.add_argument("--database", type=Path, required=True)
    value.add_argument("--backup-dir", type=Path, default=Path("factory_twin/data/backups"))
    value.add_argument("--apply", action="store_true")
    value.add_argument("--confirmation", default="")
    return value


def main() -> int:
    args = parser().parse_args()
    result = run(args.database, args.apply, args.confirmation, args.backup_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
