"""Explicit same-schema code update with a full authenticated LOCAL backup.

This is not a NAS fallback, migration, factory activation or NAS receipt.
The old NAS updater and its requirements remain unchanged. All business bytes,
reader approvals and existing remote-backup attention are preserved.
"""
from pathlib import Path
import os
import re
import shutil
import uuid

from desktop_assistant import manager as m
from desktop_assistant.migration import files
from desktop_assistant.storage import (sha, read_json, write_json, decrypt_file,
    extract_verified, database_info, signed_release_manifest)


def stopped(manager):
    state = manager.state
    if (state.get('manual_stop') is not True or state.get('operation') not in
            (None, 'import_existing', 'update', 'rollback', 'migration_recovered', 'restore')
            or state.get('onboarding_pending') or state.get('backup_work_retained')
            or not re.fullmatch('[0-9a-f]{64}', state.get('current', ''))):
        raise ValueError('本地更新仅接受明确停止且没有未决数据维护的实例')
    # A failed REMOTE backup is retained as attention, never cleared or counted.
    for entry in manager.root.iterdir():
        if entry.name == 'state.json.pending' or re.fullmatch(r'state-[0-9a-f]{32}\.pending\.json', entry.name):
            raise ValueError('状态发布尚未完成，保留现场')
    database = manager.root/'shared/data/carton_erp.sqlite3'
    for suffix in ('-wal', '-shm', '-journal'):
        path = Path(str(database)+suffix)
        if path.exists() or path.is_symlink():
            raise ValueError('数据库日志尚未闭合，保留现场')
    if manager._process() is not None:
        raise ValueError('ERP 尚未正常停止')
    return state


def update(manager, package, password, state_sha256, *, approval_check=lambda: None):
    """Caller must hold the original maintenance lock for the entire call."""
    old = stopped(manager)
    state_path = manager.root/'state.json'
    if sha(state_path) != state_sha256:
        raise ValueError('本次批准状态已经变化')
    source = manager.root/'packages'/(old['current']+'.zip')
    identity = sha(package)
    if identity == old['current'] or sha(source) != old['current']:
        raise ValueError('当前原包或目标身份无效')
    current, current_schema = m.schema_contract_from_signed_release(source, manager.public_key)
    manifest, schema = m.schema_contract_from_signed_release(package, manager.public_key)
    database = manager.root/'shared/data/carton_erp.sqlite3'
    revision = database_info(database)['revision']
    if (current != manager.manifest(old['current']) or current_schema != schema
            or current['revision'] != revision or manifest['revision'] != revision):
        raise ValueError('本地程序切换不允许数据库结构或迁移合同变化')
    m.validate_database_schema(database, schema)
    before = files(manager.root/'shared')
    db_sha = sha(database)
    candidate = manager.stage_release(package, verify_existing=True)
    if candidate != {'id': identity, **manifest}:
        raise ValueError('目标缓存不属于原签名清单')

    def check():
        approval_check()
        if (stopped(manager) != old or sha(state_path) != state_sha256
                or sha(database) != db_sha or files(manager.root/'shared') != before
                or sha(source) != old['current'] or sha(package) != identity
                or manager.manifest(identity) != manifest):
            raise ValueError('状态、业务数据或程序身份变化，未切换')
        manager._verify_cached_release(manager.root/'releases'/identity, manifest)
        manager._verify_cached_release(manager.root/'releases'/old['current'], current)
        manager._approve_execution(old['current'], identity)
        for release in (old['current'], identity):
            manager._check_dispatch_reader(release, require_active=manager._dispatch_reader(release))
            manager._check_provenance_reader(release)
            if not manager.compatible(release, revision):
                raise ValueError('不能丢失现有数据的签名读取合同')
        if manager._order_inventory_reader(identity) and not old.get(m.inventory_contract.STATE_KEY):
            raise ValueError('本地更新不执行首次读取保护激活')

    check()
    inputs = manager._backup_inputs()
    if shutil.disk_usage(manager.root).free < inputs['required']:
        raise ValueError('本地完整备份空间不足')
    job = manager.root/'staging'/('local-code-update-'+uuid.uuid4().hex)
    job.mkdir(mode=0o700)
    payload = manager._build_backup_payload(password, job, inputs)
    final = manager.root/'backups'/payload['encrypted'].name
    with payload['encrypted'].open('rb') as src, final.open('xb') as dst:
        shutil.copyfileobj(src, dst, 1024*1024); dst.flush(); os.fsync(dst.fileno())
    if sha(final) != payload['sha256'] or final.stat().st_size != payload['size']:
        raise ValueError('本地独立副本不一致')
    readback = job/'independent-readback.zip'
    if decrypt_file(final, readback, password) != payload['sha256'] or sha(readback) != payload['raw_sha256']:
        raise ValueError('本地备份认证回读失败')
    restored = job/'independent-recovery'
    recovered = extract_verified(readback, restored)
    if recovered != payload['manifest'] or database_info(restored/'shared/data/carton_erp.sqlite3') != inputs['before']:
        raise ValueError('完整恢复结构或数据库事实不一致')
    if signed_release_manifest(restored/'release.zip', manager.public_key) != current:
        raise ValueError('备份内原程序签名不一致')
    check()
    proof = dict(type='tianming.local-code-update.v1', storage='local',
        independent_device=False, nas_verified=False, backup_sha256=payload['sha256'],
        backup_bytes=payload['size'], current=identity, previous=old['current'],
        database_sha256=db_sha, shared_files=len(before), manual_stop=True,
        restored_verified=True, factory_activated=False)
    write_json(job/'verified.json', proof)
    new = dict(old, current=identity, previous=old['current'], update_backup=str(final),
               operation='update', manual_stop=True, local_code_update=proof)
    write_json(state_path, new)
    if manager.state != new:
        raise ValueError('本地切换状态回读未确认，保留现场')
    # Only this successful job's derived plaintext/copy files are disposable.
    # The authenticated final backup, small receipt and all OLD scenes stay.
    try:
        if job.parent != manager.root/'staging' or job.is_symlink():
            raise OSError('successful job changed')
        for path in job.iterdir():
            if path.name == 'verified.json':
                continue
            if path.is_symlink():
                raise OSError('successful job contains link')
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
    except OSError:
        pass  # Cleanup is never a rollback or an update failure.
    return proof


