from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from contextlib import closing
from dataclasses import asdict
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session, sessionmaker


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.api.master_data_common import audit_master_change  # noqa: E402
from app.core.database import backup_to_nas, create_sqlite_engine  # noqa: E402
from app.models.material import Material  # noqa: E402
from app.models.supplier_paper_code import SupplierPaperCode  # noqa: E402
from app.models.user import User  # noqa: E402
from app.services.master_data_versioning import (  # noqa: E402
    apply_versioned_update,
    preview_versioned_update,
)


SUPPLIER_NAME = "森林阳光"
PAPER_CODE_ID = 70
OLD_PAPER_CODE = "0"
NEW_PAPER_CODE = "+"
EXPECTED_PAPER_NAME = "芯纸"
EXPECTED_PAPER_WEIGHT = 105
CONFIRMATION = "APPLY_FOREST_SUNSHINE_ZERO_TO_PLUS"
SOURCE = "scripts.admin.repair_forest_sunshine_plus_code"

MATERIAL_CODE_CHANGES: dict[int, tuple[str, str]] = {
    408: ("80IS7", "8+IS7"),
    409: ("WNI06", "WNI+6"),
    410: ("50105", "5+1+5"),
    411: ("605", "6+5"),
    412: ("61105", "611+5"),
    413: ("90BR9", "9+BR9"),
    414: ("D010D", "D+1+D"),
    415: ("D01RC", "D+1RC"),
}

