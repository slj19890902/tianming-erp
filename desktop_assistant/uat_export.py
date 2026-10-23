"""Dedicated-UID online capture, entered only by authenticated management.

No service lifecycle, credential store, Manager constructor, or formal writes.
Raw snapshots never leave this UID's private staging directory.
"""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time

from desktop_assistant import uat_export_contract as c
from desktop_assistant import mac_keychain_install as boot
from desktop_assistant import mac_service_manage as manage
from desktop_assistant import mac_published_runtime as published
from desktop_assistant import mac_system_service as system


def _json(raw):
    def unique(items):
        value = {}
        for key, item in items:
            if key in value: raise ValueError('导出来源 JSON 字段重复')
            value[key] = item
        return value
    return json.loads(raw, object_pairs_hook=unique,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError('非有限 JSON')))


def inspect_source(context, public, folders, recheck):
    root = folders['root']
    state_raw, state = manage.record(root, 'state.json', context)
    if (not c.sha(state.get('current')) or state.get('manual_stop') is True
            or state.get('onboarding_pending') or state.get('operation') == 'migration_running'
            or 'failed' in str(state.get('operation', ''))):
        raise ValueError('必须是无未决维护、正常运行的完整实例')
    tree, manifest = published.resolve(c.ROOT, state['current'], public, kind='server')
    process_raw = c.read(c.ROOT/'control/process.json', uid=context['uid'], limit=boot.LIMIT)
    process = _json(process_raw)
    from desktop_assistant.manager import Manager
    # This method only validates a kernel process; do not construct Manager,
    # which initializes directories, or call _environment (credential access).
    item = Manager._recorded_process(None, process)
    if (not item or Path(process['exe']) != tree/'runtime/bin/python3.12'
            or item[0].uids().real != context['uid'] or item[0].uids().effective != context['uid']):
        raise ValueError('运行实例身份与签名当前程序不符')
    ready = c.read(c.ROOT/'control'/(str(process.get('nonce'))+'.ready'), uid=context['uid'], limit=64)
    if ready != str(process['pid']).encode(): raise ValueError('运行实例尚未完成启动')
    # Parse shared configuration privately and retain only six path settings.
    # Known inline values never enter this source result or a copied config.
    env_path = c.ROOT/'shared/environment.json'
    env_raw = c.read(env_path, uid=context['uid'], limit=boot.LIMIT)
    environment = c.environment_paths(env_raw)
    if env_path.stat().st_mtime > process['created']:
        raise ValueError('运行路径配置在启动后曾变化')
    shared = c.ROOT/'shared'
    # Logical DB references only identify real files when their runtime roots
    # agree. Do not export stale files left in a default directory after an
    # operator configured a different root.
    fixed_roots = {'ERP_FILE_STORAGE_DIR':'data/private_uploads',
        'ERP_LEGACY_UPLOAD_DIR':'legacy_uploads','ERP_PDF_TRAINING_DIR':'data/pdf_training_samples',
        'ERP_INVOICE_ATTACHMENT_DIR':'data/invoice_attachments',
        'ERP_SUPPLIER_INVOICE_ATTACHMENT_DIR':'data/supplier_invoice_attachments'}
    for name,relative in fixed_roots.items():
        if name in environment and Path(environment[name].replace('${SHARED}',str(shared))) != shared/relative:
            raise ValueError('业务原件运行路径与固定受管目录不符')
    # published.resolve(kind='server') verifies server/data -> shared/data.
    # This therefore matches the application's resolved default, provided the
    # real runtime map exists. Missing files never fall back to static templates.
    map_name = environment.get('ERP_TWIN_LAYOUT_RUNTIME_PATH', '${SHARED}/data/layout_runtime/twin_layout_v1.json')
    map_path = Path(map_name.replace('${SHARED}', str(shared)))
    try: map_relative = map_path.relative_to(shared).as_posix()
    except ValueError: raise ValueError('运行地图不在固定受管共享目录') from None
    with c.regular(shared/'data/carton_erp.sqlite3', uid=context['uid'], mutable=True):
        with closing(sqlite3.connect((shared/'data/carton_erp.sqlite3').as_uri()+'?mode=ro', uri=True)) as db:
            db.execute('PRAGMA query_only=ON')
            versions = db.execute('SELECT version_num FROM alembic_version').fetchall()
    if len(versions) != 1: raise ValueError('来源迁移版本不唯一')
    result = {'application_package_sha256': state['current'], 'application_git_sha': manifest['git_sha'],
        'revision': versions[0][0], 'state_sha256': c.digest(state_raw),
        'process_sha256': c.digest(process_raw), 'environment_sha256': c.digest(env_raw),
        'map_relative': map_relative, 'pid': process['pid'], 'process_created': process['created']}
    c.validate_source(result)
    c.reject_environment_credentials(env_raw, c.canonical(result))
    recheck()
    return result


