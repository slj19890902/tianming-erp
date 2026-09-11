"""Isolated, additive migration rehearsal for a signed desktop update package.

This is evidence for the later stop/backup/apply step, never an apply shortcut.
"""
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import uuid

from desktop_assistant.storage import database_info, extract_verified, sha, write_json


def quote(name):
    return '"' + name.replace('"', '""') + '"'


def schema(database):
    with closing(sqlite3.connect(database.resolve().as_uri()+'?mode=ro', uri=True)) as db:
        rows = db.execute('SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name').fetchall()
    return hashlib.sha256(json.dumps(rows,ensure_ascii=False).encode('utf-8')).hexdigest()


def facts(database, columns=None):
    result = {}
    with closing(sqlite3.connect(database.resolve().as_uri()+'?mode=ro', uri=True)) as db:
        db.execute('PRAGMA query_only=ON')
        if columns is None:
            tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name<>'alembic_version'")]
            columns = {table: [r[1] for r in db.execute('PRAGMA table_info('+quote(table)+')')] for table in tables}
        for table, names in columns.items():
            actual = {r[1] for r in db.execute('PRAGMA table_info('+quote(table)+')')}
            if not set(names) <= actual:
                raise ValueError('升级删除既有业务表或字段，必须专项评审：'+table)
            fields = ','.join(map(quote,names))
            digest = hashlib.sha256(); count=0
            for row in db.execute('SELECT '+fields+' FROM '+quote(table)+' ORDER BY '+fields):
                encoded = json.dumps(row, ensure_ascii=False, separators=(',', ':'),
                    default=lambda value: {'binary_sha256':hashlib.sha256(value).hexdigest()}).encode('utf-8')
                digest.update(len(encoded).to_bytes(8,'big'));digest.update(encoded);count+=1
            result[table]={'rows':count,'sha256':digest.hexdigest()}
    return {'columns':columns,'tables':result}


def files(root):
    result={}
    for path in root.rglob('*'):
        if path.is_symlink() or path.is_junction():
            raise ValueError('托管数据含外部链接，不能自动复制演练')
        if path.is_file() and path.relative_to(root).as_posix() not in {
                'data/carton_erp.sqlite3','data/carton_erp.sqlite3-wal','data/carton_erp.sqlite3-shm'}:
            result[path.relative_to(root).as_posix()]=sha(path)
    return result


def run_migration(release, shared, revision, log, environment=None):
    # No inherited ERP paths, credentials, .env or Python configuration.
    keep={'SYSTEMROOT','WINDIR','PATH','PATHEXT','COMSPEC','TEMP','TMP'}
    env={key:value for key,value in os.environ.items() if key.upper() in keep}
    env.update(ERP_ENVIRONMENT='test', ERP_SECRET_KEY='isolated-desktop-migration-rehearsal-only',
        ERP_DATABASE_PATH=str(shared/'data/carton_erp.sqlite3'),
        ERP_BACKUP_DIR=str(shared/'rehearsal-backups'), PYTHONPATH=str(release), PYTHONUTF8='1')
    if environment is not None:
        env.update(environment)
        env['ERP_DATABASE_PATH'] = str(shared/'data/carton_erp.sqlite3')
    with log.open('wb') as output:
        result=subprocess.run([str(release/'runtime/python.exe'),'-X','utf8','-m','alembic','upgrade',revision],
            cwd=release,env=env,stdout=output,stderr=output,timeout=600,creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise ValueError('隔离升级失败，未修改托管数据库；请查看演练日志')


def rehearse(manager, package):
    source=manager.root/'shared'; database=source/'data/carton_erp.sqlite3'
    before_info=database_info(database);before=facts(database);before_files=files(source)
    job=manager.root/'staging'/('migration-'+uuid.uuid4().hex)
    job.mkdir();report_path=job/'report.json'
    report={'status':'running','source_revision':before_info['revision'],'package_sha256':sha(package),
        'created_at':datetime.now(timezone.utc).isoformat(),'source_facts':before,
        'scope':'isolated_additive_rehearsal_only','report_path':str(report_path)}
    write_json(report_path,report)
    try:
        release=job/'release';manifest=extract_verified(package,release,manager.public_key)
        if manifest.get('type')!='tianming.release.v1':
            raise ValueError('不是已签名ERP发布包')
        if any((release/p).exists() for p in ('data','.env','static/uploads','factory_twin/data')):
            raise ValueError('程序包不得携带业务数据或配置')
        shared=job/'shared'
        def ignore(folder,names):
            return [name for name in names if Path(folder)==source/'data' and name in
                    ('carton_erp.sqlite3','carton_erp.sqlite3-wal','carton_erp.sqlite3-shm')]
        shutil.copytree(source,shared,ignore=ignore)
        copy=shared/'data/carton_erp.sqlite3'
        with closing(sqlite3.connect(database.resolve().as_uri()+'?mode=ro',uri=True)) as src:
            with closing(sqlite3.connect(copy)) as dst:src.backup(dst)
        if facts(copy)!=before or files(shared)!=before_files:
            raise ValueError('复制期间数据变化或附件不一致，请重新演练')
        run_migration(release,shared,manifest['revision'],job/'migration.log')
        after_info=database_info(copy);after=facts(copy,before['columns'])
        if after_info['revision']!=manifest['revision']:
            raise ValueError('隔离升级未达到发布包目标版本')
        if after!=before:
            raise ValueError('升级改变既有业务事实，必须专项评审，未修改托管数据库')
        if any(count for table, count in after_info['counts'].items()
               if table not in before_info['counts'] and not table.startswith('sqlite_')):
            raise ValueError('升级新增了数据，必须专项评审')
        if files(shared)!=before_files:
            raise ValueError('升级改变附件或配置，必须专项评审')
        if facts(database)!=before or files(source)!=before_files:
            raise ValueError('原系统数据已变化，本次演练结果不能用于更新')
        report.update(status='passed',target_revision=after_info['revision'],result=after_info,
            result_schema=schema(shared/'data/carton_erp.sqlite3'),
            attachments_verified=len(before_files),source_unchanged=True)
    except Exception as error:
        report.update(status='failed',error=str(error));write_json(report_path,report);raise
    write_json(report_path,report)
    return report