def main(*, script, executable):
    """Fixed dedicated service entry; no arbitrary root, path or credentials."""
    import sys
    from app.core import mac_service_keychain as keychain
    from desktop_assistant import mac_service_manage as manage
    from desktop_assistant import mac_system_service as system, mac_published_runtime as published
    from desktop_assistant import mac_keychain_install as boot
    from desktop_assistant.credential_store import unprotect_backup
    request = boot.decode(sys.stdin.buffer.read(boot.LIMIT+1))
    keys = {'package_sha256','context_sha256','publisher_sha256','registry_sha256',
            'state_sha256','challenge','action'}
    if (set(request) != keys or request['action'] != 'local-code-update'
            or any(not system._sha(request[k]) for k in keys if k.endswith('_sha256'))
            or not re.fullmatch('[0-9a-f]{32}', request['challenge'])
            or sys.platform != 'darwin' or not sys.flags.isolated or not sys.dont_write_bytecode
            or not keychain.selected()):
        raise ValueError('本地更新请求或解释器身份无效')
    context = keychain.load_context()
    if boot._actual() != boot._expected(context['uid'], context['gid']):
        raise ValueError('专用服务身份无效')
    meta = system.metadata(request['package_sha256'])
    if context != meta['context'] or any(request[k] != meta[k] for k in
            ('context_sha256','publisher_sha256','registry_sha256')):
        raise ValueError('已批准发布身份变化')
    tree, manifest = published.resolve(manage.ROOT, request['package_sha256'], meta['public'], kind='migration')
    if (type(manifest.get('runtime_capabilities', {}).get('local_code_update_v1')) is not int
            or manifest['runtime_capabilities']['local_code_update_v1'] != 1
            or Path(script).resolve() != tree/'desktop_assistant/service_local_code_update_entry.py'
            or Path(__file__).resolve() != tree/'desktop_assistant/local_code_update.py'
            or Path(executable).resolve() != tree/'runtime/bin/python3.12'):
        raise ValueError('本地更新不属于已批准签名程序')
    with manage.instance(context) as (folders, lock, recheck), manage.maintenance(lock):
        settings_raw, saved = manage.settings(folders['root'], context)
        system.verify_keychain(context)
        password = unprotect_backup(saved['protected_password'])
        def approval():
            recheck()
            if manage.settings(folders['root'], context)[0] != settings_raw:
                raise ValueError('备份配置变化，保留本次现场')
            latest = system.metadata(request['package_sha256'])
            if latest['context'] != context or any(request[k] != latest[k] for k in
                    ('context_sha256','publisher_sha256','registry_sha256')):
                raise ValueError('发布批准变化，保留现场')
        try:
            result = update(m.Manager(manage.ROOT, meta['public']), tree.parent/'package.zip',
                            password, request['state_sha256'], approval_check=approval)
        finally:
            del password
        recheck()
        if manage.settings(folders['root'], context)[0] != settings_raw:
            raise ValueError('备份配置变化，保留本次现场')
    result['challenge'] = request['challenge']
    sys.stdout.buffer.write(boot.canonical(result)+b'\n'); sys.stdout.buffer.flush()
