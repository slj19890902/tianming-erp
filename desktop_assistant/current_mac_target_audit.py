"""Read-only v3 audit of this Mac's current, stopped business and cold backup.

This is an internal context, not a command, restore, transport or activation
entry. A signed caller must supply the independently approved original restore
receipt hash. Public preparation is evidence only while this context is open.
The existing maintenance lock and repeated FD/content checks exclude cooperating
writers; they are not Windows mandatory write leases. New private audit files
remain on success and failure. No network connection or NAS credential is used.
"""
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import hmac
import os
from pathlib import Path
import re
import sys
import uuid

MODULE = 'desktop_assistant/current_mac_target_audit.py'
CAPABILITY = 'current_mac_audit_v3'
EXECUTION_CAPABILITY = 'factory_current_mac_execution_v3'
ERROR = '当前Mac完整冷备审计未确认；保留原件及私有审计现场'


def _require(value):
    if not value:
        raise ValueError(ERROR)


def _current():
    return datetime.now(timezone.utc)


def _stopped(manager, state, execution, *, mounted=False):
    from . import factory_target_audit as old
    _require(state.get('current') == execution and state.get('manual_stop') is True
        and state.get('operation') == 'update' and state.get('schema_authority') is None
        and not any(state.get(k) for k in (('onboarding_pending','backup_work_retained') if mounted
                                       else ('onboarding_pending','backup_error','backup_work_retained')))
        and (mounted or state.get('update_backup') == state.get('last_backup'))
        and isinstance(state.get('previous'), str)
        and re.fullmatch('[0-9a-f]{64}', state['previous']))
    with os.scandir(manager.root) as entries:
        _require(not any(e.name == 'state.json.pending'
            or re.fullmatch(r'state-[0-9a-f]{32}\.pending\.json', e.name) for e in entries))
    old._no_journal(manager.root/'shared')
    _require(manager._process() is None)


def _credentials(embedded, fresh, password):
    """Darwin-to-Darwin exact configuration/reference/version, secrets in RAM."""
    from . import credential_transfer as transfer
    transfer._validate(embedded); transfer._validate(fresh)
    _require(embedded['source_platform'] == fresh['source_platform'] == 'darwin'
        and embedded['bound_backup_sha256'] is fresh['bound_backup_sha256'] is None
        and embedded['files'] == fresh['files']
        and set(embedded['entries']) == set(fresh['entries'])
        and 'backup' in embedded['entries'])
    for purpose, before in embedded['entries'].items():
        after = fresh['entries'][purpose]
        _require({k:v for k,v in before.items() if k != 'value'} ==
                 {k:v for k,v in after.items() if k != 'value'})
        _require(hmac.compare_digest(before['value'].encode(), after['value'].encode()))
    _require(hmac.compare_digest(embedded['entries']['backup']['value'].encode(), password.encode()))


def _content(payload, metadata, root, state, execution_manifest, execution_archive, public, cipher_sha,
             *, backup_release=None):
    from . import factory_cutover as facts, factory_target_audit as old, migration
    from . import restore_provenance as history, delivery_dispatch_contract as dispatch
    from . import order_inventory_contract as inventory
    from .reader_contract import target_readers
    from .schema_contract import schema_contract_from_signed_release, validate_database_schema
    from .storage import sha, database_info
    source = payload/'shared'; target = root/'shared'
    backup_release = state['previous'] if backup_release is None else backup_release
    old._no_journal(source); old._no_journal(target)
    _require(metadata.get('type') == 'tianming.recovery.v1'
        and metadata.get('credential_transfer') == 'credentials.tmencrypted'
        and metadata.get('release') == backup_release
        and metadata.get('source_shared') == str(target)
        and metadata.get('schema_authority') is None
        and sha(payload/'release.zip') == backup_release
        and database_info(source/'data/carton_erp.sqlite3') == metadata.get('database'))
    release, schema = schema_contract_from_signed_release(payload/'release.zip', public)
    current, current_schema = schema_contract_from_signed_release(execution_archive, public)
    _require(release.get('runtime_platform') == current.get('runtime_platform') == 'macos-arm64'
        and release['version'] == metadata.get('version') and current == execution_manifest
        and release['revision'] == current['revision'] == metadata['database']['revision']
        and schema == current_schema)
    validate_database_schema(source/'data/carton_erp.sqlite3', schema)
    validate_database_schema(target/'data/carton_erp.sqlite3', schema)
    provenance, originals = history.for_recovery(payload, metadata, release, public)
    _require(provenance == history.identities(state) and provenance)
    _require({n for n in metadata['files'] if not n.startswith('shared/')} ==
             {'release.zip', 'credentials.tmencrypted', *originals})
    inv = inventory.for_recovery(metadata, release, cipher_sha)
    act = dispatch.validate_recovery(source, metadata, release)
    _require(state.get(inventory.STATE_KEY) == inv
        and dispatch.inspect_activation(target, state, require_active=True) == act)
    target_readers(source/'data/carton_erp.sqlite3', release, release, inv, act, factory=False)
    target_readers(target/'data/carton_erp.sqlite3', release, current, inv, act, factory=False)
    # Raw equality covers every shared file, including the complete DB and
    # environment, without v1's Windows normalization or LOGIN exceptions.
    source_files, target_files = migration.files(source), migration.files(target)
    _require(source_files == target_files)
    snapshot = facts.capture_snapshot(target)
    _require(facts.capture_snapshot(source, recorded_root=metadata['source_shared']) == snapshot)
    return snapshot, target_files


