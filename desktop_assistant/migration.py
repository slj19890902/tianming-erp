"""Isolated, additive migration rehearsal for a signed desktop update package.

This is evidence for the later stop/backup/apply step, never an apply shortcut.
"""
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess
import uuid

from desktop_assistant.storage import database_info, extract_verified, sha, write_json
from desktop_assistant.runtime_platform import prepare_runtime, runtime_python, process_options


def quote(name):
    return '"' + name.replace('"', '""') + '"'


def canonical_table_sql(sql):
    """Only unordered table constraints are sorted; columns/expressions stay exact.

    SQLAlchemy reflects constraints into sets, so process hash seeds can reorder
    identical CHECK/FK definitions. Do not ignore names, expressions or columns.
    """
    if not sql or not re.match(r'^CREATE\s+TABLE\b', sql, re.I):
        return sql
    quote_char=None; depth=0; start=None; part=None; definitions=[]; index=0
    while index < len(sql):
        char=sql[index]
        if quote_char:
            if char == quote_char:
                if quote_char != ']' and index+1 < len(sql) and sql[index+1] == quote_char:
                    index+=2
                    continue
                quote_char=None
        elif char in ('"', "'", '`', '['):
            quote_char=']' if char == '[' else char
        elif char == '(':
            if depth == 0:
                start=index; part=index+1
            depth+=1
        elif char == ')':
            depth-=1
            if depth == 0 and start is not None:
                definitions.append(sql[part:index].strip())
                columns=[]; constraints=[]
                for definition in definitions:
                    target=constraints if re.match(r'^(?:CONSTRAINT\b|PRIMARY\s+KEY\b|FOREIGN\s+KEY\b|UNIQUE\b|CHECK\b)',definition,re.I) else columns
                    target.append(definition)
                return (sql[:start], columns, sorted(constraints), sql[index+1:])
        elif char == ',' and depth == 1:
            definitions.append(sql[part:index].strip()); part=index+1
        index+=1
    raise ValueError('无法完整解析表结构，保留原数据库并停止升级')


def schema(database, *, legacy=False):
    with closing(sqlite3.connect(database.resolve().as_uri()+'?mode=ro', uri=True)) as db:
        rows = [(kind,name,table,canonical_table_sql(sql) if kind=='table' and not legacy else sql)
                for kind,name,table,sql in db.execute('SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name')]
    return hashlib.sha256(json.dumps(rows,ensure_ascii=False).encode('utf-8')).hexdigest()


def recovery_schema(report, report_path):
    if report.get('schema_fingerprint_version') == 2:
        return report['result_schema']
    if report.get('schema_fingerprint_version') not in (None, 1):
        raise ValueError('未知结构校验版本，保持停服')
    # Preserve the original report bytes and SHA. Its original raw SQL hash must
    # still authenticate the retained isolated DB before deriving an order-neutral hash.
    isolated=report_path.parent/'shared/data/carton_erp.sqlite3'
    if not isolated.is_file() or schema(isolated,legacy=True) != report['result_schema']:
        raise ValueError('旧演练结构证据缺失或变化，保持停服')
    return schema(isolated)


def facts(database, columns=None):
    result = {}
    with closing(sqlite3.connect(database.resolve().as_uri()+'?mode=ro', uri=True)) as db:
        db.execute('PRAGMA query_only=ON')
        if columns is None:
            tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name<>'alembic_version'")]
            columns = {table: [r[1] for r in db.execute('PRAGMA table_xinfo('+quote(table)+')')] for table in tables}
        for table, names in columns.items():
            actual = {r[1] for r in db.execute('PRAGMA table_xinfo('+quote(table)+')')}
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
    # pathlib glob/walk can suppress scanning errors. A complete inventory must
    # fail if any directory cannot be read, including unregistered evidence.
    from desktop_assistant.cleanup import local_path
    root = local_path(root)
    if not root.is_dir():
        raise ValueError('完整文件清单要求已有共享目录')
    result={}
    pending = [root]
    while pending:
        with os.scandir(pending.pop()) as entries:
            for entry in entries:
                path = Path(entry.path)
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                    raise ValueError('托管数据含外部链接，不能自动复制演练')
                if stat.S_ISDIR(info.st_mode):
                    pending.append(path)
                elif stat.S_ISREG(info.st_mode):
                    name = path.relative_to(root).as_posix()
                    if name not in {'data/carton_erp.sqlite3', 'data/carton_erp.sqlite3-wal',
                                    'data/carton_erp.sqlite3-shm'}:
                        result[name] = sha(path)
                else:
                    raise ValueError('托管数据含特殊文件，不能生成完整文件清单')
    return result


