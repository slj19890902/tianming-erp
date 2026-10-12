"""Authenticate a final restored target before any factory activation.

This context holds the existing maintenance lock and local input identities. It
only reads the stopped instance; plaintext is extracted into a new private job
which is retained on success or failure. No activation, credential rewrapping,
process control, remote storage access or business IO is performed here.
"""
from contextlib import contextmanager, ExitStack
from datetime import datetime, timezone
import hmac
import os
from pathlib import Path
import stat
import sys
import uuid

from desktop_assistant import factory_cutover as protocol, factory_network as network
from desktop_assistant import mac_service_restore as restore, mac_service_manage as manage
from desktop_assistant import mac_published_runtime as published, mac_backup_storage as remote
from desktop_assistant import mac_smb_stream_target as stream
from desktop_assistant import credential_transfer as credentials, credential_store
from desktop_assistant import delivery_dispatch_contract as dispatch, order_inventory_contract as inventory
from desktop_assistant import restore_provenance as history
from desktop_assistant.mac_service_restore import Lease, _read_complete
from desktop_assistant.manager import Manager
from desktop_assistant.reader_contract import target_readers, capabilities
from desktop_assistant.release_request import inspect_package, public_identity
from desktop_assistant.schema_contract import schema_contract_from_signed_release, validate_database_schema
from desktop_assistant.storage import sha, database_info, decrypt_file, extract_verified

MODULE = 'desktop_assistant/factory_target_audit.py'
CAPABILITY = 'factory_audit_v1'
ERROR = '最终恢复目标与认证来源不一致，保持停止'


class _TargetReader(Manager):
    def __init__(self, root, public):
        # Manager.__init__ creates directories. An audit must never repair one.
        self.root, self.public_key = root, public


def _current():
    return datetime.now(timezone.utc)


def _archive(destination, context):
    """Pin the selected local approval; never probe NAS or read its credential."""
    if stream.selected(destination):
        binding = stream.prepare(destination)
        def recheck():
            binding.check()
            if (binding.context != context
                    or str(stream.destination(binding.control)) != destination):
                raise ValueError(ERROR)
    else:
        contract, contract_sha, contract_context = remote._contract()
        if context != contract_context or destination != contract['destination']:
            raise ValueError(ERROR)
        def recheck():
            if remote._contract() != (contract, contract_sha, contract_context):
                raise ValueError(ERROR)
    recheck()
    return recheck


def _no_journal(shared):
    database = shared/'data/carton_erp.sqlite3'
    if any(os.path.lexists(str(database)+suffix) for suffix in ('-wal', '-shm', '-journal')):
        raise ValueError('最终目标必须是无任何SQLite日志边车的静态恢复库')


def _private_tree(parent, context):
    """Bound FD use; never open thousands of attachment descriptors at once.

    Cooperating writers are excluded by the held maintenance lock. Repeated
    metadata/content checks detect other changes; this is not a Windows-style
    mandatory OS write lease or protection against a malicious same-UID writer.
    """
    result = {}
    def walk(folder):
        folder.check()
        for name in sorted(os.listdir(folder.fd)):
            info = os.stat(name, dir_fd=folder.fd, follow_symlinks=False)
            directory = stat.S_ISDIR(info.st_mode)
            with Lease(folder.path/name, uid=context['uid'], gid=context['gid'],
                       mode=0o700 if directory else 0o600, directory=directory, parent=folder) as item:
                result[str(item.path.relative_to(parent.path))] = restore._fingerprint(item.check())
                if directory: walk(item)
    walk(parent)
    return result


def _same_credentials(embedded, fresh, password, purposes, destination):
    """Compare plaintext only in memory, with narrowly defined config changes."""
    credentials._validate(embedded); credentials._validate(fresh)
    if (embedded['source_platform'] != 'win32' or fresh['source_platform'] != 'darwin'
            or embedded['bound_backup_sha256'] is not None or fresh['bound_backup_sha256'] is not None
            or sorted(embedded['entries']) != purposes or sorted(fresh['entries']) != purposes
            or set(embedded['files']) != set(fresh['files']) or 'backup' not in purposes
            or not hmac.compare_digest(embedded['entries']['backup']['value'].encode(), password.encode())):
        raise ValueError(ERROR)
    for purpose in purposes:
        source, target = embedded['entries'][purpose], fresh['entries'][purpose]
        if (not hmac.compare_digest(source['value'].encode(), target['value'].encode())
                or (purpose == 'mailbox' and source['source_version'] != target['source_version'])):
            raise ValueError('恢复运营凭据或邮箱版本与认证备份不一致')
    for purpose, (name, field) in credentials.CONFIGS.items():
        if name not in embedded['files']: continue
        source = manage.boot.decode(embedded['files'][name].encode())
        target = manage.boot.decode(fresh['files'][name].encode())
        if purpose == 'backup':
            if target.get('nas') != destination: raise ValueError('目标备份位置不是批准的系统归档目的地')
            source.pop('nas', None); target.pop('nas', None)
        source.pop(field, None); target.pop(field, None)
        if protocol.canonical(source) != protocol.canonical(target):
            raise ValueError('恢复配置除凭据封装和批准NAS位置外发生变化')


