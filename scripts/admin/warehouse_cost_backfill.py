"""Preview by default. Applying requires a fresh verified backup and exact preview fingerprint."""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from app.models.user import User
from app.services.inventory_cost_backfill import preview,adopt


def engine_for(path, readonly):
    def connect():
        db=sqlite3.connect(path.as_uri()+f"?mode={'ro' if readonly else 'rw'}",uri=True)
        db.execute("PRAGMA foreign_keys=ON")
        if readonly:db.execute("PRAGMA query_only=ON")
        return db
    return create_engine("sqlite://",creator=connect)


def run():
    parser=argparse.ArgumentParser()
    parser.add_argument("database",type=Path);parser.add_argument("--apply",action="store_true")
    parser.add_argument("--expected");parser.add_argument("--operator",type=int);parser.add_argument("--backup",type=Path)
    parser.add_argument("--batch",default="warehouse-cost-20260911");parser.add_argument("--report",type=Path)
    args=parser.parse_args();path=args.database.resolve()
    if not path.is_file():raise ValueError("数据库不存在")
    backup_info=None
    if args.apply:
        if not args.expected or not args.operator or not args.backup:raise ValueError("缺预览指纹、管理员或备份路径")
        backup=args.backup.resolve()
        if backup.exists() or backup==path:raise ValueError("备份必须为新的独立文件")
        backup.parent.mkdir(parents=True,exist_ok=True)
        with sqlite3.connect(path.as_uri()+"?mode=ro",uri=True) as src,sqlite3.connect(str(backup)) as dst:
            src.backup(dst)
            if dst.execute("PRAGMA integrity_check").fetchall()!=[("ok",)] or dst.execute("PRAGMA foreign_key_check").fetchall():
                raise ValueError("备份完整性校验失败")
        with Session(engine_for(backup,True),autoflush=False) as session:
            if preview(session)["fingerprint"]!=args.expected:raise ValueError("备份与预览不符，禁止补价")
        backup_info=dict(path=str(backup),sha256=hashlib.sha256(backup.read_bytes()).hexdigest(),integrity="ok",foreign_keys=0)
        engine=engine_for(path,False)
        with engine.connect() as con:
            con.exec_driver_sql("BEGIN IMMEDIATE")
            try:
                with Session(bind=con,autoflush=False) as session:
                    user=session.get(User,args.operator)
                    result=adopt(session,user=user,expected=args.expected,batch_id=args.batch)
                    session.flush()
                    if con.exec_driver_sql("PRAGMA foreign_key_check").fetchall():raise ValueError("外键校验失败")
                    con.commit()
            except Exception:
                con.rollback();raise
    else:
        with Session(engine_for(path,True),autoflush=False) as session:
            session.connection().exec_driver_sql("BEGIN")
            result=preview(session);session.rollback()
    output=dict(database=str(path),applied=args.apply,backup=backup_info,result=result)
    encoded=json.dumps(output,ensure_ascii=False,indent=2,default=str)
    if args.report:
        args.report.parent.mkdir(parents=True,exist_ok=True);args.report.write_text(encoded,encoding="utf-8")
    print(json.dumps(output,ensure_ascii=True,default=str))
if __name__=="__main__":run()
