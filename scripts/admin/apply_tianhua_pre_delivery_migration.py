from __future__ import annotations
import argparse,json,os,sqlite3,sys
from pathlib import Path
from alembic import command
from alembic.config import Config

ROOT=Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))
from app.core.config import load_settings
from app.core.database import backup_to_nas

REVISION="w95q2r3s4t83"
TABLES={"tianhua_pre_delivery_import_batches","tianhua_pre_delivery_import_items","tianhua_pre_delivery_drafts","tianhua_pre_delivery_draft_items"}
PROTECTED=("sales_orders","sales_order_items","sales_deliveries","sales_delivery_items","finance_return_receipts","finance_return_receipt_items","finance_statements","finance_statement_items")


def snapshot(path):
    with sqlite3.connect(path) as c:
        tables={r[0] for r in c.execute("select name from sqlite_master where type='table'")}
        counts={t:c.execute(f'select count(*) from "{t}"').fetchone()[0] for t in PROTECTED if t in tables}
        rev=c.execute("select version_num from alembic_version").fetchone()[0]
        return {"tables":tables,"counts":counts,"revision":rev,"integrity":c.execute("pragma integrity_check").fetchone()[0],"fk":len(c.execute("pragma foreign_key_check").fetchall())}


def apply(database:Path,backup_dir:Path):
    before=snapshot(database)
    if before["revision"]==REVISION:
        if TABLES-before["tables"]: raise RuntimeError("revision 已更新但隔离表缺失")
        return {"changed":False,"revision":REVISION,"backup":None}
    backup=backup_to_nas(source_path=database,backup_dir=backup_dir,filename_suffix="_TIANHUA_PRE_DELIVERY_BEFORE_MIGRATION",keep_regular=100)
    if backup.integrity_check.lower()!="ok": raise RuntimeError("备份完整性检查失败")
    old=os.environ.get("ERP_DATABASE_PATH");os.environ["ERP_DATABASE_PATH"]=str(database)
    try: command.upgrade(Config(str(ROOT/"alembic.ini")),REVISION)
    finally:
        if old is None: os.environ.pop("ERP_DATABASE_PATH",None)
        else: os.environ["ERP_DATABASE_PATH"]=old
    after=snapshot(database)
    if after["revision"]!=REVISION or TABLES-after["tables"] or before["counts"]!=after["counts"] or after["integrity"]!="ok" or after["fk"]:
        raise RuntimeError("迁移后验证失败，请使用备份恢复")
    return {"changed":True,"revision_before":before["revision"],"revision":REVISION,"backup":{"path":str(backup.path),"sha256":backup.sha256,"size":backup.size,"integrity_check":backup.integrity_check},"protected_counts":after["counts"],"integrity_check":after["integrity"],"foreign_key_violations":after["fk"]}


def main():
    settings=load_settings(); p=argparse.ArgumentParser()
    p.add_argument("--database",type=Path,default=settings.database_path)
    p.add_argument("--backup-dir",type=Path,default=ROOT/"data"/"backups")
    a=p.parse_args(); print(json.dumps(apply(a.database.resolve(),a.backup_dir.resolve()),ensure_ascii=False,indent=2))
if __name__=="__main__": main()