def _source_relative(reference):
    if not isinstance(reference, str): raise ValueError('附件路径不是文本')
    if reference.startswith('private:'):
        value = 'data/private_uploads/' + reference[len('private:'):]
    elif reference.startswith('/static/uploads/'):
        value = 'legacy_uploads/' + reference[len('/static/uploads/'):]
    elif reference.startswith(str(c.ROOT/'shared') + '/'):
        value = reference[len(str(c.ROOT/'shared'))+1:]
    else:
        value = reference
    return c.original_relative(value)


def artifact_references(database):
    """Exact registered reference columns; no directory-wide data/config copy."""
    found = {}
    pdf = []
    with closing(sqlite3.connect(database.as_uri()+'?mode=ro', uri=True)) as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        specs = [('product_drawings','image_path',None,None),
                 ('product_drawings','thumbnail_path',None,None),
                 ('pdf_order_training_samples','file_path','file_sha256',None),
                 ('shared_file_versions','reference','sha256',None),
                 ('finance_invoice_attachments','stored_name','content_hash','data/invoice_attachments/'),
                 ('supplier_monthly_invoices','attachment_stored_name','attachment_content_hash','data/supplier_invoice_attachments/')]
        for table, column, hash_column, prefix in specs:
            if table not in tables: continue
            size_column = 'size' if table == 'shared_file_versions' else None
            for row_id, raw_path, expected, size in db.execute(
                    f'SELECT id,"{column}",'+(f'"{hash_column}"' if hash_column else 'NULL')+
                    ','+(f'"{size_column}"' if size_column else 'NULL')+
                    f' FROM "{table}" WHERE "{column}" IS NOT NULL AND trim("{column}")<>\'\''):
                if table == 'shared_file_versions' and (not raw_path.startswith('private:shared_files/')
                        or type(size) is not int or size < 0):
                    raise ValueError('共享文件原件引用或冻结大小无效')
                rel = _source_relative((prefix or '') + raw_path)
                if hash_column and not c.sha(expected): raise ValueError('原件缺少有效冻结摘要')
                previous = found.get(rel)
                if previous and expected and previous['sha256'] and previous['sha256'] != expected:
                    raise ValueError('原件引用摘要冲突')
                if previous and size is not None and previous['bytes'] is not None and previous['bytes'] != size:
                    raise ValueError('原件引用大小冲突')
                found[rel] = {'sha256':expected or (previous or {}).get('sha256'),
                              'bytes':size if size is not None else (previous or {}).get('bytes')}
                if table == 'pdf_order_training_samples': pdf.append((row_id, raw_path, rel))
    if len(found) > c.MAX_FILES: raise ValueError('原件数量超过本任务上限')
    return found, pdf


def _copy(source, target, uid):
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    digest = hashlib.sha256(); size = 0
    with c.regular(source, uid=uid) as (fd, _):
        out = os.open(target, os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW, 0o600)
        with os.fdopen(out, 'wb') as stream:
            while True:
                chunk = os.read(fd, 1024**2)
                if not chunk: break
                digest.update(chunk); size += len(chunk); stream.write(chunk)
                if size > c.MAX_BYTES: raise ValueError('原件超出本任务大小限制')
            stream.flush(); os.fsync(stream.fileno())
    return {'sha256': digest.hexdigest(), 'bytes': size}


def _hash_file(path, uid):
    digest = hashlib.sha256(); size = 0
    with c.regular(path, uid=uid) as (fd, _):
        while True:
            block = os.read(fd, 1024**2)
            if not block: break
            digest.update(block); size += len(block)
            if size > c.MAX_BYTES: raise ValueError('导出原件在读取时超过上限')
    return {'sha256': digest.hexdigest(), 'bytes': size}