PROTECTED_COUNT_TABLES = (
    "sales_orders",
    "sales_order_items",
    "supplier_requisition_orders",
    "supplier_requisition_order_items",
    "materials",
    "supplier_paper_codes",
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def database_checks(path: Path) -> dict[str, Any]:
    with closing(sqlite3.connect(path, timeout=30)) as connection:
        connection.execute("PRAGMA busy_timeout = 30000")
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
        table_names = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        counts = {
            table: int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
            for table in PROTECTED_COUNT_TABLES
            if table in table_names
        }
    return {
        "integrity_check": integrity,
        "foreign_key_violations": len(foreign_keys),
        "protected_counts": counts,
    }


def _paper_snapshot(row: SupplierPaperCode) -> dict[str, Any]:
    return {
        "id": row.id,
        "supplier_name": row.supplier_name,
        "code_char": row.code_char,
        "paper_name": row.paper_name,
        "gram_weight": row.gram_weight,
        "paper_grade": row.paper_grade,
        "paper_role": row.paper_role,
        "remark": row.remark,
        "is_active": row.is_active,
    }


def _target_composition(row: Material, old_code: str) -> str:
    source = row.paper_composition or ""
    old_token = f":{OLD_PAPER_CODE}={EXPECTED_PAPER_WEIGHT}g"
    new_token = f":{NEW_PAPER_CODE}={EXPECTED_PAPER_WEIGHT}g"
    expected_occurrences = old_code.count(OLD_PAPER_CODE)
    if source.count(old_token) != expected_occurrences:
        raise RuntimeError(
            f"材质 {row.id}/{old_code} 的组合说明与预期不一致，拒绝自动替换"
        )
    return source.replace(old_token, new_token)


def build_plan(db: Session) -> dict[str, Any]:
    paper = db.get(SupplierPaperCode, PAPER_CODE_ID)
    if paper is None:
        raise RuntimeError(f"基础纸种 ID {PAPER_CODE_ID} 不存在")
    if (
        paper.supplier_name != SUPPLIER_NAME
        or paper.paper_name != EXPECTED_PAPER_NAME
        or paper.gram_weight != EXPECTED_PAPER_WEIGHT
    ):
        raise RuntimeError("森林阳光 105g 芯纸主数据与预期不一致，拒绝修正")

    plus_conflict = db.scalar(
        select(SupplierPaperCode.id).where(
            SupplierPaperCode.supplier_name == SUPPLIER_NAME,
            SupplierPaperCode.code_char == NEW_PAPER_CODE,
            SupplierPaperCode.id != PAPER_CODE_ID,
        )
    )
    if plus_conflict is not None:
        raise RuntimeError(f"森林阳光已经存在其他 + 基础代码：ID {plus_conflict}")

    rows = {
        row.id: row
        for row in db.scalars(
            select(Material).where(Material.id.in_(MATERIAL_CODE_CHANGES))
        ).all()
    }
    if set(rows) != set(MATERIAL_CODE_CHANGES):
        missing = sorted(set(MATERIAL_CODE_CHANGES) - set(rows))
        raise RuntimeError(f"目标材质缺失：{missing}")

    old_materials = db.scalars(
        select(Material).where(
            Material.supplier_name == SUPPLIER_NAME,
            func.instr(Material.code, OLD_PAPER_CODE) > 0,
        )
    ).all()
    unexpected_old_ids = sorted(
        row.id for row in old_materials if row.id not in MATERIAL_CODE_CHANGES
    )
    if unexpected_old_ids:
        raise RuntimeError(
            f"森林阳光还有计划外的 0 组合材质：{unexpected_old_ids}，拒绝部分修正"
        )

    materials: list[dict[str, Any]] = []
    old_count = 0
    new_count = 0
    for material_id, (old_code, new_code) in MATERIAL_CODE_CHANGES.items():
        row = rows[material_id]
        if row.supplier_name != SUPPLIER_NAME:
            raise RuntimeError(f"材质 {material_id} 不属于森林阳光")
        if row.code == old_code:
            old_count += 1
            target_composition = _target_composition(row, old_code)
        elif row.code == new_code:
            new_count += 1
            target_composition = row.paper_composition or ""
            if f":{OLD_PAPER_CODE}={EXPECTED_PAPER_WEIGHT}g" in target_composition:
                raise RuntimeError(f"材质 {material_id} 已改新代码但组合说明仍含旧代码")
        else:
            raise RuntimeError(
                f"材质 {material_id} 当前代码 {row.code!r} 不是预期的 {old_code!r}/{new_code!r}"
            )

        conflict = db.scalar(
            select(Material.id).where(
                func.upper(Material.code) == new_code.upper(),
                Material.id != material_id,
            )
        )
        if conflict is not None:
            raise RuntimeError(f"新组合代码 {new_code} 已被材质 ID {conflict} 使用")
        materials.append(
            {
                "id": material_id,
                "old_code": old_code,
                "new_code": new_code,
                "current_code": row.code,
                "target_composition": target_composition,
                "current_version": row.version,
            }
        )

    paper_is_old = paper.code_char == OLD_PAPER_CODE
    paper_is_new = paper.code_char == NEW_PAPER_CODE
    if not paper_is_old and not paper_is_new:
        raise RuntimeError(f"基础纸种当前代码 {paper.code_char!r} 不在允许状态")
    if paper_is_old and new_count:
        raise RuntimeError("基础代码仍为 0，但部分组合已改为 +，拒绝继续")
    if paper_is_new and old_count:
        raise RuntimeError("基础代码已为 +，但部分组合仍为 0，拒绝继续")

    status = "ready" if paper_is_old and old_count == len(materials) else "already_applied"
    return {
        "status": status,
        "supplier_name": SUPPLIER_NAME,
        "paper_code": {
            "id": paper.id,
            "current": paper.code_char,
            "target": NEW_PAPER_CODE,
            "paper_name": paper.paper_name,
            "gram_weight": paper.gram_weight,
        },
        "materials": materials,
        "historical_snapshots_changed": False,
    }


def inspect_plan(database: Path) -> dict[str, Any]:
    engine = create_engine(
        f"sqlite+pysqlite:///{database.resolve().as_posix()}",
        connect_args={"check_same_thread": False, "timeout": 30},
        future=True,
    )

    @event.listens_for(engine, "connect")
    def configure_readonly(dbapi_connection: sqlite3.Connection, _record) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA busy_timeout = 30000")
            cursor.execute("PRAGMA foreign_keys = ON")
            cursor.execute("PRAGMA query_only = ON")
        finally:
            cursor.close()

    factory = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        with factory() as db:
            return build_plan(db)
    finally:
        engine.dispose()


def apply_repair(
    *,
    database: Path,
    backup_dir: Path,
    actor_username: str,
    expected_sha256: str,
) -> dict[str, Any]:
    source_sha256 = sha256_file(database)
    if source_sha256.lower() != expected_sha256.lower():
        raise RuntimeError("正式库 SHA-256 已变化，拒绝执行")
    before_checks = database_checks(database)
    if (
        before_checks["integrity_check"].lower() != "ok"
        or before_checks["foreign_key_violations"] != 0
    ):
        raise RuntimeError(f"正式库写入前检查失败：{before_checks}")

    preflight = inspect_plan(database)
    if preflight["status"] == "already_applied":
        return {
            "changed": False,
            "message": "森林阳光 0→+ 修正已经完整应用",
            "database_sha256": source_sha256,
            "checks": before_checks,
            "plan": preflight,
        }

    backup = backup_to_nas(
        source_path=database,
        backup_dir=backup_dir,
        filename_suffix="_FOREST_SUNSHINE_ZERO_TO_PLUS_BEFORE_UPDATE",
        keep_regular=1_000_000,
    )
    backup_checks = database_checks(backup.path)
    backup_plan = inspect_plan(backup.path)
    if (
        backup.integrity_check.lower() != "ok"
        or backup.size <= 0
        or backup_checks != before_checks
        or backup_plan != preflight
        or sha256_file(database).lower() != source_sha256.lower()
    ):
        raise RuntimeError("修正前备份未通过完整性验证")

    engine = create_sqlite_engine(database, busy_timeout_ms=30_000)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        with factory() as db:
            actor = db.scalar(select(User).where(User.username == actor_username))
            if actor is None or not actor.is_active or actor.role not in {"admin", "boss"}:
                raise RuntimeError("执行人必须是启用的 admin 或 boss 账号")
            live_plan = build_plan(db)
            if live_plan["status"] != "ready":
                raise RuntimeError("备份后数据状态已变化，拒绝重复或部分写入")

            paper = db.get(SupplierPaperCode, PAPER_CODE_ID)
            if paper is None:
                raise RuntimeError("备份后基础纸种记录消失")
            paper_before = _paper_snapshot(paper)
            paper.code_char = NEW_PAPER_CODE
            db.flush()
            audit_master_change(
                db,
                user=actor,
                action="UPDATE",
                resource="SupplierPaperCode",
                resource_id=paper.id,
                details={
                    "before": paper_before,
                    "after": _paper_snapshot(paper),
                    "reason": "老板明确授权森林阳光 105g 芯纸代码由 0 修正为 +",
                    "source": SOURCE,
                },
            )

            for item in live_plan["materials"]:
                material = db.get(Material, item["id"])
                if material is None:
                    raise RuntimeError(f"材质 {item['id']} 在事务中消失")
                updates = {
                    "code": item["new_code"],
                    "paper_composition": item["target_composition"],
                }
                preview = preview_versioned_update(
                    db,
                    object_type="material",
                    entity=material,
                    updates=updates,
                    expected_version=material.version,
                    user=actor,
                    action="system_consistency_fix",
                )
                apply_versioned_update(
                    db,
                    object_type="material",
                    entity=material,
                    updates=updates,
                    expected_version=material.version,
                    user=actor,
                    reason="森林阳光基础纸种代码 0→+ 的现行组合主数据同步修正",
                    source=SOURCE,
                    action="system_consistency_fix",
                    confirmation_token=preview["confirmation_token"],
                )
            db.commit()
    finally:
        engine.dispose()

    after_checks = database_checks(database)
    if (
        after_checks["integrity_check"].lower() != "ok"
        or after_checks["foreign_key_violations"] != 0
        or after_checks["protected_counts"] != before_checks["protected_counts"]
    ):
        raise RuntimeError(
            "修正后验证失败；保留现场并使用修正前备份恢复："
            f"{backup.path}"
        )
    result_plan = inspect_plan(database)
    if result_plan["status"] != "already_applied":
        raise RuntimeError("修正后主数据回读不完整")
    return {
        "changed": True,
        "database_sha256_before": source_sha256,
        "database_sha256_after": sha256_file(database),
        "backup": {
            **asdict(backup),
            "path": str(backup.path),
            "checks": backup_checks,
        },
        "checks_before": before_checks,
        "checks_after": after_checks,
        "plan_after": result_plan,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="将森林阳光临时基础代码 0 精确修正为 +，并同步 8 条现行组合主数据"
    )
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--actor", default="admin")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm")
    parser.add_argument("--confirm-service-stopped", action="store_true")
    parser.add_argument("--expected-sha256")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    database = args.database.resolve()
    if not database.is_file():
        raise SystemExit(f"数据库不存在：{database}")
    if not args.apply:
        result = {
            "apply": False,
            "database": str(database),
            "database_sha256": sha256_file(database),
            "checks": database_checks(database),
            "plan": inspect_plan(database),
        }
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0
    if args.confirm != CONFIRMATION:
        raise SystemExit(f"--apply 必须同时提供 --confirm {CONFIRMATION}")
    if not args.confirm_service_stopped:
        raise SystemExit("正式写入前必须停止 ERP，并提供 --confirm-service-stopped")
    if not args.expected_sha256:
        raise SystemExit("正式写入必须提供 --expected-sha256")
    backup_dir = (args.backup_dir or database.parent / "backups").resolve()
    result = apply_repair(
        database=database,
        backup_dir=backup_dir,
        actor_username=args.actor,
        expected_sha256=args.expected_sha256,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
