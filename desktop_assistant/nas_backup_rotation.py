"""Receipt-bound, incremental 2+1 index; no backup generation or ERP stop.

Run only from the approved instance after business has resumed. The concrete Mac
adapter reads existing owner/FD-protected records and calls existing validators.
Recovery reports must be emitted by the isolated recovery worker into that same
protected record directory; arbitrary exported JSON is never an apply authority.
Retired entries persist until an authenticated remote deletion is acknowledged.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import uuid
import zipfile
from datetime import timedelta

from .nas_full_backup_retention import _comparison_key, _relative, _sha, digest

INDEX = 'retention-two.json'
TYPE = 'tianming.nas-backup-rotation.v1'
RECOVERY = 'tianming.nas-independent-recovery.v1'
ROLE_KEYS = ('current', 'previous', 'schema_authority', 'migration_target',
             'restored_release_sha256', 'restored_schema_authority_sha256',
             'restored_backup_sha256', 'migration_backup_sha256',
             'last_backup', 'last_backup_stream', 'last_backup_at', 'operation', 'manual_stop')


def require(ok, reason):
    if not ok: raise ValueError('NAS two-backup rotation blocked: ' + reason)


def references(state):
    require(isinstance(state, dict) and _sha(state.get('current')), 'current runtime identity missing')
    for key in ROLE_KEYS[:8]:
        value = state.get(key)
        require(value is None or _sha(value), 'invalid runtime role ' + key)
    return {key: state.get(key) for key in ROLE_KEYS}


def _summary(expected, observed):
    return {'source_sha256': digest(expected), 'restored_sha256': digest(observed),
            'source_count': len(expected), 'restored_count': len(observed)}


def validate_recovery(report, receipt, receipt_sha):
    """Strict compact worker evidence, read only through protected FD records."""
    fields = {'type', 'backup_sha256', 'receipt_sha256', 'contract_sha256', 'raw_sha256',
              'manifest_sha256', 'source_time', 'inventory', 'shared', 'maps', 'database',
              'programs', 'credentials', 'isolated_root', 'completed_at'}
    require(isinstance(report, dict) and set(report) == fields and report['type'] == RECOVERY,
            'independent complete recovery report missing or invalid')
    require(len(json.dumps(report, sort_keys=True).encode()) < 65536, 'recovery evidence must be compact')
    require(all(report[k] == expected for k, expected in
                (('backup_sha256', receipt['sha256']), ('receipt_sha256', receipt_sha),
                 ('contract_sha256', receipt['contract_sha256']), ('raw_sha256', receipt['raw_sha256']))),
            'recovery report not bound to independent receipt bytes')
    require(_sha(report['manifest_sha256']), 'authenticated manifest digest missing')
    for section in ('inventory', 'shared', 'maps'):
        summary = report[section]
        require(isinstance(summary, dict) and set(summary) == {'source_sha256', 'restored_sha256', 'source_count', 'restored_count'}
                and _sha(summary['source_sha256']) and summary['source_sha256'] == summary['restored_sha256']
                and type(summary['source_count']) is int and summary['source_count'] > 0
                and type(summary['restored_count']) is int and summary['source_count'] == summary['restored_count'],
                'independent recovered ' + section + ' digest/count coverage incomplete')
    require(report['maps']['source_count'] <= report['shared']['source_count'] < report['inventory']['source_count'],
            'recovery coverage counts inconsistent')
    database = report['database']
    require(isinstance(database, dict) and set(database) == {'source', 'restored'}
            and database['source'] == database['restored'] == receipt['database'], 'restored database differs from receipt')
    programs = report['programs']
    require(isinstance(programs, dict) and set(programs) == {'release', 'schema_authority', 'packages'}
            and _sha(programs['release']) and (programs['schema_authority'] is None or _sha(programs['schema_authority']))
            and isinstance(programs['packages'], dict) and 1 <= len(programs['packages']) <= 8,
            'signed release/compatibility/source coverage missing')
    for name, signed in programs['packages'].items():
        require('/' not in _relative(name) and name.endswith('.zip') and isinstance(signed, dict)
                and set(signed) == {'package_sha256', 'manifest_sha256'}
                and _sha(signed['package_sha256']) and _sha(signed['manifest_sha256']), 'invalid signed recovered program')
    require(programs['packages'].get('release.zip', {}).get('package_sha256') == programs['release'], 'release binding missing')
    if programs['schema_authority'] is not None:
        require(programs['packages'].get('compatibility.zip', {}).get('package_sha256') == programs['schema_authority'],
                'schema authority recovery missing')
    credentials = report['credentials']
    require(isinstance(credentials, dict) and set(credentials) == {'source_sha256', 'restored_metadata_sha256', 'status'}
            and _sha(credentials['source_sha256']) and credentials['status'] == 'restored'
            and _sha(credentials['restored_metadata_sha256']), 'credential recovery missing')
    root = Path(report['isolated_root'])
    require(root.is_absolute() and '..' not in root.parts and str(root) != '/Library/TianmingERP/instance',
            'independent isolated recovery root required')
    created, completed = (datetime.fromisoformat(v) for v in (report['source_time'], report['completed_at']))
    require(created.utcoffset() is not None and completed.utcoffset() is not None
            and created <= completed <= datetime.now(timezone.utc), 'authenticated business time missing')
    return report['source_time']


def capture_verified_recovery(restored, receipt, receipt_sha, downloaded, raw_zip):
    """Existing isolated worker calls after real Manager.restore; no new copies.

    Original entries come from authenticated raw_zip: restore moves payload/shared
    into root/shared and may rewrite DB/environment. No payload lifetime assumption.
    Each original entry and each immutable restored shared byte is actually hashed;
    only bounded digests/counts and signature/DB/credential metadata are published.
    """
    from .storage import sha, database_info, signed_release_manifest
    root = Path(restored.root)
    require(root.is_absolute() and root == root.resolve() and root != Path('/Library/TianmingERP/instance')
            and restored._process() is None, 'stopped isolated restore task required')
    require(sha(downloaded) == receipt['sha256'] and sha(raw_zip) == receipt['raw_sha256'],
            'independent ciphertext/decrypted archive identity mismatch')
    verified_files, observed, programs = {}, {}, {}
    with zipfile.ZipFile(raw_zip) as verified:
        manifest = json.loads(verified.read('manifest.json'))
        require(manifest.get('type') == 'tianming.recovery.v1', 'authenticated recovery manifest missing')
        files = manifest['files']
        names = verified.namelist()
        require(len(names) == len(set(names)) and set(names) == set(files) | {'manifest.json'}, 'archive inventory not exact')
        for name, original_sha in files.items():
            _relative(name)
            result = hashlib.sha256()
            with verified.open(name) as source:
                while block := source.read(1024 * 1024): result.update(block)
            require(result.hexdigest() == original_sha, 'authenticated archive entry changed')
            verified_files[name] = result.hexdigest()
            if name.startswith('shared/') and name not in ('shared/data/carton_erp.sqlite3', 'shared/environment.json'):
                target = root / name
                require(target.resolve() == target and not target.is_symlink(), 'restored shared link')
                observed[name] = sha(target)
            elif name.endswith('.zip') and '/' not in name:
                package = root / 'packages' / (original_sha + '.zip')
                require(package.resolve() == package and sha(package) == original_sha, 'restored signed program changed')
                programs[name] = {'package_sha256': original_sha,
                                  'manifest_sha256': digest(signed_release_manifest(package, restored.public_key))}
    state = restored.state
    require(state.get('restored_backup_sha256') == receipt['sha256'] and state.get('source_time') == manifest['created'],
            'actual Manager.restore state not bound to backup')
    credentials = state.get('credential_restore') or {}
    require(credentials.get('status') == 'restored' and manifest.get('credential_transfer') == 'credentials.tmencrypted',
            'actual credentials not restored')
    shared = {name: files[name] for name in observed}
    map_names = [name for name in shared if name.startswith('shared/factory_twin_data/') or '/maps/' in name]
    report = {'type': RECOVERY, 'backup_sha256': receipt['sha256'], 'receipt_sha256': receipt_sha,
              'contract_sha256': receipt['contract_sha256'], 'raw_sha256': receipt['raw_sha256'],
              'manifest_sha256': digest(manifest), 'source_time': manifest['created'],
              'inventory': _summary(files, verified_files), 'shared': _summary(shared, observed),
              'maps': _summary({n: files[n] for n in map_names}, {n: observed[n] for n in map_names}),
              'database': {'source': manifest['database'], 'restored': database_info(root / 'shared/data/carton_erp.sqlite3')},
              'programs': {'release': manifest['release'], 'schema_authority': manifest.get('schema_authority'), 'packages': programs},
              'credentials': {'source_sha256': files['credentials.tmencrypted'], 'status': 'restored',
                              'restored_metadata_sha256': digest(credentials)},
              'isolated_root': str(root), 'completed_at': datetime.now(timezone.utc).isoformat()}
    validate_recovery(report, receipt, receipt_sha)
    return report


def _validate_pointer(pointer):
    require(isinstance(pointer, dict) and set(pointer) == {'receipt', 'receipt_sha256'}
            and isinstance(pointer['receipt'], str) and '/' not in _relative(pointer['receipt'])
            and re.fullmatch(r'[0-9]{8}-[0-9]{6}-[a-f0-9]{8}\.smb-stream\.json', pointer['receipt'])
            and _sha(pointer['receipt_sha256']),
            'index pointer must name an authenticated stream receipt')


def validate_index(index, contract_sha):
    require(isinstance(index, dict) and set(index) == {'type', 'contract_sha256', 'generation', 'kept', 'staged', 'retired', 'bootstrap'}
            and index['type'] == TYPE and index['contract_sha256'] == contract_sha
            and type(index['generation']) is int and index['generation'] >= 0
            and isinstance(index['kept'], list) and len(index['kept']) in (0, 2)
            and isinstance(index['retired'], list), 'rotation index contract/schema invalid')
    seed = index['bootstrap']
    require(seed is None or (not index['kept'] and isinstance(seed, dict) and set(seed) == {'first', 'task_id'}
                            and isinstance(seed['task_id'], str) and re.fullmatch('[a-f0-9]{32}', seed['task_id'])),
            'invalid persistent bootstrap reservation')
    pointers = index['kept'] + ([seed['first']] if seed is not None else []) + ([index['staged']] if index['staged'] is not None else []) + index['retired']
    seen = set()
    for pointer in pointers:
        _validate_pointer(pointer)
        require(_comparison_key(pointer['receipt']) not in seen, 'duplicate indexed pointer')
        seen.add(_comparison_key(pointer['receipt']))
    return index


class MacStreamReceiptAdapter:
    """Concrete fixed-root reader/CAS store; constructor performs no IO/login.

    Only already-approved SMB stream storage is supported. Missing legacy
    archive-contract is not a fallback. Production calls reuse existing root,
    approval, record-owner and stream-receipt validators; no arbitrary reader.
    """
    def __init__(self, manager):
        self.manager = manager
        require(Path(manager.root) == Path('/Library/TianmingERP/instance'), 'fixed instance required')

    @contextmanager
    def records(self):
        from . import mac_smb_stream_receipt as existing
        bound = existing.approval()
        with existing.root_lease(bound['context']) as root:
            with existing.receipts(root, bound['context']) as directory:
                yield existing, bound, root, directory
            require(existing.approval() == bound, 'approved storage changed')

    def state(self):
        from . import mac_backup_storage as records
        with self.records() as (_, bound, root, _):
            _, state = records._local_object(root.fd, 'state.json', bound['context']['uid'])
        require(state.get('manual_stop') is False and self.manager._process() is not None,
                'business must already be running; rotation never starts/stops it')
        references(state)
        return state

    def load_verified(self, pointer):
        _validate_pointer(pointer)
        from . import mac_backup_storage as records
        with self.records() as (existing, bound, _, directory):
            raw, receipt = records._local_object(directory, pointer['receipt'], bound['context']['uid'])
            require(hashlib.sha256(raw).hexdigest() == pointer['receipt_sha256'], 'receipt bytes changed')
            existing.validate(receipt, bound)
            require(existing.receipt_name(receipt['filename']) == pointer['receipt'], 'receipt name mismatch')
            name = 'retention-recovery-' + receipt['sha256'] + '.json'
            recovery_raw, recovery = records._local_object(directory, name, bound['context']['uid'])
            source_time = validate_recovery(recovery, receipt, pointer['receipt_sha256'])
            return {'pointer': dict(pointer), 'receipt': receipt, 'source_time': source_time,
                    'recovery_sha256': hashlib.sha256(recovery_raw).hexdigest(),
                    'contract_sha256': bound['control']['contract_sha256']}

    def load_receipt(self, pointer):
        """Authenticated candidate receipt, before a recovery report exists."""
        _validate_pointer(pointer)
        from . import mac_backup_storage as records
        with self.records() as (existing, bound, _, directory):
            raw, receipt = records._local_object(directory, pointer['receipt'], bound['context']['uid'])
            require(hashlib.sha256(raw).hexdigest() == pointer['receipt_sha256'], 'receipt bytes changed')
            existing.validate(receipt, bound)
            require(existing.receipt_name(receipt['filename']) == pointer['receipt'], 'receipt name mismatch')
            return receipt

    def publish_recovery(self, pointer, report):
        """Only the approved isolated restore worker may call this publisher."""
        _validate_pointer(pointer)
        from . import mac_backup_storage as records
        with self.records() as (existing, bound, _, directory):
            raw, receipt = records._local_object(directory, pointer['receipt'], bound['context']['uid'])
            require(hashlib.sha256(raw).hexdigest() == pointer['receipt_sha256'], 'receipt bytes changed')
            existing.validate(receipt, bound)
            validate_recovery(report, receipt, pointer['receipt_sha256'])
            name = 'retention-recovery-' + receipt['sha256'] + '.json'
            payload = records.canonical(report)
            try: existing_raw, _ = records._local_object(directory, name, bound['context']['uid'])
            except FileNotFoundError: existing_raw = None
            if existing_raw is not None:
                require(existing_raw == payload, 'different recovery report already published')
                return
            records._exclusive(directory, name, payload, bound['context']['uid'])
            os.fsync(directory)

    def active_references(self, state):
        """Collect on demand before/after large reads; no stale report reuse."""
        evidence = self.collect_active_references(state)
        return validate_references(evidence, state, evidence['contract_sha256'])

    def collect_active_references(self, state):
        """Actual fixed owner-record/staging collector; missing registration blocks.

        Existing backup/restore/rehearsal owners call record_job at lifecycle
        boundaries under their existing lock. Historical unregistered staging is
        never guessed closed. This samples small local metadata only, on demand
        after hashing as well as before it; no NAS scan or global monitor.
        """
        from . import mac_backup_storage as records
        with self.records() as (_, bound, root, directory):
            _, registry = records._local_object(directory, 'retention-jobs.json', bound['context']['uid'])
            contract_sha = bound['control']['contract_sha256']
            require(isinstance(registry, dict) and set(registry) == {'type', 'contract_sha256', 'jobs'}
                    and registry['type'] == 'tianming.nas-registered-jobs.v1'
                    and registry['contract_sha256'] == contract_sha and isinstance(registry['jobs'], dict),
                    'authenticated job registration missing')
            for job_id, job in registry['jobs'].items():
                validate_job(job)
                require(job_id == job['job_id'], 'job registration identity mismatch')
            with records._record_directory(root.fd, 'staging', bound['context']['uid'], os.fstat(root.fd).st_dev) as stage:
                observed_jobs = set(os.listdir(stage))
            require(observed_jobs <= set(registry['jobs']), 'unregistered staging job; cannot infer idle/closed')
            # State and registration must both be stable during this short read.
            _, fresh = records._local_object(root.fd, 'state.json', bound['context']['uid'])
            require(fresh == state, 'runtime state changed during reference collection')
        return collect_registered_references(registry, observed_jobs, state, contract_sha)

    def record_job(self, job):
        """Existing trusted worker emits its own lifecycle; no arbitrary JSON CLI.

        Does not assert closure of any other job. A newly created empty registry
        cannot bypass pre-existing unregistered staging: collector rejects it.
        """
        with self.manager.lock():
            self.record_job_locked(job)

    def record_job_locked(self, job):
        """Same publisher when the existing worker already holds manager.lock."""
        validate_job(job)
        from . import mac_backup_storage as records
        with self.records() as (_, bound, _, directory):
            try: _, registry = records._local_object(directory, 'retention-jobs.json', bound['context']['uid'])
            except FileNotFoundError:
                registry = {'type': 'tianming.nas-registered-jobs.v1',
                            'contract_sha256': bound['control']['contract_sha256'], 'jobs': {}}
            require(registry['type'] == 'tianming.nas-registered-jobs.v1'
                    and registry['contract_sha256'] == bound['control']['contract_sha256'], 'job registry contract changed')
            registry['jobs'][job['job_id']] = dict(job)
            payload = records.canonical(registry)
            require(len(payload) < 1024 * 1024, 'job metadata exceeds existing protected-reader limit')
            temporary = 'retention-jobs-' + uuid.uuid4().hex + '.pending'
            records._exclusive(directory, temporary, payload, bound['context']['uid'])
            os.replace(temporary, 'retention-jobs.json', src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)

    def read_index(self):
        from . import mac_backup_storage as records
        with self.records() as (_, bound, _, directory):
            contract_sha = bound['control']['contract_sha256']
            try: raw, index = records._local_object(directory, INDEX, bound['context']['uid'])
            except FileNotFoundError:
                return None, {'type': TYPE, 'contract_sha256': contract_sha, 'generation': 0,
                              'kept': [], 'staged': None, 'retired': [], 'bootstrap': None}
            validate_index(index, contract_sha)
            return hashlib.sha256(raw).hexdigest(), index

    def cas_index(self, expected_sha, index):
        """Caller holds the existing short manager lock; no network/hash under it."""
        from . import mac_backup_storage as records
        with self.records() as (_, bound, _, directory):
            validate_index(index, bound['control']['contract_sha256'])
            try: raw, _ = records._local_object(directory, INDEX, bound['context']['uid'])
            except FileNotFoundError: raw = None
            require((None if raw is None else hashlib.sha256(raw).hexdigest()) == expected_sha, 'index CAS conflict')
            payload = records.canonical(index)
            require(len(payload) < 1024 * 1024, 'index exceeds existing protected-reader limit')
            temporary = 'retention-two-' + uuid.uuid4().hex + '.pending'
            records._exclusive(directory, temporary, payload, bound['context']['uid'])
            os.replace(temporary, INDEX, src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
            saved, _ = records._local_object(directory, INDEX, bound['context']['uid'])
            require(saved == payload, 'index publication not confirmed')
            return hashlib.sha256(saved).hexdigest()


def _protected_sha(state):
    return {state[key] for key in ('restored_backup_sha256', 'migration_backup_sha256') if state.get(key)}


def validate_job(job):
    fields = {'job_id', 'kind', 'status', 'backup_path', 'backup_sha256', 'receipt'}
    require(isinstance(job, dict) and set(job) == fields
            and isinstance(job['job_id'], str)
            and re.fullmatch(r'(?:smb-stream-|local-code-update-|mounted-checkpoint-|import-)?[0-9a-f]{32}', job['job_id'])
            and job['kind'] in ('backup', 'upload', 'restore', 'rehearsal')
            and job['status'] in ('active', 'completed', 'failed_closed')
            and all(isinstance(job[k], str) for k in ('backup_path', 'receipt'))
            and (job['backup_sha256'] is None or _sha(job['backup_sha256'])), 'unknown job metadata/closure')
    require(job['status'] != 'active' or bool(job['backup_path'] or job['backup_sha256'] or job['receipt']),
            'active job must declare its backup input/output')


def collect_registered_references(registry, observed_jobs, state, contract_sha, *, now=None):
    """Producer after authenticated FD reads; stage names must all be registered."""
    require(registry['contract_sha256'] == contract_sha and set(observed_jobs) <= set(registry['jobs']),
            'unregistered or changed activity namespace')
    evidence = {'type': 'tianming.nas-active-references.v1', 'state_sha256': digest(state),
                'contract_sha256': contract_sha, 'observed_at': (now or datetime.now(timezone.utc)).isoformat(),
                'active_paths': [], 'active_receipts': [], 'active_sha256': [],
                'rehearsal_paths': [], 'rehearsal_sha256': [], 'uploads': []}
    for key, job in registry['jobs'].items():
        validate_job(job)
        require(key == job['job_id'], 'job identity changed')
        if job['status'] != 'active': continue
        prefix = 'rehearsal' if job['kind'] == 'rehearsal' else 'active'
        if job['backup_path']: evidence[prefix + '_paths'].append(job['backup_path'])
        if job['backup_sha256']: evidence[prefix + '_sha256'].append(job['backup_sha256'])
        if job['receipt']: evidence['active_receipts'].append(job['receipt'])
        if job['kind'] in ('upload', 'backup'): evidence['uploads'].append(key)
    return evidence


def validate_references(evidence, state, contract_sha):
    fields = {'type', 'state_sha256', 'contract_sha256', 'observed_at', 'active_paths', 'active_receipts',
              'active_sha256', 'rehearsal_paths', 'rehearsal_sha256', 'uploads'}
    require(isinstance(evidence, dict) and set(evidence) == fields
            and evidence['type'] == 'tianming.nas-active-references.v1'
            and evidence['state_sha256'] == digest(state) and evidence['contract_sha256'] == contract_sha,
            'complete active/rehearsal reference collection missing or stale')
    when = datetime.fromisoformat(evidence['observed_at'])
    now = datetime.now(timezone.utc)
    require(when.utcoffset() is not None and now - timedelta(seconds=60) <= when <= now + timedelta(seconds=5),
            'active reference observation must be fresh')
    for field in ('active_paths', 'active_receipts', 'active_sha256', 'rehearsal_paths', 'rehearsal_sha256', 'uploads'):
        require(isinstance(evidence[field], list) and all(isinstance(v, str) and v for v in evidence[field]),
                'active reference schema incomplete')
    require(not evidence['uploads'], 'active or unconfirmed upload')
    for field in ('active_sha256', 'rehearsal_sha256'):
        require(all(_sha(v) for v in evidence[field]), 'invalid active/rehearsal SHA')
    return {key: evidence[key] for key in fields - {'type', 'state_sha256', 'contract_sha256', 'observed_at'}}


def _protected_version(version, state, active=None):
    receipt, pointer = version['receipt'], version['pointer']
    stream = state.get('last_backup_stream') or {}
    active = active or {}
    filenames = [Path(p).name for p in [state.get('last_backup') or '', stream.get('path') or '',
                                      *active.get('active_paths', []), *active.get('rehearsal_paths', [])]]
    receipt_names = [stream.get('receipt') or '', *active.get('active_receipts', [])]
    return (receipt['sha256'] in _protected_sha(state) | set(active.get('active_sha256', [])) | set(active.get('rehearsal_sha256', []))
            or stream.get('receipt_sha256') == pointer['receipt_sha256']
            or any(_comparison_key(name) == _comparison_key(pointer['receipt']) for name in receipt_names)
            or any(_comparison_key(name) == _comparison_key(receipt['filename']) for name in filenames))


def stage_candidate(adapter, pointer):
    """Register at most one authenticated new package; never replace old kept."""
    receipt = adapter.load_receipt(pointer)
    with adapter.manager.lock():
        expected, index = adapter.read_index()
        state = adapter.state()
        stream = state.get('last_backup_stream') or {}
        require(stream.get('receipt') == pointer['receipt'] and stream.get('receipt_sha256') == pointer['receipt_sha256'],
                'staged pointer is not the published active backup')
        require(index['contract_sha256'] == receipt['contract_sha256'] and not index['retired'],
                'complete pending retirement before staging another version')
        require(index['staged'] in (None, pointer), 'another staged job needs authenticated owner closure')
        if pointer in index['kept'] or index['staged'] == pointer: return expected
        return adapter.cas_index(expected, dict(index, generation=index['generation'] + 1, staged=dict(pointer)))


def pre_backup_gate(adapter, *, bootstrap_pointer=None):
    """Call INSIDE existing lock, before creating another complete package.

    Normal runs refuse queued retirement, unfinished staging and a published but
    unindexed receipt. Explicit first bootstrap admits only a current authenticated
    first version whose real independent recovery has already passed; not an
    unconditional Boolean bypass. ERP business continues while backup is blocked.
    """
    _, index = adapter.read_index()
    state = adapter.state()
    require(not adapter.active_references(state)['uploads'], 'active/unconfirmed upload must finish before another backup')
    require(not index['retired'] and index['staged'] is None, 'resume staged/retired work before another backup')
    published = state.get('last_backup_stream') or {}
    p = {'receipt': published.get('receipt'), 'receipt_sha256': published.get('receipt_sha256')}
    _validate_pointer(p)
    if not index['kept']:
        require(index['bootstrap'] is None, 'bootstrap already reserved; resume its fixed task instead of generating another package')
        require(bootstrap_pointer == p, 'initial two-version bootstrap requires explicit authenticated first version')
        adapter.load_verified(p)
        reservation = {'first': dict(p), 'task_id': uuid.uuid4().hex}
        expected, fresh = adapter.read_index()
        require(fresh == index, 'bootstrap index changed')
        saved = adapter.cas_index(expected, dict(index, generation=index['generation'] + 1, bootstrap=reservation))
        return {'status': 'bootstrap_second_allowed', 'first': p, 'task_id': reservation['task_id'], 'index_sha256': saved}
    require(p == index['kept'][0], 'published receipt not yet taken over by rotation; resume it first')
    return {'status': 'allowed', 'kept': list(index['kept'])}


def advance(adapter, candidate=None, *, bootstrap=()):
    """After isolated verification/business resume, persist pair and retirement.

    Large ciphertext IO is exclusively in the later remote worker. Failed or
    missing recovery leaves the old index bytes unchanged. A third staged pointer
    may be registered by its owner separately; it never replaces validated kept.
    This function does not create backups, change worker pause flags or delete.
    """
    expected, index = adapter.read_index()
    pointers = list(index['kept']) or list(bootstrap)
    require(len(pointers) == 2, 'two authenticated initial versions required')
    if index['bootstrap'] is not None:
        require(index['bootstrap']['first'] in pointers, 'bootstrap first version may not be reinterpreted')
    if candidate is not None and candidate not in pointers: pointers.append(candidate)
    require(len(pointers) <= 3, 'only 2+1 versions may be examined')
    require(index['staged'] in (None, candidate), 'another staged job needs owner closure')
    require(not index['retired'] or candidate in index['kept'] or candidate is None,
            'complete pending retirement before another rotation')
    verified = [adapter.load_verified(pointer) for pointer in pointers]
    require(all(v['contract_sha256'] == index['contract_sha256'] for v in verified), 'storage contract changed')
    verified.sort(key=lambda v: datetime.fromisoformat(v['source_time']), reverse=True)
    require(len({v['receipt']['sha256'] for v in verified}) == len(verified)
            and len({datetime.fromisoformat(v['source_time']) for v in verified}) == len(verified),
            'distinct recovery business points required')
    kept = [v['pointer'] for v in verified[:2]]
    if candidate is not None: require(candidate in kept, 'candidate is not a newer authenticated business point')
    retired = list(index['retired'])
    for version in verified[2:]:
        if version['pointer'] not in retired: retired.append(version['pointer'])
    with adapter.manager.lock():
        state = adapter.state()
        active = adapter.active_references(state)
        require(not any(_protected_version(v, state, active) for v in verified[2:]),
                'retired backup is still a restore/migration runtime reference')
        latest = verified[0]['receipt']
        if candidate is not None:
            stream = state.get('last_backup_stream') or {}
            require(stream.get('receipt') == candidate['receipt']
                    and stream.get('receipt_sha256') == candidate['receipt_sha256']
                    and state.get('last_backup') == latest['local_copy']['path'], 'active backup publication changed')
        updated = dict(index, generation=index['generation'] + 1, kept=kept, staged=None, retired=retired, bootstrap=None)
        # A replay does not append, forget or re-delete retirement work.
        if kept == index['kept'] and retired == index['retired'] and index['staged'] is None:
            return {'status': 'unchanged', 'index_sha256': expected, 'retired': retired}
        saved = adapter.cas_index(expected, updated)
    return {'status': 'indexed', 'index_sha256': saved, 'retired': retired}