def _content(payload, metadata, root, state, target, target_package, public, source, backup_sha):
    """Validate authenticated original payload and restored facts; no mutations."""
    original, restored = payload/'shared', root/'shared'
    _no_journal(original); _no_journal(restored)
    database = original/'data/carton_erp.sqlite3'
    if (metadata.get('type') != 'tianming.recovery.v1'
            or metadata.get('credential_transfer') != 'credentials.tmencrypted'
            or metadata.get('release') != source['source_package_sha256']
            or sha(database) != source['snapshot']['database_sha256']
            or database_info(database) != metadata.get('database')):
        raise ValueError(ERROR)
    created = datetime.fromisoformat(metadata['created'])
    if created.utcoffset() is None or protocol._stamp(created) != source['backup_created_at']:
        raise ValueError('最终备份时间与签名来源不符')
    original_snapshot = protocol.capture_snapshot(original, recorded_root=metadata['source_shared'])
    if original_snapshot != source['snapshot']: raise ValueError(ERROR)
    # This audit precedes network application. The proposal is separately bound
    # to the request; no deployment field has been authorized to change yet.
    if (original/'environment.json').read_bytes() != (restored/'environment.json').read_bytes():
        raise ValueError('目标环境尚未激活，必须完整保留备份原配置')
    release = payload/'release.zip'
    if sha(release) != metadata['release']: raise ValueError(ERROR)
    _, source_manifest = inspect_package(release, public)
    if (source_manifest.get('runtime_platform', 'windows') != 'windows'
            or source_manifest['version'] != metadata.get('version')): raise ValueError(ERROR)
    _, schema = schema_contract_from_signed_release(release, public)
    members = {'release.zip', 'credentials.tmencrypted'}
    authority = metadata.get('schema_authority')
    if source_manifest['revision'] != original_snapshot['revision']:
        protocol._hash(authority)
        archive = payload/'compatibility.zip'
        if sha(archive) != authority: raise ValueError(ERROR)
        _, authority_manifest = inspect_package(archive, public)
        policy = authority_manifest.get('migration') or {}
        if (authority_manifest['revision'] != original_snapshot['revision']
                or policy.get('policy') != 'preserve_existing_facts_v1'
                or policy.get('rollback_package_sha256') != metadata['release']): raise ValueError(ERROR)
        _, schema = schema_contract_from_signed_release(archive, public)
        members.add('compatibility.zip')
    elif authority is not None: raise ValueError(ERROR)
    if {p for p in metadata['files'] if not p.startswith('shared/')} != members: raise ValueError(ERROR)
    validate_database_schema(database, schema)
    modeled, target_schema = schema_contract_from_signed_release(target_package, public)
    if modeled != target or target['revision'] != original_snapshot['revision']: raise ValueError(ERROR)
    validate_database_schema(database, target_schema)
    validate_database_schema(restored/'data/carton_erp.sqlite3', target_schema)
    restored_inventory = inventory.for_recovery(metadata, source_manifest, backup_sha)
    restored_dispatch = dispatch.validate_recovery(original, metadata, source_manifest)
    if (protocol.canonical(state.get(inventory.STATE_KEY)) != protocol.canonical(restored_inventory)
            or dispatch.inspect_activation(restored, state, require_active=True) != restored_dispatch):
        raise ValueError(ERROR)
    target_readers(database, source_manifest, source_manifest, restored_inventory, restored_dispatch, factory=False)
    target_readers(restored/'data/carton_erp.sqlite3', source_manifest, target, restored_inventory, restored_dispatch)
    provenance = {history.RELEASE:metadata['release'], history.AUTHORITY:authority}
    if history.identities(state) != provenance: raise ValueError(ERROR)
    history.verify(provenance, {history.MEMBERS[k]:root/'packages'/(v+'.zip')
                               for k,v in provenance.items() if v is not None}, public)
    target_snapshot = protocol.capture_snapshot(restored)
    protocol.require_matching_snapshots(original_snapshot, target_snapshot)
    return target_snapshot, provenance