def capture(plan, context, source, verify_source):
    c.validate_plan(plan, check_home_acl=False)
    if source != plan['source']: raise ValueError('预览后的完整来源身份已变化')
    environment = c.read(c.ROOT/'shared/environment.json', uid=context['uid'], limit=boot.LIMIT)
    if c.digest(environment) != source['environment_sha256']:
        raise ValueError('捕获前运行配置身份变化')
    credentials = c.environment_credentials(environment)
    work = c.staging(plan['attempt_id'])
    work.mkdir(mode=0o700)  # Any failed/successful attempt is never overwritten.
    payload = work/'payload'; payload.mkdir(mode=0o700)
    shared = c.ROOT/'shared'; uid = context['uid']
    files = {}; started = time.time()
    map_path = shared/source['map_relative']
    files['layout.json'] = _copy(map_path, payload/'layout.json', uid)
    with c.regular(shared/'data/carton_erp.sqlite3', uid=uid, mutable=True):
        from desktop_assistant.data_recovery import sqlite_snapshot
        snapshot = sqlite_snapshot(shared/'data/carton_erp.sqlite3', work/'source.sqlite3')
    raw_sha = _hash_file(work/'source.sqlite3', uid)['sha256']
    if snapshot['revision'] != source['revision']: raise ValueError('捕获期间数据库迁移版本变化')
    refs, pdf = artifact_references(work/'source.sqlite3')
    for relative, expected in sorted(refs.items()):
        name = 'originals/' + relative
        files[name] = _copy(shared/relative, payload/name, uid)
        if expected['sha256'] and files[name]['sha256'] != expected['sha256']:
            raise ValueError('原件与冻结摘要不符')
        if expected['bytes'] is not None and files[name]['bytes'] != expected['bytes']:
            raise ValueError('原件与冻结大小不符')
        if sum(f['bytes'] for f in files.values()) > c.MAX_BYTES: raise ValueError('原件累计超过容量上限')
    # The sole path transformation enables existing run_task's PDF isolation.
    # Work on a second private SQLite file; retain original snapshot SHA.
    from desktop_assistant.data_recovery import sqlite_snapshot
    sqlite_snapshot(work/'source.sqlite3', work/'rebound.sqlite3')
    rebindings = []
    with closing(sqlite3.connect(work/'rebound.sqlite3')) as db:
        before_changes = db.total_changes
        for row_id, old, relative in pdf:
            target = str(c.output(plan['attempt_id'])/'originals'/relative)
            db.execute('UPDATE pdf_order_training_samples SET file_path=? WHERE id=? AND file_path=?',
                       (target, row_id, old))
            rebindings.append({'table':'pdf_order_training_samples','field':'file_path','id':row_id,
                               'before_sha256':c.digest(old.encode()),'after_sha256':c.digest(target.encode())})
        if db.total_changes-before_changes != len(pdf): raise ValueError('路径重绑触发了额外修改')
        db.commit()
    from desktop_assistant.uat_export_sanitize import sanitize
    sanitized = sanitize(work/'rebound.sqlite3', payload/'database.sqlite3')
    files['database.sqlite3'] = _hash_file(payload/'database.sqlite3', uid)
    if sum(f['bytes'] for f in files.values()) > c.MAX_BYTES: raise ValueError('本任务副本超过容量上限')
    if _hash_file(map_path, uid) != files['layout.json']: raise ValueError('捕获窗口内运行地图变化')
    for relative in refs:
        if _hash_file(shared/relative, uid) != files['originals/'+relative]:
            raise ValueError('捕获窗口内原件变化')
    if verify_source() != source: raise ValueError('捕获结束时来源身份变化')
    receipt = {'type':c.RECEIPT_TYPE,'task':c.TASK,'attempt_id':plan['attempt_id'],
        'plan_sha256':c.digest(c.canonical(plan)), 'source':source,
        'snapshot_sha256':raw_sha,'started_at':started,'completed_at':time.time(),
        'sanitization':sanitized,'path_rebindings':rebindings,'files':files,
        'pii_anonymized':False,'source_business_written':False,'service_stopped':False,
        'map_consistency':'unchanged-across-capture-window',
        'attachment_scope':'registered drawing/PDF/shared-file/invoice references only; no twin database'}
    receipt['environment_credential_check'] = {'policy': c.ENVIRONMENT_POLICY,
        'environment_sha256': source['environment_sha256'], 'checked': True}
    raw = c.canonical(receipt)
    if len(raw) > c.MAX_MANIFEST: raise ValueError('导出回执超过上限')
    from desktop_assistant.uat_export_sanitize import verify_credential_values_absent
    for name in files:
        verify_credential_values_absent(payload/name, credentials,
            database=name=='database.sqlite3', json_document=name=='layout.json')
    c.reject_credential_json(credentials, raw)
    with (payload/'receipt.json').open('xb') as stream:
        stream.write(raw); stream.flush(); os.fsync(stream.fileno())
    return {'receipt_sha256':c.digest(raw),'files':len(files),'bytes':sum(f['bytes'] for f in files.values())}


def run(request, context, public):
    c.validate_request(request)
    with manage.instance(context) as (folders, lock, recheck), manage.maintenance(lock):
        def inspect(): return inspect_source(context, public, folders, recheck)
        source = inspect()
        result = {'type':'tianming.uat-export-worker.v1','action':request['action'],
            'package_sha256':request['package_sha256'],'challenge':request['challenge'],
            'identity':boot._actual(), 'source':source}
        if request['action'] == 'export-uat':
            plan = request['plan']
            raw = c.read(c.plan_path(plan['attempt_id']), uid=0, limit=boot.LIMIT)
            if raw != c.canonical(plan): raise ValueError('服务进程未读取到批准的固定受保护计划')
            result['capture'] = capture(plan, context, source, inspect)
        recheck()
        return result
