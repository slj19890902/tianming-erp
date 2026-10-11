"""Finish only the promoted, stopped checkpoint of an interrupted Mac restore.

Authenticate the original inputs again, compare every portable fact and secret,
then exclusively create the two missing records. Never restore, rewrap, start,
activate, repair or remove anything. Failed attempts and audit copies remain.
"""
from contextlib import ExitStack, closing
from copy import deepcopy
from datetime import datetime, timezone
import getpass
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import warnings

from desktop_assistant import mac_service_restore as restore, mac_service_manage as manage
from desktop_assistant import mac_published_runtime as published, mac_release_publish as publication
from desktop_assistant import factory_target_audit as audit, factory_cutover as facts
from desktop_assistant import credential_transfer as credentials, credential_restore, credential_store
from desktop_assistant import restore_provenance as history, delivery_dispatch_contract as dispatch
from desktop_assistant import order_inventory_contract as inventory, factory_activation_contract as activation
from desktop_assistant.reader_contract import target_readers
from desktop_assistant.release_request import inspect_package, public_identity
from desktop_assistant.schema_contract import schema_contract_from_signed_release, validate_database_schema
from desktop_assistant.storage import sha, database_info, _decrypt_stream, extract_verified, read_json

ENTRY = 'desktop_assistant/service_restore_completion_entry.py'
MODULE = 'desktop_assistant/mac_restore_completion.py'
CAPABILITY = 'service_restore_completion_v1'
RESUME_CAPABILITY = 'service_restore_resume_v1'
REAUTH_CAPABILITY = 'service_restore_reauthentication_v1'
ERROR = '中断恢复现场或认证来源不一致；保留现场并保持停止'


def require_capability(manifest):
    publication.require_service_restore(manifest)
    value = (manifest.get('runtime_capabilities') or {}).get(CAPABILITY)
    if type(value) is not int or value != 1 or not {ENTRY, MODULE} <= manifest['files'].keys():
        raise ValueError('已批准签名程序缺少有限恢复完成协议')


def _foreground_terminal():
    # macOS may return EINTR from getpass's tcsetattr after sudo foregrounds a
    # stopped background command. Refuse before an attempt exists; never fall
    # back to echoed input or take terminal ownership from the caller.
    if not restore._terminal(): raise ValueError('有限完成要求三路本机终端及隐藏口令输入')
    try:
        foreground = os.tcgetpgrp(0)
    except OSError:
        raise ValueError('有限完成要求已有前台控制终端') from None
    if foreground <= 0 or foreground != os.getpgrp():
        raise ValueError('有限完成要求已有前台控制终端')


def _attempt_record(args, execution_package):
    return {'type':'tianming.restore-completion-attempt.v1',
        'input_attempt':args.input_attempt, 'execution_package':execution_package,
        'restore_package':args.restore_package, 'backup_sha256':args.backup_sha256,
        'context_sha256':args.context_sha256}


def _resume_source(item, expected_sha, args, public):
    raw = restore._read_complete(item, 4096)
    if manage.boot.digest(raw) != expected_sha: raise ValueError(ERROR)
    record = json.loads(raw)
    if (not isinstance(record, dict) or not restore._hash(record.get('execution_package'))
            or raw != manage.boot.canonical(_attempt_record(args, record['execution_package']))):
        raise ValueError(ERROR)
    # The old approved completion program remains an independently verified
    # part of the checkpoint, even when this run uses a newer signed program.
    _, manifest = published.resolve(restore.ROOT, record['execution_package'], public, kind='migration')
    require_capability(manifest)
    item.check()
    return raw


def _resume_record(args, execution_package, original_sha):
    value = _attempt_record(args, execution_package)
    value.update(type='tianming.restore-completion-resume.v1',
        original_attempt_sha256=original_sha, credentials_sha256=args.credentials_sha256,
        publisher_sha256=args.publisher_sha256, staging_identity_sha256=args.staging_identity_sha256)
    return value


