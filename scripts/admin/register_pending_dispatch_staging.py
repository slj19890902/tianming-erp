"""Explicit, audited logical staging registration; never creates physical placement."""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from app.models.audit import OperationLog
from app.models.warehouse_inventory import WarehouseLocation
from app.services.audit_log import append_audit_event
from app.services.warehouse_relocation_pending import is_pending_relocation_location

NAME = "一楼成品待送区（待归位）"
NOTE = "管理员确认逻辑暂存于一楼成品待送区；暂不限制数量，不分配具体实物位，后续手工归位；不代表地图空间放置完成。"
ACTION = "warehouse.pending.dispatch.register"
KEY = "STAGING403-20260914-logical-staging"
FORMAL = Path("D:/TianmingERP/shared/data/carton_erp.sqlite3").resolve()


def protected_facts(db):
    conn = db.connection()
    tables = conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").scalars().all()
    result = {}
    for table in tables:
        if table == 'operation_logs':
            continue
        quoted = '"' + table.replace('"', '""') + '"'
        rows = [dict(r._mapping) for r in conn.exec_driver_sql('SELECT * FROM ' + quoted)]
        if table == 'warehouse_locations':
            for row in rows:
                if row['id'] == 656:
                    for field in ('location_name', 'remarks', 'address_version', 'updated_at'):
                        row.pop(field, None)
        result[table] = hashlib.sha256(repr(sorted(repr(r) for r in rows)).encode()).hexdigest()
    return result


def register(db: Session):
    row = db.get(WarehouseLocation, 656)
    if not is_pending_relocation_location(row):
        raise ValueError("专用待归位身份变化，拒绝登记")
    existing = db.scalar(select(OperationLog).where(OperationLog.action_code == ACTION,
                                                   OperationLog.batch_id == KEY))
    if existing:
        if row.location_name != NAME or NOTE not in (row.remarks or ""):
            raise ValueError("登记后的位置内容已变化，不能用原请求覆盖")
        return {"location_id": row.id, "name": NAME, "replayed": True}
    if row.location_name != "盘点待归位":
        raise ValueError("位置名称已变化，须重新核对")
    before = {"name": row.location_name, "remarks": row.remarks, "version": row.address_version}
    row.location_name = NAME
    row.remarks = ((row.remarks or "") + "\n" + NOTE).strip()
    row.address_version = int(row.address_version or 0) + 1
    db.flush()
    result = {"location_id": row.id, "name": NAME, "replayed": False,
              "physical_location_unchanged": True, "quantity_limit": None}
    append_audit_event(db, event_category="business", result="success", source="script",
        module_code="warehouse", action_code=ACTION, resource="WarehouseLocation",
        entity_id=row.id, batch_id=KEY, operator_name="Codex（老板本次确认授权）",
        description="登记一楼成品待送逻辑暂存；不变更实物位置或库存数量",
        details={"authorization":"2026-09-14老板确认待归位暂不设置数量限制",
                 "before":before,"after":result})
    return result


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--database", type=Path, required=True)
    p.add_argument("--expected-sha", required=True)
    p.add_argument("--apply", action="store_true")
    p.add_argument("--formal-authorized", action="store_true")
    args = p.parse_args()
    path = args.database.resolve(strict=True)
    if path == FORMAL and not (args.apply and args.formal_authorized):
        raise ValueError("正式写入必须显式授权；演练请使用独立副本")
    if hashlib.sha256(path.read_bytes()).hexdigest() != args.expected_sha:
        raise ValueError("数据库与已验证备份不一致")
    engine = create_engine("sqlite:///" + path.as_posix())
    with Session(engine) as db:
        db.connection().exec_driver_sql("PRAGMA foreign_keys=ON")
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        before = protected_facts(db)
        result = register(db)
        assert protected_facts(db) == before, '范围外业务事实变化，回滚'
        assert db.connection().exec_driver_sql("PRAGMA integrity_check").scalar() == "ok"
        assert not db.connection().exec_driver_sql("PRAGMA foreign_key_check").fetchall()
        if args.apply:
            db.commit()
        else:
            db.rollback()
    print(json.dumps({**result,"committed":args.apply}, ensure_ascii=False))


if __name__ == "__main__":
    main()