@contextmanager
def verified_restored_target(request_id, input_attempt, authorization_sha256, *, inherited_lock=None):
    """Yield public evidence and a recheck callback while original locks are held.

    The caller may publish a separate protected activation only while this
    context remains open. It must not mutate the audited instance or consume an
    evidence dictionary after the context closes as live authorization.
    """
    protocol._id(request_id); protocol._id(input_attempt); protocol._hash(authorization_sha256)
    context = restore.runtime_identity()
    if manage.boot._actual() != manage.boot._expected(context['uid'], context['gid']):
        raise ValueError('最终核验实际专用身份不符')
    root, base = manage.ROOT, manage.BASE
    with manage.instance(context, inherited_lock=inherited_lock) as (folders, lock, parents_check), \
            manage.maintenance(lock, borrowed=inherited_lock is not None), ExitStack() as stack:
        base_lease = stack.enter_context(Lease(base, uid=0, gid=None, mode=0o755, directory=True, mutable=True))
        held = []
        def pin(path, parent, *, uid=context['uid'], gid=context['gid'], mode=0o600, directory=False, mutable=False):
            item = stack.enter_context(Lease(path, uid=uid, gid=gid, mode=mode, directory=directory,
                                            parent=parent, mutable=mutable))
            held.append(item)
            return item
        def record(path, parent, **kwargs):
            item = pin(path, parent, **kwargs)
            state_record = path == root/'state.json'
            raw = _read_complete(item, manage.STATE_LIMIT if state_record else protocol.LIMIT)
            return raw, (manage.decode_record(raw, 'state.json') if state_record else manage.boot.decode(raw))
        public_item = pin(published.PUBLIC, base_lease, uid=0, gid=None, mode=0o644)
        public = _read_complete(public_item, 65536)
        context_raw, actual_context = record(restore.keychain.CONTEXT, base_lease, uid=0, gid=None, mode=0o644)
        pin(published.REGISTRY, base_lease, uid=0, gid=None, mode=0o644)
        if context != actual_context: raise ValueError(ERROR)
        request_raw, request = record(base/('factory-target-'+request_id+'.json'), base_lease, uid=0, gid=None, mode=0o644)
        protocol.validate_request(request)
        _, proposed = record(base/('factory-network-'+request_id+'.json'), base_lease, uid=0, gid=None, mode=0o644)
        network.validate(proposed)
        if (request['request_id'] != request_id or request['network_sha256'] != protocol.digest(proposed)
                or request['publisher_sha256'] != public_identity(public)[1]
                or request['target'] != protocol.local_target(root, context['installation_id'])):
            raise ValueError(ERROR)
        tree, manifest = published.resolve(root, request['package_sha256'], public, kind='migration')
        runtime = manifest.get('runtime_capabilities') or {}
        if (type(runtime.get(CAPABILITY)) is not int or runtime.get(CAPABILITY) != 1
                or capabilities(manifest).get('factory_cutover_v1') != 1 or MODULE not in manifest['files']
                or Path(__file__).resolve() != tree/MODULE
                or Path(sys.executable).resolve() != tree/'runtime/bin/python3.12'):
            raise ValueError('最终核验必须使用批准的不可变程序及完整切换协议')
        inputs = pin(base/'restore-inputs', base_lease, uid=0, mode=0o750, directory=True)
        attempt = pin(inputs.path/input_attempt, inputs, uid=0, mode=0o750, directory=True)
        if set(os.listdir(attempt.fd)) != {'complete.tmbackup', 'authorization.json'}: raise ValueError(ERROR)
        backup = pin(attempt.path/'complete.tmbackup', attempt, uid=0, mode=0o440)
        auth_item = pin(attempt.path/'authorization.json', attempt, uid=0, mode=0o440)
        authorization = _read_complete(auth_item, protocol.LIMIT)
        if manage.boot.digest(authorization) != authorization_sha256: raise ValueError(ERROR)
        body = protocol.verify_authorization(authorization, public, request)
        protocol.require_install_window(body, now=_current())
        source = body['source']
        if restore._digest(backup.fd) != source['backup_sha256']: raise ValueError(ERROR)
        _, state = record(root/'state.json', folders['root'])
        expected_binding = {'type':'tianming.mac-native-restore.v1', 'package_sha256':request['package_sha256'],
            'backup_sha256':source['backup_sha256'], 'context_sha256':manage.boot.digest(context_raw),
            'database_revision':source['snapshot']['revision'], 'schema_migrated':False, 'factory_cutover':False}
        credential_report = {'status':'restored','restored':source['credential_purposes'],'mailbox_version_preserved':True}
        if (state.get('current') != request['package_sha256'] or state.get('previous') is not None
                or state.get('schema_authority') is not None or state.get('manual_stop') is not True
                or state.get('operation') != 'restore' or state.get('onboarding_pending')
                or state.get('restored_backup_sha256') != source['backup_sha256']
                or protocol.canonical(state.get('native_restore_binding')) != protocol.canonical(expected_binding)
                or protocol.canonical(state.get('credential_restore')) != protocol.canonical(credential_report)):
            raise ValueError(ERROR)
        home = pin(base/'home', base_lease, mode=0o700, directory=True, mutable=True)
        _, receipt = record(home.path/('service-restore-'+input_attempt+'.json'), home)
        expected_receipt = {'type':'tianming.mac-service-restore.v1', 'backup_sha256':source['backup_sha256'],
            'package_sha256':request['package_sha256'],'publisher_sha256':request['publisher_sha256'],
            'credentials_sha256':None,'started':False,'factory_cutover':False,'manual_stop':True,
            'native_binding':expected_binding, 'result':{'version':manifest['version'],
                'data_time':state.get('source_time'),'started':False,'credentials':credential_report}}
        if protocol.canonical(receipt) != protocol.canonical(expected_receipt): raise ValueError(ERROR)
        paths = {root/name for name,_ in credentials.CONFIGS.values()} | {root/'control/process.json'}
        present = {p for p in paths if os.path.lexists(p)}
        for path in present:
            pin(path, folders['control'] if path.parent == root/'control' else folders['root'])
        _, preferences = manage.settings(folders['root'], context)
        destination = preferences.get('nas')
        archive_check = _archive(destination, context)
        manager = _TargetReader(root, public)
        if manager._process(): raise ValueError('目标ERP仍运行，不自动停止')
        manager._approve_execution(state['current'])
        _no_journal(root/'shared')
        tree_before = _private_tree(folders['shared'], context)
        data = pin(root/'shared/data', folders['shared'], mode=0o700, directory=True)
        pin(data.path/'carton_erp.sqlite3', data)
        package_ids = {request['package_sha256'], state.get(history.RELEASE), state.get(history.AUTHORITY)} - {None}
        for identity in package_ids:
            protocol._hash(identity)
            pin(root/'packages'/(identity+'.zip'), folders['packages'])
            if sha(root/'packages'/(identity+'.zip')) != identity: raise ValueError(ERROR)
        # No prompts or password arguments: use the already-restored service key.
        password = credential_store.unprotect_backup(preferences['protected_password'])
        previous_umask = os.umask(0o077)
        try:
            name = 'factory-audit-'+uuid.uuid4().hex
            os.mkdir(name, mode=0o700, dir_fd=folders['staging'].fd)
            job = pin(folders['staging'].path/name, folders['staging'], mode=0o700, directory=True, mutable=True)
            raw = job.path/'recovery.zip'
            authenticated = decrypt_file(backup.path, raw, password)
            backup.check()
            if authenticated != source['backup_sha256']: raise ValueError(ERROR)
            payload = job.path/'payload'
            metadata = extract_verified(raw, payload)
        finally:
            os.umask(previous_umask)
        if metadata.get('created') != state.get('source_time'): raise ValueError(ERROR)
        target_snapshot, provenance = _content(payload, metadata, root, state, manifest,
                                               tree.parent/'package.zip', public, source, authenticated)
        credential_bytes = credentials._read(payload/'credentials.tmencrypted', credentials.LIMIT)
        if manage.boot.digest(credential_bytes) != metadata['files']['credentials.tmencrypted']:
            raise ValueError(ERROR)
        embedded = credentials.unseal(credential_bytes, password)
        def recheck():
            protocol.require_install_window(body, now=_current())
            parents_check()
            for item in held: item.check()
            archive_check()
            if (manager._process() or {p for p in paths if os.path.lexists(p)} != present
                    or _private_tree(folders['shared'], context) != tree_before): raise ValueError(ERROR)
            _no_journal(root/'shared')
            if protocol.capture_snapshot(root/'shared') != target_snapshot: raise ValueError(ERROR)
            manager._approve_execution(state['current'])
            if any(sha(root/'packages'/(identity+'.zip')) != identity for identity in package_ids):
                raise ValueError(ERROR)
            history.verify(provenance, {history.MEMBERS[k]:root/'packages'/(v+'.zip')
                                       for k,v in provenance.items() if v is not None}, public)
            _same_credentials(embedded, credentials.collect(root, immutable_database=True), password,
                              source['credential_purposes'], destination)
            if restore._digest(backup.fd) != authenticated: raise ValueError(ERROR)
            for item in held: item.check()
            if _private_tree(folders['shared'], context) != tree_before: raise ValueError(ERROR)
            archive_check()
            protocol.require_install_window(body, now=_current())
        recheck()
        evidence = {'type':'tianming.factory-target-audit.v1', 'request_sha256':manage.boot.digest(request_raw),
            'authorization_sha256':authorization_sha256,'backup_sha256':authenticated,
            'package_sha256':request['package_sha256'],'context_sha256':manage.boot.digest(context_raw),
            'network_sha256':request['network_sha256'],'target_snapshot':dict(target_snapshot),
            'credential_purposes':list(source['credential_purposes']),'credentials_verified':True,
            'retained_workspace':str(job.path),'factory_activated':False,'network_reachability_verified':False,
            'source_physical_isolation_verified':False}
        yield evidence, recheck
        recheck()