def _reauth_source(item, expected_sha, args, public, original_sha):
    raw = restore._read_complete(item, 4096)
    if manage.boot.digest(raw) != expected_sha: raise ValueError(ERROR)
    record = json.loads(raw)
    if (not isinstance(record, dict) or not restore._hash(record.get('execution_package'))
            or raw != manage.boot.canonical(_resume_record(args, record['execution_package'], original_sha))):
        raise ValueError(ERROR)
    _, manifest = published.resolve(restore.ROOT, record['execution_package'], public, kind='migration')
    require_capability(manifest)
    capability = (manifest.get('runtime_capabilities') or {}).get(RESUME_CAPABILITY)
    if type(capability) is not int or capability != 1: raise ValueError(ERROR)
    item.check()
    return raw


def _authenticate_before_attempt(raw, hidden, stable, backup_sha):
    """Only known, hash-checked external ciphertext; no filesystem writes here."""
    for attempt in range(3):
        stable()
        password = hidden('凭据移交口令（隐藏输入）：')
        _foreground_terminal()
        stable()
        try:
            record = credentials.unseal(raw, password)
        except ValueError:
            stable()
            if attempt == 2:
                raise ValueError('移交凭据未通过认证；未创建新的恢复尝试') from None
            print('移交口令或凭据未通过认证，请重新输入（剩余%d次）。' % (2-attempt), flush=True)
            continue
        if record['bound_backup_sha256'] != backup_sha: raise ValueError(ERROR)
        stable()
        return password, record


def _source(payload, metadata, record, target, public, backup_sha):
    """The authenticated, unmodified source determines all completion state."""
    database = payload/'shared/data/carton_erp.sqlite3'
    audit._no_journal(payload/'shared')
    if (metadata.get('type') != 'tianming.recovery.v1'
            or sha(payload/'release.zip') != metadata.get('release')
            or database_info(database) != metadata.get('database')):
        raise ValueError(ERROR)
    source, contract = schema_contract_from_signed_release(payload/'release.zip', public)
    if (source.get('runtime_platform', 'windows') != 'windows'
            or source.get('version') != metadata.get('version') or record['source_platform'] != 'win32'):
        raise ValueError('有限完成仅接受原Windows来源')
    provenance, originals = history.for_recovery(payload, metadata, source, public)
    authority = metadata.get('schema_authority')
    if source['revision'] != metadata['database']['revision']:
        if not restore._hash(authority) or sha(payload/'compatibility.zip') != authority: raise ValueError(ERROR)
        authorized, contract = schema_contract_from_signed_release(payload/'compatibility.zip', public)
        policy = authorized.get('migration') or {}
        if (authorized['revision'] != metadata['database']['revision']
                or policy.get('policy') != 'preserve_existing_facts_v1'
                or policy.get('rollback_package_sha256') != metadata['release']): raise ValueError(ERROR)
    elif authority is not None:
        raise ValueError('有限完成不接受多余结构授权')
    members = {'release.zip', *originals}
    if authority: members.add('compatibility.zip')
    if metadata.get('credential_transfer') is not None: members.add('credentials.tmencrypted')
    if {p for p in metadata['files'] if not p.startswith('shared/')} != members: raise ValueError(ERROR)
    validate_database_schema(database, contract)
    _, native_contract = schema_contract_from_signed_release(target['package'], public)
    if target['manifest']['revision'] != metadata['database']['revision']: raise ValueError(ERROR)
    validate_database_schema(database, native_contract)
    inv = inventory.for_recovery(metadata, source, backup_sha)
    act = dispatch.validate_recovery(payload/'shared', metadata, source)
    target_readers(database, source, target['manifest'], inv, act, factory=False)
    if not dispatch.signed_capability(target['manifest']): raise ValueError(ERROR)
    if inventory.signed_capability(target['manifest']) and inv is None: raise ValueError(ERROR)
    dispatch.inspect_activation(payload/'shared', {dispatch.STATE_KEY:dispatch.state_index(act)} if act else {},
                                require_active=True)
    # Check the old DPAPI identity/version against the authenticated original DB.
    with closing(sqlite3.connect(database.as_uri()+'?mode=ro&immutable=1', uri=True)) as db:
        credential_restore._match(credential_restore._mailbox(db), record)
    if not provenance:
        provenance = {history.RELEASE:metadata['release'], history.AUTHORITY:authority}
    return source, native_contract, provenance, inv, act