def prepare_migration(release, shared, revision, environment=None, *, release_manifest):
    """Validate the exact signed migration environment without spawning a writer."""
    # Callers pass the manifest authenticated by extraction or package signature.
    if revision != release_manifest.get('revision'):
        raise ValueError('迁移目标与签名发布包不一致')
    from desktop_assistant import mac_published_runtime
    system_service = mac_published_runtime.selected()
    if system_service:
        identity = Path(release).parent.name
        approved, actual_manifest = mac_published_runtime.resolve(
            mac_published_runtime.ROOT, identity, mac_published_runtime._read(mac_published_runtime.PUBLIC),
            kind='migration')
        if approved != release or actual_manifest != release_manifest:
            raise ValueError('系统迁移不允许从可写缓存或另一签名副本执行')
    else:
        prepare_runtime(release, release_manifest)
    python = runtime_python(release, release_manifest, runnable=True)
    database = shared / 'data/carton_erp.sqlite3'
    if not database.is_file():
        raise ValueError('迁移数据库不存在，拒绝创建空库')
    if not database.is_absolute() or database.resolve() != database.absolute() or database.is_symlink():
        raise ValueError('迁移数据库不能通过链接访问')
    keep = {'SYSTEMROOT', 'WINDIR', 'PATH', 'PATHEXT', 'COMSPEC', 'TEMP', 'TMP'}
    env = {key: value for key, value in os.environ.items() if key.upper() in keep}
    env.update(ERP_ENVIRONMENT='test', ERP_SECRET_KEY='isolated-desktop-migration-rehearsal-only',
               ERP_BACKUP_DIR=str(shared / 'rehearsal-backups'))
    if environment is not None:
        env.update(environment)
    # Neither restored settings nor the parent's environment can redirect code
    # or the database. The original entry point stays supported for old Windows
    # releases; new native Mac releases require the guarded migration entry.
    for name in list(env):
        if name.startswith('PYTHON') or name in {'OPENAI_API_KEY', 'DEEPSEEK_API_KEY'} or name.lower() in (
                'http_proxy', 'https_proxy', 'all_proxy', 'ftp_proxy'):
            env.pop(name, None)
    env.update(ERP_DATABASE_PATH=str(database), PYTHONPATH=str(release), PYTHONUTF8='1',
               PYTHONNOUSERSITE='1')
    native = release_manifest.get('runtime_platform') == 'macos-arm64'
    if native or environment is None:
        env['ERP_HOME_REHEARSAL'] = '1'
    if system_service:
        env = mac_published_runtime.environment(env)
    module = 'alembic'
    if native:
        env['PYTHONDONTWRITEBYTECODE'] = '1'
        entry = release / 'desktop_assistant/migration_entry.py'
        expected = release_manifest.get('files', {}).get('desktop_assistant/migration_entry.py')
        if not expected or not entry.is_file() or entry.is_symlink() or sha(entry) != expected:
            raise ValueError('Mac迁移安全入口缺失或校验失败')
        module = 'desktop_assistant.migration_entry'
    from desktop_assistant.home_process import command
    arguments = ([str(python), '-I', '-B', str(entry), 'upgrade', revision] if system_service else
                 [str(python), '-X', 'utf8', '-m', module, 'upgrade', revision])
    arguments = command(arguments, env)
    return arguments, env, system_service


def run_migration(release, shared, revision, log, environment=None, *, release_manifest):
    # Repeat preflight immediately before spawn: an earlier online preflight
    # does not authorize a changed package, runtime, database path or entry.
    arguments, env, system_service = prepare_migration(
        release, shared, revision, environment=environment, release_manifest=release_manifest)
    with log.open('wb') as output:
        if system_service:
            process = subprocess.Popen(arguments, cwd=release, env=env, stdout=output, stderr=output,
                                       **process_options())
            interrupted = False
            while True:
                try:
                    code = process.wait()
                    break
                except KeyboardInterrupt:
                    interrupted = True  # Keep observing this writer; do not kill/abandon it.
            if interrupted:
                raise ValueError('已等候中断的系统迁移进程退出，保持停服并保留现场待核验')
        else:
            code = subprocess.run(arguments, cwd=release, env=env, stdout=output, stderr=output,
                                  timeout=600, **process_options()).returncode
    if code:
        raise ValueError('升级子进程失败，保留现场；不得启动未核验数据库，请查看迁移日志')


def rehearse(manager, package):
    from desktop_assistant import mac_published_runtime
    approved = (manager._execution_release(sha(package), kind='migration')
                if mac_published_runtime.selected() else None)
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
        run_migration(approved or release,shared,manifest['revision'],job/'migration.log', release_manifest=manifest)
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
            schema_fingerprint_version=2,
            result_schema=schema(shared/'data/carton_erp.sqlite3'),
            attachments_verified=len(before_files),source_unchanged=True)
    except Exception as error:
        report.update(status='failed',error=str(error));write_json(report_path,report);raise
    write_json(report_path,report)
    # Version-2 reports contain the schema proof needed for interrupted-update
    # recovery; successful rehearsal payloads are no longer needed afterward.
    from desktop_assistant.cleanup import remove_owned_tree
    cleanup = {}
    for name in ('release', 'shared'):
        try:
            cleanup[name] = {'deleted_bytes': remove_owned_tree(job/name, job)}
        except (OSError, ValueError) as error:
            cleanup[name] = {'retained': str(error)}
    report['temporary_cleanup'] = cleanup
    write_json(report_path,report)
    return report