@contextmanager
def verified_current_mac_backup(execution_package_sha256, monitor_package_sha256, restore_attempt, *,
        context_sha256, publisher_sha256, registry_sha256, restore_receipt_sha256,
        inherited_lock=None, expected_preparation=None, mounted_fd=None, audit_package_sha256=None):
    """Yield detached v3 preparation and live recheck; never accept caller facts.

    expected_preparation retains its original prepared_at, while all other
    fields are reconstructed and compared. Signature/installation windows belong
    to the later consumer, not this read-only audit. Hash inputs are approvals,
    not discovered credentials. All paths derive from the fixed installation.
    """
    from . import current_mac_cutover_contract as contract, factory_cutover as facts
    from . import factory_target_audit as old, mac_service_restore as restore
    from . import mac_service_manage as manage, mac_system_service as system
    from . import mac_published_runtime as published, mac_smb_stream_runtime as stream_runtime
    from . import mac_smb_stream_target as stream, mac_smb_stream_receipt as receipts
    from . import credential_transfer as transfer, credential_store, migration, restore_provenance as history
    from .mac_restore_completion import _decrypt_retained
    from .mac_service_restore import Lease, _read_complete
    from .reader_contract import capabilities
    from .storage import sha, extract_verified

    for value in (execution_package_sha256, monitor_package_sha256, context_sha256,
                  publisher_sha256, registry_sha256, restore_receipt_sha256): contract._hash(value)
    contract._id(restore_attempt)
    expected = None if expected_preparation is None else contract.decode(contract.canonical(expected_preparation))
    if expected is not None:
        contract.validate_preparation(expected)
        _require(contract._time(expected['prepared_at']) <= _current())
    mounted = mounted_fd is not None
    _require(mounted == (audit_package_sha256 is not None))
    if mounted: contract._hash(audit_package_sha256)
    context = restore.runtime_identity()
    _require(context['uid'] == context['gid'] == 499
        and manage.boot._actual() == manage.boot._expected(499, 499)
        and str(manage.ROOT) == contract.ROOT and manage.ROOT == manage.BASE/'instance')
    root, base = manage.ROOT, manage.BASE
    active = False
    with manage.instance(context, inherited_lock=inherited_lock) as (folders, lock, parents_check), \
            manage.maintenance(lock, borrowed=inherited_lock is not None), ExitStack() as stack:
        held = []
        def pin(path, parent=None, *, uid=499, gid=499, mode=0o600, directory=False, mutable=False):
            item = stack.enter_context(Lease(path, uid=uid, gid=gid, mode=mode, directory=directory,
                                            mutable=mutable, parent=parent))
            held.append(item)
            return item
        def record(path, parent=None, **kwargs):
            item = pin(path, parent, **kwargs)
            state_file = path == root/'state.json'
            raw = _read_complete(item, manage.STATE_LIMIT if state_file else contract.LIMIT)
            value = manage.decode_record(raw, 'state.json') if state_file else manage.boot.decode(raw)
            return raw, value
        base_lease = pin(base, uid=0, gid=None, mode=0o755, directory=True, mutable=True)
        public_item = pin(published.PUBLIC, base_lease, uid=0, gid=None, mode=0o644)
        public = _read_complete(public_item, contract.LIMIT)
        context_raw, actual_context = record(restore.keychain.CONTEXT, base_lease, uid=0, gid=None, mode=0o644)
        registry_raw, _ = record(published.REGISTRY, base_lease, uid=0, gid=None, mode=0o644)
        _require(actual_context == context and manage.boot.digest(public) == publisher_sha256
            and manage.boot.digest(context_raw) == context_sha256
            and manage.boot.digest(registry_raw) == registry_sha256)
        _require(not os.path.lexists(base/'factory-activation.json'))

        def approved(identity):
            meta = system.metadata(identity)
            tree, manifest = published.resolve(root, identity, public, kind='migration')
            _require(meta['tree'] == tree and meta['context'] == context
                and meta['context_sha256'] == context_sha256 and meta['publisher_sha256'] == publisher_sha256
                and meta['registry_sha256'] == registry_sha256 and meta['public'] == public)
            return meta, manifest
        execution_meta, execution_manifest = approved(execution_package_sha256)
        monitor_meta, monitor_manifest = approved(monitor_package_sha256)
        audit_meta,audit_manifest = approved(audit_package_sha256) if mounted else (execution_meta,execution_manifest)
        caps = audit_manifest.get('runtime_capabilities') or {}
        _require(type(caps.get(CAPABILITY)) is int and caps[CAPABILITY] == 1
            and MODULE in audit_manifest['files']
            and Path(__file__).resolve() == audit_meta['tree']/MODULE
            and Path(sys.executable).resolve() == audit_meta['tree']/'runtime/bin/python3.12')
        system.require_management(monitor_manifest)
        for manifest in (execution_manifest, monitor_manifest):
            execution_cap = (manifest.get('runtime_capabilities') or {}).get(EXECUTION_CAPABILITY)
            _require(type(execution_cap) is int and execution_cap == 1
                and capabilities(manifest).get('factory_cutover_v1') == 1)
        control_raw, control = record(system.CONTROL, base_lease, uid=0, gid=None, mode=0o644)
        _require(control == system.record_for(monitor_meta, True))
        plist_raw = system._installed_plist(monitor_meta)
        _require(system.management_service_state() == dict(registered=True, enabled=True, scheduled_backups=True))
        system.verify_keychain(context)
        state_raw, state = record(root/'state.json', folders['root'])
        manager = old._TargetReader(root, public)
        _stopped(manager, state, execution_package_sha256, mounted=mounted)
        _, cold_manifest = approved(state['previous'])
        binding = state.get('native_restore_binding')
        _require(isinstance(binding, dict))
        contract._hash(binding.get('package_sha256'))
        _, native_manifest = approved(binding['package_sha256'])
        _require(native_manifest['revision'] == execution_manifest['revision'])
        expected_binding = dict(type='tianming.mac-native-restore.v1', package_sha256=binding['package_sha256'],
            backup_sha256=state.get('restored_backup_sha256'), context_sha256=context_sha256,
            database_revision=execution_manifest['revision'], schema_migrated=False, factory_cutover=False)
        _require(manage.boot.canonical(binding) == manage.boot.canonical(expected_binding))
        contract._hash(binding['backup_sha256'])
        provenance = history.identities(state)
        _require(bool(provenance))
        package_ids = {execution_package_sha256, state['previous'], binding['package_sha256'],
                       *provenance.values()} - {None}
        for identity in package_ids:
            pin(root/'packages'/(identity+'.zip'), folders['packages'])
            _require(sha(root/'packages'/(identity+'.zip')) == identity)
        history_paths = {history.MEMBERS[k]:root/'packages'/(v+'.zip') for k,v in provenance.items() if v is not None}
        history.verify(provenance, history_paths, public)
        inputs = pin(base/'restore-inputs', base_lease, uid=0, mode=0o750, directory=True)
        original = pin(inputs.path/restore_attempt, inputs, uid=0, mode=0o750, directory=True)
        _require(set(os.listdir(original.fd)) == {'complete.tmbackup', 'credentials.tmencrypted'})
        old_cipher = pin(original.path/'complete.tmbackup', original, uid=0, mode=0o440)
        old_credentials = pin(original.path/'credentials.tmencrypted', original, uid=0, mode=0o440)
        _require(restore._digest(old_cipher.fd) == binding['backup_sha256'])
        old_credentials_sha = restore._digest(old_credentials.fd)
        home = pin(base/'home', base_lease, mode=0o700, directory=True, mutable=True)
        old_receipt_raw, old_receipt = record(home.path/('service-restore-'+restore_attempt+'.json'), home)
        _require(manage.boot.digest(old_receipt_raw) == restore_receipt_sha256)
        report = state.get('credential_restore')
        _require(isinstance(report, dict) and set(report) == {'status', 'restored', 'mailbox_version_preserved'}
            and report['status'] == 'restored' and report['mailbox_version_preserved'] is True
            and isinstance(report['restored'], list) and report['restored'] == sorted(set(report['restored']))
            and 'backup' in report['restored'] and set(report['restored']) <= {'backup','mailbox','deepseek','openai'})
        expected_receipt = dict(type='tianming.mac-service-restore.v1', backup_sha256=binding['backup_sha256'],
            package_sha256=binding['package_sha256'], publisher_sha256=publisher_sha256,
            credentials_sha256=old_credentials_sha, started=False, factory_cutover=False, manual_stop=True,
            native_binding=binding, result=dict(version=native_manifest['version'], data_time=state.get('source_time'),
                                               started=False, credentials=report))
        _require(manage.boot.canonical(old_receipt) == manage.boot.canonical(expected_receipt))
        restore_proof = dict(backup_sha256=binding['backup_sha256'],
            backup_created_at=contract.normalize_timestamp(state['source_time']), credentials_sha256=old_credentials_sha,
            restore_package_sha256=binding['package_sha256'], restore_receipt_sha256=restore_receipt_sha256,
            native_binding_sha256=contract.digest(binding), **provenance)

        paths = {root/name for name,_ in transfer.CONFIGS.values()} | {root/'control/process.json'}
        present = {p for p in paths if os.path.lexists(p)}
        for path in present: pin(path, folders['control'] if path.parent == root/'control' else folders['root'])
        _, preferences = manage.settings(folders['root'], context)
        # prepare() checks the transport module's own __file__; load_binding()
        # independently authenticates that package without conflating execution.
        backup_release = None; checkpoint = None
        if mounted:
            from . import mounted_nas_proof as mount_proof
            from types import SimpleNamespace
            bound = mount_proof.load()
            _require(bound['control']['package_sha256'] == audit_package_sha256
                and bound['control']['context_sha256'] == context_sha256
                and bound['control']['publisher_sha256'] == publisher_sha256)
            mount_proof.check_fd(mounted_fd,bound)
            for path,raw_bytes in bound['raw'].items():
                held_file = pin(path,base_lease,uid=0,gid=None,mode=0o644)
                _require(_read_complete(held_file,contract.LIMIT) == raw_bytes)
            proof,original_local,proof_path,proof_raw = mount_proof._original_proof(state)
            _require(sha(proof_path) == bound['contract']['local_update_receipt_sha256']
                and str(proof_path) == bound['contract']['local_update_proof_path'])
            if bound['contract']['checkpoint_attempt_id'] is not None:
                checkpoint = mount_proof.checkpoint_result(bound['contract']['checkpoint_attempt_id'])
                mount_proof.validate_checkpoint(checkpoint)
                _require(checkpoint['release_sha256'] == state['current']
                    and checkpoint['local_update_receipt_sha256'] == sha(proof_path))
                backup_release = checkpoint['release_sha256']
                check_path = Path(checkpoint['proof_path'])
                check_folder = pin(check_path.parent,folders['staging'],mode=0o700,directory=True)
                check_item = pin(check_path,check_folder)
                _require(_read_complete(check_item,contract.LIMIT) == contract.canonical(checkpoint))
                source_path = Path(checkpoint['local_path'])
            else:
                source_path = original_local
            _require(str(source_path) == bound['contract']['local_path'])
            original_backup = pin(original_local,folders['backups'])
            _require(restore._digest(original_backup.fd)==proof['backup_sha256'])
            index = {'receipt_sha256':bound['control']['receipt_sha256']}
            receipt = dict(sha256=bound['receipt']['sha256'],size=bound['receipt']['size'],
                verified_at=bound['receipt']['verified_at'],control_sha256=bound['control_sha'],
                contract_sha256=bound['control']['contract_sha256'],package_sha256=audit_package_sha256)
            if checkpoint: receipt.update(raw_sha256=checkpoint['raw_sha256'],database=checkpoint['database'])
            backup = pin(source_path,folders['backups'])
            _require(mount_proof.fingerprint(os.fstat(backup.fd)) == bound['receipt']['local_identity'])
            def mount_check():
                _require(mount_proof.load()['raw'] == bound['raw'])
                mount_proof.check_fd(mounted_fd,bound)
                _require(mount_proof.fingerprint(os.fstat(backup.fd)) == bound['receipt']['local_identity']
                    and mount_proof._original_proof(state)[3] == proof_raw)
            transport = SimpleNamespace(check=mount_check)
        else:
            transport = stream_runtime.load_binding()
            _require(transport.context == context and stream.selected(preferences.get('nas'))
                and str(stream.destination(transport.control)) == preferences['nas'])
            index = state.get(receipts.INDEX)
            _require(isinstance(index, dict) and set(index) == {'path','receipt','receipt_sha256'}
                and index['path'] == state['last_backup'])
            contract._hash(index['receipt_sha256'])
            filename = Path(index['path']).name
            _require(index['receipt'] == receipts.receipt_name(filename))
            receipt_dir = pin(root/'control/backup-receipts', folders['control'], mode=0o700, directory=True)
            receipt_raw, receipt = record(receipt_dir.path/index['receipt'], receipt_dir)
            _require(receipt_raw == receipts.cfg.canonical(receipt)
                and manage.boot.digest(receipt_raw) == index['receipt_sha256'])
            bound = dict(context=context, control=transport.control, value=transport.value, control_sha=transport.control_sha)
            receipts.validate(receipt, bound, now=_current())
            _require(receipt['local_copy']['path'] == index['path'] and receipt['filename'] == filename
                and receipt['verified_at'] == state['last_backup_at'])
            backup = stack.enter_context(receipts.copy_lease(folders['root'], context, filename))
            held.append(backup)
            _require(receipts.fingerprint(os.fstat(backup.fd)) == receipt['local_copy']['identity']
                and restore._digest(backup.fd) == receipt['sha256'])
        tree_before = old._private_tree(folders['shared'], context)
        files_before = migration.files(root/'shared')

        def root_check():
            parents_check()
            for item in held: item.check()
            _require(not os.path.lexists(base/'factory-activation.json')
                and restore.runtime_identity() == context
                and manage.boot._actual() == manage.boot._expected(499, 499)
                and Path(__file__).resolve() == audit_meta['tree']/MODULE
                and Path(sys.executable).resolve() == audit_meta['tree']/'runtime/bin/python3.12')
            _stopped(manager, state, execution_package_sha256, mounted=mounted)
            _require(manage.record(folders['root'], 'state.json', context)[0] == state_raw
                and {p for p in paths if os.path.lexists(p)} == present)
            for identity in (execution_package_sha256, monitor_package_sha256, state['previous'], binding['package_sha256']):
                approved(identity)
            _require(system.checked_control(monitor_meta)[0] == control_raw
                and system._installed_plist(monitor_meta) == plist_raw
                and system.management_service_state() == dict(registered=True, enabled=True, scheduled_backups=True))
            transport.check()
            if not mounted:
                fresh_binding = stream_runtime.load_binding()
                _require(fresh_binding.records == transport.records and fresh_binding.context == context)
            else:
                approved(audit_package_sha256)
            _require(all(sha(root/'packages'/(identity+'.zip')) == identity for identity in package_ids))
            history.verify(provenance, history_paths, public)
            _require(restore._digest(old_cipher.fd) == binding['backup_sha256']
                and restore._digest(old_credentials.fd) == old_credentials_sha
                and (mounted or receipts.fingerprint(os.fstat(backup.fd)) == receipt['local_copy']['identity'])
                and restore._digest(backup.fd) == receipt['sha256']
                and old._private_tree(folders['shared'], context) == tree_before
                and migration.files(root/'shared') == files_before)

        root_check()
        password = credential_store.unprotect_backup(preferences['protected_password'])
        previous_umask = os.umask(0o077)
        try:
            name = 'current-mac-audit-'+uuid.uuid4().hex
            os.mkdir(name, mode=0o700, dir_fd=folders['staging'].fd)
            job = pin(folders['staging'].path/name, folders['staging'], mode=0o700, directory=True, mutable=True)
            raw = job.path/'recovery.zip'
            authenticated = _decrypt_retained(backup, raw, password)
            _require(authenticated == receipt['sha256']
                and ('raw_sha256' not in receipt or sha(raw) == receipt['raw_sha256']))
            payload = job.path/'payload'
            metadata = extract_verified(raw, payload)
        finally:
            os.umask(previous_umask)
        backup.check()
        snapshot, shared_files = _content(payload, metadata, root, state, execution_manifest,
            execution_meta['tree'].parent/'package.zip', public, authenticated, backup_release=backup_release)
        _require('database' not in receipt or metadata['database'] == receipt['database'])
        if checkpoint:
            _require(checkpoint['shared_manifest_sha256'] == facts.digest(shared_files)
                and checkpoint['created_at'] == metadata['created'])
        created = datetime.fromisoformat(metadata['created'])
        verified = datetime.fromisoformat(receipt['verified_at'])
        _require(created.utcoffset() is not None and verified.utcoffset() is not None
            and created <= verified <= _current())
        credential_bytes = transfer._read(payload/'credentials.tmencrypted', transfer.LIMIT)
        _require(manage.boot.digest(credential_bytes) == metadata['files']['credentials.tmencrypted'])
        embedded = transfer.unseal(credential_bytes, password)
        # collect preserves config text semantically through UTF-8-SIG. Require
        # exact original raw bytes too; unsupported BOM inputs fail closed.
        for name, text in embedded['files'].items():
            _require((root/name).read_bytes() == text.encode('utf-8'))
        private_before = old._private_tree(job, context)
        private_files = migration.files(job.path)
        backup_proof = dict(sha256=authenticated, size=receipt['size'],
            created_at=contract.normalize_timestamp(metadata['created']),
            verified_at=contract.normalize_timestamp(receipt['verified_at']), receipt_sha256=index['receipt_sha256'],
            transport_control_sha256=receipt['control_sha256'], transport_contract_sha256=receipt['contract_sha256'],
            transport_package_sha256=receipt['package_sha256'], release_sha256=metadata['release'],
            schema_authority_sha256=metadata['schema_authority'], credentials_sha256=manage.boot.digest(credential_bytes))
        prepared_at = contract._time(expected['prepared_at']) if expected is not None else _current()
        preparation = contract.make_preparation(target=facts.local_target(root, context['installation_id']),
            publisher_sha256=publisher_sha256, context_sha256=context_sha256, registry_sha256=registry_sha256,
            state_sha256=manage.boot.digest(state_raw), execution_package_sha256=execution_package_sha256,
            monitor_package_sha256=monitor_package_sha256, system_control_sha256=manage.boot.digest(control_raw),
            system_plist_sha256=manage.boot.digest(plist_raw), restore=restore_proof, backup=backup_proof,
            target_snapshot=snapshot, shared_manifest_sha256=facts.digest(shared_files),
            credential_purposes=sorted(embedded['entries']), now=prepared_at)
        _require(expected is None or expected == preparation)
        active = True
        def recheck():
            _require(active)
            root_check()
            _require(old._private_tree(job, context) == private_before and migration.files(job.path) == private_files)
            _credentials(embedded, transfer.collect(root, immutable_database=True), password)
            _require(facts.capture_snapshot(root/'shared') == snapshot)
            # Credential providers may take time; close that mutation window.
            root_check()
            _require(old._private_tree(job, context) == private_before and migration.files(job.path) == private_files)
            return deepcopy(preparation)
        try:
            recheck()
            yield deepcopy(preparation), recheck
            recheck()
        finally:
            active = False