def _state(metadata, target, context_sha, backup_sha, record, provenance, inv, act):
    report = {'status':'restored', 'restored':sorted(record['entries']), 'mailbox_version_preserved':True}
    return {'current':target['id'], 'previous':None, 'restored_at':datetime.now(timezone.utc).isoformat(),
        'source_time':metadata['created'], 'restored_backup_sha256':backup_sha, 'schema_authority':None,
        **provenance, 'credential_restore':report, 'manual_stop':True, 'operation':'restore',
        'native_restore_binding':{'type':'tianming.mac-native-restore.v1', 'package_sha256':target['id'],
            'backup_sha256':backup_sha, 'context_sha256':context_sha,
            'database_revision':metadata['database']['revision'], 'schema_migrated':False, 'factory_cutover':False},
        **({inventory.STATE_KEY:inv} if inv is not None else {}),
        **({dispatch.STATE_KEY:dispatch.state_index(act)} if act else {})}


def _same_credentials(record, root, password):
    # The external envelope's binding was checked before this normalization for
    # the shared comparison. Unlike factory activation, no NAS edit is allowed.
    normalized = deepcopy(record)
    normalized['bound_backup_sha256'] = None
    fresh = credentials.collect(root, immutable_database=True)
    original_preferences = json.loads(record['files']['preferences.json'])
    current_preferences = json.loads(fresh['files']['preferences.json'])
    original_nas = original_preferences.get('nas')
    if ('nas' in original_preferences) != ('nas' in current_preferences): raise ValueError(ERROR)
    audit._same_credentials(normalized, fresh, password, sorted(record['entries']), original_nas)


def _decrypt_retained(backup, destination, password):
    # The new, private audit file is deliberately retained even on a bad tag.
    # Only a successful complete GCM authentication permits extraction below.
    with os.fdopen(os.dup(backup.fd), 'rb') as source, destination.open('xb') as output:
        try:
            return _decrypt_stream(source, password, output)
        finally:
            output.flush(); os.fsync(output.fileno())


def _phase(name):
    print(json.dumps({'restore_completion_phase':name}), flush=True)


def run(args, *, script, executable, prompt=getpass.getpass):
    _foreground_terminal()
    resume_sha = getattr(args, 'resume_before_decryption_sha256', None)
    reauth_sha = getattr(args, 'reauthenticate_before_decryption_sha256', None)
    if (not isinstance(args.input_attempt, str) or not re.fullmatch('[0-9a-f]{32}', args.input_attempt)
            or any(not restore._hash(getattr(args, name)) for name in ('execution_package', 'restore_package',
                'backup_sha256', 'publisher_sha256', 'context_sha256', 'staging_identity_sha256'))
            or (args.credentials_sha256 is not None and not restore._hash(args.credentials_sha256))
            or (resume_sha is not None and not restore._hash(resume_sha))
            or (reauth_sha is not None and (not restore._hash(reauth_sha)
                or resume_sha is None or args.credentials_sha256 is None))):
        raise ValueError('有限完成参数不完整')
    _phase('readonly_signed_identity_preflight')
    context = restore.runtime_identity()
    if manage.boot._actual() != manage.boot._expected(context['uid'], context['gid']): raise ValueError(ERROR)
    public = published._read(published.PUBLIC)
    if public_identity(public)[1] != args.publisher_sha256: raise ValueError(ERROR)
    code, code_manifest = published.resolve(restore.ROOT, args.execution_package, public, kind='migration')
    require_capability(code_manifest)
    if resume_sha is not None:
        value = (code_manifest.get('runtime_capabilities') or {}).get(RESUME_CAPABILITY)
        if type(value) is not int or value != 1: raise ValueError('签名程序缺少解密前续接协议')
    if reauth_sha is not None:
        value = (code_manifest.get('runtime_capabilities') or {}).get(REAUTH_CAPABILITY)
        if type(value) is not int or value != 1: raise ValueError('签名程序缺少移交凭据认证续接协议')
    if (Path(script).resolve() != code/ENTRY or Path(__file__).resolve() != code/MODULE
            or Path(executable).resolve() != code/'runtime/bin/python3.12'
            or Path(sys.executable).resolve() != code/'runtime/bin/python3.12'):
        raise ValueError('有限完成未运行批准的完整签名入口')
    target_tree, target_manifest = published.resolve(restore.ROOT, args.restore_package, public, kind='migration')
    publication.require_service_restore(target_manifest)
    target = {'id':args.restore_package, 'manifest':target_manifest, 'package':target_tree.parent/'package.zip'}
    publication._no_service()
    from app.core.home_rehearsal import install_network_guard
    os.environ['ERP_HOME_REHEARSAL'] = '1'; install_network_guard()
    old_umask = os.umask(0o077)
    try:
        with ExitStack() as stack:
            base = stack.enter_context(restore.Lease(restore.BASE, uid=0, gid=None, mode=0o755, directory=True))
            held = []
            def pin(path, parent, *, uid=context['uid'], gid=context['gid'], mode=0o600,
                    directory=False, mutable=False):
                item = stack.enter_context(restore.Lease(path, uid=uid, gid=gid, mode=mode,
                    directory=directory, parent=parent, mutable=mutable))
                held.append(item); return item
            context_item = pin(restore.keychain.CONTEXT, base, uid=0, gid=None, mode=0o644)
            if manage.boot.digest(restore._read_complete(context_item, 65536)) != args.context_sha256:
                raise ValueError(ERROR)
            pin(published.PUBLIC, base, uid=0, gid=None, mode=0o644)
            pin(published.REGISTRY, base, uid=0, gid=None, mode=0o644)
            home = pin(base.path/'home', base, mode=0o700, directory=True, mutable=True)
            # No creation of a missing restore lock is allowed at this checkpoint.
            pin(home.path/'service-restore.lock', home)
            stack.enter_context(restore.restore_lock(home, context))
            folders, lock, parents_check = stack.enter_context(manage.instance(context))
            stack.enter_context(manage.maintenance(lock))
            root = folders['root']
            receipt_name = 'service-restore-'+args.input_attempt+'.json'
            attempt_name = 'restore-completion-'+args.input_attempt+'.json'
            resume_name = 'restore-completion-resume-'+args.input_attempt+'.json'
            reauth_name = 'restore-completion-reauth-'+args.input_attempt+'.json'
            for path in (home.path/receipt_name, home.path/reauth_name,
                         *(() if reauth_sha else (home.path/resume_name,)), root.path/'state.json',
                         root.path/'state.json.pending', *(() if resume_sha else (home.path/attempt_name,))):
                if os.path.lexists(path): raise ValueError('已有完成状态或尝试；不能覆盖重试')
            original_attempt = None
            if resume_sha is not None:
                original_attempt = pin(home.path/attempt_name, home)
                original_attempt_raw = _resume_source(original_attempt, resume_sha, args, public)
            prior_resume = None
            if reauth_sha is not None:
                prior_resume = pin(home.path/resume_name, home)
                prior_resume_raw = _reauth_source(prior_resume, reauth_sha, args, public, resume_sha)
            inputs = pin(base.path/'restore-inputs', base, uid=0, mode=0o750, directory=True)
            attempt = pin(inputs.path/args.input_attempt, inputs, uid=0, mode=0o750, directory=True)
            expected_inputs = {'complete.tmbackup'} | ({'credentials.tmencrypted'} if args.credentials_sha256 else set())
            if set(os.listdir(attempt.fd)) != expected_inputs: raise ValueError(ERROR)
            backup = pin(attempt.path/'complete.tmbackup', attempt, uid=0, mode=0o440)
            if restore._digest(backup.fd) != args.backup_sha256: raise ValueError(ERROR)
            raw_transfer = None
            if args.credentials_sha256:
                transfer = pin(attempt.path/'credentials.tmencrypted', attempt, uid=0, mode=0o440)
                raw_transfer = restore._read_complete(transfer, credentials.LIMIT)
                if manage.boot.digest(raw_transfer) != args.credentials_sha256: raise ValueError(ERROR)
            jobs = os.listdir(folders['staging'].fd)
            if (len(jobs) != 1 or not re.fullmatch('[0-9a-f]{32}', jobs[0])
                    or hashlib.sha256(jobs[0].encode()).hexdigest() != args.staging_identity_sha256):
                raise ValueError('不是批准的唯一原恢复暂存槽')
            prior = pin(folders['staging'].path/jobs[0], folders['staging'], mode=0o700, directory=True)
            if set(os.listdir(prior.fd)) != {'payload','recovery.zip'}: raise ValueError(ERROR)
            prior_raw = pin(prior.path/'recovery.zip', prior)
            prior_payload = pin(prior.path/'payload', prior, mode=0o700, directory=True)
            if os.path.lexists(prior_payload.path/'shared'): raise ValueError('恢复尚未提升到固定实例')
            before_shared = audit._private_tree(folders['shared'], context)
            before_prior = audit._private_tree(prior, context)
            configs = {name for name,_ in credentials.CONFIGS.values() if os.path.lexists(root.path/name)}
            for name in configs: pin(root.path/name, folders['control'] if '/' in name else root)
            expected_root = {'releases','shared','control','backups','packages','staging','preferences.json'}
            expected_control = {'operation.lock'} | {Path(name).name for name in configs if name.startswith('control/')}
            if (set(os.listdir(root.fd)) != expected_root or set(os.listdir(folders['control'].fd)) != expected_control
                    or os.listdir(folders['backups'].fd) or 'preferences.json' not in configs): raise ValueError(ERROR)
            manager = audit._TargetReader(root.path, public)
            published_state = False
            expected_staging = set(jobs)
            def stable():
                parents_check()
                for item in held: item.check()
                if (original_attempt is not None
                        and restore._read_complete(original_attempt, 4096) != original_attempt_raw):
                    raise ValueError(ERROR)
                if prior_resume is not None and restore._read_complete(prior_resume, 4096) != prior_resume_raw:
                    raise ValueError(ERROR)
                publication._no_service()
                if os.path.lexists(base.path/activation.NAME): raise ValueError('已有工厂激活记录，不能续接家庭恢复')
                if (manager._process() or set(os.listdir(root.fd)) != expected_root | ({'state.json'} if published_state else set())
                        or set(os.listdir(folders['control'].fd)) != expected_control
                        or os.listdir(folders['backups'].fd)
                        or set(os.listdir(folders['staging'].fd)) != expected_staging
                        or audit._private_tree(folders['shared'], context) != before_shared
                        or audit._private_tree(prior, context) != before_prior): raise ValueError(ERROR)
                audit._no_journal(folders['shared'].path)
                if published_state:
                    raw_state, _ = manage.record(root, 'state.json', context)
                    if raw_state != manage.boot.canonical(state): raise ValueError(ERROR)
            stable()
            from desktop_assistant.mac_system_service import verify_keychain
            def hidden(message):
                _foreground_terminal()
                with warnings.catch_warnings():
                    warnings.simplefilter('error', getpass.GetPassWarning)
                    return prompt(message)
            transfer_password = None
            if reauth_sha is not None:
                verify_keychain(context)
                _phase('authenticate_transfer_before_new_attempt')
                transfer_password, record = _authenticate_before_attempt(raw_transfer, hidden, stable, args.backup_sha256)
                password = record['entries']['backup']['value']
            # Exclusive attempt survives authentication, validation or IO failure.
            _foreground_terminal()
            attempt_record = _attempt_record(args, args.execution_package)
            if resume_sha is not None:
                attempt_record = _resume_record(args, args.execution_package, resume_sha)
            if reauth_sha is not None:
                attempt_record.update(type='tianming.restore-completion-reauth.v1',previous_resume_sha256=reauth_sha)
            new_name = reauth_name if reauth_sha else resume_name if resume_sha else attempt_name
            _phase('create_exclusive_reauthentication_attempt' if reauth_sha else
                   'create_exclusive_resume_attempt' if resume_sha else 'create_exclusive_completion_attempt')
            restore._receipt(home, new_name, manage.boot.canonical(attempt_record), context, stable)
            pin(home.path/new_name, home)
            if reauth_sha is None: verify_keychain(context)
            _phase('authenticate_original_credentials_and_backup')
            if reauth_sha is None:
                if raw_transfer:
                    transfer_password = hidden('凭据移交口令（隐藏输入）：')
                    record = credentials.unseal(raw_transfer, transfer_password)
                    if record['bound_backup_sha256'] != args.backup_sha256: raise ValueError(ERROR)
                    password = record['entries']['backup']['value']
                else:
                    password = hidden('完整备份恢复口令（隐藏输入）：')
            stable()
            name = 'completion-'+args.input_attempt
            os.mkdir(name, mode=0o700, dir_fd=folders['staging'].fd)
            expected_staging.add(name)
            job = pin(folders['staging'].path/name, folders['staging'], mode=0o700, directory=True, mutable=True)
            raw = job.path/'recovery.zip'
            if _decrypt_retained(backup, raw, password) != args.backup_sha256: raise ValueError(ERROR)
            if sha(raw) != restore._digest(prior_raw.fd): raise ValueError('原暂存ZIP与重新认证备份不一致')
            payload = job.path/'payload'; metadata = extract_verified(raw, payload)
            record = credential_restore.load_for_recovery(metadata, payload, backup.path, password,
                raw_transfer, transfer_password, authenticated_backup_sha256=args.backup_sha256)
            if record is None or 'backup' not in record['entries']: raise ValueError(ERROR)
            if not hmac.compare_digest(record['entries']['backup']['value'].encode(), password.encode()):
                raise ValueError('原备份凭据与实际认证口令不一致')
            _phase('compare_complete_original_facts_attachments_and_credentials')
            source, contract, provenance, inv, act = _source(payload, metadata, record, target, public, args.backup_sha256)
            if manage.boot.canonical(read_json(prior_payload.path/'manifest.json')) != manage.boot.canonical(metadata):
                raise ValueError('原暂存清单不属于重新认证备份')
            expected_prior = {p for p in metadata['files'] if '/' not in p} | {'manifest.json','credential-config'}
            if set(os.listdir(prior_payload.fd)) != expected_prior: raise ValueError(ERROR)
            for name in expected_prior - {'manifest.json','credential-config'}:
                if sha(prior_payload.path/name) != sha(payload/name): raise ValueError(ERROR)
            config_root = prior_payload.path/'credential-config'
            config_files = {p.relative_to(config_root).as_posix() for p in config_root.rglob('*') if p.is_file()}
            if config_files != configs or config_files != set(record['files']): raise ValueError(ERROR)
            for name in configs:
                if (config_root/name).read_bytes() != (root.path/name).read_bytes(): raise ValueError(ERROR)
            state = _state(metadata, target, args.context_sha256, args.backup_sha256, record, provenance, inv, act)
            source_snapshot = facts.capture_snapshot(payload/'shared', recorded_root=metadata['source_shared'])
            snapshot = facts.capture_snapshot(root.path/'shared')
            facts.require_matching_snapshots(source_snapshot, snapshot)
            if (payload/'shared/environment.json').read_bytes() != (root.path/'shared/environment.json').read_bytes():
                raise ValueError('恢复尚未激活，环境必须保持原字节')
            validate_database_schema(root.path/'shared/data/carton_erp.sqlite3', contract)
            target_readers(root.path/'shared/data/carton_erp.sqlite3', source, target_manifest, inv, act, factory=False)
            if dispatch.inspect_activation(root.path/'shared', state, require_active=True) != act: raise ValueError(ERROR)
            package_ids = {args.restore_package, metadata['release'], metadata.get('schema_authority'), *provenance.values()} - {None}
            if (set(os.listdir(folders['packages'].fd)) != {p+'.zip' for p in package_ids}
                    or set(os.listdir(folders['releases'].fd)) != package_ids): raise ValueError(ERROR)
            for identity in package_ids: pin(folders['packages'].path/(identity+'.zip'), folders['packages'])
            def verify():
                stable()
                if restore._digest(backup.fd) != args.backup_sha256: raise ValueError(ERROR)
                if facts.capture_snapshot(root.path/'shared') != snapshot: raise ValueError(ERROR)
                for identity in package_ids:
                    package = folders['packages'].path/(identity+'.zip')
                    if sha(package) != identity: raise ValueError(ERROR)
                    _, manifest = inspect_package(package, public)
                    manager._verify_cached_release(folders['releases'].path/identity, manifest)
                for kind in ('server','migration'): manager._published_release(args.restore_package, kind=kind)
                history.verify(provenance, {history.MEMBERS[k]:folders['packages'].path/(v+'.zip')
                                           for k,v in provenance.items() if v is not None}, public)
                _same_credentials(record, root.path, password)
                stable()
            verify()
            result = {'version':target_manifest['version'], 'data_time':metadata['created'], 'started':False,
                      'credentials':state['credential_restore']}
            receipt = {'type':'tianming.mac-service-restore.v1', 'backup_sha256':args.backup_sha256,
                'package_sha256':args.restore_package, 'publisher_sha256':args.publisher_sha256,
                'credentials_sha256':args.credentials_sha256, 'started':False, 'factory_cutover':False,
                'manual_stop':True, 'native_binding':state['native_restore_binding'], 'result':result}
            # O_EXCL, fsync and byte readback; no overwrite/rename of old state.
            _phase('publish_stopped_state_and_restore_receipt')
            published_state = True
            restore._receipt(root, 'state.json', manage.boot.canonical(state), context, verify)
            restore._receipt(home, receipt_name, manage.boot.canonical(receipt), context, verify)
            verify()
            if manage.record(home, receipt_name, context)[0] != manage.boot.canonical(receipt): raise ValueError(ERROR)
            return receipt
    finally:
        os.umask(old_umask)


def main(*, script, executable):
    from desktop_assistant.restore_cli import SafeParser
    parser = SafeParser(description='完成已落位的中断恢复；保持停止及家庭隔离', allow_abbrev=False)
    for name in ('input-attempt','execution-package','restore-package','backup-sha256','publisher-sha256',
                 'context-sha256','staging-identity-sha256'):
        parser.add_argument('--'+name, required=True)
    parser.add_argument('--credentials-sha256')
    parser.add_argument('--resume-before-decryption-sha256')
    parser.add_argument('--reauthenticate-before-decryption-sha256')
    args = parser.parse_args()
    try:
        print(json.dumps(run(args, script=script, executable=executable), ensure_ascii=False))
    except (Exception, KeyboardInterrupt):
        raise SystemExit('有限恢复完成未确认；保留原数据和本次尝试，不覆盖重试、不输出秘密。ERP未由此入口启动。') from None
